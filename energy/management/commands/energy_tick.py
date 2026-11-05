"""The energy manager's periodic check (every 30 minutes via cron).

1. Record what the car last reported to Tesla's cache (never wakes the car).
2. Read the calendar: every event becomes a 40% target 30 minutes before it.
3. Re-plan from the latest battery level, targets and prices; store the plan
   when it changed.
4. Report targets that can't be reached.
5. Send the plan to the car when it differs and the first change is within
   the hour (this wakes the car).
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from energy import manager, sync
from energy.calendar_sync import CalendarError, sync_calendar
from energy.notify import notify
from tesla import client

LOCK_ID = 74_865_201  # pg advisory lock: one tick at a time


class Command(BaseCommand):
    help = "Read the car's cached state, re-plan and keep the car's schedules in line with the plan."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            if not cursor.fetchone()[0]:
                self.stdout.write("Another tick is still running; skipped.")
                return
            try:
                self.tick()
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])

    def tick(self):
        errors = []
        now = timezone.now()
        vehicle = None
        try:
            vehicle = client.get_vehicle()
            obs = client.observe(vehicle)
            when = f"{obs.reported_at:%Y-%m-%d %H:%M %Z}" if obs.reported_at else "unknown time"
            self.stdout.write(f"{obs.vehicle}: {obs.battery_level}% {obs.charging_state or '?'}, reported {when}")
        except client.NotConnected as e:
            self.stdout.write(f"Car not read: {e}")
        except client.AuthError as e:
            notify(f"auth:{timezone.localdate(now)}", "Reconnect Tesla: the login stopped working", str(e))
            errors.append(f"Tesla login failed: {e}")
        except client.TeslaError as e:
            errors.append(f"Reading the car's cached state failed: {e}")

        if settings.CHARGE_CALENDAR_URL:
            try:
                added, changed, removed = sync_calendar(now)
                self.stdout.write(f"Calendar: {added} added, {changed} changed, {removed} removed")
            except CalendarError as e:
                # keep the targets we have; the next check tries again
                notify(f"calendar:{timezone.localdate(now)}", "Couldn't read the calendar", str(e))
                errors.append(str(e))

        # Plan anyway: with the last known battery level if the cache read failed
        plan = manager.build_plan(now)
        if plan is None:
            self.stdout.write("No battery level known yet; nothing to plan.")
        else:
            _, changed = manager.save_snapshot(plan)
            blocks = ", ".join(f"{timezone.localtime(b.start):%a %H:%M}-{timezone.localtime(b.end):%H:%M}"
                               for b in plan.blocks) or "none"
            self.stdout.write(f"Plan {'changed' if changed else 'unchanged'}: limit {plan.charge_limit}, blocks {blocks}")
            for r in plan.problems:
                self.stdout.write(f"WARNING {r.need.label}: about {r.shortfall_pct:.0f}% short")
                notify(
                    f"short:{r.need.label}",
                    f"Not enough time to reach {r.need.label}",
                    f"The car will be about {r.shortfall_pct:.0f}% short of {r.need.label}.",
                )
            if vehicle is not None:
                try:
                    decision, sub = sync.sync(plan, vehicle, manager.latest_observation(), now)
                    msg = {sync.IN_SYNC: "car is in sync",
                           sync.WAIT: f"send at {timezone.localtime(decision.send_at):%a %H:%M}" if decision.send_at else "",
                           sync.SEND: "sent to the car",
                           sync.UNPLUGGED: "not sent: car not plugged in",
                           sync.BUDGET: "not sent: daily wake-up limit reached"}[decision.action]
                    self.stdout.write(f"Sync: {msg}{' (' + decision.summary + ')' if decision.reasons else ''}")
                except client.TeslaError as e:
                    errors.append(f"Sending the plan to the car failed: {e}")
        if errors:
            for e in errors:
                self.stderr.write(e)
            raise CommandError("; ".join(errors))
