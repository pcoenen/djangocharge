"""The energy manager's periodic check (every 30 minutes via cron).

Phase 1 - watch only: record what the car last reported to Tesla's cache.
Never wakes the car and never sends commands.
"""

from django.core.management.base import BaseCommand, CommandError

from tesla import client


class Command(BaseCommand):
    help = "Record the car's cached battery state (never wakes the car)."

    def handle(self, *args, **options):
        try:
            obs = client.observe()
        except client.NotConnected as e:
            self.stdout.write(f"Skipped: {e}")
            return
        except client.TeslaError as e:
            raise CommandError(f"Reading the car's cached state failed: {e}") from e
        self.stdout.write(
            f"{obs.vehicle}: {obs.battery_level}% {obs.charging_state or '?'}, "
            f"reported {obs.reported_at:%Y-%m-%d %H:%M %Z}" if obs.reported_at else f"{obs.vehicle}: no report time"
        )
