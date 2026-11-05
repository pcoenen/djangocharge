"""Calendar events (.ics link in CHARGE_CALENDAR_URL) become charge targets.

Every event needs the car at CALENDAR_PCT % CALENDAR_LEAD before it starts.
Each 30-minute check reads the calendar again and syncs the targets for the
events starting within the next 24 hours:
new event -> new target, moved event -> deadline moves, deleted or cancelled
event -> target removed. An event you ignored stays ignored, also when moved.
"""

from datetime import UTC, date, datetime, time, timedelta

import recurring_ical_events
import requests
from django.conf import settings
from django.utils import timezone
from icalendar import Calendar

from .models import ChargeTarget

CALENDAR_PCT = 40
CALENDAR_LEAD = timedelta(minutes=30)
LOOKAHEAD = timedelta(hours=24)  # only events starting within 24 hours become targets


class CalendarError(Exception):
    pass


def fetch(url):
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        # don't show the private link in errors
        raise CalendarError(f"Reading the calendar failed: {type(e).__name__}") from e
    return resp.content


def _aware(value):
    """DTSTART value -> aware datetime; all-day dates start at local midnight."""
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.get_current_timezone())
    raise CalendarError(f"Unsupported start: {value!r}")


def events(ics, start, end):
    """[{key, start, summary}] for the events starting in [start, end)."""
    try:
        cal = Calendar.from_ical(ics)
        occurrences = recurring_ical_events.of(cal).between(start, end)
    except Exception as e:  # parsing errors come in many types
        raise CalendarError(f"The calendar couldn't be read: {e}") from e
    # recurring_ical_events puts a RECURRENCE-ID on every occurrence, so tell
    # repeating events apart from the raw calendar.
    repeating = {
        str(c.get("UID", "")) for c in cal.walk("VEVENT") if c.get("RRULE") or c.get("RECURRENCE-ID")
    }
    out = []
    for ev in occurrences:
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin = _aware(ev.decoded("DTSTART"))
        if not (start <= begin < end):
            continue
        uid = str(ev.get("UID", ""))
        # Repeating events: one target per occurrence, keyed by its original start,
        # so moving one occurrence keeps its key (and its ignored flag).
        if uid in repeating:
            original = ev.decoded("RECURRENCE-ID") if ev.get("RECURRENCE-ID") else ev.decoded("DTSTART")
            key = f"{uid}@{_aware(original).astimezone(UTC).isoformat()}"
        else:
            key = uid
        out.append({"key": key or begin.isoformat(), "start": begin, "summary": str(ev.get("SUMMARY", "")).strip()})
    return out


def sync_calendar(now=None, url=None):
    """Bring the calendar targets in line with the calendar. Returns (added, changed, removed)."""
    now = now or timezone.now()
    url = url if url is not None else settings.CHARGE_CALENDAR_URL
    if not url:
        return 0, 0, 0
    found = events(fetch(url), now, now + LOOKAHEAD)
    existing = {t.external_id: t for t in ChargeTarget.objects.filter(source=ChargeTarget.CALENDAR)}
    added = changed = 0
    seen = set()
    for ev in found:
        key = ev["key"][:500]
        seen.add(key)
        deadline = ev["start"] - CALENDAR_LEAD
        label = ev["summary"] or "calendar event"
        target = existing.get(key)
        if target is None:
            ChargeTarget.objects.create(
                source=ChargeTarget.CALENDAR, external_id=key, battery_pct=CALENDAR_PCT, deadline=deadline, label=label
            )
            added += 1
        elif target.deadline != deadline or target.label != label:
            target.deadline, target.label = deadline, label
            target.save(update_fields=["deadline", "label"])
            changed += 1
    # Gone from the calendar (deleted/cancelled/moved out of range), or long past
    stale = [t.pk for key, t in existing.items() if key not in seen]
    removed, _ = ChargeTarget.objects.filter(pk__in=stale).delete()
    return added, changed, removed
