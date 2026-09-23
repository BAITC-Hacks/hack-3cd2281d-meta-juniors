"""Independent stage-2 regression audit. Use Django test runner only.
Application: e4a146e; checkout ae5bfad. No live AI. Isolated test database.
"""
import copy
import json
import uuid
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from quest.tests import CareerQuestTests
from quest.models import DatasetState, Employee, ImportDraft, Participation
from quest.services.career import profile_context

@override_settings(OPENAI_API_KEY="", DEMO_MODE=True, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class Stage2Audit(TestCase):
    @classmethod
    def setUpTestData(cls):
        CareerQuestTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.employee = Employee.objects.get(pk="E0028")
        self.client = self.session(self.employee_user)
        provider = patch("quest.services.recommendations.OpenAI", side_effect=AssertionError("Live AI disabled"))
        provider.start()
        self.addCleanup(provider.stop)

    def session(self, user):
        c = Client(enforce_csrf_checks=True, raise_request_exception=False)
        c.force_login(user)
        c.get("/hr/import/" if user == self.hr_user else "/people/E0028/")
        c.defaults["HTTP_X_CSRFTOKEN"] = c.cookies["csrftoken"].value
        return c

    def post(self, path, data, client=None):
        return (client or self.client).post(path, json.dumps(data), content_type="application/json")

    def state(self):
        return (Employee.objects.count(), Participation.objects.count(), DatasetState.objects.get().revision,
                profile_context(self.employee)["levels"])

    def row(self):
        r = copy.deepcopy(json.loads(self.files["employees"])["employees"][27])
        r["employee_id"] = "JUDGE_AUDIT"
        r["full_name"] = "Synthetic QA"
        return r

    def upload(self, client, employees=None, history=None):
        files = {}
        if employees is not None:
            files["employees"] = SimpleUploadedFile("employees.json", json.dumps({"employees": employees}).encode())
        if history is not None:
            files["history"] = SimpleUploadedFile("activity_history.csv", history)
        return client.post("/hr/import/", files, follow=True)

    def test_01_plan_stages_and_idempotency_with_csrf(self):
        before = self.state()
        base = "/api/people/E0028/"
        self.assertEqual(self.post(base+"complete/EV_009/", {"request_id":str(uuid.uuid4())}).status_code,409)
        for action in ["add","add","start","start"]:
            self.assertEqual(self.post(base+"plan/EV_009/"+action+"/", {}).status_code,200)
            self.assertEqual(self.state()[3], before[3])
        self.assertEqual(Participation.objects.count(), before[1]+1)
        key = {"request_id":str(uuid.uuid4())}
        response = self.post(base+"complete/EV_009/",key)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["coverage_after"],74)
        after = self.state()
        self.assertTrue(self.post(base+"complete/EV_009/",key).json()["already_completed"])
        self.assertEqual(self.state(),after)
        self.assertEqual(self.post(base+"complete/EV_009/",{"request_id":str(uuid.uuid4())}).status_code,409)

    def test_02_plan_owner_and_csrf(self):
        path="/api/people/E0028/plan/EV_009/add/"
        hr=self.session(self.hr_user)
        self.assertEqual(self.post(path,{},hr).status_code,403)
        self.assertEqual(self.post("/api/people/E0001/plan/EV_009/add/",{}).status_code,404)
        missing=Client(enforce_csrf_checks=True)
        missing.force_login(self.employee_user)
        self.assertEqual(self.post(path,{},missing).status_code,403)

    def test_03_preview_confirm_repeat_with_csrf(self):
        hr=self.session(self.hr_user)
        before=self.state()
        response=self.upload(hr,[self.row()])
        self.assertContains(response,"данные ещё не изменены")
        self.assertEqual(self.state(),before)
        draft=ImportDraft.objects.get()
        data={"action":"confirm","draft":str(draft.pk)}
        self.assertContains(hr.post("/hr/import/",data,follow=True),"Загрузка завершена")
        self.assertTrue(Employee.objects.filter(pk="JUDGE_AUDIT").exists())
        after=self.state()
        hr.post("/hr/import/",data,follow=True)
        self.assertEqual(self.state(),after)

    def test_04_wrong_csv_header_is_rejected(self):
        hr=self.session(self.hr_user)
        before=self.state()
        response=self.upload(hr,history=b"not_a_dataset\n")
        draft=ImportDraft.objects.first()
        if draft:
            response=hr.post("/hr/import/",{"action":"confirm","draft":str(draft.pk)},follow=True)
        print("WRONG_HEADER", "draft_created",bool(draft),"revision",before[2],self.state()[2])
        self.assertContains(response,'class="notice error"')
        self.assertEqual(self.state(),before)

    def test_05_nonrepeatable_history_cannot_award_twice(self):
        hr=self.session(self.hr_user)
        history=("record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\n"
                 "AUDIT_DOUBLE_1,JUDGE_AUDIT,EV_009,2026-09-20,,completed,100,90,5,self\n"
                 "AUDIT_DOUBLE_2,JUDGE_AUDIT,EV_009,2026-09-21,,completed,100,90,5,self\n").encode()
        self.upload(hr,[self.row()],history)
        draft=ImportDraft.objects.first()
        if draft:
            hr.post("/hr/import/",{"action":"confirm","draft":str(draft.pk)},follow=True)
        employee=Employee.objects.filter(pk="JUDGE_AUDIT").first()
        if employee:
            cloud=profile_context(employee)["levels"]["SK_CLOUD"]
            print("NONREPEATABLE_HISTORY", "baseline",employee.skills["SK_CLOUD"],"actual",cloud,"expected_max",2)
            self.assertLessEqual(cloud,2)
        else:
            self.assertFalse(Participation.objects.filter(record_id__startswith="AUDIT_DOUBLE_").exists())

    def test_06_completion_non_object_json_is_400(self):
        before=self.state()
        for value in [[], "bad", 42, True, None]:
            with self.subTest(value=value):
                response=self.post("/api/people/E0028/complete/EV_009/",value)
                print("NON_OBJECT_JSON",repr(value),response.status_code)
                self.assertEqual(response.status_code,400)
        self.assertEqual(self.state(),before)

    def test_07_invalid_bundle_is_atomic(self):
        hr=self.session(self.hr_user)
        before=self.state()
        history=b"record_id,employee_id,event_id,date,due_date,status,completion_pct,score,feedback_rating,assigned_by\nAUDIT_BAD,JUDGE_AUDIT,MISSING,2026-09-20,,completed,100,90,5,self\n"
        response=self.upload(hr,[self.row()],history)
        self.assertContains(response,"неизвестный")
        self.assertEqual(self.state(),before)
        self.assertEqual(ImportDraft.objects.count(),0)

    def test_08_goal_invalid_json_is_400(self):
        for value in [[],None,True,42,"bad"]:
            with self.subTest(value=value):
                self.assertEqual(self.post("/api/people/E0028/goal/",value).status_code,400)
