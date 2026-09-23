"""Independent audit tests for 0534bf7. Run only with Django test runner.

No live model calls; all mutations occur in Django's isolated test database.
This file is an audit artifact, not a change to the application repository.
"""
import copy
import json
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from quest import tests as original_tests
from quest.models import DatasetState, Employee, Event, Participation, RecommendationCache
from quest.services.career import candidates_for, hr_summary, profile_context
from quest.services.recommendations import Choice, Decision, get_recommendations


@override_settings(
    OPENAI_API_KEY="",
    DEMO_MODE=True,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class IndependentBackendAudit(TestCase):
    @classmethod
    def setUpTestData(cls):
        original_tests.CareerQuestTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.employee = Employee.objects.get(pk="E0028")
        self.client = self.csrf_client(self.employee_user)
        # Any accidentally unmocked provider constructor fails before network IO.
        provider = patch("quest.services.recommendations.OpenAI", side_effect=AssertionError("LIVE AI FORBIDDEN"))
        provider.start()
        self.addCleanup(provider.stop)

    def csrf_client(self, user):
        client = Client(enforce_csrf_checks=True, raise_request_exception=False)
        client.force_login(user)
        page = client.get("/hr/import/" if user == self.hr_user else "/people/E0028/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("csrftoken", client.cookies)
        client.defaults["HTTP_X_CSRFTOKEN"] = client.cookies["csrftoken"].value
        return client

    def post(self, path, data, client=None):
        return (client or self.client).post(path, json.dumps(data), content_type="application/json")

    def state(self):
        return (
            Employee.objects.count(), Event.objects.count(), Participation.objects.count(),
            DatasetState.objects.get(pk="main").revision,
            list(Employee.objects.order_by("pk").values_list("pk", "skills", "career_goal", "full_name")),
        )

    def new_employee(self):
        row = copy.deepcopy(json.loads(self.files["employees"])["employees"][27])
        row["employee_id"] = "JUDGE_AUDIT"
        row["full_name"] = "Synthetic independent audit profile"
        return row

    def upload(self, employees=None, history=None, client=None):
        uploads = {}
        if employees is not None:
            raw = employees if isinstance(employees, bytes) else json.dumps(employees).encode()
            uploads["employees"] = SimpleUploadedFile("employees.json", raw)
        if history is not None:
            uploads["history"] = SimpleUploadedFile("activity_history.csv", history)
        return (client or self.csrf_client(self.hr_user)).post("/hr/import/", uploads)

    def history_csv(self, employee="JUDGE_AUDIT", event="EV_006", record="JUDGE_AUDIT_R1"):
        return (
            "record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n"
            f"{record},{employee},{event},2026-09-08,,completed,100,90,5,self\n"
        ).encode()

    def test_csrf_control_rejects_missing_and_accepts_valid_token(self):
        goal = {"target_role": "Backend Engineer", "target_grade": "Senior"}
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.employee_user)
        denied = self.post("/api/people/E0028/goal/", goal, client)
        allowed = self.post("/api/people/E0028/goal/", goal)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_employee_cannot_mutate_other_profile_with_valid_csrf(self):
        before = self.state()
        for path, data in [
            ("/api/people/E0001/goal/", {"target_role": "Backend Engineer", "target_grade": "Senior"}),
            ("/api/people/E0001/complete/EV_009/", {"request_id": str(uuid.uuid4())}),
            ("/api/people/E0001/recommendations/", {}),
        ]:
            with self.subTest(path=path):
                response = self.post(path, data)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.state(), before)

    def test_employee_cannot_import_with_valid_csrf(self):
        before = self.state()
        response = self.upload({"employees": [self.new_employee()]}, client=self.client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.state(), before)

    def test_hr_cannot_change_goal_or_complete_with_valid_csrf(self):
        client = self.csrf_client(self.hr_user)
        before = self.state()
        for path, data in [
            ("/api/people/E0028/goal/", {"target_role": "Backend Engineer", "target_grade": "Senior"}),
            ("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())}),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.post(path, data, client).status_code, 403)
        self.assertEqual(self.state(), before)

    def test_invalid_goals_rejected_without_mutation(self):
        before = self.state()
        cases = [None, [], "bad", 42, {}, {"target_role": "Unknown", "target_grade": "Senior"},
                 {"target_role": "Backend Engineer", "target_grade": "CEO"},
                 {"target_role": "", "target_grade": "Senior"}]
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self.post("/api/people/E0028/goal/", data).status_code, 400)
        self.assertEqual(self.state(), before)

    def test_completion_rejects_missing_invalid_uuid_and_unknown_event(self):
        before = self.state()
        for data in [{}, {"request_id": "bad"}, {"request_id": None}, {"request_id": 42}]:
            with self.subTest(data=data):
                self.assertEqual(self.post("/api/people/E0028/complete/EV_009/", data).status_code, 400)
        self.assertEqual(self.post("/api/people/E0028/complete/NO_EVENT/", {"request_id": str(uuid.uuid4())}).status_code, 404)
        self.assertEqual(self.state(), before)

    def test_completion_non_object_json_is_client_error_not_server_error(self):
        before = self.state()
        for data in [[], "bad", 42, True, None]:
            with self.subTest(data=data):
                response = self.post("/api/people/E0028/complete/EV_009/", data)
                print("AUDIT_NON_OBJECT", repr(data), response.status_code,
                      repr(response.exc_info[1]) if response.exc_info else "no exception")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.state(), before)

    @override_settings(DEMO_MODE=False)
    def test_completion_disabled_outside_demo(self):
        before = self.state()
        response = self.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.state(), before)

    def test_completion_rejects_capped_wrong_audience_and_missing_prerequisite(self):
        before = self.state()
        self.assertEqual(self.post("/api/people/E0028/complete/EV_005/", {"request_id": str(uuid.uuid4())}).status_code, 409)
        event = Event.objects.get(pk="EV_009")
        event.target_roles = ["No Such Role"]
        event.save(update_fields=["target_roles"])
        self.assertEqual(self.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())}).status_code, 409)
        event.target_roles = ["Backend Engineer"]
        event.prerequisites = {"SK_CLOUD": 5}
        event.save(update_fields=["target_roles", "prerequisites"])
        self.assertEqual(self.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(self.state(), before)

    def test_completion_idempotency_exact_gain_cache_and_conflicts(self):
        before = Participation.objects.count()
        get_recommendations(profile_context(self.employee))
        token = str(uuid.uuid4())
        url = "/api/people/E0028/complete/EV_009/"
        first = self.post(url, {"request_id": token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["changes"], {"SK_CLOUD": [1, 2]})
        self.assertEqual((first.json()["coverage_before"], first.json()["coverage_after"]), (72, 74))
        self.assertFalse(RecommendationCache.objects.filter(employee=self.employee).exists())
        repeat = self.post(url, {"request_id": token})
        self.assertEqual(repeat.status_code, 200)
        self.assertTrue(repeat.json()["already_completed"])
        self.assertEqual(self.post(url, {"request_id": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(self.post("/api/people/E0028/complete/EV_010/", {"request_id": token}).status_code, 409)
        self.assertEqual(Participation.objects.count(), before + 1)

    def test_hr_summary_updates_after_completion(self):
        before = hr_summary(Employee.objects.filter(pk="E0028"))["profiles"][0]
        self.assertEqual(self.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())}).status_code, 200)
        after = hr_summary(Employee.objects.filter(pk="E0028"))["profiles"][0]
        self.assertEqual((before["coverage"], after["coverage"]), (72, 74))
        self.assertEqual(after["completed90"], before["completed90"] + 1)

    def test_judge_profile_and_history_import_via_http_valid_csrf(self):
        response = self.upload({"employees": [self.new_employee()]}, self.history_csv())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Employee.objects.filter(pk="JUDGE_AUDIT").exists())
        ctx = profile_context(Employee.objects.get(pk="JUDGE_AUDIT"))
        self.assertEqual(ctx["levels"]["SK_SYSTEM_DESIGN"], 3)
        self.assertEqual(len(ctx["applied"]), 1)
        self.assertEqual(len(get_recommendations(ctx)["steps"]), 3)

    def test_unknown_history_event_rolls_back_upload_via_http(self):
        before = self.state()
        response = self.upload({"employees": [self.new_employee()]}, self.history_csv(event="MISSING"))
        self.assertContains(response, "неизвестный сотрудник или мероприятие")
        self.assertEqual(self.state(), before)

    def test_nonrepeatable_event_cannot_gain_twice_from_imported_history(self):
        employee = self.new_employee()
        history = (
            "record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n"
            "AUDIT_DOUBLE_1,JUDGE_AUDIT,EV_009,2026-09-20,,completed,100,90,5,self\n"
            "AUDIT_DOUBLE_2,JUDGE_AUDIT,EV_009,2026-09-21,,completed,100,90,5,self\n"
        ).encode()
        response = self.upload({"employees": [employee]}, history)
        self.assertEqual(response.status_code, 200)
        imported = Employee.objects.filter(pk="JUDGE_AUDIT").first()
        if imported is None:
            # Rejecting this invalid upload atomically is acceptable.
            self.assertFalse(Participation.objects.filter(record_id__startswith="AUDIT_DOUBLE_").exists())
            return
        levels = profile_context(imported)["levels"]
        print("AUDIT_DOUBLE_COMPLETION", "baseline_cloud", imported.skills.get("SK_CLOUD"),
              "after_cloud", levels.get("SK_CLOUD"), "records", imported.history.count())
        self.assertLessEqual(levels.get("SK_CLOUD", 0), 2,
                             "EV_009 is not repeatable per kit README; two record IDs must not award Cloud twice")

    def test_unknown_history_employee_rolls_back_upload_via_http(self):
        before = self.state()
        response = self.upload({"employees": [self.new_employee()]}, self.history_csv(employee="MISSING"))
        self.assertContains(response, "неизвестный сотрудник или мероприятие")
        self.assertEqual(self.state(), before)

    def test_invalid_employee_references_reject_entire_upload(self):
        for field, value in [("manager_id", "MISSING"), ("skills", {"MISSING": 2}),
                             ("career_goal", {"target_role": "MISSING", "target_grade": "Senior"})]:
            with self.subTest(field=field):
                before = self.state()
                row = self.new_employee()
                row[field] = value
                response = self.upload({"employees": [row]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.state(), before)
                self.assertContains(response, "неизвестн")

    def test_malformed_json_utf8_and_invalid_rows_are_atomic(self):
        bad_values = [b"{bad", b"\xff", b"null", b"42", b'{"employees":[{}]}']
        for raw in bad_values:
            with self.subTest(raw=raw):
                before = self.state()
                response = self.upload(employees=raw)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.state(), before)
                self.assertContains(response, 'class="notice error"')

    def test_malformed_history_rolls_back_valid_employee(self):
        before = self.state()
        malformed = self.history_csv().replace(b"completed,100", b"no_show,100")
        response = self.upload({"employees": [self.new_employee()]}, malformed)
        self.assertContains(response, "completion_pct=0")
        self.assertEqual(self.state(), before)

    def test_history_with_wrong_header_is_rejected(self):
        before = self.state()
        response = self.upload(history=b"not_a_dataset\n")
        print("AUDIT_WRONG_CSV_HEADER", "http", response.status_code,
              "revision_before", before[3], "revision_after", DatasetState.objects.get(pk="main").revision)
        self.assertContains(response, 'class="notice error"')
        self.assertEqual(self.state(), before)

    @override_settings(OPENAI_API_KEY="fake-test-key-never-sent")
    def test_ai_malformed_unknown_duplicate_and_unsupported_evidence_fall_back(self):
        ctx = profile_context(self.employee)
        candidate = candidates_for(ctx)[0][0]
        valid = Choice(event_id=candidate["event_id"], evidence_ids=[f["id"] for f in candidate["facts"]], priority_reason="balanced_step")
        outputs = [None, {"steps": []},
                   Decision(steps=[Choice(event_id="FAKE", evidence_ids=["a", "b"], priority_reason="balanced_step")]),
                   Decision(steps=[valid, valid]),
                   Decision(steps=[Choice(event_id=candidate["event_id"], evidence_ids=["FAKE1", "FAKE2"], priority_reason="balanced_step")])]
        for output in outputs:
            with self.subTest(output=str(output)):
                RecommendationCache.objects.all().delete()
                with patch("quest.services.recommendations.choose_with_ai", return_value=output):
                    response = self.post("/api/people/E0028/recommendations/", {})
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(result["mode"], "rules")
                self.assertIn("непроверяемый", result["notice"])
                self.assertTrue(all(Event.objects.filter(pk=x["event_id"]).exists() for x in result["steps"]))

    @override_settings(OPENAI_API_KEY="fake-test-key-never-sent")
    def test_ai_timeout_mock_falls_back_with_explicit_timeout_budget(self):
        future = MagicMock()
        future.result.side_effect = FutureTimeoutError("simulated executor timeout")
        with patch("quest.services.recommendations.AI_EXECUTOR.submit", return_value=future) as submit:
            start = time.monotonic()
            response = self.post("/api/people/E0028/recommendations/", {})
            elapsed = time.monotonic() - start
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "rules")
        self.assertIn("недоступен", response.json()["notice"])
        future.result.assert_called_once_with(timeout=8)
        future.cancel.assert_called_once()
        submit.assert_called_once()
        print("AUDIT_MOCK_TIMEOUT", round(elapsed * 1000), "ms; no real waiting/provider call")

    @override_settings(OPENAI_API_KEY="fake-test-key-never-sent")
    def test_ai_provider_failure_fallback_and_no_false_ai_label(self):
        with patch("quest.services.recommendations.choose_with_ai", side_effect=RuntimeError("simulated provider outage")):
            response = self.post("/api/people/E0028/recommendations/", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "rules")
        self.assertEqual(response.json()["mode_label"], "Подбор по правилам")

    def test_rules_recommendations_use_gap_history_target_requirement(self):
        result = self.post("/api/people/E0028/recommendations/", {}).json()
        self.assertEqual(result["mode"], "rules")
        self.assertEqual(len(result["steps"]), 3)
        for step in result["steps"]:
            categories = {fact["category"] for fact in step["explanation"]}
            self.assertTrue({"gap", "history"} <= categories)
            self.assertTrue(any("при требуемых" in fact["text"] for fact in step["explanation"]))

    def test_history_and_critical_target_counterexample(self):
        # Explicit counterexample with three missed public-speaking events.
        self.employee.skills["SK_PUBLIC_SPEAKING"] = 0
        self.employee.skills["SK_SYSTEM_DESIGN"] = 2
        self.employee.last_review_date = "2026-09-30"
        self.employee.career_goal = {"target_role": "Backend Engineer", "target_grade": "Senior"}
        self.employee.save()
        for code, skill in [("AUDIT_CRITICAL", "SK_SYSTEM_DESIGN"), ("AUDIT_LOWEST", "SK_PUBLIC_SPEAKING")]:
            Event.objects.create(event_id=code, title=code, description="Synthetic independent audit event",
                type="workshop" if code == "AUDIT_LOWEST" else "course", format="self_paced",
                duration_hours=4, mandatory=False, target_roles=["Backend Engineer"], target_grades=["Middle"],
                develops_skills=[{"skill_id": skill, "gain": 1, "max_level": 4}], prerequisites={}, upcoming_sessions=[])
        for i, day in enumerate(["2026-09-10", "2026-09-15", "2026-09-20"]):
            Participation.objects.create(record_id=f"AUDIT_MISS_{i}", employee=self.employee,
                event_id="AUDIT_LOWEST", date=day, status="no_show", completion_pct=0, assigned_by="self")
        self.employee.refresh_from_db()
        ctx = profile_context(self.employee)
        result = get_recommendations(ctx)
        self.assertEqual(result["steps"][0]["event_id"], "AUDIT_CRITICAL")
        lowest = next(c for c in candidates_for(ctx)[0] if c["event_id"] == "AUDIT_LOWEST")
        self.assertEqual(lowest["same_event_failures"], 3)
        self.assertTrue(any("3 пропусков" in fact["text"] for fact in lowest["facts"]))

    def test_storage_failure_returns_no_success_and_rolls_back(self):
        before = self.state()
        with patch("quest.views.Participation.objects.create", side_effect=RuntimeError("simulated storage failure")):
            response = self.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.state(), before)
