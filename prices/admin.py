from django.contrib import admin

from .models import EnergyContract, PriceInterval


@admin.register(PriceInterval)
class PriceIntervalAdmin(admin.ModelAdmin):
    list_display = ("start", "price")
    date_hierarchy = "start"


@admin.register(EnergyContract)
class EnergyContractAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "valid_from",
        "converter",
        "consumption_factor",
        "consumption_offset",
        "vat_rate",
        "injection_factor",
        "injection_offset",
    )
