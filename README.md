# DjangoCharge

Django port of the dyncharge PHP app: Belgian day-ahead electricity prices
(ENTSO-E), and when to run the dishwasher or charge the car.

## Local development

```bash
pip install -r requirements.txt
createdb djangocharge
cp .env.example .env          # set ENTSOE_TOKEN
python manage.py migrate
python manage.py fetch_prices --start 2026-10-01 --days 35   # fill 30-day history for quartile colours
python manage.py runserver
python manage.py test prices
```

## Pages

- `/prices/?date=YYYY-MM-DD` – quarter-hourly consumption/injection price (c€/kWh), coloured by the
  quartiles of the previous 30 days
- `/dishwasher/` – relative cost of starting now, in 3, 6 or 9 hours
- `/car-charging/` – cheapest quarters to charge before a deadline

Prices are fetched on demand when a day is missing, and by the `fetch_prices` cron job.

## Deployment (Appliku)

`appliku.yml` defines the build, `run.sh` (gunicorn), `release.sh` (migrations), a PostgreSQL 17
database (`DATABASE_URL`), and an hourly `fetch_prices` cron job. Set `SECRET_KEY` and
`ENTSOE_TOKEN` as config vars; `ALLOWED_HOSTS` comes from the app's domains.
