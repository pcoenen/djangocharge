"""Fetching day-ahead prices from the ENTSO-E Transparency Platform."""

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import requests
from django.conf import settings
from django.core.cache import cache

from .models import QUARTER, PriceInterval
from .pricing import day_bounds

logger = logging.getLogger(__name__)

# Don't hit the API again for a day whose prices aren't published yet
# (tomorrow's prices appear around 13:00 CET).
RETRY_AFTER = 10 * 60


class EntsoeError(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _iter(el, name):
    return (e for e in el.iter() if _local(e.tag) == name)


def _parse_dt(s):
    # ENTSO-E uses e.g. "2025-10-18T22:00Z"
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


def _parse_resolution(s):
    if s == "PT15M":
        return timedelta(minutes=15)
    if s == "PT60M":
        return timedelta(hours=1)
    raise EntsoeError(f"Unsupported resolution {s}")


def parse_prices(xml_text):
    """Parse an A44 publication document into {quarter_start_utc: price}.

    Handles PT15M and PT60M periods (hourly prices are spread over 4 quarters)
    and curve type A03, where points with an unchanged price are omitted.
    15-minute data wins over hourly data when both are present.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise EntsoeError(f"Invalid XML: {e}") from e

    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = next(_iter(root, "text"), None)
        raise EntsoeError(reason.text if reason is not None else "No data available")

    by_resolution = {}
    for period in _iter(root, "Period"):
        interval = _child(period, "timeInterval")
        start = _parse_dt(_child(interval, "start").text)
        end = _parse_dt(_child(interval, "end").text)
        resolution = _parse_resolution(_child(period, "resolution").text)

        points = {}
        for point in period:
            if _local(point.tag) != "Point":
                continue
            pos = int(_child(point, "position").text)
            points[pos] = float(_child(point, "price.amount").text)
        if not points:
            continue

        n_slots = int((end - start) / resolution)
        current = None
        target = by_resolution.setdefault(resolution, {})
        for pos in range(1, n_slots + 1):
            current = points.get(pos, current)
            if current is None:
                continue
            slot_start = start + (pos - 1) * resolution
            t = slot_start
            while t < slot_start + resolution:
                target[t] = current
                t += QUARTER

    result = {}
    for resolution in sorted(by_resolution, reverse=True):  # coarse first, fine overrides
        result.update(by_resolution[resolution])
    return result


def fetch_from_api(start_utc, end_utc):
    if not settings.ENTSOE_TOKEN:
        raise EntsoeError("ENTSOE_TOKEN is not configured")
    params = {
        "securityToken": settings.ENTSOE_TOKEN,
        "documentType": "A44",
        "in_Domain": settings.ENTSOE_AREA,
        "out_Domain": settings.ENTSOE_AREA,
        "periodStart": start_utc.astimezone(UTC).strftime("%Y%m%d%H%M"),
        "periodEnd": end_utc.astimezone(UTC).strftime("%Y%m%d%H%M"),
    }
    try:
        resp = requests.get(settings.ENTSOE_API_URL, params=params, timeout=60)
    except requests.RequestException as e:
        raise EntsoeError(f"Request failed: {e}") from e
    # ENTSO-E returns an acknowledgement document (often with HTTP 400) when
    # there's no data; parse_prices turns that into an EntsoeError.
    return parse_prices(resp.text)


def store_prices(prices):
    objs = [PriceInterval(start=start, price=price) for start, price in prices.items()]
    PriceInterval.objects.bulk_create(
        objs, update_conflicts=True, unique_fields=["start"], update_fields=["price"]
    )
    return len(objs)


def has_prices_for(day):
    start, end = day_bounds(day)
    expected = int((end - start) / QUARTER)
    return PriceInterval.objects.filter(start__gte=start, start__lt=end).count() >= expected


def ensure_prices_for(day, force=False):
    """Make sure prices for the given local date are in the database.

    Returns True when the day is complete, False when the data isn't available.
    """
    if not force and has_prices_for(day):
        return True
    cache_key = f"entsoe-miss-{day.isoformat()}"
    if not force and cache.get(cache_key):
        return False

    start, end = day_bounds(day)
    try:
        prices = fetch_from_api(start, end)
    except EntsoeError as e:
        logger.info("No ENTSO-E prices for %s: %s", day, e)
        cache.set(cache_key, True, RETRY_AFTER)
        return False

    prices = {t: p for t, p in prices.items() if start <= t < end}
    store_prices(prices)
    complete = has_prices_for(day)
    if not complete:
        cache.set(cache_key, True, RETRY_AFTER)
    return complete
