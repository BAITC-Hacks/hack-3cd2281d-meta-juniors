from django.shortcuts import render
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(exc, NotAuthenticated):
        response.data = {
            "code": "session_expired",
            "detail": "Сессия завершилась. Войди снова, затем повтори действие.",
        }
    elif (
        response is not None
        and isinstance(exc, PermissionDenied)
        and str(exc.detail).startswith("CSRF Failed")
    ):
        response.data = {"detail": "Форма устарела. Обнови страницу и повтори действие."}
    return response


def permission_denied(request, exception=None):
    return render(
        request,
        "quest/error.html",
        {
            "error_code": 403,
            "error_title": "Для этого действия нет доступа",
            "error_text": "Открой свой кабинет. Если тебе нужен доступ к этой функции, обратись к администратору.",
        },
        status=403,
    )


def page_not_found(request, exception=None):
    return render(
        request,
        "quest/error.html",
        {
            "error_code": 404,
            "error_title": "Страница недоступна",
            "error_text": "Проверь адрес или вернись в свой кабинет, чтобы продолжить.",
        },
        status=404,
    )


def csrf_failure(request, reason=""):
    return render(
        request,
        "quest/error.html",
        {
            "error_code": 403,
            "error_title": "Форма устарела",
            "error_text": "Действие не выполнено. Обнови страницу формы и попробуй снова. Если сессия завершилась, войди в свой кабинет.",
        },
        status=403,
    )
