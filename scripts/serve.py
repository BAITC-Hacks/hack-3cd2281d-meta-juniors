"""One-command demo startup. Existing data is never reset."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    import django

    django.setup()
    from django.core.management import call_command
    from waitress import serve

    from config.wsgi import application

    call_command("migrate", interactive=False)
    call_command("bootstrap_demo")
    serve(application, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threads=8)


if __name__ == "__main__":
    main()
