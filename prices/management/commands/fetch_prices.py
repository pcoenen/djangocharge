from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from prices.entsoe import ensure_prices_for

HISTORY_DAYS = 30  # needed for the quartile colours on the price list


class Command(BaseCommand):
    help = (
        "Fetch ENTSO-E day-ahead prices. By default makes sure the last 30 days, today and "
        "tomorrow are stored; complete days are skipped, so this is cheap to run from cron."
    )

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat, help="First date (YYYY-MM-DD)")
        parser.add_argument("--days", type=int, default=None, help="Number of days to fetch")
        parser.add_argument("--force", action="store_true", help="Refetch even if already stored")

    def handle(self, *args, start=None, days=None, force=False, **options):
        today = timezone.localdate()
        if start is None:
            start = today - timedelta(days=HISTORY_DAYS)
            days = days or HISTORY_DAYS + 2  # through tomorrow
        days = days or 1
        for i in range(days):
            day = start + timedelta(days=i)
            ok = ensure_prices_for(day, force=force)
            self.stdout.write(f"{day}: {'ok' if ok else 'not available'}")
