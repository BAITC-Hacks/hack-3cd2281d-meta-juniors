import uuid

from django.db.models import F
from django.utils import timezone

from quest.models import (
    DatasetState,
    DevelopmentRequest,
    Event,
    Participation,
    PracticeBrief,
    RecommendationCache,
    WorkSubmission,
)
from quest.services.career import profile_context
from quest.services.plans import PlanConflict

ACTIVE_PLAN_STATUSES = ["planned", "in_progress", "submitted", "needs_revision"]


def practice_template(skill):
    if skill.pk == "SK_SYSTEM_DESIGN":
        return {
            "title": "Спроектировать надёжный сервис уведомлений",
            "instructions": (
                "Учебный кейс: сервис отправляет до 10 000 уведомлений в минуту по email и push. "
                "Провайдер иногда недоступен, а одно сообщение может поступить повторно. "
                "Нужно сохранить уведомления и исключить повторную отправку там, где это возможно.\n\n"
                "1. Определи требования: допустимую задержку, ограничения и предположения.\n"
                "2. Нарисуй компоненты: API, очередь, хранилище, обработчики, внешние провайдеры. Покажи путь сообщения.\n"
                "3. Предложи ключ идемпотентности и объясни, где хранится состояние отправки.\n"
                "4. Разбери отказ провайдера: повторные попытки с задержкой, ограничение попыток и очередь ошибок.\n"
                "5. Объясни масштабирование обработчиков и выбери метрики для контроля задержек и потерь.\n"
                "6. Сравни минимум два варианта решения и обоснуй компромиссы. Код необязателен: важна аргументация."
            ),
            "deliverable": "Схема архитектуры и пояснение на 1–2 страницы: поток данных, повторы, отказы, масштабирование и компромиссы. Схему можно приложить ссылкой; основные решения изложи текстом.",
            "criteria": "Показаны компоненты и полный путь уведомления\nОбъяснены идемпотентность и обработка повторов\nОписаны отказы, повторные попытки и очередь ошибок\nОбоснованы масштабирование, метрики и минимум один компромисс",
            "duration_hours": 4,
        }
    return {
        "title": f"Практика: {skill.name}",
        "instructions": f"Примени навык «{skill.name}» к конкретной рабочей ситуации.\n1. Опиши задачу и ограничения.\n2. Предложи два варианта решения и обоснуй выбор.\n3. Подготовь результат: документ, пример решения или демонстрацию.\n4. Проверь результат на примере и опиши, что изменилось после проверки.\n5. Сформулируй ограничения и следующий шаг. HR уточняет предметную задачу перед публикацией.",
        "deliverable": "Конкретный результат работы и короткое объяснение: задача, выбранное решение, способ проверки и выводы. При необходимости добавь ссылку на документ.",
        "criteria": "Задача и ограничения описаны конкретно\nВыбор решения обоснован и связан с развиваемым навыком\nЕсть проверяемый результат и объяснение его проверки",
        "duration_hours": 3,
    }


def brief_for(event):
    brief = PracticeBrief.objects.filter(event=event).first()
    if brief:
        return {
            "instructions": brief.instructions,
            "deliverable": brief.deliverable,
            "criteria": brief.criteria,
            "custom": True,
        }
    return {
        "instructions": f"Активность из каталога: {event.description}\n\n1. Пройди мероприятие через его организатора.\n2. Выбери приём или подход, который можешь применить в работе.\n3. Подготовь небольшой пример применения и опиши его результат.\n4. Отправь отчёт и подтверждение участия на проверку HR.",
        "deliverable": "Краткий отчёт: что изучено, как применено и что получилось. Добавь ссылку на результат или подтверждение прохождения. Учебные материалы самого мероприятия в стартовом наборе не предоставлены.",
        "criteria": [
            "Прохождение активности подтверждено проверяющим",
            "Приведён конкретный пример применения изученного",
            "Результат связан с навыками указанного мероприятия",
        ],
        "custom": False,
    }


def create_request(employee, skill_id, note):
    """All mutating functions here run under the caller's employee row lock."""
    ctx = profile_context(employee)
    gap = next((g for g in ctx["open_gaps"] if g["skill_id"] == skill_id), None)
    if not gap:
        raise PlanConflict("Этот навык уже достигнут или не входит в текущую цель. Обнови страницу.")
    existing = employee.development_requests.filter(
        skill_id=skill_id, status__in=["open", "proposed"]
    ).first()
    if existing:
        return existing
    return DevelopmentRequest.objects.create(
        employee=employee,
        skill_id=skill_id,
        current_level=gap["current"],
        required_level=gap["required"],
        note=note,
        target={"role": ctx["target"].role, "grade": ctx["target"].grade},
    )


