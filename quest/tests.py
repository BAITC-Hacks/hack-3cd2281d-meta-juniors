import copy
import json
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from quest.models import Employee, Event, Participation, RecommendationCache, RoleProfile, Skill
from quest.services.career import apply_gain, candidates_for, current_skills, profile_context
from quest.services.importer import ImportFailure, import_dataset
from quest.services.recommendations import Choice, Decision, get_recommendations, validate_decision


@override_settings(
    OPENAI_API_KEY="",
    DEMO_MODE=True,
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class CareerQuestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Path(settings.BASE_DIR) / "data"
        cls.files = {
            key: (root / filename).read_bytes()
            for key, filename in {
                "employees": "employees.json",
                "events": "events.json",
                "skills": "skills.json",
                "history": "activity_history.csv",
            }.items()
        }
        import_dataset(**cls.files)
        cls.employee_user = User.objects.create_user("tester", password="test-only-password")
        cls.hr_user = User.objects.create_user("hr-tester", password="test-only-password")
        group = Group.objects.create(name="HR")
        cls.hr_user.groups.add(group)
        Employee.objects.filter(pk="E0028").update(user=cls.employee_user)

    def setUp(self):
        self.employee = Employee.objects.get(pk="E0028")
        self.client.force_login(self.employee_user)

    def test_starter_counts(self):
        self.assertEqual(
            [
                Employee.objects.count(),
                Event.objects.count(),
                Skill.objects.count(),
                RoleProfile.objects.count(),
                Participation.objects.count(),
            ],
            [200, 40, 60, 32, 2743],
        )

    def test_malformed_metadata_is_validation_error(self):
        with self.assertRaises(ImportFailure):
            import_dataset(employees='{"meta": null, "employees": []}')

    def test_empty_candidates_do_not_invent_events(self):
        Event.objects.update(mandatory=True)
        result = get_recommendations(profile_context(self.employee))
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["mode"], "empty")

    def test_critical_career_gap_beats_lowest_skill(self):
        self.employee.skills["SK_PUBLIC_SPEAKING"] = 0
        self.employee.career_goal = {"target_role": "Backend Engineer", "target_grade": "Senior"}
        self.employee.save()
        for code, skill, cap in [
            ("TEST_CRITICAL", "SK_SYSTEM_DESIGN", 4),
            ("TEST_LOWEST", "SK_PUBLIC_SPEAKING", 3),
        ]:
            Event.objects.create(
                event_id=code,
                title=code,
                description="Synthetic test activity",
                type="course",
                format="self_paced",
                duration_hours=4,
                mandatory=False,
                target_roles=["Backend Engineer"],
                target_grades=["Middle"],
                develops_skills=[{"skill_id": skill, "gain": 1, "max_level": cap}],
                prerequisites={},
                upcoming_sessions=[],
            )
        result = get_recommendations(profile_context(self.employee))
        self.assertEqual(result["steps"][0]["event_id"], "TEST_CRITICAL")

    def test_unmet_prerequisite_excludes_event(self):
        event = Event.objects.get(pk="EV_009")
        event.prerequisites = {"SK_CLOUD": 5}
        event.save()
        candidates, _, _ = candidates_for(profile_context(self.employee))
        self.assertNotIn("EV_009", {c["event_id"] for c in candidates})

    def test_same_history_reimport_does_not_change_levels(self):
        before = profile_context(self.employee)["levels"]
        import_dataset(**self.files)
        self.employee.refresh_from_db()
        self.assertEqual(profile_context(self.employee)["levels"], before)
        self.assertEqual(Participation.objects.count(), 2743)

    def test_e0028_replays_only_after_review(self):
        ctx = profile_context(self.employee)
        self.assertEqual(ctx["levels"]["SK_SYSTEM_DESIGN"], 3)
        self.assertEqual(ctx["levels"]["SK_API_DESIGN"], 4)
        self.assertEqual(len(ctx["applied"]), 3)
        self.assertEqual(ctx["levels"], profile_context(self.employee)["levels"])

    def test_skill_caps_never_reduce_existing_level(self):
        event = Event.objects.get(pk="EV_005")
        levels = {"SK_SYSTEM_DESIGN": 5, "SK_API_DESIGN": 4}
        self.assertEqual(apply_gain(levels, event), levels)
        self.assertEqual(levels["SK_SYSTEM_DESIGN"], 5)

    def test_candidates_have_real_gain_and_valid_audience(self):
        ctx = profile_context(self.employee)
        candidates, _, uncovered = candidates_for(ctx)
        self.assertEqual({c["event_id"] for c in candidates}, {"EV_009", "EV_010", "EV_037"})
        self.assertIn("System Design", uncovered)
        self.assertNotIn("EV_005", {c["event_id"] for c in candidates})
        for c in candidates:
            self.assertTrue(all(b["after"] > b["before"] for b in c["benefits"]))

    def test_hidden_profile_and_history_import(self):
        original = json.loads(self.files["employees"])["employees"][27]
        employee = copy.deepcopy(original)
        employee["employee_id"] = "JUDGE_001"
        employee["full_name"] = "Synthetic judge profile"
        payload = json.dumps({"employees": [employee]}).encode()
        history = b"record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\nJUDGE_R1,JUDGE_001,EV_006,2026-09-08,,completed,100,90,5,self\n"
        import_dataset(employees=payload, history=history)
        ctx = profile_context(Employee.objects.get(pk="JUDGE_001"))
        self.assertEqual(ctx["levels"]["SK_SYSTEM_DESIGN"], 3)
        self.assertEqual(len(ctx["applied"]), 1)
        import_dataset(employees=payload, history=history)
        self.assertEqual(Employee.objects.count(), 201)
        self.assertEqual(Participation.objects.count(), 2744)

    def test_bad_reference_rolls_back_whole_upload(self):
        employee = copy.deepcopy(json.loads(self.files["employees"])["employees"][0])
        employee["employee_id"] = "JUDGE_BAD"
        history = b"record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\nBAD_R,JUDGE_BAD,NO_EVENT,2026-09-08,,completed,100,,,self\n"
        with self.assertRaises(ImportFailure):
            import_dataset(employees=json.dumps({"employees": [employee]}), history=history)
        self.assertFalse(Employee.objects.filter(pk="JUDGE_BAD").exists())

    def test_duplicate_ids_rejected(self):
        row = json.loads(self.files["employees"])["employees"][0]
        with self.assertRaises(ImportFailure):
            import_dataset(employees=json.dumps({"employees": [row, row]}))

    def test_import_out_of_range_rejected(self):
        row = copy.deepcopy(json.loads(self.files["employees"])["employees"][0])
        row["skills"]["SK_PYTHON"] = 6
        with self.assertRaises(ImportFailure):
            import_dataset(employees=json.dumps({"employees": [row]}))

    def test_history_id_cannot_be_reassigned(self):
        row = Participation.objects.first()
        other = "E0028" if row.employee_id != "E0028" else "E0001"
        history = f"record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n{row.pk},{other},{row.event_id},2026-09-08,,completed,100,,,self\n"
        with self.assertRaises(ImportFailure):
            import_dataset(history=history)

    def test_missing_goal_defaults_to_next_grade(self):
        ctx = profile_context(self.employee)
        self.assertTrue(ctx["inferred_goal"])
        self.assertEqual(ctx["target"].grade, "Senior")

    def test_lead_without_goal_has_no_invented_grade(self):
        self.employee.grade = "Lead"
        self.employee.career_goal = None
        ctx = profile_context(self.employee)
        self.assertIsNone(ctx["target"])
        self.assertEqual(get_recommendations(ctx)["steps"], [])

    def test_employee_cannot_read_other_employee_or_hr(self):
        for url in ("/people/E0001/", "/api/people/E0001/"):
            self.assertEqual(self.client.get(url).status_code, 404)
        for url in ("/hr/", "/hr/import/"):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post("/api/people/E0001/recommendations/").status_code, 404)

    def test_anonymous_access_denied(self):
        self.client.logout()
        self.assertEqual(self.client.get("/people/E0028/").status_code, 302)
        self.assertEqual(self.client.get("/api/people/E0028/").status_code, 403)

    def test_hr_can_view_and_import_but_not_complete_for_employee(self):
        self.client.force_login(self.hr_user)
        self.assertEqual(self.client.get("/hr/").status_code, 200)
        self.assertEqual(self.client.get("/people/E0028/").status_code, 200)
        self.assertEqual(self.client.get("/hr/import/").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/people/E0028/complete/EV_009/",
                {"request_id": str(uuid.uuid4())},
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_import_web_error_is_clear(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            "/hr/import/", {"employees": SimpleUploadedFile("employees.json", b"{bad-json")}
        )
        self.assertContains(response, "Не удалось прочитать")
        self.assertEqual(Employee.objects.count(), 200)

    def test_csrf_enforced_on_state_change(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.employee_user)
        self.assertEqual(
            client.post(
                "/api/people/E0028/goal/", {"target_role": "Backend Engineer", "target_grade": "Senior"}
            ).status_code,
            403,
        )

    def test_goal_change_validates_role_and_invalidates_recommendations(self):
        get_recommendations(profile_context(self.employee))
        response = self.client.post(
            "/api/people/E0028/goal/",
            {"target_role": "Unknown", "target_grade": "Senior"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/people/E0028/goal/",
            {"target_role": "Backend Engineer", "target_grade": "Lead"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RecommendationCache.objects.filter(employee=self.employee).exists())

    def test_completion_is_idempotent_and_recalculates(self):
        url = "/api/people/E0028/complete/EV_009/"
        data = {"request_id": str(uuid.uuid4())}
        before = profile_context(self.employee)["levels"]
        response = self.client.post(url, data, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        first = profile_context(self.employee)["levels"]
        self.assertGreater(first["SK_CLOUD"], before.get("SK_CLOUD", 0))
        second = self.client.post(url, data, content_type="application/json")
        self.assertTrue(second.json()["already_completed"])
        self.assertEqual(first, profile_context(self.employee)["levels"])
        other = self.client.post(url, {"request_id": str(uuid.uuid4())}, content_type="application/json")
        self.assertEqual(other.status_code, 409)

    def test_mandatory_completion_is_not_gamified(self):
        response = self.client.post(
            "/api/people/E0028/complete/EV_001/",
            {"request_id": str(uuid.uuid4())},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

    def test_demo_completion_on_review_date_counts(self):
        self.employee.last_review_date = date(2026, 10, 1)
        event = Event.objects.get(pk="EV_006")
        row = Participation(
            record_id="DEMO_TEST",
            employee=self.employee,
            event=event,
            date=date(2026, 10, 1),
            status="completed",
            request_id=uuid.uuid4(),
        )
        levels, applied = current_skills(self.employee, [row], date(2026, 10, 1))
        self.assertEqual(levels["SK_SYSTEM_DESIGN"], 3)
        self.assertEqual(len(applied), 1)

    def test_rules_fallback_is_labelled_and_multifactor(self):
        result = get_recommendations(profile_context(self.employee))
        self.assertEqual(result["mode"], "rules")
        self.assertEqual(len(result["steps"]), 3)
        for step in result["steps"]:
            self.assertTrue({"gap", "history"} <= {f["category"] for f in step["explanation"]})

    def test_llm_cannot_invent_event_or_evidence(self):
        candidates, _, _ = candidates_for(profile_context(self.employee))
        with self.assertRaises(ValueError):
            validate_decision(
                Decision(
                    steps=[
                        Choice(event_id="EV_FAKE", evidence_ids=["a", "b"], priority_reason="balanced_step")
                    ]
                ),
                candidates,
            )
        with self.assertRaises(ValueError):
            validate_decision(
                Decision(
                    steps=[
                        Choice(
                            event_id=candidates[0]["event_id"],
                            evidence_ids=["invented", "other"],
                            priority_reason="balanced_step",
                        )
                    ]
                ),
                candidates,
            )

    @override_settings(OPENAI_API_KEY="test-key-never-sent")
    @patch(
        "quest.services.recommendations.choose_with_ai",
        side_effect=RuntimeError("simulated provider failure"),
    )
    def test_ai_error_falls_back(self, mocked):
        result = get_recommendations(profile_context(self.employee))
        self.assertEqual(result["mode"], "rules")
        self.assertIn("недоступен", result["notice"])
        mocked.assert_called_once()

    @override_settings(OPENAI_API_KEY="test-key-never-sent")
    @patch("quest.services.recommendations.choose_with_ai")
    def test_ai_selection_is_used_and_cached(self, mocked):
        ctx = profile_context(self.employee)
        candidates, _, _ = candidates_for(ctx)
        chosen = candidates[-1]
        mocked.return_value = Decision(
            steps=[
                Choice(
                    event_id=chosen["event_id"],
                    evidence_ids=[f["id"] for f in chosen["facts"]],
                    priority_reason="balanced_step",
                )
            ]
        )
        result = get_recommendations(ctx)
        self.assertEqual(result["mode"], "ai")
        self.assertEqual(result["steps"][0]["event_id"], chosen["event_id"])
        self.assertTrue(get_recommendations(ctx)["cached"])
        mocked.assert_called_once()

    def test_profile_html_and_api(self):
        page = self.client.get("/people/E0028/")
        self.assertContains(page, "Твои следующие шаги")
        self.assertContains(page, "SK_SYSTEM_DESIGN", count=0)
        self.assertEqual(self.client.get("/api/people/E0028/").json()["skills"]["SK_SYSTEM_DESIGN"], 3)
