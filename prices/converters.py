"""Pluggable price converters.

The database only stores raw ENTSO-E quarter prices (EUR/MWh). A converter turns
them into what an energy contract actually bills, *and* decides the granularity:
it groups quarters into the periods the contract prices on (quarters, hours, ...).

    converter = get_converter(contract)
    for period in converter.periods(quarter_intervals):
        period.start, period.end, period.consumption, period.injection

`PriceSeries` does the same across contract switches and is what the views use.

To add a new kind of contract, subclass `Converter` (override `bucket`,
`aggregate` and/or the price methods) and decorate it with `@register`; it then
shows up as a choice on EnergyContract.converter.
"""

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import groupby

from django.utils import timezone

QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)

_REGISTRY = {}


def register(cls):
    _REGISTRY[cls.key] = cls
    return cls


def converter_choices():
    return [(key, cls.label) for key, cls in _REGISTRY.items()]


def get_converter(contract):
    return _REGISTRY[contract.converter](contract)


@dataclass
class PricePeriod:
    """One billing period with its converted prices."""

    start: datetime
    end: datetime
    market_price: float  # the index the formula is applied to (EUR/MWh)
    consumption: float  # c€/kWh incl. VAT
    injection: float  # c€/kWh
    quarters: list = field(default_factory=list)  # underlying PriceIntervals
    contract: object = None

    def contains(self, dt):
        return self.start <= dt < self.end


class Converter:
    """Base converter: one period per quarter, linear formula from the contract."""

    key = None
    label = None
    period_length = QUARTER

    def __init__(self, contract):
        self.contract = contract

    # --- granularity -----------------------------------------------------

    def bucket(self, start):
        """Start of the period a quarter belongs to."""
        return start

    def aggregate(self, prices):
        """Combine the market prices of the quarters in one period."""
        return statistics.fmean(prices)

    # --- formula ---------------------------------------------------------

    def consumption_price(self, market_price):
        return self.contract.consumption_price(market_price)

    def injection_price(self, market_price):
        return self.contract.injection_price(market_price)

    # --- iterator --------------------------------------------------------

    def periods(self, intervals):
        """Yield PricePeriods for chronologically ordered quarter intervals."""
        for bucket_start, group in groupby(intervals, key=lambda i: self.bucket(i.start)):
            quarters = list(group)
            market = self.aggregate([q.price for q in quarters])
            yield PricePeriod(
                start=bucket_start,
                end=bucket_start + self.period_length,
                market_price=market,
                consumption=self.consumption_price(market),
                injection=self.injection_price(market),
                quarters=quarters,
                contract=self.contract,
            )


@register
class QuarterConverter(Converter):
    key = "quarter"
    label = "Per quarter (15-minute price)"


class HourlyConverter(Converter):
    period_length = HOUR

    def bucket(self, start):
        # Belgian time is a whole-hour offset from UTC, so UTC hours are local hours.
        return start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


@register
class HourlyMeanConverter(HourlyConverter):
    key = "hour_mean"
    label = "Per hour (average of the 4 quarters)"


@register
class HourlyMedianConverter(HourlyConverter):
    key = "hour_median"
    label = "Per hour (median of the 4 quarters)"

    def aggregate(self, prices):
        return statistics.median(prices)


class PriceSeries:
    """Converts quarter prices with whichever contract applied at the time.

    Splits the quarters into runs per contract (contracts change at local
    midnight) and lets each contract's converter produce the periods.
    """

    def __init__(self, contracts=None):
        from .models import EnergyContract

        if contracts is None:
            contracts = EnergyContract.objects.all()
        self._contracts = sorted(contracts, key=lambda c: c.valid_from)
        self._dates = [c.valid_from for c in self._contracts]
        self._default = EnergyContract(name="Default")

    def contract_at(self, dt):
        i = bisect.bisect_right(self._dates, timezone.localtime(dt).date()) - 1
        if i < 0:
            return self._contracts[0] if self._contracts else self._default
        return self._contracts[i]

    def periods(self, intervals):
        """Yield PricePeriods for chronologically ordered quarter intervals."""
        for contract, run in groupby(intervals, key=lambda i: self.contract_at(i.start)):
            yield from get_converter(contract).periods(run)

    def periods_between(self, start, end):
        """Periods for the stored prices in [start, end).

        Periods are always built from whole local days, so an hourly period at
        the edge of the window still uses all its quarters.
        """
        from .models import PriceInterval
        from .pricing import day_bounds

        first, _ = day_bounds(timezone.localtime(start).date())
        _, last = day_bounds(timezone.localtime(end - QUARTER).date())
        intervals = PriceInterval.objects.filter(start__gte=first, start__lt=last).order_by("start")
        for period in self.periods(intervals):
            if period.end > start and period.start < end:
                yield period
