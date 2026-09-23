from django.conf import settings


def app_context(request):
    return {
        "demo_mode": settings.DEMO_MODE,
        "is_hr": request.user.is_authenticated
        and (request.user.is_superuser or request.user.groups.filter(name="HR").exists()),
    }
