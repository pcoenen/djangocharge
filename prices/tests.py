from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .entsoe import EntsoeError, ensure_prices_for, parse_prices
from .models import EnergyContract, PriceInterval
from .converters import Converter, PriceSeries, converter_choices, get_converter, register
from .pricing import (
    day_bounds,
    dishwasher_options,
    plan_charging,
    quartiles_for_date,
)

class TestCase(TestCase):
    """Start every test with only the original (quarter) contract from migration 0003,
    so tests don't depend on whichever real contracts later migrations add."""

    def setUp(self):
        super().setUp()
        EnergyContract.objects.exclude(name="Original dyncharge formula").delete()


NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


def doc(periods):
    body = ""
    for start, end, res, points in periods:
        pts = "".join(
            f"<Point><position>{p}</position><price.amount>{v}</price.amount></Point>" for p, v in points
        )
        body += (
            f"<TimeSeries><Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{res}</resolution>{pts}</Period></TimeSeries>"
        )
    return f'<Publication_MarketDocument xmlns="{NS}">{body}</Publication_MarketDocument>'


def fill_day(day, price_fn=lambda i: 50.0):
    start, end = day_bounds(day)
    t, i = start, 0
    objs = []
    while t < end:
        objs.append(PriceInterval(start=t, price=price_fn(i)))
        t += timedelta(minutes=15)
        i += 1
    PriceInterval.objects.bulk_create(objs)


class ParseTests(TestCase):
    def test_quarter_resolution_with_omitted_points(self):
        xml = doc([("2026-11-03T23:00Z", "2026-11-04T00:00Z", "PT15M", [(1, 10), (3, 30)])])
        prices = parse_prices(xml)
        start = datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
        self.assertEqual(
            [prices[start + timedelta(minutes=15 * i)] for i in range(4)], [10, 10, 30, 30]
        )

    def test_hourly_resolution_is_spread_over_quarters(self):
        xml = doc([("2026-11-03T23:00Z", "2026-11-04T01:00Z", "PT60M", [(1, 10), (2, 20)])])
        prices = parse_prices(xml)
        self.assertEqual(len(prices), 8)
        self.assertEqual(prices[datetime(2026, 11, 4, 0, 45, tzinfo=UTC)], 20)

    def test_quarter_data_wins_over_hourly(self):
        xml = doc(
            [
                ("2026-11-03T23:00Z", "2026-11-04T00:00Z", "PT60M", [(1, 99)]),
                ("2026-11-03T23:00Z", "2026-11-04T00:00Z", "PT15M", [(1, 1), (2, 2), (3, 3), (4, 4)]),
            ]
        )
        self.assertEqual(sorted(parse_prices(xml).values()), [1, 2, 3, 4])

    def test_acknowledgement_raises(self):
        xml = (
            '<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">'
            "<Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>"
        )
        with self.assertRaisesMessage(EntsoeError, "No matching data found"):
            parse_prices(xml)


class DayBoundsTests(TestCase):
    def test_dst_days(self):
        for day, quarters in [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100), (date(2026, 11, 4), 96)]:
            start, end = day_bounds(day)
            self.assertEqual((end - start) / timedelta(minutes=15), quarters, day)

    def test_local_midnight(self):
        start, _ = day_bounds(date(2026, 11, 4))
        self.assertEqual(start, datetime(2026, 11, 3, 23, 0, tzinfo=UTC))


def consumption(market):
    return (1.02 * market + 4) / 10 * 1.06


class FormulaTests(TestCase):
    def test_seeded_contract_matches_original_formula(self):
        contract = EnergyContract.objects.get()
        self.assertAlmostEqual(contract.consumption_price(100), (102 + 4) / 10 * 1.06)
        self.assertAlmostEqual(contract.injection_price(100), (98 - 15) / 10)

    def test_quartiles_default_without_data(self):
        self.assertEqual(quartiles_for_date(date(2026, 11, 4)), [0, 100, 100])

    def test_quartiles(self):
        fill_day(date(2026, 11, 3), lambda i: float(i))  # 0..95
        self.assertEqual(quartiles_for_date(date(2026, 11, 4)), [24.0, 48.0, 72.0])


class EnsurePricesTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    @override_settings(ENTSOE_TOKEN="t")
    def test_skips_api_when_day_complete(self):
        fill_day(date(2026, 11, 4))
        with mock.patch("prices.entsoe.fetch_from_api") as fetch:
            self.assertTrue(ensure_prices_for(date(2026, 11, 4)))
        fetch.assert_not_called()

    @override_settings(ENTSOE_TOKEN="t")
    def test_backs_off_after_miss(self):
        with mock.patch("prices.entsoe.fetch_from_api", side_effect=EntsoeError("no data")) as fetch:
            self.assertFalse(ensure_prices_for(date(2026, 11, 5)))
            self.assertFalse(ensure_prices_for(date(2026, 11, 5)))
        self.assertEqual(fetch.call_count, 1)

    @override_settings(ENTSOE_TOKEN="t")
    def test_stores_only_requested_day(self):
        start, end = day_bounds(date(2026, 11, 4))
        prices = {start + timedelta(minutes=15 * i): 1.0 for i in range(-4, 100)}
        with mock.patch("prices.entsoe.fetch_from_api", return_value=prices):
            self.assertTrue(ensure_prices_for(date(2026, 11, 4)))
        self.assertEqual(PriceInterval.objects.count(), 96)


class DishwasherTests(TestCase):
    def test_cheapest_option_is_100_percent(self):
        now = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
        # 50 EUR/MWh until 15:00 UTC, then 10
        fill_day(date(2026, 11, 4), lambda i: 50.0 if i < 64 else 10.0)
        options = dishwasher_options(now)
        self.assertEqual([o["percentage"] for o in options], [500, 500, 100, 100])
        self.assertEqual(options[2]["color"], "rgba(0, 255, 0, 1.0)")
        self.assertEqual(options[0]["color"], "rgba(0, 255, 0, 0.0)")

    def test_missing_prices(self):
        options = dishwasher_options(datetime(2026, 11, 4, 9, 0, tzinfo=UTC))
        self.assertTrue(all(o["percentage"] is None for o in options))


class ChargingTests(TestCase):
    def test_picks_cheapest_quarters_and_groups_them(self):
        # cheap between 02:00 and 03:00 local (quarters 8..11) and at 05:00
        cheap = {8, 9, 10, 11, 20}
        fill_day(date(2026, 11, 4), lambda i: 10.0 if i in cheap else 100.0)
        now = datetime(2026, 11, 3, 23, 0, tzinfo=UTC) - timedelta(minutes=1)
        deadline = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
        # 5 quarters * 3.174 kW / 4 = 3.97 kWh -> ~6.6% of 60 kWh
        plan = plan_charging(20, 26, now, deadline)
        self.assertEqual(plan.needed_quarters, 5)
        self.assertFalse(plan.not_enough_time)
        self.assertEqual(
            [(g["start"].strftime("%H:%M"), g["end_label"]) for g in plan.groups],
            [("02:00", "03:00"), ("05:00", "05:15")],
        )
        self.assertAlmostEqual(plan.average_energy_price, consumption(10) / 100)

    def test_blocks_split_at_midnight(self):
        fill_day(date(2026, 11, 4))
        fill_day(date(2026, 11, 5))
        now = datetime(2026, 11, 4, 22, 29, tzinfo=UTC)  # 23:29 local
        plan = plan_charging(0, 4, now, datetime(2026, 11, 4, 23, 30, tzinfo=UTC))
        self.assertEqual(
            [(g["day"], g["start"].strftime("%H:%M"), g["end_label"]) for g in plan.groups],
            [("Wednesday", "23:30", "24:00"), ("Thursday", "00:00", "00:30")],
        )

    def test_not_enough_time(self):
        plan = plan_charging(0, 100, datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 4, 1, tzinfo=UTC))
        self.assertTrue(plan.not_enough_time)
        self.assertEqual(plan.groups, [])

    def test_nothing_needed(self):
        plan = plan_charging(80, 50, datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 5, tzinfo=UTC))
        self.assertEqual(plan.needed_quarters, 0)


