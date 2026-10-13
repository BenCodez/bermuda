"""Bounded descriptive hardware metadata from existing, proven HA identities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .profiles import MAX_TEXT_LENGTH, receiver_value

_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", re.IGNORECASE)
_PRIVATE_TEXT = re.compile(
    r"https?://|(?:\d{1,3}\.){3}\d{1,3}|[0-9a-f]{2}(?::[0-9a-f]{2}){5}|"
    r"[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,}|\b[0-9a-f]{32,64}\b",
    re.IGNORECASE,
)
_HARDWARE_FIELDS = ("manufacturer", "model", "hardware_type")


def hardware_text(value: Any) -> str | None:
    """Expose only bounded descriptive text, never addresses or identity keys."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(char for char in value[:MAX_TEXT_LENGTH] if char.isprintable() or char.isspace()).split())
    return text if text and not _PRIVATE_TEXT.search(text) else None


def _connections_match(device: Any, address: str) -> bool:
    if not _MAC.fullmatch(address):
        return False
    return any(
        kind in {"bluetooth", "mac"} and isinstance(value, str) and value.lower() == address.lower()
        for kind, value in (getattr(device, "connections", ()) or ())
    )


def _config_entries(coordinator: Any, device: Any) -> list:
    get_entry = getattr(getattr(getattr(coordinator, "hass", None), "config_entries", None), "async_get_entry", None)
    if not callable(get_entry):
        return []
    entry_id = getattr(device, "config_entry_id", None)
    ids = (entry_id,) if isinstance(entry_id, str) else getattr(device, "config_entries", ()) or ()
    return [entry for entry_id in sorted(ids) if (entry := get_entry(entry_id)) is not None]


def _esphome_source_matches(entry: Any, device: Any, address: str) -> bool:
    """Recheck explicit BLE/base evidence without accessing an ESPHome client."""
    data = getattr(entry, "data", None)
    info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
    ble_mac = (data.get("bluetooth_mac_address") if isinstance(data, Mapping) else None) or getattr(
        info, "bluetooth_mac_address", None
    )
    base_mac = getattr(info, "mac_address", None) or getattr(entry, "unique_id", None)
    return (
        isinstance(ble_mac, str)
        and ble_mac.lower() == address.lower()
        and isinstance(base_mac, str)
        and _connections_match(device, base_mac)
    )


def _scanner_entry(coordinator: Any, scanner: Any, address: str) -> Any:  # noqa: PLR0911
    registry = getattr(coordinator, "dr", None)
    get_device = getattr(registry, "async_get", None)
    if not callable(get_device):
        return None
    binding = getattr(scanner, "scanner_binding", None)
    binding = binding if isinstance(binding, Mapping) else {}
    options = getattr(coordinator, "options", None) or {}
    namespace = options.get("measurement_toolkit", {})
    bindings = namespace.get("scanner_bindings", {}) if isinstance(namespace, Mapping) else {}
    configured = receiver_value(bindings, address) if isinstance(bindings, Mapping) else None
    manual_id = configured.get("device_registry_id") if isinstance(configured, Mapping) else None
    if isinstance(manual_id, str) and manual_id:
        # A changed/removed association is unknown until the identity owner has
        # resolved it. Never reuse the metadata of the previous manual winner.
        if binding.get("method") != "manual" or binding.get("device_registry_id") != manual_id:
            return None
        return get_device(manual_id)
    if binding.get("method") == "manual":
        return None
    candidates = [
        entry_id
        for entry_id in (binding.get("device_registry_id"), getattr(scanner, "entry_id", None))
        if isinstance(entry_id, str)
    ]
    for entry_id in dict.fromkeys(candidates):
        if (device := get_device(entry_id)) is None:
            continue
        if _connections_match(device, address):
            return device
        if binding.get("method") == "esphome_bluetooth_mac" and any(
            entry.domain == "esphome" and _esphome_source_matches(entry, device, address)
            for entry in _config_entries(coordinator, device)
        ):
            return device
    return None


def _beacon_entry(coordinator: Any, address: str) -> Any:
    """Only an exact public MAC owned by another integration proves a beacon."""
    if not _MAC.fullmatch(address):
        return None
    registry = getattr(coordinator, "dr", None)
    if registry is None:
        return None
    connections = {("bluetooth", address.upper()), ("mac", address.lower())}
    lookup = getattr(registry, "async_get_devices", None)
    if callable(lookup):
        candidates = lookup(connections=connections)
    elif getattr(registry, "devices", None) is not None:
        candidates = registry.devices.get_entries(None, connections=connections)
    else:
        return None
    matched = [
        device
        for device in candidates
        if _connections_match(device, address)
        and any(entry.domain != "bermuda" for entry in _config_entries(coordinator, device))
    ]
    return matched[0] if len(matched) == 1 else None


def hardware_metadata(coordinator: Any, device: Any, *, kind: str = "receiver", device_id: str | None = None) -> dict:
    """
    Return detected and effective descriptions without touching the radio model.

    Registry and existing ESPHome runtime information are read on each snapshot,
    so removal, rebinding and late metadata changes cannot retain stale values.
    Antenna placement is never detectable: a missing manual bool means unknown.
    """
    address = getattr(device, "address", "")
    address = address if isinstance(address, str) else ""
    matched = (
        _scanner_entry(coordinator, device, address) if kind == "receiver" else _beacon_entry(coordinator, address)
    )
    detected = {}
    provenance = "unknown"
    if matched is not None:
        detected = {
            field: text
            for field in _HARDWARE_FIELDS
            if (text := hardware_text(getattr(matched, field, None))) is not None
        }
        if detected:
            provenance = "home_assistant_device_registry"
        if kind == "receiver":
            infos = [
                info
                for entry in _config_entries(coordinator, matched)
                if entry.domain == "esphome"
                and (info := getattr(getattr(entry, "runtime_data", None), "device_info", None)) is not None
            ]
            if len(infos) == 1:
                info = infos[0]
                for field in _HARDWARE_FIELDS:
                    value = getattr(info, field, None)
                    if field == "hardware_type":
                        value = value or getattr(info, "platform", None)
                    if field not in detected and (text := hardware_text(value)) is not None:
                        detected[field] = text
                        provenance = "esphome"
    options = getattr(coordinator, "options", None) or {}
    namespace = options.get("measurement_toolkit", {})
    profiles = namespace.get(f"{kind}_profiles", {}) if isinstance(namespace, Mapping) else {}
    profiles = profiles if isinstance(profiles, Mapping) else {}
    profile = receiver_value(profiles, address, {}) if kind == "receiver" else profiles.get(device_id or address, {})
    profile = profile if isinstance(profile, Mapping) else {}
    effective = dict(detected)
    for field in _HARDWARE_FIELDS:
        if field in profile:
            # Empty manual text is still an explicit override; callers can
            # remove that field to resume detection rather than guessing.
            text = hardware_text(profile[field])
            effective[field] = text or ""
    if isinstance(profile.get("external_antenna"), bool):
        effective["external_antenna"] = profile["external_antenna"]
    return {"detected": detected, "effective": effective, "provenance": provenance}
