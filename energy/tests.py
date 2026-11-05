from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from prices.models import EnergyContract, PriceInterval
from tesla.models import CarObservation, TeslaAccount, TeslaVehicle

from . import manager
from .models import ChargeTarget, EnergySettings, PlanSnapshot
from .planner import MET, SHORT, WAITING, Need, Slot, make_plan

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
Q = timedelta(minutes=15)


def slots(prices, cheap_below=None, start=T0):
    """One slot per price, starting at `start`."""
    return [
        Slot(start + i * Q, start + (i + 1) * Q, p, "cheap" if cheap_below is not None and p < cheap_below else "")
        for i, p in enumerate(prices)
    ]


def starts(plan):
    return [(b.start, b.quarters) for b in plan.blocks]


class PlannerTests(TestCase):
    def test_nothing_to_do(self):
        plan = make_plan(50, slots([10, 20]), [], now=T0, pct_per_quarter=1)
        self.assertEqual(plan.blocks, [])
        self.assertIsNone(plan.charge_limit)

    def test_target_uses_cheapest_quarters_before_deadline(self):
        prices = [30, 10, 20, 5, 1]  # the 1 is after the deadline
        need = Need(T0 + 4 * Q, 52, "52% target")
        plan = make_plan(50, slots(prices), [need], now=T0, pct_per_quarter=1)
        self.assertEqual(starts(plan), [(T0 + Q, 1), (T0 + 3 * Q, 1)])
        self.assertEqual(plan.needs[0].status, MET)
        self.assertEqual(plan.charge_limit, 52)
        self.assertIn("needed for 52% target", plan.blocks[0].reasons)

    def test_cheap_charging_counts_toward_targets(self):
        """The price rule already charges enough: nothing extra for the target."""
        prices = [30, 1, 1, 30]
        need = Need(T0 + 4 * Q, 52, "event")
        plan = make_plan(50, slots(prices, cheap_below=5), [need], now=T0, pct_per_quarter=1)
        self.assertEqual(starts(plan), [(T0 + Q, 2)])
        self.assertEqual(plan.blocks[0].reasons, ["cheap"])
        self.assertEqual(plan.needs[0].status, MET)
        self.assertEqual(plan.charge_limit, 100)  # no cap on price-rule charging

    def test_cheap_charging_partially_covers_target(self):
        prices = [30, 20, 1, 40]
        need = Need(T0 + 4 * Q, 52, "event")
        plan = make_plan(50, slots(prices, cheap_below=5), [need], now=T0, pct_per_quarter=1)
        # one cheap quarter + the cheapest other one (20)
        self.assertEqual(starts(plan), [(T0 + Q, 2)])
        self.assertEqual(plan.blocks[0].reasons, ["needed for event", "cheap"])

    def test_earlier_target_charging_counts_for_later_target(self):
        prices = [5, 50, 50, 50, 1, 50]
        first = Need(T0 + 2 * Q, 51, "first")
        second = Need(T0 + 6 * Q, 52, "second")
        plan = make_plan(50, slots(prices), [second, first], now=T0, pct_per_quarter=1)
        self.assertEqual(starts(plan), [(T0, 1), (T0 + 4 * Q, 1)])
        self.assertEqual([r.status for r in plan.needs], [MET, MET])

    def test_not_enough_time(self):
        need = Need(T0 + 2 * Q, 60, "too soon")
        plan = make_plan(50, slots([10, 10, 10]), [need], now=T0, pct_per_quarter=1)
        result = plan.needs[0]
        self.assertEqual(result.status, SHORT)
        self.assertAlmostEqual(result.shortfall_pct, 8)
        self.assertEqual(plan.problems, [result])

    def test_deadline_beyond_known_prices_waits_when_there_is_time(self):
        need = Need(T0 + 20 * Q, 52, "far")  # prices known for 4 quarters only
        plan = make_plan(50, slots([1, 1, 1, 1]), [need], now=T0, pct_per_quarter=1)
        self.assertEqual(plan.blocks, [])
        self.assertEqual(plan.needs[0].status, WAITING)
        self.assertEqual(plan.needs[0].quarters_later, 2)

    def test_deadline_beyond_known_prices_charges_now_when_needed(self):
        need = Need(T0 + 6 * Q, 55, "far")  # 5 quarters needed, only 2 after the known prices
        plan = make_plan(50, slots([1, 9, 2, 3]), [need], now=T0, pct_per_quarter=1)
        self.assertEqual(plan.quarters, 3)
        self.assertEqual(plan.needs[0].status, WAITING)
        self.assertEqual(plan.charge_limit, 55)

    def test_stops_at_full(self):
        plan = make_plan(98, slots([1] * 10, cheap_below=5), [], now=T0, pct_per_quarter=1)
        self.assertEqual(plan.quarters, 2)

    def test_past_quarters_and_targets_are_ignored(self):
        need = Need(T0 - Q, 90, "past")
        plan = make_plan(50, slots([1, 1, 1], cheap_below=5), [need], now=T0 + Q, pct_per_quarter=1)
        self.assertEqual(starts(plan), [(T0 + Q, 2)])
        self.assertEqual(plan.needs, [])


class EnergyTestCase(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))
        EnergyContract.objects.exclude(name="Original dyncharge formula").delete()
        self.now = timezone.now().replace(second=0, microsecond=0)
        start = self.now.replace(minute=0) - timedelta(days=31)
        # 31 days of history at 100 €/MWh, cheap night hours at 0, then upcoming prices
        rows = []
        t = start
        while t < self.now + timedelta(hours=24):
            rows.append(PriceInterval(start=t, price=0.0 if t.hour < 8 else 100.0))
            t += Q
        PriceInterval.objects.bulk_create(rows)

    def observe(self, pct=50):
        account = TeslaAccount.objects.create(access_token="a", refresh_token="r", expires_at=self.now + timedelta(hours=1))
        vehicle = TeslaVehicle.objects.create(account=account, vin="VIN1", name="Model 3")
        return CarObservation.objects.create(vehicle=vehicle, battery_level=pct, charging_state="Stopped",
                                             reported_at=self.now)


