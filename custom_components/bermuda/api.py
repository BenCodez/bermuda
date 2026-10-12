"""
Stable, detached measurements without enabling per-scanner entities.

Functions bearing the ``async_`` prefix are Home Assistant event-loop callbacks,
not coroutines. Subscribe to the returned coordinator's ``async_add_listener``
for updates. Distances always use metres; names and slugs are display metadata,
while device/scanner keys are stable Bermuda addresses. The version-1 PR834
fields remain available with additive measurement, provenance and limit fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import TYPE_CHECKING, Any, Final

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.util import slugify

from .const import (
    CONF_ATTENUATION,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    DEFAULT_ATTENUATION,
    DEFAULT_REF_POWER,
    DISTANCE_INFINITE,
    DISTANCE_TIMEOUT,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import BermudaDataUpdateCoordinator

SNAPSHOT_VERSION: Final = 1
MAX_DEVICES: Final = 256
MAX_SCANNERS: Final = 128
MAX_OBSERVATIONS: Final = 4096
MAX_HISTORY: Final = 5
MAX_ALIASES: Final = 32
MAX_TEXT: Final = 512
SOURCE_CLOCK_TOLERANCE_S: Final = 0.8
_PRIVATE_KEY_RE: Final = re.compile(r"^[0-9a-fA-F]{32}$")


def public_device_id(device_or_key: Any) -> str:
    """Return a stable public ID without exposing a private BLE identity key."""
    key = device_or_key if isinstance(device_or_key, str) else getattr(device_or_key, "address", "")
    if not isinstance(key, str):
        return ""
    if _PRIVATE_KEY_RE.fullmatch(key):
        return "private_" + hashlib.sha256(key.lower().encode()).hexdigest()[:32]
    return key


def resolve_public_device_id(coordinator: Any, key: str) -> str | None:
    """Resolve a public ID to the canonical device key, without re-keying it."""
    if not isinstance(key, str):
        return None
    for address in getattr(coordinator, "devices", None) or {}:
        if isinstance(address, str) and public_device_id(address) == key:
            return address
    return None


def scanner_identity_from_registry_entry(device_entry: Any, source: str) -> dict[str, str] | None:
    """Read a saved Bermuda identity only with exact Bluetooth source evidence."""
    connections = getattr(device_entry, "connections", ()) or ()
    if not any(
        kind == "bluetooth" and isinstance(address, str) and address.lower() == source.lower()
        for kind, address in connections
    ):
        return None
    identities = {
        identity
        for domain, identity in (getattr(device_entry, "identifiers", ()) or ())
        if domain == DOMAIN and isinstance(identity, str) and identity
    }
    if len(identities) != 1:
        return None
    identity = identities.pop()
    return {"unique_id": identity, "entity_id_address": identity}


def _public_text(value: Any, private_key: str) -> str | None:
    text = _text(value)
    if text is not None and _PRIVATE_KEY_RE.fullmatch(private_key):
        text = re.sub(re.escape(private_key), public_device_id(private_key), text, flags=re.IGNORECASE)
    return text


def _public_context(value: Any, private_key: str) -> Any:
    """Redact a private owner's key from optional model/quality diagnostics."""
    if not _PRIVATE_KEY_RE.fullmatch(private_key):
        return value
    if isinstance(value, str):
        return _public_text(value, private_key)
    if isinstance(value, dict):
        return {_public_text(key, private_key): _public_context(item, private_key) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_context(item, private_key) for item in value]
    return value


def _number(value: Any) -> float | None:
    """JSON finite numbers only; booleans are never readings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def _json_value(value: Any, depth: int = 0) -> Any:  # noqa: PLR0911
    """Detach optional diagnostics with strict depth, length and numeric bounds."""
    if depth > 4:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, Mapping):
        return {
            key[:MAX_TEXT]: _json_value(item, depth + 1)
            for key, item in islice(value.items(), 64)
            if isinstance(key, str)
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth + 1) for item in value[:64]]
    return None


@callback
def async_get_coordinator(hass: HomeAssistant, entry_id: str | None = None) -> BermudaDataUpdateCoordinator | None:
    """Return an entry's coordinator; an implicit choice must be unambiguous."""
    found = None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry_id is not None and entry.entry_id != entry_id:
            continue
        state = getattr(entry, "state", None)
        if state is not None and state not in {ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS}:
            continue
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
        if coordinator is not None and getattr(getattr(coordinator, "toolkit", None), "closed", False) is not True:
            if found is not None and entry_id is None:
                msg = "Select a Bermuda entry when multiple entries are loaded"
                raise ValueError(msg)
            found = coordinator
    return found


