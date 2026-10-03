"""Unit tests for the pure builders in custom_components/zepp/statistics.py."""
import base64
import datetime as dt
import importlib
import json
import sys
import types
from pathlib import Path

_pkg = types.ModuleType("zepp_under_test")
_pkg.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "zepp")]
sys.modules.setdefault("zepp_under_test", _pkg)
ST = importlib.import_module("zepp_under_test.statistics")

TZ = dt.timezone(dt.timedelta(hours=2))


def _band(date, steps_per_minute=None, hr=None, total=None):
    raw = bytearray(1440 * 3)
    for minute, steps in (steps_per_minute or {}).items():
        raw[minute * 3 + 2] = steps
    summary = {"stp": {"ttl": total if total is not None else sum((steps_per_minute or {}).values()),
                       "dis": 1000, "cal": 50}, "slp": {"dp": 60, "lt": 200, "dt": 80, "ss": 75, "rhr": 58}}
    item = {"date_time": date, "summary": base64.b64encode(json.dumps(summary).encode()).decode(),
            "data": base64.b64encode(bytes(raw)).decode()}
    if hr:
        item["data_hr"] = base64.b64encode(bytes(hr)).decode()
    return item


def test_hourly_mean_rows_group_by_local_hour():
    base = dt.datetime(2026, 10, 3, 10, 0, tzinfo=TZ).timestamp() * 1000
    rows = ST.hourly_mean_rows([(base, 10), (base + 30 * 60000, 20), (base + 61 * 60000, 40)], TZ)
    assert [r["start"].hour for r in rows] == [10, 11]
    assert rows[0]["mean"] == 15 and rows[0]["min"] == 10 and rows[0]["max"] == 20


def test_steps_sum_rows_continue_from_base_and_reset_state_daily():
    items = [_band("2026-10-02", {600: 100, 1430: 50}), _band("2026-10-03", {5: 30})]
    activity = ST.band_points(items, TZ)["activity"]["steps"]
    now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=TZ)
    rows = ST.sum_rows(activity, base=1000, now=now)
    by_start = {r["start"]: r for r in rows}
    assert by_start[dt.datetime(2026, 10, 2, 23, tzinfo=TZ)]["sum"] == 1150
    assert by_start[dt.datetime(2026, 10, 3, 0, tzinfo=TZ)]["state"] == 30   # new day
    assert by_start[dt.datetime(2026, 10, 3, 0, tzinfo=TZ)]["sum"] == 1180
    assert max(by_start) <= now                                               # no future hours


def test_heart_rate_points_use_local_midnight_of_record():
    hr = [0xFF] * 600 + [70] + [0xFF] * 839
    pts = ST.band_points([_band("2026-10-03", hr=hr)], TZ)["heart_rate"]
    assert len(pts) == 1
    assert dt.datetime.fromtimestamp(pts[0][0] / 1000, TZ).hour == 10


def test_daily_rows_sleep_pai_readiness():
    midnight_ms = dt.datetime(2026, 10, 3, tzinfo=TZ).timestamp() * 1000
    rows = ST.daily_rows(
        [_band("2026-10-03")],
        [{"timestamp": midnight_ms, "totalPai": "18.08"}],
        [{"subType": "watch_score", "timestamp": midnight_ms + 3600_000,
          "value": {"timestamp": midnight_ms, "rdnsScore": 90, "sleepHRV": 60}}],
        [{"subType": "odi", "timestamp": midnight_ms, "score": "100"}],
        TZ,
    )
    assert rows["sleep_score"][0]["mean"] == 75 and rows["sleep_duration"][0]["mean"] == 340
    assert rows["pai"][0]["mean"] == 18.1
    assert rows["readiness"][0]["mean"] == 90 and rows["hrv_night"][0]["mean"] == 60
    assert rows["breathing_score"][0]["mean"] == 100
    assert rows["readiness"][0]["start"] == dt.datetime(2026, 10, 3, tzinfo=TZ)


def test_statistic_ids_are_valid():
    import re
    for key in ST.METRICS:
        assert re.fullmatch(r"[a-z0-9_]+:[a-z0-9_]+", ST.statistic_id(key))
