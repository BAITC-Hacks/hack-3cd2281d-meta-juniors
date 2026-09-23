from collections import Counter
from datetime import date

from quest.models import DatasetState, DevelopmentPlanItem, Event, RoleProfile, Skill

GRADES = ["Junior", "Middle", "Senior", "Lead"]
FORMAT_NAMES = {"online": "онлайн", "offline": "очно", "self_paced": "в своём темпе"}
STATUS_NAMES = {
    "completed": "Завершено",
    "in_progress": "В процессе",
    "dropped": "Прервано",
    "no_show": "Пропуск",
    "declined": "Отказ",
    "overdue": "Просрочено",
}


def catalog():
    state = DatasetState.objects.first()
    return {
        "today": state.as_of_date if state else date(2026, 10, 1),
        "revision": state.revision if state else 0,
        "skills": {s.pk: s for s in Skill.objects.all()},
        "roles": {(r.role, r.grade): r for r in RoleProfile.objects.all()},
        "events": {e.pk: e for e in Event.objects.all()},
    }


def apply_gain(levels, event):
    updated = dict(levels)
    for effect in event.develops_skills:
        key = effect["skill_id"]
        previous = updated.get(key, 0)
        updated[key] = max(previous, min(5, effect["max_level"], previous + effect["gain"]))
    return updated


def current_skills(employee, history, today):
    levels = dict(employee.skills)
    applied = []
    for row in sorted(history, key=lambda r: (r.date, r.record_id)):
        after_review = row.date > employee.last_review_date or (
            row.request_id and row.date == employee.last_review_date
        )
        if row.status == "completed" and after_review and row.date <= today:
            before = levels
            levels = apply_gain(levels, row.event)
            applied.append(
                {
                    "record_id": row.record_id,
                    "event_id": row.event_id,
                    "changes": {k: [before.get(k, 0), v] for k, v in levels.items() if v != before.get(k, 0)},
                }
            )
    return levels, applied


def target_for(employee, cat):
    if employee.career_goal:
        g = employee.career_goal
        return cat["roles"].get((g["target_role"], g["target_grade"])), False
    idx = GRADES.index(employee.grade)
    if idx == len(GRADES) - 1:
        return None, False
    return cat["roles"].get((employee.role, GRADES[idx + 1])), True


def eligibility(employee, event, levels, history, today):
    reasons = []
    if event.for_employee_id and event.for_employee_id != employee.pk:
        reasons.append("Индивидуальная практика другого сотрудника")
    if event.mandatory:
        reasons.append("Обязательное мероприятие")
    if employee.role not in event.target_roles:
        reasons.append("Не подходит текущая роль")
    if employee.grade not in event.target_grades:
        reasons.append("Не подходит текущий грейд")
    if any(levels.get(k, 0) < v for k, v in event.prerequisites.items()):
        reasons.append("Не выполнены требования к участию")
    if event.format != "self_paced" and not any(
        date.fromisoformat(s) >= today for s in event.upcoming_sessions
    ):
        reasons.append("Нет предстоящих сессий")
    completed = [h for h in history if h.event_id == event.pk and h.status == "completed"]
    if completed and event.pk != "EV_036":
        reasons.append("Уже завершено")
    if event.pk == "EV_036" and any(h.date == today for h in completed):
        reasons.append("Клуб уже завершён в эту дату")
    if any(h.event_id == event.pk and h.status == "in_progress" for h in history):
        reasons.append("Уже в процессе")
    return reasons


def profile_context(employee, cat=None, history=None):
    cat = cat or catalog()
    history = list(history if history is not None else employee.history.select_related("event").all())
    levels, applied = current_skills(employee, history, cat["today"])
    target, inferred = target_for(employee, cat)
    gaps = []
    if target:
        for key, required in target.required_skills.items():
            value = levels.get(key, 0)
            gaps.append(
                {
                    "skill_id": key,
                    "name": cat["skills"][key].name,
                    "current": value,
                    "baseline": employee.skills.get(key, 0),
                    "required": required,
                    "gap": max(0, required - value),
                    "critical": key in target.critical_skills,
                    "width": value * 20,
                    "target_width": required * 20,
                }
            )
    gaps.sort(key=lambda x: (not x["critical"], -x["gap"], x["name"]))
    total = sum(g["required"] for g in gaps)
    coverage = round(sum(min(g["current"], g["required"]) for g in gaps) / total * 100) if total else 0
    voluntary = [h for h in history if not h.event.mandatory and h.date <= cat["today"]]
    return {
        "employee": employee,
        "cat": cat,
        "history": history,
        "levels": levels,
        "applied": applied,
        "target": target,
        "inferred_goal": inferred,
        "gaps": gaps,
        "coverage": coverage,
        "critical_gaps": [g for g in gaps if g["critical"] and g["gap"]],
        "open_gaps": [g for g in gaps if g["gap"]],
        "voluntary": voluntary,
        "completed_count": sum(h.status == "completed" for h in voluntary),
    }


