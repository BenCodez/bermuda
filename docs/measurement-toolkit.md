# BenCodez Bermuda measurement toolkit

This custom fork combines the 18 fixes inherited from [bermuda-patched](https://github.com/davidcoulson/bermuda-patched) with twelve measurement and calibration features. The integration domain remains `bermuda`. Existing tracked devices, option keys and entity identities remain compatible; the default distance filter remains Bermuda's legacy filter. This is a fork of [Ashley Gittins's Bermuda](https://github.com/agittins/bermuda), not an official upstream release.

Install or replace the HACS package using the [README instructions](../README.md#install-this-fork-with-hacs). Open **Bermuda toolkit** in the Home Assistant sidebar as an administrator. Its five tabs are **Settings**, **Calibration**, **History & replay**, **Fingerprints** and **Health**. The selected Bermuda configuration entry owns its settings and saved toolkit data. [Panel reference](toolkit-panel.md) describes the controls in more detail.

## Defaults and compatibility

| Setting | Default | Control |
| --- | --- | --- |
| Device filter | `legacy` for every device | Settings → Device filter mode |
| Automatic calibration | Off | Settings → Enable automatic calibration |
| Reliability weighting | Off | Settings → Enable reliability weighting |
| Fingerprint live area selection | Off | Settings → Enable fingerprint area selection |
| Receiver radius override | `0`, following the existing global radius | Settings → Receiver maximum distance |
| Continuous diagnostic recording | Off | History & replay → Start recording |
| History time window | 3,600 seconds | Settings → History window; 60–86,400 seconds allowed |
| Profiles, fixed references, manual bindings and fingerprints | Empty | Settings and Fingerprints |

Toolkit settings live under the additive `measurement_toolkit` options namespace. Existing global attenuation, reference power, receiver offsets, tracked-device choices and entity IDs remain in their existing locations. Toolkit and receiver-offset changes apply live; changes to other legacy integration options retain the usual reload behavior.

Continuous recording is optional, but an explicit **Capture labelled readings** action deliberately saves a bounded snapshot even when recording is stopped. Configured fixed references also authorize collecting labelled snapshots for those references. **Stop recording** retains existing captures; **Clear history** removes them. Saved captures and fingerprints are local Home Assistant data and can survive restart.

## The twelve features

### 1. Stable measurement API

[api.py](../custom_components/bermuda/api.py) provides `async_get_coordinator`, `async_get_advert_snapshot` and `snapshot_observations`. They are Home Assistant event-loop callbacks, despite their `async_` names; call them without `await`. Consumers can subscribe to the coordinator's `async_add_listener` rather than enabling an entity for every device/receiver pair.

```python
from custom_components.bermuda.api import async_get_advert_snapshot, snapshot_observations

snapshot = async_get_advert_snapshot(hass, entry_id=entry_id, include_history=False)
observations = snapshot_observations(snapshot)
```

Version 1 retains the earlier PR834-shaped fields and adds receiver/device identifiers, source aliases, raw RSSI in dBm, effective offset/reference power/attenuation, raw and filtered ranges in metres, packet age/status, quality and model version. Device keys are opaque: private BLE identities have stable public hashes, while iBeacon keys retain their supported identity. Friendly names and slugs are display fields.

The snapshot separates capture UTC time, actual observation time and source monotonic stamps. Refreshing or publishing a snapshot never makes an old packet fresh. Raw evidence remains available when a receiver radius excludes it from area selection. Limits are 256 devices, 128 receivers and 4,096 observation pairs, with explicit omission counts. Optional recent API history contains at most five runtime samples per pair; it is not a frozen replay recording.

### 2. Reliable scanner matching and duplicate-name calibration selectors

Scanner resolution in [bermuda_device.py](../custom_components/bermuda/bermuda_device.py) uses Bluetooth source/registry evidence and ESPHome config-entry relationships. Previously recorded Bermuda scanner identities are retained when an improved binding becomes available. Generic BLE names do not establish hardware identity.

The existing Bermuda options/calibration selector in [config_flow.py](../custom_components/bermuda/config_flow.py) distinguishes receivers with the same friendly name using their area/address information; each selection maps to its own receiver offset.

For an exceptional association, use **Settings → Explicit scanner bindings**. Copy the Bluetooth source ID from Health and the intended Home Assistant device registry ID, then save a map such as:

```json
{
  "synthetic-scanner-source": {
    "device_registry_id": "synthetic-device-registry-id"
  }
}
```

Use the actual IDs from your installation. Changing this association does not create a second copy of existing entities or infer an adjacent MAC/name match.

### 3. Scanner health, freshness and rejection reasons

**Health → Scanner health and freshness** shows receivers, last packet age, area and binding. **Current device observations** and **Load full diagnostics** show source stamps, raw/last-valid timestamps, RSSI variability, estimated observed cadence and structured rejection reasons. [signal_quality.py](../custom_components/bermuda/signal_quality.py) and [api.py](../custom_components/bermuda/api.py) supply these fields.

Duplicates and out-of-order stamps do not add measurements. A missing packet ages the last accepted value; a panel refresh does not fill the gap. Diagnostics can distinguish missing/stale data, an unconfirmed RSSI jump, source-clock issues, insufficient calibration coverage and rejected fingerprint matches. Cadence describes gaps between the actual observations Home Assistant received, not the beacon's guaranteed advertising interval. Receiver health describes observed Bluetooth activity, not proof of proxy network connectivity.

### 4. Live corrections with preview, apply and rollback

**Calibration → Preview calibration** computes a receiver-offset proposal without changing live settings. Inspect `eligible`, coverage, training/held-out errors, proposed absolute offsets, relative deltas and rejection reasons. **Apply accepted proposal** is available for an eligible result; the server checks the proposal, model version and its five-minute expiry again. **Rollback calibration** restores the saved applicable baseline. An intervening model change can make a trial inapplicable.

The [toolkit manager](../custom_components/bermuda/toolkit.py) persists rollback evidence before publishing an options change. Applied receiver offsets use the existing offset controls; an explicit receiver profile offset replaces that receiver's legacy offset, rather than adding it twice. Numeric model changes recompute range without changing the packet stamp or overwriting received RSSI. After selection updates, distance entities publish the corrected value once even when the change falls inside the normal jitter deadband or the first read is delayed.

### 5. Guided calibration at independently measured locations

Use **Calibration → Capture independent measured truth**:

1. Choose a tracked beacon and place it at an independently known stationary spot. Enter a distinct location label such as `reference:a`; this label does not come from Bermuda's predicted area.
2. Measure the straight-line receiver-to-beacon **slant distance in metres**, including mounting-height differences. For example, 6 m horizontal separation and 0.8 m vertical separation give approximately 6.05 m slant distance. Enter only measured receivers and leave the others blank. Allowed ranges are 0.05–1,000 m.
3. Enter the measurement tolerance in metres, then **Capture labelled readings**. The live capture accepts matching observations no older than five seconds. Check that the returned capture count is positive; otherwise collect new matching packets before previewing. This label applies to that capture only.
4. Collect genuinely new packets, multiple positions and multiple beacons. Repeat at another independently labelled location, then preview. Repeated clicks on one cached packet cannot create independent evidence.
5. Inspect the held-out location and held-out beacon results before applying. A lower range error does not establish better room classification or map position accuracy.

[calibration.py](../custom_components/bermuda/calibration.py) fits receiver RSSI offsets using the frozen radio model. It does not automatically fit antenna gain, transmitter power or a new attenuation model. Default absolute calibration requires independently verified reference power, at least two beacons and two locations, adequate receiver coverage, at least three fitting samples per receiver and at least two samples per receiver in each holdout dimension. The lexically last device and location are reserved from fitting.

An advertised TX-power label is not automatically a measured reference RSSI at 1 m. Only mark **Reference power independently verified** when the entered reference has been measured or independently calibrated. With unknown transmitter reference power, calibration can propose relative receiver corrections using consistent multi-beacon evidence and a zero-mean correction gauge. This path needs at least three fitting beacons plus a separate held-out beacon, at least two receivers and multiple locations. It cannot absorb one beacon's unknown transmitter bias into a global receiver correction. Weak, noisy, insufficient or regressing evidence remains pending/rejected.

### 6. Safe automatic calibration

**Settings → Enable automatic calibration** is off by default. The manager checks at most every 30 seconds and applies only a current, validated proposal supported by new independent data. Default policy caps each automatic step at 1 dB, bounds total calibrated offsets to ±20 dB, and enforces a 900-second cooldown. A capped step must itself pass held-out validation; a successful larger proposal is not enough. These bounds apply to new corrections; finite inherited baselines and unrelated legacy receivers do not block trial monitoring. New independent trial evidence can trigger rollback on regression. Restoring the offsets and disabling automatic mode use one saved options update; an administrator must enable automatic mode again afterward.

For ongoing independently labelled evidence, **Settings → Fixed reference labels** accepts a deliberately configured stationary beacon map:

```json
{
  "synthetic-beacon-id": {
    "location_id": "reference:a",
    "ranges": {
      "synthetic-scanner-one": 6.05,
      "synthetic-scanner-two": 12.4
    },
    "accuracy_m": 0.15
  }
}
```

Replace placeholders with opaque IDs selected from the panel. Fixed labels assert the listed position/ranges for future observations; remove or update them when the beacon moves. Save `{}` to remove fixed references. A fixed reference can also include independently established `reference_power` and `reference_power_verified`, subject to the same verification rule as a beacon profile. Bermuda's current area and fingerprint suggestions never become automatic calibration truth.

### 7. Receiver and beacon hardware profiles

Use **Settings → Hardware and radio profile** to select a receiver or beacon. [profiles.py](../custom_components/bermuda/profiles.py) validates the saved profile. Hardware type, manufacturer/model, antenna description, antenna gain in dBi, height and indoor/outdoor notes describe your installation. These descriptions do not invent gain corrections or infer a hardware model from a Bluetooth name.

Only explicit radio-model fields change ranging: `reference_power` (−120–0 dBm), `attenuation` (0.1–8) and receiver `rssi_offset` (−30–30 dB). A beacon profile's reference power takes precedence over a nonzero legacy per-device value, then receiver profile power, then global power. Receiver attenuation takes precedence over beacon attenuation, then global attenuation. Clear a numeric field and save to restore inheritance. Profiles accept at most 128 entries of each kind.

### 8. Receiver reliability and radius limits

**Settings → Receiver maximum distance** sets a local 0–1,000 m area-selection radius. `0` follows the existing global radius. An explicit positive limit applies to both challengers and the incumbent, including a configured limit equal to global. Distant raw RSSI/range observations remain available through the measurement API, history and diagnostics; the limit changes area eligibility only.

**Settings → Enable reliability weighting** optionally discounts less reliable receivers during the distance comparison. RSSI variance/noise and freshness contribute to measurement reliability, and range variance is propagated through the current attenuation model. These are uncertainty estimates, not a guarantee of physical position accuracy. An explicitly supplied measured range/tolerance can support a range residual check in the pure quality helper. Profile `distance_accuracy_m` is metadata; it is not a certified live accuracy reading or an automatic model correction.

Weights affect the area comparison and historical hysteresis tests without replacing raw or published physical range values. The default leaves weighting off and retains legacy hysteresis, including its existing global-incumbent behavior.

### 9. Optional per-device pet/stationary filtering

Choose **Settings → Device filter mode → legacy / pet / stationary**. Legacy retains normal Bermuda distance filtering and its 30-second expiry. The optional modes use the actual elapsed packet time to smooth RSSI; pet adapts faster, while stationary smooths more strongly.

An isolated large jump is held as a candidate. A sustained real step is accepted after three fresh confirmations in pet mode or four in stationary mode, so it cannot remain clamped indefinitely. Raw RSSI, raw range and last actual packet time remain separate from filtered RSSI/range and last-valid time. Duplicates cannot confirm a step. Known clock/session resets start a new filter state; gaps can expire and reseed smoothing. Optional tag-data timeout follows observed cadence within 30–120 seconds, while public API freshness continues to expose source-age status.

Use independently labelled recordings to compare noise suppression with movement/filter delay. Fewer RSSI spikes alone do not establish better pet tracking. The filter implementation is [signal_quality.py](../custom_components/bermuda/signal_quality.py), with live hooks in [bermuda_advert.py](../custom_components/bermuda/bermuda_advert.py).

### 10. Bounded labelled history, replay and shadow promotion

**History & replay → Start recording** records the selected tracker or all tracked devices; starting through the panel clears the preceding history. **Stop recording** stops continuous capture, and **Clear history** removes stored captures. [recording.py](../custom_components/bermuda/recording.py) freezes actual observation/model inputs and independent label provenance, deduplicates source stamps and bounds time, frames, rows and bytes.

The current manager uses a 3,600-second window by default, with limits of 600 frames, 10,000 observations and 1,500,000 serialized bytes; older entries are evicted when any limit is reached. This byte bound leaves room for the 2 MiB transport envelope and identifier pseudonyms. The history is persisted locally in Home Assistant's `.storage/bermuda.measurement_toolkit.<entry_id>` store, periodically while recording and during explicit saves/unload. A sudden process loss can lose observations since the last save. An instant labelled capture saves immediately. Stopped history retains its existing labels; they do not label future packets.

**Export JSON** / **Copy recording** exclude private key fields. **Redact recording identifiers for sharing** is selected by default and additionally pseudonymizes device, receiver and location IDs while preserving relationships within the recording. Unchecking it retains identifiers for local analysis. Imported pseudonyms do not automatically identify live hardware. The panel and HTTP/service boundary accept at most 2 MiB per import/request. Imports validate JSON and recording budgets; no server disk path is accepted.

**Run replay** compares frozen raw, current and candidate ranges, range error by receiver/device/location, packet gaps and available movement/filter-delay measurements. Enter candidate **absolute receiver offsets** as JSON; an empty object uses the current proposal when present. Live settings are unchanged. Candidate filter modes are also available through the service/API `filter_modes` payload. The pure [ReplayEngine](../custom_components/bermuda/replay.py) supports explicit held-out locations; the panel replay does not select a holdout by itself. Calibration preview and shadow promotion perform their own independent coverage checks.

**Start shadow** observes a bound candidate without applying it. Collect new independently labelled captures/fixed-reference observations, then **Promote shadow candidate** requests validation and an explicit apply. Default promotion requires at least six new labelled observations and two independent locations, sufficient coverage for affected receivers/devices, improvement and no material per-group regression. Available labelled movement transitions are checked for response regression; absent transitions leave delay unavailable, so range-only validation does not establish movement responsiveness. Settings, history replacement or session changes can cancel an inapplicable candidate. Shadow sessions are observational runtime state; saved history supplies repeatable evidence after restart.

Range residuals are not map position accuracy. Replay reports position accuracy unavailable; neither a room label nor a model-selected area becomes a measured coordinate.

### 11. Supervised room fingerprints and template areas

**Fingerprints → Train labelled sample** trains the chosen device only in the Home Assistant area you independently select. Repeat at different positions/orientations/times in that known area; train other relevant areas too. No passive self-training uses the current Bermuda area. Samples require at least two genuinely newer receivers. A usable template requires at least five fresh snapshots spanning at least four seconds, with stable receiver coverage. [fingerprints.py](../custom_components/bermuda/fingerprints.py) penalizes missing/extra receivers and rejects insufficient coverage, diffuse training, unknown patterns and ambiguous competing labels.

Models are device-specific; different beacons are not assumed to share transmit power or RF patterns. Their state is bounded to 64 frames per label, 128 total labels, 1,024 total frames and 2,000,000 serialized bytes. Changes to the model signature invalidate matching against old templates: clear the selected device's fingerprints and retrain with the new settings.

The disabled-by-default diagnostic **Suggested Area** sensor exposes accepted advisory results separately from the normal **Area** sensor. Enable it in the tracked device's entities when useful for templates/automations. Its attributes include confidence, coverage and rejection reasons. **Fingerprints → Current fingerprint matches** and Health diagnostics also expose matching scores/margins and current results.

Only **Settings → Enable fingerprint area selection** permits a live area override. It additionally requires an accepted result, confidence of at least 0.9 and an existing Home Assistant area. A single eligible location's confidence is capped at 0.85, so one trained area alone cannot override live tracking. A suggested room can lack its own receiver; the fork then leaves distance to that area unknown rather than inventing a range. Stock area selection supplies the fallback when the checks fail.

### 12. Runtime performance diagnostics

**Health → Load full diagnostics** reports completed update count, last/max foreground update duration, observation/receiver counts, filter-state count, worker status and skipped background cycles. Timings cover collection, distance calculation, area selection and pruning before background toolkit work. They describe the running installation, not a claimed universal benchmark improvement.

[coordinator.py](../custom_components/bermuda/coordinator.py) reports only completed updates. The manager owns one background recorder task and one executor analysis slot, with bounded histories and up to 4,096 receiver/device filter states. Busy analysis or changing settings can produce a conflict/pending result; retry after the displayed work completes. Duplicate-source processing does not add measurement evidence, while time-dependent expiry continues even without new packets.

## Services and authenticated HTTP

The panel uses the administrator-only `GET /api/bermuda/toolkit?entry_id=...` and JSON `POST /api/bermuda/toolkit` boundary in [toolkit_http.py](../custom_components/bermuda/toolkit_http.py). POST includes `action`, optional `entry_id` and action-specific fields. Responses are not cached. Home Assistant user context determines access; a JSON permission claim has no effect.

The matching response-optional service is `bermuda.measurement_toolkit`, registered by [toolkit_services.py](../custom_components/bermuda/toolkit_services.py). Administrator user calls and trusted local automations can use it. `entry_id` may be omitted when exactly one Bermuda entry is loaded:

```yaml
action: bermuda.measurement_toolkit
data:
  action: calibration_preview
  payload: {}
response_variable: calibration_preview
```

Supported actions are `state`, `diagnostics`, `settings`, `profile`, `capture`, `calibration_preview`, `calibration_apply`, `calibration_rollback`, `history_start`, `history_stop`, `history_clear`, `history_export`, `history_import`, `replay`, `shadow_start`, `shadow_stop`, `shadow_promote`, `fingerprint_train` and `fingerprint_clear`. They use the same manager checks as the panel.

Only one action runs at a time. Disconnecting a caller lets an already accepted action finish its complete transaction. Local evidence and rollback records use atomic writes with visible save errors; a failed write keeps the prior live state. Options changes also keep a bounded local commit record. This preserves trial evidence while Home Assistant saves its options file and lets restart recovery complete an accepted change without overwriting later manual edits. Internal commit identifiers stay out of published measurements and control settings. Host reboot detection starts a new source-clock session, while ordinary integration reloads retain packet deduplication. Changing the history window prunes retained captures before saving the new bound.

## Privacy and interpretation

These tools need local packet timestamps, opaque IDs, RSSI, ranges, model settings and explicit labels. They do not require GPS files or external location uploads. Repository examples and tests use synthetic data; do not add personal GPS tracks, real private BLE identity keys or household recordings to this repository.

Private keys are excluded from toolkit responses/exports, and private BLE IDs are hashed for the public API. Full diagnostics can still contain receiver/device names, area names, timing and range patterns; use the redacted history export for sharing and review the remaining content. Unredacted local captures, fixed labels and templates are stored on your Home Assistant instance and inherit its backup/access policy.

Physical ranging, supervised room classification and Home/Not Home tracking describe different outcomes. Treat missing evidence as unavailable, verify movement/placement changes independently and retain the raw channels when evaluating a candidate.
