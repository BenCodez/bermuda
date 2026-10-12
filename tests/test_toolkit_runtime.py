"""Real coordinator, adverts and HA options exercise toolkit ownership boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.components.number import NumberExtraStoredData
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda import BermudaData
from custom_components.bermuda.api import async_get_coordinator, public_device_id
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import (
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    DOMAIN,
)
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
from custom_components.bermuda.number import BermudaNumber
from custom_components.bermuda.entity import BermudaEntity
from custom_components.bermuda.toolkit import OPTIONS_KEY, ToolkitManager

DEVICE = "aa:bb:cc:dd:ee:01"
SCANNER = "aa:bb:cc:dd:ee:12"
OFFLINE = "aa:bb:cc:dd:ee:99"
SOURCE_STAMP = 100.0


@pytest.fixture
async def runtime(hass, device_registry, monkeypatch):
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: SOURCE_STAMP)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="toolkit-runtime",
        data={"source": "user"},
        options={
            CONF_DEVICES: [DEVICE],
            CONF_REF_POWER: -55.0,
            CONF_ATTENUATION: 3.0,
            CONF_RSSI_OFFSETS: {SCANNER: 1.5, OFFLINE: 7.0},
            "legacy_unrelated": "retained",
        },
    )
    entry.add_to_hass(hass)
    token = current_entry.set(entry)
    try:
        coordinator = BermudaDataUpdateCoordinator(hass, entry)
    finally:
        current_entry.reset(token)
    manager = coordinator.toolkit = ToolkitManager(coordinator)
    manager.store = MagicMock(spec=Store)
    manager.store.async_load = AsyncMock(return_value=None)
    manager.store.async_save = AsyncMock()
    entry.runtime_data = BermudaData(coordinator)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    scanner = BermudaDevice(SCANNER, coordinator)
    scanner._is_scanner = True
    scanner._is_remote_scanner = True
    scanner.stamps = {DEVICE.upper(): SOURCE_STAMP}
    scanner.last_seen = SOURCE_STAMP
    scanner._hascanner = SimpleNamespace(source=SCANNER, name="Synthetic Receiver")
    bluetooth_entry = MockConfigEntry(domain="bluetooth", entry_id="runtime-receiver", data={})
    bluetooth_entry.add_to_hass(hass)
    registry_device = device_registry.async_get_or_create(
        config_entry_id=bluetooth_entry.entry_id,
        connections={("bluetooth", SCANNER.upper())},
        name="Synthetic Receiver",
    )
    scanner.async_as_scanner_resolve_device_entries()
    coordinator.scanner_list_add(scanner)
    device = BermudaDevice(DEVICE, coordinator)
    device.create_sensor = True
    coordinator.devices.update({DEVICE: device, SCANNER: scanner})
    data = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=-68,
        platform_data=(),
    )
    advert = BermudaAdvert(device, data, coordinator.options, scanner)
    device.adverts[(DEVICE, SCANNER)] = advert
    advert.calculate_data()
    yield SimpleNamespace(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        manager=manager,
        device=device,
        scanner=scanner,
        advert=advert,
        registry_device=registry_device,
    )
    manager.store.async_save.side_effect = None
    await manager.async_close()


def _proposal(manager, offsets):
    return {
        "eligible": True,
        "model_version": manager.version(),
        "proposal_id": "synthetic-current-proposal",
        "created_at": time.time(),
        "data_id": "synthetic-independent-data",
        "offsets": offsets,
    }


async def test_settings_and_profiles_apply_to_live_advert_without_losing_legacy_options(runtime):
    manager, entry, advert = runtime.manager, runtime.entry, runtime.advert
    original = deepcopy(dict(entry.options))
    original_stamp = advert.stamp
    result = await manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 5.0}}})
    assert result["options"]["receiver_limits"] == {SCANNER: 5.0}
    assert manager.receiver_limit(SCANNER, 20) == 5.0
    assert manager.receiver_limit(OFFLINE, 20) == 20
    result = await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"attenuation": 2.0, "rssi_offset": 3.0}}
    )
    assert result["profile"] == {"attenuation": 2.0, "rssi_offset": 3.0}
    assert manager.effective_advert_model(advert)["rssi_offset"] == 3.0
    assert manager.effective_advert_model(advert)["attenuation"] == 2.0
    assert advert.rssi_distance_raw != pytest.approx(10 ** ((-55 - (-68 + 1.5)) / 30))
    assert advert.stamp == original_stamp
    for key, value in original.items():
        assert entry.options[key] == value
    assert runtime.coordinator.options[CONF_RSSI_OFFSETS][OFFLINE] == 7.0


async def test_clearing_optional_profile_numerics_restores_effective_legacy_model(runtime):
    manager, advert = runtime.manager, runtime.advert
    await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"reference_power": -62.0, "attenuation": 2.2}}
    )
    assert manager.effective_advert_model(advert)["reference_power"] == -62
    await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {}, "remove": ["reference_power", "attenuation"]}
    )
    assert manager.settings["receiver_profiles"][SCANNER] == {}
    assert manager.effective_advert_model(advert)["reference_power"] == -55
    assert manager.effective_advert_model(advert)["attenuation"] == 3


async def test_apply_and_rollback_bind_to_current_model_and_keep_other_offsets(runtime):
    manager, entry = runtime.manager, runtime.entry
    before = deepcopy(dict(entry.options))
    manager.proposal = _proposal(manager, {SCANNER: -2.0})
    original_version = manager.version()
    result = await manager.async_dispatch("calibration_apply", {"proposal_id": manager.proposal["proposal_id"]})
    assert result["applied"] is True
    assert manager.version() != original_version
    assert entry.options[CONF_RSSI_OFFSETS] == {SCANNER: -2.0, OFFLINE: 7.0}
    assert runtime.advert.conf_rssi_offset == -2.0
    payload = manager.store.async_save.call_args.args[0]
    assert payload["trial"]["model_version"] == manager.version()
    assert payload["trial"]["previous_options"] == before
    assert manager.store.async_save.await_count == 1
    rolled = await manager.async_dispatch("calibration_rollback", {})
    assert rolled["rolled_back"] is True
    assert entry.options[CONF_RSSI_OFFSETS] == before[CONF_RSSI_OFFSETS]
    assert manager.version() == original_version
    assert manager.trial is None


async def test_model_change_invalidates_an_existing_calibration_trial(runtime):
    manager = runtime.manager
    manager.proposal = _proposal(manager, {SCANNER: 2.0})
    await manager.async_dispatch("calibration_apply", {})
    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"attenuation": 2.5}})
    with pytest.raises(RuntimeError, match="unchanged trial"):
        await manager.async_dispatch("calibration_rollback", {})


@pytest.mark.parametrize("action", ["settings", "profile", "calibration_apply"])
async def test_failed_persistence_cannot_publish_live_settings_or_offsets(runtime, action):
    manager = runtime.manager
    previous_options = deepcopy(dict(runtime.entry.options))
    previous_settings = deepcopy(manager.settings)
    raw_distance = runtime.advert.rssi_distance_raw
    manager.proposal = _proposal(manager, {SCANNER: -1.0})
    manager.store.async_save.side_effect = OSError("synthetic store failure")
    payload = {
        "settings": {"receiver_limits": {SCANNER: 4.0}},
        "kind": "receiver",
        "id": SCANNER,
        "profile": {"attenuation": 2.0},
    }
    with pytest.raises(OSError, match="synthetic store failure"):
        await manager.async_dispatch(action, payload)
    assert dict(runtime.entry.options) == previous_options
    assert manager.settings == previous_settings
    assert runtime.coordinator.options[CONF_RSSI_OFFSETS] == previous_options[CONF_RSSI_OFFSETS]
    assert runtime.advert.rssi_distance_raw == raw_distance
    assert manager.trial is None
    assert manager.proposal is not None


async def test_cancelled_analysis_retains_executor_slot_until_worker_really_finishes(runtime):
    started, release = threading.Event(), threading.Event()

    def slow_analysis():
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("Synthetic worker was not released")
        return {"complete": True}

    async def wait_until_started():
        while not started.is_set():
            await asyncio.sleep(0.005)

    manager = runtime.manager
    task = asyncio.create_task(manager._run_worker(slow_analysis))
    try:
        await asyncio.wait_for(wait_until_started(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        worker = manager._worker
        assert worker is not None and not worker.done()
        with pytest.raises(RuntimeError, match="already running"):
            await manager._run_worker(lambda: {"second": True})
        release.set()
        assert await asyncio.wait_for(asyncio.shield(worker), timeout=1) == {"complete": True}
        await asyncio.sleep(0)
        assert manager._worker is None
    finally:
        release.set()


async def test_optional_filter_preserves_raw_rssi_source_clock_and_deduplicates_packets(runtime):
    manager, advert = runtime.manager, runtime.advert
    await manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet"}}})
    raw, stamp, raw_distance = advert.rssi, advert.stamp, advert.rssi_distance_raw
    advert.new_stamp = SOURCE_STAMP
    advert.calculate_data()
    state = manager._states[(DEVICE, SCANNER)]
    count = state.snapshot(SOURCE_STAMP)["samples"]
    advert.new_stamp = SOURCE_STAMP
    advert.calculate_data()
    assert state.snapshot(SOURCE_STAMP)["samples"] == count
    assert advert.rssi == raw
    assert advert.stamp == stamp
    assert advert.rssi_distance_raw == raw_distance
    with patch("custom_components.bermuda.toolkit.monotonic_time_coarse", return_value=SOURCE_STAMP + 40):
        advert.calculate_data()
    assert advert.rssi_distance is None
    assert advert.rssi == raw and advert.stamp == stamp


async def test_historic_scanner_ids_require_exact_registry_source_proof(runtime):
    manager = runtime.manager
    manager._identities.clear()
    runtime.coordinator.dr.async_update_device(
        runtime.registry_device.id, new_identifiers={(DOMAIN, "synthetic-historic-receiver")}
    )
    assert manager.scanner_identity(SCANNER, "new-network-mac") == {
        "unique_id": "synthetic-historic-receiver",
        "entity_id_address": "synthetic-historic-receiver",
    }
    assert manager.scanner_identity("aa:bb:cc:dd:ee:13", "different-network-mac") == {
        "unique_id": "different-network-mac",
        "entity_id_address": "different-network-mac",
    }


async def test_private_ble_owner_keys_are_absent_from_public_state(runtime):
    private_key = "1234567890abcdef1234567890abcdef"
    coordinator, manager = runtime.coordinator, runtime.manager
    coordinator.devices[private_key] = coordinator.devices.pop(DEVICE)
    runtime.device.name = f"Synthetic {private_key}"
    runtime.device.unique_id = private_key
    state = await manager.async_dispatch("state", {})
    assert public_device_id(private_key) in state["snapshot"]["devices"]
    assert private_key not in json.dumps(state)


async def test_legacy_model_bounds_remain_compatible_without_explicit_profiles(runtime):
    coordinator, manager, advert = runtime.coordinator, runtime.manager, runtime.advert
    coordinator.options[CONF_REF_POWER] = advert.conf_ref_power = 1.0
    coordinator.options[CONF_ATTENUATION] = advert.conf_attenuation = 12.0
    coordinator.options[CONF_RSSI_OFFSETS] = {SCANNER: 40.0}
    model = manager.effective_advert_model(advert)
    assert model["reference_power"] == 1.0
    assert model["attenuation"] == 12.0
    assert model["rssi_offset"] == 40.0
    advert._update_raw_distance(reading_is_new=False)
    assert advert.rssi_distance_raw == pytest.approx(10 ** ((1 - (-68 + 40)) / 120))


async def test_number_reference_power_change_invalidates_suggestions_and_proposals(runtime):
    manager = runtime.manager
    manager.proposal = _proposal(manager, {SCANNER: 1.0})
    manager._suggestions[DEVICE] = {"accepted": True, "suggested_area": "old_area"}
    version = manager.version()
    number = BermudaNumber(runtime.coordinator, runtime.entry, DEVICE)
    with patch.object(number, "async_write_ha_state"):
        await number.async_set_native_value(-61.0)
    assert runtime.device.ref_power == -61.0
    assert runtime.advert.ref_power == -61.0
    assert manager.version() != version
    assert manager.proposal is None
    assert manager.area_suggestion(runtime.device)["accepted"] is False
    proposal = manager.proposal = _proposal(manager, {SCANNER: 1.0})
    with patch.object(number, "async_write_ha_state"):
        await number.async_set_native_value(-61.0)
    assert manager.proposal is proposal


async def test_close_makes_runtime_unavailable_before_hass_removes_runtime_data(runtime):
    assert async_get_coordinator(runtime.hass, runtime.entry.entry_id) is runtime.coordinator
    await runtime.manager.async_close()
    assert runtime.entry.runtime_data.coordinator is runtime.coordinator
    assert async_get_coordinator(runtime.hass, runtime.entry.entry_id) is None
    with pytest.raises(RuntimeError, match="unloading"):
        await runtime.manager.async_dispatch("state", {})


async def test_profile_update_tolerates_a_discovered_advert_without_source_stamp(runtime):
    runtime.scanner.stamps.clear()
    data = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=-68,
        platform_data=(),
    )
    unmeasured = BermudaAdvert(runtime.device, data, runtime.coordinator.options, runtime.scanner)
    assert unmeasured.rssi is None
    runtime.device.adverts["unmeasured"] = unmeasured
    result = await runtime.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"attenuation": 2.0}}
    )
    assert result["profile"]["attenuation"] == 2.0
    assert unmeasured.rssi is None and unmeasured.rssi_distance is None


async def test_options_listener_handles_toolkit_live_and_defers_legacy_changes_to_reload(runtime):
    manager, entry, coordinator = runtime.manager, runtime.entry, runtime.coordinator
    options = {**dict(entry.options), OPTIONS_KEY: {"receiver_limits": {SCANNER: 6.0}}}
    runtime.hass.config_entries.async_update_entry(entry, options=options)
    assert await manager.async_options_changed(entry) is True
    assert manager.receiver_limit(SCANNER, 20) == 6.0
    old_power = coordinator.options[CONF_REF_POWER]
    runtime.hass.config_entries.async_update_entry(entry, options={**dict(entry.options), CONF_REF_POWER: -64.0})
    assert await manager.async_options_changed(entry) is False
    assert coordinator.options[CONF_REF_POWER] == old_power


async def test_preview_without_independent_labels_has_no_live_side_effects(runtime):
    before = deepcopy(dict(runtime.entry.options))
    raw = runtime.advert.rssi_distance_raw
    result = await runtime.manager.async_dispatch("calibration_preview", {})
    assert result["eligible"] is False
    assert dict(runtime.entry.options) == before
    assert runtime.advert.rssi_distance_raw == raw
    assert runtime.manager.store.async_save.await_count == 0


async def test_instant_capture_labels_only_the_captured_packet(runtime):
    manager = runtime.manager
    result = await manager.async_dispatch(
        "capture", {"device_id": DEVICE, "location_id": "synthetic_measured_location", "ranges": {SCANNER: 2.5}}
    )
    assert result["captured"] == 1
    assert manager.history.frames()[0]["truth"][0]["source"] == "measured"
    await manager.async_dispatch("history_start", {"clear": False})
    runtime.scanner.stamps[DEVICE.upper()] = SOURCE_STAMP + 1
    data = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=-66,
        platform_data=(),
    )
    with (
        patch("custom_components.bermuda.toolkit.monotonic_time_coarse", return_value=SOURCE_STAMP + 1),
        patch("custom_components.bermuda.api.monotonic_time_coarse", return_value=SOURCE_STAMP + 1),
    ):
        runtime.advert.update_advertisement(data, runtime.scanner)
        runtime.advert.calculate_data()
        await manager._after_update()
    frames = manager.history.frames()
    assert len(frames) == 2
    assert frames[-1]["observations"][0]["stamp"] == SOURCE_STAMP + 1
    assert frames[-1]["truth"] == []


async def test_failed_capture_persistence_does_not_leave_unsaved_labels(runtime):
    manager = runtime.manager
    before = manager.history.frames()
    manager.store.async_save.side_effect = OSError("synthetic capture persistence failure")
    with pytest.raises(OSError, match="synthetic capture"):
        await manager.async_dispatch(
            "capture", {"device_id": DEVICE, "location_id": "synthetic_measured_location", "ranges": {SCANNER: 2.5}}
        )
    assert manager.history.frames() == before


async def test_invalid_history_selection_is_rejected_before_publishing_settings(runtime):
    manager = runtime.manager
    before = deepcopy(dict(runtime.entry.options))
    with pytest.raises(ValueError):
        await manager.async_dispatch("history_start", {"devices": "invalid-selection"})
    assert dict(runtime.entry.options) == before
    assert manager.settings["history_enabled"] is False
    assert manager.history.enabled is False
    assert manager.store.async_save.await_count == 0


async def test_first_shared_metadevice_profile_matches_raw_range_and_api_model(runtime):
    canonical_id = "1234567890abcdef1234567890abcdef_1_1"
    manager, coordinator = runtime.manager, runtime.coordinator
    canonical = BermudaDevice(canonical_id, coordinator)
    canonical.create_sensor = True
    canonical.adverts[(DEVICE, SCANNER)] = runtime.advert
    runtime.device.create_sensor = False
    coordinator.options[CONF_DEVICES] = [canonical_id]
    coordinator.devices[canonical_id] = canonical
    await manager.async_dispatch(
        "profile",
        {"kind": "beacon", "id": canonical_id, "profile": {"reference_power": -60.0, "attenuation": 2.0}},
    )
    manager.prepare_update()
    expected = 10 ** ((-60 - (-68 + 1.5)) / 20)
    assert runtime.advert.rssi_distance_raw == pytest.approx(expected)
    state = await manager.async_dispatch("state", {})
    reading = state["snapshot"]["devices"][canonical_id]["scanners"][SCANNER]
    assert reading["reference_power"] == -60.0
    assert reading["attenuation"] == 2.0
    assert reading["raw_distance"] == pytest.approx(expected)
    assert reading["source_id"] == DEVICE
    assert runtime.advert.device_address == DEVICE
    assert runtime.advert._device is runtime.device


async def test_metadata_only_profile_changes_preserve_current_filtered_and_raw_readings(runtime):
    manager, advert = runtime.manager, runtime.advert
    advert.rssi_distance = advert.rssi_distance_raw * 0.8
    before = {
        "rssi": advert.rssi,
        "stamp": advert.stamp,
        "raw": advert.rssi_distance_raw,
        "filtered": advert.rssi_distance,
        "hist_distance": advert.hist_distance.copy(),
        "hist_intervals": advert.hist_distance_by_interval.copy(),
    }
    version = manager.version()
    state = manager._states[(DEVICE, SCANNER)]
    await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"manufacturer": "Synthetic Hardware"}}
    )
    assert manager.version() == version
    assert manager._states[(DEVICE, SCANNER)] is state
    assert {
        "rssi": advert.rssi,
        "stamp": advert.stamp,
        "raw": advert.rssi_distance_raw,
        "filtered": advert.rssi_distance,
        "hist_distance": advert.hist_distance,
        "hist_intervals": advert.hist_distance_by_interval,
    } == before


async def test_restored_reference_power_also_invalidates_cached_evidence(runtime):
    manager = runtime.manager
    manager.proposal = _proposal(manager, {SCANNER: 1.0})
    manager._suggestions[DEVICE] = {"accepted": True}
    number = BermudaNumber(runtime.coordinator, runtime.entry, DEVICE)
    restored = NumberExtraStoredData(
        native_max_value=0,
        native_min_value=-127,
        native_step=1,
        native_unit_of_measurement="dBm",
        native_value=-60,
    )
    with (
        patch.object(BermudaEntity, "async_added_to_hass", new=AsyncMock()),
        patch.object(number, "async_get_last_number_data", new=AsyncMock(return_value=restored)),
    ):
        await number.async_added_to_hass()
    assert runtime.device.ref_power == -60
    assert manager.proposal is None
    assert manager.area_suggestion(runtime.device)["accepted"] is False


async def test_legacy_power_change_rejects_inflight_preview_before_republishing_old_evidence(runtime):
    started, release = threading.Event(), threading.Event()
    manager = runtime.manager

    def slow_preview(rows, *, now, model_version, current_offsets, verified_reference_devices):
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("Synthetic preview was not released")
        return {"model_version": model_version, "data_id": "old-model-data", "eligible": False}

    async def wait_until_started():
        while not started.is_set():
            await asyncio.sleep(0.005)

    with patch.object(manager.engine, "preview", new=slow_preview):
        task = asyncio.create_task(manager.async_dispatch("calibration_preview", {}))
        try:
            await asyncio.wait_for(wait_until_started(), timeout=1)
            number = BermudaNumber(runtime.coordinator, runtime.entry, DEVICE)
            with patch.object(number, "async_write_ha_state"):
                await number.async_set_native_value(-62)
            release.set()
            with pytest.raises(RuntimeError, match="Settings changed"):
                await asyncio.wait_for(task, timeout=1)
            assert manager.proposal is None
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
