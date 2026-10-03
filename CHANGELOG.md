# Changelog

This is a fork of [yardeff/ha-zepp](https://github.com/yardeff/ha-zepp).

## 1.1.0

Fixes for values that were stale or never arrived. Entity IDs and unique IDs are unchanged,
so existing sensors and their history carry over.

### Fixed
- **Sleep** stayed on the first night it ever read until Home Assistant restarted. Every poll now
  takes the newest night that actually contains sleep (empty day records are skipped).
- **Stress** showed yesterday's daily average and changed once a day. It now shows the newest
  5-minute reading; the daily average, min/max and zone percentages are attributes.
- **Event order**: Zepp returns events oldest-first unless `reverse=1` is sent, so stress, PAI,
  HRV and readiness were read from an old record. All event requests are now newest-first and
  sorted client side as well.
- **SpO2** spot readings were never parsed (`extra.spo2` is a number, not a list) and
  `spo2_decrease` (an apnea low point) was mistaken for a saturation value.
- **Breathing quality** now comes from the nightly blood-oxygen summary
  (`/events/dateString`, `blood_oxygen/odi`), with ODI and desaturation count as attributes.
- **Readiness** is read from `/v2/users/me/events` (`value` envelope) with the legacy endpoint as
  fallback; `255` sentinels are ignored. The sensor is always created.
- **HRV** is the overnight rMSSD average (Zepp splits a night over two UTC-day buckets; both are
  merged). Last sample, min, max and sample count are attributes.
- **Weight** values are inside a `summary` object and were never found.
- **Training load** asks for the newest day first.
- The **one-year history import** ran on every restart and every options change. It now runs once
  per config entry; the button and `zepp.sync_history` still run it on demand.
- Removed the direct SQL writes into the recorder statistics table (they only worked for one
  hard-coded entity id). A proper statistics import is planned.

### Added
- Amazfit Bip Max (`deviceSource` 11206915) in the device catalog.
- Attributes: heart rate `measured_at`; sleep `sleep_start` / `sleep_end`; PAI `daily_pai` and
  zone minutes; readiness components; training load `overreaching`.
- `custom_components/zepp/parsers.py` with unit tests in `tests/`.
