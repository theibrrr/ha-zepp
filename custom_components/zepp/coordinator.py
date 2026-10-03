"""DataUpdateCoordinator for Zepp (Amazfit) integration."""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ZeppAuthError,
    async_fetch_band_data,
    async_fetch_devices,
    async_fetch_sport_load,
    async_fetch_user_events,
    async_fetch_user_events_by_date,
    async_fetch_v2_events,
    async_fetch_weight_records,
    async_login_web,
)
from .parsers import (
    parse_activity_today,
    parse_charge,
    parse_respiratory_rate,
    parse_yesterday,
    parse_heart_rate,
    parse_hrv,
    parse_odi,
    parse_pai,
    parse_readiness,
    parse_sleep,
    parse_sport_load,
    parse_spo2_spot,
    parse_step_goal,
    parse_stress,
    parse_weight,
)
from .const import (
    CONF_APPTOKEN,
    CONF_CNAME,
    CONF_COUNTRY_CODE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION_HOST,
    CONF_SCAN_INTERVAL,
    CONF_USERID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ZeppCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch all current metrics from Zepp cloud."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | dict[str, Any]) -> None:
        interval_minutes = DEFAULT_SCAN_INTERVAL
        if isinstance(entry, ConfigEntry):
            self.entry: ConfigEntry | None = entry
            self.entry_data = entry.data
            interval_minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        else:
            self.entry = None
            self.entry_data = entry

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(minutes=interval_minutes),
        )

        self.apptoken: str = self.entry_data[CONF_APPTOKEN]
        self.userid: str = str(self.entry_data[CONF_USERID])
        self.host: str = self.entry_data[CONF_REGION_HOST]
        self.devices: list[dict[str, Any]] = self.entry_data.get("devices", [])

    async def _async_refresh_token(self) -> bool:
        """Attempt to re-authenticate using stored credentials."""
        if not self.entry:
            return False

        password = self.entry.data.get(CONF_PASSWORD)
        email = self.entry.data.get(CONF_EMAIL)
        country_code = self.entry.data.get(CONF_COUNTRY_CODE, "AUTO")

        if not password or not email:
            return False

        _LOGGER.info("Zepp token expired, attempting automatic re-authentication for %s", email)
        session = async_get_clientsession(self.hass)
        try:
            auth_data = await async_login_web(session, email, password, country_code=country_code)
            new_token = auth_data["apptoken"]
            self.apptoken = new_token
            new_data = {**self.entry.data, CONF_APPTOKEN: new_token}
            if auth_data.get("cname"):
                new_data[CONF_CNAME] = auth_data["cname"]
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)
            self.entry_data = new_data
            _LOGGER.info("Successfully refreshed Zepp token for %s", email)
            return True
        except Exception as err:
            _LOGGER.warning("Automatic re-authentication failed for %s: %s", email, err)
            return False

    # Keys that belong to each data section. When a section returns nothing
    # (network hiccup, or the cloud simply has no newer record) the previous
    # values are kept; when it returns data, the values are always replaced.
    SECTION_KEYS: dict[str, tuple[str, ...]] = {
        "heart_rate": ("heart_rate", "hr_min", "hr_max", "hr_avg", "hr_measured_at"),
        "sleep": (
            "sleep_date", "sleep_score", "deep_sleep", "light_sleep", "rem_sleep", "awake_time",
            "wake_count", "sleep_duration", "sleep_rhr", "sleep_start", "sleep_end",
        ),
        "stress": (
            "stress", "stress_measured_at", "stress_avg", "stress_min", "stress_max",
            "stress_relaxed_pct", "stress_normal_pct", "stress_medium_pct", "stress_high_pct", "stress_day",
        ),
        "spo2": ("spo2", "spo2_measured_at"),
        "odi": ("odi", "odi_events", "spo2_measured_minutes", "odi_night"),
        "pai": (
            "total_pai", "daily_pai", "pai_rest_hr", "pai_low_zone_minutes",
            "pai_medium_zone_minutes", "pai_high_zone_minutes",
        ),
        "hrv": ("hrv", "hrv_last", "hrv_last_at", "hrv_min", "hrv_max", "hrv_samples"),
        "readiness": (
            "readiness_score", "readiness_hrv_score", "readiness_rhr_score", "readiness_physical",
            "readiness_mental", "readiness_sleep_hrv", "readiness_sleep_rhr", "readiness_ahi_score",
            "readiness_updated_at",
        ),
        "training_load": (
            "training_load_total", "training_load_today", "training_load_min",
            "training_load_max", "training_load_overreaching",
        ),
        "weight": ("weight", "bmi", "body_fat", "muscle_mass", "body_water", "bone_mass", "weight_measured_at"),
        "biocharge": (
            "biocharge", "biocharge_physical", "biocharge_mental", "biocharge_measured_at",
            "biocharge_today_min", "biocharge_today_max",
        ),
        "respiratory": (
            "respiratory_rate", "respiratory_rate_min", "respiratory_rate_max", "respiratory_rate_minutes",
        ),
        "yesterday": ("yesterday",),
    }

    def _apply(self, result: dict[str, Any], section: str, values: dict[str, Any] | None) -> None:
        """Replace a section's values, or keep the previous ones when nothing new arrived."""
        prev = self.data or {}
        for key in self.SECTION_KEYS[section]:
            if values is not None:
                result[key] = values.get(key)
            else:
                result[key] = prev.get(key)

    async def _fetch_metrics(self) -> dict[str, Any]:
        """Fetch current metrics from the Zepp cloud.

        Every value is recomputed on every poll from the newest record the
        cloud has (older versions kept the first value they ever saw).
        """
        session = async_get_clientsession(self.hass)
        api = (session, self.host, self.apptoken)
        now = dt_util.now()
        tz = dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC
        today_str = now.strftime("%Y-%m-%d")
        now_ms = int(now.timestamp() * 1000)
        to_ms = now_ms + 3600 * 1000

        def days_ago_ms(days: int) -> int:
            return int((now - datetime.timedelta(days=days)).timestamp() * 1000)

        def days_ago_str(days: int) -> str:
            return (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

        prev = self.data or {}
        result: dict[str, Any] = {"last_updated": now.isoformat()}

        async def guarded(name: str, coro):
            """Run one fetch; auth errors propagate, anything else is logged and yields None."""
            try:
                return await coro
            except ZeppAuthError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Zepp %s update failed: %s", name, err)
                return None

        # 1. band_data: steps, heart rate, sleep -------------------------------
        band_items = await guarded(
            "band data",
            async_fetch_band_data(*api, self.userid, days_ago_str(3), today_str, query_type="detail"),
        ) or []

        activity = parse_activity_today(band_items, today_str) if band_items else None
        if activity is not None:
            result.update(activity)
            self._last_steps_date = today_str
        elif band_items and getattr(self, "_last_steps_date", None) != today_str:
            # A new day started and the watch has not synced it yet: today is 0 so far.
            result.update({"steps": 0, "distance": 0, "calories": 0})
        else:
            for key in ("steps", "distance", "calories"):
                result[key] = prev.get(key, 0)
        result["step_goal"] = (parse_step_goal(band_items) if band_items else None) or prev.get("step_goal", 8000)

        self._apply(result, "heart_rate", parse_heart_rate(band_items, tz) if band_items else None)
        sleep = parse_sleep(band_items) if band_items else None
        self._apply(result, "sleep", sleep)

        # 2. stress ------------------------------------------------------------
        stress_items = await guarded(
            "stress",
            async_fetch_user_events(*api, self.userid, "all_day_stress",
                                    from_ts=days_ago_ms(3), to_ts=to_ms, limit=10, reverse=True),
        )
        self._apply(result, "stress", parse_stress(stress_items) if stress_items else None)

        # 3. blood oxygen: spot readings + nightly breathing summary -----------
        spo2_items = await guarded(
            "SpO2",
            async_fetch_user_events(*api, self.userid, "blood_oxygen",
                                    from_ts=days_ago_ms(7), to_ts=to_ms, limit=300, reverse=True),
        )
        self._apply(result, "spo2", parse_spo2_spot(spo2_items) if spo2_items else None)
        odi_items = await guarded(
            "breathing quality",
            async_fetch_user_events_by_date(*api, self.userid, "blood_oxygen", "odi",
                                            days_ago_str(7), today_str, self.hass.config.time_zone or "UTC"),
        )
        odi = parse_odi(odi_items) if odi_items else None
        self._apply(result, "odi", odi)

        # 4. PAI ---------------------------------------------------------------
        pai_items = await guarded(
            "PAI",
            async_fetch_user_events(*api, self.userid, "PaiHealthInfo",
                                    from_ts=days_ago_ms(3), to_ts=to_ms, limit=10, reverse=True),
        )
        self._apply(result, "pai", parse_pai(pai_items) if pai_items else None)

        # 5. HRV (overnight rMSSD) ---------------------------------------------
        hrv_items = await guarded(
            "HRV",
            async_fetch_v2_events(*api, "HRVRMSSD", sub_type="real_data",
                                  from_ts=days_ago_ms(3), to_ts=to_ms, limit=10, reverse=True),
        )
        hrv = None
        if hrv_items:
            hrv = parse_hrv(
                hrv_items,
                (sleep or {}).get("_sleep_start_s"),
                (sleep or {}).get("_sleep_end_s"),
            )
        self._apply(result, "hrv", hrv)

        # 6. Readiness (v2, with the legacy endpoint as fallback) --------------
        rd_v2 = await guarded(
            "readiness",
            async_fetch_v2_events(*api, "readiness", sub_type="watch_score",
                                  from_ts=days_ago_ms(7), to_ts=to_ms, limit=10, reverse=True),
        ) or []
        rd_v1: list[dict[str, Any]] = []
        if not rd_v2:
            rd_v1 = await guarded(
                "readiness (legacy)",
                async_fetch_user_events(*api, self.userid, "readiness",
                                        from_ts=days_ago_ms(7), to_ts=to_ms, limit=20, reverse=True),
            ) or []
        readiness = parse_readiness(rd_v2, rd_v1) if (rd_v2 or rd_v1) else None
        self._apply(result, "readiness", readiness)

        # 6b. BioCharge (per-minute energy score) -------------------------------
        charge_items = await guarded(
            "BioCharge",
            async_fetch_v2_events(*api, "Charge", sub_type="real_data",
                                  from_ts=days_ago_ms(3), to_ts=to_ms, limit=10, reverse=True),
        ) or []
        self._apply(result, "biocharge", parse_charge(charge_items, tz, now.date()) if charge_items else None)

        # 6c. Overnight respiratory rate ---------------------------------------
        resp_items = await guarded(
            "respiratory rate",
            async_fetch_v2_events(*api, "RespiratoryRate", sub_type="real_data",
                                  from_ts=days_ago_ms(3), to_ts=to_ms, limit=10, reverse=True),
        )
        resp = None
        if resp_items:
            resp = parse_respiratory_rate(
                resp_items,
                (sleep or {}).get("_sleep_start_s"),
                (sleep or {}).get("_sleep_end_s"),
            )
        self._apply(result, "respiratory", resp)

        # 6d. Yesterday, complete, straight from the cloud ---------------------
        yesterday = None
        if band_items:
            yesterday = parse_yesterday(
                band_items, stress_items or [], pai_items or [], charge_items,
                tz, (now - datetime.timedelta(days=1)).date(),
            )
        self._apply(result, "yesterday", {"yesterday": yesterday} if yesterday else None)
        expected_day = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if (result.get("yesterday") or {}).get("date") != expected_day:
            result["yesterday"] = None  # never show the day before yesterday as "yesterday"

        # Derived values -------------------------------------------------------
        result["breathing_score"] = (
            (odi or {}).get("breathing_score")
            if odi is not None
            else prev.get("breathing_score")
        )
        if result["breathing_score"] is None:
            result["breathing_score"] = result.get("readiness_ahi_score")
        result["resting_hr"] = (
            result.get("sleep_rhr")
            or result.get("readiness_sleep_rhr")
            or result.get("pai_rest_hr")
        )

        # 7. Training load -----------------------------------------------------
        load_items = await guarded(
            "training load",
            async_fetch_sport_load(*api, self.userid, days_ago_str(7), today_str, limit=10),
        )
        self._apply(result, "training_load", parse_sport_load(load_items) if load_items else None)

        # 8. Weight / body composition ------------------------------------------
        weight_items = await guarded(
            "weight",
            async_fetch_weight_records(*api, self.userid, limit=5),
        )
        self._apply(result, "weight", parse_weight(weight_items) if weight_items else None)

        # 9. Device battery (only some older bands report it to the cloud) ----
        result["device_batteries"] = prev.get("device_batteries", {})
        raw_devices = await guarded(
            "devices", async_fetch_devices(session, self.apptoken, self.userid, self.host)
        )
        if raw_devices:
            batteries: dict[str, int] = {}
            for dev in raw_devices:
                dev_id = dev.get("deviceId") or dev.get("macAddress", "").replace(":", "")
                for raw_source in (dev.get("additionalSource"), dev.get("additionalInfo")):
                    if not raw_source:
                        continue
                    try:
                        p = json.loads(raw_source) if isinstance(raw_source, str) else raw_source
                        if isinstance(p, dict) and isinstance(p.get("battery"), dict):
                            level = p["battery"].get("level")
                            if level is not None:
                                batteries[dev_id] = int(level)
                                break
                    except (ValueError, TypeError):
                        pass
            result["device_batteries"] = batteries

        # Internal helper keys are not exposed.
        result.pop("_sleep_start_s", None)
        result.pop("_sleep_end_s", None)
        return result

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Zepp API with automatic retry and token refresh on auth errors."""
        try:
            return await self._fetch_metrics()
        except ZeppAuthError as auth_err:
            refreshed = await self._async_refresh_token()
            if refreshed:
                try:
                    return await self._fetch_metrics()
                except Exception as err:
                    raise UpdateFailed(f"Error after re-authenticating with Zepp: {err}") from err
            raise ConfigEntryAuthFailed("Zepp authentication expired or invalid") from auth_err
        except Exception as err:
            _LOGGER.exception("Error updating Zepp coordinator: %s", err)
            raise UpdateFailed(f"Error communicating with Zepp cloud: {err}") from err
