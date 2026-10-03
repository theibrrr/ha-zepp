"""History import for Zepp (Amazfit) into Home Assistant long-term statistics.

Since 1.3.0 everything is written as external statistics (``zepp:*``, see
statistics.py) instead of into the statistics of the live sensors. That keeps
Home Assistant's own sensor statistics untouched and makes the import safe to
run again at any time (rows are upserted, running sums continue from the last
row before the imported period).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .api import (
    ZeppAuthError,
    async_fetch_band_data,
    async_fetch_user_events,
    async_fetch_user_events_by_date,
    async_fetch_v2_events,
)
from .const import CONF_APPTOKEN, CONF_REGION_HOST, CONF_USERID, DOMAIN
from .statistics import async_write_statistics

_LOGGER = logging.getLogger(__name__)

# Per-minute streams (BioCharge, HRV, respiratory rate) are large; they are
# imported for at most this many days.
MAX_INTRADAY_DAYS = 60

V2_STREAMS = {
    "charge": ("Charge", "real_data"),
    "hrv": ("HRVRMSSD", "real_data"),
    "respiratory": ("RespiratoryRate", "real_data"),
}


def _set_status(hass: HomeAssistant, **status: Any) -> None:
    hass.data.setdefault(DOMAIN, {})["history_sync_status"] = status
    async_dispatcher_send(hass, f"{DOMAIN}_history_sync_update")


async def async_sync_historical_data(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    days: int = 365,
) -> int:
    """Fetch up to ``days`` of history from Zepp and write it as ``zepp:*`` statistics."""
    current = hass.data.get(DOMAIN, {}).get("history_sync_status", {}).get("status")
    if current == "syncing":
        _LOGGER.info("History sync is already in progress, skipping redundant trigger")
        return 0

    session = async_get_clientsession(hass)
    userid = str(entry_data[CONF_USERID])
    api = (session, entry_data[CONF_REGION_HOST], entry_data[CONF_APPTOKEN])

    tz = dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC
    now = dt_util.now()
    start = now - datetime.timedelta(days=days)
    from_ms = int(start.timestamp() * 1000)
    to_ms = int(now.timestamp() * 1000) + 3600 * 1000

    status: dict[str, Any] = {"status": "syncing", "progress": 5, "synced_days": 0, "total_days": days,
                              "heart_rate_points": 0, "last_synced_date": None}
    _set_status(hass, **status)
    _LOGGER.info("Starting Zepp history import for %s days", days)

    raw: dict[str, list[dict[str, Any]]] = {
        k: [] for k in ("band", "stress", "pai", "readiness", "odi", "charge", "hrv", "respiratory")
    }
    try:
        # 1. Daily band records (steps, minute heart rate, sleep) in 30-day chunks
        cursor = start
        seen: set[str] = set()
        while cursor < now:
            chunk_end = min(cursor + datetime.timedelta(days=30), now)
            items = await async_fetch_band_data(
                *api, userid, cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"), query_type="detail"
            )
            for item in items:
                day = item.get("date_time")
                if day and day not in seen:
                    seen.add(day)
                    raw["band"].append(item)
            cursor = chunk_end + datetime.timedelta(days=1)
            status.update(progress=min(50, 5 + int(len(seen) / max(1, days) * 45)), synced_days=len(seen),
                          last_synced_date=max(seen) if seen else None)
            _set_status(hass, **status)
            await asyncio.sleep(0.2)

        # 2. Daily events (about one item per day each)
        limit = min(1000, days * 3 + 20)
        raw["stress"] = await async_fetch_user_events(*api, userid, "all_day_stress", from_ts=from_ms,
                                                      to_ts=to_ms, limit=limit, reverse=True)
        raw["pai"] = await async_fetch_user_events(*api, userid, "PaiHealthInfo", from_ts=from_ms,
                                                   to_ts=to_ms, limit=limit, reverse=True)
        raw["readiness"] = await async_fetch_user_events(*api, userid, "readiness", from_ts=from_ms,
                                                         to_ts=to_ms, limit=limit, reverse=True)
        raw["odi"] = await async_fetch_user_events_by_date(
            *api, userid, "blood_oxygen", "odi", start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"),
            hass.config.time_zone or "UTC", limit=999,
        )
        status.update(progress=60)
        _set_status(hass, **status)

        # 3. Per-minute v2 streams in 7-day chunks
        intraday_start = now - datetime.timedelta(days=min(days, MAX_INTRADAY_DAYS))
        total_span = max((now - intraday_start).total_seconds(), 1)
        cursor = intraday_start
        while cursor < now:
            chunk_end = min(cursor + datetime.timedelta(days=7), now)
            c_from = int(cursor.timestamp() * 1000)
            c_to = int(chunk_end.timestamp() * 1000) + 3600 * 1000
            for key, (event, sub) in V2_STREAMS.items():
                raw[key].extend(await async_fetch_v2_events(*api, event, sub_type=sub, from_ts=c_from,
                                                            to_ts=c_to, limit=20, reverse=True))
            cursor = chunk_end
            status.update(progress=60 + int((cursor - intraday_start).total_seconds() / total_span * 25))
            _set_status(hass, **status)
            await asyncio.sleep(0.2)
    except ZeppAuthError:
        _LOGGER.warning("Zepp authentication expired during history import; importing what was fetched")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Error while fetching Zepp history: %s", err)

    # v2 chunks overlap at their edges: keep one item per day bucket
    for key in V2_STREAMS:
        raw[key] = list({item.get("timestamp"): item for item in raw[key]}.values())

    rows = 0
    try:
        if any(raw.values()):
            rows = await async_write_statistics(hass, raw, tz, now)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Writing Zepp statistics failed: %s", err)

    last_day = max((i["date_time"] for i in raw["band"]), default=None)
    _set_status(hass, status="completed", progress=100, synced_days=len(raw["band"]), total_days=days,
                heart_rate_points=sum(1 for i in raw["band"] if i.get("data_hr")),
                last_synced_date=last_day, statistics_rows=rows)
    _LOGGER.info("Zepp history import finished: %d days, %d statistics rows", len(raw["band"]), rows)
    return len(raw["band"])