class PriceRuleTests(EnergyTestCase):
    def test_marks_use_actual_price(self):
        marks = manager.price_marks(timezone.localdate(self.now))
        contract = EnergyContract.objects.get()
        self.assertAlmostEqual(marks["p25"], contract.consumption_price(0.0))
        self.assertAlmostEqual(marks["median"], contract.consumption_price(100.0))

    def test_rules(self):
        s = EnergySettings(price_rule=EnergySettings.RULE_BELOW_PRICE, price_threshold=10)
        marks = {"p25": 5, "median": 8}
        self.assertTrue(manager.cheap_reason(s, 9.99, marks))
        self.assertFalse(manager.cheap_reason(s, 10, marks))
        s.price_rule = EnergySettings.RULE_BELOW_P25
        self.assertTrue(manager.cheap_reason(s, 4, marks))
        self.assertFalse(manager.cheap_reason(s, 6, marks))
        s.price_rule = EnergySettings.RULE_BELOW_MEDIAN
        self.assertTrue(manager.cheap_reason(s, 6, marks))
        s.price_rule = EnergySettings.RULE_OFF
        self.assertFalse(manager.cheap_reason(s, -100, marks))


class BuildPlanTests(EnergyTestCase):
    def test_no_battery_level_no_plan(self):
        self.assertIsNone(manager.build_plan(self.now))

    def test_price_rule_plans_cheap_night(self):
        self.observe(50)
        settings = EnergySettings.load()
        settings.price_rule = EnergySettings.RULE_BELOW_MEDIAN
        settings.save()
        plan = manager.build_plan(self.now)
        self.assertTrue(plan.blocks)
        self.assertTrue(all(b.start.astimezone(UTC).hour < 8 for b in plan.blocks))
        self.assertEqual(plan.charge_limit, 100)

    def test_target_is_planned(self):
        self.observe(50)
        ChargeTarget.objects.create(battery_pct=60, deadline=self.now + timedelta(hours=20))
        plan = manager.build_plan(self.now)
        self.assertEqual(plan.needs[0].status, MET)
        self.assertGreaterEqual(plan.expected_final_pct, 60)
        self.assertEqual(plan.charge_limit, 61)

    def test_snapshot_only_when_changed(self):
        self.observe(50)
        ChargeTarget.objects.create(battery_pct=60, deadline=self.now + timedelta(hours=20))
        _, created = manager.save_snapshot(manager.build_plan(self.now))
        _, again = manager.save_snapshot(manager.build_plan(self.now))
        self.assertEqual((created, again), (True, False))
        ChargeTarget.objects.create(battery_pct=80, deadline=self.now + timedelta(hours=20))
        _, changed = manager.save_snapshot(manager.build_plan(self.now))
        self.assertTrue(changed)
        self.assertEqual(PlanSnapshot.objects.count(), 2)


@mock.patch("energy.views._ensure_upcoming_prices")
class ViewTests(EnergyTestCase):
    def test_plan_page(self, _):
        self.observe(40)
        ChargeTarget.objects.create(battery_pct=60, deadline=self.now + timedelta(hours=20), label="trip")
        r = self.client.get("/energy/")
        self.assertContains(r, "40%")
        self.assertContains(r, "Charging plan")
        self.assertContains(r, "needed for 60%")
        self.assertContains(r, "trip")

    def test_plan_page_without_car(self, _):
        r = self.client.get("/energy/")
        self.assertContains(r, "No battery level from the car yet")

    def test_add_and_remove_target(self, _):
        deadline = timezone.localtime(self.now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.client.post("/energy/targets/add/", {"battery_pct": 70, "deadline": deadline, "label": ""})
        target = ChargeTarget.objects.get()
        self.assertEqual(target.battery_pct, 70)
        self.client.post(f"/energy/targets/{target.pk}/delete/")
        self.assertFalse(ChargeTarget.objects.exists())

    def test_target_in_the_past_is_refused(self, _):
        deadline = timezone.localtime(self.now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        self.client.post("/energy/targets/add/", {"battery_pct": 70, "deadline": deadline})
        self.assertFalse(ChargeTarget.objects.exists())

    def test_calendar_targets_cant_be_removed_by_hand(self, _):
        t = ChargeTarget.objects.create(battery_pct=40, deadline=self.now + timedelta(hours=3),
                                        source=ChargeTarget.CALENDAR)
        self.assertEqual(self.client.post(f"/energy/targets/{t.pk}/delete/").status_code, 404)

    def test_settings(self, _):
        r = self.client.get("/energy/settings/")
        self.assertContains(r, "median")
        r = self.client.post("/energy/settings/", {"price_rule": "below_price"})
        self.assertContains(r, "Enter the price")
        self.client.post("/energy/settings/", {"price_rule": "below_price", "price_threshold": "12.5"})
        s = EnergySettings.load()
        self.assertEqual((s.price_rule, s.price_threshold), ("below_price", 12.5))


class TickTests(EnergyTestCase):
    def test_tick_plans_without_car(self):
        out = StringIO()
        call_command("energy_tick", stdout=out)
        self.assertIn("Car not read", out.getvalue())
        self.assertIn("nothing to plan", out.getvalue())
