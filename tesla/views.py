import secrets

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import client
from .models import ChargePlanSubmission, TeslaAccount, TeslaVehicle


def _configured():
    return bool(settings.TESLA_CLIENT_ID and settings.TESLA_CLIENT_SECRET and settings.TESLA_PRIVATE_KEY_B64)


def _with_changes(observations):
    """Attach the battery change versus the previous (older) report to each observation."""
    for newer, older in zip(observations, observations[1:] + [None]):
        newer.delta = None
        if older and newer.battery_level is not None and older.battery_level is not None:
            newer.delta = newer.battery_level - older.battery_level
    return observations


def dashboard(request):
    """Connection status: account, virtual key, last reported car state. Never wakes the car."""
    ctx = {
        "configured": _configured(),
        "account": TeslaAccount.objects.first(),
        "vehicle": TeslaVehicle.objects.first(),
        "add_key_url": f"https://tesla.com/_ak/{settings.TESLA_DOMAIN}",
        "home_set": bool(settings.TESLA_HOME_LAT or settings.TESLA_HOME_LON),
        "submissions": ChargePlanSubmission.objects.select_related("vehicle")[:5],
        "errors": [],
    }
    vehicle = ctx["vehicle"]
    if ctx["account"] and vehicle:
        try:
            ctx["fleet"] = client.fleet_status(vehicle.vin)
        except client.TeslaError as e:
            ctx["errors"].append(str(e))
        try:
            client.observe(vehicle)
        except client.TeslaError as e:
            ctx["errors"].append(f"Couldn't read the car's cached state: {e}")
        ctx["observations"] = _with_changes(list(vehicle.observations.all()[:20]))
        ctx["latest"] = ctx["observations"][0] if ctx["observations"] else None
    return render(request, "tesla/dashboard.html", ctx)


def connect(request):
    state = secrets.token_urlsafe(16)
    request.session["tesla_oauth_state"] = state
    return redirect(client.authorize_url(state))


def callback(request):
    expected = request.session.pop("tesla_oauth_state", None)
    if request.GET.get("error"):
        messages.error(request, f"Tesla login was cancelled: {request.GET.get('error_description') or request.GET['error']}")
    elif not expected or request.GET.get("state") != expected:
        messages.error(request, "Tesla login failed: invalid state. Please try again.")
    elif not request.GET.get("code"):
        messages.error(request, "Tesla login failed: no authorization code received.")
    else:
        try:
            client.connect(request.GET["code"])
            messages.success(request, "Tesla account connected.")
        except client.TeslaError as e:
            messages.error(request, f"Connecting the Tesla account failed: {e}")
    return redirect("tesla:dashboard")


@require_POST
def disconnect(request):
    TeslaAccount.objects.all().delete()
    messages.success(request, "Tesla account disconnected.")
    return redirect("tesla:dashboard")


@require_POST
def refresh_vehicles(request):
    try:
        vehicles = client.sync_vehicles()
        messages.success(request, f"Found {len(vehicles)} car(s).")
    except client.TeslaError as e:
        messages.error(request, str(e))
    return redirect("tesla:dashboard")


@require_POST
def wake(request):
    try:
        client.wake_up(client.get_vehicle().vin)
        messages.success(request, "The car is awake.")
    except client.TeslaError as e:
        messages.error(request, str(e))
    return redirect("tesla:dashboard")
