from django.db import migrations


def include_previous_demo_steps(apps, schema_editor):
    Participation = apps.get_model("quest", "Participation")
    Plan = apps.get_model("quest", "DevelopmentPlanItem")
    for row in Participation.objects.filter(
        status="completed", request_id__isnull=False, event__mandatory=False
    ).order_by("date", "pk"):
        Plan.objects.get_or_create(
            employee_id=row.employee_id, event_id=row.event_id,
            defaults={"status": "completed", "participation_id": row.pk},
        )


class Migration(migrations.Migration):
    dependencies = [("quest", "0003_existing_progress_to_plan")]
    operations = [migrations.RunPython(include_previous_demo_steps, migrations.RunPython.noop)]
