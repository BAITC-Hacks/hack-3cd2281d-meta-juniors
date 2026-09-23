from django.contrib import admin

from .models import Employee, Event, Participation, RoleProfile, Skill

for model in (Employee, Event, Participation, RoleProfile, Skill):
    admin.site.register(model)
