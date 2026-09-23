from django.contrib import admin
from django.urls import include, path

urlpatterns = [path("admin/", admin.site.urls), path("", include("quest.urls"))]

handler403 = "quest.http_errors.permission_denied"
handler404 = "quest.http_errors.page_not_found"
