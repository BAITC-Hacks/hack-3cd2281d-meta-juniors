import copy
import importlib
import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from quest.models import (
    DatasetState,
    DevelopmentPlanItem,
    DevelopmentRequest,
    Employee,
    Event,
    ImportDraft,
    Participation,
    RecommendationCache,
    RoleProfile,
    Skill,
    WorkSubmission,
)
from quest.services.career import apply_gain, candidates_for, current_skills, profile_context
from quest.services.importer import ImportFailure, import_dataset, preview_dataset
from quest.services.practice import practice_template
from quest.services.recommendations import Choice, Decision, get_recommendations, validate_decision
from quest.services.trajectory import trajectory_for


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
        self.plan_action("add")
        self.plan_action("start")
        before = profile_context(self.employee)["levels"]
        submission = self.send_work()
        self.review(submission)
        first = profile_context(self.employee)["levels"]
        self.assertGreater(first["SK_CLOUD"], before.get("SK_CLOUD", 0))
        self.review(submission)
        self.assertEqual(first, profile_context(self.employee)["levels"])
        self.client.force_login(self.employee_user)
        other = self.client.post("/api/people/E0028/complete/EV_009/", {}, content_type="application/json")
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
        self.assertContains(page, "Твой фокус — System Design")
        self.assertEqual(self.client.get("/api/people/E0028/").json()["skills"]["SK_SYSTEM_DESIGN"], 3)

    def plan_action(self, action, event="EV_009", expected=200):
        response = self.client.post(
            f"/api/people/E0028/plan/{event}/{action}/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, expected, response.content)
        return response

    def test_planning_and_starting_do_not_award_skills_or_duplicate_history(self):
        before = profile_context(self.employee)["levels"]
        history_count = Participation.objects.count()
        get_recommendations(profile_context(self.employee))
        self.plan_action("add")
        self.plan_action("add")
        self.assertEqual(Participation.objects.count(), history_count)
        self.assertFalse(RecommendationCache.objects.filter(employee=self.employee).exists())
        self.assertNotIn("EV_009", {c["event_id"] for c in candidates_for(profile_context(self.employee))[0]})
        self.plan_action("start")
        self.plan_action("start")
        self.assertEqual(Participation.objects.count(), history_count + 1)
        self.assertEqual(profile_context(self.employee)["levels"], before)
        self.assertEqual(
            DevelopmentPlanItem.objects.filter(employee=self.employee, event_id="EV_009").count(), 1
        )

    def test_cancelled_step_returns_to_recommendations(self):
        self.plan_action("add")
        self.plan_action("cancel")
        self.plan_action("cancel")
        self.assertIn("EV_009", {c["event_id"] for c in candidates_for(profile_context(self.employee))[0]})
        self.plan_action("add")
        self.plan_action("start")
        self.plan_action("cancel", expected=409)

    def test_cannot_skip_plan_stages_or_add_mandatory_event(self):
        self.plan_action("start", expected=409)
        self.plan_action("add", "EV_001", expected=409)
        self.plan_action("add")
        response = self.client.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 409)

    def test_plan_and_event_access_control(self):
        for url in ["/people/E0028/plan/", "/people/E0028/events/EV_009/"]:
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.get(url.replace("E0028", "E0001")).status_code, 404)
        self.assertEqual(self.client.post("/api/people/E0001/plan/EV_009/add/").status_code, 404)
        self.client.force_login(self.hr_user)
        self.assertContains(self.client.get("/people/E0028/events/EV_009/"), "Как изменятся навыки")
        self.plan_action("add", expected=403)

    @override_settings(DEMO_MODE=False)
    def test_workflow_can_start_without_demo_but_direct_completion_is_disabled(self):
        self.plan_action("add")
        self.plan_action("start")
        self.assertNotContains(self.client.get("/people/E0028/plan/"), "Начать в демо")
        self.assertEqual(self.client.post("/api/people/E0028/complete/EV_009/").status_code, 409)

    def test_goal_change_revalidates_planned_step(self):
        self.plan_action("add")
        self.client.post(
            "/api/people/E0028/goal/",
            {"target_role": "Backend Engineer", "target_grade": "Junior"},
            content_type="application/json",
        )
        self.plan_action("start", expected=409)
        self.assertContains(self.client.get("/people/E0028/plan/"), "Шаг больше не подходит")
        self.assertEqual(
            DevelopmentPlanItem.objects.get(employee=self.employee, event_id="EV_009").status, "planned"
        )

    def test_started_activity_can_finish_after_goal_change(self):
        self.plan_action("add")
        self.plan_action("start")
        self.client.post(
            "/api/people/E0028/goal/",
            {"target_role": "Backend Engineer", "target_grade": "Junior"},
            content_type="application/json",
        )
        self.review(self.send_work())
        self.assertEqual(
            DevelopmentPlanItem.objects.get(employee=self.employee, event_id="EV_009").status, "completed"
        )

    def test_imported_in_progress_is_in_plan(self):
        rows = Participation.objects.filter(status="in_progress", event__mandatory=False)
        self.assertTrue(rows.exists())
        for row in rows:
            self.assertTrue(
                DevelopmentPlanItem.objects.filter(
                    employee=row.employee, event=row.event, status="in_progress"
                ).exists()
            )

    def test_migration_preserves_preexisting_demo_completions(self):
        record = Participation.objects.create(
            record_id="DEMO_OLD_VERSION",
            employee=self.employee,
            event_id="EV_009",
            date=date(2026, 10, 1),
            status="completed",
            completion_pct=100,
            assigned_by="self",
            request_id=uuid.uuid4(),
        )
        before = profile_context(self.employee)["levels"]
        migration = importlib.import_module("quest.migrations.0004_existing_demo_completions")
        migration.include_previous_demo_steps(apps, None)
        migration.include_previous_demo_steps(apps, None)
        item = DevelopmentPlanItem.objects.get(employee=self.employee, event_id="EV_009")
        self.assertEqual(item.status, "completed")
        self.assertEqual(item.participation, record)
        self.assertEqual(profile_context(self.employee)["levels"], before)

    def test_import_preview_does_not_write_and_counts_actual_changes(self):
        revision = DatasetState.objects.get().revision
        row = copy.deepcopy(json.loads(self.files["employees"])["employees"][27])
        updated, new = copy.deepcopy(row), copy.deepcopy(row)
        updated["full_name"] = "Changed name"
        new["employee_id"] = "JUDGE_PREVIEW"
        unchanged = json.loads(self.files["employees"])["employees"][0]
        files = {"employees": json.dumps({"employees": [updated, new, unchanged]})}
        preview = preview_dataset(**files)
        self.assertEqual(
            preview["counts"]["Employee"], {"processed": 3, "created": 1, "updated": 1, "unchanged": 1}
        )
        self.assertEqual(DatasetState.objects.get().revision, revision)
        self.assertFalse(Employee.objects.filter(pk="JUDGE_PREVIEW").exists())
        self.assertEqual(Employee.objects.get(pk="E0028").full_name, row["full_name"])
        import_dataset(expected_fingerprint=preview["fingerprint"], **files)
        self.assertTrue(Employee.objects.filter(pk="JUDGE_PREVIEW").exists())

    def test_stale_import_preview_rejected_after_profile_change(self):
        row = json.loads(self.files["employees"])["employees"][27]
        files = {"employees": json.dumps({"employees": [row]})}
        preview = preview_dataset(**files)
        Employee.objects.filter(pk="E0028").update(full_name="Changed since preview")
        with self.assertRaisesMessage(ImportFailure, "после предпросмотра"):
            import_dataset(expected_fingerprint=preview["fingerprint"], **files)
        self.assertEqual(Employee.objects.get(pk="E0028").full_name, "Changed since preview")

    def make_import_draft(self):
        self.client.force_login(self.hr_user)
        row = copy.deepcopy(json.loads(self.files["employees"])["employees"][27])
        row["employee_id"] = "JUDGE_WEB"
        response = self.client.post(
            "/hr/import/",
            {"employees": SimpleUploadedFile("employees.json", json.dumps({"employees": [row]}).encode())},
            follow=True,
        )
        self.assertContains(response, "данные ещё не изменены")
        self.assertFalse(Employee.objects.filter(pk="JUDGE_WEB").exists())
        return ImportDraft.objects.get()

    def test_import_web_preview_confirm_and_retry(self):
        draft = self.make_import_draft()
        data = {"action": "confirm", "draft": str(draft.pk)}
        response = self.client.post("/hr/import/", data, follow=True)
        self.assertContains(response, "/people/JUDGE_WEB/")
        self.assertContains(response, "Загрузка завершена")
        revision = DatasetState.objects.get().revision
        self.client.post("/hr/import/", data)
        self.assertEqual(DatasetState.objects.get().revision, revision)
        self.assertEqual(Employee.objects.filter(pk="JUDGE_WEB").count(), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.files, {})

    def test_import_draft_is_private_and_expires(self):
        draft = self.make_import_draft()
        other = User.objects.create_user("other-hr")
        other.groups.add(Group.objects.get(name="HR"))
        self.client.force_login(other)
        self.assertEqual(self.client.get(f"/hr/import/?draft={draft.pk}").status_code, 404)
        self.assertEqual(
            self.client.post("/hr/import/", {"action": "confirm", "draft": str(draft.pk)}).status_code, 404
        )
        self.client.force_login(self.hr_user)
        ImportDraft.objects.filter(pk=draft.pk).update(created_at=timezone.now() - timedelta(minutes=31))
        self.assertContains(
            self.client.post("/hr/import/", {"action": "confirm", "draft": str(draft.pk)}),
            "Предпросмотр устарел",
        )
        self.assertFalse(Employee.objects.filter(pk="JUDGE_WEB").exists())

    def test_hr_filters_and_aggregates_use_same_group(self):
        self.client.force_login(self.hr_user)
        page = self.client.get("/hr/", {"grade": "Middle", "skill": "SK_CLOUD", "attention": "1"})
        rows = page.context["profiles"]
        self.assertGreater(len(rows), 0)
        self.assertEqual(page.context["count"], len(rows))
        self.assertEqual(page.context["attention_count"], len(rows))
        for row in rows:
            self.assertEqual(row["employee"].grade, "Middle")
            self.assertTrue(row["attention"])
            self.assertTrue(
                any(g["skill_id"] == "SK_CLOUD" for g in profile_context(row["employee"])["open_gaps"])
            )
        self.assertEqual(self.client.get("/hr/", {"q": "E0028"}).context["count"], 1)
        self.assertContains(self.client.get("/hr/", {"q": "NONEXISTENTPERSON"}), "сотрудников не найдено")

    def test_repeated_skips_lower_score_and_are_explained(self):
        before = next(
            c for c in candidates_for(profile_context(self.employee))[0] if c["event_id"] == "EV_009"
        )
        for number in range(3):
            Participation.objects.create(
                record_id=f"SKIP_{number}",
                employee=self.employee,
                event_id="EV_009",
                date=date(2026, 9, 20 + number),
                status="no_show",
                assigned_by="self",
            )
        after = next(
            c for c in candidates_for(profile_context(self.employee))[0] if c["event_id"] == "EV_009"
        )
        self.assertLess(after["score"], before["score"])
        self.assertEqual(after["same_event_failures"], before["same_event_failures"] + 3)
        self.assertTrue(any("Причины в данных не указаны" in f["text"] for f in after["facts"]))

    def test_fully_met_goal_does_not_recommend_pointless_training(self):
        self.employee.skills = {key: 5 for key in Skill.objects.values_list("pk", flat=True)}
        self.employee.save()
        ctx = profile_context(self.employee)
        self.assertEqual(ctx["coverage"], 100)
        result = get_recommendations(ctx)
        self.assertEqual(result["steps"], [])
        self.assertIn("Требования выбранного профиля по навыкам выполнены", result["notice"])

    def request_practice(self):
        self.client.force_login(self.employee_user)
        response = self.client.post(
            "/people/E0028/requests/",
            {"skill": "SK_SYSTEM_DESIGN", "note": "Хочу разобраться с отказоустойчивой архитектурой."},
        )
        self.assertEqual(response.status_code, 302, response.content)
        return DevelopmentRequest.objects.get(employee=self.employee, skill_id="SK_SYSTEM_DESIGN")

    def publish_practice(self):
        development_request = self.request_practice()
        self.client.force_login(self.hr_user)
        response = self.client.post(
            f"/hr/development/requests/{development_request.pk}/",
            {**practice_template(development_request.skill), "gain": 1, "max_level": 4},
        )
        self.assertEqual(response.status_code, 302, response.content)
        development_request.refresh_from_db()
        self.client.force_login(self.employee_user)
        return development_request.event

    def send_work(self, event="EV_009", previous_attempt=0, body=None):
        response = self.client.post(
            f"/people/E0028/work/{event}/",
            {
                "previous_attempt": previous_attempt,
                "body": body
                or "Подготовлено решение: описаны требования, выбран подход и проведена проверка на примере. "
                "В приложенной схеме показаны компоненты, поток данных и обработка ошибок.",
                "artifact_url": "https://example.com/architecture",
            },
        )
        self.assertEqual(response.status_code, 302, response.content)
        return WorkSubmission.objects.get(
            plan_item__employee=self.employee, plan_item__event_id=event, attempt=previous_attempt + 1
        )

    def review(
        self,
        submission,
        decision="approve",
        feedback="Результат проверен по всем критериям. Хорошо обоснованы решения.",
    ):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            f"/hr/development/reviews/{submission.pk}/",
            {
                "decision": decision,
                "checked_criteria": [str(i) for i in range(len(submission.criteria))]
                if decision == "approve"
                else [],
                "feedback": feedback,
            },
        )
        self.assertEqual(response.status_code, 302, response.content)
        return response

    def test_request_is_idempotent_and_has_no_skill_effect(self):
        before = profile_context(self.employee)["levels"]
        first = self.request_practice()
        second = self.request_practice()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual((first.current_level, first.required_level), (3, 4))
        self.assertEqual(first.target, {"role": "Backend Engineer", "grade": "Senior"})
        self.assertEqual(profile_context(self.employee)["levels"], before)
        response = self.client.post("/people/E0028/requests/", {"skill": "SK_NOT_REAL"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(DevelopmentRequest.objects.count(), 1)

    def test_hr_publishes_personal_practice_without_assigning_it(self):
        event = self.publish_practice()
        self.assertEqual(event.for_employee, self.employee)
        self.assertEqual(len(event.practice_brief.criteria), 4)
        self.assertFalse(DevelopmentPlanItem.objects.filter(event=event).exists())
        self.assertIn(event.pk, {c["event_id"] for c in candidates_for(profile_context(self.employee))[0]})
        self.assertEqual(profile_context(self.employee)["levels"]["SK_SYSTEM_DESIGN"], 3)
        self.assertContains(self.client.get(f"/people/E0028/events/{event.pk}/"), "Что нужно подготовить")
        self.client.force_login(self.hr_user)
        self.assertEqual(self.client.get(f"/people/E0001/events/{event.pk}/").status_code, 404)
        other = Employee.objects.get(pk="E0001")
        other.role, other.grade, other.skills = self.employee.role, self.employee.grade, self.employee.skills
        self.assertNotIn(event.pk, {c["event_id"] for c in candidates_for(profile_context(other))[0]})

    def test_publish_revalidates_goal_and_skill_bounds(self):
        development_request = self.request_practice()
        self.client.force_login(self.hr_user)
        url = f"/hr/development/requests/{development_request.pk}/"
        payload = {**practice_template(development_request.skill), "gain": 5, "max_level": 5}
        self.assertContains(self.client.post(url, payload), "Прирост и потолок")
        self.employee.skills["SK_SYSTEM_DESIGN"] = 5
        self.employee.save()
        payload.update(gain=1, max_level=4)
        self.assertContains(self.client.post(url, payload), "Потребность изменилась")
        self.assertFalse(Event.objects.filter(for_employee=self.employee).exists())

    def test_closing_request_requires_explanation_and_can_be_requested_again(self):
        development_request = self.request_practice()
        self.client.force_login(self.hr_user)
        url = f"/hr/development/requests/{development_request.pk}/"
        self.assertContains(self.client.post(url, {"action": "close", "response": "нет"}), "от 10 до 3000")
        response = self.client.post(
            url, {"action": "close", "response": "Обсудим другой формат на встрече с руководителем."}
        )
        self.assertEqual(response.status_code, 302)
        development_request.refresh_from_db()
        self.assertEqual(development_request.status, "closed")
        self.client.force_login(self.employee_user)
        response = self.client.post("/people/E0028/requests/", {"skill": "SK_SYSTEM_DESIGN"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DevelopmentRequest.objects.filter(status="open").count(), 1)

    def test_practice_cycle_return_resubmit_approve_preserves_versions(self):
        event = self.publish_practice()
        self.plan_action("add", event.pk)
        self.plan_action("start", event.pk)
        before = profile_context(self.employee)["levels"]
        submission = self.send_work(event.pk)
        self.assertEqual(profile_context(self.employee)["levels"], before)
        self.assertContains(self.client.get("/people/E0028/plan/"), "На проверке")
        self.review(
            submission,
            decision="return",
            feedback="Добавь обработку повторов и объясни ключ идемпотентности.",
        )
        self.assertEqual(profile_context(self.employee)["levels"], before)
        self.client.force_login(self.employee_user)
        page = self.client.get(f"/people/E0028/work/{event.pk}/")
        self.assertContains(page, "Добавь обработку повторов")
        second = self.send_work(
            event.pk,
            previous_attempt=1,
            body=submission.body + " Повторы отсеиваются по ключу идемпотентности в хранилище.",
        )
        self.review(submission)  # Stale reviewer tab must never approve a returned version.
        self.assertEqual(profile_context(self.employee)["levels"], before)
        self.review(second)
        second.refresh_from_db()
        submission.refresh_from_db()
        self.assertEqual((submission.status, second.status), ("changes_requested", "approved"))
        self.assertEqual(second.plan_item.status, "completed")
        self.assertEqual(WorkSubmission.objects.count(), 2)
        self.assertEqual(profile_context(self.employee)["levels"]["SK_SYSTEM_DESIGN"], 4)
        self.assertEqual(DevelopmentRequest.objects.get(event=event).status, "completed")
        self.assertEqual(second.result["changes"], [{"name": "System Design", "before": 3, "after": 4}])
        self.client.force_login(self.employee_user)
        self.assertContains(self.client.get(f"/people/E0028/work/{event.pk}/"), "РЕЗУЛЬТАТ ПОДТВЕРЖДЁН")

    def test_submission_requires_started_step_and_safe_artifact(self):
        self.plan_action("add")
        url = "/people/E0028/work/EV_009/"
        payload = {"previous_attempt": 0, "body": "Подробный пример результата работы и его проверки. " * 3}
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.assertFalse(WorkSubmission.objects.exists())
        self.plan_action("start")
        payload["artifact_url"] = "javascript:alert(1)"
        response = self.client.post(url, payload)
        self.assertTrue(response.context["form"].errors)
        payload.update(artifact_url="", body="Готово")
        self.assertTrue(self.client.post(url, payload).context["form"].errors)
        self.assertFalse(WorkSubmission.objects.exists())

    def test_duplicate_submission_and_stale_attempt_do_not_replace_work(self):
        self.plan_action("add")
        self.plan_action("start")
        submission = self.send_work()
        payload = {"previous_attempt": 0, "body": submission.body}
        response = self.client.post("/people/E0028/work/EV_009/", payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WorkSubmission.objects.count(), 1)
        self.review(submission, decision="return")
        self.client.force_login(self.employee_user)
        self.assertContains(self.client.post("/people/E0028/work/EV_009/", payload), "Работа уже обновилась")
        self.assertEqual(WorkSubmission.objects.count(), 1)

    def test_approval_requires_all_criteria_and_feedback(self):
        self.plan_action("add")
        self.plan_action("start")
        submission = self.send_work()
        before = profile_context(self.employee)["levels"]
        self.client.force_login(self.hr_user)
        url = f"/hr/development/reviews/{submission.pk}/"
        self.assertContains(
            self.client.post(url, {"decision": "approve", "feedback": "Проверил результат работы."}),
            "проверь все критерии",
        )
        response = self.client.post(url, {"decision": "return", "feedback": ""})
        self.assertTrue(response.context["form"].errors)
        submission.refresh_from_db()
        self.assertEqual(submission.status, "pending")
        self.assertEqual(profile_context(self.employee)["levels"], before)

    def test_employee_cannot_review_or_publish_and_hr_cannot_submit_for_employee(self):
        development_request = self.request_practice()
        self.plan_action("add")
        self.plan_action("start")
        submission = self.send_work()
        for url in [
            "/hr/development/",
            f"/hr/development/requests/{development_request.pk}/",
            f"/hr/development/reviews/{submission.pk}/",
        ]:
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {}).status_code, 403)
        for url in ["/people/E0001/requests/", "/people/E0001/work/EV_009/"]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.hr_user)
        self.assertContains(self.client.get("/hr/development/"), "Проверить работу")
        for url in ["/people/E0028/requests/", "/people/E0028/work/EV_009/"]:
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_self_review_is_forbidden_even_with_hr_role(self):
        self.plan_action("add")
        self.plan_action("start")
        submission = self.send_work()
        self.employee_user.groups.add(Group.objects.get(name="HR"))
        response = self.client.post(
            f"/hr/development/reviews/{submission.pk}/",
            {
                "decision": "approve",
                "checked_criteria": ["0", "1", "2"],
                "feedback": "Все критерии выполнены.",
            },
        )
        self.assertContains(response, "Свою работу нельзя подтвердить")
        submission.refresh_from_db()
        self.assertEqual(submission.status, "pending")

    def test_submitted_text_is_escaped(self):
        self.plan_action("add")
        self.plan_action("start")
        body = "<script>alert('test')</script> " + "Описание результата работы. " * 5
        submission = self.send_work(body=body)
        self.client.force_login(self.hr_user)
        page = self.client.get(f"/hr/development/reviews/{submission.pk}/")
        self.assertNotContains(page, "<script>alert")
        self.assertContains(page, "&lt;script&gt;")

    def test_forecast_never_writes_actual_skills_and_moves_to_fact_on_approval(self):
        event = self.publish_practice()
        before = profile_context(self.employee)
        self.plan_action("add", event.pk)
        prediction = trajectory_for(profile_context(self.employee))
        self.assertEqual(prediction["action_item"].event_id, event.pk)
        self.assertGreater(prediction["projected_coverage"], before["coverage"])
        self.assertEqual(profile_context(self.employee)["levels"], before["levels"])
        self.plan_action("start", event.pk)
        submission = self.send_work(event.pk)
        self.review(submission)
        after = profile_context(self.employee)
        self.assertEqual(after["coverage"], prediction["projected_coverage"])
        self.assertNotIn(event.pk, [i.event_id for i in trajectory_for(after)["forecast_items"]])

    def test_import_cannot_overwrite_personal_practice_or_bypass_review(self):
        event = self.publish_practice()
        row = json.loads(self.files["events"])["events"][0]
        row["event_id"] = event.pk
        with self.assertRaisesRegex(ImportFailure, "защищена от перезаписи"):
            import_dataset(events=json.dumps({"events": [row]}))
        history = f"record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\nFAKE_APPROVAL,E0028,{event.pk},2026-10-01,,completed,100,,,self\n"
        with self.assertRaisesRegex(ImportFailure, "через проверку HR"):
            import_dataset(history=history)
        self.assertEqual(profile_context(self.employee)["levels"]["SK_SYSTEM_DESIGN"], 3)

    def test_import_cannot_replace_submitted_catalog_work(self):
        self.plan_action("add")
        self.plan_action("start")
        submission = self.send_work()
        record = submission.plan_item.participation
        history = f"record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n{record.pk},E0028,EV_009,2026-10-01,,completed,100,,,self\n"
        with self.assertRaisesRegex(ImportFailure, "отправлена работа"):
            import_dataset(history=history)
        record.refresh_from_db()
        self.assertEqual(record.status, "in_progress")

    def test_login_keeps_username_but_never_echoes_password(self):
        self.client.logout()
        response = self.client.post(
            "/login/", {"username": "wrong<name>", "password": "private-invalid-password"}
        )
        self.assertContains(response, 'value="wrong&lt;name&gt;"')
        self.assertNotContains(response, "private-invalid-password")
        self.assertContains(response, "Проверь логин и пароль")

    def test_login_redirects_to_requested_internal_page_and_rejects_external_next(self):
        self.client.logout()
        response = self.client.post(
            "/login/", {"username": "tester", "password": "test-only-password", "next": "/people/E0028/plan/"}
        )
        self.assertRedirects(response, "/people/E0028/plan/", fetch_redirect_response=False)
        self.client.logout()
        response = self.client.post(
            "/login/",
            {"username": "tester", "password": "test-only-password", "next": "https://example.com/phishing"},
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_demo_role_selection_ignores_previous_roles_destination(self):
        for username, next_url, destination in [
            ("tester", "/hr/", "/people/E0028/"),
            ("hr-tester", "/people/E0028/plan/", "/hr/"),
        ]:
            self.client.logout()
            response = self.client.post(
                "/login/",
                {"username": username, "password": "test-only-password", "next": next_url, "demo_entry": "1"},
                follow=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.redirect_chain[-1][0], destination)

    @override_settings(DEMO_MODE=False)
    def test_normal_login_has_no_demo_credentials_or_demo_override(self):
        self.client.logout()
        self.assertNotContains(self.client.get("/login/"), "career-demo-2026")
        self.assertNotContains(self.client.get("/login/"), "demo-fill")
        response = self.client.post(
            "/login/",
            {
                "username": "tester",
                "password": "test-only-password",
                "next": "/people/E0028/plan/",
                "demo_entry": "1",
            },
        )
        self.assertRedirects(response, "/people/E0028/plan/", fetch_redirect_response=False)

    def test_logout_revokes_session_and_api_reports_login_requirement(self):
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.post("/logout/").status_code, 302)
        self.assertEqual(self.client.get("/people/E0028/").status_code, 302)
        response = self.client.post("/api/people/E0028/recommendations/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "session_expired")

    @override_settings(DEBUG=False)
    def test_forbidden_and_missing_pages_offer_safe_return(self):
        page = self.client.get("/hr/")
        self.assertContains(page, "Для этого действия нет доступа", status_code=403)
        page = self.client.get("/people/E0001/")
        self.assertContains(page, "Страница недоступна", status_code=404)
        self.assertNotContains(page, Employee.objects.get(pk="E0001").full_name, status_code=404)
        self.assertContains(page, "В мой кабинет", status_code=404)

    def test_stale_csrf_does_not_mutate_and_explains_recovery(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.employee_user)
        page = client.post("/people/E0028/requests/", {"skill": "SK_SYSTEM_DESIGN"})
        self.assertContains(page, "Форма устарела", status_code=403)
        self.assertFalse(DevelopmentRequest.objects.exists())
        response = client.post("/api/people/E0028/plan/EV_009/add/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertIn("Обнови страницу", response.json()["detail"])

    def test_empty_recommendations_distinguish_goal_catalog_and_active_plan(self):
        Event.objects.update(mandatory=True)
        self.assertEqual(get_recommendations(profile_context(self.employee))["empty_reason"], "catalog_gap")
        DevelopmentPlanItem.objects.create(employee=self.employee, event_id="EV_009", status="planned")
        self.assertEqual(get_recommendations(profile_context(self.employee))["empty_reason"], "plan_active")
        self.employee.skills = {key: 5 for key in Skill.objects.values_list("pk", flat=True)}
        self.employee.save()
        self.assertEqual(get_recommendations(profile_context(self.employee))["empty_reason"], "goal_met")
        self.employee.grade, self.employee.career_goal = "Lead", None
        self.employee.save()
        self.assertEqual(get_recommendations(profile_context(self.employee))["empty_reason"], "no_goal")
