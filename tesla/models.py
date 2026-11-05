from django.db import models


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
    deadline = models.DateTimeField()
    schedules = models.JSONField(default=list, help_text="Schedules sent: [{id, day, start, end}]")
    succeeded = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    replaced = models.BooleanField(default=False, help_text="Schedules removed from the car again")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Plan for {self.vehicle} sent {self.created_at:%Y-%m-%d %H:%M}"
