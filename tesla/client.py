"""Tesla Fleet API client.

Read-only calls (vehicle list, status, charge state) go straight to the Fleet API.
Commands are signed with our private key (Vehicle Command Protocol) via the
`tesla-fleet-api` library, so no separate signing proxy is needed.
"""

import asyncio
import base64
import logging
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import CarObservation, ChargePlanSubmission, TeslaAccount, TeslaVehicle

logger = logging.getLogger(__name__)

MIN_CHARGE_LIMIT = 50  # Tesla doesn't accept a lower charge limit


class TeslaError(Exception):
    pass


class NotConnected(TeslaError):
    pass


class VehicleAsleep(TeslaError):
    pass


class AuthError(TeslaError):
    """The Tesla login no longer works (refresh token expired or revoked): reconnect."""


# --- OAuth -------------------------------------------------------------------


def authorize_url(state):
    return settings.TESLA_AUTH_URL + "?" + urlencode(
        {
            "client_id": settings.TESLA_CLIENT_ID,
            "locale": "en-US",
            "prompt": "login",
            "redirect_uri": settings.TESLA_REDIRECT_URI,
            "response_type": "code",
            "scope": settings.TESLA_SCOPES,
            "state": state,
        }
    )


def _token_request(data):
    resp = requests.post(
        settings.TESLA_TOKEN_URL,
        data={"client_id": settings.TESLA_CLIENT_ID, **data},
        timeout=30,
    )
    if not resp.ok:
        cls = AuthError if resp.status_code in (400, 401, 403) else TeslaError
        raise cls(f"Token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _save_tokens(account, tokens):
    account.access_token = tokens["access_token"]
    account.refresh_token = tokens.get("refresh_token", account.refresh_token)
    # Refresh a minute early so a token never expires mid-request
    account.expires_at = timezone.now() + timedelta(seconds=tokens["expires_in"] - 60)
    account.save()
    return account


def connect(code):
    """Exchange the OAuth code for tokens; replaces any previously connected account."""
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "client_secret": settings.TESLA_CLIENT_SECRET,
            "code": code,
            "audience": settings.TESLA_API_URL,
            "redirect_uri": settings.TESLA_REDIRECT_URI,
        }
    )
    TeslaAccount.objects.all().delete()
    account = _save_tokens(TeslaAccount(), tokens)
    sync_vehicles(account)
    return account


def get_account():
    account = TeslaAccount.objects.first()
    if account is None:
        raise NotConnected("No Tesla account connected")
    return account


def access_token(account=None, force_refresh=False):
    account = account or get_account()
    if force_refresh or account.expires_at <= timezone.now():
        tokens = _token_request({"grant_type": "refresh_token", "refresh_token": account.refresh_token})
        _save_tokens(account, tokens)
    return account.access_token


# --- Plain Fleet API calls ---------------------------------------------------------


