from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import DevelopmentRequestForm, PracticePublicationForm, ReviewForm, SubmissionForm
from .models import DatasetState, DevelopmentPlanItem, DevelopmentRequest, Employee, WorkSubmission
from .services.career import profile_context
from .services.plans import PlanConflict
from .services.practice import (
    brief_for,
    create_request,
    practice_template,
    publish_practice,
    review_work,
    submit_work,
)
from .views import accessible_employee, is_hr


@login_required
@require_http_methods(["GET", "POST"])
def development_requests(request, employee_id):
    employee = accessible_employee(request.user, employee_id)
    owner = employee.user_id == request.user.pk
    if request.method == "POST" and not owner:
        raise PermissionDenied
    ctx = profile_context(employee)
    form = DevelopmentRequestForm(
        request.POST or None, gaps=ctx["open_gaps"], initial={"skill": request.GET.get("skill")}
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            employee = Employee.objects.select_for_update().get(pk=employee.pk)
            try:
                create_request(employee, form.cleaned_data["skill"], form.cleaned_data["note"])
            except PlanConflict as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Запрос передан HR. Ответ и практика появятся на этой странице.")
                return redirect("development-requests", employee_id=employee.pk)
    ctx.update(
        page="requests",
        form=form,
        can_manage=owner,
        requests=employee.development_requests.select_related("skill", "event").all(),
    )
    return render(request, "quest/development_requests.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def workspace(request, employee_id, event_id):
    employee = accessible_employee(request.user, employee_id)
    item = get_object_or_404(
        DevelopmentPlanItem.objects.select_related("event", "participation"),
        employee=employee,
        event_id=event_id,
    )
    owner = employee.user_id == request.user.pk
    if request.method == "POST" and not owner:
        raise PermissionDenied
    submissions = list(item.submissions.select_related("reviewer"))
    last = submissions[0] if submissions else None
    form = SubmissionForm(
        request.POST or None,
        initial={
            "previous_attempt": last.attempt if last else 0,
            "body": last.body if last and last.status == "changes_requested" else "",
            "artifact_url": last.artifact_url if last and last.status == "changes_requested" else "",
        },
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            Employee.objects.select_for_update().get(pk=employee.pk)
            item = DevelopmentPlanItem.objects.select_related("event", "participation").get(pk=item.pk)
            try:
                submit_work(item, form.cleaned_data)
            except PlanConflict as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request, "Результат отправлен на проверку. Навыки обновятся после подтверждения."
                )
                return redirect("workspace", employee_id=employee.pk, event_id=event_id)
    ctx = profile_context(employee)
    after = next((s for s in submissions if s.status == "approved"), None)
    ctx.update(
        page="work",
        item=item,
        event=item.event,
        brief=brief_for(item.event),
        form=form,
        submissions=submissions,
        last_submission=last,
        approved_submission=after,
        can_submit=owner and item.status in {"in_progress", "needs_revision"},
        can_manage=owner,
    )
    return render(request, "quest/workspace.html", ctx)


@login_required
def hr_development(request):
    if not is_hr(request.user):
        raise PermissionDenied
    requests = DevelopmentRequest.objects.select_related("employee", "skill", "event")
    reviews = (
        WorkSubmission.objects.filter(status="pending")
        .select_related("plan_item__employee", "plan_item__event")
        .order_by("submitted_at")
    )
    return render(
        request,
        "quest/hr_development.html",
        {
            "page": "hr-development",
            "open_requests": requests.filter(status="open"),
            "handled_requests": requests.exclude(status="open")[:30],
            "reviews": reviews,
            "approved_count": WorkSubmission.objects.filter(status="approved").count(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def hr_request(request, request_id):
    if not is_hr(request.user):
        raise PermissionDenied
    development_request = get_object_or_404(
        DevelopmentRequest.objects.select_related("employee", "skill", "event"), pk=request_id
    )
    ctx = profile_context(development_request.employee)
    gap = next((g for g in ctx["open_gaps"] if g["skill_id"] == development_request.skill_id), None)
    initial = {
        **practice_template(development_request.skill),
        "gain": 1,
        "max_level": gap["required"] if gap else development_request.required_level,
    }
    closing = request.method == "POST" and request.POST.get("action") == "close"
    form = PracticePublicationForm(
        request.POST if request.method == "POST" and not closing else None, initial=initial
    )
    if request.method == "POST" and (closing or form.is_valid()):
        with transaction.atomic():
            # Same lock order as dataset import, which can affect event and employee eligibility.
            DatasetState.objects.select_for_update().get(key="main")
            Employee.objects.select_for_update().get(pk=development_request.employee_id)
            development_request = DevelopmentRequest.objects.select_related("employee", "skill", "event").get(
                pk=request_id
            )
            try:
                if closing:
                    feedback = request.POST.get("response", "").strip()
                    if development_request.status != "open" or not 10 <= len(feedback) <= 3000:
                        raise PlanConflict(
                            "Закрой необработанный запрос с объяснением от 10 до 3000 символов."
                        )
                    development_request.status, development_request.response = "closed", feedback
                    development_request.handled_by = request.user
                    development_request.save(update_fields=["status", "response", "handled_by", "updated_at"])
                else:
                    publish_practice(development_request, request.user, form.cleaned_data)
            except PlanConflict as exc:
                if closing:
                    messages.error(request, str(exc))
                else:
                    form.add_error(None, str(exc))
            else:
                messages.success(request, "Ответ сохранён. Сотрудник увидит его в своих запросах.")
                return redirect("hr-development")
    return render(
        request,
        "quest/hr_request.html",
        {"page": "hr-development", "development_request": development_request, "form": form, "gap": gap},
    )


@login_required
@require_http_methods(["GET", "POST"])
def hr_review(request, submission_id):
    if not is_hr(request.user):
        raise PermissionDenied
    submission = get_object_or_404(
        WorkSubmission.objects.select_related(
            "plan_item__employee", "plan_item__event", "plan_item__participation", "reviewer"
        ),
        pk=submission_id,
    )
    form = ReviewForm(request.POST or None, criteria=submission.criteria)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            Employee.objects.select_for_update().get(pk=submission.plan_item.employee_id)
            submission = WorkSubmission.objects.select_related(
                "plan_item__employee", "plan_item__event", "plan_item__participation"
            ).get(pk=submission_id)
            try:
                changed = review_work(submission, request.user, form.cleaned_data)
            except PlanConflict as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    "Решение сохранено и доступно сотруднику."
                    if changed
                    else "Эта версия уже проверена. Повторного начисления нет.",
                )
                return redirect("hr-development")
    return render(
        request,
        "quest/hr_review.html",
        {
            "page": "hr-development",
            "submission": submission,
            "item": submission.plan_item,
            "brief": brief_for(submission.plan_item.event),
            "form": form,
        },
    )
