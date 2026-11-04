from django.urls import path

from . import views

app_name = "prices"

urlpatterns = [
    path("", views.index, name="index"),
    path("prices/", views.price_list, name="price_list"),
    path("dishwasher/", views.dishwasher, name="dishwasher"),
    path("car-charging/", views.car_charging, name="car_charging"),
]
