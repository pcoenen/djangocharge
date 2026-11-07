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
- `/car-charging/` – cheapest quarters to charge before a deadline (can be added as a target)
- `/energy/` – the energy manager: battery, targets and the combined charging plan with reasons
- `/energy/settings/` – price rule: always charge below a fixed price, the 30-day 25% mark or median
- `/tesla/` – Tesla connection, virtual key and battery log

## Energy manager

`python manage.py energy_tick` runs every 30 minutes (cron). It reads the car's battery from
Tesla's cache (never wakes the car), re-plans (price rule first, then each target's shortfall with
the cheapest quarters before its deadline) and, one hour before the first quarter that differs
from what's on the car, wakes the car and replaces its charge schedules and limit. A recurring 1-minute "hold" schedule
(04:00 daily) always stays on the car: without any schedule, a Tesla charges as soon as it's
plugged in. A car that
isn't plugged in isn't woken; at most 6 sends a day. Problems are emailed once to `NOTIFY_EMAIL`
through Maileroo SMTP (the preference_planner account); set `EMAIL_HOST_PASSWORD` to enable.

## Energy contracts

The database stores the raw ENTSO-E market prices (EUR/MWh). An `EnergyContract`
(managed in `/admin/`) converts them into what the provider charges and pays:

    consumption (c€/kWh) = (consumption_factor × market + consumption_offset) / 10 × (1 + VAT%)
    injection   (c€/kWh) = (injection_factor × market + injection_offset) / 10

`converter` picks the converter class (`prices/converters.py`) for the tariff. The converter
groups the stored quarters into the periods the tariff bills on and converts them; the UI just
shows whatever periods it yields:

- `quarter` – one row per 15 minutes
- `hour_mean` / `hour_median` – one row per hour, priced on the mean/median of its quarters

A new kind of tariff is a subclass of `Converter` decorated with `@register`; it then appears as a
choice in the admin.

Each contract applies from its `valid_from` date until the next one, so when you switch
provider you add a new contract and history keeps using the old one.

Prices are fetched on demand when a day is missing, and by the `fetch_prices` cron job.

## Deployment (Appliku)

`appliku.yml` defines the build, `run.sh` (gunicorn), `release.sh` (migrations), a PostgreSQL 17
database (`DATABASE_URL`), a daily `fetch_prices` and a 30-minute `energy_tick` cron job. Set
`SECRET_KEY`, `ENTSOE_TOKEN`, the `TESLA_*` vars, `CHARGE_CALENDAR_URL`, `EMAIL_HOST_PASSWORD` and
`NOTIFY_EMAIL` as config vars; `ALLOWED_HOSTS` comes from the app's domains.
