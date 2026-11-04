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
database (`DATABASE_URL`), and an hourly `fetch_prices` cron job. Set `SECRET_KEY` and
`ENTSOE_TOKEN` as config vars; `ALLOWED_HOSTS` comes from the app's domains.
