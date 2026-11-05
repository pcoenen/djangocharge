from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from . import client
from .models import ChargePlanSubmission, TeslaAccount, TeslaVehicle

BRUSSELS = ZoneInfo("Europe/Brussels")
VIN = "5YJ3E7EB0KF000001"


def response(status=200, data=None):
    return SimpleNamespace(
        status_code=status, ok=200 <= status < 300, json=lambda: data or {}, text=str(data)
    )


def make_account(expired=False):
    account = TeslaAccount.objects.create(
        access_token="old-token",
        refresh_token="refresh",
        expires_at=timezone.now() + timedelta(hours=-1 if expired else 1),
    )
    vehicle = TeslaVehicle.objects.create(account=account, vin=VIN, vehicle_id=123, name="Model 3")
    return account, vehicle


@override_settings(
    TESLA_CLIENT_ID="cid",
    TESLA_CLIENT_SECRET="secret",
    TESLA_PRIVATE_KEY_B64="key",
    TESLA_REDIRECT_URI="https://dyncharge.oraston.be/tesla-api/redirect.php",
    TESLA_HOME_LAT=50.8,
    TESLA_HOME_LON=5.2,
)
class TeslaTestCase(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))


class OAuthTests(TeslaTestCase):
    def test_connect_redirects_to_tesla_with_state(self):
        r = self.client.get("/tesla/connect/")
        url = urlparse(r["Location"])
        q = parse_qs(url.query)
        self.assertEqual(url.netloc, "auth.tesla.com")
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["redirect_uri"], ["https://dyncharge.oraston.be/tesla-api/redirect.php"])
        self.assertIn("vehicle_charging_cmds", q["scope"][0])
        self.assertEqual(q["state"], [self.client.session["tesla_oauth_state"]])

    def test_callback_rejects_wrong_state(self):
        self.client.get("/tesla/connect/")
        r = self.client.get("/tesla-api/redirect.php", {"code": "abc", "state": "forged"}, follow=False)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(TeslaAccount.objects.exists())

    @mock.patch("tesla.client.requests.request")
    @mock.patch("tesla.client.requests.post")
    def test_callback_stores_tokens_and_cars(self, post, request):
        post.return_value = response(data={"access_token": "at", "refresh_token": "rt", "expires_in": 28800})
        request.return_value = response(data={"response": [{"vin": VIN, "id": 42, "display_name": "Model 3"}]})
        self.client.get("/tesla/connect/")
        state = self.client.session["tesla_oauth_state"]
        # the redirect URI registered at Tesla (PHP-era path)
        self.client.get("/tesla-api/redirect.php", {"code": "abc", "state": state})
        account = TeslaAccount.objects.get()
        self.assertEqual((account.access_token, account.refresh_token), ("at", "rt"))
        self.assertEqual(TeslaVehicle.objects.get().vin, VIN)
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(post.call_args.kwargs["data"]["code"], "abc")


