"""The Zepp (Amazfit) integration."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import ZeppCoordinator
from .device_catalog import async_init_device_catalog
from .history_sync import async_sync_historical_data
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

HISTORY_STORE_VERSION = 1


def _history_store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, HISTORY_STORE_VERSION, f"{DOMAIN}_history_{entry.entry_id}")


# Bumped when the statistics format changes and history must be imported again.
# 2 = external zepp:* statistics (1.3.0).
STATISTICS_FORMAT = 2


async def _async_initial_backfill(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Import one year of history once per statistics format, then remember it."""
    await async_sync_historical_data(hass, entry.data, days=365)
    await _history_store(hass, entry).async_save(
        {"initial_backfill_done": True, "statistics_format": STATISTICS_FORMAT}
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Zepp component."""
    await async_register_services(hass)
    await async_init_device_catalog(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Zepp from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await async_init_device_catalog(hass)

    coordinator = ZeppCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # The one-year history import runs once per config entry and statistics
    # format (it used to run on every restart and every options change).
    # 1.3.0 writes zepp:* external statistics, so it runs once more after the
    # upgrade. A manual import is still available via the button or zepp.sync_history.
    sync_task = None
    stored = await _history_store(hass, entry).async_load() or {}
    if stored.get("statistics_format") != STATISTICS_FORMAT:
        sync_task = entry.async_create_background_task(
            hass, _async_initial_backfill(hass, entry), "zepp_history_import"
        )

    hass.data[DOMAIN][entry.entry_id] = {
        "data": entry.data,
        "coordinator": coordinator,
        "history_sync_task": sync_task,
    }

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_info = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_info:
        sync_task = entry_info.get("history_sync_task")
        if sync_task and not sync_task.done():
            _LOGGER.debug("Cancelling active historical sync task during unload")
            sync_task.cancel()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the one-time history flag when the integration is removed."""
    await _history_store(hass, entry).async_remove()
