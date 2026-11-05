from django.contrib import admin

from .models import ChargePlanSubmission, TeslaAccount, TeslaVehicle


@admin.register(TeslaAccount)
class TeslaAccountAdmin(admin.ModelAdmin):
    list_display = ("connected_at", "expires_at")
    exclude = ("access_token", "refresh_token")


@admin.register(TeslaVehicle)
class TeslaVehicleAdmin(admin.ModelAdmin):
    list_display = ("name", "vin", "vehicle_id")


@admin.register(ChargePlanSubmission)
class ChargePlanSubmissionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "vehicle", "charge_limit", "deadline", "succeeded", "replaced")
    readonly_fields = ("created_at",)