class ApiTests(TeslaTestCase):
    @mock.patch("tesla.client.requests.request")
    @mock.patch("tesla.client.requests.post")
    def test_expired_token_is_refreshed(self, post, request):
        make_account(expired=True)
        post.return_value = response(data={"access_token": "new", "refresh_token": "rt2", "expires_in": 28800})
        request.return_value = response(data={"response": {"state": "online"}})
        self.assertEqual(client.online_state(VIN), "online")
        self.assertEqual(post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer new")
        self.assertEqual(TeslaAccount.objects.get().refresh_token, "rt2")

    @mock.patch("tesla.client.requests.request", return_value=response(408))
    def test_asleep_car(self, _):
        make_account()
        with self.assertRaises(client.VehicleAsleep):
            client.charge_state(VIN)

    @mock.patch("tesla.client.requests.request")
    def test_fleet_status(self, request):
        make_account()
        request.return_value = response(
            data={
                "response": {
                    "key_paired_vins": [VIN],
                    "unpaired_vins": [],
                    "vehicle_info": {VIN: {"firmware_version": "2026.38.4", "vehicle_command_protocol_required": True}},
                }
            }
        )
        self.assertEqual(
            client.fleet_status(VIN), {"key_paired": True, "firmware": "2026.38.4", "signing_required": True}
        )

    def test_not_connected(self):
        with self.assertRaises(client.NotConnected):
            client.online_state(VIN)


def group(start, end):
    return {"start": start, "end": end}


class PlanToScheduleTests(TeslaTestCase):
    def test_blocks_split_at_midnight_are_joined(self):
        wed = datetime(2026, 11, 4, 21, 0, tzinfo=BRUSSELS)
        thu = datetime(2026, 11, 5, 0, 0, tzinfo=BRUSSELS)
        groups = [
            group(wed, thu),
            group(thu, thu.replace(hour=4)),
            group(thu.replace(hour=5, minute=15), thu.replace(hour=6)),
        ]
        schedules = client.plan_to_schedules(groups)
        self.assertEqual(
            [(s["day"], s["start_time"], s["end_time"]) for s in schedules],
            [("Wednesday", 21 * 60, 4 * 60), ("Thursday", 5 * 60 + 15, 6 * 60)],
        )


def car_schedules(*ids):
    return {"battery_level": 50, "schedules": [{"id": i, "name": "x"} for i in ids]}


class SendPlanTests(TeslaTestCase):
    def setUp(self):
        super().setUp()
        self.account, self.vehicle = make_account()
        start = datetime(2026, 11, 5, 1, 0, tzinfo=BRUSSELS)
        self.plan = SimpleNamespace(groups=[group(start, start + timedelta(hours=2))])
        self.deadline = datetime(2026, 11, 5, 7, 0, tzinfo=BRUSSELS)
        patcher = mock.patch("tesla.client.charge_state", return_value=car_schedules())
        self.charge_state = patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch("tesla.client.online_state", return_value="online")
    @mock.patch("tesla.client.send_signed")
    def test_sends_limit_and_one_time_schedules(self, send, _):
        sub = client.send_charge_plan(self.vehicle, self.plan, 80, self.deadline)
        vin, actions = send.call_args.args
        self.assertEqual(vin, VIN)
        self.assertEqual(actions[0], ("set_charge_limit", {"percent": 80}, True))
        name, kwargs, optional = actions[1]
        self.assertEqual(name, "add_charge_schedule")
        self.assertFalse(optional)
        self.assertEqual(
            {k: kwargs[k] for k in ("days_of_week", "start_time", "end_time", "one_time", "lat", "lon")},
            {"days_of_week": "Thursday", "start_time": 60, "end_time": 180, "one_time": True, "lat": 50.8, "lon": 5.2},
        )
        self.assertTrue(sub.succeeded)
        self.assertEqual(sub.schedules[0]["id"], kwargs["id"])

    @mock.patch("tesla.client.online_state", return_value="online")
    @mock.patch("tesla.client.send_signed")
    def test_removes_all_schedules_on_the_car_first(self, send, _):
        # 7 and 9 were made in the Tesla app, not by Dyncharge
        self.charge_state.return_value = car_schedules(7, 9)
        client.send_charge_plan(self.vehicle, self.plan, 80, self.deadline)
        names = [a[0] for a in send.call_args.args[1]]
        removed = [a[1]["id"] for a in send.call_args.args[1] if a[0] == "remove_charge_schedule"]
        self.assertEqual(removed, [7, 9])
        # removals happen before the new schedules are added
        self.assertLess(names.index("remove_charge_schedule"), names.index("add_charge_schedule"))

    @mock.patch("tesla.client.online_state", return_value="online")
    @mock.patch("tesla.client.send_signed")
    def test_replaces_previous_schedules_and_clamps_limit(self, send, _):
        first = client.send_charge_plan(self.vehicle, self.plan, 90, self.deadline)
        # the car no longer reports it (e.g. read before it synced), we still remove it
        client.send_charge_plan(self.vehicle, self.plan, 30, self.deadline)
        actions = send.call_args.args[1]
        self.assertEqual(actions[0][1], {"percent": 50})  # Tesla minimum
        self.assertIn(("remove_charge_schedule", {"id": first.schedules[0]["id"]}, True), actions)
        first.refresh_from_db()
        self.assertTrue(first.replaced)

    @mock.patch("tesla.client.online_state", return_value="online")
    @mock.patch("tesla.client.send_signed")
    def test_new_ids_never_clash_with_existing_ones(self, send, _):
        far_future_id = int(datetime(2030, 1, 1).timestamp())
        self.charge_state.return_value = car_schedules(far_future_id)
        sub = client.send_charge_plan(self.vehicle, self.plan, 80, self.deadline)
        self.assertGreater(sub.schedules[0]["id"], far_future_id)

    @mock.patch("tesla.client.online_state", return_value="online")
    @mock.patch("tesla.client.send_signed")
    def test_nothing_is_changed_if_schedules_cant_be_read(self, send, _):
        self.charge_state.side_effect = client.VehicleAsleep("asleep")
        with self.assertRaises(client.TeslaError):
            client.send_charge_plan(self.vehicle, self.plan, 80, self.deadline)
        send.assert_not_called()
        sub = ChargePlanSubmission.objects.get()
        self.assertFalse(sub.succeeded)
        self.assertIn("nothing was changed", sub.error)

    @mock.patch("tesla.client.wake_up")
    @mock.patch("tesla.client.online_state", return_value="asleep")
    @mock.patch("tesla.client.send_signed", side_effect=client.TeslaError("add_charge_schedule failed: denied"))
    def test_failure_is_recorded_and_car_is_woken(self, send, _, wake):
        with self.assertRaises(client.TeslaError):
            client.send_charge_plan(self.vehicle, self.plan, 80, self.deadline)
        wake.assert_called_once_with(VIN)
        sub = ChargePlanSubmission.objects.get()
        self.assertFalse(sub.succeeded)
        self.assertIn("denied", sub.error)


class SignedCommandTests(TeslaTestCase):
    def test_private_key_is_loaded_from_base64_pem(self):
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
        with self.settings(TESLA_PRIVATE_KEY_B64=base64.b64encode(pem).decode()):
            key = client._private_key()
        self.assertIsInstance(key, ec.EllipticCurvePrivateKey)

    def test_signed_vehicle_runs_commands(self):
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        make_account()
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )

        async def fake_limit(self, percent):
            return {"response": {"result": True, "percent": percent}}

        from tesla_fleet_api.tesla.vehicle.signed import VehicleSigned

        with self.settings(TESLA_PRIVATE_KEY_B64=base64.b64encode(pem).decode()), mock.patch.object(
            VehicleSigned, "set_charge_limit", fake_limit
        ):
            results = client.send_signed(VIN, [("set_charge_limit", {"percent": 80}, False)])
        self.assertEqual(results, [("set_charge_limit", {"response": {"result": True, "percent": 80}})])


