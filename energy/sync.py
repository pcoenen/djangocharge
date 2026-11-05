"""Keep the car's charge schedules in line with the plan, waking it as little as possible.

Rules:
- Compare the plan with what was last sent to the car, per quarter.
- Ignore differences that don't change what the car does (extra quarters at
  the end, after the car would already have reached its limit).
- Send one hour before the first quarter that differs (02:00 → the 01:00 check), so
  the plan includes everything known by then (battery after the evening drive,
  new prices). Sending wakes the car.
- Don't send to a car that isn't plugged in (plugging in wakes it and the next
  check picks it up), and at most MAX_SENDS_PER_DAY times a day.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from tesla import client
from tesla.models import ChargePlanSubmission

from .notify import notify

QUARTER = timedelta(minutes=15)
SEND_AHEAD = timedelta(hours=1)  # plan starting 02:00 is sent at the 01:00 check (01:30 retries)
MAX_SENDS_PER_DAY = 6
LIMIT_SLACK = 5  # % the car's limit may be above the plan's before it's worth a wake
FAILURES_BEFORE_EMAIL = 2

IN_SYNC, WAIT, SEND, UNPLUGGED, BUDGET = "in_sync", "wait", "send", "unplugged", "budget"


@dataclass
class Decision:
    action: str
    reasons: list = field(default_factory=list)
    send_at: datetime | None = None  # WAIT: when it will be sent
    first_change: datetime | None = None
    must: bool = False  # a target or calendar event depends on it
    limit: int | None = None  # charge limit to send

    @property
    def summary(self):
        return "; ".join(self.reasons)


def _quarters(blocks):
    """Quarter starts covered by blocks of (start, end)."""
    out = set()
    for start, end in blocks:
        t = start
        while t < end:
            out.add(t)
            t += QUARTER
    return out


def on_car(vehicle):
    """The last plan that reached the car, or None."""
    return ChargePlanSubmission.objects.filter(vehicle=vehicle, succeeded=True).order_by("-created_at", "-pk").first()


def _submission_blocks(sub):
    return [(datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])) for s in sub.schedules]


def desired_limit(plan):
    # Nothing to charge: the lowest limit Tesla accepts, so plugging in doesn't start charging
    if not plan.blocks:
        return client.MIN_CHARGE_LIMIT
    return max(client.MIN_CHARGE_LIMIT, plan.charge_limit or client.MIN_CHARGE_LIMIT)


def decide(plan, vehicle, observation, now=None):
    now = now or timezone.now()
    current = lambda q: q + QUARTER > now  # noqa: E731 - still to come (or running)
    want = {q for q in _quarters([(b.start, b.end) for b in plan.blocks]) if current(q)}
    must_quarters = {
        q for b in plan.blocks if any(r.startswith("needed for") for r in b.reasons)
        for q in _quarters([(b.start, b.end)])
    }
    limit = desired_limit(plan)
    sub = on_car(vehicle)
    have = {q for q in _quarters(_submission_blocks(sub)) if current(q)} if sub else set()
    car_limit = sub.charge_limit if sub else None

    reasons, times, must = [], [], False
    missing = sorted(want - have)
    if missing:
        reasons.append(f"{len(missing)} quarter(s) to add from {timezone.localtime(missing[0]):%a %H:%M}")
        times.append(missing[0])
        must = must or bool(must_quarters & set(missing))
    last_wanted = max(want) if want else None
    surplus = sorted(have - want)
    early = [q for q in surplus if last_wanted and q < last_wanted]
    late = [q for q in surplus if q not in early]
    if early:
        reasons.append(f"{len(early)} quarter(s) to drop from {timezone.localtime(early[0]):%a %H:%M}")
        times.append(early[0])
    level_after = plan.expected_final_pct
    if late and (car_limit is None or car_limit > level_after + 3):
        reasons.append("the car would charge more than planned")
        times.append(late[0])
    if car_limit is None:
        # Never sent: only worth a wake once there's something to charge
        if want:
            reasons.append("no plan sent to the car yet")
            times.append(min(want))
    elif limit > car_limit or car_limit > limit + LIMIT_SLACK:
        reasons.append(f"charge limit {car_limit}% → {limit}%")
        times.append(min(want | have, default=now))
        must = must or limit > car_limit

    if not reasons:
        return Decision(IN_SYNC, limit=limit)
    first = max(min(times), now)
    decision = Decision(SEND, reasons, first_change=first, must=must, limit=limit)
    if first - now > SEND_AHEAD:
        decision.action, decision.send_at = WAIT, first - SEND_AHEAD
    elif observation is not None and not observation.plugged_in:
        decision.action = UNPLUGGED
    elif sends_last_day(vehicle, now) >= MAX_SENDS_PER_DAY:
        decision.action = BUDGET
    return decision


def sends_last_day(vehicle, now):
    return ChargePlanSubmission.objects.filter(
        vehicle=vehicle, trigger="auto", created_at__gte=now - timedelta(days=1)
    ).count()


def consecutive_failures(vehicle):
    count = 0
    for ok in ChargePlanSubmission.objects.filter(vehicle=vehicle, trigger="auto").order_by("-created_at", "-pk") \
            .values_list("succeeded", flat=True)[:10]:
        if ok:
            break
        count += 1
    return count


def sync(plan, vehicle, observation, now=None):
    """Decide and, when it's time, send the plan to the car. Returns (decision, submission|None)."""
    now = now or timezone.now()
    decision = decide(plan, vehicle, observation, now)
    when = f"{timezone.localtime(decision.first_change):%a %H:%M}" if decision.first_change else ""
    if decision.action == UNPLUGGED and decision.must:
        notify(
            f"unplugged:{decision.first_change.isoformat()}",
            f"Plug in the car: charging for a target should start {when}",
            f"The car isn't plugged in, but charging needed for a target starts {when}.\n{decision.summary}",
        )
    if decision.action == BUDGET:
        notify(
            f"budget:{timezone.localdate(now)}",
            "Charge plan not sent: daily limit of car wake-ups reached",
            f"Dyncharge already sent {MAX_SENDS_PER_DAY} plans in the last 24 hours. Pending: {decision.summary}",
        )
    if decision.action != SEND:
        return decision, None

    last = plan.needs[-1].need.deadline if plan.needs else (plan.blocks[-1].end if plan.blocks else None)
    try:
        sub = client.send_charge_plan(
            vehicle, [{"start": b.start, "end": b.end} for b in plan.blocks], decision.limit,
            deadline=last, trigger="auto", reason=decision.summary,
        )
    except client.TeslaError as e:
        if consecutive_failures(vehicle) >= FAILURES_BEFORE_EMAIL:
            last_ok = on_car(vehicle)
            notify(
                f"sendfail:{last_ok.pk if last_ok else 0}",
                "Sending the charge plan to the car keeps failing",
                f"Last error: {e}\nPending: {decision.summary}",
            )
        raise
    return decision, sub
