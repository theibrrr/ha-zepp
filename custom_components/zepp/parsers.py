"""Pure parsing helpers for Zepp cloud payloads.

Nothing in this module talks to the network or to Home Assistant, so every
function can be unit tested with recorded API responses.

Notes on the payloads (verified against a live Amazfit Bip Max account):
* ``/users/{id}/events`` and ``/v2/users/me/events`` return items oldest-first
  unless ``reverse=1`` is sent. We request newest-first AND sort client side.
* v1 user events carry their numbers as strings ("34"), v2 events wrap the
  payload in a ``value`` object.
* ``255`` (and ``32767`` for temperatures) are "not measured" sentinels.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any

SENTINELS = {255, 32767, -1}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def num(value: Any) -> float | None:
    """Return value as float, or None for missing / non numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def num_int(value: Any) -> int | None:
    v = num(value)
    return None if v is None else int(round(v))


def score(value: Any, lo: float = 0, hi: float = 100) -> int | None:
    """A 0..100 style score with sentinel filtering."""
    v = num(value)
    if v is None or int(v) in SENTINELS or not lo <= v <= hi:
        return None
    return int(round(v))


def maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def ts_of(item: dict[str, Any]) -> float:
    """Event timestamp in ms (0 if missing)."""
    return num(item.get("timestamp")) or 0.0


