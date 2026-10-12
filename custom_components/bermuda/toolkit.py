"""Live, entry-scoped measurement tools with independent evidence and atomic controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from functools import partial
from typing import Any

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .api import async_get_advert_snapshot, public_device_id, snapshot_observations
from .calibration import CalibrationEngine, range_from_rssi
from .const import (
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    DEFAULT_ATTENUATION,
    DEFAULT_REF_POWER,
    DOMAIN,
)
from .fingerprints import FingerprintModel
from .profiles import effective_model, finite_number, model_version, normalize_toolkit_settings, receiver_value
from .recording import DiagnosticHistory, validate_truth
from .replay import ReplayEngine, ShadowSession
from .signal_quality import FilterState

OPTIONS_KEY = "measurement_toolkit"
MAX_STATES = 4096
HISTORY_BYTES = 1_500_000


def _settings(value: Any) -> dict:
    """Use opaque public beacon keys even for older or directly submitted options."""
    settings = normalize_toolkit_settings(value)
    for field in ("beacon_profiles", "filters", "fixed_references"):
        canonical = {}
        for key, item in settings[field].items():
            public = public_device_id(key)
            if public in canonical:
                msg = "Multiple settings refer to the same beacon"
                raise ValueError(msg)
            if field == "fixed_references":
                item["device_id"] = public
            canonical[public] = item
        settings[field] = canonical
    return settings


class ToolkitManager:
    """Own runtime hooks, bounded workers, persisted controls and measured trials."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.entry = coordinator.config_entry
        self.settings = _settings(self.entry.options.get(OPTIONS_KEY))
        self._saved_options = deepcopy(dict(self.entry.options))
        self.coordinator.options[OPTIONS_KEY] = self.settings
        self.store = Store(self.hass, 1, f"{DOMAIN}.measurement_toolkit.{self.entry.entry_id}")
        self._lock = asyncio.Lock()
        self._worker: asyncio.Future | None = None
        self._background: asyncio.Task | None = None
        self._closed = False
        self._generation = 0
        self._version_cache: str | None = None
        self._identities: dict[str, dict] = {}
        self._identity_dirty = False
        self._states: dict[tuple[str, str], FilterState] = {}
        self._quality: dict[tuple[str, str], dict] = {}
        self._owners: dict[Any, str] = {}
        self._suggestions: dict[str, dict] = {}
        self.engine = CalibrationEngine()
        self.fingerprints = FingerprintModel()
        self.history = DiagnosticHistory(
            window_s=self.settings["history_window_s"],
            enabled=self.settings["history_enabled"],
            max_bytes=HISTORY_BYTES,
        )
        self.shadow: ShadowSession | None = None
        self._shadow_history = DiagnosticHistory(enabled=True, max_bytes=HISTORY_BYTES)
        self.proposal: dict | None = None
        self._proposal_rows: list[dict] = []
        self.last_result: dict | None = None
        self.trial: dict | None = None
        self.last_applied: float | None = None
        self.last_data_id: str | None = None
        self._last_auto = 0.0
        self._last_saved = 0.0
        self._perf = {"updates": 0, "last_update_s": 0.0, "max_update_s": 0.0, "background_skipped": 0}

    @property
    def closed(self) -> bool:
        """Let public lookup refuse a coordinator during unload."""
        return self._closed

    @property
    def fingerprint_area_selection(self) -> bool:
        """Room override is explicitly enabled, never implied by a trained model."""
        return self.settings["fingerprint_area_selection"]

    def _version_for(self, settings: dict, options: dict) -> str:
        """Calculate a signature without publishing candidate settings."""
        if any("reference_power" in reference for reference in settings["fixed_references"].values()):
            settings = deepcopy(settings)
            for device_id, reference in settings["fixed_references"].items():
                if "reference_power" in reference:
                    settings["beacon_profiles"][device_id] = {
                        "reference_power": reference["reference_power"],
                        "reference_power_verified": reference.get("reference_power_verified", False),
                        **settings["beacon_profiles"].get(device_id, {}),
                    }
        powers = {
            public_device_id(key): device.ref_power
            for key, device in self.coordinator.devices.items()
            if getattr(device, "ref_power", 0) != 0 and getattr(device, "create_sensor", False)
        }
        signature = model_version(
            settings,
            options.get(CONF_RSSI_OFFSETS, {}),
            options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
            options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
            device_reference_powers=powers,
        )
        # Filters change the frozen current-distance baseline, even when raw ranges agree.
        return hashlib.sha256((signature + json.dumps(settings["filters"], sort_keys=True)).encode()).hexdigest()[:24]

    def version(self) -> str:
        """Share one model signature across a completed observation batch."""
        if self._version_cache is None:
            self._version_cache = self._version_for(self.settings, self.coordinator.options)
        return self._version_cache

    def invalidate_model(self) -> None:
        """Invalidate evidence immediately when a legacy power control changes."""
        self._version_cache = None
        self._generation += 1
        self.proposal = None
        self._proposal_rows = []
        self._suggestions.clear()

    async def async_load(self) -> None:
        """Load only validated bounded local state before scanner identity resolution."""
        saved = await self.store.async_load()
        if not isinstance(saved, dict):
            return
        identities = saved.get("identities", {})
        if isinstance(identities, dict) and len(identities) <= 128:
            self._identities = {
                key: dict(value)
                for key, value in identities.items()
                if isinstance(key, str)
                and isinstance(value, dict)
                and set(value) == {"unique_id", "entity_id_address"}
                and all(isinstance(item, str) and 0 < len(item) <= 256 for item in value.values())
            }
        if saved.get("history"):
            try:
                self.history.import_data(saved["history"], now=time.time())
                self.history.enabled = self.settings["history_enabled"]
            except ValueError:
                self.last_result = {"reasons": ["saved_history_invalid"]}
        if saved.get("fingerprints"):
            self.fingerprints.import_state(saved["fingerprints"])
        trial = saved.get("trial")
        if isinstance(trial, dict) and isinstance(trial.get("previous_options"), dict):
            self.trial = trial
        for field in ("last_applied", "last_data_id"):
            value = saved.get(field)
            if (field == "last_applied" and finite_number(value)) or (
                field == "last_data_id" and isinstance(value, str)
            ):
                setattr(self, field, value)

    def _store_payload(
        self, *, trial: dict | None = None, replace_trial: bool = False, history: DiagnosticHistory | None = None
    ) -> dict:
        return {
            "identities": deepcopy(self._identities),
            "history": (history or self.history).export(redact=False),
            "fingerprints": self.fingerprints.export_state(),
            "trial": deepcopy(trial if replace_trial else self.trial),
            "last_applied": self.last_applied,
            "last_data_id": self.last_data_id,
        }

    async def async_close(self) -> None:
        """Finish outstanding work without allowing it to publish after unloading."""
        self._closed = True
        if self._background:
            await asyncio.gather(self._background, return_exceptions=True)
        if self._worker:
            await asyncio.shield(self._worker)
        async with self._lock:
            try:
                await self.store.async_save(self._store_payload())
            finally:
                self.history.stop()
                if self.shadow:
                    self.shadow.stop()

    def scanner_identity(self, source: str, proposed_uid: str) -> dict:
        """Keep exactly evidenced historic entity identities when improving bindings."""
        key = source.lower()
        if key not in self._identities:
            from .api import scanner_identity_from_registry_entry  # noqa: PLC0415

            historical = self.coordinator.dr.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, source.upper())})
            preserved = scanner_identity_from_registry_entry(historical, source) if historical else None
            identity = preserved or {"unique_id": proposed_uid, "entity_id_address": proposed_uid}
            if len(self._identities) < 128:
                self._identities[key] = identity
                self._identity_dirty = True
            return deepcopy(identity)
        return deepcopy(self._identities[key])

    def _device_id(self, advert: Any) -> str:
        return self._owners.get(advert, public_device_id(advert.device_address))

    def effective_advert_model(self, advert: Any, device_id: str | None = None) -> dict:
        """Resolve explicit profiles without changing legacy power precedence."""
        options = self.coordinator.options
        reference = options.get(CONF_REF_POWER, DEFAULT_REF_POWER)
        offset = receiver_value(options.get(CONF_RSSI_OFFSETS, {}), advert.scanner_address, 0.0)
        owner = device_id or self._device_id(advert)
        settings = self.settings
        fixed = settings["fixed_references"].get(owner, {})
        if "reference_power" in fixed:
            settings = deepcopy(settings)
            existing = settings["beacon_profiles"].get(owner, {})
            settings["beacon_profiles"][owner] = {
                "reference_power": fixed["reference_power"],
                "reference_power_verified": fixed.get("reference_power_verified", False),
                **existing,
            }
        return effective_model(
            settings,
            advert.scanner_address,
            owner,
            reference_power=reference,
            attenuation=options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
            rssi_offset=offset,
            device_reference_power=getattr(advert, "ref_power", 0.0),
        )

    def process_advert(self, advert: Any, new_stamp: float | None) -> bool:
        """Keep diagnostics for every reading; optional modes handle filtered ranges."""
        device_id = self._device_id(advert)
        key = device_id, advert.scanner_address
        mode = self.settings["filters"].get(device_id, "legacy")
        if mode == "legacy" and not (
            getattr(getattr(advert, "_device", None), "create_sensor", False)
            or advert in self._owners
            or device_id in self.settings["fixed_references"]
        ):
            return False
        state = self._states.get(key)
        if state is None or state.mode != mode:
            if state is None and len(self._states) >= MAX_STATES:
                return False
            state = self._states[key] = FilterState(mode)
        now = monotonic_time_coarse()
        model = self.effective_advert_model(advert)
        kwargs = {"raw_distance": getattr(advert, "rssi_distance_raw", None), "attenuation": model["attenuation"]}
        if new_stamp is not None:
            quality = state.process(
                advert.rssi, new_stamp, now, observed=time.time() - max(0, now - new_stamp), **kwargs
            )
        else:
            quality = state.snapshot(now, **kwargs)
        self._quality[key] = quality
        if mode == "legacy":
            return False
        filtered = quality.get("filtered_rssi")
        if filtered is None:
            advert.rssi_distance = None
        else:
            try:
                advert.rssi_distance = range_from_rssi(
                    filtered, model["reference_power"], model["attenuation"], model["rssi_offset"]
                )
            except ValueError:
                advert.rssi_distance = None
                quality.setdefault("reasons", []).append({"code": "unsafe_range_conversion"})
        if new_stamp is not None and quality.get("fresh") and advert.rssi_distance is not None:
            advert.hist_distance_by_interval.append(advert.rssi_distance)
            del advert.hist_distance_by_interval[:-32]
        return True

    def snapshot_metadata(self, device_id: str, scanner_id: str, advert: Any) -> dict:
        """Publish effective settings and source-clock quality, independently of capture time."""
        state = self._states.get((device_id, scanner_id))
        model = self.effective_advert_model(advert, device_id)
        quality = (
            state.snapshot(
                monotonic_time_coarse(),
                raw_distance=getattr(advert, "rssi_distance_raw", None),
                attenuation=model["attenuation"],
            )
            if state
            else {"reasons": [{"code": "not_processed"}], "reliability": None}
        )
        return {
            **model,
            "model_version": self.version(),
            "quality": quality,
            "filtered_rssi": quality.get("filtered_rssi"),
        }

    def receiver_limit(self, scanner_id: str, global_radius: float) -> float:
        """A zero local limit inherits the global eligibility radius."""
        return receiver_value(self.settings["receiver_limits"], scanner_id, 0) or global_radius

    def receiver_has_limit(self, scanner_id: str) -> bool:
        """Distinguish an explicit cap from unchanged legacy behavior."""
        return bool(receiver_value(self.settings["receiver_limits"], scanner_id, 0))

    def weighted_distance(self, advert: Any, distance: float) -> float:
        """Optional reliability influences election only, not reported physical ranges."""
        if not self.settings["reliability_weighting"]:
            return distance
        quality = self._quality.get((self._device_id(advert), advert.scanner_address), {})
        reliability = quality.get("reliability")
        return distance / max(0.2, reliability) if finite_number(reliability) else distance

    def area_suggestion(self, device: Any) -> dict:
        """Return independent advisory classification with explicit override enablement."""
        return {
            **deepcopy(
                self._suggestions.get(public_device_id(device), {"accepted": False, "reasons": [{"code": "untrained"}]})
            ),
            "enabled": self.fingerprint_area_selection,
        }

    def _snapshot(self) -> dict:
        snapshot = async_get_advert_snapshot(self.hass, entry_id=self.entry.entry_id, include_empty=True)
        if snapshot is None:
            msg = "The Bermuda entry is not loaded"
            raise RuntimeError(msg)
        return snapshot

    def _current_offsets(self) -> dict:
        offsets = deepcopy(dict(self.coordinator.options.get(CONF_RSSI_OFFSETS, {})))
        for key, profile in self.settings["receiver_profiles"].items():
            if "rssi_offset" in profile:
                offsets[key] = profile["rssi_offset"]
        return offsets

    def _state(self) -> dict:
        snapshot = self._snapshot()
        areas = [{"id": area.id, "name": area.name} for area in self.coordinator.ar.async_list_areas()]
        return {
            "snapshot": snapshot,
            "options": deepcopy(self.settings),
            "areas": areas,
            "calibration": {
                "proposal": deepcopy(self.proposal),
                "last_result": deepcopy(self.last_result),
                "trial": {key: value for key, value in (self.trial or {}).items() if key != "previous_options"},
            },
            "history": {**self.history.summary(), "running": self.history.enabled},
            "shadow": self.shadow.summary() if self.shadow else {"state": "absent"},
            "fingerprints": self.fingerprints.summary(),
            "fingerprint_results": deepcopy(self._suggestions),
            "diagnostics": {
                **self._perf,
                "model_version": self.version(),
                "worker_busy": self._worker is not None,
                "filter_states": len(self._states),
                "history_persisted": True,
                "notes": [
                    "Range error does not establish map position accuracy",
                    "Receiver health describes observations, not proxy connectivity",
                ],
            },
        }

    async def _run_worker(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        """A cancelled request keeps the single executor slot until its work really ends."""
        if self._worker is not None:
            msg = "A measurement analysis is already running"
            raise RuntimeError(msg)
        future = self.hass.async_add_executor_job(partial(function, *args, **kwargs))
        self._worker = future

        def finished(result: asyncio.Future) -> None:
            if self._worker is result:
                self._worker = None
            if not result.cancelled():
                result.exception()

        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        finally:
            if future.done() and self._worker is future:
                self._worker = None

    async def _persist_options(
        self,
        options: dict,
        *,
        trial: dict | None = None,
        replace_trial: bool = False,
        history: DiagnosticHistory | None = None,
        expected_version: str | None = None,
    ) -> None:
        """Save rollback evidence before publishing a synchronous HA options update."""
        settings = _settings(options.get(OPTIONS_KEY))
        options[OPTIONS_KEY] = settings
        payload = self._store_payload(trial=trial, replace_trial=replace_trial, history=history)
        if trial and replace_trial:
            payload["last_applied"] = trial["applied_at"]
            payload["last_data_id"] = trial["data_id"]
        await self.store.async_save(payload)
        if expected_version is not None and self.version() != expected_version:
            await self.store.async_save(self._store_payload())
            msg = "The radio model changed while saving; preview again"
            raise RuntimeError(msg)
        if self._closed:
            msg = "The Bermuda entry is unloading"
            raise RuntimeError(msg)
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self._saved_options = deepcopy(options)
        self._adopt_settings(settings, options)
        if history is not None:
            self.history = history
        if replace_trial:
            self.trial = deepcopy(trial)

    def _adopt_settings(self, settings: dict, options: dict) -> None:
        previous = self.version()
        binding_changed = settings["scanner_bindings"] != self.settings["scanner_bindings"]
        self.settings = settings
        self.coordinator.options[OPTIONS_KEY] = settings
        self.coordinator.options[CONF_RSSI_OFFSETS] = deepcopy(options.get(CONF_RSSI_OFFSETS, {}))
        self._version_cache = None
        self.history.window_s = settings["history_window_s"]
        self.history.enabled = settings["history_enabled"]
        self._generation += 1
        changed = self.version() != previous
        if changed:
            self.proposal = None
            self._proposal_rows = []
            self._states.clear()
            self._quality.clear()
            self._suggestions.clear()
            if self.shadow:
                self.shadow.cancel("live settings changed")
            for device in self.coordinator.devices.values():
                for advert in getattr(device, "adverts", {}).values():
                    advert.conf_rssi_offset = receiver_value(
                        options.get(CONF_RSSI_OFFSETS, {}), advert.scanner_address, 0
                    )
                    if finite_number(getattr(advert, "rssi", None)):
                        advert._update_raw_distance(reading_is_new=False)  # noqa: SLF001
        # Manual scanner mappings affect association only; source keys and historic UIDs survive.
        if binding_changed:
            for scanner in self.coordinator.get_scanners:
                scanner.async_as_scanner_resolve_device_entries()

    async def async_options_changed(self, entry: Any) -> bool:
        """Handle toolkit/offset-only edits live; reload all other legacy option changes."""
        new = deepcopy(dict(entry.options))
        excluded = {OPTIONS_KEY, CONF_RSSI_OFFSETS}
        old_other = {key: value for key, value in self._saved_options.items() if key not in excluded}
        new_other = {key: value for key, value in new.items() if key not in excluded}
        if old_other != new_other:
            return False
        async with self._lock:
            if new != self._saved_options:
                self._adopt_settings(_settings(new.get(OPTIONS_KEY)), new)
                self._saved_options = new
        return True

    def _labelled_rows(self, frames: list[dict] | None = None) -> list[dict]:
        result = []
        known = {
            public_device_id(key)
            for key, device in self.coordinator.devices.items()
            if getattr(device, "create_sensor", False)
        }
        for frame in frames if frames is not None else self.history.frames():
            if frame["model_version"] != self.version():
                continue
            for truth in frame["truth"]:
                for row in frame["observations"]:
                    expected = receiver_value(truth["ranges"], row["scanner_id"])
                    if (
                        row["device_id"] in known
                        and row["status"] == "current"
                        and row["device_id"] == truth["device_id"]
                        and expected is not None
                    ):
                        result.append(
                            {
                                **row,
                                "expected_distance": expected,
                                "location_id": truth["location_id"],
                                "accuracy_m": truth["accuracy_m"],
                                "truth_source": truth["source"],
                                "independent": True,
                                "captured_at": frame["captured_at"],
                                "frozen": True,
                            }
                        )
        return result

    def _verified_devices(self) -> list[str]:
        return [key for key, value in self.settings["beacon_profiles"].items() if value.get("reference_power_verified")]

    async def _preview(self, device_id: str | None = None) -> dict:
        rows = self._labelled_rows()
        if device_id:
            rows = [row for row in rows if row["device_id"] == device_id]
        version, generation = self.version(), self._generation
        result = await self._run_worker(
            self.engine.preview,
            rows,
            now=time.time(),
            model_version=version,
            current_offsets=self._current_offsets(),
            verified_reference_devices=self._verified_devices(),
        )
        if generation != self._generation or version != self.version() or self._closed:
            msg = "Settings changed while calibration was running"
            raise RuntimeError(msg)
        result["proposal_id"] = f"{version}:{result['data_id']}"
        self.proposal, self._proposal_rows = result, rows
        return deepcopy(result)

    def _validate_offsets(self, value: Any) -> dict:
        if not isinstance(value, dict) or len(value) > 128:
            msg = "Candidate offsets must be a bounded object"
            raise ValueError(msg)
        known = {scanner.address: scanner.address for scanner in self.coordinator.get_scanners}
        result = {}
        for key, offset in value.items():
            canonical = receiver_value(known, key) if isinstance(key, str) else None
            if canonical is None:
                msg = "Candidate refers to an unknown receiver"
                raise ValueError(msg)
            if not finite_number(offset) or abs(offset) > self.engine.policy.max_offset_db:
                msg = "Candidate offsets are outside safe bounds"
                raise ValueError(msg)
            if canonical in result and result[canonical] != offset:
                msg = "Conflicting offsets refer to the same receiver"
                raise ValueError(msg)
            result[canonical] = float(offset)
        return result

    def _offset_options(self, offsets: dict) -> dict:
        options = deepcopy(dict(self.entry.options))
        stored = options.setdefault(CONF_RSSI_OFFSETS, {})
        settings = _settings(options.get(OPTIONS_KEY))
        for key, value in offsets.items():
            matches = [old for old in stored if old.lower() == key.lower()]
            for old in matches:
                stored[old] = value
            if not matches:
                stored[key] = value
            for old, profile in settings["receiver_profiles"].items():
                if old.lower() == key.lower():
                    profile.pop("rssi_offset", None)
        options[OPTIONS_KEY] = settings
        return options

    async def _apply(self, offsets: dict, *, automatic: bool = False, extra_settings: dict | None = None) -> dict:
        offsets = self._validate_offsets(offsets)
        expected_version = self.version()
        previous = deepcopy(dict(self.entry.options))
        options = self._offset_options(offsets)
        if extra_settings:
            options[OPTIONS_KEY] = _settings({**options[OPTIONS_KEY], **extra_settings})
        now = time.time()
        previous_offsets = self._current_offsets()
        for key in offsets:
            previous_offsets[key] = receiver_value(previous_offsets, key, 0.0)
        trial = {
            "previous_options": previous,
            "previous_offsets": previous_offsets,
            "offsets": offsets,
            "applied_at": now,
            "automatic": automatic,
            "data_id": (self.proposal or {}).get("data_id"),
            "model_version": self._version_for(options[OPTIONS_KEY], options),
        }
        await self._persist_options(options, trial=trial, replace_trial=True, expected_version=expected_version)
        self.last_applied, self.last_data_id = now, trial["data_id"]
        self.last_result = {
            "applied": True,
            "automatic": automatic,
            "offsets": offsets,
            "model_version": self.version(),
        }
        return deepcopy(self.last_result)

    async def _rollback(self) -> dict:
        if not self.trial or self.trial.get("model_version") != self.version():
            msg = "No applicable unchanged trial is available to roll back"
            raise RuntimeError(msg)
        previous = self.trial["previous_options"]
        options = deepcopy(dict(self.entry.options))
        options[CONF_RSSI_OFFSETS] = deepcopy(previous.get(CONF_RSSI_OFFSETS, {}))
        prior = _settings(previous.get(OPTIONS_KEY))
        settings = deepcopy(self.settings)
        for key, profile in settings["receiver_profiles"].items():
            old = prior["receiver_profiles"].get(key, {})
            if "rssi_offset" in old:
                profile["rssi_offset"] = old["rssi_offset"]
            else:
                profile.pop("rssi_offset", None)
        settings["filters"] = prior["filters"]
        options[OPTIONS_KEY] = settings
        await self._persist_options(options, trial=None, replace_trial=True)
        self.last_result = {"rolled_back": True, "model_version": self.version()}
        return deepcopy(self.last_result)

    def _candidate(self, payload: dict) -> tuple[dict, dict]:
        value = payload.get("candidate")
        if value is None:
            value = (self.proposal or {}).get("offsets", {})
        modes = payload.get("filter_modes", {})
        settings = normalize_toolkit_settings({"filters": modes})
        return self._validate_offsets(value), settings["filters"]

    async def _capture(self, payload: dict) -> dict:
        snapshot = self._snapshot()
        now = snapshot["captured"]
        truth = validate_truth(
            {
                "source": "measured",
                "device_id": payload.get("device_id"),
                "location_id": payload.get("location_id"),
                "ranges": payload.get("ranges"),
                "accuracy_m": payload.get("accuracy_m", 0.0),
                "observed": now,
                "independent": True,
            },
            captured_at=now,
        )
        if truth["device_id"] not in snapshot["devices"]:
            msg = "Select a currently tracked device"
            raise ValueError(msg)
        known = {key: key for key in snapshot["devices"][truth["device_id"]]["scanners"]}
        canonical_ranges = {}
        for key, distance in truth["ranges"].items():
            canonical = receiver_value(known, key)
            if canonical is None:
                msg = "Measured range refers to a receiver without observations for this beacon"
                raise ValueError(msg)
            if canonical in canonical_ranges and canonical_ranges[canonical] != distance:
                msg = "Conflicting measured ranges refer to the same receiver"
                raise ValueError(msg)
            canonical_ranges[canonical] = distance
        truth["ranges"] = canonical_ranges
        rows = [
            row
            for row in snapshot_observations(snapshot)
            if row["device_id"] == truth["device_id"]
            and row["status"] == "current"
            and finite_number(row["age"])
            and row["age"] <= 5
            and receiver_value(truth["ranges"], row["scanner_id"]) is not None
        ]
        if not rows:
            msg = "No fresh observations match these measured receivers"
            raise ValueError(msg)
        history = deepcopy(self.history)
        count = history.record(rows, captured_at=now, model_version=self.version(), truth=[truth], force=True)
        if count == 0:
            msg = "No new observations were saved; wait for fresh source readings"
            raise ValueError(msg)
        shadow, shadow_history = self.shadow, self._shadow_history
        if self.shadow and self.shadow.state == "active":
            shadow, shadow_history = deepcopy(self.shadow), deepcopy(self._shadow_history)
            shadow_history.record(rows, captured_at=now, model_version=self.version(), truth=[truth], force=True)
            for frame in shadow_history.frames():
                shadow.add_frame(frame)
        await self.store.async_save(self._store_payload(history=history))
        self.history, self.shadow, self._shadow_history = history, shadow, shadow_history
        self.proposal = None
        self._proposal_rows = []
        return {"captured": count, "truth": truth, "history": self.history.summary()}

    def _feed_shadow(self) -> None:
        if self.shadow and self.shadow.state == "active":
            for frame in self._shadow_history.frames():
                self.shadow.add_frame(frame)

    async def async_dispatch(self, action: str, payload: dict) -> dict:  # noqa: C901, PLR0911
        """Execute entry-scoped actions; the transport/service verifies administrator access."""
        if self._closed:
            msg = "The Bermuda entry is unloading"
            raise RuntimeError(msg)
        if not isinstance(payload, dict):
            msg = "Action payload must be an object"
            raise ValueError(msg)  # noqa: TRY004 - validated JSON API errors are reported as 400.
        if action in ("state", "diagnostics"):
            return self._state()
        async with self._lock:
            if self._closed:
                msg = "The Bermuda entry is unloading"
                raise RuntimeError(msg)
            if action == "settings":
                change = payload.get("settings")
                if not isinstance(change, dict):
                    msg = "settings must be an object"
                    raise ValueError(msg)
                options = deepcopy(dict(self.entry.options))
                options[OPTIONS_KEY] = _settings({**self.settings, **change})
                await self._persist_options(options)
                return {"options": deepcopy(self.settings)}
            if action == "profile":
                kind, key = payload.get("kind"), payload.get("id")
                if kind not in ("receiver", "beacon") or not isinstance(key, str) or not key:
                    msg = "Select a receiver or beacon profile"
                    raise ValueError(msg)
                profile, remove = payload.get("profile"), payload.get("remove", [])
                if (
                    not isinstance(profile, dict)
                    or not isinstance(remove, list)
                    or any(not isinstance(item, str) for item in remove)
                ):
                    msg = "profile and remove must be an object and a list"
                    raise ValueError(msg)
                settings = deepcopy(self.settings)
                profiles = settings[f"{kind}_profiles"]
                current = {**profiles.get(key, {}), **profile}
                for field in remove:
                    current.pop(field, None)
                profiles[key] = current
                options = {**deepcopy(dict(self.entry.options)), OPTIONS_KEY: _settings(settings)}
                await self._persist_options(options)
                return {"profile": deepcopy(self.settings[f"{kind}_profiles"][key])}
            if action == "capture":
                return await self._capture(payload)
            if action == "calibration_preview":
                return await self._preview(payload.get("device_id"))
            if action == "calibration_apply":
                proposal = self.proposal
                if not proposal or not proposal.get("eligible") or proposal["model_version"] != self.version():
                    msg = "A current verified calibration preview is required"
                    raise RuntimeError(msg)
                if payload.get("proposal_id", proposal["proposal_id"]) != proposal["proposal_id"]:
                    msg = "The calibration proposal changed"
                    raise RuntimeError(msg)
                if time.time() - proposal["created_at"] > 300:
                    msg = "The preview expired; run calibration preview again"
                    raise RuntimeError(msg)
                return await self._apply(proposal["offsets"])
            if action == "calibration_rollback":
                return await self._rollback()
            if action in ("history_start", "history_stop", "history_clear"):
                settings = deepcopy(self.settings)
                settings["history_enabled"] = (
                    action != "history_stop" and self.history.enabled
                ) or action == "history_start"
                options = {**deepcopy(dict(self.entry.options)), OPTIONS_KEY: settings}
                history = deepcopy(self.history)
                if action == "history_start":
                    clear = payload.get("clear", True)
                    if type(clear) is not bool:
                        msg = "clear must be a boolean"
                        raise ValueError(msg)
                    history.start(selected_devices=payload.get("devices"), clear=clear)
                elif action == "history_stop":
                    history.stop()
                elif action == "history_clear":
                    history.clear()
                await self._persist_options(options, history=history)
                if self.shadow:
                    self.shadow.cancel("history session changed")
                self.proposal = None
                self._proposal_rows = []
                return self.history.summary()
            if action == "history_export":
                redact = payload.get("redact", True)
                if type(redact) is not bool:
                    msg = "redact must be a boolean"
                    raise ValueError(msg)
                return await self._run_worker(deepcopy(self.history).export, redact=redact)
            if action == "history_import":
                replacement = DiagnosticHistory(window_s=self.settings["history_window_s"], max_bytes=HISTORY_BYTES)
                await self._run_worker(replacement.import_data, deepcopy(payload.get("data")), now=time.time())
                replacement.enabled = self.settings["history_enabled"]
                old = self.history
                self.history = replacement
                try:
                    await self.store.async_save(self._store_payload())
                except Exception:
                    self.history = old
                    raise
                if self.shadow:
                    self.shadow.cancel("history was imported")
                self.proposal = None
                self._proposal_rows = []
                return self.history.summary()
            if action in ("replay", "shadow_start"):
                offsets, modes = self._candidate(payload)
                frames = self.history.frames()
                if action == "replay":
                    return await self._run_worker(
                        ReplayEngine().evaluate, frames, proposed_offsets=offsets, filter_modes=modes, now=time.time()
                    )
                frames = [frame for frame in frames if frame["model_version"] == self.version()]
                shadow = await self._run_worker(
                    ShadowSession, frames, model_version=self.version(), proposed_offsets=offsets, filter_modes=modes
                )
                if shadow.model_version != self.version() or self._closed:
                    msg = "The radio model changed during shadow setup"
                    raise RuntimeError(msg)
                self.shadow = shadow
                self._shadow_history = DiagnosticHistory(
                    enabled=True, window_s=self.settings["history_window_s"], max_bytes=HISTORY_BYTES
                )
                return shadow.summary()
            if action == "shadow_stop":
                if self.shadow:
                    self.shadow.stop()
                return self.shadow.summary() if self.shadow else {"state": "absent"}
            if action == "shadow_promote":
                if not self.shadow:
                    msg = "No shadow comparison is running"
                    raise RuntimeError(msg)
                result = await self._run_worker(
                    deepcopy(self.shadow).promotion_check, current_model_version=self.version()
                )
                if not result["eligible"]:
                    return result
                offsets, modes = self.shadow.proposed_offsets, self.shadow.filter_modes
                try:
                    await self._apply(offsets, extra_settings={"filters": {**self.settings["filters"], **modes}})
                except Exception:
                    self.shadow.cancel("promotion could not be saved")
                    raise
                self.shadow.state = "promoted"
                return {"promoted": True, "validation": result}
            if action in ("fingerprint_train", "fingerprint_clear"):
                previous = self.fingerprints.export_state()
                if action == "fingerprint_train":
                    device_id, location_id = payload.get("device_id"), payload.get("location_id")
                    if not self.coordinator.ar.async_get_area(location_id):
                        msg = "Select an existing Home Assistant area"
                        raise ValueError(msg)
                    snapshot = self._snapshot()
                    rows = [row for row in snapshot_observations(snapshot) if row["device_id"] == device_id]
                    result = self.fingerprints.train(
                        device_id,
                        location_id,
                        rows,
                        independent=True,
                        truth_source="measured",
                        now=snapshot["captured"],
                    )
                else:
                    result = {"cleared": self.fingerprints.clear(payload.get("device_id"), payload.get("location_id"))}
                try:
                    await self.store.async_save(self._store_payload())
                except Exception:
                    self.fingerprints.import_state(previous)
                    raise
                return result
            msg = "Unknown measurement action"
            raise ValueError(msg)

    def prepare_update(self) -> None:
        """Resolve tracked canonical owners before processing shared metadevice readings."""
        configured = {public_device_id(key).lower() for key in self.coordinator.options.get(CONF_DEVICES, [])}
        owners = {}
        for key, device in self.coordinator.devices.items():
            if getattr(device, "create_sensor", False) or public_device_id(key).lower() in configured:
                for advert in getattr(device, "adverts", {}).values():
                    owners[advert] = public_device_id(key)
        previous, self._owners = self._owners, owners
        for advert, owner in owners.items():
            if previous.get(advert) != owner and finite_number(getattr(advert, "rssi", None)):
                advert._update_raw_distance(reading_is_new=False)  # noqa: SLF001

    def after_update(self, duration_s: float) -> None:
        """Measure completed batches and schedule at most one background recorder."""
        self._perf["updates"] += 1
        self._perf["last_update_s"] = duration_s
        self._perf["max_update_s"] = max(self._perf["max_update_s"], duration_s)
        self._version_cache = None
        if self._closed:
            return
        if self._background and not self._background.done():
            self._perf["background_skipped"] += 1
            return
        self._background = self.hass.async_create_background_task(self._after_update(), "bermuda measurement tools")

    async def _after_update(self) -> None:
        """Record real fresh source data; inferred dog locations never become truth."""
        try:
            async with self._lock:
                if self._closed:
                    return
                snapshot = self._snapshot()
                rows = [
                    row
                    for row in snapshot_observations(snapshot)
                    if row["status"] == "current" and finite_number(row["rssi"]) and finite_number(row["raw_distance"])
                ]
                now, version = snapshot["captured"], self.version()
                truth = []
                for device_id, reference in self.settings["fixed_references"].items():
                    truth.append(
                        {
                            "source": "fixed_reference",
                            "device_id": device_id,
                            "location_id": reference["location_id"],
                            "ranges": reference["ranges"],
                            "accuracy_m": reference["accuracy_m"],
                            "observed": now,
                            "independent": True,
                        }
                    )
                matched_truth = [
                    label
                    for label in truth
                    if any(
                        row["device_id"] == label["device_id"]
                        and receiver_value(label["ranges"], row["scanner_id"]) is not None
                        for row in rows
                    )
                ]
                scanner_ids = {row["scanner_id"]: row["scanner_id"] for row in rows}
                for label in matched_truth:
                    label["ranges"] = {
                        receiver_value(scanner_ids, key, key): value for key, value in label["ranges"].items()
                    }
                if self.history.enabled or matched_truth:
                    recording_rows = (
                        rows
                        if self.history.enabled
                        else [
                            row
                            for row in rows
                            if any(
                                row["device_id"] == label["device_id"]
                                and receiver_value(label["ranges"], row["scanner_id"]) is not None
                                for label in matched_truth
                            )
                        ]
                    )
                    self.history.record(
                        recording_rows,
                        captured_at=now,
                        model_version=version,
                        truth=matched_truth,
                        force=bool(matched_truth),
                    )
                if self.shadow and self.shadow.state == "active":
                    self._shadow_history.record(rows, captured_at=now, model_version=version, truth=matched_truth)
                    self._feed_shadow()
                self._suggestions = {
                    device_id: self.fingerprints.suggest(
                        device_id, [row for row in rows if row["device_id"] == device_id], now=now
                    )
                    for device_id in snapshot["devices"]
                }
                self._perf["observations"] = len(rows)
                self._perf["scanners"] = len(snapshot["scanners"])
                if self.settings["auto_calibration"] and now - self._last_auto >= 30:
                    self._last_auto = now
                    await self._automatic(now)
                if self._identity_dirty or (self.history.enabled and now - self._last_saved >= 30):
                    await self.store.async_save(self._store_payload())
                    self._identity_dirty = False
                    self._last_saved = now
        except (ValueError, RuntimeError, OSError) as error:
            self.last_result = {"reasons": [type(error).__name__], "status": "pending"}

    async def _automatic(self, now: float) -> None:
        rows = self._labelled_rows()
        if self.trial and self.trial.get("automatic") and self.trial.get("model_version") == self.version():
            fresh = [row for row in rows if row["observed"] > self.trial["applied_at"]]
            result = await self._run_worker(
                self.engine.evaluate_trial,
                fresh,
                previous_offsets=self.trial["previous_offsets"],
                proposed_offsets=self.trial["offsets"],
                now=now,
                model_version=self.version(),
                verified_reference_devices=self._verified_devices(),
            )
            self.last_result = result
            if result["rollback"]:
                await self._rollback()
                # Prevent repeated oscillation until an administrator explicitly re-enables auto mode.
                options = deepcopy(dict(self.entry.options))
                options[OPTIONS_KEY] = {**self.settings, "auto_calibration": False}
                await self._persist_options(options)
                return
        proposal = await self._preview()
        decision = self.engine.automatic_decision(
            proposal,
            now=time.time(),
            model_version=self.version(),
            current_offsets=self._current_offsets(),
            last_applied=self.last_applied,
            last_data_id=self.last_data_id,
            observations=self._proposal_rows,
            verified_reference_devices=self._verified_devices(),
        )
        self.last_result = decision
        if decision["apply"]:
            await self._apply(decision["offsets"], automatic=True)
