from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from quest.services.importer import ImportFailure, import_dataset


class Command(BaseCommand):
    help = "Import starter kit or additional profiles/history atomically"

    def add_arguments(self, parser):
        parser.add_argument("--directory", default="data")

    def handle(self, *args, **options):
        root = Path(options["directory"])
        kwargs = {}
        for key, name in {
            "employees": "employees.json",
            "history": "activity_history.csv",
            "skills": "skills.json",
            "events": "events.json",
        }.items():
            path = root / name
            if path.is_file():
                kwargs[key] = path.read_bytes()
        if not kwargs:
            raise CommandError("No dataset files found")
        try:
            self.stdout.write(str(import_dataset(**kwargs)))
        except ImportFailure as exc:
            raise CommandError(str(exc)) from exc
