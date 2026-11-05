"""Collects the planner's inputs from the database and stores its plans."""

import math
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from prices.converters import PriceSeries
from prices.pricing import charging_power_kw, day_bounds, floor_to_quarter
from tesla.models import CarObservation

from .models import ChargeTarget, EnergySettings, PlanSnapshot
from .planner import Need, Slot, make_plan


def pct_per_quarter():
    """Battery % gained per quarter of charging."""
    return charging_power_kw() / 4 / settings.CAR_BATTERY_KWH * 100


def price_marks(day):
    """25% mark and median of the actual price (c€/kWh incl. VAT) over the 30 days before `day`.

    Same window as the price list colours, but on the price you actually pay.
    """
    start, _ = day_bounds(day - timedelta(days=30))
    end, _ = day_bounds(day)
    values = sorted(p.consumption for p in PriceSeries().periods_between(start, end))
    if not values:
        return None
    n = len(values)
    return {"p25": values[math.floor(0.25 * n)], "median": values[math.floor(0.5 * n)]}


def cheap_reason(energy_settings, price, marks):
    """Why the price rule wants to charge at `price`, or "" if it doesn't."""
    rule = energy_settings.price_rule
    if rule == EnergySettings.RULE_BELOW_PRICE:
        limit = energy_settings.price_threshold
        if limit is not None and price < limit:
            return f"price below {limit:.2f} c€".replace(".", ",")
    elif rule == EnergySettings.RULE_BELOW_P25 and marks and price < marks["p25"]:
        return "price in the cheapest 25% of the last 30 days"
    elif rule == EnergySettings.RULE_BELOW_MEDIAN and marks and price < marks["median"]:
        return "price below the 30-day median"
    return ""


def latest_observation():
    return CarObservation.objects.exclude(battery_level=None).order_by("-first_seen_at", "-pk").first()


def upcoming_slots(now, energy_settings):
    today = timezone.localdate(now)
    _, end = day_bounds(today + timedelta(days=1))
    marks = price_marks(today) if energy_settings.price_rule in (
        EnergySettings.RULE_BELOW_P25, EnergySettings.RULE_BELOW_MEDIAN) else None
    slots = []
    for period in PriceSeries().periods_between(floor_to_quarter(now), end):
        cheap = cheap_reason(energy_settings, period.consumption, marks)
        slots += [Slot(q.start, q.end, period.consumption, cheap) for q in period.quarters]
    return slots


def needs(now):
    return [
        Need(deadline=t.deadline, pct=t.battery_pct, label=str(t))
        for t in ChargeTarget.objects.filter(deadline__gt=now, ignored=False)
    ]


def build_plan(now=None, battery_pct=None):
    """The plan for right now, or None when there's no battery level to start from."""
    now = now or timezone.now()
    if battery_pct is None:
        obs = latest_observation()
        if obs is None:
            return None
        battery_pct = obs.battery_level
    return make_plan(
        battery_pct, upcoming_slots(now, EnergySettings.load()), needs(now),
        now=now, pct_per_quarter=pct_per_quarter(),
    )


def _serialize(plan):
    return {
        "battery_pct": plan.battery_pct,
        "charge_limit": plan.charge_limit,
        "blocks": [
            {"start": b.start.isoformat(), "end": b.end.isoformat(), "reasons": b.reasons, "price": round(b.price, 3)}
            for b in plan.blocks
        ],
        "targets": [
            {"label": r.need.label, "deadline": r.need.deadline.isoformat(), "pct": r.need.pct,
             "expected": round(r.expected_pct, 1), "status": r.status}
            for r in plan.needs
        ],
        "warnings": [f"{r.need.label}: about {r.shortfall_pct:.0f}% short" for r in plan.problems],
    }


def save_snapshot(plan):
    """Store the plan if it differs from the last stored one. Returns (snapshot, created)."""
    data = _serialize(plan)
    last = PlanSnapshot.objects.first()
    if last and all(getattr(last, k) == v for k, v in data.items()):
        return last, False
    return PlanSnapshot.objects.create(created_at=plan.now, **data), True
