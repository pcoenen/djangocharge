from django.contrib import admin

from .models import PriceInterval


@admin.register(PriceInterval)
class PriceIntervalAdmin(admin.ModelAdmin):
    list_display = ("start", "price")
    date_hierarchy = "start"