class DashboardTests(TeslaTestCase):
    def test_not_connected(self):
        r = self.client.get("/tesla/")
        self.assertContains(r, "Connect Tesla")

    @mock.patch("tesla.client.charge_state")
    @mock.patch("tesla.client.online_state", return_value="online")
    @mock.patch("tesla.client.fleet_status", return_value={"key_paired": False, "firmware": None, "signing_required": True})
    def test_connected_unpaired(self, *_):
        make_account()
        client.charge_state.return_value = {
            "battery_level": 64,
            "charge_limit": 80,
            "charging_state": "Stopped",
            "range_km": 300,
            "schedules": [{"name": "DynCharge", "start_time": 60, "end_time": 180, "start_enabled": True, "end_enabled": True, "one_time": True, "enabled": True}],
        }
        r = self.client.get("/tesla/")
        self.assertContains(r, "Add virtual key")
        self.assertContains(r, "https://tesla.com/_ak/dyncharge.oraston.be")
        self.assertContains(r, "64%")
        self.assertContains(r, "01:00 &ndash; 03:00")

    @mock.patch("tesla.client.online_state", return_value="asleep")
    @mock.patch("tesla.client.fleet_status", return_value={"key_paired": True, "firmware": "2026.38", "signing_required": True})
    def test_asleep_car_is_not_woken(self, *_):
        make_account()
        with mock.patch("tesla.client.wake_up") as wake, mock.patch("tesla.client.charge_state") as charge:
            r = self.client.get("/tesla/")
        wake.assert_not_called()
        charge.assert_not_called()
        self.assertContains(r, "Wake up")


class CarChargingIntegrationTests(TeslaTestCase):
    @mock.patch("tesla.client.charge_state", return_value={"battery_level": 37})
    @mock.patch("tesla.client.online_state", return_value="online")
    def test_battery_level_is_prefilled(self, *_):
        make_account()
        r = self.client.get("/car-charging/")
        self.assertContains(r, 'value="37"')

    @mock.patch("prices.views._ensure_upcoming_prices")
    def test_result_offers_send_to_car(self, _):
        make_account()
        from prices.models import PriceInterval

        now = timezone.now()
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        PriceInterval.objects.bulk_create(
            [PriceInterval(start=start + timedelta(minutes=15 * i), price=50.0) for i in range(40)]
        )
        r = self.client.post(
            "/car-charging/", {"current_percentage": 60, "desired_percentage": 70, "day": "tomorrow", "hour": "23:00"}
        )
        self.assertContains(r, "Send to Model 3")