def api(method, path, **kwargs):
    account = get_account()
    for attempt in (1, 2):
        token = access_token(account, force_refresh=attempt == 2)
        resp = requests.request(
            method,
            settings.TESLA_API_URL + path,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            **kwargs,
        )
        if resp.status_code == 401 and attempt == 1:
            continue  # token revoked/expired early: refresh once
        break
    if resp.status_code == 408:
        raise VehicleAsleep("The car is asleep or offline")
    if not resp.ok:
        raise TeslaError(f"Tesla API {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("response")


def sync_vehicles(account=None):
    account = account or get_account()
    seen = []
    for v in api("GET", "/api/1/vehicles") or []:
        vehicle, _ = TeslaVehicle.objects.update_or_create(
            vin=v["vin"],
            defaults={"account": account, "vehicle_id": v.get("id"), "name": v.get("display_name") or ""},
        )
        seen.append(vehicle.pk)
    TeslaVehicle.objects.exclude(pk__in=seen).delete()
    return list(TeslaVehicle.objects.all())


def get_vehicle():
    vehicle = TeslaVehicle.objects.first()
    if vehicle is None:
        raise NotConnected("No car found on the connected Tesla account")
    return vehicle


def fleet_status(vin):
    """Whether our virtual key is on the car, plus firmware info."""
    data = api("POST", "/api/1/vehicles/fleet_status", json={"vins": [vin]})
    info = (data.get("vehicle_info") or {}).get(vin, {})
    return {
        "key_paired": vin in (data.get("key_paired_vins") or []),
        "firmware": info.get("firmware_version"),
        "signing_required": info.get("vehicle_command_protocol_required"),
    }


def online_state(vin):
    """'online', 'asleep' or 'offline' - doesn't wake the car."""
    return api("GET", f"/api/1/vehicles/{vin}")["state"]


def _parse_charge(charge):
    ts = charge.get("timestamp")
    return {
        "reported_at": datetime.fromtimestamp(ts / 1000, UTC) if ts else None,
        "battery_level": charge.get("battery_level"),
        "charge_limit": charge.get("charge_limit_soc"),
        "charging_state": charge.get("charging_state") or "",
        "charger_power": charge.get("charger_power"),
        "range_km": round(charge["battery_range"] * 1.609344) if charge.get("battery_range") else None,
    }


def charge_state(vin):
    """Live battery and schedules (car must be awake). Raises VehicleAsleep instead of waking it."""
    data = api("GET", f"/api/1/vehicles/{vin}/vehicle_data", params={"endpoints": "charge_state;charge_schedule_data"})
    schedules = (data.get("charge_schedule_data") or {}).get("charge_schedules") or []
    return {**_parse_charge(data.get("charge_state") or {}), "schedules": schedules}


def cached_charge_state(vin):
    """What the car last reported to Tesla. Works while it sleeps and never wakes it."""
    data = api("GET", f"/api/1/vehicles/{vin}/vehicle_data_cached")
    return _parse_charge((data or {}).get("charge_state") or {})


def observe(vehicle=None):
    """Read the cache and store it as a CarObservation (a new row only if the report changed)."""
    vehicle = vehicle or get_vehicle()
    data = cached_charge_state(vehicle.vin)
    now = timezone.now()
    latest = vehicle.observations.first()
    if latest and latest.same_report(data):
        latest.checked_at = now
        latest.save(update_fields=["checked_at"])
        return latest
    return CarObservation.objects.create(vehicle=vehicle, first_seen_at=now, checked_at=now, **data)


def wake_up(vin, timeout=60):
    """Wake the car and wait until it's online. Costs a 'wake' on the Fleet API."""
    api("POST", f"/api/1/vehicles/{vin}/wake_up")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if online_state(vin) == "online":
            return True
        time.sleep(3)
    raise VehicleAsleep("The car didn't wake up within a minute; try again")


def car_summary(fetch=True):
    """Best-effort info for other pages: None if no car, else name + last reported battery.

    Reads the cache (never wakes the car) and never raises.
    """
    vehicle = TeslaVehicle.objects.select_related("account").first()
    if vehicle is None:
        return None
    summary = {"name": str(vehicle), "battery_level": None, "reported_at": None}
    if fetch:
        try:
            obs = observe(vehicle)
            summary["battery_level"], summary["reported_at"] = obs.battery_level, obs.reported_at
        except TeslaError as e:
            logger.info("Car summary unavailable: %s", e)
    return summary


# --- Signed commands -----------------------------------------------------------------


def _private_key():
    from cryptography.hazmat.primitives import serialization

    if not settings.TESLA_PRIVATE_KEY_B64:
        raise TeslaError("TESLA_PRIVATE_KEY_B64 is not configured")
    pem = base64.b64decode(settings.TESLA_PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(pem, password=None)


async def _run_signed(vin, token, actions):
    import aiohttp
    from tesla_fleet_api import TeslaFleetApi

    async with aiohttp.ClientSession() as session:
        fleet = TeslaFleetApi(session=session, access_token=token, region=settings.TESLA_REGION)
        fleet.private_key = _private_key()
        car = fleet.vehicles.createSigned(vin)
        results = []
        for name, kwargs, optional in actions:
            try:
                results.append((name, await getattr(car, name)(**kwargs)))
            except Exception as e:  # library raises per Tesla error type
                if not optional:
                    raise TeslaError(f"{name} failed: {e}") from e
                logger.info("Optional command %s failed: %s", name, e)
                results.append((name, {"error": str(e)}))
        return results


def send_signed(vin, actions):
    """Run [(command, kwargs, optional)] in one signed session."""
    return asyncio.run(_run_signed(vin, access_token(), actions))


# --- Charge plans --------------------------------------------------------------------


def plan_to_schedules(groups):
    """Turn the charging blocks of a plan into car charge schedules.

    The plan splits blocks at midnight for display; the car can run a schedule
    across midnight (end before start = next day), so those are joined again.
    """
    merged = []
    for g in groups:
        if merged and merged[-1]["end"] == g["start"]:
            merged[-1]["end"] = g["end"]
        else:
            merged.append({"start": g["start"], "end": g["end"]})
    schedules = []
    for m in merged:
        start, end = m["start"], m["end"]
        schedules.append(
            {
                "day": start.strftime("%A"),
                "start_time": start.hour * 60 + start.minute,
                "end_time": end.hour * 60 + end.minute,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
    return schedules


def send_charge_plan(vehicle, groups, charge_limit, deadline=None, trigger="manual", reason=""):
    """Set the charge limit and replace ALL charge schedules on the car with these blocks.

    `groups` are [{"start", "end"}] aware datetimes; an empty list just clears
    the schedules. Removes every schedule on the car (also ones made in the
    Tesla app), plus any schedules from earlier Dyncharge plans that weren't
    confirmed removed.
    """
    limit = max(MIN_CHARGE_LIMIT, min(100, int(charge_limit)))
    tz = timezone.get_current_timezone()
    schedules = plan_to_schedules(
        [{"start": g["start"].astimezone(tz), "end": g["end"].astimezone(tz)} for g in groups]
    )
    # Includes failed attempts: some of their schedules may have reached the car.
    # Materialized now, so the submission saved below isn't counted as "previous".
    previous = list(ChargePlanSubmission.objects.filter(vehicle=vehicle, replaced=False))
    submission = ChargePlanSubmission(
        vehicle=vehicle, charge_limit=limit, deadline=deadline, schedules=schedules, trigger=trigger, reason=reason[:300]
    )

    try:
        if online_state(vehicle.vin) != "online":
            wake_up(vehicle.vin)
        try:
            on_car = [s["id"] for s in charge_state(vehicle.vin)["schedules"] if "id" in s]
        except TeslaError as e:
            # Don't half-replace the planning if we can't see what's on the car
            raise TeslaError(f"Couldn't read the charge schedules on the car, nothing was changed: {e}") from e

        old_ids = sorted(set(on_car) | {s["id"] for sub in previous for s in sub.schedules})
        # Unique new ids; never reuse one, even within the same second
        base_id = max([int(time.time())] + [i + 1 for i in old_ids])
        for i, s in enumerate(schedules):
            s["id"] = base_id + i

        actions = [("set_charge_limit", {"percent": limit}, True)]  # fails harmlessly if already set
        # optional: earlier one-time schedules may already be gone
        actions += [("remove_charge_schedule", {"id": i}, True) for i in old_ids]
        actions += [
            (
                "add_charge_schedule",
                {
                    "days_of_week": s["day"],
                    "enabled": True,
                    "lat": settings.TESLA_HOME_LAT,
                    "lon": settings.TESLA_HOME_LON,
                    "start_time": s["start_time"],
                    "end_time": s["end_time"],
                    "one_time": True,
                    "id": s["id"],
                    "name": "DynCharge",
                },
                False,
            )
            for s in schedules
        ]
        send_signed(vehicle.vin, actions)
    except TeslaError as e:
        submission.error = str(e)
        submission.save()
        raise
    submission.succeeded = True
    submission.save()
    ChargePlanSubmission.objects.filter(pk__in=[p.pk for p in previous]).update(replaced=True)
    return submission