def _scanner_metadata(scanner_id: str, scanner: Any, nowstamp: float) -> dict[str, Any]:
    name = _text(getattr(scanner, "name", None)) or scanner_id
    last_seen = _number(getattr(scanner, "last_seen", None))
    aliases = getattr(scanner, "metadevice_sources", None)
    return {
        "scanner_id": scanner_id,
        "address": scanner_id,
        "name": name,
        "slug": slugify(name),
        "unique_id": _text(getattr(scanner, "unique_id", None)),
        "entity_id_address": _text(getattr(scanner, "entity_id_address", None)),
        "address_wifi_mac": _text(getattr(scanner, "address_wifi_mac", None)),
        "address_ble_mac": _text(getattr(scanner, "address_ble_mac", None)),
        "device_registry_id": _text(getattr(scanner, "entry_id", None)),
        "binding": _json_value(getattr(scanner, "scanner_binding", None)),
        "source_aliases": [_text(alias) for alias in (aliases or [])[:MAX_ALIASES] if isinstance(alias, str)],
        "area_id": _text(getattr(scanner, "area_id", None)),
        "area_name": _text(getattr(scanner, "area_name", None)),
        "scanner_last_seen_age": max(0.0, nowstamp - last_seen) if last_seen is not None and last_seen > 0 else None,
    }


def _model_metadata(coordinator: Any, device_id: str, scanner_id: str, advert: Any) -> dict[str, Any]:
    """Read current effective advert settings and the toolkit's optional context."""
    options = getattr(coordinator, "options", None) or {}
    offset = _number(getattr(advert, "conf_rssi_offset", None))
    if offset is None:
        offset = _number(options.get(CONF_RSSI_OFFSETS, {}).get(scanner_id, 0)) or 0.0
    reference = _number(getattr(advert, "ref_power", None))
    if reference is None or reference == 0:
        reference = _number(getattr(advert, "conf_ref_power", None))
    if reference is None:
        reference = _number(options.get(CONF_REF_POWER))
    if reference is None:
        reference = float(DEFAULT_REF_POWER)
    attenuation = _number(getattr(advert, "conf_attenuation", None))
    if attenuation is None or attenuation <= 0:
        attenuation = _number(options.get(CONF_ATTENUATION))
    if attenuation is None or attenuation <= 0:
        attenuation = float(DEFAULT_ATTENUATION)
    result = {"rssi_offset": offset, "reference_power": reference, "attenuation": attenuation}
    hook = getattr(getattr(coordinator, "toolkit", None), "snapshot_metadata", None)
    if callable(hook):
        metadata = hook(public_device_id(device_id), public_device_id(scanner_id), advert)
        if isinstance(metadata, Mapping):
            result.update(_json_value(metadata))
    for field in ("rssi_offset", "reference_power", "attenuation"):
        value = _number(result.get(field))
        if value is not None and (field != "attenuation" or value > 0):
            result[field] = value
        else:
            result[field] = {"rssi_offset": offset, "reference_power": reference, "attenuation": attenuation}[field]
    if not isinstance(result.get("model_version"), str):
        signature = {field: result[field] for field in ("rssi_offset", "reference_power", "attenuation")}
        result["model_version"] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
    return _public_context(result, device_id)


def _recent_history(advert: Any, nowstamp: float, observed_now: float) -> list[dict[str, Any]]:
    stamps = getattr(advert, "hist_stamp", None) or []
    readings = getattr(advert, "hist_rssi", None) or []
    distances = getattr(advert, "hist_distance", None) or []
    history = []
    for index, reading in enumerate(readings[:MAX_HISTORY]):
        stamp = _number(stamps[index]) if index < len(stamps) else None
        age = nowstamp - stamp if stamp is not None and stamp > 0 else None
        history.append(
            {
                "rssi": _number(reading),
                "raw_distance": _number(distances[index]) if index < len(distances) else None,
                "stamp": stamp,
                "observed": observed_now - age if age is not None and age >= 0 else None,
                "age": age,
                "model_provenance": "runtime_history_not_frozen",
            }
        )
    return history


