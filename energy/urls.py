from django.urls import path

from . import views

app_name = "energy"

urlpatterns = [
    path("", views.plan, name="plan"),
    path("targets/add/", views.add_target, name="add_target"),
    path("targets/<int:pk>/delete/", views.delete_target, name="delete_target"),
    path("targets/<int:pk>/ignore/", views.toggle_ignore, name="toggle_ignore"),
    path("settings/", views.settings_view, name="settings"),
]