@mock.patch("prices.views.ensure_prices_for", return_value=True)
class ViewTests(TestCase):
    def test_pages_render(self, _):
        fill_day(date(2026, 11, 4))
        for name in ["index", "dishwasher", "car_charging"]:
            self.assertEqual(self.client.get(reverse(f"prices:{name}")).status_code, 200, name)
        r = self.client.get(reverse("prices:price_list"), {"date": "2026-11-04"})
        self.assertContains(r, "04/11/2026")
        self.assertContains(r, "00:00 - 00:15")
        self.assertContains(r, "5,83")  # ejection price of 50 EUR/MWh

    def test_car_charging_post(self, _):
        r = self.client.post(
            reverse("prices:car_charging"),
            {"current_percentage": 20, "desired_percentage": 80, "day": "tomorrow", "hour": "07:00"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not enough time")

    def test_invalid_date_falls_back(self, _):
        self.assertEqual(self.client.get(reverse("prices:price_list"), {"date": "nope"}).status_code, 200)


def q(utc_dt, price):
    return PriceInterval(start=utc_dt, price=price)


def contract(name, valid_from, converter="quarter", **kw):
    defaults = dict(
        consumption_factor=1, consumption_offset=0, vat_rate=0, injection_factor=1, injection_offset=0
    )
    defaults.update(kw)
    return EnergyContract.objects.create(name=name, valid_from=valid_from, converter=converter, **defaults)


# Nov 5 00:00-01:00 local = Nov 4 23:00-24:00 UTC
FIRST_HOUR_NOV5 = [datetime(2026, 11, 4, 23, 15 * i, tzinfo=UTC) for i in range(4)]


class ConverterTests(TestCase):
    def test_registry(self):
        self.assertEqual([k for k, _ in converter_choices()][:3], ["quarter", "hour_mean", "hour_median"])

    def test_quarter_converter_yields_one_period_per_quarter(self):
        c = get_converter(contract("q", date(2026, 11, 5)))
        periods = list(c.periods([q(t, p) for t, p in zip(FIRST_HOUR_NOV5, [10, 20, 30, 100])]))
        self.assertEqual([p.market_price for p in periods], [10, 20, 30, 100])
        self.assertEqual([p.end - p.start for p in periods], [timedelta(minutes=15)] * 4)

    def test_hour_mean_yields_one_period_per_hour(self):
        c = get_converter(contract("h", date(2026, 11, 5), "hour_mean", vat_rate=10))
        (period,) = c.periods([q(t, p) for t, p in zip(FIRST_HOUR_NOV5, [10, 20, 30, 100])])
        self.assertEqual((period.start, period.end), (FIRST_HOUR_NOV5[0], FIRST_HOUR_NOV5[0] + timedelta(hours=1)))
        self.assertEqual(period.market_price, 40)
        self.assertAlmostEqual(period.consumption, 4.4)
        self.assertAlmostEqual(period.injection, 4.0)
        self.assertEqual(len(period.quarters), 4)

    def test_hour_median(self):
        c = get_converter(contract("m", date(2026, 11, 5), "hour_median"))
        (period,) = c.periods([q(t, p) for t, p in zip(FIRST_HOUR_NOV5, [10, 20, 30, 100])])
        self.assertEqual(period.market_price, 25)

    def test_custom_converter_can_be_plugged_in(self):
        @register
        class FlatConverter(Converter):
            key = "test_flat"
            label = "Flat 30 c/kWh"

            def consumption_price(self, market_price):
                return 30.0

        try:
            c = get_converter(contract("flat", date(2026, 11, 5), "test_flat"))
            (period,) = c.periods([q(FIRST_HOUR_NOV5[0], 500)])
            self.assertEqual(period.consumption, 30.0)
        finally:
            from . import converters

            converters._REGISTRY.pop("test_flat")


class PriceSeriesTests(TestCase):
    def test_each_period_uses_the_contract_valid_at_that_time(self):
        contract("New provider", date(2026, 11, 5), "hour_mean", consumption_offset=10, vat_rate=21)
        quarters = [q(datetime(2026, 11, 4, 22, 15 * i, tzinfo=UTC), 100) for i in range(4)]  # Nov 4 23:xx local
        quarters += [q(t, 100) for t in FIRST_HOUR_NOV5]  # Nov 5 00:xx local
        periods = list(PriceSeries().periods(quarters))
        # 4 quarter periods with the original formula, then 1 hourly period with the new one
        self.assertEqual(len(periods), 5)
        self.assertAlmostEqual(periods[0].consumption, consumption(100))
        self.assertAlmostEqual(periods[4].consumption, 110 / 10 * 1.21)
        self.assertEqual(periods[4].contract.name, "New provider")

    def test_dates_before_first_contract_use_the_earliest(self):
        EnergyContract.objects.all().delete()
        contract("Only", date(2026, 11, 5))
        self.assertEqual(PriceSeries().contract_at(datetime(2020, 1, 1, tzinfo=UTC)).name, "Only")

    def test_without_contracts_falls_back_to_original_formula(self):
        EnergyContract.objects.all().delete()
        (period,) = PriceSeries().periods([q(datetime(2026, 11, 4, tzinfo=UTC), 100)])
        self.assertAlmostEqual(period.consumption, consumption(100))

    def test_periods_between_uses_whole_hours_at_the_edges(self):
        contract("h", date(2026, 11, 5), "hour_mean")
        fill_day(date(2026, 11, 5), lambda i: [10.0, 20.0, 30.0, 100.0][i] if i < 4 else 50.0)
        # window starts at 00:30 local, inside the first hour
        periods = list(PriceSeries().periods_between(FIRST_HOUR_NOV5[2], FIRST_HOUR_NOV5[0] + timedelta(hours=2)))
        self.assertEqual([p.market_price for p in periods], [40, 50])


@mock.patch("prices.views.ensure_prices_for", return_value=True)
class PriceListTests(TestCase):
    def test_history_keeps_old_contract_and_granularity(self, _):
        contract("New", date(2026, 11, 5), "hour_mean", consumption_offset=10, vat_rate=21)
        fill_day(date(2026, 11, 4))
        fill_day(date(2026, 11, 5))
        old = self.client.get(reverse("prices:price_list"), {"date": "2026-11-04"})
        new = self.client.get(reverse("prices:price_list"), {"date": "2026-11-05"})
        self.assertContains(old, "5,83")  # (1.02*50+4)/10*1.06
        self.assertContains(old, "00:00 - 00:15")
        self.assertEqual(old.content.decode().count("badge-pill"), 96)
        self.assertContains(new, "7,26")  # (50+10)/10*1.21
        self.assertContains(new, "00:00 - 01:00")
        self.assertEqual(new.content.decode().count("badge-pill"), 24)


class HourlyChargingTests(TestCase):
    def test_charging_ranks_by_hourly_price(self):
        fill_day(date(2026, 11, 5), lambda i: [10.0, 20.0, 30.0, 100.0][i] if i < 4 else 50.0)
        # Quarter-wise 00:00 (10 EUR) is cheapest, but per hour 00:00-01:00 averages 40
        # while 01:00-02:00 costs a flat 35.
        PriceInterval.objects.filter(
            start__gte=datetime(2026, 11, 5, 0, tzinfo=UTC), start__lt=datetime(2026, 11, 5, 1, tzinfo=UTC)
        ).update(price=35)
        now = datetime(2026, 11, 4, 22, 59, tzinfo=UTC)
        deadline = datetime(2026, 11, 5, 1, 0, tzinfo=UTC)  # 02:00 local
        needed = (20, 21)  # 0.6 kWh -> 1 quarter

        c = contract("tariff", date(2026, 11, 5))
        plan = plan_charging(*needed, now, deadline)
        self.assertEqual(plan.groups[0]["start"].strftime("%H:%M"), "00:00")  # the 10 EUR quarter

        c.converter = "hour_mean"
        c.save()
        plan = plan_charging(*needed, now, deadline)
        self.assertEqual(plan.groups[0]["start"].strftime("%H:%M"), "01:00")  # hour avg 35 < 40
        self.assertAlmostEqual(plan.average_energy_price, 3.5 / 100)


class BelpexContractMigrationTests(TestCase):
    def setUp(self):
        pass  # keep the contracts created by the migrations

    def test_belpex_contract_formula(self):
        contract = EnergyContract.objects.get(name="Dynamic contract - Belpex hourly")
        self.assertEqual(contract.converter, "hour_mean")
        # (0.102 * index + 0.408) c/kWh excl. VAT, + 6% VAT
        self.assertAlmostEqual(contract.consumption_price(200), (0.102 * 200 + 0.408) * 1.06)


class FetchPricesCommandTests(TestCase):
    def test_default_covers_history_through_tomorrow(self):
        from io import StringIO

        from django.core.management import call_command

        with mock.patch("prices.management.commands.fetch_prices.ensure_prices_for", return_value=True) as ensure, \
                mock.patch("prices.management.commands.fetch_prices.timezone.localdate", return_value=date(2026, 11, 4)):
            call_command("fetch_prices", stdout=StringIO())
        days = [c.args[0] for c in ensure.call_args_list]
        self.assertEqual(days[0], date(2026, 10, 5))
        self.assertEqual(days[-1], date(2026, 11, 5))
