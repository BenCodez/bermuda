"""
Strict JSON profiles and effective radio models for the measurement toolkit.

Hardware and antenna descriptions are metadata. Only explicitly supplied numeric
model parameters change the range calculation; unset values preserve Bermuda's
existing per-device reference power and per-receiver RSSI offset.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from .const import DEFAULT_ATTENUATION, DEFAULT_REF_POWER

MAX_PROFILES = 128
MAX_ID_LENGTH = 256
MAX_TEXT_LENGTH = 512
_PROFILE_TEXT = frozenset({"hardware_type", "manufacturer", "model", "antenna", "environment", "notes"})
_PROFILE_NUMBERS = {
    "reference_power": (-120.0, 0.0),
    "attenuation": (0.1, 8.0),
    "rssi_offset": (-30.0, 30.0),
    "height_m": (0.0, 100.0),
    "antenna_gain_dbi": (-30.0, 30.0),
    "distance_accuracy_m": (0.0, 100.0),
}
_PROFILE_BOOLS = frozenset({"reference_power_verified"})
_SETTINGS_KEYS = frozenset(
    {
        "receiver_profiles",
        "beacon_profiles",
        "filters",
        "receiver_limits",
        "auto_calibration",
        "fingerprint_area_selection",
        "fixed_references",
        "history_enabled",
        "history_window_s",
        "scanner_bindings",
        "reliability_weighting",
    }
)


def finite_number(value: Any) -> bool:
    """Return whether a JSON value is a real, finite number, excluding booleans."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _number(value: Any, low: float, high: float, field: str) -> float:
    if not finite_number(value) or not low <= value <= high:
        msg = f"{field} must be a finite number between {low:g} and {high:g}"
        raise ValueError(msg)
    return float(value)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH or any(ord(c) < 32 for c in value):
        msg = f"{field} must be a nonempty identifier of at most {MAX_ID_LENGTH} characters"
        raise ValueError(msg)
    return value


def _mapping(value: Any, field: str, maximum: int = MAX_PROFILES) -> Mapping:
    if not isinstance(value, Mapping) or len(value) > maximum:
        msg = f"{field} must be an object containing at most {maximum} entries"
        raise ValueError(msg)
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{field} must be a boolean"
        raise ValueError(msg)  # noqa: TRY004 - one JSON schema validation exception contract
    return value


def normalize_profile(value: Mapping, *, kind: str = "receiver") -> dict:
    """
    Validate and detach an optional receiver or beacon radio profile.

    ``reference_power_verified`` is a deliberate user assertion that the supplied
    reference is measured or independently calibrated, not an advertised TX label.
    A metadata-only profile never creates a numeric correction.
    """
    if not isinstance(kind, str) or kind not in {"receiver", "beacon"}:
        msg = "kind must be receiver or beacon"
        raise ValueError(msg)
    profile = _mapping(value, "profile", 20)
    allowed = _PROFILE_TEXT | _PROFILE_NUMBERS.keys() | _PROFILE_BOOLS | {"antenna_gain_db"}
    if set(profile) - allowed:
        msg = "Unknown profile fields"
        raise ValueError(msg)
    result = {}
    for raw_key, item in profile.items():
        key = raw_key
        # Preserve compatibility with the initial dB metadata spelling. Antenna
        # gain is always metadata in dBi and is never an RSSI correction.
        if key == "antenna_gain_db":
            if "antenna_gain_dbi" in profile:
                msg = "Specify only one antenna gain field"
                raise ValueError(msg)
            key = "antenna_gain_dbi"
        if key in _PROFILE_TEXT:
            if not isinstance(item, str) or len(item) > MAX_TEXT_LENGTH or any(ord(c) < 32 and c != "\n" for c in item):
                msg = f"{key} must be text of at most {MAX_TEXT_LENGTH} characters"
                raise ValueError(msg)
            if key == "environment" and item not in {"indoor", "outdoor", "unknown"}:
                msg = "environment must be indoor, outdoor, or unknown"
                raise ValueError(msg)
            result[key] = item
        elif key in _PROFILE_NUMBERS:
            # Null means unset, including a receiver offset following legacy
            # corrections. Do not invent a zero when a form clears this field.
            if item is not None:
                result[key] = _number(item, *_PROFILE_NUMBERS[key], key)
        else:
            result[key] = _boolean(item, key)
    if result.get("reference_power_verified") and "reference_power" not in result:
        msg = "A verified reference requires explicit reference_power"
        raise ValueError(msg)
    return result


