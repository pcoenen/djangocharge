from datetime import timedelta
from decimal import Decimal

from django.db import models

from .converters import converter_choices

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


class EnergyContract(models.Model):
    """Converts raw ENTSO-E market prices into what a provider actually charges/pays.

    Each contract applies from `valid_from` (local date) until the next contract
    starts, so history keeps being converted with the contract that applied then.

        consumption (c€/kWh) = (consumption_factor * market + consumption_offset) / 10 * (1 + vat_rate/100)
        injection   (c€/kWh) = (injection_factor * market + injection_offset) / 10

    with market and offsets in EUR/MWh. `converter` names the converter class
    (see prices/converters.py) that groups quarters into billing periods
    (quarters, hours, ...) and applies the formula.
    """

    name = models.CharField(max_length=100, help_text="e.g. provider + product name")
    valid_from = models.DateField(unique=True, help_text="First day this contract applies")
    converter = models.CharField(
        max_length=50,
        choices=converter_choices,
        default="quarter",
        help_text="How market prices are grouped and converted for this tariff",
    )
    consumption_factor = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("1.02"))
    consumption_offset = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("4"), help_text="EUR/MWh added to the market price"
    )
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("6"), help_text="VAT on consumption in %"
    )
    injection_factor = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0.98"))
    injection_offset = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("-15"), help_text="EUR/MWh added (usually negative)"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-valid_from"]

    def __str__(self):
        return f"{self.name} (from {self.valid_from})"

    def get_converter(self):
        from .converters import get_converter

        return get_converter(self)

    def consumption_price(self, market_price):
        """Consumption price in c€/kWh, incl. VAT."""
        mwh = float(self.consumption_factor) * market_price + float(self.consumption_offset)
        return mwh / 10 * (1 + float(self.vat_rate) / 100)

    def injection_price(self, market_price):
        """Injection price in c€/kWh."""
        return (float(self.injection_factor) * market_price + float(self.injection_offset)) / 10
