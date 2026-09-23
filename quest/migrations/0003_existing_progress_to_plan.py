from django.db import migrations


def seed_plans(apps, schema_editor):
    Participation = apps.get_model("quest", "Participation")
    Plan = apps.get_model("quest", "DevelopmentPlanItem")
    for row in Participation.objects.filter(status="in_progress", event__mandatory=False).order_by("date", "pk"):
        Plan.objects.update_or_create(
            employee_id=row.employee_id, event_id=row.event_id,
            defaults={"status": "in_progress", "participation_id": row.pk},
        )


class Migration(migrations.Migration):
    dependencies = [("quest", "0002_importdraft_developmentplanitem")]
    operations = [migrations.RunPython(seed_plans, migrations.RunPython.noop)]
