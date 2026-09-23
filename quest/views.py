import uuid
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods
from pydantic import ValidationError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    Employee,
    Event,
    ImportDraft,
    Participation,
    RecommendationCache,
    RoleProfile,
    Skill,
)
from .schemas import GoalInput
from .services.career import (
    FORMAT_NAMES,
    GRADES,
    STATUS_NAMES,
    apply_gain,
    candidates_for,
    eligibility,
    hr_summary,
    profile_context,
)
from .services.importer import ImportFailure, import_dataset, preview_dataset
from .services.plans import PlanConflict, change_plan
from .services.practice import brief_for
from .services.recommendations import get_recommendations
from .services.trajectory import trajectory_for


def is_hr(user):
    return user.is_authenticated and (user.is_superuser or user.groups.filter(name="HR").exists())


def accessible_employee(user, employee_id):
    query = Employee.objects.all()
    if not is_hr(user):
        query = query.filter(user=user)
    return get_object_or_404(query, pk=employee_id)


class AppLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if settings.DEMO_MODE:
            ctx["demo_password"] = settings.DEMO_PASSWORD
        return ctx

    def get_success_url(self):
        # Demo role selection always opens that role's home, even from a stale ?next= link.
        if settings.DEMO_MODE and self.request.POST.get("demo_entry") == "1":
            return "/"
        return super().get_success_url()


@login_required
def home(request):
    if is_hr(request.user):
        return redirect("hr")
    employee = Employee.objects.filter(user=request.user).first()
    if not employee:
        return render(request, "quest/no_profile.html", status=403)
    return redirect("profile", employee_id=employee.pk)


@login_required
def profile(request, employee_id):
    employee = accessible_employee(request.user, employee_id)
    ctx = profile_context(employee)
    options = defaultdict(list)
    for r in RoleProfile.objects.all():
        options[r.role].append(r.grade)
    ctx["goal_options"] = dict(options)
    ctx["page"] = "profile"
    ctx["can_manage"] = employee.user_id == request.user.id
    ctx["trajectory"] = trajectory_for(ctx)
    ctx["plan_count"] = ctx["trajectory"]["active_count"]
    ctx["history_rows"] = [
        {"record": h, "label": STATUS_NAMES.get(h.status, h.status)}
        for h in sorted(ctx["history"], key=lambda r: (r.date, r.record_id), reverse=True)
    ]
    return render(request, "quest/profile.html", ctx)