def normalize_fixed_reference(value: Mapping, *, device_id: str | None = None) -> dict:
    """Validate an explicit fixed beacon position using measured slant ranges."""
    reference = _mapping(value, "fixed_reference", 10)
    allowed = {"device_id", "location_id", "ranges", "accuracy_m", "reference_power", "reference_power_verified"}
    if set(reference) - allowed:
        msg = "Unknown fixed reference fields"
        raise ValueError(msg)
    result = {
        "location_id": _identifier(reference.get("location_id"), "location_id"),
        "accuracy_m": _number(reference.get("accuracy_m", 0.0), 0.0, 100.0, "accuracy_m"),
        "ranges": {},
    }
    supplied_id = reference.get("device_id", device_id)
    if supplied_id is not None:
        result["device_id"] = _identifier(supplied_id, "device_id")
        if device_id is not None and supplied_id != device_id:
            msg = "Fixed reference device_id does not match its key"
            raise ValueError(msg)
    ranges = _mapping(reference.get("ranges"), "ranges")
    if not ranges:
        msg = "Fixed reference needs at least one measured range"
        raise ValueError(msg)
    for scanner, distance in ranges.items():
        scanner_id = _identifier(scanner, "scanner_id")
        # One physical receiver must have one measured truth before live
        # lookups or recording can select different case representations.
        scanner_id = receiver_value({scanner_id.lower(): scanner_id.lower()}, scanner_id, scanner_id)
        measured = _number(distance, 0.05, 1000.0, "range")
        if scanner_id in result["ranges"] and result["ranges"][scanner_id] != measured:
            msg = "Fixed reference contains conflicting receiver aliases"
            raise ValueError(msg)
        result["ranges"][scanner_id] = measured
    if "reference_power" in reference:
        result["reference_power"] = _number(reference["reference_power"], -120.0, 0.0, "reference_power")
    if "reference_power_verified" in reference:
        result["reference_power_verified"] = _boolean(reference["reference_power_verified"], "reference_power_verified")
    if result.get("reference_power_verified") and "reference_power" not in result:
        msg = "A verified fixed reference requires explicit reference_power"
        raise ValueError(msg)
    return result


def normalize_toolkit_settings(value: Mapping | None) -> dict:
    """
    Validate the isolated JSON options namespace without mutating its input.

    Callers merging partial settings must merge into the previous namespace before
    calling this function. Unknown keys and nonfinite/boolean numeric values fail
    atomically, rather than silently becoming corrections.
    """
    source = _mapping({} if value is None else value, "measurement_toolkit", len(_SETTINGS_KEYS))
    if set(source) - _SETTINGS_KEYS:
        msg = "Unknown measurement toolkit settings"
        raise ValueError(msg)
    result = {
        "receiver_profiles": {},
        "beacon_profiles": {},
        "filters": {},
        "receiver_limits": {},
        "auto_calibration": False,
        "fingerprint_area_selection": False,
        "fixed_references": {},
        "history_enabled": False,
        "history_window_s": 3600.0,
        "scanner_bindings": {},
        "reliability_weighting": False,
    }
    for key, kind in (("receiver_profiles", "receiver"), ("beacon_profiles", "beacon")):
        for identifier, profile in _mapping(source.get(key, {}), key).items():
            result[key][_identifier(identifier, key)] = normalize_profile(profile, kind=kind)
    for identifier, mode in _mapping(source.get("filters", {}), "filters").items():
        if not isinstance(mode, str) or mode not in {"legacy", "pet", "stationary"}:
            msg = "Filter mode must be legacy, pet, or stationary"
            raise ValueError(msg)
        result["filters"][_identifier(identifier, "device_id")] = mode
    for identifier, limit in _mapping(source.get("receiver_limits", {}), "receiver_limits").items():
        result["receiver_limits"][_identifier(identifier, "scanner_id")] = _number(limit, 0.0, 1000.0, "receiver_limit")
    for identifier, reference in _mapping(source.get("fixed_references", {}), "fixed_references").items():
        result["fixed_references"][_identifier(identifier, "device_id")] = normalize_fixed_reference(
            reference, device_id=identifier
        )
    for identifier, binding in _mapping(source.get("scanner_bindings", {}), "scanner_bindings").items():
        fields = _mapping(binding, "scanner_binding", 1)
        if set(fields) != {"device_registry_id"}:
            msg = "A scanner binding must contain only device_registry_id"
            raise ValueError(msg)
        result["scanner_bindings"][_identifier(identifier, "scanner_id")] = {
            "device_registry_id": _identifier(fields["device_registry_id"], "device_registry_id")
        }
    for key in ("auto_calibration", "fingerprint_area_selection", "history_enabled", "reliability_weighting"):
        if key in source:
            result[key] = _boolean(source[key], key)
    if "history_window_s" in source:
        result["history_window_s"] = _number(source["history_window_s"], 60.0, 86400.0, "history_window_s")
    return result


