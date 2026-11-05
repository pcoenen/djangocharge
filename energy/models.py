from django.db import models
from django.utils import timezone


class ChargeTarget(models.Model):
    """The car must have at least `battery_pct` % at `deadline`."""

    MANUAL = "manual"
    CALENDAR = "calendar"
    SOURCES = [(MANUAL, "Manual"), (CALENDAR, "Calendar")]

    battery_pct = models.PositiveSmallIntegerField()
    deadline = models.DateTimeField()
    label = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=20, choices=SOURCES, default=MANUAL)
    external_id = models.CharField(max_length=500, blank=True, help_text="Calendar event UID")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["deadline"]

    def __str__(self):
        when = timezone.localtime(self.deadline).strftime("%a %d/%m %H:%M")
        return f"{self.battery_pct}% by {when}" + (f" ({self.label})" if self.label else "")


class EnergySettings(models.Model):
    """Single row with the price rule: when to charge regardless of targets."""

    RULE_OFF = "off"
    RULE_BELOW_PRICE = "below_price"
    RULE_BELOW_P25 = "below_p25"
    RULE_BELOW_MEDIAN = "below_median"
    RULES = [
        (RULE_OFF, "Off: only charge for targets"),
        (RULE_BELOW_PRICE, "Below a fixed price"),
        (RULE_BELOW_P25, "Below the cheapest 25% of the last 30 days"),
        (RULE_BELOW_MEDIAN, "Below the median of the last 30 days"),
    ]

    price_rule = models.CharField(max_length=20, choices=RULES, default=RULE_OFF)
    price_threshold = models.FloatField(
        null=True, blank=True, help_text="c€/kWh incl. VAT, as shown in the price list"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "energy settings"

    def __str__(self):
        return self.get_price_rule_display()

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PlanSnapshot(models.Model):
    """A plan made by the energy tick; stored when it differs from the previous one."""

    created_at = models.DateTimeField(default=timezone.now)
    battery_pct = models.FloatField(null=True, help_text="Battery level the plan started from")
    charge_limit = models.PositiveSmallIntegerField(null=True, blank=True)
    blocks = models.JSONField(default=list, help_text="[{start, end, reasons, price}]")
    targets = models.JSONField(default=list, help_text="Expected battery level per target")
    warnings = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Plan {self.created_at:%Y-%m-%d %H:%M} ({len(self.blocks)} blocks)"
