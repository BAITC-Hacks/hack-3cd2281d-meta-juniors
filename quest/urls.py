from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.AppLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("health/", views.health, name="health"),
    path("people/<str:employee_id>/", views.profile, name="profile"),
    path("people/<str:employee_id>/plan/", views.development_plan, name="plan"),
    path("people/<str:employee_id>/events/<str:event_id>/", views.event_detail, name="event"),
    path("hr/", views.hr_dashboard, name="hr"),
    path("hr/import/", views.import_page, name="import"),
    path("api/people/<str:employee_id>/", views.profile_api, name="profile-api"),
    path(
        "api/people/<str:employee_id>/recommendations/", views.recommendations_api, name="recommendations-api"
    ),
    path("api/people/<str:employee_id>/goal/", views.goal_api, name="goal-api"),
    path("api/people/<str:employee_id>/plan/<str:event_id>/<str:action>/", views.plan_api, name="plan-api"),
    path("api/people/<str:employee_id>/complete/<str:event_id>/", views.complete_api, name="complete-api"),
]
