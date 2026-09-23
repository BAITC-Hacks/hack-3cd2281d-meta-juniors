from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import io
import json
from datetime import date
from pathlib import Path
import sys

from pydantic import ValidationError

ROOT = Path(r"C:\Users\Rakhat\.codex\visualizations\2026\09\23\01a0cd09-2cc4-7673-a28c-281541ba442d")
CODE = ROOT / "career-quest-code-review"
OUT = ROOT / "qa-audit-20260923" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)
source_paths = [CODE / "data" / name for name in ("employees.json", "events.json", "skills.json", "activity_history.csv")]
hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
employees_source = json.loads(source_paths[0].read_text(encoding="utf-8-sig"))
events_source = json.loads(source_paths[1].read_text(encoding="utf-8-sig"))
skills_source = json.loads(source_paths[2].read_text(encoding="utf-8-sig"))
base_history = list(csv.DictReader(io.StringIO(source_paths[3].read_text(encoding="utf-8-sig"))))
today = date(2026, 10, 1)
events = {e["event_id"]: e for e in events_source["events"]}
skills = {s["skill_id"]: s for s in skills_source["skills"]}
roles = {(r["role"], r["grade"]): r for r in skills_source["role_profiles"]}
base_employees = {e["employee_id"]: e for e in employees_source["employees"]}


def profile(employee_id, name, grade, goal_grade):
    return {
        "employee_id": employee_id,
        "full_name": name,
        "department": "Backend Development",
        "role": "Backend Engineer",
        "grade": grade,
        "manager_id": "E0175",
        "hire_date": "2024-10-01",
        "tenure_months": 24,
        "work_format": "hybrid",
        "preferred_language": "ru",
        "career_goal": {"target_role": "Backend Engineer", "target_grade": goal_grade},
        "skills": {k: 3 for k in skills},
        "last_review_date": "2026-06-01",
    }


qa1 = profile("QA001", "QA Synthetic Counterexample", "Middle", "Senior")
qa1["skills"].update(roles[("Backend Engineer", "Senior")]["required_skills"])
qa1["skills"].update(SK_PUBLIC_SPEAKING=0, SK_SYSTEM_DESIGN=2)
qa2 = profile("QA002", "QA Synthetic No Gaps", "Middle", "Senior")
qa2["skills"] = {k: 5 for k in skills}
qa3 = profile("QA003", "QA Synthetic Skill Caps", "Senior", "Lead")
qa3["skills"].update(roles[("Backend Engineer", "Lead")]["required_skills"])
qa3["skills"].update(SK_SYSTEM_DESIGN=4, SK_API_DESIGN=5, SK_OBSERVABILITY=3)
profiles = [qa1, qa2, qa3]

columns = ["record_id", "employee_id", "event_id", "date", "due_date", "status", "completion_pct", "score", "feedback_rating", "assigned_by"]


def history_row(record_id, employee_id, event_id, when, status, assigned_by):
    return dict(zip(columns, [record_id, employee_id, event_id, when, "", status, 100 if status == "completed" else 0, "", "", assigned_by]))


