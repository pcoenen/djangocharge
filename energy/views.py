from datetime import timedelta

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from prices.views import _ensure_upcoming_prices

from tesla.models import TeslaVehicle

from . import manager, sync
from .models import ChargeTarget, EnergySettings, PlanSnapshot


class TargetForm(forms.ModelForm):
    deadline = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )
    battery_pct = forms.IntegerField(min_value=1, max_value=100, initial=80)

    class Meta:
        model = ChargeTarget
        fields = ["battery_pct", "deadline", "label"]
        widgets = {"label": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional, e.g. trip to Ghent"})}

    def clean_deadline(self):
        deadline = self.cleaned_data["deadline"]
        if deadline <= timezone.now():
            raise forms.ValidationError("Pick a time in the future.")
        return deadline


class SettingsForm(forms.ModelForm):
    class Meta:
        model = EnergySettings
        fields = ["price_rule", "price_threshold"]
        widgets = {
            "price_rule": forms.RadioSelect(attrs={"class": "form-check-input"}),
            "price_threshold": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
        }

    def clean(self):
        data = super().clean()
        if data.get("price_rule") == EnergySettings.RULE_BELOW_PRICE and data.get("price_threshold") is None:
            self.add_error("price_threshold", "Enter the price below which the car should always charge.")
        return data


def plan(request):
    _ensure_upcoming_prices()
    now = timezone.now()
    default_deadline = timezone.localtime(now).replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(days=1)
    plan = manager.build_plan(now)
    vehicle = TeslaVehicle.objects.first()
    decision = sync.decide(plan, vehicle, manager.latest_observation(), now) if plan and vehicle else None
    on_car = sync.on_car(vehicle) if vehicle else None
    return render(request, "energy/plan.html", {
        "plan": plan,
        "decision": decision,
        "on_car": on_car,
        "on_car_blocks": sync._submission_blocks(on_car) if on_car else [],
        "observation": manager.latest_observation(),
        "targets": ChargeTarget.objects.filter(deadline__gt=now),
        "form": TargetForm(initial={"deadline": default_deadline}),
        "settings": EnergySettings.load(),
        "snapshots": PlanSnapshot.objects.all()[:5],
        "max_sends": sync.MAX_SENDS_PER_DAY,
    })


@require_POST
def add_target(request):
    form = TargetForm(request.POST)
    if form.is_valid():
        target = form.save()
        messages.success(request, f"Target added: {target}.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("energy:plan")


@require_POST
def delete_target(request, pk):
    target = get_object_or_404(ChargeTarget, pk=pk, source=ChargeTarget.MANUAL)
    target.delete()
    messages.success(request, f"Target removed: {target}.")
    return redirect("energy:plan")


@require_POST
def toggle_ignore(request, pk):
    target = get_object_or_404(ChargeTarget, pk=pk, source=ChargeTarget.CALENDAR)
    target.ignored = not target.ignored
    target.save(update_fields=["ignored"])
    if target.ignored:
        messages.success(request, f"No charging for {target.label} any more.")
    else:
        messages.success(request, f"Charging for {target.label} again.")
    return redirect("energy:plan")


def settings_view(request):
    obj = EnergySettings.load()
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("energy:plan")
    return render(request, "energy/settings.html", {
        "form": form,
        "marks": manager.price_marks(timezone.localdate()),
    })
