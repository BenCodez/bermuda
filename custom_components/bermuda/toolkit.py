"""Live, entry-scoped measurement tools with independent evidence and atomic controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from copy import copy, deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import async_get_advert_snapshot, public_device_id, snapshot_observations
from .calibration import CalibrationEngine, range_from_rssi
from .const import (
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ATTENUATION,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_MAX_RADIUS,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_REF_POWER,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_UPDATE_INTERVAL,
    DISTANCE_INFINITE,
    DOMAIN,
    SIGNAL_SCANNERS_CHANGED,
)
from .fingerprints import FingerprintModel
from .profiles import effective_model, finite_number, model_version, normalize_toolkit_settings, receiver_value
from .recording import DiagnosticHistory, validate_truth
from .replay import ReplayEngine, ShadowSession
from .signal_quality import FilterState, normalize_source_age
from .toolkit_store import DurableToolkitStore
from .util import rssi_to_metres

OPTIONS_KEY = "measurement_toolkit"
MAX_STATES = 4096
HISTORY_BYTES = 1_500_000
COMMIT_KEY = "_commit_id"
MAX_COMMIT_ANCHORS = 128
MAX_COMMIT_BYTES = 4 * 1024 * 1024


def _host_clock_id() -> str | None:
    """Read a private boot marker outside the event loop; never export its value."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:24] if value else None


def _settings(value: Any) -> dict:
    """Use opaque public beacon keys even for older or directly submitted options."""
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != COMMIT_KEY}
    settings = normalize_toolkit_settings(value)
    for field in ("beacon_profiles", "filters", "fixed_references"):
        canonical = {}
        for key, item in settings[field].items():
            public = _candidate_identity(public_device_id(key))
            if field == "fixed_references":
                item["device_id"] = public
            if public in canonical and canonical[public] != item:
                msg = "Multiple settings refer to the same beacon"
                raise ValueError(msg)
            canonical[public] = item
        settings[field] = canonical
    return settings


def _same_options(left: dict, right: dict) -> bool:
    """Compare complete JSON state without conflating boolean/numeric edits."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def _candidate_identity(identifier: str) -> str:
    """Collapse MAC aliases only in detached candidate identity maps."""
    canonical = identifier.lower()
    return receiver_value({canonical: canonical}, identifier, identifier)


def _fixed_reference_profile(reference: dict, profile: dict) -> dict:
    """Keep verification attached to the power supplied by the same source."""
    return {
        "reference_power": reference["reference_power"],
        "reference_power_verified": (
            False if "reference_power" in profile else reference.get("reference_power_verified", False)
        ),
        **profile,
    }


def _options_digest(options: dict, modified_at: str) -> str:
    value = {"options": options, "modified_at": modified_at}
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _disk_options(path: str, entry_id: str) -> dict | None:
    """Read only an acknowledgement of HA's own delayed options write."""
    try:
        file = Path(path)
        if file.stat().st_size > 32 * 1024 * 1024:
            return None
        saved = json.loads(file.read_bytes())
        for entry in saved.get("data", {}).get("entries", []):
            if entry.get("entry_id") == entry_id and isinstance(entry.get("options"), dict):
                return {"options": entry["options"], "modified_at": entry.get("modified_at")}
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return None


def _rebase_distance(distance: float | None, old_model: dict, new_model: dict) -> float | None:
    """Project one stored distance through its equivalent RSSI without reseeding filters."""
    if distance is None or distance == DISTANCE_INFINITE:
        return distance
    if finite_number(distance) and distance == 0:
        # Floating-point underflow loses the equivalent RSSI. Retain that
        # accepted lower bound rather than inventing a filtered observation.
        return 0.0
    if not finite_number(distance) or distance <= 0:
        return None
    equivalent_rssi = (
        old_model["reference_power"] - old_model["rssi_offset"] - 10.0 * old_model["attenuation"] * math.log10(distance)
    )
    return rssi_to_metres(
        equivalent_rssi + new_model["rssi_offset"], new_model["reference_power"], new_model["attenuation"]
    )


def _rebase_raw_history(history: list, rssi_history: list, old_model: dict, new_model: dict) -> list:
    """Recover saturated raw values from their original aligned RSSI samples."""
    rebased = []
    for index, distance in enumerate(history):
        value = _rebase_distance(distance, old_model, new_model)
        if (distance in (0, DISTANCE_INFINITE) or value is None) and index < len(rssi_history):
            rssi = rssi_history[index]
            if finite_number(rssi):
                value = rssi_to_metres(
                    rssi + new_model["rssi_offset"], new_model["reference_power"], new_model["attenuation"]
                )
        # Legacy velocity arithmetic requires numeric samples, even when a
        # malformed or unavailable old value has no usable source evidence.
        rebased.append(float(DISTANCE_INFINITE) if value is None else value)
    return rebased