history = [
    history_row("QA_H001", "QA001", "EV_004", "2024-10-10", "completed", "hr"),
    history_row("QA_H002", "QA002", "EV_004", "2024-10-10", "completed", "hr"),
    history_row("QA_H003", "QA003", "EV_004", "2024-10-10", "completed", "hr"),
    history_row("QA_H004", "QA001", "EV_036", "2026-07-09", "no_show", "self"),
    history_row("QA_H005", "QA001", "EV_036", "2026-08-06", "no_show", "self"),
    history_row("QA_H006", "QA001", "EV_036", "2026-09-03", "no_show", "self"),
]
history.sort(key=lambda r: (r["date"], r["employee_id"], r["event_id"]))
payload = {"meta": {"dataset": "Career Quest QA synthetic additions", "version": "qa-20260923-1", "as_of_date": "2026-10-01"}, "employees": profiles}
(OUT / "employees.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


write_csv(OUT / "activity_history.csv", history)
bad_payload = copy.deepcopy(payload)
bad_payload["employees"] = [copy.deepcopy(qa1)]
bad_payload["employees"][0]["skills"]["SK_SYSTEM_DESIGN"] = 6
(OUT / "invalid_employees.json").write_text(json.dumps(bad_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
bad_history = [history_row("QA_NEG_UNKNOWN_EVENT", "QA001", "EV_UNKNOWN_QA", "2026-09-20", "no_show", "self")]
write_csv(OUT / "invalid_history_unknown_event.csv", bad_history)

# Read only the standalone Pydantic definitions. Do not initialize Django, connect
# to a database, import fixtures into the app, run its tests, or call an AI service.
spec = importlib.util.spec_from_file_location("qa_fixture_schemas", CODE / "quest" / "schemas.py")
schema_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = schema_module
spec.loader.exec_module(schema_module)


def parse_csv_rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [{k: None if v == "" and k in {"due_date", "score", "feedback_rating"} else v for k, v in r.items()} for r in rows]


for employee in profiles:
    schema_module.EmployeeInput.model_validate(employee)
for row in parse_csv_rows(OUT / "activity_history.csv"):
    schema_module.HistoryInput.model_validate(row)
assert set(e["employee_id"] for e in profiles) == {"QA001", "QA002", "QA003"}
assert not ({e["employee_id"] for e in profiles} & base_employees.keys())
assert len({h["record_id"] for h in history}) == len(history)
assert not ({h["record_id"] for h in history} & {h["record_id"] for h in base_history})
for employee in profiles:
    assert (employee["role"], employee["grade"]) in roles
    goal = employee["career_goal"]
    assert (goal["target_role"], goal["target_grade"]) in roles
    assert set(employee["skills"]) == set(skills)
    assert all(type(v) is int and 0 <= v <= 5 for v in employee["skills"].values())
    manager = base_employees[employee["manager_id"]]
    assert manager["grade"] == "Lead" and manager["department"] == employee["department"]
    assert date.fromisoformat(employee["hire_date"]) <= date.fromisoformat(employee["last_review_date"]) <= today
    hired = date.fromisoformat(employee["hire_date"])
    assert employee["tenure_months"] == (today.year - hired.year) * 12 + today.month - hired.month - (today.day < hired.day)
for row in history:
    employee = next(e for e in profiles if e["employee_id"] == row["employee_id"])
    event = events[row["event_id"]]
    assert date.fromisoformat(employee["hire_date"]) <= date.fromisoformat(row["date"]) <= date(2026, 9, 30)
    assert employee["role"] in event["target_roles"] and employee["grade"] in event["target_grades"]
    assert all(employee["skills"].get(k, 0) >= v for k, v in event["prerequisites"].items())
    if row["status"] == "no_show":
        assert event["format"] != "self_paced" and row["completion_pct"] == 0
    if row["status"] == "completed":
        assert date.fromisoformat(row["date"]) <= date.fromisoformat(employee["last_review_date"])
assert len([h for h in history if h["employee_id"] == "QA001" and h["status"] == "no_show"]) == 3

try:
    schema_module.EmployeeInput.model_validate(bad_payload["employees"][0])
except ValidationError as error:
    negative_json_error = error.errors(include_input=False)[0]
    assert negative_json_error["loc"] == ("skills", "SK_SYSTEM_DESIGN")
else:
    raise AssertionError("Negative JSON unexpectedly passes the level range")
for row in parse_csv_rows(OUT / "invalid_history_unknown_event.csv"):
    schema_module.HistoryInput.model_validate(row)
    assert row["employee_id"] in {e["employee_id"] for e in profiles}
    assert row["event_id"] not in events

# Independent arithmetic and eligibility from raw data and README rules; none
# of quest/services/career.py or recommendations.py is executed here.
def post_levels(levels, event_id):
    output = dict(levels)
    for effect in events[event_id]["develops_skills"]:
        old = output[effect["skill_id"]]
        if old < effect["max_level"]:
            output[effect["skill_id"]] = min(old + effect["gain"], effect["max_level"], 5)
    return output


def coverage(employee, levels):
    goal = employee["career_goal"]
    required = roles[(goal["target_role"], goal["target_grade"])]["required_skills"]
    numerator = sum(min(levels[k], need) for k, need in required.items())
    denominator = sum(required.values())
    return {"numerator": numerator, "denominator": denominator, "percent": round(numerator / denominator * 100)}


def useful_candidates(employee):
    goal = employee["career_goal"]
    needs = roles[(goal["target_role"], goal["target_grade"])]["required_skills"]
    own_history = [h for h in history if h["employee_id"] == employee["employee_id"]]
    result = []
    for event in events.values():
        if event["mandatory"] or employee["role"] not in event["target_roles"] or employee["grade"] not in event["target_grades"]:
            continue
        if any(employee["skills"][k] < v for k, v in event["prerequisites"].items()):
            continue
        if event["format"] != "self_paced" and not any(date.fromisoformat(d) >= today for d in event["upcoming_sessions"]):
            continue
        if any(h["event_id"] == event["event_id"] and h["status"] == "completed" for h in own_history) and event["event_id"] != "EV_036":
            continue
        afterward = post_levels(employee["skills"], event["event_id"])
        if any(employee["skills"][k] < need and afterward[k] > employee["skills"][k] for k, need in needs.items()):
            result.append(event["event_id"])
    return sorted(result)


facts = {
    "status": "static fixture validation only; no application or fixture import was run",
    "valid_profiles": 3,
    "valid_history_records": len(history),
    "original_profiles": len(base_employees),
    "original_history_records": len(base_history),
    "expected_total_profiles_after_fresh_import": len(base_employees) + 3,
    "expected_total_history_after_fresh_import": len(base_history) + len(history),
    "source_sha256": hashes,
    "profiles": {},
    "negative_json_field": ".".join(negative_json_error["loc"]),
    "negative_csv_unknown_event": "EV_UNKNOWN_QA",
}
for employee in profiles:
    eid = employee["employee_id"]
    after = post_levels(employee["skills"], "EV_006")
    facts["profiles"][eid] = {
        "history_count": sum(h["employee_id"] == eid for h in history),
        "coverage_before": coverage(employee, employee["skills"]),
        "useful_candidate_ids": useful_candidates(employee),
        "EV_006_changes": {k: [employee["skills"][k], after[k]] for k in ("SK_SYSTEM_DESIGN", "SK_API_DESIGN", "SK_OBSERVABILITY")},
        "coverage_after_EV_006": coverage(employee, after),
    }
assert facts["profiles"]["QA001"]["useful_candidate_ids"] == ["EV_005", "EV_006", "EV_007", "EV_036"]
assert facts["profiles"]["QA002"]["useful_candidate_ids"] == []
assert facts["profiles"]["QA003"]["useful_candidate_ids"] == ["EV_006"]
assert facts["profiles"]["QA003"]["EV_006_changes"] == {"SK_SYSTEM_DESIGN": [4, 5], "SK_API_DESIGN": [5, 5], "SK_OBSERVABILITY": [3, 4]}
assert facts["profiles"]["QA002"]["coverage_before"]["percent"] == 100
assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths} == hashes
assert "django" not in sys.modules
(OUT / "STATIC_CHECK.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(facts, indent=2, ensure_ascii=False))