def _advert_measurement(
    coordinator: Any,
    device_id: str,
    scanner_id: str,
    advert: Any,
    nowstamp: float,
    observed_now: float,
    *,
    include_history: bool,
) -> dict[str, Any]:
    metadata = _model_metadata(coordinator, device_id, scanner_id, advert)
    stamp = _number(getattr(advert, "stamp", None))
    age = nowstamp - stamp if stamp is not None and stamp > 0 else None
    observed = _number(metadata.get("observed", getattr(advert, "observed", None)))
    clock = "source_epoch" if observed is not None else "derived_monotonic"
    if observed is None and age is not None and age >= -SOURCE_CLOCK_TOLERANCE_S:
        # Coarse source and receiver clocks can differ slightly. Keep the raw
        # stamp for deduplication, but never publish derived UTC in the future.
        observed = observed_now - max(0.0, age)
    rssi = _number(getattr(advert, "rssi", None))
    raw_distance = _number(getattr(advert, "rssi_distance_raw", None))
    filtered_distance = _number(getattr(advert, "rssi_distance", None))
    quality = _json_value(metadata.get("quality", getattr(advert, "quality", None)))
    quality = quality if isinstance(quality, dict) else {}
    timeout = float(DISTANCE_TIMEOUT)
    adaptive_timeout = _number(quality.get("timeout_s"))
    if quality.get("mode") in {"pet", "stationary"} and adaptive_timeout is not None and 30 <= adaptive_timeout <= 120:
        timeout = adaptive_timeout
    reasons = []
    if age is None:
        reasons.append("missing_source_stamp")
    elif age < -SOURCE_CLOCK_TOLERANCE_S:
        reasons.append("source_clock_in_future")
    elif age > timeout:
        reasons.append("stale_observation")
    if rssi is None:
        reasons.append("missing_or_invalid_rssi")
    if raw_distance is None or raw_distance <= 0 or raw_distance == DISTANCE_INFINITE:
        raw_distance = None
        reasons.append("raw_range_unavailable")
    status = "missing" if age is None or rssi is None else "current"
    if "source_clock_in_future" in reasons or "stale_observation" in reasons:
        status = "stale"
    if filtered_distance is not None and (filtered_distance <= 0 or filtered_distance == DISTANCE_INFINITE):
        filtered_distance = None
    if status != "current":
        filtered_distance = None
    previous_reasons = quality.get("rejection_reasons")
    previous_reasons = (
        [item for item in previous_reasons if isinstance(item, str)] if isinstance(previous_reasons, list) else []
    )
    quality["rejection_reasons"] = list(dict.fromkeys(previous_reasons + reasons))[:64]
    quality["fresh"] = status == "current"
    unavailable = []
    filtered_rssi = _number(metadata.get("filtered_rssi", getattr(advert, "filtered_rssi", None)))
    if filtered_rssi is None:
        unavailable.append("filtered_rssi")
    if observed is None:
        unavailable.append("observed")
    return {
        "device_id": public_device_id(device_id),
        "scanner_id": public_device_id(scanner_id),
        "source_id": public_device_id(_text(getattr(advert, "device_address", None)) or device_id),
        "observed": observed,
        "observed_clock": clock,
        "stamp": stamp,
        "age": max(0.0, age) if age is not None and age >= -SOURCE_CLOCK_TOLERANCE_S else age,
        "status": status,
        "rssi": rssi,
        "raw_rssi": rssi,
        "filtered_rssi": filtered_rssi,
        "rssi_offset": metadata["rssi_offset"],
        "reference_power": metadata["reference_power"],
        "attenuation": metadata["attenuation"],
        "raw_distance": raw_distance,
        "filtered_distance": filtered_distance,
        "distance": filtered_distance,
        "distance_raw": raw_distance,
        "model_version": metadata["model_version"],
        "reference_power_verified": metadata.get("reference_power_verified", False) is True,
        "quality": quality,
        "unavailable": unavailable,
        "history": _recent_history(advert, nowstamp, observed_now) if include_history else None,
        "history_status": "runtime_recent" if include_history else "omitted",
        "history_omitted": max(0, len(getattr(advert, "hist_rssi", None) or []) - MAX_HISTORY)
        if include_history
        else None,
    }


