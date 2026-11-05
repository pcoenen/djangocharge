from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
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


class SyncTests(TestCase):
    """Sync decisions; times are around 2026-11-10 (UTC)."""

    def setUp(self):
        account = TeslaAccount.objects.create(access_token="a", refresh_token="r", expires_at=T0 + timedelta(days=9))
        self.vehicle = TeslaVehicle.objects.create(account=account, vin="VIN1", name="Model 3")
        self.obs = CarObservation(vehicle=self.vehicle, battery_level=50, charging_state="Stopped")
        # Night: expensive until 01:00 UTC, cheap 01:00-03:00, deadline 06:00
        self.night = T0.replace(hour=0)
        prices = [50] * 4 + [1] * 8 + [50] * 12
        self.slots = slots(prices, start=self.night)
        self.need = Need(self.night + timedelta(hours=6), 55, "55% target")

    def plan(self, now, need=None):
        return make_plan(50, self.slots, [need or self.need], now=now, pct_per_quarter=1)

    def sent(self, plan, limit=None, ok=True, trigger="auto", created=None):
        from tesla.models import ChargePlanSubmission

        sub = ChargePlanSubmission.objects.create(
            vehicle=self.vehicle, charge_limit=limit or plan.charge_limit, succeeded=ok, trigger=trigger,
            schedules=[{"start": b.start.isoformat(), "end": b.end.isoformat()} for b in plan.blocks],
        )
        if created:
            ChargePlanSubmission.objects.filter(pk=sub.pk).update(created_at=created)
        return sub

    def test_waits_until_an_hour_before_the_first_quarter(self):
        from . import sync

        early = self.night - timedelta(hours=3)
        d = sync.decide(self.plan(early), self.vehicle, self.obs, early)
        self.assertEqual(d.action, sync.WAIT)
        self.assertEqual(d.send_at, self.night)  # charging starts 01:00 -> send at 00:00
        self.assertTrue(d.must)
        at = self.night + timedelta(seconds=5)  # the 00:00 cron run
        self.assertEqual(sync.decide(self.plan(at), self.vehicle, self.obs, at).action, sync.SEND)
        half_hour_before = self.night - timedelta(minutes=30)
        self.assertEqual(sync.decide(self.plan(half_hour_before), self.vehicle, self.obs, half_hour_before).action,
                         sync.WAIT)

    def test_nothing_planned_and_never_sent_doesnt_wake(self):
        from . import sync

        plan = make_plan(50, self.slots, [], now=self.night, pct_per_quarter=1)
        self.assertEqual(sync.decide(plan, self.vehicle, self.obs, self.night).action, sync.IN_SYNC)

    def test_in_sync_after_sending(self):
        from . import sync

        plan = self.plan(self.night)
        self.sent(plan)
        self.assertEqual(sync.decide(plan, self.vehicle, self.obs, self.night).action, sync.IN_SYNC)
        # later, during the block, the passed quarters don't count as differences
        later = self.night + timedelta(hours=1, minutes=20)  # one quarter charged: 51%
        replanned = make_plan(51, self.slots, [self.need], now=later, pct_per_quarter=1)
        self.assertEqual(sync.decide(replanned, self.vehicle, self.obs, later).action, sync.IN_SYNC)

    def test_extra_quarters_at_the_end_are_ignored(self):
        """Charged a bit faster than estimated: the car stops at its limit anyway."""
        from . import sync

        plan = self.plan(self.night)
        self.sent(plan)
        fuller = make_plan(51, self.slots, [self.need], now=self.night, pct_per_quarter=1)
        self.assertEqual(sync.decide(fuller, self.vehicle, self.obs, self.night).action, sync.IN_SYNC)

    def test_more_needed_after_driving(self):
        from . import sync

        self.sent(self.plan(self.night))
        emptier = make_plan(45, self.slots, [self.need], now=self.night, pct_per_quarter=1)
        d = sync.decide(emptier, self.vehicle, self.obs, self.night)
        self.assertEqual(d.action, sync.SEND)
        self.assertTrue(d.must)

    def test_unplugged_car_isnt_woken_and_is_reported(self):
        from . import sync
        from .models import Notification

        self.obs.charging_state = "Disconnected"
        with mock.patch("tesla.client.send_charge_plan") as send:
            d, sub = sync.sync(self.plan(self.night), self.vehicle, self.obs, self.night)
        send.assert_not_called()
        self.assertEqual(d.action, sync.UNPLUGGED)
        self.assertIn("Plug in", Notification.objects.get().subject)

    def test_daily_budget(self):
        from . import sync

        for _ in range(sync.MAX_SENDS_PER_DAY):
            self.sent(make_plan(50, self.slots, [], now=self.night, pct_per_quarter=1), limit=50,
                      created=self.night - timedelta(hours=2))
        d = sync.decide(self.plan(self.night), self.vehicle, self.obs, self.night)
        self.assertEqual(d.action, sync.BUDGET)

    def test_sync_sends_blocks_and_limit(self):
        from . import sync

        plan = self.plan(self.night)
        with mock.patch("tesla.client.send_charge_plan") as send:
            d, _ = sync.sync(plan, self.vehicle, self.obs, self.night)
        self.assertEqual(d.action, sync.SEND)
        vehicle, groups, limit = send.call_args.args
        self.assertEqual(limit, 55)
        self.assertEqual(groups, [{"start": self.night + timedelta(hours=1), "end": self.night + timedelta(hours=2, minutes=15)}])
        self.assertEqual(send.call_args.kwargs["trigger"], "auto")

    def test_limit_below_tesla_minimum_is_raised(self):
        from . import sync

        need = Need(self.night + timedelta(hours=6), 40, "calendar 40%")
        plan = make_plan(30, self.slots, [need], now=self.night, pct_per_quarter=1)
        self.assertEqual(sync.desired_limit(plan), 50)

    def test_repeated_failures_are_emailed_once(self):
        from tesla import client

        from . import sync
        from .models import Notification

        def fail(vehicle, *args, **kwargs):
            self.sent(self.plan(self.night), ok=False)
            raise client.TeslaError("denied")

        with mock.patch("tesla.client.send_charge_plan", side_effect=fail), self.settings(NOTIFY_EMAIL="me@example.com"):
            for _ in range(3):
                with self.assertRaises(client.TeslaError):
                    sync.sync(self.plan(self.night), self.vehicle, self.obs, self.night)
        from django.core import mail

        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("keeps failing", mail.outbox[0].subject)


