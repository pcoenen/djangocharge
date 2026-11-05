from django.contrib import admin

from .models import ChargeTarget, EnergySettings, PlanSnapshot


@admin.register(ChargeTarget)
class ChargeTargetAdmin(admin.ModelAdmin):
    list_display = ("deadline", "battery_pct", "label", "source")
    list_filter = ("source",)


@admin.register(EnergySettings)
class EnergySettingsAdmin(admin.ModelAdmin):
    list_display = ("price_rule", "price_threshold", "updated_at")


@admin.register(PlanSnapshot)
class PlanSnapshotAdmin(admin.ModelAdmin):
    list_display = ("created_at", "battery_pct", "charge_limit")
