import csv
import io
import json
from datetime import date

from django.db import transaction
from pydantic import ValidationError

from quest.models import DatasetState, Employee, Event, Participation, RoleProfile, Skill
from quest.schemas import EmployeeInput, EventInput, HistoryInput, RoleInput, SkillInput


class ImportFailure(ValueError):
    pass


def parse_rows(raw, section, schema, csv_mode=False):
    if raw is None:
        return [], None
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        if csv_mode:
            rows = list(csv.DictReader(io.StringIO(text)))
            rows = [
                {
                    k: (None if v == "" and k in {"due_date", "score", "feedback_rating"} else v)
                    for k, v in row.items()
                }
                for row in rows
            ]
            snapshot = None
        else:
            payload = json.loads(text)
            rows = payload if isinstance(payload, list) else payload[section]
            metadata = payload.get("meta", {}) if isinstance(payload, dict) else {}
            if not isinstance(metadata, dict):
                raise ValueError("meta должен быть объектом")
            snapshot = metadata.get("as_of_date")
        if not isinstance(rows, list):
            raise ValueError("Ожидается массив записей")
        if len(rows) > 20000:
            raise ValueError("Слишком много записей: максимум 20 000 в файле")
        parsed = []
        for number, row in enumerate(rows, start=1):
            try:
                parsed.append(schema.model_validate(row).model_dump(mode="json"))
            except ValidationError as exc:
                first = exc.errors(include_input=False)[0]
                field = ".".join(map(str, first["loc"]))
                raise ImportFailure(f"{section}, запись {number}, {field}: {first['msg']}") from None
        return parsed, date.fromisoformat(snapshot) if snapshot else None
    except ImportFailure:
        raise
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise ImportFailure(
            f"Не удалось прочитать {section}: проверьте UTF-8 и схему файла ({type(exc).__name__})."
        ) from None


def unique(rows, field):
    seen = set()
    for row in rows:
        value = row[field] if isinstance(field, str) else tuple(row[k] for k in field)
        if value in seen:
            raise ImportFailure(f"Повтор идентификатора в файле: {value}")
        seen.add(value)
    return seen


@transaction.atomic
def import_dataset(*, employees=None, history=None, skills=None, events=None):
    erows, esnap = parse_rows(employees, "employees", EmployeeInput)
    hrows, _ = parse_rows(history, "activity_history", HistoryInput, csv_mode=True)
    srows, ssnap = parse_rows(skills, "skills", SkillInput)
    rrows, _ = parse_rows(skills, "role_profiles", RoleInput)
    vrows, vsnap = parse_rows(events, "events", EventInput)
    state, _ = DatasetState.objects.get_or_create(key="main")
    state = DatasetState.objects.select_for_update().get(pk=state.pk)
    snapshots = {x for x in (esnap, ssnap, vsnap) if x}
    if len(snapshots) > 1 or (snapshots and Employee.objects.exists() and state.as_of_date not in snapshots):
        raise ImportFailure("Дата среза должна совпадать с текущим набором данных.")
    if snapshots:
        state.as_of_date = snapshots.pop()
    known_skills = set(Skill.objects.values_list("pk", flat=True)) | unique(srows, "skill_id")
    known_roles = set(RoleProfile.objects.values_list("role", "grade")) | unique(rrows, ("role", "grade"))
    known_employees = set(Employee.objects.values_list("pk", flat=True)) | unique(erows, "employee_id")
    known_events = set(Event.objects.values_list("pk", flat=True)) | unique(vrows, "event_id")
    unique(hrows, "record_id")

    def check_skills(keys, where):
        missing = set(keys) - known_skills
        if missing:
            raise ImportFailure(f"{where}: неизвестные навыки {', '.join(sorted(missing))}")

    for r in rrows:
        check_skills(r["required_skills"], r["role"])
        if set(r["critical_skills"]) - set(r["required_skills"]):
            raise ImportFailure("critical_skills должны входить в required_skills")
    for e in erows:
        if (e["role"], e["grade"]) not in known_roles:
            raise ImportFailure(f"{e['employee_id']}: неизвестная роль / грейд")
        goal = e["career_goal"]
        if goal and (goal["target_role"], goal["target_grade"]) not in known_roles:
            raise ImportFailure(f"{e['employee_id']}: неизвестная карьерная цель")
        check_skills(e["skills"], e["employee_id"])
        if e["manager_id"] and e["manager_id"] not in known_employees:
            raise ImportFailure(f"{e['employee_id']}: неизвестный manager_id")
        if date.fromisoformat(e["last_review_date"]) > state.as_of_date:
            raise ImportFailure(f"{e['employee_id']}: аттестация позже даты среза")
    for v in vrows:
        check_skills(v["prerequisites"], v["event_id"])
        check_skills([x["skill_id"] for x in v["develops_skills"]], v["event_id"])
        unique(v["develops_skills"], "skill_id")
    existing = {
        p.record_id: p for p in Participation.objects.filter(record_id__in=[r["record_id"] for r in hrows])
    }
    for h in hrows:
        if h["employee_id"] not in known_employees or h["event_id"] not in known_events:
            raise ImportFailure(f"{h['record_id']}: неизвестный сотрудник или мероприятие")
        if date.fromisoformat(h["date"]) > state.as_of_date:
            raise ImportFailure(f"{h['record_id']}: история позже даты среза")
        old = existing.get(h["record_id"])
        if old and (old.employee_id != h["employee_id"] or old.event_id != h["event_id"] or old.request_id):
            raise ImportFailure(f"{h['record_id']}: конфликт с существующей записью")
    counts = {}
    for model, rows, key in [
        (Skill, srows, "skill_id"),
        (Event, vrows, "event_id"),
        (Employee, erows, "employee_id"),
        (Participation, hrows, "record_id"),
    ]:
        created = 0
        for row in rows:
            values = dict(row)
            pk = values.pop(key)
            _, new = model.objects.update_or_create(**{key: pk}, defaults=values)
            created += int(new)
        counts[model.__name__] = {"processed": len(rows), "created": created}
    for r in rrows:
        RoleProfile.objects.update_or_create(
            role=r["role"],
            grade=r["grade"],
            defaults={"required_skills": r["required_skills"], "critical_skills": r["critical_skills"]},
        )
    counts["RoleProfile"] = {"processed": len(rrows)}
    state.revision += 1
    state.save()
    return counts
