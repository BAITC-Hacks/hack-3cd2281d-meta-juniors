"""Real HTTP QA of isolated local Career Quest; fixtures only use QA001-003.

Login/cookie/CSRF flow follows scripts/smoke_http.py. Timings are full HTTP
response latency, not browser rendering or live AI latency. No API keys used.
"""
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8010"
OUT = Path(__file__).resolve().parent
FIXTURES = OUT.parent / "fixtures"
ALLOWED_IDS = {"QA001", "QA002", "QA003"}
jar = http.cookiejar.CookieJar()
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
report = {
    "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "base_url": BASE,
    "code_commit": "0534bf7",
    "method": "Python stdlib real HTTP with cookiejar and CSRF; based on scripts/smoke_http.py",
    "limitations": ["HTTP latency excludes browser rendering", "No live AI; rules mode", "Global history count is not exposed by HTTP API"],
    "checks": {},
}


def save():
    (OUT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def call(path, payload=None, as_json=False, raw=None, content_type=None):
    headers = {"Referer": BASE + "/"}
    body = raw
    if payload is not None:
        body = (json.dumps(payload) if as_json else urllib.parse.urlencode(payload)).encode()
        content_type = "application/json" if as_json else "application/x-www-form-urlencoded"
    if body is not None:
        headers["X-CSRFToken"] = next((c.value for c in jar if c.name == "csrftoken"), "")
        headers["Content-Type"] = content_type
    start = time.perf_counter()
    try:
        with client.open(urllib.request.Request(BASE + path, data=body, headers=headers), timeout=30) as response:
            text = response.read().decode("utf-8")
            return response.status, text, round((time.perf_counter() - start) * 1000, 2)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), round((time.perf_counter() - start) * 1000, 2)


def check(name, passed, **details):
    report["checks"][name] = {"passed": bool(passed), **details}
    save()
    print(name, "PASS" if passed else "FAIL", flush=True)


def document_text(body):
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def notices(body):
    return [document_text(value).strip() for value in re.findall(r'<div class="notice ([^"]*)" role="status">(.*?)</div>', body, re.S) for value in [value[1]]]


