from django.urls import path

from . import views

app_name = "tesla"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("connect/", views.connect, name="connect"),
    path("callback/", views.callback, name="callback"),
    path("disconnect/", views.disconnect, name="disconnect"),
    path("vehicles/refresh/", views.refresh_vehicles, name="refresh_vehicles"),
    path("wake/", views.wake, name="wake"),
    path("send-plan/", views.send_plan, name="send_plan"),
]