@callback
def async_get_advert_snapshot(
    hass: HomeAssistant,
    addresses: Iterable[str] | None = None,
    *,
    entry_id: str | None = None,
    include_history: bool = False,
    tracked_only: bool = True,
    include_empty: bool = False,
    now: float | None = None,
) -> dict[str, Any] | None:
    """
    Return a bounded JSON snapshot, or None when the entry is unavailable.

    ``now`` is the capture UTC epoch, not a source monotonic stamp. ``observed``
    derives the actual packet's epoch from its source stamp when no epoch was
    provided; capturing repeatedly never makes a stale packet fresh. The optional
    five-reading history is runtime history and is explicitly not a frozen replay.
    No raw BLE payloads, IRK bytes or HA configuration data are exported.
    """
    coordinator = async_get_coordinator(hass, entry_id)
    if coordinator is None:
        return None
    observed_now = _number(now) if now is not None else time.time()
    if observed_now is None:
        msg = "now must be a finite epoch number"
        raise ValueError(msg)
    nowstamp = monotonic_time_coarse()
    wanted = (
        {public_device_id(address).lower() for address in addresses if isinstance(address, str)}
        if addresses is not None
        else None
    )
    registry = getattr(coordinator, "devices", None) or {}
    devices: dict[str, Any] = {}
    roster: dict[str, Any] = {}
    omitted = {"devices": 0, "scanners": 0, "observations": 0, "source_aliases": 0}
    observation_count = 0
    for address, device in registry.items():
        if not isinstance(address, str):
            continue
        if getattr(device, "is_scanner", False) is True:
            if len(roster) < MAX_SCANNERS:
                roster[address] = _scanner_metadata(address, device, nowstamp)
            else:
                omitted["scanners"] += 1
        device_id = public_device_id(address)
        if wanted is not None and device_id.lower() not in wanted:
            continue
        tracked = getattr(device, "create_sensor", False) is True
        if tracked_only and not tracked:
            continue
        adverts = getattr(device, "adverts", None) or {}
        if not adverts and not include_empty:
            continue
        if len(devices) >= MAX_DEVICES:
            omitted["devices"] += 1
            continue
        selected: dict[str, Any] = {}
        source_aliases: dict[str, list[str]] = {}
        for advert in adverts.values():
            scanner_id = _text(getattr(advert, "scanner_address", None))
            if scanner_id is None:
                omitted["observations"] += 1
                continue
            alias = public_device_id(_text(getattr(advert, "device_address", None)) or address)
            aliases = source_aliases.setdefault(scanner_id, [])
            if alias not in aliases:
                if len(aliases) < MAX_ALIASES:
                    aliases.append(alias)
                else:
                    omitted["source_aliases"] += 1
            previous = selected.get(scanner_id)
            if previous is None or (_number(getattr(advert, "stamp", None)) or 0) > (
                _number(getattr(previous, "stamp", None)) or 0
            ):
                selected[scanner_id] = advert
        scanners = {}
        for scanner_id, advert in selected.items():
            if len(scanners) >= MAX_SCANNERS or observation_count >= MAX_OBSERVATIONS:
                omitted["observations"] += 1
                continue
            scanner = registry.get(scanner_id) or getattr(advert, "scanner_device", None)
            measurement = _scanner_metadata(scanner_id, scanner, nowstamp)
            measurement.update(
                _advert_measurement(
                    coordinator, address, scanner_id, advert, nowstamp, observed_now, include_history=include_history
                )
            )
            measurement["source_aliases"] = source_aliases[scanner_id]
            scanners[scanner_id] = measurement
            observation_count += 1
        name = _public_text(getattr(device, "name", None), address) or device_id
        devices[device_id] = _public_context(
            {
                "device_id": device_id,
                "owner_id": device_id,
                "address": device_id,
                "name": name,
                "slug": slugify(name),
                "unique_id": _public_text(getattr(device, "unique_id", None), address),
                "source_aliases": [
                    public_device_id(alias)
                    for alias in (getattr(device, "metadevice_sources", None) or [])[:MAX_ALIASES]
                    if isinstance(alias, str)
                ],
                "tracked": tracked,
                "address_type": _text(getattr(device, "address_type", None)),
                "area_id": _text(getattr(device, "area_id", None)),
                "area_name": _text(getattr(device, "area_name", None)),
                "scanners": scanners,
            },
            address,
        )
    return {
        "version": SNAPSHOT_VERSION,
        "entry_id": _text(getattr(getattr(coordinator, "config_entry", None), "entry_id", None)) or entry_id,
        "stamp": nowstamp,
        "captured": observed_now,
        "clocks": {"stamp": "monotonic", "captured": "utc_epoch", "observed": "source_or_derived_utc_epoch"},
        "devices": devices,
        "scanners": roster,
        "omitted": omitted,
        "limits": {"devices": MAX_DEVICES, "scanners": MAX_SCANNERS, "observations": MAX_OBSERVATIONS},
        "history_status": "runtime_recent" if include_history else "omitted",
    }


def snapshot_observations(snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract detached shared observations from this versioned snapshot."""
    if not snapshot or snapshot.get("version") != SNAPSHOT_VERSION:
        return []
    observations = []
    fields = (
        "device_id",
        "scanner_id",
        "source_id",
        "observed",
        "observed_clock",
        "stamp",
        "rssi",
        "rssi_offset",
        "reference_power",
        "reference_power_verified",
        "attenuation",
        "raw_distance",
        "filtered_distance",
        "filtered_rssi",
        "age",
        "status",
        "quality",
        "model_version",
        "unavailable",
    )
    for device in (snapshot.get("devices") or {}).values():
        for scanner in (device.get("scanners") or {}).values():
            if len(observations) >= MAX_OBSERVATIONS:
                return observations
            observations.append({field: _json_value(scanner.get(field)) for field in fields})
    return observations
