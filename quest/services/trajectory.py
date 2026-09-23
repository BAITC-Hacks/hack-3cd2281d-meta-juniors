from types import SimpleNamespace

from quest.services.career import apply_gain, candidates_for, eligibility
from quest.services.practice import ACTIVE_PLAN_STATUSES


def trajectory_for(ctx):
    employee, target = ctx["employee"], ctx["target"]
    items = list(
        employee.plan_items.filter(status__in=ACTIVE_PLAN_STATUSES).select_related("event", "participation")
    )
    candidates = candidates_for(ctx, include_planned=True)[0]
    focus = next(iter(ctx["critical_gaps"] or ctx["open_gaps"]), None)
    forecast, history = dict(ctx["levels"]), list(ctx["history"])
    included = []
    # Started work precedes planned work; each event applies to the projected levels.
    for item in sorted(items, key=lambda i: (i.status == "planned", i.created_at, i.pk)):
        other_history = [h for h in history if h.pk != item.participation_id]
        reasons = eligibility(employee, item.event, forecast, other_history, ctx["cat"]["today"])
        if reasons and item.status == "planned":
            continue
        if item.event.mandatory:
            continue
        forecast = apply_gain(forecast, item.event)
        included.append(item)
        history = other_history + [
            SimpleNamespace(
                pk=f"FORECAST_{item.pk}", event_id=item.event_id, status="completed", date=ctx["cat"]["today"]
            )
        ]
    required = target.required_skills if target else {}
    total = sum(required.values())
    projected = (
        round(sum(min(forecast.get(k, 0), v) for k, v in required.items()) / total * 100) if total else 0
    )
    action_item, action_candidate, development_request = None, None, None
    if focus:
        key = focus["skill_id"]
        action_item = next(
            (
                i
                for i in included
                if any(
                    e["skill_id"] == key
                    and min(e["max_level"], ctx["levels"].get(key, 0) + e["gain"]) > ctx["levels"].get(key, 0)
                    for e in i.event.develops_skills
                )
            ),
            None,
        )
        action_candidate = next(
            (c for c in candidates if any(b["skill_id"] == key for b in c["benefits"])), None
        )
        development_request = employee.development_requests.filter(
            skill_id=key, status__in=["open", "proposed"]
        ).first()
    return {
        "focus": focus,
        "projected_coverage": projected,
        "projected_gain": max(0, projected - ctx["coverage"]),
        "forecast_items": included,
        "forecast_count": len(included),
        "active_count": len(items),
        "action_item": action_item,
        "action_candidate": action_candidate,
        "development_request": development_request,
        "forecast_skills": [
            {"name": ctx["cat"]["skills"][k].name, "before": ctx["levels"].get(k, 0), "after": v}
            for k, v in forecast.items()
            if v > ctx["levels"].get(k, 0)
        ],
    }
