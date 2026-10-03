"""Long-term statistics for Zepp data (external statistics, ``zepp:*``).

Why external statistics?
  Home Assistant compiles its own statistics for every sensor from the states
  it sees every poll. Writing into those statistic ids (as versions <= 1.2
  did for steps / distance / calories) mixes two sources and can corrupt the
  running ``sum`` of total-increasing sensors. Separate ``zepp:<metric>``
  statistics never collide with what Home Assistant records itself.

What is written:
  hourly mean / min / max  - heart rate (per minute), stress (per 5 min),
                             BioCharge (per minute), HRV (overnight),
                             respiratory rate (overnight)
  hourly sum               - steps, distance, calories
  daily value at midnight  - sleep score / duration / stages, sleep resting HR,
                             PAI, readiness, overnight HRV, breathing quality

The builders below are pure functions (unit tested); the ``async_*`` helpers
talk to the recorder.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .parsers import (
    _b64,
    _charge_samples,
    decode_summary,
    maybe_json,
    num,
    score,
    ts_of,
)

_LOGGER = logging.getLogger(__name__)

SOURCE = "zepp"

# statistic key -> (name, unit, kind) ; kind: "mean" | "sum"
METRICS: dict[str, tuple[str, str | None, str]] = {
    "heart_rate": ("Zepp heart rate", "bpm", "mean"),
    "stress": ("Zepp stress", None, "mean"),
    "biocharge": ("Zepp BioCharge", None, "mean"),
    "hrv": ("Zepp HRV", "ms", "mean"),
    "respiratory_rate": ("Zepp respiratory rate", "breaths/min", "mean"),
    "steps": ("Zepp steps", "steps", "sum"),
    "distance": ("Zepp distance", "m", "sum"),
    "calories": ("Zepp calories", "kcal", "sum"),
    "sleep_score": ("Zepp sleep score", None, "mean"),
    "sleep_duration": ("Zepp sleep duration", "min", "mean"),
    "deep_sleep": ("Zepp deep sleep", "min", "mean"),
    "light_sleep": ("Zepp light sleep", "min", "mean"),
    "rem_sleep": ("Zepp REM sleep", "min", "mean"),
    "awake_time": ("Zepp awake time", "min", "mean"),
    "sleep_resting_hr": ("Zepp sleep resting heart rate", "bpm", "mean"),
    "pai": ("Zepp PAI", "PAI", "mean"),
    "readiness": ("Zepp readiness", None, "mean"),
    "hrv_night": ("Zepp overnight HRV", "ms", "mean"),
    "breathing_score": ("Zepp breathing quality", None, "mean"),
}

Row = dict[str, Any]


def statistic_id(key: str) -> str:
    return f"{SOURCE}:{key}"


# --------------------------------------------------------------------------
# pure builders
# --------------------------------------------------------------------------
def _hour_start(ms: float, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz).replace(minute=0, second=0, microsecond=0)


def _day_start(day: str, tz: dt.tzinfo) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    except (TypeError, ValueError):
        return None


def hourly_mean_rows(points: Iterable[tuple[float, float]], tz: dt.tzinfo) -> list[Row]:
    """(time ms, value) points -> one mean/min/max row per local hour."""
    buckets: dict[dt.datetime, list[float]] = defaultdict(list)
    for t, v in points:
        buckets[_hour_start(t, tz)].append(float(v))
    rows = []
    for start in sorted(buckets):
        vals = buckets[start]
        mean = round(sum(vals) / len(vals), 2)
        rows.append({"start": start, "mean": mean, "min": min(vals), "max": max(vals), "state": vals[-1]})
    return rows


def band_points(band_items: list[dict[str, Any]], tz: dt.tzinfo) -> dict[str, Any]:
    """Minute heart rate points and per-hour step / distance / calorie amounts."""
    hr: list[tuple[float, float]] = []
    activity: dict[str, dict[dt.datetime, float]] = {"steps": {}, "distance": {}, "calories": {}}
    for item in band_items or []:
        day = _day_start(item.get("date_time"), tz)
        if day is None:
            continue
        base_ms = day.timestamp() * 1000
        for minute, bpm in enumerate(_b64(item.get("data_hr"))[:1440]):
            if 20 <= bpm <= 240:
                hr.append((base_ms + minute * 60000, bpm))

        raw = _b64(item.get("data"))
        minutes = min(1440, len(raw) // 3)
        stp = decode_summary(item.get("summary")).get("stp") or {}
        total_steps = num(stp.get("ttl")) or 0
        total_dis = num(stp.get("dis")) or 0
        total_cal = num(stp.get("cal")) or 0
        hourly = [0.0] * 24
        for m in range(minutes):
            hourly[m // 60] += raw[m * 3 + 2]
        if not minutes and total_steps:
            hourly[12] = total_steps  # no minute data: put the day total at noon
        minute_total = sum(hourly) or 0
        for h in range(24):
            start = day + dt.timedelta(hours=h)
            share = hourly[h] / minute_total if minute_total else 0
            activity["steps"][start] = hourly[h]
            activity["distance"][start] = round(total_dis * share, 1)
            activity["calories"][start] = round(total_cal * share, 2)
    return {"heart_rate": hr, "activity": activity}


def sum_rows(amounts: dict[dt.datetime, float], base: float, now: dt.datetime) -> list[Row]:
    """Hourly amounts -> rows with a running ``sum`` starting at ``base``.

    ``state`` is the running total of the local day (resets at midnight), which
    is what a "today" counter shows. Hours in the future are skipped.
    """
    rows = []
    running = base
    day_total = 0.0
    current_day = None
    for start in sorted(amounts):
        if start > now:
            break
        if start.date() != current_day:
            current_day = start.date()
            day_total = 0.0
        day_total += amounts[start]
        running += amounts[start]
        rows.append({"start": start, "state": round(day_total, 2), "sum": round(running, 2)})
    return rows


def stress_points(items: list[dict[str, Any]]) -> list[tuple[float, float]]:
    out = []
    for item in items or []:
        points = maybe_json(item.get("data"))
        if not isinstance(points, list):
            continue
        for p in points:
            if isinstance(p, dict):
                t, v = num(p.get("time")), num(p.get("value"))
                if t and v is not None and 1 <= v <= 100:
                    out.append((t, v))
    return out


def charge_points(items: list[dict[str, Any]]) -> list[tuple[float, float]]:
    return [(s["t"], s["total"]) for s in _charge_samples(items)]


def hrv_points(items: list[dict[str, Any]]) -> list[tuple[float, float]]:
    out = []
    for item in items or []:
        value = item.get("value") or {}
        start = num(value.get("startTime")) or ts_of(item)
        for s in value.get("samples") or []:
            v = num(s.get("hrv"))
            if v and 0 < v < 255:
                out.append((start + (num(s.get("s")) or 0), v))
    return out


def respiratory_points(items: list[dict[str, Any]]) -> list[tuple[float, float]]:
    out = []
    for item in items or []:
        start = ts_of(item)
        for minute, rate in enumerate(_b64((item.get("value") or {}).get("measurements"))):
            if 0 < rate < 60:
                out.append((start + minute * 60000, rate))
    return out


def _daily_row(day: dt.datetime, value: float) -> Row:
    return {"start": day, "mean": value, "min": value, "max": value, "state": value}


def daily_rows(
    band_items: list[dict[str, Any]],
    pai_items: list[dict[str, Any]],
    readiness_items: list[dict[str, Any]],
    odi_items: list[dict[str, Any]],
    tz: dt.tzinfo,
) -> dict[str, list[Row]]:
    """One value per day, stored at local midnight of that day."""
    out: dict[str, dict[dt.datetime, float]] = defaultdict(dict)

    for item in band_items or []:
        day = _day_start(item.get("date_time"), tz)
        slp = decode_summary(item.get("summary")).get("slp") or {}
        deep, light, rem = (int(num(slp.get(k)) or 0) for k in ("dp", "lt", "dt"))
        if day is None or deep + light + rem <= 0:
            continue
        out["sleep_duration"][day] = deep + light + rem
        out["deep_sleep"][day] = deep
        out["light_sleep"][day] = light
        out["rem_sleep"][day] = rem
        if num(slp.get("wk")) is not None:
            out["awake_time"][day] = num(slp.get("wk"))
        if score(slp.get("ss")) is not None:
            out["sleep_score"][day] = score(slp.get("ss"))
        if num(slp.get("rhr")):
            out["sleep_resting_hr"][day] = num(slp.get("rhr"))

    def local_day(ms: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(ms / 1000, tz).replace(hour=0, minute=0, second=0, microsecond=0)

    for item in pai_items or []:
        total = num(item.get("totalPai"))
        if total is not None and ts_of(item):
            out["pai"][local_day(ts_of(item))] = round(total, 1)

    for item in readiness_items or []:
        body = item.get("value") if isinstance(item.get("value"), dict) else item
        if item.get("subType") not in (None, "watch_score"):
            continue
        day_ms = num(body.get("timestamp")) or ts_of(item)
        if not day_ms:
            continue
        rdns = score(body.get("rdnsScore"))
        if rdns is not None:
            out["readiness"][local_day(day_ms)] = rdns
        hrv = score(body.get("sleepHRV"), 1, 250)
        if hrv is not None:
            out["hrv_night"][local_day(day_ms)] = hrv

    for item in odi_items or []:
        s = score(item.get("score"))
        if s is not None and ts_of(item):
            out["breathing_score"][local_day(ts_of(item))] = s

    return {key: [_daily_row(d, v) for d, v in sorted(days.items())] for key, days in out.items()}


def build_all(raw: dict[str, list[dict[str, Any]]], tz: dt.tzinfo, now: dt.datetime) -> dict[str, Any]:
    """Every statistic that can be built from one batch of raw cloud items.

    Returns {"mean": {key: rows}, "sum_amounts": {key: {hour: amount}}}.
    """
    band = band_points(raw.get("band", []), tz)
    mean: dict[str, list[Row]] = {
        "heart_rate": hourly_mean_rows(band["heart_rate"], tz),
        "stress": hourly_mean_rows(stress_points(raw.get("stress", [])), tz),
        "biocharge": hourly_mean_rows(charge_points(raw.get("charge", [])), tz),
        "hrv": hourly_mean_rows(hrv_points(raw.get("hrv", [])), tz),
        "respiratory_rate": hourly_mean_rows(respiratory_points(raw.get("respiratory", [])), tz),
    }
    mean.update(daily_rows(raw.get("band", []), raw.get("pai", []), raw.get("readiness", []),
                           raw.get("odi", []), tz))
    # never write rows for hours that have not started yet
    mean = {k: [r for r in rows if r["start"] <= now] for k, rows in mean.items()}
    return {"mean": mean, "sum_amounts": band["activity"]}


# --------------------------------------------------------------------------
# recorder glue
# --------------------------------------------------------------------------
def _lock(hass) -> asyncio.Lock:
    from .const import DOMAIN

    data = hass.data.setdefault(DOMAIN, {})
    if "stats_lock" not in data:
        data["stats_lock"] = asyncio.Lock()
    return data["stats_lock"]


def _metadata(key: str) -> dict[str, Any]:
    from homeassistant.components.recorder.models import StatisticMeanType

    name, unit, kind = METRICS[key]
    return {
        "mean_type": StatisticMeanType.ARITHMETIC if kind == "mean" else StatisticMeanType.NONE,
        "has_sum": kind == "sum",
        "name": name,
        "source": SOURCE,
        "statistic_id": statistic_id(key),
        "unit_class": None,
        "unit_of_measurement": unit,
    }


async def _sum_base(hass, key: str, before: dt.datetime) -> float:
    """Running sum of the last row before ``before`` (0 if there is none)."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    stat_id = statistic_id(key)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, before - dt.timedelta(days=400), before,
        {stat_id}, "hour", None, {"sum"},
    )
    series = rows.get(stat_id) or []
    for row in reversed(series):
        if row.get("sum") is not None:
            return float(row["sum"])
    return 0.0


async def async_write_statistics(hass, raw: dict[str, list[dict[str, Any]]], tz: dt.tzinfo, now: dt.datetime) -> int:
    """Build and write all ``zepp:*`` statistics for one batch of raw items.

    Rows are upserted, so the same hours can be written again when more data
    arrives (the phone often uploads hours later). Returns the number of rows.
    """
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    built = build_all(raw, tz, now)
    written = 0
    async with _lock(hass):
        for key, rows in built["mean"].items():
            if rows and key in METRICS:
                async_add_external_statistics(hass, _metadata(key), rows)
                written += len(rows)
        for key, amounts in built["sum_amounts"].items():
            if not amounts:
                continue
            first = min(amounts)
            base = await _sum_base(hass, key, first)
            rows = sum_rows(amounts, base, now)
            if rows:
                async_add_external_statistics(hass, _metadata(key), rows)
                written += len(rows)
    _LOGGER.debug("Wrote %d zepp statistics rows", written)
    return written
