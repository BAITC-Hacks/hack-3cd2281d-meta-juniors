from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import BaseCommand

from quest.models import Employee


class Command(BaseCommand):
    help = "Seed once and create isolated synthetic demo accounts"

    def handle(self, *args, **kwargs):
        if not Employee.objects.exists():
            call_command("import_dataset", directory=str(settings.BASE_DIR / "data"))
        if settings.DEMO_MODE:
            group, _ = Group.objects.get_or_create(name="HR")
            for username in ("employee", "hr"):
                user, created = User.objects.get_or_create(username=username)
                if created:
                    user.set_password(settings.DEMO_PASSWORD)
                    user.save()
                if username == "hr":
                    user.groups.add(group)
                else:
                    Employee.objects.filter(pk="E0028").update(user=user)
            self.stdout.write("Demo accounts ready: employee / hr (password from DEMO_PASSWORD)")
