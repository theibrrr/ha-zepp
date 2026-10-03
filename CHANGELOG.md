# Changelog

This is a fork of [yardeff/ha-zepp](https://github.com/yardeff/ha-zepp).

## 1.3.0

### Changed
- **Long-term statistics are now external statistics** (`zepp:*`) instead of being written into
  the statistics of the live sensors. Home Assistant keeps compiling its own sensor statistics
  untouched, so the running sums of steps / distance / calories can no longer be corrupted.
- The **history import** (first start, the "Sync Full History" button and `zepp.sync_history`) was
  rewritten on top of the same writer. It is safe to run again at any time: rows are upserted and
  running sums continue from the last row before the imported period. It runs once automatically
  after upgrading to 1.3.0. Per-minute streams are imported for up to 60 days.
- Fixed a deprecation: statistics metadata now uses `mean_type` / `unit_class`
  (`has_mean` was removed in Home Assistant 2026.4).

### Added
- Hourly mean/min/max statistics built from the watch's own detail data, so graphs show what the
  watch measured even if Home Assistant only polled every few minutes or the phone uploaded late:
  `zepp:heart_rate` (per minute), `zepp:stress` (per 5 min), `zepp:biocharge` (per minute),
  `zepp:hrv`, `zepp:respiratory_rate` (overnight).
- Hourly sum statistics `zepp:steps`, `zepp:distance`, `zepp:calories`.
- Daily statistics (at local midnight): `zepp:sleep_score`, `zepp:sleep_duration`,
  `zepp:deep_sleep`, `zepp:light_sleep`, `zepp:rem_sleep`, `zepp:awake_time`,
  `zepp:sleep_resting_hr`, `zepp:pai`, `zepp:readiness`, `zepp:hrv_night`, `zepp:breathing_score`.
- The last 3 days are re-written every hour from the regular poll, so late uploads fill in.
- Use them with the Statistics graph card (entity `zepp:biocharge` etc.).

## 1.2.0

### Added
- **BioCharge** (`Charge/real_data`, updated every minute on the watch): three sensors
  `biocharge`, `biocharge_physical`, `biocharge_mental`. The total sensor has `today_min`,
  `today_max` and `measured_at` attributes. Samples with `total` 255 (not calculated) are skipped.
- **Sleep Start** / **Sleep End**: timestamp sensors for bedtime and wake time of the last night.
- **Respiratory Rate**: overnight average breaths per minute, with min / max attributes.
- **Yesterday Summary**: state is yesterday's date; attributes hold yesterday's complete values
  straight from the cloud (steps, distance, calories, heart rate avg/min/max, resting HR, stress
  avg/min/max, PAI, BioCharge max/min and the night that ended yesterday morning). Unlike a
  23:59 snapshot it also includes data the phone uploads after midnight.
- New entities follow the entity-id prefix of the device's existing sensors
  (e.g. `sensor.amazfit_steps` -> `sensor.amazfit_biocharge`).

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
