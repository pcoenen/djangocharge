from datetime import timedelta

from django.db import models

QUARTER = timedelta(minutes=15)


class PriceInterval(models.Model):
    """Day-ahead market price for one 15-minute interval (Belgium, ENTSO-E)."""

    start = models.DateTimeField(unique=True)
    price = models.FloatField(help_text="Market price in EUR/MWh")

    class Meta:
        ordering = ["start"]

    def __str__(self):
        return f"{self.start:%Y-%m-%d %H:%M} {self.price} EUR/MWh"

    @property
    def end(self):
        return self.start + QUARTER