class TickSyncTests(EnergyTestCase):
    def test_tick_reads_plans_and_sends(self):
        self.observe(50)
        ChargeTarget.objects.create(battery_pct=60, deadline=self.now + timedelta(minutes=90))
        out = StringIO()
        with mock.patch("tesla.client.cached_charge_state", side_effect=lambda vin: {
            "reported_at": self.now, "battery_level": 50, "charging_state": "Stopped", "charge_limit": 80,
            "charger_power": 0, "range_km": 250}), \
                mock.patch("tesla.client.send_charge_plan") as send:
            call_command("energy_tick", stdout=out)
        self.assertIn("sent to the car", out.getvalue())
        send.assert_called_once()

    def test_expired_login_is_reported(self):
        from tesla import client

        from .models import Notification

        self.observe(50)
        with mock.patch("tesla.client.cached_charge_state", side_effect=client.AuthError("Token request failed (401)")):
            with self.assertRaises(CommandError):
                call_command("energy_tick", stdout=StringIO(), stderr=StringIO())
        self.assertIn("Reconnect Tesla", Notification.objects.get().subject)


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:shift-1
DTSTAMP:20261101T000000Z
DTSTART:20261110T070000Z
DTEND:20261110T143000Z
SUMMARY:V
END:VEVENT
BEGIN:VEVENT
UID:shift-2
DTSTAMP:20261101T000000Z
DTSTART:20261111T213000Z
DTEND:20261112T070000Z
SUMMARY:N
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTAMP:20261101T000000Z
DTSTART:20261110T120000Z
SUMMARY:Gone
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTAMP:20261101T000000Z
DTSTART:20261103T180000Z
RRULE:FREQ=WEEKLY
SUMMARY:Training
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTAMP:20261101T000000Z
DTSTART;VALUE=DATE:20261112
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
UID:far
DTSTAMP:20261101T000000Z
DTSTART:20261130T070000Z
SUMMARY:Later
END:VEVENT
END:VCALENDAR
"""

NOW = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)


@mock.patch("energy.calendar_sync.LOOKAHEAD", timedelta(days=3))  # these tests use a wider window
@mock.patch("energy.calendar_sync.fetch")
class CalendarTests(EnergyTestCase):
    def sync(self, fetch, ics=ICS):
        from .calendar_sync import sync_calendar

        fetch.return_value = ics.encode()
        return sync_calendar(NOW, url="https://example.com/cal.ics")

    def targets(self):
        return {t.label: t for t in ChargeTarget.objects.filter(source=ChargeTarget.CALENDAR)}

    def test_events_become_40pct_targets_half_an_hour_before(self, fetch):
        self.assertEqual(self.sync(fetch), (4, 0, 0))
        t = self.targets()
        # V, N, the weekly Training on Tue 10th, the all-day Holiday; not the cancelled or far one
        self.assertEqual(set(t), {"V", "N", "Training", "Holiday"})
        self.assertEqual(t["V"].deadline, datetime(2026, 11, 10, 6, 30, tzinfo=UTC))
        self.assertEqual(t["V"].battery_pct, 40)
        # all-day: local midnight (CET) minus 30 minutes
        self.assertEqual(t["Holiday"].deadline, datetime(2026, 11, 11, 22, 30, tzinfo=UTC))
        self.assertTrue(t["Training"].external_id.startswith("weekly@2026-11-10T18:00"))

    def test_only_next_24_hours_by_default(self, fetch):
        from . import calendar_sync

        with mock.patch.object(calendar_sync, "LOOKAHEAD", timedelta(hours=24)):
            self.sync(fetch)
        # V (07:00) and Training (18:00) are within 24h; N and Holiday (the 11th/12th) not yet
        self.assertEqual(set(self.targets()), {"V", "Training"})

    def test_sync_is_idempotent(self, fetch):
        self.sync(fetch)
        self.assertEqual(self.sync(fetch), (0, 0, 0))

    def test_moved_and_deleted_events(self, fetch):
        self.sync(fetch)
        moved = ICS.replace("DTSTART:20261110T070000Z", "DTSTART:20261110T080000Z")
        moved = moved.replace("UID:shift-2", "UID:shift-2-deleted").replace("SUMMARY:N", "SUMMARY:N2")
        self.assertEqual(self.sync(fetch, moved), (1, 1, 1))
        t = self.targets()
        self.assertEqual(t["V"].deadline, datetime(2026, 11, 10, 7, 30, tzinfo=UTC))
        self.assertNotIn("N", t)

    def test_ignored_stays_ignored_when_moved(self, fetch):
        self.sync(fetch)
        v = self.targets()["V"]
        v.ignored = True
        v.save()
        self.sync(fetch, ICS.replace("DTSTART:20261110T070000Z", "DTSTART:20261110T090000Z"))
        self.assertTrue(self.targets()["V"].ignored)

    def test_ignored_targets_are_not_planned(self, fetch):
        self.sync(fetch)
        ChargeTarget.objects.exclude(label="V").delete()
        self.assertEqual(len(manager.needs(NOW)), 1)
        ChargeTarget.objects.update(ignored=True)
        self.assertEqual(manager.needs(NOW), [])

    def test_unreadable_calendar_keeps_targets(self, fetch):
        from .calendar_sync import CalendarError, sync_calendar

        self.sync(fetch)
        fetch.side_effect = CalendarError("Reading the calendar failed: ConnectTimeout")
        with self.assertRaises(CalendarError):
            sync_calendar(NOW, url="https://example.com/cal.ics")
        self.assertEqual(len(self.targets()), 4)

    @mock.patch("energy.views._ensure_upcoming_prices")
    def test_ignore_button(self, _, fetch):
        self.sync(fetch)
        v = self.targets()["V"]
        ChargeTarget.objects.filter(pk=v.pk).update(deadline=self.now + timedelta(hours=5))
        r = self.client.get("/energy/")
        self.assertContains(r, "Ignore")
        self.client.post(f"/energy/targets/{v.pk}/ignore/")
        v.refresh_from_db()
        self.assertTrue(v.ignored)
        self.assertContains(self.client.get("/energy/"), "Use again")
        self.client.post(f"/energy/targets/{v.pk}/ignore/")
        v.refresh_from_db()
        self.assertFalse(v.ignored)

    def test_manual_targets_cant_be_ignored(self, fetch):
        t = ChargeTarget.objects.create(battery_pct=60, deadline=self.now + timedelta(hours=3))
        self.assertEqual(self.client.post(f"/energy/targets/{t.pk}/ignore/").status_code, 404)
