"""Price calculations, ported from the dyncharge PHP app."""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from .converters import PriceSeries
from .models import PriceInterval


def day_bounds(day):
    """Start and end (exclusive) of a local calendar day as aware datetimes.

    Handles DST days (92 or 100 quarters) correctly.
    """
    tz = timezone.get_current_timezone()
    # Convert to UTC: subtracting two aware datetimes that share a tzinfo
    # uses wall-clock time and would ignore the DST shift.
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def floor_to_quarter(dt):
    return dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)


def quartiles_for_date(day):
    """25/50/75th percentile of market prices over the 30 days before `day`."""
    start, _ = day_bounds(day - timedelta(days=30))
    end, _ = day_bounds(day)
    prices = sorted(
        PriceInterval.objects.filter(start__gte=start, start__lt=end).values_list("price", flat=True)
    )
    if not prices:
        return [0, 100, 100]
    n = len(prices)
    return [prices[math.floor(q * n)] for q in (0.25, 0.5, 0.75)]


def price_class(price, quartiles):
    if price > quartiles[2]:
        return "text-danger"
    if price > quartiles[1]:
        return "text-warning"
    if price < quartiles[0]:
        return "text-success"
    return ""


def get_price(dt):
    """Market price of the quarter containing `dt`, or None if unknown."""
    return PriceInterval.objects.filter(start=floor_to_quarter(dt)).values_list("price", flat=True).first()


# --- Dishwasher -------------------------------------------------------------

# (offset from start, kWh used) for a dishwasher cycle
DISHWASHER_PROFILE = [
    (timedelta(minutes=0), 0.25),
    (timedelta(minutes=15), 2),  # first heating peak
    (timedelta(minutes=75), 1),
    (timedelta(minutes=135), 2),  # second heating peak
]

DISHWASHER_OPTIONS = [
    ("Now", timedelta(hours=0)),
    ("Three hours", timedelta(hours=3)),
    ("Six hours", timedelta(hours=6)),
    ("Nine hours", timedelta(hours=9)),
]


def dishwasher_cost(start):
    """Relative cost of a cycle starting at `start`; None if prices are missing."""
    total = 0
    for offset, kwh in DISHWASHER_PROFILE:
        price = get_price(start + offset)
        if price is None:
            return None
        total += price * kwh
    return total


def dishwasher_options(now):
    options = [{"label": label, "cost": dishwasher_cost(now + delta)} for label, delta in DISHWASHER_OPTIONS]
    known = [o["cost"] for o in options if o["cost"] is not None]
    min_cost = min(known) if known else None
    for o in options:
        if o["cost"] is None:
            o["percentage"] = None
            o["color"] = "transparent"
            continue
        denominator = abs(min_cost) or 1
        pct = round((o["cost"] - min_cost) / denominator * 100 + 100)
        opacity = round(max(0, 100 - (pct - 100) / 2) / 100, 2)
        o["percentage"] = pct
        o["color"] = f"rgba(0, 255, 0, {opacity})"
    return options


# --- Car charging -------------------------------------------------------------


@dataclass
class ChargingPlan:
    needed_percentage: int
    needed_kwh: float
    needed_quarters: int
    groups: list = field(default_factory=list)
    found_quarters: int = 0
    average_energy_price: float = 0.0  # EUR/kWh
    kwh_from_grid: float = 0.0

    @property
    def not_enough_time(self):
        return self.found_quarters < self.needed_quarters

    @property
    def extra_hours_needed(self):
        return (self.needed_quarters - self.found_quarters) / 4

    @property
    def average_total_price(self):
        return self.average_energy_price + settings.GRID_FEE_PER_KWH

    @property
    def total_energy_cost(self):
        return self.kwh_from_grid * self.average_energy_price

    @property
    def total_cost(self):
        return self.kwh_from_grid * self.average_total_price


def charging_power_kw():
    s = settings
    return s.CAR_CHARGE_AMPS * s.CAR_CHARGE_VOLTS * s.CAR_CHARGE_PHASES * s.CAR_CHARGE_EFFICIENCY / 1000


def plan_charging(current_pct, desired_pct, now, deadline):
    """Pick the cheapest quarters between now and the deadline."""
    needed_pct = desired_pct - current_pct
    needed_kwh = max(needed_pct, 0) / 100 * settings.CAR_BATTERY_KWH
    needed_quarters = math.ceil(needed_kwh / charging_power_kw() * 4)
    plan = ChargingPlan(needed_percentage=needed_pct, needed_kwh=needed_kwh, needed_quarters=needed_quarters)
    if needed_quarters == 0:
        return plan

    # Rank quarters by what the tariff actually charges: with an hourly tariff
    # all quarters of an hour cost the same, whatever their own market price.
    # The car can start/stop per quarter, so we still pick individual quarters.
    candidates = [
        (period.consumption, quarter)
        for period in PriceSeries().periods_between(now, deadline)
        for quarter in period.quarters
        if now < quarter.start < deadline
    ]
    candidates.sort(key=lambda c: (c[0], c[1].start))
    chosen = sorted(candidates[:needed_quarters], key=lambda c: c[1].start)
    cheapest = [q for _, q in chosen]
    plan.found_quarters = len(cheapest)
    if not cheapest:
        return plan

    tz = timezone.get_current_timezone()
    groups = []
    for interval in cheapest:
        start, end = interval.start.astimezone(tz), interval.end.astimezone(tz)
        # Extend the current block if consecutive and on the same day (blocks split at midnight)
        if groups and groups[-1]["end"] == start and groups[-1]["start"].date() == start.date():
            groups[-1]["end"] = end
        else:
            groups.append({"start": start, "end": end})
    for g in groups:
        g["day"] = g["start"].strftime("%A")
        g["end_label"] = "24:00" if g["end"].date() > g["start"].date() else g["end"].strftime("%H:%M")
    plan.groups = groups

    plan.average_energy_price = sum(price for price, _ in chosen) / len(chosen) / 100
    plan.kwh_from_grid = needed_kwh * settings.CAR_CHARGE_LOSS_FACTOR
    return plan
