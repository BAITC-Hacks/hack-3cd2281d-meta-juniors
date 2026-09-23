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
    Employee,
    Event,
    ImportDraft,
    Participation,
    RecommendationCache,
    RoleProfile,
    Skill,
)
from quest.services.career import apply_gain, candidates_for, current_skills, profile_context
from quest.services.importer import ImportFailure, import_dataset, preview_dataset
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
        self.plan_action("add")
        self.plan_action("start")
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
    def test_plan_is_available_but_simulation_is_disabled_without_demo(self):
        self.plan_action("add")
        self.plan_action("start", expected=403)
        self.assertNotContains(self.client.get("/people/E0028/plan/"), "Начать в демо")

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
        response = self.client.post("/api/people/E0028/complete/EV_009/", {"request_id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200, response.content)
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

    def audit_history(self, rows):
        header = "record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n"
        return (header + "".join(
            f"{record},{employee},{event},{day},,completed,100,90,5,self\n"
            for record, employee, event, day in rows
        )).encode()

    def test_csv_header_validated_even_without_rows(self):
        before = DatasetState.objects.get().revision
        for raw in [b"", b"not_a_dataset\n", b"record_id,record_id\n",
                    self.audit_history([]).replace(b"assigned_by", b"unknown")]:
            with self.subTest(raw=raw):
                with self.assertRaisesMessage(ImportFailure, "CSV-заголовок"):
                    preview_dataset(history=raw)
                with self.assertRaises(ImportFailure):
                    import_dataset(history=raw)
        self.assertEqual(DatasetState.objects.get().revision, before)
        self.assertEqual(preview_dataset(history=self.audit_history([]))["counts"]["Participation"]["processed"], 0)

    def test_duplicate_nonrepeatable_completed_bundle_is_atomic(self):
        employee = copy.deepcopy(json.loads(self.files["employees"])["employees"][27])
        employee["employee_id"] = "QA_DUPLICATE"
        history = self.audit_history([
            ("DUP_1", "QA_DUPLICATE", "EV_009", "2026-09-20"),
            ("DUP_2", "QA_DUPLICATE", "EV_009", "2026-09-21"),
        ])
        revision = DatasetState.objects.get().revision
        with self.assertRaisesMessage(ImportFailure, "повторное завершение"):
            import_dataset(employees=json.dumps({"employees": [employee]}), history=history)
        self.assertFalse(Employee.objects.filter(pk="QA_DUPLICATE").exists())
        self.assertFalse(Participation.objects.filter(pk__in=["DUP_1", "DUP_2"]).exists())
        self.assertEqual(DatasetState.objects.get().revision, revision)

    def test_duplicate_completion_checks_database_and_allows_same_id_reimport(self):
        first = self.audit_history([("DUP_EXISTING", "E0028", "EV_009", "2026-09-20")])
        import_dataset(history=first)
        import_dataset(history=first)
        self.assertEqual(profile_context(self.employee)["levels"]["SK_CLOUD"], 2)
        with self.assertRaisesMessage(ImportFailure, "повторное завершение"):
            import_dataset(history=self.audit_history([("DUP_NEW", "E0028", "EV_009", "2026-09-21")]))
        self.assertFalse(Participation.objects.filter(pk="DUP_NEW").exists())

    def test_club_repeats_on_different_dates_only(self):
        import_dataset(history=self.audit_history([
            ("CLUB_1", "E0028", "EV_036", "2026-09-20"),
            ("CLUB_2", "E0028", "EV_036", "2026-09-21"),
        ]))
        with self.assertRaisesMessage(ImportFailure, "повторное завершение"):
            import_dataset(history=self.audit_history([("CLUB_3", "E0028", "EV_036", "2026-09-21")]))
        self.assertEqual(Participation.objects.filter(pk__in=["CLUB_1", "CLUB_2"]).count(), 2)

    def test_legacy_duplicate_history_does_not_award_twice(self):
        for number in [1, 2]:
            Participation.objects.create(record_id=f"LEGACY_DUP_{number}", employee=self.employee,
                event_id="EV_009", date=date(2026, 9, 20 + number), status="completed",
                completion_pct=100, assigned_by="self")
        ctx = profile_context(self.employee)
        self.assertEqual(ctx["levels"]["SK_CLOUD"], 2)
        self.assertEqual(sum(x["event_id"] == "EV_009" for x in ctx["applied"]), 1)

    def test_completion_non_object_json_returns_400(self):
        before = Participation.objects.count()
        for value in [[], "bad", 42, True, None]:
            with self.subTest(value=value):
                response = self.client.post("/api/people/E0028/complete/EV_009/",
                    json.dumps(value), content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("JSON-объектом", response.json()["detail"])
        self.assertEqual(Participation.objects.count(), before)

    def test_hr_no_step_filter_tracks_goal_and_planned_steps(self):
        self.client.force_login(self.hr_user)
        page = self.client.get("/hr/", {"q": "E0028", "no_step": "1"})
        self.assertEqual(page.context["count"], 0)
        for event in ["EV_009", "EV_010", "EV_037"]:
            DevelopmentPlanItem.objects.create(employee=self.employee, event_id=event, status="planned")
        page = self.client.get("/hr/", {"q": "E0028", "no_step": "1"})
        self.assertEqual(page.context["count"], 1)
        self.assertEqual(page.context["no_step_count"], 1)
        self.assertContains(page, "Есть шаги в личном плане")
        self.employee.skills = {key: 5 for key in Skill.objects.values_list("pk", flat=True)}
        self.employee.save()
        page = self.client.get("/hr/", {"q": "E0028", "no_step": "1"})
        self.assertEqual(page.context["count"], 1)
        self.assertContains(page, "Требования цели по навыкам выполнены")

    def test_hr_participation_counts_same_filtered_employees(self):
        self.client.force_login(self.hr_user)
        page = self.client.get("/hr/", {"grade": "Middle", "skill": "SK_CLOUD", "attention": "1"})
        selected = {p["employee"].pk for p in page.context["profiles"]}
        rows = list(Participation.objects.filter(employee_id__in=selected,
            date__gte=date(2026, 10, 1) - timedelta(days=90), date__lte=date(2026, 10, 1)))
        self.assertGreater(len(rows), 0)
        summary = page.context["event_participation"]
        self.assertEqual(sum(item["records"] for item in summary), len(rows))
        for item in summary:
            event_rows = [r for r in rows if r.event_id == item["event"].pk]
            self.assertEqual(item["people"], len({r.employee_id for r in event_rows}))
            for status in ["completed", "in_progress", "dropped", "no_show", "declined", "overdue"]:
                self.assertEqual(item[status], sum(r.status == status for r in event_rows))
        empty = self.client.get("/hr/", {"q": "NO_SUCH_PERSON"})
        self.assertEqual(empty.context["event_participation"], [])
        self.assertContains(empty, "нет записей участия за 90 дней")