def candidates_for(ctx, include_planned=False):
    employee, cat, levels, history = ctx["employee"], ctx["cat"], ctx["levels"], ctx["history"]
    gaps = {g["skill_id"]: g for g in ctx["gaps"] if g["gap"]}
    candidates, exclusions = [], Counter()
    planned = (
        set()
        if include_planned
        else set(
            DevelopmentPlanItem.objects.filter(employee=employee, status="planned").values_list(
                "event_id", flat=True
            )
        )
    )
    for event in cat["events"].values():
        reasons = eligibility(employee, event, levels, history, cat["today"])
        if event.pk in planned:
            reasons.append("Уже в личном плане")
        if reasons:
            exclusions.update(reasons)
            continue
        after = apply_gain(levels, event)
        benefits = [
            {
                "skill_id": k,
                "name": g["name"],
                "before": levels.get(k, 0),
                "after": after.get(k, 0),
                "required": g["required"],
                "critical": g["critical"],
                "closes": min(g["gap"], after.get(k, 0) - levels.get(k, 0)),
            }
            for k, g in gaps.items()
            if after.get(k, 0) > levels.get(k, 0)
        ]
        if not benefits:
            exclusions["Не сокращает разрыв к цели"] += 1
            continue
        relevant = [
            h for h in ctx["voluntary"] if h.event.type == event.type and h.event.format == event.format
        ]
        recent = [h for h in relevant if (cat["today"] - h.date).days <= 180]
        counts = Counter(h.status for h in recent)
        same_event = [h for h in ctx["voluntary"] if h.event_id == event.pk]
        same_failures = sum(h.status in {"no_show", "dropped", "declined"} for h in same_event)
        skipped = counts["no_show"] + counts["dropped"] + counts["declined"]
        critical_benefit = sum(b["closes"] for b in benefits if b["critical"])
        gain_score = sum(b["closes"] * (4 if b["critical"] else 1) for b in benefits)
        reliability = (counts["completed"] + 1) / (len(recent) + 2)
        remote_fit = employee.work_format == "remote" and event.format != "offline"
        score = gain_score * 10 + reliability * 4 - min(skipped, 5) * 1.2 - same_failures * 2
        score += (2 if remote_fit else 0) - min(event.duration_hours, 40) * 0.05
        fact_list = []

        def fact(suffix, category, text):
            fact_list.append({"id": f"{event.pk}:{suffix}", "category": category, "text": text})

        for i, benefit in enumerate(benefits):
            suffix = " Критично для целевого грейда." if benefit["critical"] else ""
            fact(
                f"gap{i}",
                "gap",
                f"{benefit['name']}: {benefit['before']} → {benefit['after']} при требуемых {benefit['required']}.{suffix}",
            )
        fact(
            "history",
            "history",
            f"За 180 дней в активностях такого типа и формата: завершено {counts['completed']}, пропусков/отказов/прерываний {skipped}."
            if recent
            else "За 180 дней нет истории активностей такого типа и формата; предпочтение пока неизвестно.",
        )
        if same_failures:
            fact(
                "repeat",
                "history",
                f"В истории этой активности есть {same_failures} пропусков, отказов или прерываний. Причины в данных не указаны.",
            )
        fact(
            "effort", "effort", f"Нагрузка: {event.duration_hours:g} ч. Формат: {FORMAT_NAMES[event.format]}."
        )
        if remote_fit:
            fact("format", "format", "Формат доступен дистанционно и совместим с удалённой работой.")
        sessions = sorted(s for s in event.upcoming_sessions if date.fromisoformat(s) >= cat["today"])
        candidates.append(
            {
                "event_id": event.pk,
                "title": event.title,
                "description": event.description,
                "format": FORMAT_NAMES[event.format],
                "type": event.type,
                "duration_hours": event.duration_hours,
                "next_session": sessions[0] if sessions else "В любое время",
                "benefits": benefits,
                "facts": fact_list,
                "score": round(score, 2),
                "critical_benefit": critical_benefit,
                "history_records": [h.record_id for h in recent[-8:]],
                "same_event_failures": same_failures,
            }
        )
    candidates.sort(key=lambda c: (-c["score"], c["event_id"]))
    covered = {b["skill_id"] for c in candidates for b in c["benefits"]}
    uncovered = [g["name"] for g in ctx["open_gaps"] if g["skill_id"] not in covered]
    return candidates, dict(exclusions), uncovered


def hr_summary(employees, *, attention_only=False, skill_id=""):
    cat = catalog()
    profiles = []
    deficits = {}
    for employee in employees:
        ctx = profile_context(employee, cat, list(employee.history.all()))
        recent = [r for r in ctx["voluntary"] if (cat["today"] - r.date).days <= 90]
        unfinished = sum(r.status in {"no_show", "dropped", "declined"} for r in recent)
        completed = sum(r.status == "completed" for r in recent)
        attention = unfinished >= 2 or completed == 0
        if attention_only and not attention:
            continue
        if skill_id and not any(g["skill_id"] == skill_id for g in ctx["open_gaps"]):
            continue
        for g in ctx["open_gaps"]:
            entry = deficits.setdefault(
                g["skill_id"],
                {
                    "skill_id": g["skill_id"],
                    "name": g["name"],
                    "people": 0,
                    "critical_people": 0,
                    "total_gap": 0,
                },
            )
            entry["people"] += 1
            entry["critical_people"] += int(g["critical"])
            entry["total_gap"] += g["gap"]
        profiles.append(
            {
                "employee": employee,
                "coverage": ctx["coverage"],
                "target": ctx["target"],
                "critical_count": len(ctx["critical_gaps"]),
                "attention": attention,
                "completed90": completed,
                "unfinished90": unfinished,
            }
        )
    deficits = sorted(deficits.values(), key=lambda d: (-d["critical_people"], -d["people"], d["name"]))
    count = len(profiles)
    for d in deficits:
        d["width"] = round(d["people"] / count * 100) if count else 0
    return {
        "profiles": profiles,
        "deficits": deficits[:10],
        "count": count,
        "attention_count": sum(p["attention"] for p in profiles),
        "average_coverage": round(
            sum(p["coverage"] for p in profiles if p["target"])
            / max(1, sum(bool(p["target"]) for p in profiles))
        ),
        "today": cat["today"],
    }