@login_required
def hr_dashboard(request):
    if not is_hr(request.user):
        raise PermissionDenied
    employees = Employee.objects.prefetch_related(
        Prefetch("history", queryset=Participation.objects.select_related("event"))
    )
    department = request.GET.get("department", "")
    grade = request.GET.get("grade", "")
    attention = request.GET.get("attention") == "1"
    skill_id = request.GET.get("skill", "")
    query = request.GET.get("q", "").strip()
    departments = list(
        Employee.objects.order_by("department").values_list("department", flat=True).distinct()
    )
    if department:
        employees = employees.filter(department=department)
    if grade:
        employees = employees.filter(grade=grade)
    if query:
        employees = employees.filter(
            Q(full_name__icontains=query) | Q(employee_id__icontains=query) | Q(role__icontains=query)
        )
    ctx = hr_summary(employees, attention_only=attention, skill_id=skill_id)
    for deficit in ctx["deficits"]:
        params = request.GET.copy()
        params["skill"] = deficit["skill_id"]
        deficit["query"] = params.urlencode()
    ctx.update(
        page="hr",
        departments=departments,
        department=department,
        grades=GRADES,
        grade=grade,
        attention=attention,
        selected_skill=skill_id,
        skill_options=Skill.objects.order_by("name"),
        query=query,
    )
    return render(request, "quest/hr.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def import_page(request):
    if not is_hr(request.user):
        raise PermissionDenied
    draft = None
    if request.GET.get("draft"):
        try:
            draft_id = uuid.UUID(request.GET["draft"])
        except ValueError:
            return redirect("import")
        draft = get_object_or_404(ImportDraft, pk=draft_id, owner=request.user)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "confirm":
                try:
                    draft_id = uuid.UUID(request.POST.get("draft", ""))
                except ValueError:
                    raise ImportFailure("Предпросмотр не найден. Загрузите файлы заново.") from None
                with transaction.atomic():
                    draft = get_object_or_404(
                        ImportDraft.objects.select_for_update(), pk=draft_id, owner=request.user
                    )
                    if not draft.confirmed_at:
                        if timezone.now() - draft.created_at > timedelta(minutes=30):
                            raise ImportFailure("Предпросмотр устарел. Загрузите файлы заново.")
                        import_dataset(expected_fingerprint=draft.preview["fingerprint"], **draft.files)
                        draft.confirmed_at = timezone.now()
                        draft.files = {}
                        draft.save(update_fields=["confirmed_at", "files"])
                        messages.success(
                            request, "Данные загружены. Откройте профиль для проверки рекомендаций."
                        )
                return redirect(f"{request.path}?draft={draft.pk}")
            if not request.FILES:
                raise ImportFailure("Выберите хотя бы один файл.")
            data = {}
            for key in ("employees", "history", "skills", "events"):
                uploaded = request.FILES.get(key)
                if uploaded:
                    if uploaded.size > 10 * 1024 * 1024:
                        raise ImportFailure("Размер файла не должен превышать 10 МБ.")
                    try:
                        data[key] = uploaded.read().decode("utf-8-sig")
                    except UnicodeDecodeError:
                        raise ImportFailure("Файл должен быть в кодировке UTF-8.") from None
            if not data:
                raise ImportFailure("Файлы не распознаны.")
            preview = preview_dataset(**data)
            ImportDraft.objects.filter(
                owner=request.user, created_at__lt=timezone.now() - timedelta(minutes=30)
            ).delete()
            draft = ImportDraft.objects.create(owner=request.user, files=data, preview=preview)
            return redirect(f"{request.path}?draft={draft.pk}")
        except ImportFailure as exc:
            messages.error(request, str(exc))
    labels = {
        "Employee": "Сотрудники",
        "Participation": "История",
        "Event": "Мероприятия",
        "Skill": "Навыки",
        "RoleProfile": "Профили ролей",
    }
    counts = (
        [{"label": labels[k], **v} for k, v in draft.preview["counts"].items() if v["processed"]]
        if draft
        else []
    )
    return render(request, "quest/import.html", {"page": "import", "draft": draft, "counts": counts})


@login_required
def development_plan(request, employee_id):
    employee = accessible_employee(request.user, employee_id)
    ctx = profile_context(employee)
    candidates, _, _ = candidates_for(ctx, include_planned=True)
    eligible = {c["event_id"] for c in candidates}
    columns = [
        {"status": s, "title": title, "items": []}
        for s, title in [
            ("planned", "Запланировано"),
            ("in_progress", "В работе"),
            ("submitted", "На проверке"),
            ("completed", "Завершено"),
        ]
    ]
    by_status = {c["status"]: c["items"] for c in columns}
    for item in employee.plan_items.select_related("event", "participation").exclude(status="cancelled"):
        item.can_start = item.event_id in eligible
        by_status["in_progress" if item.status == "needs_revision" else item.status].append(item)
    total = sum(len(c["items"]) for c in columns)
    completed = len(by_status["completed"])
    ctx.update(
        page="plan",
        columns=columns,
        plan_total=total,
        plan_completed=completed,
        plan_progress=round(completed / total * 100) if total else 0,
        trajectory=trajectory_for(ctx),
        can_manage=employee.user_id == request.user.id,
    )
    return render(request, "quest/plan.html", ctx)


@login_required
def event_detail(request, employee_id, event_id):
    employee = accessible_employee(request.user, employee_id)
    event = get_object_or_404(
        Event.objects.filter(Q(for_employee__isnull=True) | Q(for_employee=employee)), pk=event_id
    )
    ctx = profile_context(employee)
    candidates, _, _ = candidates_for(ctx, include_planned=True)
    candidate = next((c for c in candidates if c["event_id"] == event_id), None)
    after = apply_gain(ctx["levels"], event)
    effects = [
        {
            "name": ctx["cat"]["skills"][e["skill_id"]].name,
            "before": ctx["levels"].get(e["skill_id"], 0),
            "after": after[e["skill_id"]],
            "cap": e["max_level"],
        }
        for e in event.develops_skills
    ]
    prerequisites = [
        {
            "name": ctx["cat"]["skills"][key].name,
            "required": required,
            "current": ctx["levels"].get(key, 0),
            "met": ctx["levels"].get(key, 0) >= required,
        }
        for key, required in event.prerequisites.items()
    ]
    reasons = eligibility(employee, event, ctx["levels"], ctx["history"], ctx["cat"]["today"])
    if not candidate and not reasons:
        reasons.append("Активность не сокращает текущий разрыв к карьерной цели.")
    item = employee.plan_items.filter(event=event).exclude(status="cancelled").first()
    ctx.update(
        page="event",
        event=event,
        brief=brief_for(event),
        candidate=candidate,
        effects=effects,
        prerequisites=prerequisites,
        reasons=reasons,
        item=item,
        event_format=FORMAT_NAMES.get(event.format, event.format),
        sessions=[s for s in event.upcoming_sessions if s >= str(ctx["cat"]["today"])],
        can_manage=employee.user_id == request.user.id,
    )
    return render(request, "quest/event.html", ctx)


@api_view(["POST"])
def plan_api(request, employee_id, event_id, action):
    employee = accessible_employee(request.user, employee_id)
    if employee.user_id != request.user.id:
        return Response({"detail": "План изменяет сам сотрудник."}, status=403)
    if action not in {"add", "start", "cancel"}:
        return Response({"detail": "Неизвестное действие."}, status=400)
    with transaction.atomic():
        employee = Employee.objects.select_for_update().get(pk=employee.pk)
        event = get_object_or_404(Event, pk=event_id)
        try:
            item = change_plan(employee, event, action)
        except PlanConflict as exc:
            return Response({"detail": str(exc)}, status=409)
    return Response({"status": item.status, "event_id": event.pk})


@api_view(["GET"])
def profile_api(request, employee_id):
    employee = accessible_employee(request.user, employee_id)
    ctx = profile_context(employee)
    return Response(
        {
            "employee_id": employee.pk,
            "role": employee.role,
            "grade": employee.grade,
            "skills": ctx["levels"],
            "gaps": ctx["gaps"],
            "coverage": ctx["coverage"],
            "applied_records": ctx["applied"],
            "as_of_date": str(ctx["cat"]["today"]),
        }
    )


@api_view(["POST"])
def recommendations_api(request, employee_id):
    employee = accessible_employee(request.user, employee_id)
    return Response(get_recommendations(profile_context(employee)))


@api_view(["POST"])
def goal_api(request, employee_id):
    employee = accessible_employee(request.user, employee_id)
    if employee.user_id != request.user.id:
        return Response({"detail": "Цель выбирает сам сотрудник."}, status=403)
    try:
        goal = GoalInput.model_validate(request.data)
    except ValidationError:
        return Response({"detail": "Выберите роль и грейд из каталога."}, status=400)
    if not RoleProfile.objects.filter(role=goal.target_role, grade=goal.target_grade).exists():
        return Response({"detail": "Такого целевого профиля нет в каталоге."}, status=400)
    with transaction.atomic():
        employee = Employee.objects.select_for_update().get(pk=employee.pk)
        employee.career_goal = goal.model_dump()
        employee.save(update_fields=["career_goal"])
        RecommendationCache.objects.filter(employee=employee).delete()
    return Response({"saved": True})


@api_view(["POST"])
def complete_api(request, employee_id, event_id):
    employee = accessible_employee(request.user, employee_id)
    if employee.user_id != request.user.id:
        return Response({"detail": "Результат отправляет сам сотрудник."}, status=403)
    return Response(
        {"detail": "Открой задание и отправь результат на проверку HR. Прямое завершение отключено."},
        status=409,
    )


@require_GET
def health(request):
    return JsonResponse({"status": "ok", "service": "career-quest"})
