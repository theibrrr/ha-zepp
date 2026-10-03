"""Unit tests for custom_components/zepp/parsers.py (no Home Assistant needed).

Run with:  python -m pytest tests
The payloads below are synthetic, shaped like real Zepp cloud responses.
"""
import base64
import datetime as dt
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "zepp_parsers", Path(__file__).parents[1] / "custom_components" / "zepp" / "parsers.py"
)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

TZ = dt.timezone(dt.timedelta(hours=2))


def _summary(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _band_day(date, slp=None, steps=0, hr=None):
    item = {"date_time": date, "summary": _summary({"stp": {"ttl": steps, "dis": steps // 2, "cal": 10}, "slp": slp or {}, "goal": 8000})}
    if hr is not None:
        item["data_hr"] = base64.b64encode(bytes(hr)).decode()
    return item


def test_sleep_skips_empty_day_and_takes_newest_real_night():
    band = [
        _band_day("2026-10-01", {"dp": 0, "lt": 0, "dt": 0, "ss": 0}),
        _band_day("2026-10-02", {"dp": 73, "lt": 262, "dt": 115, "wk": 6, "wc": 4, "ss": 89, "rhr": 63,
                                 "st": 1790890200, "ed": 1790917560}),
        _band_day("2026-10-03", {"dp": 0, "lt": 0, "dt": 0, "ss": 0}),  # today, not slept yet
    ]
    s = P.parse_sleep(band)
    assert s["sleep_score"] == 89
    assert s["sleep_duration"] == 450
    assert s["sleep_start"].startswith("2026-10-01T21:30")


def test_heart_rate_last_valid_minute():
    hr = [0xFF] * 60 + [70] * 10 + [0xFE] * 5
    out = P.parse_heart_rate([_band_day("2026-10-03", hr=hr)], TZ)
    assert out["heart_rate"] == 70
    assert out["hr_measured_at"].startswith("2026-10-03T01:09")


def test_activity_today_missing_returns_none():
    assert P.parse_activity_today([_band_day("2026-10-02", steps=500)], "2026-10-03") is None
    assert P.parse_activity_today([_band_day("2026-10-03", steps=500)], "2026-10-03")["steps"] == 500


def test_stress_uses_newest_reading_not_daily_average():
    items = [
        {"timestamp": 1790892000001, "avgStress": "36", "minStress": "5", "maxStress": "66",
         "data": json.dumps([{"time": 1790971200000, "value": 55}])},
        {"timestamp": 1790978400001, "avgStress": "34", "minStress": "7", "maxStress": "65",
         "relaxProportion": "55", "data": json.dumps([{"time": 1790978400000, "value": 30},
                                                      {"time": 1791035100000, "value": 45}])},
    ]
    out = P.parse_stress(items)  # deliberately oldest-first, like the API default
    assert out["stress"] == 45
    assert out["stress_avg"] == 34
    assert out["stress_relaxed_pct"] == 55


def test_spo2_spot_reading_scalar_extra_and_odi_ignored():
    items = [
        {"subType": "odi", "timestamp": 2, "score": "100", "odi": "0"},
        {"subType": "click", "timestamp": 1, "extra": json.dumps({"spo2": 97})},
    ]
    assert P.parse_spo2_spot(items)["spo2"] == 97
    assert P.parse_spo2_spot([items[0]]) is None


def test_odi_nightly_summary():
    out = P.parse_odi([{"subType": "odi", "timestamp": 1790978400000, "score": "100", "odi": "0.13",
                        "odiNum": "1", "cost": "27360"}])
    assert out == {**out, "breathing_score": 100, "odi": 0.13, "odi_events": 1, "spo2_measured_minutes": 456}


def test_pai_newest():
    items = [{"timestamp": 1, "totalPai": "16.35"}, {"timestamp": 2, "totalPai": "18.08", "dailyPai": "1.7"}]
    assert P.parse_pai(items)["total_pai"] == 18.1


def test_readiness_v2_value_envelope_and_sentinels():
    items = [{"subType": "watch_score", "timestamp": 1791004680000,
              "value": {"rdnsScore": 90, "hrvScore": 88, "phyScore": 255, "sleepHRV": 60}}]
    out = P.parse_readiness(items)
    assert out["readiness_score"] == 90
    assert out["readiness_physical"] is None
    assert out["readiness_sleep_hrv"] == 60


def test_readiness_v1_flat_strings():
    items = [{"subType": "watch_score_data", "timestamp": "5"},
             {"subType": "watch_score", "timestamp": "1", "timestampUpdate": "2", "rdnsScore": "91"}]
    assert P.parse_readiness([], items)["readiness_score"] == 91


def test_hrv_night_average_across_two_utc_buckets():
    start1 = 1790892000000  # bucket 1
    start2 = 1790978400000  # bucket 2
    items = [
        {"timestamp": start1, "value": {"startTime": start1, "samples": [{"s": 86_000_000, "hrv": 50}]}},
        {"timestamp": start2, "value": {"startTime": start2, "samples": [{"s": 0, "hrv": 60}, {"s": 60000, "hrv": 70}]}},
    ]
    out = P.parse_hrv(items, sleep_start_s=start1 / 1000 + 85_000, sleep_end_s=start2 / 1000 + 3600)
    assert out["hrv"] == 60.0
    assert out["hrv_samples"] == 3


def test_weight_from_summary():
    items = [{"generatedTime": 1790871211, "summary": json.dumps({"weight": 82.0, "bmi": 24.5})}]
    out = P.parse_weight(items)
    assert out["weight"] == 82.0 and out["bmi"] == 24.5 and out["body_fat"] is None


def test_sport_load_newest_day():
    items = [{"dayId": "2026-10-01", "wtlSum": 5}, {"dayId": "2026-10-03", "wtlSum": 0, "currnetDayTrainLoad": 0,
                                                   "wtlSumOptimalMin": 54, "wtlSumOptimalMax": 182}]
    out = P.parse_sport_load(items)
    assert out["training_load_total"] == 0 and out["training_load_min"] == 54
