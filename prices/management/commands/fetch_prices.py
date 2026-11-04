from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from prices.entsoe import ensure_prices_for


class Command(BaseCommand):
    help = "Fetch ENTSO-E day-ahead prices (default: today and tomorrow). Suitable for cron."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat, help="First date (YYYY-MM-DD)")
        parser.add_argument("--days", type=int, default=None, help="Number of days to fetch")
        parser.add_argument("--force", action="store_true", help="Refetch even if already stored")

    def handle(self, *args, start=None, days=None, force=False, **options):
        days = days or (1 if start else 2)
        start = start or timezone.localdate()
        for i in range(days):
            day = start + timedelta(days=i)
            ok = ensure_prices_for(day, force=force)
            self.stdout.write(f"{day}: {'ok' if ok else 'not available'}")
