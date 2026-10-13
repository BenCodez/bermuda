"""Synthetic measurements exercise the stable in-process API contract."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.api import (
    MAX_DEVICES,
    MAX_HISTORY,
    MAX_SCANNERS,
    async_get_advert_snapshot,
    async_get_coordinator,
    public_device_id,
    resolve_public_device_id,
    scanner_identity_from_registry_entry,
    snapshot_observations,
)
from custom_components.bermuda.const import DOMAIN

DEVICE = "aa:bb:cc:dd:ee:01"
SCANNER = "aa:bb:cc:dd:ee:12"


def _coordinator(hass, *, device_id=DEVICE, entry_id="measurements", count=1):
    scanner = SimpleNamespace(
        address=SCANNER,
        name="Synthetic Receiver",
        unique_id="aa:bb:cc:dd:ee:10",
        entity_id_address="aa:bb:cc:dd:ee:10",
        address_wifi_mac="aa:bb:cc:dd:ee:10",
        address_ble_mac=SCANNER,
        entry_id="receiver-registry",
        is_scanner=True,
        last_seen=100.0,
        create_sensor=False,
        adverts={},
        metadevice_sources=[],
        area_id="synthetic_room",
        area_name="Synthetic Room",
    )
    advert = SimpleNamespace(
        device_address=device_id,
        scanner_address=SCANNER,
        scanner_device=scanner,
        name="Old Receiver Name",
        stamp=98.0,
        rssi=-65.0,
        rssi_distance_raw=3.2,
        rssi_distance=3.0,
        ref_power=-59.0,
        conf_ref_power=-55.0,
        conf_attenuation=2.0,
        conf_rssi_offset=4.0,
        hist_stamp=[98.0, 97.0, 96.0],
        hist_rssi=[-65.0, -66.0, -67.0],
        hist_distance=[3.2, 3.4, 3.6],
    )
    device = SimpleNamespace(
        address=device_id,
        name="Synthetic Beacon",
        unique_id=device_id,
        create_sensor=True,
        is_scanner=False,
        address_type="public",
        area_id="synthetic_room",
        area_name="Synthetic Room",
        adverts={(device_id, SCANNER): advert},
    )
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, data={})
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(devices={device_id: device, SCANNER: scanner}, options={}, config_entry=entry)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    return coordinator, device, advert, scanner


def _snapshot(hass, **kwargs):
    with patch("custom_components.bermuda.api.monotonic_time_coarse", return_value=100.0):
        return async_get_advert_snapshot(hass, now=1000.0, **kwargs)


def test_unloaded_or_wrong_entry_is_unavailable(hass):
    assert async_get_coordinator(hass) is None
    assert _snapshot(hass) is None
    coordinator, _, _, _ = _coordinator(hass)
    assert async_get_coordinator(hass, "measurements") is coordinator
    assert async_get_coordinator(hass, "different") is None
    assert _snapshot(hass, entry_id="different") is None


def test_multiple_loaded_entries_require_explicit_scope(hass):
    first, _, _, _ = _coordinator(hass, entry_id="first")
    second, _, _, _ = _coordinator(hass, entry_id="second")
    with pytest.raises(ValueError, match="multiple entries"):
        async_get_coordinator(hass)
    with pytest.raises(ValueError, match="multiple entries"):
        _snapshot(hass)
    assert async_get_coordinator(hass, "first") is first
    assert async_get_coordinator(hass, "second") is second
    assert _snapshot(hass, entry_id="second")["entry_id"] == "second"


def test_snapshot_exposes_actual_measurements_clocks_and_owner(hass):
    _, _, _, _ = _coordinator(hass)
    snapshot = _snapshot(hass)
    reading = snapshot["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["rssi"] == reading["raw_rssi"] == -65.0
    assert reading["filtered_rssi"] is None
    assert reading["distance"] == reading["filtered_distance"] == 3.0
    assert reading["raw_distance"] == reading["distance_raw"] == 3.2
    assert reading["reference_power"] == -59.0
    assert reading["attenuation"] == 2.0
    assert reading["rssi_offset"] == 4.0
    assert reading["observed"] == 998.0
    assert reading["stamp"] == 98.0
    assert reading["age"] == 2.0
    assert snapshot["captured"] == 1000.0
    assert reading["status"] == "current"
    assert reading["name"] == "Synthetic Receiver"
    assert reading["entity_id_address"] == "aa:bb:cc:dd:ee:10"
    assert reading["device_registry_id"] == "receiver-registry"
    assert reading["source_aliases"] == [DEVICE]
    observations = snapshot_observations(snapshot)
    assert observations[0]["device_id"] == DEVICE
    assert observations[0]["scanner_id"] == SCANNER
    observations[0]["quality"]["fresh"] = False
    assert reading["quality"]["fresh"] is True


def test_snapshot_defaults_to_tracked_and_preserves_roster(hass):
    coordinator, device, _, _ = _coordinator(hass)
    device.create_sensor = False
    snapshot = _snapshot(hass)
    assert snapshot["devices"] == {}
    assert SCANNER in snapshot["scanners"]
    assert DEVICE in _snapshot(hass, tracked_only=False)["devices"]
    coordinator.devices["empty"] = SimpleNamespace(name="Empty", create_sensor=True, adverts={})
    assert "empty" not in _snapshot(hass)["devices"]
    assert "empty" in _snapshot(hass, include_empty=True)["devices"]


def test_case_insensitive_explicit_address_filter(hass):
    _coordinator(hass)
    assert list(_snapshot(hass, addresses={DEVICE.upper()})["devices"]) == [DEVICE]
    assert _snapshot(hass, addresses={"different"})["devices"] == {}


def test_repeated_capture_does_not_freshen_old_packet(hass):
    _, _, advert, _ = _coordinator(hass)
    advert.stamp = 60.0
    reading = _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["observed"] == 960.0
    assert reading["age"] == 40.0
    assert reading["status"] == "stale"
    assert reading["distance"] is None
    assert reading["raw_distance"] == 3.2
    assert "stale_observation" in reading["quality"]["rejection_reasons"]


@pytest.mark.parametrize("mode", ["pet", "stationary"])
def test_optional_slow_cadence_uses_bounded_filter_timeout(hass, mode):
    coordinator, _, advert, _ = _coordinator(hass)
    advert.stamp = 55.0
    coordinator.toolkit = SimpleNamespace(
        snapshot_metadata=lambda device, scanner, advert: {"quality": {"mode": mode, "timeout_s": 60.0}}
    )
    reading = _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["age"] == 45.0
    assert reading["status"] == "current"
    assert reading["filtered_distance"] == 3.0
    advert.stamp = 39.0
    reading = _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["age"] == 61.0
    assert reading["status"] == "stale"
    assert reading["filtered_distance"] is None


@pytest.mark.parametrize("mode,timeout", [("legacy", 60.0), ("pet", 121.0), ("stationary", True)])
def test_legacy_and_invalid_adaptive_timeouts_keep_legacy_freshness(hass, mode, timeout):
    coordinator, _, advert, _ = _coordinator(hass)
    advert.stamp = 55.0
    coordinator.toolkit = SimpleNamespace(
        snapshot_metadata=lambda device, scanner, advert: {"quality": {"mode": mode, "timeout_s": timeout}}
    )
    assert _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]["status"] == "stale"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 10**1000, True, "-65", None])
def test_invalid_readings_are_null_and_json_safe(hass, invalid):
    _, _, advert, _ = _coordinator(hass)
    advert.rssi = advert.rssi_distance = advert.rssi_distance_raw = invalid
    snapshot = _snapshot(hass)
    reading = snapshot["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["rssi"] is None
    assert reading["raw_distance"] is None
    assert reading["filtered_distance"] is None
    assert reading["status"] == "missing"
    json.dumps(snapshot, allow_nan=False)


def test_future_or_absent_clock_is_explicitly_rejected(hass):
    _, _, advert, _ = _coordinator(hass)
    advert.stamp = 0.0
    reading = _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["observed"] is None
    assert reading["age"] is None
    assert reading["status"] == "missing"
    advert.stamp = 103.0
    reading = _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["observed"] is None
    assert reading["status"] == "stale"
    assert "source_clock_in_future" in reading["quality"]["rejection_reasons"]


def test_runtime_history_is_bounded_and_not_claimed_frozen(hass):
    _, _, advert, _ = _coordinator(hass)
    advert.hist_stamp = [98.0] * 40
    advert.hist_rssi = [-65.0] * 40
    advert.hist_distance = [3.2] * 40
    assert _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]["history"] is None
    reading = _snapshot(hass, include_history=True)["devices"][DEVICE]["scanners"][SCANNER]
    assert len(reading["history"]) == MAX_HISTORY
    assert reading["history"][0]["observed"] == 998.0
    assert reading["history"][0]["model_provenance"] == "runtime_history_not_frozen"


def test_newest_source_alias_is_selected_without_merging_owners(hass):
    coordinator, device, advert, _ = _coordinator(hass)
    older = SimpleNamespace(**vars(advert))
    older.stamp = 80.0
    older.rssi = -82.0
    older.device_address = "aa:bb:cc:dd:ee:02"
    device.adverts[(older.device_address, SCANNER)] = older
    other_owner = SimpleNamespace(**vars(device))
    other_owner.address = "aa:bb:cc:dd:ee:03"
    coordinator.devices[other_owner.address] = other_owner
    snapshot = _snapshot(hass)
    assert len(snapshot["devices"]) == 2
    reading = snapshot["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["rssi"] == -65.0
    assert reading["source_aliases"] == [DEVICE, older.device_address]


def test_toolkit_effective_profiles_quality_and_source_epoch(hass):
    coordinator, _, _, _ = _coordinator(hass)
    quality = {"variance": 3.0, "rejection_reasons": ["suspect_placement"]}
    coordinator.toolkit = SimpleNamespace(
        snapshot_metadata=lambda device, scanner, advert: {
            "rssi_offset": 5.0,
            "reference_power": -61.0,
            "reference_power_verified": True,
            "attenuation": 2.5,
            "filtered_rssi": -64.2,
            "observed": 995.0,
            "model_version": "profile-v2",
            "quality": quality,
        }
    )
    snapshot = _snapshot(hass)
    reading = snapshot["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["model_version"] == "profile-v2"
    assert reading["reference_power"] == -61.0
    assert reading["reference_power_verified"] is True
    assert reading["filtered_rssi"] == -64.2
    assert reading["raw_rssi"] == -65.0
    assert reading["observed"] == 995.0
    assert reading["observed_clock"] == "source_epoch"
    quality["variance"] = 99.0
    assert reading["quality"]["variance"] == 3.0


def test_default_settings_match_runtime_fallbacks(hass):
    _, _, advert, _ = _coordinator(hass)
    advert.ref_power = 0
    advert.conf_ref_power = float("nan")
    advert.conf_attenuation = True
    reading = _snapshot(hass)["devices"][DEVICE]["scanners"][SCANNER]
    assert reading["reference_power"] == -55.0
    assert reading["attenuation"] == 3.0


def test_snapshot_limits_report_omitted_counts(hass):
    coordinator, device, advert, _ = _coordinator(hass)
    coordinator.devices = {f"device-{i}": device for i in range(MAX_DEVICES + 2)}
    device.adverts = {}
    for index in range(MAX_SCANNERS + 2):
        clone = SimpleNamespace(**vars(advert))
        clone.scanner_address = f"scanner-{index}"
        device.adverts[(DEVICE, clone.scanner_address)] = clone
    snapshot = _snapshot(hass)
    assert len(snapshot["devices"]) == MAX_DEVICES
    assert len(next(iter(snapshot["devices"].values()))["scanners"]) == MAX_SCANNERS
    assert snapshot["omitted"]["devices"] == 2
    assert snapshot["omitted"]["observations"] > 0
    json.dumps(snapshot, allow_nan=False)


def test_private_ble_keys_never_leave_snapshot(hass):
    private_key = "1234567890abcdef1234567890abcdef"
    coordinator, device, advert, _ = _coordinator(hass, device_id=private_key)
    device.name = f"bermuda_{private_key}"
    coordinator.toolkit = SimpleNamespace(
        snapshot_metadata=lambda device, scanner, advert: {"quality": {"detail": f"owner {private_key}"}}
    )
    public_id = public_device_id(private_key)
    snapshot = _snapshot(hass, addresses={public_id})
    assert public_id.startswith("private_")
    assert public_id in snapshot["devices"]
    assert resolve_public_device_id(coordinator, public_id) == private_key
    assert resolve_public_device_id(coordinator, "unknown") is None
    assert private_key not in json.dumps(snapshot)
    assert advert.device_address == private_key
    assert device.unique_id == private_key


@pytest.mark.parametrize("invalid_now", [True, float("nan"), "1000"])
def test_capture_clock_rejects_invalid_inputs(hass, invalid_now):
    _coordinator(hass)
    with pytest.raises(ValueError, match="finite epoch"):
        async_get_advert_snapshot(hass, now=invalid_now)


def test_real_coordinator_keeps_listener_subscription():
    from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

    assert callable(BermudaDataUpdateCoordinator.async_add_listener)


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    ],
)
def test_failed_or_unloading_entry_does_not_expose_old_runtime(hass, state):
    coordinator, _, _, _ = _coordinator(hass)
    coordinator.config_entry._async_set_state(hass, state, None)
    assert async_get_coordinator(hass) is None
    assert _snapshot(hass) is None


def test_setting_up_entry_is_visible_and_closed_manager_is_unavailable(hass):
    coordinator, _, _, _ = _coordinator(hass)
    coordinator.config_entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    assert async_get_coordinator(hass) is coordinator
    coordinator.toolkit = SimpleNamespace(closed=True)
    assert async_get_coordinator(hass) is None


def test_historical_scanner_identity_requires_exact_source_and_one_bermuda_id():
    entry = SimpleNamespace(connections={("bluetooth", SCANNER.upper())}, identifiers={(DOMAIN, "legacy-scanner")})
    assert scanner_identity_from_registry_entry(entry, SCANNER) == {
        "unique_id": "legacy-scanner",
        "entity_id_address": "legacy-scanner",
    }
    assert scanner_identity_from_registry_entry(entry, "unrelated") is None
    entry.identifiers.add((DOMAIN, "ambiguous-owner"))
    assert scanner_identity_from_registry_entry(entry, SCANNER) is None
    entry.identifiers = {("esphome", "network-device")}
    assert scanner_identity_from_registry_entry(entry, SCANNER) is None
