from datetime import UTC, date, datetime, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .entsoe import EntsoeError, ensure_prices_for, parse_prices
from .models import PriceInterval
from .pricing import (
    day_bounds,
    dishwasher_options,
    ejection_price,
    injection_price,
    plan_charging,
    quartiles_for_date,
)

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


class FormulaTests(TestCase):
    def test_prices(self):
        self.assertAlmostEqual(ejection_price(100), (102 + 4) / 10 * 1.06)
        self.assertAlmostEqual(injection_price(100), (98 - 15) / 10)

    def test_quartiles_default_without_data(self):
        self.assertEqual(quartiles_for_date(date(2026, 11, 4)), [0, 100, 100])

    def test_quartiles(self):
        fill_day(date(2026, 11, 3), lambda i: float(i))  # 0..95
        self.assertEqual(quartiles_for_date(date(2026, 11, 4)), [24.0, 48.0, 72.0])


class EnsurePricesTests(TestCase):
    def setUp(self):
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
        self.assertAlmostEqual(plan.average_energy_price, ejection_price(10) / 100)

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
