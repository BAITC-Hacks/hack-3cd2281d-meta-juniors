import uuid

from django.conf import settings
from django.db import models


class DatasetState(models.Model):
    key = models.CharField(max_length=30, primary_key=True, default="main")
    as_of_date = models.DateField(default="2026-10-01")
    revision = models.PositiveIntegerField(default=0)


class Skill(models.Model):
    skill_id = models.CharField(max_length=100, primary_key=True)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=10)
    category = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class RoleProfile(models.Model):
    role = models.CharField(max_length=100)
    grade = models.CharField(max_length=20)
    required_skills = models.JSONField(default=dict)
    critical_skills = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["role", "grade"], name="unique_role_grade")]


class Employee(models.Model):
    employee_id = models.CharField(max_length=100, primary_key=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="employee"
    )
    full_name = models.CharField(max_length=200)
    department = models.CharField(max_length=200)
    role = models.CharField(max_length=100)
    grade = models.CharField(max_length=20)
    manager_id = models.CharField(max_length=100, null=True, blank=True)
    hire_date = models.DateField()
    tenure_months = models.PositiveIntegerField()
    work_format = models.CharField(max_length=20)
    preferred_language = models.CharField(max_length=5)
    career_goal = models.JSONField(null=True, blank=True)
    skills = models.JSONField(default=dict)
    last_review_date = models.DateField()

    class Meta:
        ordering = ["employee_id"]

    def __str__(self):
        return f"{self.full_name} · {self.employee_id}"


class Event(models.Model):
    event_id = models.CharField(max_length=100, primary_key=True)
    title = models.CharField(max_length=300)
    description = models.TextField()
    type = models.CharField(max_length=30)
    format = models.CharField(max_length=20)
    duration_hours = models.FloatField()
    mandatory = models.BooleanField(default=False)
    target_roles = models.JSONField(default=list)
    target_grades = models.JSONField(default=list)
    develops_skills = models.JSONField(default=list)
    prerequisites = models.JSONField(default=dict)
    upcoming_sessions = models.JSONField(default=list)
    for_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.PROTECT, related_name="personal_events"
    )

    def __str__(self):
        return self.title


class Participation(models.Model):
    record_id = models.CharField(max_length=100, primary_key=True)
    employee = models.ForeignKey(Employee, related_name="history", on_delete=models.CASCADE)
    event = models.ForeignKey(Event, related_name="participations", on_delete=models.PROTECT)
    date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20)
    completion_pct = models.PositiveSmallIntegerField(default=0)
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    feedback_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    assigned_by = models.CharField(max_length=20)
    request_id = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["date", "record_id"]
        indexes = [models.Index(fields=["employee", "status", "date"])]


class RecommendationCache(models.Model):
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE)
    fingerprint = models.CharField(max_length=64)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now=True)


class DevelopmentPlanItem(models.Model):
    STATUS_CHOICES = [
        ("planned", "Запланировано"),
        ("in_progress", "В работе"),
        ("submitted", "На проверке"),
        ("needs_revision", "На доработке"),
        ("completed", "Завершено"),
        ("cancelled", "Убрано из плана"),
    ]
    employee = models.ForeignKey(Employee, related_name="plan_items", on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="planned", choices=STATUS_CHOICES)
    participation = models.OneToOneField(
        Participation, null=True, blank=True, on_delete=models.SET_NULL, related_name="plan_item"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "event"], name="unique_employee_plan_event"),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "planned",
                        "in_progress",
                        "submitted",
                        "needs_revision",
                        "completed",
                        "cancelled",
                    ]
                ),
                name="valid_plan_status",
            ),
        ]


class ImportDraft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    files = models.JSONField()
    preview = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)


class DevelopmentRequest(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="development_requests")
    skill = models.ForeignKey(Skill, on_delete=models.PROTECT)
    target = models.JSONField()
    current_level = models.PositiveSmallIntegerField()
    required_level = models.PositiveSmallIntegerField()
    note = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        default="open",
        choices=[
            ("open", "Ожидает HR"),
            ("proposed", "Практика подготовлена"),
            ("completed", "Результат подтверждён"),
            ("closed", "Закрыт с ответом"),
        ],
    )
    response = models.TextField(blank=True)
    event = models.OneToOneField(
        Event, null=True, blank=True, on_delete=models.PROTECT, related_name="development_request"
    )
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "skill"],
                condition=models.Q(status__in=["open", "proposed"]),
                name="one_active_development_request",
            )
        ]


class PracticeBrief(models.Model):
    event = models.OneToOneField(Event, on_delete=models.CASCADE, related_name="practice_brief")
    instructions = models.TextField()
    deliverable = models.TextField()
    criteria = models.JSONField()
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class WorkSubmission(models.Model):
    plan_item = models.ForeignKey(DevelopmentPlanItem, on_delete=models.CASCADE, related_name="submissions")
    attempt = models.PositiveIntegerField()
    body = models.TextField()
    artifact_url = models.URLField(max_length=1000, blank=True)
    criteria = models.JSONField()
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "На проверке"),
            ("changes_requested", "Нужна доработка"),
            ("approved", "Подтверждено"),
        ],
    )
    feedback = models.TextField(blank=True)
    checked_criteria = models.JSONField(default=list)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict)

    class Meta:
        ordering = ["-attempt"]
        constraints = [
            models.UniqueConstraint(fields=["plan_item", "attempt"], name="unique_submission_attempt"),
            models.UniqueConstraint(
                fields=["plan_item"], condition=models.Q(status="pending"), name="one_pending_submission"
            ),
        ]
