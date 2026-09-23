import uuid

from django.db.models import Q

from quest.models import DevelopmentPlanItem, Participation, RecommendationCache
from quest.services.career import candidates_for, profile_context


class PlanConflict(ValueError):
    pass


def sync_plan_history(rows):
    """Reflect imported progress without creating participation or awarding skills."""
    for row in rows.filter(Q(status="in_progress") | Q(plan_item__isnull=False)):
        item = DevelopmentPlanItem.objects.filter(participation=row).first()
        if item:
            item.status = row.status if row.status in {"in_progress", "completed"} else "cancelled"
            item.save(update_fields=["status", "updated_at"])
        elif row.status == "in_progress" and not row.event.mandatory:
            item, _ = DevelopmentPlanItem.objects.get_or_create(employee=row.employee, event=row.event)
            if item.status in {"planned", "cancelled"}:
                item.status, item.participation = "in_progress", row
                item.save(update_fields=["status", "participation", "updated_at"])


def change_plan(employee, event, action):
    """Caller must hold the employee row lock for every state transition."""
    item = DevelopmentPlanItem.objects.filter(employee=employee, event=event).first()
    if action == "add" and item and item.status in {"planned", "in_progress"}:
        return item
    if action == "start" and item and item.status == "in_progress":
        return item
    if action == "cancel":
        if not item or item.status not in {"planned", "cancelled"}:
            raise PlanConflict("Убрать из плана можно только ещё не начатую активность.")
        item.status = "cancelled"
        item.save(update_fields=["status", "updated_at"])
    else:
        if action == "start" and (not item or item.status != "planned"):
            raise PlanConflict("Сначала добавьте активность в план.")
        ctx = profile_context(employee)
        candidates, _, _ = candidates_for(ctx, include_planned=True)
        if event.pk not in {c["event_id"] for c in candidates}:
            raise PlanConflict("Активность больше не подходит профилю или текущей цели. Обновите подбор.")
        if action == "add":
            item, _ = DevelopmentPlanItem.objects.update_or_create(
                employee=employee, event=event, defaults={"status": "planned", "participation": None}
            )
        elif action == "start":
            record = Participation.objects.create(
                record_id=f"PLAN_{uuid.uuid4().hex}",
                employee=employee,
                event=event,
                date=ctx["cat"]["today"],
                status="in_progress",
                completion_pct=0,
                assigned_by="self",
            )
            item.status, item.participation = "in_progress", record
            item.save(update_fields=["status", "participation", "updated_at"])
    RecommendationCache.objects.filter(employee=employee).delete()
    return item
