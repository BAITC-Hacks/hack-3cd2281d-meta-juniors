import csv
import hashlib
import io
import json
from datetime import date

from django.db import transaction
from pydantic import ValidationError

from quest.models import (
    DatasetState,
    Employee,
    Event,
    Participation,
    PracticeBrief,
    RoleProfile,
    Skill,
    WorkSubmission,
)
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


def prepare_dataset(*, employees=None, history=None, skills=None, events=None):
    erows, esnap = parse_rows(employees, "employees", EmployeeInput)
    hrows, _ = parse_rows(history, "activity_history", HistoryInput, csv_mode=True)
    srows, ssnap = parse_rows(skills, "skills", SkillInput)
    rrows, _ = parse_rows(skills, "role_profiles", RoleInput)
    vrows, vsnap = parse_rows(events, "events", EventInput)
    state = DatasetState.objects.filter(key="main").first() or DatasetState(as_of_date=date(2026, 10, 1))
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
    practice_events = set(PracticeBrief.objects.values_list("event_id", flat=True))
    for v in vrows:
        if v["event_id"] in practice_events:
            raise ImportFailure(
                f"{v['event_id']}: индивидуальная практика утверждена HR и защищена от перезаписи импортом"
            )
        check_skills(v["prerequisites"], v["event_id"])
        check_skills([x["skill_id"] for x in v["develops_skills"]], v["event_id"])
        unique(v["develops_skills"], "skill_id")
    existing = {
        p.record_id: p for p in Participation.objects.filter(record_id__in=[r["record_id"] for r in hrows])
    }
    reviewed_records = set(
        WorkSubmission.objects.filter(plan_item__participation_id__in=existing).values_list(
            "plan_item__participation_id", flat=True
        )
    )
    for h in hrows:
        if h["employee_id"] not in known_employees or h["event_id"] not in known_events:
            raise ImportFailure(f"{h['record_id']}: неизвестный сотрудник или мероприятие")
        if date.fromisoformat(h["date"]) > state.as_of_date:
            raise ImportFailure(f"{h['record_id']}: история позже даты среза")
        if h["event_id"] in practice_events:
            raise ImportFailure(
                f"{h['record_id']}: результат индивидуальной практики подтверждается через проверку HR"
            )
        old = existing.get(h["record_id"])
        if h["record_id"] in reviewed_records:
            raise ImportFailure(f"{h['record_id']}: для активности уже отправлена работа на проверку")
        if old and (
            old.employee_id != h["employee_id"]
            or old.event_id != h["event_id"]
            or old.request_id
            or old.pk.startswith("PLAN_")
        ):
            raise ImportFailure(f"{h['record_id']}: конфликт с существующей записью")
    groups = [
        (Skill, srows, ("skill_id",)),
        (Event, vrows, ("event_id",)),
        (Employee, erows, ("employee_id",)),
        (Participation, hrows, ("record_id",)),
        (RoleProfile, rrows, ("role", "grade")),
    ]
    return state, groups


def summarize_dataset(state, groups):
    counts = {}
    baseline = {"revision": state.revision, "as_of_date": str(state.as_of_date), "rows": []}
    affected = set()
    incoming_people = {}
    for model, rows, keys in groups:
        counts[model.__name__] = {"processed": len(rows), "created": 0, "updated": 0, "unchanged": 0}
        existing = {
            tuple(str(getattr(obj, k)) for k in keys): obj
            for obj in model.objects.filter(**{f"{keys[0]}__in": [r[keys[0]] for r in rows]})
        }
        for row in rows:
            key = tuple(str(row[k]) for k in keys)
            old = existing.get(key)
            values = {k: getattr(old, k) for k in row} if old else None
            values = json.loads(json.dumps(values, default=str))
            status = "created" if old is None else "unchanged" if values == row else "updated"
            counts[model.__name__][status] += 1
            baseline["rows"].append([model.__name__, key, values])
            if model == Employee:
                incoming_people[row["employee_id"]] = row
                affected.add(row["employee_id"])
            if model == Participation:
                affected.add(row["employee_id"])
    current_people = {e.pk: e for e in Employee.objects.filter(pk__in=affected)}
    profiles = []
    for pk in sorted(affected):
        row, old = incoming_people.get(pk), current_people.get(pk)
        profiles.append(
            {"employee_id": pk, "full_name": row["full_name"] if row else old.full_name, "new": old is None}
        )
    fingerprint = hashlib.sha256(json.dumps(baseline, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "counts": counts,
        "profiles": profiles,
        "fingerprint": fingerprint,
        "as_of_date": str(state.as_of_date),
    }


def preview_dataset(**files):
    return summarize_dataset(*prepare_dataset(**files))


@transaction.atomic
def import_dataset(*, expected_fingerprint=None, **files):
    DatasetState.objects.get_or_create(key="main")
    DatasetState.objects.select_for_update().get(key="main")
    state, groups = prepare_dataset(**files)
    employee_ids = {
        row["employee_id"] for model, rows, _ in groups if model in {Employee, Participation} for row in rows
    }
    list(Employee.objects.select_for_update().filter(pk__in=employee_ids).order_by("pk"))
    # Revalidate after acquiring locks: a completion may have happened while waiting.
    state, groups = prepare_dataset(**files)
    summary = summarize_dataset(state, groups)
    if expected_fingerprint and summary["fingerprint"] != expected_fingerprint:
        raise ImportFailure(
            "Данные изменились после предпросмотра. Загрузите файлы заново и проверьте изменения."
        )
    for model, rows, keys in groups:
        for row in rows:
            model.objects.update_or_create(
                **{k: row[k] for k in keys}, defaults={k: v for k, v in row.items() if k not in keys}
            )
    from quest.services.plans import sync_plan_history

    record_ids = [r["record_id"] for model, rows, _ in groups if model == Participation for r in rows]
    sync_plan_history(Participation.objects.filter(pk__in=record_ids).select_related("employee", "event"))
    state.revision += 1
    state.save()
    return summary["counts"]
