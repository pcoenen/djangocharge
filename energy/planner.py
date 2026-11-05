"""The energy planner: one charging plan that serves every need at once.

Inputs are the battery level, the upcoming quarters with their actual price,
whether the price rule wants a quarter ("cheap"), and the needs (targets,
later calendar events). The plan is built in two steps:

1. Take every quarter the price rule wants.
2. Walk the needs by deadline. Charging already planned before a deadline
   (cheap quarters, quarters picked for an earlier need) counts toward it; only
   the shortfall is filled with the cheapest free quarters before the deadline.

So a cheap-price block that already brings the car to 40% before a calendar
event means nothing extra is planned for that event.

`make_plan` is pure (no database); `build_plan` collects the inputs.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

QUARTER = timedelta(minutes=15)
FULL = 100.0

MET, WAITING, SHORT = "met", "waiting", "short"


@dataclass
class Slot:
    start: datetime
    end: datetime
    price: float  # c€/kWh incl. VAT
    cheap: str = ""  # why the price rule wants this quarter; "" if it doesn't


@dataclass
class Need:
    deadline: datetime
    pct: float
    label: str


@dataclass
class NeedResult:
    need: Need
    expected_pct: float  # battery at the deadline with the planned (known-price) charging
    status: str  # MET, WAITING (rest is charged once later prices are known) or SHORT
    quarters_later: int = 0  # quarters still to be planned once prices are known
    shortfall_pct: float = 0.0  # SHORT: how much it will miss


@dataclass
class Block:
    start: datetime
    end: datetime
    reasons: list
    price: float  # average c€/kWh
    quarters: int


@dataclass
class Plan:
    now: datetime
    battery_pct: float
    pct_per_quarter: float
    prices_until: datetime | None
    blocks: list = field(default_factory=list)
    needs: list = field(default_factory=list)
    charge_limit: int | None = None

    @property
    def quarters(self):
        return sum(b.quarters for b in self.blocks)

    @property
    def expected_final_pct(self):
        return min(FULL, self.battery_pct + self.quarters * self.pct_per_quarter)

    @property
    def problems(self):
        return [r for r in self.needs if r.status == SHORT]


def make_plan(battery_pct, slots, needs, *, now, pct_per_quarter):
    slots = sorted((s for s in slots if s.start >= now), key=lambda s: s.start)
    prices_until = slots[-1].end if slots else None
    reasons = {i: [s.cheap] for i, s in enumerate(slots) if s.cheap}

    def level_at(t):
        charged = sum(1 for i in reasons if slots[i].end <= t)
        return min(FULL, battery_pct + charged * pct_per_quarter)

    results, limit_for_needs = [], 0
    for need in sorted(needs, key=lambda n: n.deadline):
        if need.deadline <= now:
            continue
        missing = need.pct - level_at(need.deadline)
        if missing <= 0:
            results.append(NeedResult(need, level_at(need.deadline), MET))
            continue
        quarters_needed = math.ceil(missing / pct_per_quarter - 1e-9)
        # Quarters between the last known price and the deadline can be planned
        # tomorrow, once their prices are published.
        later = 0
        if prices_until is None or need.deadline > prices_until:
            later = int((need.deadline - max(prices_until or now, now)) / QUARTER)
        now_needed = max(0, quarters_needed - later)
        free = sorted(
            (i for i, s in enumerate(slots) if i not in reasons and s.end <= need.deadline),
            key=lambda i: (slots[i].price, slots[i].start),
        )
        for i in free[:now_needed]:
            reasons[i] = [f"needed for {need.label}"]
        if now_needed:
            limit_for_needs = max(limit_for_needs, math.ceil(need.pct))
        expected = level_at(need.deadline)
        still_later = quarters_needed - min(len(free), now_needed)
        if len(free) < now_needed:
            reachable = min(FULL, expected + later * pct_per_quarter)
            results.append(NeedResult(need, expected, SHORT, later, need.pct - reachable))
        elif expected >= need.pct:
            results.append(NeedResult(need, expected, MET))
        else:
            limit_for_needs = max(limit_for_needs, math.ceil(need.pct))
            results.append(NeedResult(need, expected, WAITING, still_later))

    # The car stops at 100%: drop quarters that would come after that.
    kept, level = [], battery_pct
    for i in sorted(reasons):
        if level >= FULL:
            break
        kept.append(i)
        level += pct_per_quarter

    plan = Plan(now=now, battery_pct=battery_pct, pct_per_quarter=pct_per_quarter,
                prices_until=prices_until, needs=results)
    for i in kept:
        s = slots[i]
        b = plan.blocks[-1] if plan.blocks else None
        if b and b.end == s.start:
            b.price = (b.price * b.quarters + s.price) / (b.quarters + 1)
            b.end, b.quarters = s.end, b.quarters + 1
            b.reasons += [r for r in reasons[i] if r not in b.reasons]
        else:
            plan.blocks.append(Block(s.start, s.end, list(reasons[i]), s.price, 1))

    if kept:
        any_cheap = any(slots[i].cheap for i in kept)
        plan.charge_limit = 100 if any_cheap else min(100, max(limit_for_needs, math.ceil(level)))
    return plan
