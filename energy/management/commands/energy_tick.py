"""The energy manager's periodic check (every 30 minutes via cron).

1. Record what the car last reported to Tesla's cache (never wakes the car).
2. Re-plan from the latest battery level, targets and prices; store the plan
   when it changed.

Nothing is sent to the car yet.
"""

from django.core.management.base import BaseCommand, CommandError

from energy import manager
from tesla import client


class Command(BaseCommand):
    help = "Read the car's cached battery state and re-plan (never wakes the car)."

    def handle(self, *args, **options):
        error = None
        try:
            obs = client.observe()
            when = f"{obs.reported_at:%Y-%m-%d %H:%M %Z}" if obs.reported_at else "unknown time"
            self.stdout.write(f"{obs.vehicle}: {obs.battery_level}% {obs.charging_state or '?'}, reported {when}")
        except client.NotConnected as e:
            self.stdout.write(f"Car not read: {e}")
        except client.TeslaError as e:
            error = f"Reading the car's cached state failed: {e}"
            self.stderr.write(error)

        # Plan anyway: with the last known battery level if the cache read failed
        plan = manager.build_plan()
        if plan is None:
            self.stdout.write("No battery level known yet; nothing to plan.")
        else:
            _, changed = manager.save_snapshot(plan)
            blocks = ", ".join(f"{b.start:%a %H:%M}-{b.end:%H:%M}" for b in plan.blocks) or "none"
            self.stdout.write(f"Plan {'changed' if changed else 'unchanged'}: limit {plan.charge_limit}, blocks {blocks}")
            for r in plan.problems:
                self.stdout.write(f"WARNING {r.need.label}: about {r.shortfall_pct:.0f}% short")
        if error:
            raise CommandError(error)
