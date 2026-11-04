from datetime import date, datetime, timedelta

from django import forms
from django.shortcuts import render
from django.utils import timezone

from .entsoe import ensure_prices_for
from .models import PriceInterval
from .pricing import (
    day_bounds,
    dishwasher_options,
    ejection_price,
    floor_to_quarter,
    injection_price,
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
    current_quarter = floor_to_quarter(timezone.localtime())

    rows = []
    for interval in PriceInterval.objects.filter(start__gte=start, start__lt=end):
        injection = injection_price(interval.price)
        rows.append(
            {
                "start": timezone.localtime(interval.start),
                "end": timezone.localtime(interval.end),
                "ejection": ejection_price(interval.price),
                "ejection_class": price_class(interval.price, quartiles),
                "injection": injection,
                "injection_class": "text-danger" if injection < 0 else "",
                "is_current": interval.start == current_quarter,
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


def car_charging(request):
    form = CarChargingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _ensure_upcoming_prices()
        data = form.cleaned_data
        target_day = timezone.localdate()
        if data["day"] == "tomorrow":
            target_day += timedelta(days=1)
        deadline = datetime.combine(target_day, data["hour"], tzinfo=timezone.get_current_timezone())
        plan = plan_charging(data["current_percentage"], data["desired_percentage"], timezone.now(), deadline)
        return render(request, "prices/car_charging_result.html", {"plan": plan, "deadline": deadline})
    return render(request, "prices/car_charging.html", {"form": form})