class ToolkitManager:
    """Own runtime hooks, bounded workers, persisted controls and measured trials."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.entry = coordinator.config_entry
        self.settings = _settings(self.entry.options.get(OPTIONS_KEY))
        self._saved_options = deepcopy(dict(self.entry.options))
        self._options_journal: dict | None = None
        self.coordinator.options[OPTIONS_KEY] = self.settings
        self.store = DurableToolkitStore(self.hass, 1, f"{DOMAIN}.measurement_toolkit.{self.entry.entry_id}")
        self._lock = asyncio.Lock()
        self._worker: asyncio.Future | None = None
        self._background: asyncio.Task | None = None
        self._action_task: asyncio.Task | None = None
        self._closed = False
        self._available = True
        self._generation = 0
        self._version_cache: str | None = None
        self._boot_id: str | None = None
        self._identities: dict[str, dict] = {}
        self._identities_ready = True
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
        self._history_epoch_rebased_at: float | None = None
        self._last_auto = 0.0
        self._last_saved = 0.0
        self._perf = {"updates": 0, "last_update_s": 0.0, "max_update_s": 0.0, "background_skipped": 0}

    @property
    def closed(self) -> bool:
        """Let public lookup refuse a coordinator during unload."""
        return self._closed

    @property
    def scanner_identity_ready(self) -> bool:
        """Defer permanent entity identity choices during persisted state loading."""
        return self._identities_ready

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
                    settings["beacon_profiles"][device_id] = _fixed_reference_profile(
                        reference, settings["beacon_profiles"].get(device_id, {})
                    )
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
        if self.shadow:
            self.shadow.cancel("the radio model changed")

    async def async_load(self) -> None:
        """Load only validated bounded local state before scanner identity resolution."""
        self._available = False
        self._identities_ready = False
        self._boot_id = await self.hass.async_add_executor_job(_host_clock_id)
        saved = await self.store.async_load()
        if not isinstance(saved, dict):
            self._finish_identity_loading()
            return
        saved = await self._recover_options(saved)
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
                self.history.import_data(saved["history"], now=time.time(), resume=True)
                if saved.get("history_clocks") is not None:
                    self.history.restore_clocks(saved["history_clocks"], now=time.time())
                self.history.enabled = self.settings["history_enabled"]
            except ValueError:
                self.last_result = {"reasons": ["saved_history_invalid"]}
        if saved.get("fingerprints"):
            self.fingerprints.import_state(saved["fingerprints"])
        clock = saved.get("clock", {})
        rebased_at = saved.get("history_epoch_rebased_at")
        if finite_number(rebased_at) and rebased_at >= 0:
            self._history_epoch_rebased_at = rebased_at
        reset = False
        if isinstance(clock, dict):
            if self._boot_id and clock.get("boot_id"):
                reset = self._boot_id != clock["boot_id"]
            elif finite_number(clock.get("stamp")) and finite_number(clock.get("observed")):
                elapsed_wall = time.time() - clock["observed"]
                elapsed_source = monotonic_time_coarse() - clock["stamp"]
                reset = elapsed_source < 0 or abs(elapsed_wall - elapsed_source) > 5
        if reset:
            if self.history.new_source_session(compact=True):
                self.last_result = {"reasons": ["history_clock_epochs_rebased"], "status": "recovered"}
                if isinstance(saved.get("last_data_id"), str) and saved["last_data_id"]:
                    self._history_epoch_rebased_at = time.time()
            self.history.enabled = self.settings["history_enabled"]
            self.fingerprints.new_session()
        trial = saved.get("trial")
        if isinstance(trial, dict) and isinstance(trial.get("previous_options"), dict):
            self.trial = trial
        for field in ("last_applied", "last_data_id"):
            value = saved.get(field)
            if (field == "last_applied" and finite_number(value)) or (
                field == "last_data_id" and isinstance(value, str)
            ):
                setattr(self, field, value)
        self._finish_identity_loading()

    def _finish_identity_loading(self) -> None:
        """Resolve receivers discovered during loading before platforms create IDs."""
        self._identities_ready = True
        pending = getattr(self.coordinator, "scanner_identity_pending", False) is True
        if pending:
            self.coordinator.scanner_identity_pending = False
        try:
            for scanner in tuple(getattr(self.coordinator, "get_scanners", ())):
                resolve = getattr(scanner, "async_as_scanner_resolve_device_entries", None)
                if callable(resolve):
                    resolve()
        except BaseException:
            self._identities_ready = False
            if pending:
                self.coordinator.scanner_identity_pending = True
            raise
        self._available = True

    def _store_payload(
        self,
        *,
        trial: dict | None = None,
        replace_trial: bool = False,
        history: DiagnosticHistory | None = None,
        fingerprints: FingerprintModel | None = None,
    ) -> dict:
        return {
            "clock": {"boot_id": self._boot_id, "stamp": monotonic_time_coarse(), "observed": time.time()},
            "identities": deepcopy(self._identities),
            "history": (history or self.history).export(redact=False),
            "history_clocks": (history or self.history).export_clocks(),
            "history_epoch_rebased_at": self._history_epoch_rebased_at,
            "fingerprints": (fingerprints or self.fingerprints).export_state(),
            "trial": deepcopy(trial if replace_trial else self.trial),
            "last_applied": self.last_applied,
            "last_data_id": self.last_data_id,
            "options_journal": deepcopy(self._options_journal),
        }

    def _trial_state(self) -> dict:
        return {"trial": deepcopy(self.trial), "last_applied": self.last_applied, "last_data_id": self.last_data_id}

    def _reconcile_options(self, options: dict) -> None:
        """Rebuild all constructor defaults before any refresh or Number restore."""
        defaults = {
            CONF_ATTENUATION: DEFAULT_ATTENUATION,
            CONF_DEVTRACK_TIMEOUT: DEFAULT_DEVTRACK_TIMEOUT,
            CONF_MAX_RADIUS: DEFAULT_MAX_RADIUS,
            CONF_MAX_VELOCITY: DEFAULT_MAX_VELOCITY,
            CONF_REF_POWER: DEFAULT_REF_POWER,
            CONF_SMOOTHING_SAMPLES: DEFAULT_SMOOTHING_SAMPLES,
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
            CONF_RSSI_OFFSETS: {},
        }
        self._adopt_settings(_settings(options.get(OPTIONS_KEY)), options)
        self.coordinator.options.clear()
        self.coordinator.options.update({**defaults, **deepcopy(options), OPTIONS_KEY: self.settings})
        self._version_cache = None
        self._saved_options = deepcopy(options)

    @staticmethod
    def _valid_journal(value: Any) -> bool:
        """Require bounded local provenance before replaying an options transition."""
        try:
            ancestor, depth = value, 0
            while isinstance(ancestor, dict) and "fallback" in ancestor:
                depth += 1
                if depth > MAX_COMMIT_ANCHORS:
                    return False
                ancestor = ancestor["fallback"]
            return (
                isinstance(value, dict)
                and value.get("phase") in ("prepared", "accepted")
                and isinstance(value.get("desired"), dict)
                and isinstance(value.get("before"), dict)
                and isinstance(value.get("after"), dict)
                and ("previous_options" not in value or isinstance(value["previous_options"], dict))
                and isinstance(value.get("anchors"), list)
                and 0 < len(value["anchors"]) <= MAX_COMMIT_ANCHORS
                and all(isinstance(anchor, str) and len(anchor) == 64 for anchor in value["anchors"])
                and len(json.dumps(value, allow_nan=False).encode()) <= MAX_COMMIT_BYTES
                and isinstance(value["desired"].get(OPTIONS_KEY), dict)
                and isinstance(value["desired"][OPTIONS_KEY].get(COMMIT_KEY), str)
                and len(value["desired"][OPTIONS_KEY][COMMIT_KEY]) == 32
            )
        except (ValueError, TypeError, RecursionError):
            return False

    async def _recover_options(self, saved: dict) -> dict:
        """Recover durable accepted intent while respecting later actual HA edits."""
        journal = saved.get("options_journal")
        if not self._valid_journal(journal):
            return saved
        current = deepcopy(dict(self.entry.options))
        selected = journal
        compensating = False
        if journal["phase"] == "prepared":
            previous = journal.get("previous_options")
            if isinstance(previous, dict) and _same_options(current, journal["desired"]):
                # HA may have flushed provisional options before the accepted
                # witness failed. A prepared action was never acknowledged;
                # compensate its exact options without overwriting later edits.
                journal = deepcopy(journal)
                journal["anchors"] = [_options_digest(current, self.entry.modified_at.isoformat())]
                self._options_journal = journal
                compensating = True
                self.hass.config_entries.async_update_entry(self.entry, options=deepcopy(previous))
                self._reconcile_options(previous)
                current = deepcopy(previous)
            fallback = journal.get("fallback")
            if not self._valid_journal(fallback):
                return {**saved, **deepcopy(journal["before"])}
            if fallback["phase"] == "prepared":
                recovered = await self._recover_options(
                    {**saved, **deepcopy(journal["before"]), "options_journal": fallback}
                )
                if compensating:
                    self._options_journal = journal
                return recovered
            saved = {
                **saved,
                **{
                    key: deepcopy(journal["before"][key])
                    for key in ("history", "history_clocks")
                    if key in journal["before"]
                },
            }
            selected = fallback
        desired = selected["desired"]
        if _same_options(current, desired):
            recovered = selected["after"]
        elif (
            selected["phase"] == "accepted"
            and _options_digest(current, self.entry.modified_at.isoformat()) in selected["anchors"]
        ):
            # The complete old options and token must match a known committed
            # ancestor. Newer external edits are never merged or overwritten.
            self.hass.config_entries.async_update_entry(self.entry, options=deepcopy(desired))
            self._reconcile_options(desired)
            recovered = selected["after"]
        else:
            # An external edit can retain the latest token, including an undo.
            # Preserve it. Retain prior monitoring evidence rather than discard
            # it solely because restored Number powers are not available yet.
            recovered = selected["before"] if selected["before"].get("trial") else selected["after"]
            backup = selected.get("backup_trial")
            if isinstance(backup, dict) and self._trial_offsets_match(backup, current):
                recovered = {**recovered, "trial": backup}
        # Keep compensation recoverable through diagnostic writes or another
        # accepted action before HA's delayed rollback reaches its own file.
        self._options_journal = deepcopy(journal if compensating else selected)
        metadata = {
            key: deepcopy(recovered[key]) for key in ("trial", "last_applied", "last_data_id") if key in recovered
        }
        return {**saved, **metadata}

    @staticmethod
    def _trial_offsets_match(trial: dict, options: dict) -> bool:
        settings = _settings(options.get(OPTIONS_KEY))
        for scanner, expected in trial.get("offsets", {}).items():
            profile = receiver_value(settings["receiver_profiles"], scanner, {})
            current = profile.get("rssi_offset", receiver_value(options.get(CONF_RSSI_OFFSETS, {}), scanner, 0.0))
            if not finite_number(current) or current != expected:
                return False
        return bool(trial.get("offsets"))

    async def async_close(self) -> None:
        """Finish outstanding work without allowing it to publish after unloading."""
        self._closed = True
        if self._action_task:
            await asyncio.gather(self._action_task, return_exceptions=True)
        if self._background:
            await asyncio.gather(self._background, return_exceptions=True)
        if self._worker:
            await asyncio.shield(self._worker)
        async with self._lock:
            try:
                if self._available:
                    await self.store.async_save(self._store_payload())
            finally:
                self.history.stop()
                if self.shadow:
                    self.shadow.stop()

    def scanner_identity(self, source: str, proposed_uid: str) -> dict:
        """Keep exactly evidenced historic entity identities when improving bindings."""
        if not self._identities_ready:
            return {}
        key = _candidate_identity(source)
        explicit = self.settings["scanner_identity_recovery"].get(key)
        if explicit and (target := self._range_entity_identity(explicit["entity_id"])) is not None:
            identity = {"unique_id": target, "entity_id_address": target}
            if self._identities.get(key) != identity and (key in self._identities or len(self._identities) < 128):
                self._identities[key] = identity
                self._identity_dirty = True
            return deepcopy(identity)
        if key not in self._identities:
            from .api import scanner_identity_from_registry_entry  # noqa: PLC0415

            registry = self.coordinator.dr
            connections = {(dr.CONNECTION_BLUETOOTH, source.upper())}
            if hasattr(registry, "async_get_devices"):
                entries = registry.async_get_devices(connections=connections)
            else:
                entries = registry.devices.get_entries(None, connections=connections)
            evidenced = [
                identity
                for historical in entries
                if (identity := scanner_identity_from_registry_entry(historical, source)) is not None
            ]
            preserved = evidenced[0] if evidenced and all(identity == evidenced[0] for identity in evidenced) else None
            if not evidenced:
                sources = self._range_entity_sources()
                candidates = [identity for identity, owners in sources.items() if owners == {key}]
                claimed = {
                    _candidate_identity(value["entity_id_address"])
                    for owner, value in self._identities.items()
                    if owner != key
                }
                if len(candidates) == 1 and _candidate_identity(candidates[0]) not in claimed:
                    preserved = {"unique_id": candidates[0], "entity_id_address": candidates[0]}
            identity = preserved or {"unique_id": proposed_uid, "entity_id_address": proposed_uid}
            if len(self._identities) < 128:
                self._identities[key] = identity
                self._identity_dirty = True
            return deepcopy(identity)
        return deepcopy(self._identities[key])

    def _range_entity_identity(self, entity_id: str) -> str | None:
        """Read a per-receiver suffix only from this entry's actual beacon device."""
        registry = er.async_get(self.hass)
        entity = registry.async_get(entity_id)
        if entity is None or entity.platform != DOMAIN or entity.config_entry_id != self.entry.entry_id:
            return None
        unique_id = entity.unique_id
        if (
            not entity_id.startswith("sensor.")
            or not isinstance(unique_id, str)
            or not unique_id.endswith(("_range", "_range_raw"))
        ):
            return None
        suffix = "_range_raw" if unique_id.endswith("_range_raw") else "_range"
        device = self.coordinator.dr.async_get(entity.device_id) if entity.device_id else None
        if device is None:
            return None
        owner = getattr(device, "config_entry_id", None)
        if self.entry.entry_id not in ((owner,) if owner else getattr(device, "config_entries", ())):
            return None
        identities = {
            unique_id[len(identifier) + 1 : -len(suffix)]
            for domain, identifier in device.identifiers
            if domain == DOMAIN and unique_id.startswith(f"{identifier}_")
        }
        if len(identities) != 1:
            return None
        identity = identities.pop()
        return identity if 0 < len(identity) <= 256 else None

    def _range_entity_sources(self) -> dict[str, set[str]]:
        """Require exact current or restored source attributes; neighboring MACs are not proof."""
        stored = restore_state.async_get(self.hass).last_states
        sources: dict[str, set[str]] = {}
        for entity in er.async_entries_for_config_entry(er.async_get(self.hass), self.entry.entry_id):
            identity = self._range_entity_identity(entity.entity_id)
            if identity is None:
                continue
            previous = stored.get(entity.entity_id)
            for state in (self.hass.states.get(entity.entity_id), previous.state if previous else None):
                source = state.attributes.get("area_scanner_mac") if state else None
                if isinstance(source, str) and source:
                    sources.setdefault(identity, set()).add(_candidate_identity(source))
        return sources

    def _validate_identity_recovery(self, settings: dict) -> None:
        """Validate deliberate associations without changing any live or persisted identity."""
        recoveries = settings["scanner_identity_recovery"]
        if not recoveries:
            return
        known = {
            _candidate_identity(scanner.address) for scanner in self.coordinator.get_scanners
        } | self._identities.keys()
        sources = self._range_entity_sources()
        resolved = {}
        for source, recovery in recoveries.items():
            identity = self._range_entity_identity(recovery["entity_id"])
            if source not in known or identity is None:
                msg = "Choose a known scanner and an existing Bermuda range entity from this entry"
                raise ValueError(msg)
            if sources.get(identity, {source}) != {source}:
                msg = "The selected range entity has evidence of a different scanner"
                raise ValueError(msg)
            resolved[source] = identity
        claims = {
            owner: value["entity_id_address"] for owner, value in self._identities.items() if owner not in resolved
        }
        claims.update(resolved)
        if len({_candidate_identity(identity) for identity in claims.values()}) != len(claims):
            msg = "Multiple scanners cannot claim the same existing entity identity"
            raise ValueError(msg)

    def _device_id(self, advert: Any) -> str:
        return self._owners.get(advert, public_device_id(advert.device_address))

    def effective_advert_model(self, advert: Any, device_id: str | None = None) -> dict:
        """Resolve explicit profiles without changing legacy power precedence."""
        options = self.coordinator.options
        reference = options.get(CONF_REF_POWER, DEFAULT_REF_POWER)
        offset = receiver_value(options.get(CONF_RSSI_OFFSETS, {}), advert.scanner_address, 0.0)
        owner = _candidate_identity(device_id or self._device_id(advert))
        settings = self.settings
        fixed = settings["fixed_references"].get(owner, {})
        if "reference_power" in fixed:
            settings = deepcopy(settings)
            existing = settings["beacon_profiles"].get(owner, {})
            settings["beacon_profiles"][owner] = _fixed_reference_profile(fixed, existing)
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
        reference = self.settings["fixed_references"].get(device_id)
        if reference:
            kwargs["expected_distance"] = receiver_value(reference["ranges"], advert.scanner_address)
            kwargs["expected_accuracy_m"] = reference["accuracy_m"]
        if new_stamp is not None:
            quality = state.process(
                advert.rssi, new_stamp, now, observed=time.time() - max(0, now - new_stamp), **kwargs
            )
        else:
            quality = state.snapshot(now, **kwargs)
            last = self._quality.get(key, {})
            reasons = {reason.get("code"): reason for reason in last.get("reasons", []) + quality.get("reasons", [])}
            quality["reasons"] = list(reasons.values())[:32]
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
            advert.hist_distance_by_interval.insert(0, advert.rssi_distance)
            del advert.hist_distance_by_interval[32:]
        return True

    def snapshot_metadata(self, device_id: str, scanner_id: str, advert: Any) -> dict:
        """Publish effective settings and source-clock quality, independently of capture time."""
        # Untracked source aliases can expose the canonical owner's advert.
        # Report the model that actually produced its ranges and filter state.
        device_id = self._device_id(advert)
        state = self._states.get((device_id, scanner_id))
        model = self.effective_advert_model(advert)
        reference = self.settings["fixed_references"].get(device_id, {})
        quality = (
            state.snapshot(
                monotonic_time_coarse(),
                raw_distance=getattr(advert, "rssi_distance_raw", None),
                attenuation=model["attenuation"],
                expected_distance=receiver_value(reference.get("ranges", {}), scanner_id),
                expected_accuracy_m=reference.get("accuracy_m"),
            )
            if state
            else {"reasons": [{"code": "not_processed"}], "reliability": None}
        )
        last = self._quality.get((device_id, scanner_id), {})
        reasons = {reason.get("code"): reason for reason in last.get("reasons", []) + quality.get("reasons", [])}
        quality["reasons"] = list(reasons.values())[:32]
        if state and state.mode == "legacy":
            quality["filtered_rssi"] = None  # Legacy smooths ranges; it does not produce an RSSI estimate.
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
        """Classify current source data even while the asynchronous recorder is busy."""
        device_id = public_device_id(device)
        epoch, nowstamp = time.time(), monotonic_time_coarse()
        selected = {}
        for advert in getattr(device, "adverts", {}).values():
            scanner, stamp, rssi = advert.scanner_address, getattr(advert, "stamp", None), getattr(advert, "rssi", None)
            if not finite_number(stamp) or not finite_number(rssi) or stamp <= 0:
                continue
            previous = selected.get(scanner)
            if previous is not None and previous["stamp"] >= stamp:
                continue
            if scanner not in selected and len(selected) >= 128:
                continue
            source_age = nowstamp - stamp
            age = normalize_source_age(source_age)
            selected[scanner] = {
                "device_id": device_id,
                "scanner_id": scanner,
                "stamp": stamp,
                "rssi": rssi,
                "observed": epoch - age if age is not None else epoch,
                "age": age if age is not None else source_age,
                "status": "current" if age is not None else "stale",
                "model_version": self.version(),
            }
        result = self.fingerprints.suggest(device_id, list(selected.values()), now=epoch)
        self._suggestions[device_id] = result
        return {**deepcopy(result), "enabled": self.fingerprint_area_selection}

    def _snapshot(self) -> dict:
        snapshot = async_get_advert_snapshot(self.hass, entry_id=self.entry.entry_id, include_empty=True)
        if snapshot is None:
            msg = "The Bermuda entry is not loaded"
            raise RuntimeError(msg)
        return snapshot

    def _history_live_snapshot(self) -> dict:
        """Inspect import clock boundaries without advancing live quality state."""
        states, version = self._states, self._version_cache
        self._states = {key: copy(state) for key, state in states.items()}
        try:
            return self._snapshot()
        finally:
            self._states, self._version_cache = states, version

    def _current_offsets(self) -> dict:
        legacy = self.coordinator.options.get(CONF_RSSI_OFFSETS, {})
        offsets = deepcopy(dict(legacy))
        sources = {key for key in (*legacy, *self.settings["receiver_profiles"]) if isinstance(key, str)}
        for key in tuple(sources):
            # Materialize canonical MAC IDs for frozen observations even when
            # their scanner is offline. Opaque source IDs remain case-sensitive.
            canonical = receiver_value({key: key.lower()}, key.lower())
            if canonical is not None:
                sources.add(canonical)
        sources.update(scanner.address for scanner in self.coordinator.get_scanners)
        for source in sources:
            # Resolve the original maps separately: merging profile aliases into
            # legacy aliases changes exact-key precedence and ambiguity handling.
            offsets[source] = effective_model(
                self.settings,
                source,
                "",
                reference_power=self.coordinator.options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
                attenuation=self.coordinator.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
                rssi_offset=receiver_value(legacy, source, 0.0),
            )["rssi_offset"]
        return offsets

    def _state(self) -> dict:
        snapshot = self._snapshot()
        for device in self.coordinator.devices.values():
            if getattr(device, "create_sensor", False):
                self.area_suggestion(device)
        areas = [{"id": area.id, "name": area.name} for area in self.coordinator.ar.async_list_areas()]
        return {
            "snapshot": snapshot,
            "options": deepcopy(self.settings),
            "receiver_models": self._receiver_models(snapshot),
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
                "scanner_hardware": {
                    key: deepcopy(scanner["hardware"]) for key, scanner in snapshot["scanners"].items()
                },
                "notes": [
                    "Range error does not establish map position accuracy",
                    "Receiver health describes observations, not proxy connectivity",
                ],
            },
        }

    def _receiver_models(self, snapshot: dict) -> dict:
        """Show inherited receiver settings without creating profile overrides."""
        options = self.coordinator.options
        legacy_offsets = options.get(CONF_RSSI_OFFSETS, {})
        result = {}
        for scanner_id in snapshot["scanners"]:
            profile = receiver_value(self.settings["receiver_profiles"], scanner_id, {})
            legacy_offset = receiver_value(legacy_offsets, scanner_id)
            inherited = effective_model(
                {"receiver_profiles": {}, "beacon_profiles": {}},
                scanner_id,
                "",
                reference_power=options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
                attenuation=options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
                rssi_offset=legacy_offset if finite_number(legacy_offset) else 0.0,
            )
            model = effective_model(
                self.settings,
                scanner_id,
                "",
                reference_power=options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
                attenuation=options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
                rssi_offset=legacy_offset if finite_number(legacy_offset) else 0.0,
            )
            sources = {
                field: "receiver_profile" if field in profile else "global_default"
                for field in ("rssi_offset", "reference_power", "attenuation")
            }
            if "rssi_offset" not in profile and finite_number(legacy_offset):
                sources["rssi_offset"] = "legacy_receiver_offset"
            inherited_sources = dict.fromkeys(sources, "global_default")
            if finite_number(legacy_offset):
                inherited_sources["rssi_offset"] = "legacy_receiver_offset"
            result[scanner_id] = {
                **{field: model[field] for field in sources},
                "sources": sources,
                "inherited": {field: inherited[field] for field in sources},
                "inherited_sources": inherited_sources,
            }
        return result

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
        preserve_namespace: bool = False,
    ) -> None:
        """Durably prepare and acknowledge a recoverable normal HA options update."""
        baseline_options = deepcopy(dict(self.entry.options))
        previous_bindings = deepcopy(self.settings["scanner_bindings"])
        previous_recovery = deepcopy(self.settings["scanner_identity_recovery"])
        baseline_modified = self.entry.modified_at.isoformat()
        previous_journal = deepcopy(self._options_journal)
        before = self._trial_state()
        settings = _settings(options.get(OPTIONS_KEY))
        self._validate_identity_recovery(settings)
        options = deepcopy(options)
        namespace = dict(options.get(OPTIONS_KEY) or {}) if preserve_namespace else settings
        if preserve_namespace:
            for field in ("beacon_profiles", "filters", "fixed_references", "scanner_identity_recovery"):
                if field in namespace:
                    namespace[field] = deepcopy(settings[field])
        options[OPTIONS_KEY] = {**namespace, COMMIT_KEY: uuid.uuid4().hex}
        candidate_history = history or self.history
        if candidate_history.window_s != settings["history_window_s"]:
            history = deepcopy(candidate_history)
            history.reconfigure(window_s=settings["history_window_s"], now=time.time())
        payload = self._store_payload(trial=trial, replace_trial=replace_trial, history=history)
        if trial and replace_trial:
            payload["last_applied"] = trial["applied_at"]
            payload["last_data_id"] = trial["data_id"]
        after = {key: deepcopy(payload[key]) for key in before}
        if history is not None:
            before.update(
                {"history": self.history.export(redact=False), "history_clocks": self.history.export_clocks()}
            )
        # Compact ancestry retains commits that HA's one-second delayed write
        # may not have reached. A read-only acknowledgement permits safe pruning.
        disk = await self.hass.async_add_executor_job(
            _disk_options, self.hass.config.path(".storage", "core.config_entries"), self.entry.entry_id
        )
        if not _same_options(dict(self.entry.options), baseline_options) or self._closed:
            msg = "Options changed while preparing the commit"
            raise RuntimeError(msg)
        anchors = list((previous_journal or {}).get("anchors", []))
        acknowledged = (
            disk is not None
            and disk.get("modified_at") == baseline_modified
            and _same_options(disk["options"], baseline_options)
        )
        if acknowledged:
            anchors = []
        if previous_journal:
            previous_digest = previous_journal.get("desired_anchor")
            if isinstance(previous_digest, str) and previous_digest not in anchors:
                anchors.append(previous_digest)
        digest = _options_digest(baseline_options, baseline_modified)
        if digest not in anchors:
            anchors.append(digest)
        journal = {
            "phase": "prepared",
            "anchors": anchors,
            "desired": options,
            "previous_options": baseline_options,
            "before": before,
            "after": after,
        }
        journal["backup_trial"] = deepcopy(before.get("trial") or (previous_journal or {}).get("backup_trial"))
        if previous_journal and not acknowledged:
            journal["fallback"] = previous_journal
        if not self._valid_journal(journal):
            msg = "Wait for Home Assistant options to finish saving before another toolkit change"
            raise RuntimeError(msg)
        payload["options_journal"] = journal
        await self.store.async_save(payload)
        if not _same_options(dict(self.entry.options), baseline_options) or (
            expected_version is not None and self.version() != expected_version
        ):
            await self.store.async_save(self._store_payload())
            msg = "The radio model changed while saving; preview again"
            raise RuntimeError(msg)
        if self._closed:
            msg = "The Bermuda entry is unloading"
            raise RuntimeError(msg)
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        accepted = {
            key: deepcopy(value) for key, value in journal.items() if key not in ("fallback", "previous_options")
        }
        accepted["phase"] = "accepted"
        accepted["desired_anchor"] = _options_digest(options, self.entry.modified_at.isoformat())
        # Keep the accepted live pipeline processing actual packets until the
        # acknowledgement is durable. A provisional filter switch cannot be
        # undone by options compensation after it has discarded its old state.
        payload = self._store_payload(trial=trial, replace_trial=replace_trial, history=history)
        payload.update(options_journal=accepted, last_applied=after["last_applied"], last_data_id=after["last_data_id"])
        try:
            await self.store.async_save(payload)
        except (OSError, asyncio.CancelledError):
            self._options_journal = deepcopy(journal)
            if _same_options(dict(self.entry.options), options):
                self._options_journal["desired_anchor"] = _options_digest(options, self.entry.modified_at.isoformat())
                self.hass.config_entries.async_update_entry(self.entry, options=baseline_options)
            raise
        self._options_journal = accepted
        self._saved_options = deepcopy(options)
        # HA can accept a later external edit while the Store awaits I/O. Adopt
        # its actual current policy, without briefly resetting to the stale one.
        # Keep _saved_options on the witnessed intent so its listener still
        # records the later edit and requests any required legacy reload.
        latest = deepcopy(dict(self.entry.options))
        self._adopt_settings(settings if _same_options(latest, options) else _settings(latest.get(OPTIONS_KEY)), latest)
        if history is not None:
            history.reconfigure(window_s=self.settings["history_window_s"], now=time.time())
            history.enabled = self.settings["history_enabled"]
            self.history = history
        if replace_trial:
            self.trial = deepcopy(trial)
        self.last_applied, self.last_data_id = after["last_applied"], after["last_data_id"]
        self._notify_scanner_binding_change(previous_bindings)
        if (
            previous_recovery != self.settings["scanner_identity_recovery"]
            and not self._closed
            and not preserve_namespace
        ):
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    def _notify_scanner_binding_change(self, previous_bindings: dict) -> None:
        """Backfill receiver entities only after their binding is accepted."""
        if (
            not self._closed
            and self._available
            and self._identities_ready
            and previous_bindings != self.settings["scanner_bindings"]
        ):
            async_dispatcher_send(self.hass, SIGNAL_SCANNERS_CHANGED)

    def _adopt_settings(self, settings: dict, options: dict) -> None:
        # A selected alias may not have reached the next coordinator batch yet.
        # Establish its independent output before freezing the previous models.
        self.prepare_update()
        previous = self.version()
        binding_changed = settings["scanner_bindings"] != self.settings["scanner_bindings"]
        previous_filters = self.settings["filters"]
        changed_filters = {
            device_id
            for device_id in previous_filters.keys() | settings["filters"].keys()
            if previous_filters.get(device_id, "legacy") != settings["filters"].get(device_id, "legacy")
        }
        # Sources and metadevices can share the same advert object. Freeze its
        # old radio model/output once before publishing any replacement settings.
        adverts = {}
        advert_devices = {}
        for device in self.coordinator.devices.values():
            for advert in getattr(device, "adverts", {}).values():
                advert_devices.setdefault(id(advert), {})[id(device)] = device
                if id(advert) not in adverts:
                    adverts[id(advert)] = (
                        advert,
                        self._device_id(advert),
                        self.effective_advert_model(advert),
                        getattr(advert, "rssi_distance", None),
                        list(getattr(advert, "hist_distance", [])),
                        list(getattr(advert, "hist_distance_by_interval", [])),
                    )
        self.settings = settings
        self.coordinator.options[OPTIONS_KEY] = settings
        self.coordinator.options[CONF_RSSI_OFFSETS] = deepcopy(options.get(CONF_RSSI_OFFSETS, {}))
        self._version_cache = None
        self.history.window_s = settings["history_window_s"]
        self.history.enabled = settings["history_enabled"]
        self._generation += 1
        changed = self.version() != previous
        if changed:
            affected_devices = {}
            self.proposal = None
            self._proposal_rows = []
            for key in list(self._states):
                if key[0] in changed_filters:
                    del self._states[key]
            self._quality.clear()
            self._suggestions.clear()
            if self.shadow:
                self.shadow.cancel("live settings changed")
            radio_fields = ("reference_power", "attenuation", "rssi_offset")
            for advert, device_id, old_model, filtered, raw_history, filtered_history in adverts.values():
                advert.conf_rssi_offset = receiver_value(options.get(CONF_RSSI_OFFSETS, {}), advert.scanner_address, 0)
                new_model = self.effective_advert_model(advert)
                if any(old_model[field] != new_model[field] for field in radio_fields):
                    affected_devices.update(advert_devices[id(advert)])
                    # RSSI states are independent of the distance model. Preserve
                    # accepted/candidate RSSI, cadence and source freshness while
                    # reprojecting both raw and filtered distance histories.
                    if finite_number(getattr(advert, "rssi", None)):
                        advert.rssi_distance_raw = rssi_to_metres(
                            advert.rssi + new_model["rssi_offset"],
                            new_model["reference_power"],
                            new_model["attenuation"],
                        )
                    advert.rssi_distance = _rebase_distance(filtered, old_model, new_model)
                    advert.hist_distance[:] = _rebase_raw_history(
                        raw_history, getattr(advert, "hist_rssi", []), old_model, new_model
                    )
                    advert.hist_distance_by_interval[:] = [
                        _rebase_distance(value, old_model, new_model) for value in filtered_history
                    ]
                if device_id in changed_filters:
                    affected_devices.update(advert_devices[id(advert)])
                    # An explicit policy switch has a new pipeline, unlike a
                    # radio correction. Await its normal source-age processing.
                    advert.rssi_distance = None
                    advert.hist_distance_by_interval.clear()
                    advert.hist_velocity.clear()
            for device in affected_devices.values():
                device.model_revision = getattr(device, "model_revision", 0) + 1
                # Closest Distance consumes this revision only after the normal
                # selection pass publishes ranges from the adopted model.
                device.model_selection_pending = True
        # Manual scanner mappings affect association only; source keys and historic UIDs survive.
        if binding_changed:
            for scanner in self.coordinator.get_scanners:
                scanner.async_as_scanner_resolve_device_entries()

    async def async_options_changed(self, entry: Any) -> bool:
        """Handle toolkit/offset-only edits live; reload all other legacy option changes."""
        async with self._lock:
            if self._closed:
                return True
            previous_recovery = deepcopy(self.settings["scanner_identity_recovery"])
            new = deepcopy(dict(entry.options))
            excluded = {OPTIONS_KEY, CONF_RSSI_OFFSETS}
            old_other = {key: value for key, value in self._saved_options.items() if key not in excluded}
            new_other = {key: value for key, value in new.items() if key not in excluded}
            if new != self._saved_options:
                previous_bindings = deepcopy(self.settings["scanner_bindings"])
                backup = (self._options_journal or {}).get("backup_trial")
                if self.trial is None and isinstance(backup, dict) and self._trial_offsets_match(backup, new):
                    self.trial = deepcopy(backup)
                try:
                    await self._persist_options(new, preserve_namespace=True)
                except (OSError, RuntimeError):
                    if self._closed:
                        return True
                    # HA already accepted this external edit. A failed best-effort
                    # witness must not prevent its live corrections or reload.
                    latest = deepcopy(dict(entry.options))
                    self._adopt_settings(_settings(latest.get(OPTIONS_KEY)), latest)
                    self._saved_options = latest
                    self._notify_scanner_binding_change(previous_bindings)
                    self.last_result = {"reasons": ["external_options_witness_unavailable"]}
                    new_other = {key: value for key, value in latest.items() if key not in excluded}
                else:
                    latest = deepcopy(dict(entry.options))
                    if not _same_options(latest, self._saved_options):
                        previous_bindings = deepcopy(self.settings["scanner_bindings"])
                        self._adopt_settings(_settings(latest.get(OPTIONS_KEY)), latest)
                        self._saved_options = latest
                        self._notify_scanner_binding_change(previous_bindings)
                    new_other = {key: value for key, value in latest.items() if key not in excluded}
            if old_other != new_other or previous_recovery != self.settings["scanner_identity_recovery"]:
                return False
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
                                "session_epoch": frame["session_epoch"],
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

    def _validate_offsets(self, value: Any, *, frames: list[dict] | None = None) -> dict:
        if not isinstance(value, dict) or len(value) > 128:
            msg = "Candidate offsets must be a bounded object"
            raise ValueError(msg)
        known = (
            {scanner.address: scanner.address for scanner in self.coordinator.get_scanners}
            if frames is None
            else {
                _candidate_identity(row["scanner_id"]): _candidate_identity(row["scanner_id"])
                for frame in frames
                for row in frame["observations"]
            }
        )
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
            matches = [old for old in stored if receiver_value({key: True}, old, False)]
            for old in matches:
                stored[old] = value
            if not matches:
                stored[key] = value
            for old, profile in settings["receiver_profiles"].items():
                if receiver_value({key: True}, old, False):
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

    async def _rollback(self, *, disable_auto: bool = False) -> dict:
        expected_version = self.version()
        if not self.trial or self.trial.get("model_version") != expected_version:
            msg = "No applicable unchanged trial is available to roll back"
            raise RuntimeError(msg)
        previous = self.trial["previous_options"]
        options = deepcopy(dict(self.entry.options))
        options[CONF_RSSI_OFFSETS] = deepcopy(previous.get(CONF_RSSI_OFFSETS, {}))
        prior = _settings(previous.get(OPTIONS_KEY))
        settings = deepcopy(self.settings)
        profiles = settings["receiver_profiles"]
        prior_profiles = prior["receiver_profiles"]
        for scanner in self.trial["offsets"]:
            # Profiles may have been deleted or MAC-case renamed after apply
            # removed their numeric override. Match only the receiver identity;
            # retain current metadata and never restore old hardware descriptions.
            aliases = [key for key in profiles if receiver_value({scanner: True}, key, False)]
            old = receiver_value(prior_profiles, scanner, {})
            for key in aliases:
                profiles[key].pop("rssi_offset", None)
            if "rssi_offset" not in old:
                continue
            if scanner in aliases:
                target = scanner
            elif len(aliases) == 1:
                target = aliases[0]
            elif aliases:
                msg = "Ambiguous receiver profile aliases prevent rollback"
                raise RuntimeError(msg)
            else:
                keys = {key: key for key in prior_profiles}
                target = receiver_value(keys, scanner, scanner)
            profiles.setdefault(target, {})["rssi_offset"] = old["rssi_offset"]
        settings["filters"] = prior["filters"]
        if disable_auto:
            settings["auto_calibration"] = False
        options[OPTIONS_KEY] = settings
        await self._persist_options(options, trial=None, replace_trial=True, expected_version=expected_version)
        self.last_result = {"rolled_back": True, "model_version": self.version()}
        return deepcopy(self.last_result)

    def _candidate(self, payload: dict, *, frames: list[dict] | None = None) -> tuple[dict, dict]:
        """Bind read-only candidates to frozen inputs and shadow candidates to live identities."""
        value = payload.get("candidate")
        if value is None:
            value = (self.proposal or {}).get("offsets", {})
        modes = payload.get("filter_modes", {})
        settings = normalize_toolkit_settings({"filters": modes})
        known = (
            {public_device_id(key): public_device_id(key) for key in self.coordinator.devices}
            if frames is None
            else {
                _candidate_identity(row["device_id"]): _candidate_identity(row["device_id"])
                for frame in frames
                for row in frame["observations"]
            }
        )
        filters = {}
        for key, mode in settings["filters"].items():
            canonical = receiver_value(known, key)
            if canonical is None:
                msg = "Candidate filter refers to an unknown device"
                raise ValueError(msg)
            if canonical in filters and filters[canonical] != mode:
                msg = "Conflicting filter modes refer to the same device"
                raise ValueError(msg)
            filters[canonical] = mode
        return self._validate_offsets(value, frames=frames), filters

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
            and finite_number(row["rssi"])
            and finite_number(row["raw_distance"])
            and receiver_value(truth["ranges"], row["scanner_id"]) is not None
        ]
        labelled = {
            (row["device_id"], row["scanner_id"], row["stamp"])
            for frame in self.history.frames()
            if frame["session_epoch"] == self.history.session_epoch
            for row in frame["observations"]
            if any(
                label["device_id"] == row["device_id"] and row["scanner_id"] in label["ranges"]
                for label in frame["truth"]
            )
        }
        rows = [row for row in rows if (row["device_id"], row["scanner_id"], row["stamp"]) not in labelled]
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

    async def async_dispatch(self, action: str, payload: dict) -> dict:
        """Own one complete mutation until it commits, even if its caller disconnects."""
        if action in ("state", "diagnostics"):
            return await self._async_dispatch(action, payload)
        if self._action_task is not None and not self._action_task.done():
            msg = "A measurement action is already running"
            raise RuntimeError(msg)
        task = self.hass.async_create_background_task(
            self._async_dispatch(action, deepcopy(payload)), "bermuda toolkit action"
        )
        self._action_task = task

        def finished(result: asyncio.Task) -> None:
            if self._action_task is result:
                self._action_task = None
            if not result.cancelled():
                result.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._action_task is task:
                self._action_task = None

    async def _async_dispatch(self, action: str, payload: dict) -> dict:  # noqa: C901, PLR0911
        """Execute entry-scoped actions; the transport/service verifies administrator access."""
        if self._closed or not self._available:
            msg = "The Bermuda entry is loading or unloading"
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
                patch_maps = payload.get("patch_maps", False)
                if type(patch_maps) is not bool:
                    msg = "patch_maps must be a boolean"
                    raise ValueError(msg)
                if patch_maps:
                    if set(change) - {"receiver_limits", "filters"}:
                        msg = "Only receiver limits and device filters support map patches"
                        raise ValueError(msg)
                    patched = {}
                    for field, updates in change.items():
                        # Normalize just the patch, then merge into the current
                        # server map under the action lock. MAC case aliases
                        # update their existing slot; opaque IDs remain exact.
                        normalized_updates = _settings({field: updates})[field]
                        merged = deepcopy(self.settings[field])
                        aliases = {key: key for key in merged}
                        touched = set()
                        for key, value in normalized_updates.items():
                            slot = receiver_value(aliases, key, key)
                            if slot in touched and merged[slot] != value:
                                msg = "Map patch contains conflicting receiver aliases"
                                raise ValueError(msg)
                            merged[slot] = value
                            aliases[slot] = slot
                            touched.add(slot)
                        patched[field] = merged
                    change = patched
                previous_recovery = deepcopy(self.settings["scanner_identity_recovery"])
                options = deepcopy(dict(self.entry.options))
                options[OPTIONS_KEY] = _settings({**self.settings, **change})
                await self._persist_options(options)
                return {
                    "options": deepcopy(self.settings),
                    "reload_scheduled": previous_recovery != self.settings["scanner_identity_recovery"],
                }
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
                slot = (
                    receiver_value({existing: existing for existing in profiles}, key, key)
                    if kind == "receiver"
                    else _candidate_identity(public_device_id(key))
                )
                current = {**profiles.get(slot, {}), **profile}
                for field in remove:
                    current.pop(field, None)
                profiles[slot] = current
                options = {**deepcopy(dict(self.entry.options)), OPTIONS_KEY: _settings(settings)}
                await self._persist_options(options)
                return {"profile": deepcopy(self.settings[f"{kind}_profiles"][slot])}
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
                    history.start(selected_devices=payload.get("devices", []), clear=clear)
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
                snapshot = self._history_live_snapshot()
                replacement.retain_live_sources(self.history, snapshot_observations(snapshot), now=snapshot["captured"])
                replacement.enabled = self.settings["history_enabled"]
                await self.store.async_save(self._store_payload(history=replacement))
                self.history = replacement
                if self.shadow:
                    self.shadow.cancel("history was imported")
                self.proposal = None
                self._proposal_rows = []
                return self.history.summary()
            if action in ("replay", "shadow_start"):
                frames = self.history.frames()
                offsets, modes = self._candidate(payload, frames=frames if action == "replay" else None)
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
                shadow, version, generation = self.shadow, self.version(), self._generation
                result = await self._run_worker(deepcopy(shadow).promotion_check, current_model_version=version)
                if (
                    generation != self._generation
                    or version != self.version()
                    or shadow is not self.shadow
                    or self._closed
                ):
                    msg = "The shadow validation no longer matches the live model"
                    raise RuntimeError(msg)
                if not result["eligible"]:
                    return result
                try:
                    offsets, modes = self._candidate(
                        {"candidate": self.shadow.proposed_offsets, "filter_modes": self.shadow.filter_modes}
                    )
                    await self._apply(offsets, extra_settings={"filters": {**self.settings["filters"], **modes}})
                except Exception:
                    self.shadow.cancel("promotion could not be saved")
                    raise
                self.shadow.state = "promoted"
                return {"promoted": True, "validation": result}
            if action in ("fingerprint_train", "fingerprint_clear"):
                fingerprints = deepcopy(self.fingerprints)
                if action == "fingerprint_train":
                    device_id, location_id = payload.get("device_id"), payload.get("location_id")
                    if not self.coordinator.ar.async_get_area(location_id):
                        msg = "Select an existing Home Assistant area"
                        raise ValueError(msg)
                    snapshot = self._snapshot()
                    rows = [row for row in snapshot_observations(snapshot) if row["device_id"] == device_id]
                    result = fingerprints.train(
                        device_id,
                        location_id,
                        rows,
                        independent=True,
                        truth_source="measured",
                        now=snapshot["captured"],
                    )
                else:
                    result = {"cleared": fingerprints.clear(payload.get("device_id"), payload.get("location_id"))}
                await self.store.async_save(self._store_payload(fingerprints=fingerprints))
                self.fingerprints = fingerprints
                self._suggestions.clear()
                return result
            msg = "Unknown measurement action"
            raise ValueError(msg)

    def advert_uses_optional_filter(self, advert: Any) -> bool:
        """Resolve the filter policy for the advert's current tracked owner."""
        return self.settings["filters"].get(self._device_id(advert), "legacy") != "legacy"

    def rebase_advert_reference_power(self, advert: Any, value: float) -> bool:
        """Reproject an optional filter's ranges while preserving its RSSI pipeline."""
        if not self.advert_uses_optional_filter(advert):
            return False
        old_model = self.effective_advert_model(advert)
        advert.ref_power = value
        new_model = self.effective_advert_model(advert)
        if old_model != new_model:
            self._project_output(advert, old_model, new_model)
        return True

    @staticmethod
    def _project_output(advert: Any, old_model: dict, new_model: dict) -> None:
        """Move derived distance state between models without changing packet state."""
        if finite_number(getattr(advert, "rssi", None)):
            advert.rssi_distance_raw = rssi_to_metres(
                advert.rssi + new_model["rssi_offset"], new_model["reference_power"], new_model["attenuation"]
            )
        advert.rssi_distance = _rebase_distance(getattr(advert, "rssi_distance", None), old_model, new_model)
        for field in ("hist_distance", "hist_distance_by_interval"):
            history = getattr(advert, field, None)
            if isinstance(history, list):
                history[:] = (
                    _rebase_raw_history(history, getattr(advert, "hist_rssi", []), old_model, new_model)
                    if field == "hist_distance"
                    else [_rebase_distance(value, old_model, new_model) for value in history]
                )

    @staticmethod
    def _copy_source_reading(target: Any, source: Any, *, new_view: bool) -> None:
        """Copy packet evidence without sharing mutable histories or renewing its clock."""
        if target.scanner_device is not source.scanner_device:
            target.apply_new_scanner(source.scanner_device)
        fresh = source.new_stamp is not None and (
            new_view or not finite_number(target.stamp) or source.new_stamp > target.stamp
        )
        for field in ("rssi", "stamp", "tx_power", "stale_update_count"):
            setattr(target, field, getattr(source, field))
        for field in (
            "hist_rssi",
            "hist_stamp",
            "hist_interval",
            "local_name",
            "manufacturer_data",
            "service_data",
            "service_uuids",
        ):
            setattr(target, field, list(getattr(source, field)))
        if fresh:
            target.new_stamp = source.new_stamp
        elif target.new_stamp != source.stamp:
            target.new_stamp = None

    def prepare_update(self) -> None:
        """Give simultaneous tracked aliases independent outputs from one physical reading."""
        configured = {public_device_id(key).lower() for key in self.coordinator.options.get(CONF_DEVICES, [])}
        groups = {}
        for key, device in self.coordinator.devices.items():
            owner = public_device_id(key)
            tracked = getattr(device, "create_sensor", False) or owner.lower() in configured
            for pair, advert in getattr(device, "adverts", {}).items():
                source = getattr(advert, "_source_advert", advert)
                groups.setdefault(source, []).append((device, pair, advert, owner, tracked))
        previous = self._owners
        owners = {}
        outputs = []
        refresh_raw = set()
        for source, entries in groups.items():
            tracked = {owner: (device, advert) for device, _pair, advert, owner, selected in entries if selected}
            source_owner = public_device_id(source.device_address)
            primary = source_owner if source_owner in tracked else next(iter(tracked), source_owner)
            old_model = self.effective_advert_model(source)
            if previous.get(source, source_owner) != primary:
                refresh_raw.add(source)
            # If an independent alias becomes the sole owner, preserve its
            # accepted output rather than restoring the other owner's pipeline.
            surviving = tracked.get(primary, (None, source))[1]
            if surviving is not source and getattr(surviving, "_source_advert", None) is source:
                refresh_raw.add(source)
                old_model = self.effective_advert_model(surviving)
                for field in (
                    "ref_power",
                    "rssi_distance",
                    "hist_distance",
                    "hist_distance_by_interval",
                    "hist_velocity",
                ):
                    value = getattr(surviving, field)
                    setattr(source, field, list(value) if isinstance(value, list) else value)
            if tracked:
                owners[source] = primary
                if len(tracked) > 1:
                    source.ref_power = tracked[primary][0].ref_power
            outputs.append((source, old_model))
            views = {}
            for device, pair, advert, owner, selected in entries:
                target = source
                if selected and owner != primary:
                    target = views.get(owner)
                    if target is None:
                        target = advert if getattr(advert, "_source_advert", None) is source else copy(source)
                        new_view = target is not advert
                        view_model = old_model if new_view else self.effective_advert_model(target)
                        if new_view:
                            for field, value in vars(target).items():
                                if isinstance(value, list):
                                    setattr(target, field, list(value))
                            target._source_advert = source  # noqa: SLF001
                        self._copy_source_reading(target, source, new_view=new_view)
                        target.ref_power = device.ref_power
                        owners[target] = owner
                        outputs.append((target, view_model))
                        views[owner] = target
                if advert is not target:
                    device.adverts[pair] = target
                    if getattr(device, "area_advert", None) is advert:
                        device.area_advert = target
        self._owners = owners
        for advert, old_model in outputs:
            new_model = self.effective_advert_model(advert)
            if old_model != new_model:
                self._project_output(advert, old_model, new_model)
            if getattr(advert, "_source_advert", None) is not None or advert in refresh_raw:
                # Raw histories always come from the physical packets, converted
                # with this owner's model; cached copies add no source samples.
                advert.hist_distance[:] = [
                    rssi_to_metres(
                        rssi + new_model["rssi_offset"], new_model["reference_power"], new_model["attenuation"]
                    )
                    for rssi in advert.hist_rssi
                ]
                if finite_number(advert.rssi):
                    advert.rssi_distance_raw = rssi_to_metres(
                        advert.rssi + new_model["rssi_offset"], new_model["reference_power"], new_model["attenuation"]
                    )
        # Ownership metadata follows only current device/source references.
        removed = {(owner, advert.scanner_address) for advert, owner in previous.items()}
        active = {(owner, advert.scanner_address) for advert, owner in owners.items()}
        for key in removed - active:
            self._states.pop(key, None)
            self._quality.pop(key, None)

    def after_update(self, duration_s: float) -> None:
        """Measure completed batches and schedule at most one background recorder."""
        self._perf["updates"] += 1
        self._perf["last_update_s"] = duration_s
        self._perf["max_update_s"] = max(self._perf["max_update_s"], duration_s)
        self._version_cache = None
        if self._closed or not self._available:
            return
        if self._background and not self._background.done():
            self._perf["background_skipped"] += 1
            return
        self._background = self.hass.async_create_background_task(self._after_update(), "bermuda measurement tools")

    async def _after_update(self) -> None:
        """Record real fresh source data; inferred dog locations never become truth."""
        try:
            async with self._lock:
                if self._closed or not self._available:
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
                # Prevent repeated oscillation until an administrator explicitly re-enables auto mode.
                await self._rollback(disable_auto=True)
                return
        proposal = await self._preview()
        consumed = self.last_data_id
        # Renumbering retained epochs changes signatures, not received evidence.
        # A consumed proposal still needs a genuinely newer eligible source UTC.
        if (
            consumed
            and self._history_epoch_rebased_at is not None
            and (
                not finite_number(proposal.get("latest_observed"))
                or proposal["latest_observed"] <= self._history_epoch_rebased_at
            )
        ):
            consumed = proposal["data_id"]
        decision = self.engine.automatic_decision(
            proposal,
            now=time.time(),
            model_version=self.version(),
            current_offsets=self._current_offsets(),
            last_applied=self.last_applied,
            last_data_id=consumed,
            observations=self._proposal_rows,
            verified_reference_devices=self._verified_devices(),
        )
        self.last_result = decision
        if decision["apply"]:
            await self._apply(decision["offsets"], automatic=True)