def newest_first(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return sorted((i for i in (items or []) if isinstance(i, dict)), key=ts_of, reverse=True)


def ms_to_dt(ms: float | None) -> dt.datetime | None:
    if ms is None:
        return None
    if ms > 1e12:
        ms /= 1000.0
    try:
        return dt.datetime.fromtimestamp(ms, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def decode_summary(summary_b64: str | None) -> dict[str, Any]:
    if not summary_b64:
        return {}
    try:
        return json.loads(base64.b64decode(summary_b64).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _b64(data: str | None) -> bytes:
    if not data:
        return b""
    try:
        return base64.b64decode(data)
    except Exception:  # noqa: BLE001
        return b""


# --------------------------------------------------------------------------
# band_data (steps, heart rate, sleep)
# --------------------------------------------------------------------------
def parse_activity_today(band_items: list[dict[str, Any]], today: str) -> dict[str, Any] | None:
    """Steps / distance / calories for ``today`` (YYYY-MM-DD). None if the day has not synced yet."""
    item = next((i for i in band_items if i.get("date_time") == today), None)
    if item is None:
        return None
    summary = decode_summary(item.get("summary"))
    stp = summary.get("stp") or {}
    out = {
        "steps": int(num(stp.get("ttl")) or 0),
        "distance": int(num(stp.get("dis")) or 0),
        "calories": int(num(stp.get("cal")) or 0),
    }
    if not out["steps"]:
        raw = _b64(item.get("data"))
        minutes = len(raw) // 3
        minute_steps = sum(raw[m * 3 + 2] for m in range(min(1440, minutes)))
        if minute_steps:
            out["steps"] = minute_steps
    return out


def parse_step_goal(band_items: list[dict[str, Any]]) -> int | None:
    for item in sorted(band_items, key=lambda i: i.get("date_time") or "", reverse=True):
        goal = num(decode_summary(item.get("summary")).get("goal"))
        if goal:
            return int(goal)
    return None


def parse_heart_rate(band_items: list[dict[str, Any]], tz: dt.tzinfo) -> dict[str, Any] | None:
    """Latest valid minute heart rate plus the min / max / avg of that day."""
    for item in sorted(band_items, key=lambda i: i.get("date_time") or "", reverse=True):
        raw = _b64(item.get("data_hr"))
        valid = [(minute, bpm) for minute, bpm in enumerate(raw) if 20 <= bpm <= 240]
        if not valid:
            continue
        values = [bpm for _, bpm in valid]
        last_minute, last_bpm = valid[-1]
        measured_at = None
        try:
            day = dt.datetime.strptime(item["date_time"], "%Y-%m-%d").replace(tzinfo=tz)
            measured_at = day + dt.timedelta(minutes=last_minute)
        except (KeyError, ValueError):
            pass
        return {
            "heart_rate": last_bpm,
            "hr_min": min(values),
            "hr_max": max(values),
            "hr_avg": round(sum(values) / len(values), 1),
            "hr_measured_at": measured_at.isoformat() if measured_at else None,
        }
    return None


def parse_sleep(band_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most recent night that actually contains sleep (empty day records are skipped)."""
    for item in sorted(band_items, key=lambda i: i.get("date_time") or "", reverse=True):
        slp = decode_summary(item.get("summary")).get("slp") or {}
        deep = int(num(slp.get("dp")) or 0)
        light = int(num(slp.get("lt")) or 0)
        rem = int(num(slp.get("dt")) or 0)
        if deep + light + rem <= 0:
            continue
        start = num(slp.get("st"))
        end = num(slp.get("ed"))
        rhr = num_int(slp.get("rhr"))
        return {
            "sleep_date": item.get("date_time"),
            "sleep_score": score(slp.get("ss")),
            "deep_sleep": deep,
            "light_sleep": light,
            "rem_sleep": rem,
            "awake_time": num_int(slp.get("wk")),
            "wake_count": num_int(slp.get("wc")),
            "sleep_duration": deep + light + rem,
            "sleep_rhr": rhr if rhr else None,
            "sleep_start": ms_to_dt(start).isoformat() if start else None,
            "sleep_end": ms_to_dt(end).isoformat() if end else None,
            "_sleep_start_s": start,
            "_sleep_end_s": end,
        }
    return None


# --------------------------------------------------------------------------
# stress
# --------------------------------------------------------------------------
def parse_stress(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest 5-minute stress reading plus that day's roll-up."""
    for item in newest_first(items):
        points = maybe_json(item.get("data"))
        latest = None
        if isinstance(points, list):
            readings = [
                p for p in points
                if isinstance(p, dict) and (num(p.get("value")) or 0) >= 1 and num(p.get("time"))
            ]
            if readings:
                latest = max(readings, key=lambda p: num(p.get("time")) or 0)
        avg = num(item.get("avgStress"))
        if latest is None and not avg:
            continue
        return {
            "stress": num_int(latest.get("value")) if latest else num_int(avg),
            "stress_measured_at": ms_to_dt(num(latest.get("time"))).isoformat() if latest else None,
            "stress_avg": num_int(avg),
            "stress_min": num_int(item.get("minStress")),
            "stress_max": num_int(item.get("maxStress")),
            "stress_relaxed_pct": num_int(item.get("relaxProportion")),
            "stress_normal_pct": num_int(item.get("normalProportion")),
            "stress_medium_pct": num_int(item.get("mediumProportion")),
            "stress_high_pct": num_int(item.get("highProportion")),
            "stress_day": ms_to_dt(ts_of(item)).isoformat() if ts_of(item) else None,
        }
    return None


# --------------------------------------------------------------------------
# blood oxygen
# --------------------------------------------------------------------------
def parse_spo2_spot(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest spot / automatic SpO2 reading (subType click and similar).

    ``odi`` and ``osa_event`` items are nightly summaries / apnea dips, not a
    saturation reading, and are ignored here.
    """
    for item in newest_first(items):
        if item.get("subType") in ("odi", "osa_event"):
            continue
        extra = maybe_json(item.get("extra"))
        source = extra if isinstance(extra, dict) else item
        value = source.get("spo2", source.get("value"))
        if isinstance(value, list) and value:
            value = value[-1].get("value") if isinstance(value[-1], dict) else value[-1]
        v = num(value)
        if v is None or not 50 <= v <= 100:
            continue
        when = num(source.get("timestamp")) or ts_of(item)
        return {"spo2": int(round(v)), "spo2_measured_at": ms_to_dt(when).isoformat() if when else None}
    return None


def parse_odi(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Nightly breathing quality (blood_oxygen / odi from the dateString endpoint)."""
    for item in newest_first(items):
        if item.get("subType") not in (None, "odi"):
            continue
        night_score = score(item.get("score"))
        odi = num(item.get("odi"))
        if night_score is None and odi is None:
            continue
        cost = num(item.get("cost"))
        return {
            "breathing_score": night_score,
            "odi": odi,
            "odi_events": num_int(item.get("odiNum")),
            "spo2_measured_minutes": int(round(cost / 60)) if cost else None,
            "odi_night": ms_to_dt(ts_of(item)).isoformat() if ts_of(item) else None,
        }
    return None


# --------------------------------------------------------------------------
# PAI
# --------------------------------------------------------------------------
def parse_pai(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in newest_first(items):
        total = num(item.get("totalPai"))
        if total is None:
            continue
        return {
            "total_pai": round(total, 1),
            "daily_pai": round(num(item.get("dailyPai")) or 0.0, 1),
            "pai_rest_hr": num_int(item.get("restHr")),
            "pai_low_zone_minutes": num_int(item.get("lowZoneMinutes")),
            "pai_medium_zone_minutes": num_int(item.get("mediumZoneMinutes")),
            "pai_high_zone_minutes": num_int(item.get("highZoneMinutes")),
        }
    return None


# --------------------------------------------------------------------------
# HRV (v2 HRVRMSSD / real_data)
# --------------------------------------------------------------------------
def parse_hrv(
    items: list[dict[str, Any]],
    sleep_start_s: float | None = None,
    sleep_end_s: float | None = None,
) -> dict[str, Any] | None:
    """Average overnight rMSSD.

    Zepp buckets v2 events by UTC day, so one night can be split over two
    items. All samples are put on an absolute time line and, when the sleep
    window is known, only samples inside it are averaged.
    """
    samples: list[tuple[float, float]] = []
    for item in newest_first(items)[:3]:
        value = item.get("value") or {}
        start = num(value.get("startTime")) or ts_of(item)
        for s in value.get("samples") or []:
            hrv = num(s.get("hrv"))
            if hrv is None or hrv <= 0 or hrv >= 255:
                continue
            samples.append((start + (num(s.get("s")) or 0), hrv))
    if not samples:
        return None
    samples.sort()
    window = samples
    if sleep_start_s and sleep_end_s:
        lo, hi = (sleep_start_s - 1800) * 1000, (sleep_end_s + 1800) * 1000
        window = [s for s in samples if lo <= s[0] <= hi] or []
    if not window:
        # Sleep window unknown or no overlap: use the newest 10 hours of samples.
        last = samples[-1][0]
        window = [s for s in samples if s[0] >= last - 10 * 3600 * 1000]
    values = [v for _, v in window]
    return {
        "hrv": round(sum(values) / len(values), 1),
        "hrv_last": window[-1][1],
        "hrv_last_at": ms_to_dt(window[-1][0]).isoformat(),
        "hrv_min": min(values),
        "hrv_max": max(values),
        "hrv_samples": len(values),
    }


# --------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------
def parse_readiness(v2_items: list[dict[str, Any]], v1_items: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Readiness ``watch_score``. v2 wraps it in ``value``, v1 is flat with strings."""
    candidates: list[dict[str, Any]] = []
    for item in newest_first(v2_items):
        if item.get("subType") in (None, "watch_score") and isinstance(item.get("value"), dict):
            candidates.append({**item["value"], "_ts": ts_of(item)})
    for item in newest_first(v1_items):
        if item.get("subType") == "watch_score":
            candidates.append({**item, "_ts": num(item.get("timestampUpdate")) or ts_of(item)})
    candidates.sort(key=lambda c: c["_ts"], reverse=True)
    for c in candidates:
        rdns = score(c.get("rdnsScore"))
        if rdns is None:
            continue
        return {
            "readiness_score": rdns,
            "readiness_hrv_score": score(c.get("hrvScore")),
            "readiness_rhr_score": score(c.get("rhrScore")),
            "readiness_physical": score(c.get("phyScore")),
            "readiness_mental": score(c.get("mentScore")),
            "readiness_sleep_hrv": score(c.get("sleepHRV"), 1, 250),
            "readiness_sleep_rhr": score(c.get("sleepRHR"), 20, 200),
            "readiness_ahi_score": score(c.get("ahiScore")),
            "readiness_updated_at": ms_to_dt(c["_ts"]).isoformat() if c["_ts"] else None,
        }
    return None


# --------------------------------------------------------------------------
# training load, weight
# --------------------------------------------------------------------------
def parse_sport_load(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = sorted((i for i in items or [] if isinstance(i, dict)), key=lambda i: i.get("dayId") or "", reverse=True)
    if not rows:
        return None
    r = rows[0]
    return {
        "training_load_total": num_int(r.get("wtlSum")),
        "training_load_today": num_int(r.get("currnetDayTrainLoad", r.get("currentDayTrainLoad"))),
        "training_load_min": num_int(r.get("wtlSumOptimalMin")),
        "training_load_max": num_int(r.get("wtlSumOptimalMax")),
        "training_load_overreaching": num_int(r.get("wtlSumOverreaching")),
    }


def parse_weight(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Weight records keep their measurements inside a ``summary`` JSON object."""
    def when(i: dict[str, Any]) -> float:
        return num(i.get("generatedTime")) or num(i.get("createTime")) or 0.0

    for item in sorted((i for i in items or [] if isinstance(i, dict)), key=when, reverse=True):
        s = maybe_json(item.get("summary"))
        if not isinstance(s, dict):
            s = item
        weight = num(s.get("weight"))
        if not weight:
            continue
        def first(*keys: str) -> float | None:
            for k in keys:
                v = num(s.get(k))
                if v is not None and v > 0:
                    return round(v, 2)
            return None
        return {
            "weight": round(weight, 2),
            "bmi": first("bmi"),
            "body_fat": first("fatRate", "body_fat_rate", "bodyFat"),
            "muscle_mass": first("muscleRate", "muscle_mass", "muscleMass"),
            "body_water": first("bodyWaterRate", "body_water_rate", "waterRate"),
            "bone_mass": first("boneMass", "bone_mass"),
            "weight_measured_at": ms_to_dt(when(item)).isoformat() if when(item) else None,
        }
    return None