def import_files(files, artifact_name):
    boundary = "CareerQuestAudit" + uuid.uuid4().hex
    chunks = []
    for field, filename in files.items():
        file_path = FIXTURES / filename
        data = file_path.read_bytes()
        filename = file_path.name
        chunks.extend([
            ("--" + boundary + "\r\n").encode(),
            (f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n').encode(),
            b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n",
        ])
    chunks.append(("--" + boundary + "--\r\n").encode())
    status, body, elapsed = call("/hr/import/", raw=b"".join(chunks), content_type="multipart/form-data; boundary=" + boundary)
    # HTML has CSRF tokens; retain only extracted messages and import result.
    result_section = re.search(r'<section class="panel import-result">(.*?)</section>', body, re.S)
    result = {"status": status, "elapsed_ms": elapsed, "messages": notices(body), "result_text": document_text(result_section.group(1)).strip() if result_section else ""}
    (OUT / (artifact_name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def snapshot():
    state = {}
    for ident in sorted(ALLOWED_IDS):
        status, body, _ = call(f"/api/people/{ident}/")
        state[ident] = {"api_status": status, "api": json.loads(body)}
        page_status, page, _ = call(f"/people/{ident}/")
        records = re.search(r'>(\d+) записей</span>', page)
        state[ident]["html_status"] = page_status
        state[ident]["history_count"] = int(records.group(1)) if records else None
        history = re.search(r'<section id="history".*?</section>', page, re.S)
        state[ident]["history_sha256"] = hashlib.sha256((history.group() if history else "").encode()).hexdigest()
    return state


def main():
    payload = json.loads((FIXTURES / "employees.json").read_text(encoding="utf-8"))
    employees = payload["employees"] if isinstance(payload, dict) else payload
    assert {row["employee_id"] for row in employees} == ALLOWED_IDS
    status, page, _ = call("/login/")
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
    status, page, _ = call("/login/", {"username": "hr", "password": "career-demo-2026", "csrfmiddlewaretoken": token})
    check("hr_login", status == 200 and "Выйти" in page and "Профили сотрудников" in page, status=status)
    if not report["checks"]["hr_login"]["passed"]:
        raise RuntimeError("HR login failed")
    for ident in sorted(ALLOWED_IDS):
        status, _, _ = call(f"/api/people/{ident}/")
        report.setdefault("before_import", {})[ident] = status
    result = import_files({"employees": "employees.json", "history": "activity_history.csv"}, "positive_import")
    check("positive_import", result["status"] == 200 and "Данные загружены" in " ".join(result["messages"]), **result)
    state = snapshot()
    report["after_import"] = state
    check("three_profiles_and_history", all(item["api_status"] == 200 and item["html_status"] == 200 for item in state.values()) and [state[ident]["history_count"] for ident in sorted(ALLOWED_IDS)] == [4, 1, 1], history_counts={key: val["history_count"] for key, val in state.items()})
    expected_coverage = {"QA001": 92, "QA002": 100, "QA003": 97}
    check("coverage_matches_expected", all(state[key]["api"]["coverage"] == value for key, value in expected_coverage.items()), observed={key: val["api"]["coverage"] for key, val in state.items()}, expected=expected_coverage)
    check("old_history_not_replayed", all(item["api"]["applied_records"] == [] for item in state.values()))
    result = import_files({"employees": "employees.json", "history": "activity_history.csv"}, "repeat_import")
    state_again = snapshot()
    check("repeat_import_no_duplicates", result["status"] == 200 and state_again == state and "новых 0" in result["result_text"], **result)
    for name, files, required_message in [
        ("invalid_employee_range", {"employees": "invalid_employees.json"}, "skills.SK_SYSTEM_DESIGN"),
        ("invalid_history_event", {"history": "invalid_history_unknown_event.csv"}, "неизвестный сотрудник или мероприятие"),
        ("combined_import_atomic_rejection", {"employees": "employees.json", "history": "invalid_history_unknown_event.csv"}, "неизвестный сотрудник или мероприятие"),
    ]:
        result = import_files(files, name)
        after = snapshot()
        check(name, required_message in " ".join(result["messages"]) and not result["result_text"] and after == state, profiles_and_history_unchanged=after == state, **result)
    invalid_header = OUT / "invalid_header.csv"
    invalid_header.write_text("not_a_dataset\n", encoding="utf-8")
    result = import_files({"history": str(invalid_header)}, "invalid_csv_header")
    check("invalid_csv_header_rejected", "Данные загружены" not in " ".join(result["messages"]) and not result["result_text"], expected="Reject malformed CSV header", **result)
    status, hr, _ = call("/hr/")
    hr_count = re.search(r'<strong>(\d+)<small>сотрудников</small>', hr)
    report["hr_employee_count"] = int(hr_count.group(1)) if hr_count else None
    check("hr_has_203_profiles", status == 200 and report["hr_employee_count"] == 203)
    report["expected_global_history_count_from_known_actions"] = 2750
    save()
    print("IMPORT_READY: positive/repeat/negative imports finished; QA001/QA002/QA003 available", flush=True)
    recommendations = {}
    for ident in sorted(ALLOWED_IDS):
        status, body, elapsed = call(f"/api/people/{ident}/recommendations/", {}, as_json=True)
        result = json.loads(body)
        recommendations[ident] = {"status": status, "http_elapsed_ms": elapsed, "payload": result}
        (OUT / (ident + "_recommendations.json")).write_text(json.dumps(recommendations[ident], ensure_ascii=False, indent=2), encoding="utf-8")
    report["recommendations"] = recommendations
    rec1 = recommendations["QA001"]["payload"]
    rec2 = recommendations["QA002"]["payload"]
    rec3 = recommendations["QA003"]["payload"]
    check("QA001_critical_gap_prioritized", 1 <= len(rec1["steps"]) <= 3 and rec1["steps"][0]["event_id"] in {"EV_005", "EV_006", "EV_007"}, ordered_event_ids=[s["event_id"] for s in rec1["steps"]], mode=rec1["mode"])
    check("QA002_empty_recommendation", rec2["steps"] == [] and rec2["mode"] == "empty", notice=rec2["notice"])
    check("QA003_single_expected_candidate", [s["event_id"] for s in rec3["steps"]] == ["EV_006"], benefits=rec3["steps"][0]["benefits"] if rec3["steps"] else [])
    report["timing_note"] = "Five sequential full HTTP requests per route, network plus full response read. No browser render measured. Service already running; caches recorded for rules; no live AI key."
    report["timings"] = {}
    for name, path, payload in [
        ("profile_html", "/people/E0028/", None),
        ("profile_api", "/api/people/E0028/", None),
        ("rules_recommendations", "/api/people/E0028/recommendations/", {}),
        ("hr_html", "/hr/", None),
    ]:
        samples = []
        details = []
        for _ in range(5):
            status, body, elapsed = call(path, payload, as_json=payload is not None)
            samples.append(elapsed)
            details.append({"status": status})
            if payload is not None:
                data = json.loads(body)
                details[-1].update(mode=data.get("mode"), cached=data.get("cached"), service_elapsed_ms=data.get("elapsed_ms"))
        report["timings"][name] = {"path": path, "samples_ms": samples, "median_ms": statistics.median(samples), "max_ms": max(samples), "responses": details}
        save()
    report["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save()
    print(json.dumps({"checks": {k: v["passed"] for k, v in report["checks"].items()}, "timings": report["timings"]}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        save()
        raise