def receiver_value(values: Mapping, scanner_id: str, default: Any = None) -> Any:
    """
    Look up a receiver case-insensitively only when it is a MAC address.

    Source IDs otherwise remain opaque. Existing uppercase MAC options continue
    applying to lowercase Bluetooth source IDs without re-keying unrelated data.
    """
    if scanner_id in values:
        return values[scanner_id]
    parts = scanner_id.split(":")
    if len(parts) == 6 and all(len(part) == 2 and all(c in "0123456789abcdefABCDEF" for c in part) for part in parts):
        canonical = scanner_id.lower()
        matches = [value for key, value in values.items() if isinstance(key, str) and key.lower() == canonical]
        if len(matches) == 1:
            return matches[0]
    return default


def _legacy_power(value: Any) -> float:
    return float(value) if finite_number(value) else float(DEFAULT_REF_POWER)


def _legacy_attenuation(value: Any) -> float:
    return float(value) if finite_number(value) and value > 0 else float(DEFAULT_ATTENUATION)


def _legacy_offset(value: Any) -> float:
    return float(value) if finite_number(value) else 0.0


def _legacy_device_power(value: Any) -> float:
    # Zero is Bermuda's unset per-device sentinel, so corrupt legacy values must
    # follow the global/receiver model rather than create a transmitter override.
    return float(value) if finite_number(value) else 0.0


def effective_model(
    settings: Mapping,
    scanner_id: str,
    device_id: str,
    *,
    reference_power: float,
    attenuation: float,
    rssi_offset: float,
    device_reference_power: float | None = None,
) -> dict:
    """
    Resolve explicit profiles over a caller's effective legacy model.

    Beacon profile power, a nonzero legacy device power, receiver profile power,
    and global power are applied in that order. Supply the optional legacy device
    value separately when receiver profiles and old per-device settings coexist.
    A profile RSSI offset explicitly replaces that receiver's legacy offset. The
    caller should persist calibration offsets in the same effective source.
    Antenna gain/type and hardware metadata do not participate in this model.
    """
    receiver = receiver_value(settings.get("receiver_profiles", {}), scanner_id, {})
    beacon = settings.get("beacon_profiles", {}).get(device_id, {})
    # Existing settings have deliberately broader semantics than new profiles:
    # rssi_to_metres supports every finite power/offset and positive exponent.
    # Applying profile bounds to these baselines would break legacy tracking.
    power = _legacy_power(reference_power)
    exponent = _legacy_attenuation(attenuation)
    offset = _legacy_offset(rssi_offset)
    if "reference_power" in receiver:
        power = _number(receiver["reference_power"], -120.0, 0.0, "reference_power")
    device_power = _legacy_device_power(device_reference_power)
    if device_power != 0:
        power = device_power
    if "reference_power" in beacon:
        power = _number(beacon["reference_power"], -120.0, 0.0, "reference_power")
    if "attenuation" in beacon:
        exponent = _number(beacon["attenuation"], 0.1, 8.0, "attenuation")
    if "attenuation" in receiver:
        exponent = _number(receiver["attenuation"], 0.1, 8.0, "attenuation")
    if "rssi_offset" in receiver:
        offset = _number(receiver["rssi_offset"], -30.0, 30.0, "rssi_offset")
    verified = beacon.get("reference_power_verified", False)
    return {
        "reference_power": power,
        "attenuation": exponent,
        "rssi_offset": offset,
        "reference_power_verified": verified is True,
    }


def model_version(
    settings: Mapping,
    legacy_offsets: Mapping | None = None,
    reference_power: float | None = None,
    attenuation: float | None = None,
    *,
    device_reference_powers: Mapping | None = None,
) -> str:
    """Return a deterministic signature of only range-affecting configuration."""
    keys = {"reference_power", "attenuation", "rssi_offset", "reference_power_verified"}
    profiles = {}
    for namespace, kind in (("receiver_profiles", "receiver"), ("beacon_profiles", "beacon")):
        profiles[namespace] = {}
        for identifier, profile in settings.get(namespace, {}).items():
            validated = normalize_profile(profile, kind=kind)
            numeric = {key: value for key, value in validated.items() if key in keys}
            if numeric:
                profiles[namespace][identifier] = numeric
    powers = {key: _legacy_device_power(value) for key, value in (device_reference_powers or {}).items()}
    model = {
        "profiles": profiles,
        "offsets": {key: _legacy_offset(value) for key, value in (legacy_offsets or {}).items()},
        "reference_power": _legacy_power(reference_power),
        "attenuation": _legacy_attenuation(attenuation),
        "device_reference_powers": {key: value for key, value in powers.items() if value != 0.0},
    }
    encoded = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]
