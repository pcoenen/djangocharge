from datetime import date, datetime, timedelta

from django import forms
from django.shortcuts import render
from django.utils import timezone

from .entsoe import ensure_prices_for
from .converters import PriceSeries
from .pricing import (
    day_bounds,
    dishwasher_options,
    plan_charging,
    price_class,
    quartiles_for_date,
)


def index(request):
    return render(request, "prices/index.html")


def price_list(request):
    today = timezone.localdate()
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = today

    ensure_prices_for(day)
    quartiles = quartiles_for_date(day)
    start, end = day_bounds(day)
    now = timezone.now()

    # The tariff's converter decides whether rows are quarters, hours, ...
    rows = []
    for period in PriceSeries().periods_between(start, end):
        rows.append(
            {
                "start": timezone.localtime(period.start),
                "end": timezone.localtime(period.end),
                "ejection": period.consumption,
                "ejection_class": price_class(period.market_price, quartiles),
                "injection": period.injection,
                "injection_class": "text-danger" if period.injection < 0 else "",
                "is_current": period.contains(now),
            }
        )

    return render(
        request,
        "prices/price_list.html",
        {
            "day": day,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "rows": rows,
        },
    )


def _ensure_upcoming_prices():
    today = timezone.localdate()
    ensure_prices_for(today)
    ensure_prices_for(today + timedelta(days=1))


def dishwasher(request):
    _ensure_upcoming_prices()
    return render(request, "prices/dishwasher.html", {"options": dishwasher_options(timezone.now())})


class CarChargingForm(forms.Form):
    current_percentage = forms.IntegerField(min_value=0, max_value=100, initial=20)
    desired_percentage = forms.IntegerField(min_value=0, max_value=100, initial=100)
    day = forms.ChoiceField(choices=[("today", "Today"), ("tomorrow", "Tomorrow")], initial="tomorrow")
    hour = forms.TimeField(input_formats=["%H:%M"], initial="07:00")


def plan_from_form(data):
    """The charging plan for a validated CarChargingForm. Returns (plan, deadline)."""
    _ensure_upcoming_prices()
    target_day = timezone.localdate()
    if data["day"] == "tomorrow":
        target_day += timedelta(days=1)
    deadline = datetime.combine(target_day, data["hour"], tzinfo=timezone.get_current_timezone())
    plan = plan_charging(data["current_percentage"], data["desired_percentage"], timezone.now(), deadline)
    return plan, deadline


def car_charging(request):
    from tesla.client import car_summary

    car = car_summary() if request.method == "GET" else None
    initial = {"current_percentage": car["battery_level"]} if car and car.get("battery_level") is not None else {}
    form = CarChargingForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        plan, deadline = plan_from_form(form.cleaned_data)
        return render(
            request,
            "prices/car_charging_result.html",
            {"plan": plan, "deadline": deadline, "form_data": request.POST, "tesla_connected": car_summary(fetch=False)},
        )
    return render(request, "prices/car_charging.html", {"form": form, "car": car})