def publish_practice(development_request, actor, data):
    if development_request.status != "open":
        raise PlanConflict("Этот запрос уже обработан.")
    employee = development_request.employee
    ctx = profile_context(employee)
    gap = next((g for g in ctx["open_gaps"] if g["skill_id"] == development_request.skill_id), None)
    if not gap:
        raise PlanConflict(
            "Потребность изменилась: разрыва по этому навыку больше нет. Закрой запрос с объяснением."
        )
    if (
        not gap["current"] < data["max_level"] <= gap["required"]
        or data["gain"] > gap["required"] - gap["current"]
    ):
        raise PlanConflict("Прирост и потолок должны соответствовать текущему разрыву сотрудника.")
    event = Event.objects.create(
        event_id=f"CQ_{uuid.uuid4().hex[:16]}",
        title=data["title"],
        description=data["deliverable"],
        type="workshop",
        format="self_paced",
        duration_hours=data["duration_hours"],
        for_employee=employee,
        target_roles=[employee.role],
        target_grades=[employee.grade],
        prerequisites={development_request.skill_id: gap["current"]},
        develops_skills=[
            {"skill_id": development_request.skill_id, "gain": data["gain"], "max_level": data["max_level"]}
        ],
    )
    PracticeBrief.objects.create(
        event=event,
        instructions=data["instructions"],
        deliverable=data["deliverable"],
        criteria=data["criteria"],
        approved_by=actor,
    )
    development_request.event, development_request.status, development_request.handled_by = (
        event,
        "proposed",
        actor,
    )
    development_request.save(update_fields=["event", "status", "handled_by", "updated_at"])
    DatasetState.objects.filter(key="main").update(revision=F("revision") + 1)
    RecommendationCache.objects.filter(employee=employee).delete()
    return event


def submit_work(item, data):
    if item.status not in {"in_progress", "needs_revision"} or not item.participation_id:
        raise PlanConflict("Результат можно отправить из начатого шага или после доработки.")
    last = item.submissions.first()
    attempt = last.attempt if last else 0
    if data["previous_attempt"] != attempt:
        raise PlanConflict(
            "Работа уже обновилась. Открой страницу заново, чтобы отправить актуальную версию."
        )
    submission = WorkSubmission.objects.create(
        plan_item=item,
        attempt=attempt + 1,
        body=data["body"],
        artifact_url=data["artifact_url"],
        criteria=brief_for(item.event)["criteria"],
    )
    item.status = "submitted"
    item.save(update_fields=["status", "updated_at"])
    return submission


def review_work(submission, actor, data):
    item = submission.plan_item
    if item.employee.user_id == actor.pk:
        raise PlanConflict("Свою работу нельзя подтвердить самостоятельно.")
    if submission.status != "pending":
        return False
    if item.status != "submitted" or item.submissions.first().pk != submission.pk:
        raise PlanConflict("На проверку уже отправлена другая версия работы.")
    approve = data["decision"] == "approve"
    if approve and set(data["checked_criteria"]) != {str(i) for i in range(len(submission.criteria))}:
        raise PlanConflict("Нужно проверить все критерии.")
    if approve:
        ctx = profile_context(item.employee)
        record = item.participation
        if not record or record.status != "in_progress" or item.event.mandatory:
            raise PlanConflict("История участия изменилась. Завершение недоступно.")
        completed = Participation.objects.filter(employee=item.employee, event=item.event, status="completed")
        if (
            completed.filter(date=ctx["cat"]["today"]).exists()
            if item.event_id == "EV_036"
            else completed.exists()
        ):
            raise PlanConflict("Эта активность уже учтена как завершённая.")
        record.status, record.completion_pct, record.date = "completed", 100, ctx["cat"]["today"]
        record.request_id = uuid.uuid4()
        record.save(update_fields=["status", "completion_pct", "date", "request_id"])
        updated = profile_context(item.employee)
        submission.result = {
            "coverage_before": ctx["coverage"],
            "coverage_after": updated["coverage"],
            "as_of_date": str(ctx["cat"]["today"]),
            "target": {"role": ctx["target"].role, "grade": ctx["target"].grade} if ctx["target"] else None,
            "changes": [
                {"name": ctx["cat"]["skills"][k].name, "before": ctx["levels"].get(k, 0), "after": v}
                for k, v in updated["levels"].items()
                if v != ctx["levels"].get(k, 0)
            ],
        }
        DevelopmentRequest.objects.filter(employee=item.employee, event=item.event, status="proposed").update(
            status="completed", updated_at=timezone.now()
        )
        item.status, submission.status = "completed", "approved"
        RecommendationCache.objects.filter(employee=item.employee).delete()
    else:
        item.status, submission.status = "needs_revision", "changes_requested"
    submission.feedback, submission.checked_criteria = data["feedback"], data["checked_criteria"]
    submission.reviewer, submission.reviewed_at = actor, timezone.now()
    submission.save()
    item.save(update_fields=["status", "updated_at"])
    return True
