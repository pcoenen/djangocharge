from django.db import models
from django.utils import timezone


class TeslaAccount(models.Model):
    """OAuth tokens of the connected Tesla account (there is only one)."""

    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Tesla account (connected {self.connected_at:%Y-%m-%d})"


class TeslaVehicle(models.Model):
    account = models.ForeignKey(TeslaAccount, on_delete=models.CASCADE, related_name="vehicles")
    vin = models.CharField(max_length=17, unique=True)
    vehicle_id = models.BigIntegerField(null=True, blank=True)
    name = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name or self.vin


class ChargePlanSubmission(models.Model):
    """A charge plan sent to the car, so its schedules can be replaced later."""

    vehicle = models.ForeignKey(TeslaVehicle, on_delete=models.CASCADE, related_name="submissions")
    created_at = models.DateTimeField(auto_now_add=True)
    charge_limit = models.PositiveSmallIntegerField()
    deadline = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=20, default="manual", help_text="manual or auto (energy manager)")
    reason = models.CharField(max_length=300, blank=True, help_text="Why it was sent")
    schedules = models.JSONField(default=list, help_text="Schedules sent: [{id, day, start, end}]")
    succeeded = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    replaced = models.BooleanField(default=False, help_text="Schedules removed from the car again")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Plan for {self.vehicle} sent {self.created_at:%Y-%m-%d %H:%M}"


class CarObservation(models.Model):
    """What the car last reported to Tesla, read from the cache (never wakes the car).

    The cache only changes when the car reports, so a row is added only when the
    report changed; repeated checks of the same report just move `checked_at`.
    """

    vehicle = models.ForeignKey(TeslaVehicle, on_delete=models.CASCADE, related_name="observations")
    reported_at = models.DateTimeField(null=True, blank=True, help_text="When the car reported this")
    first_seen_at = models.DateTimeField(default=timezone.now)
    checked_at = models.DateTimeField(default=timezone.now, help_text="Last check that still saw this report")
    battery_level = models.PositiveSmallIntegerField(null=True, blank=True)
    charge_limit = models.PositiveSmallIntegerField(null=True, blank=True)
    charging_state = models.CharField(max_length=20, blank=True)
    charger_power = models.FloatField(null=True, blank=True, help_text="kW")
    range_km = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-first_seen_at", "-pk"]

    def __str__(self):
        return f"{self.vehicle} {self.battery_level}% ({self.charging_state or '?'})"

    @property
    def plugged_in(self):
        return self.charging_state not in ("", "Disconnected")

    def same_report(self, data):
        return all(getattr(self, k) == data.get(k) for k in ("reported_at", "battery_level", "charging_state"))
