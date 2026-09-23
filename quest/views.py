import uuid
from collections import defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods
from pydantic import ValidationError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Employee, Event, Participation, RecommendationCache, RoleProfile
from .schemas import GoalInput
from .services.career import STATUS_NAMES, candidates_for, hr_summary, profile_context
from .services.importer import ImportFailure, import_dataset
from .services.recommendations import get_recommendations


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
    ctx["can_complete"] = employee.user_id == request.user.id and settings.DEMO_MODE
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
    departments = list(
        Employee.objects.order_by("department").values_list("department", flat=True).distinct()
    )
    if department:
        employees = employees.filter(department=department)
    ctx = hr_summary(employees)
    ctx.update(page="hr", departments=departments, department=department)
    return render(request, "quest/hr.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def import_page(request):
    if not is_hr(request.user):
        raise PermissionDenied
    result = None
    if request.method == "POST":
        try:
            if not request.FILES:
                raise ImportFailure("Выберите хотя бы один файл.")
            data = {}
            for key in ("employees", "history", "skills", "events"):
                uploaded = request.FILES.get(key)
                if uploaded:
                    if uploaded.size > 10 * 1024 * 1024:
                        raise ImportFailure("Размер файла не должен превышать 10 МБ.")
                    data[key] = uploaded.read()
            if not data:
                raise ImportFailure("Файлы не распознаны.")
            result = import_dataset(**data)
            messages.success(request, "Данные загружены. Новые профили доступны на HR-экране.")
        except ImportFailure as exc:
            messages.error(request, str(exc))
    return render(request, "quest/import.html", {"page": "import", "result": result})


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
    if employee.user_id != request.user.id or not settings.DEMO_MODE:
        return Response(
            {"detail": "Демонстрация выполнения доступна только владельцу профиля в демо-режиме."}, status=403
        )
    try:
        request_id = uuid.UUID(str(request.data.get("request_id", "")))
    except ValueError:
        return Response({"detail": "Нужен request_id в формате UUID."}, status=400)
    with transaction.atomic():
        employee = Employee.objects.select_for_update().get(pk=employee.pk)
        previous = Participation.objects.filter(request_id=request_id).first()
        if previous:
            if previous.employee_id != employee.pk or previous.event_id != event_id:
                return Response({"detail": "request_id уже используется другой операцией."}, status=409)
            return Response({"completed": True, "already_completed": True, "record_id": previous.pk})
        event = get_object_or_404(Event, pk=event_id)
        ctx = profile_context(employee)
        candidates, _, _ = candidates_for(ctx)
        if event_id not in {c["event_id"] for c in candidates}:
            return Response(
                {
                    "detail": "Активность уже завершена или больше не подходит текущему профилю. Обновите рекомендации."
                },
                status=409,
            )
        record = Participation.objects.create(
            record_id=f"DEMO_{request_id.hex}",
            request_id=request_id,
            employee=employee,
            event=event,
            date=ctx["cat"]["today"],
            status="completed",
            completion_pct=100,
            assigned_by="self",
        )
        RecommendationCache.objects.filter(employee=employee).delete()
        updated = profile_context(employee)
        return Response(
            {
                "completed": True,
                "already_completed": False,
                "record_id": record.pk,
                "coverage_before": ctx["coverage"],
                "coverage_after": updated["coverage"],
                "changes": {
                    k: [ctx["levels"].get(k, 0), v]
                    for k, v in updated["levels"].items()
                    if v != ctx["levels"].get(k, 0)
                },
            }
        )


@require_GET
def health(request):
    return JsonResponse({"status": "ok", "service": "career-quest"})
