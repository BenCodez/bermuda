"""Real coordinator, adverts and HA options exercise toolkit ownership boundaries."""

from __future__ import annotations

import asyncio
import json
import math
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
from custom_components.bermuda.api import async_get_coordinator, public_device_id, snapshot_observations
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
from custom_components.bermuda.recording import DiagnosticHistory
from custom_components.bermuda.replay import ShadowSession
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
    assert manager.store.async_save.await_count == 2
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


@pytest.mark.parametrize("action", ["calibration_apply", "calibration_rollback"])
@pytest.mark.parametrize("power_source", ["manual", "restored"])
async def test_power_changes_during_save_cannot_publish_apply_or_rollback(runtime, action, power_source):
    manager = runtime.manager
    manager.proposal = _proposal(manager, {SCANNER: -2.0})
    if action == "calibration_rollback":
        await manager.async_dispatch("calibration_apply", {})
    before_options = deepcopy(dict(runtime.entry.options))
    before_trial = deepcopy(manager.trial)
    expected_version = manager.version()
    entered, release = asyncio.Event(), asyncio.Event()
    saved = []

    async def pause_first_save(payload):
        saved.append(deepcopy(payload))
        if len(saved) == 1:
            entered.set()
            await release.wait()

    manager.store.async_save.side_effect = pause_first_save
    request = asyncio.create_task(manager.async_dispatch(action, {}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        number = BermudaNumber(runtime.coordinator, runtime.entry, DEVICE)
        if power_source == "manual":
            with patch.object(number, "async_write_ha_state"):
                await number.async_set_native_value(-62.0)
        else:
            restored = NumberExtraStoredData(
                native_max_value=0,
                native_min_value=-127,
                native_step=1,
                native_unit_of_measurement="dBm",
                native_value=-62.0,
            )
            with (
                patch.object(BermudaEntity, "async_added_to_hass", new=AsyncMock()),
                patch.object(number, "async_get_last_number_data", new=AsyncMock(return_value=restored)),
            ):
                await number.async_added_to_hass()
        assert manager.version() != expected_version
        release.set()
        with pytest.raises(RuntimeError, match="changed while saving"):
            await asyncio.wait_for(request, timeout=1)
        assert dict(runtime.entry.options) == before_options
        assert runtime.coordinator.options[CONF_RSSI_OFFSETS] == before_options[CONF_RSSI_OFFSETS]
        assert runtime.advert.conf_rssi_offset == before_options[CONF_RSSI_OFFSETS][SCANNER]
        assert manager.trial == before_trial
        assert runtime.device.ref_power == -62.0
        assert not (manager.last_result or {}).get("rolled_back")
        assert len(saved) == 2
        assert saved[-1]["trial"] == before_trial
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("fail_write", [None, 1, 2])
async def test_automatic_regression_rollback_disables_auto_in_one_transaction(runtime, fail_write):
    manager = runtime.manager
    await manager.async_dispatch("settings", {"settings": {"auto_calibration": True}})
    baseline_offsets = deepcopy(dict(runtime.entry.options[CONF_RSSI_OFFSETS]))
    await manager._apply({SCANNER: -2.0}, automatic=True)
    applied_options = deepcopy(dict(runtime.entry.options))
    applied_trial = deepcopy(manager.trial)
    saved = []

    async def save_or_fail(payload):
        saved.append(deepcopy(payload))
        if len(saved) == fail_write:
            raise OSError("Synthetic automatic rollback write failure")

    manager.store.async_save.side_effect = save_or_fail
    regression = {"rollback": True, "sufficient": True, "reasons": ["regression:synthetic-receiver"]}
    with patch.object(manager.engine, "evaluate_trial", return_value=regression):
        if fail_write in (1, 2):
            with pytest.raises(OSError, match="automatic rollback write failure"):
                await manager._automatic(time.time())
            assert dict(runtime.entry.options) == applied_options
            assert manager.trial == applied_trial
            assert manager.settings["auto_calibration"] is True
        else:
            await manager._automatic(time.time())
            assert runtime.entry.options[CONF_RSSI_OFFSETS] == baseline_offsets
            assert runtime.coordinator.options[CONF_RSSI_OFFSETS] == baseline_offsets
            assert runtime.entry.options[OPTIONS_KEY]["auto_calibration"] is False
            assert manager.settings["auto_calibration"] is False
            assert manager.trial is None
            assert saved[-1]["trial"] is None
            assert manager.last_result["rolled_back"] is True
        assert len(saved) == (fail_write or 2)


@pytest.mark.parametrize("baseline_kind", ["legacy", "profile", "affected_receiver"])
async def test_automatic_trial_monitoring_accepts_inherited_offsets_outside_new_bounds(runtime, baseline_kind):
    manager = runtime.manager
    options = deepcopy(dict(runtime.entry.options))
    if baseline_kind == "legacy":
        options[CONF_RSSI_OFFSETS][OFFLINE] = 30.0
    elif baseline_kind == "profile":
        options[OPTIONS_KEY] = {"receiver_profiles": {OFFLINE: {"rssi_offset": -30.0}}}
    else:
        options[CONF_RSSI_OFFSETS][SCANNER] = 20.5
    runtime.hass.config_entries.async_update_entry(runtime.entry, options=options)
    assert await manager.async_options_changed(runtime.entry) is True
    candidate = 19.5 if baseline_kind == "affected_receiver" else 2.0
    await manager._apply({SCANNER: candidate}, automatic=True)
    applied = deepcopy(dict(runtime.entry.options))
    trial = deepcopy(manager.trial)
    # No independent new truth means continued observation. An inherited
    # baseline must not abort monitoring before the real engine can decide.
    await manager._automatic(time.time())
    assert manager.last_result["apply"] is False
    assert dict(runtime.entry.options) == applied
    assert manager.trial == trial


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


@pytest.mark.parametrize(
    ("reference_power", "attenuation", "offset", "rssi"),
    [
        (-55.0, 50.0, 0.0, -68.0),
        (-55.0, 0.05, 0.0, -55.1),
        (-55.0, 3.0, 101.0, -68.0),
        (-55.0, 3.0, -101.0, -68.0),
        (25.0, 3.0, 0.0, -68.0),
        (-201.0, 3.0, 0.0, -68.0),
        (1e308, 1e308, 1e308, -68.0),
        (-1e308, 1e308, -1e308, -68.0),
        (-55.0, 5e-324, 0.0, -55.0),
    ],
)
async def test_legacy_models_record_capture_and_round_trip_without_becoming_new_profiles(
    runtime, reference_power, attenuation, offset, rssi
):
    manager, coordinator, advert = runtime.manager, runtime.coordinator, runtime.advert
    legacy_options = {
        **deepcopy(dict(runtime.entry.options)),
        CONF_REF_POWER: reference_power,
        CONF_ATTENUATION: attenuation,
        CONF_RSSI_OFFSETS: {SCANNER: offset},
    }
    runtime.hass.config_entries.async_update_entry(runtime.entry, options=legacy_options)
    coordinator.options.update(legacy_options)
    advert.conf_ref_power, advert.conf_attenuation, advert.conf_rssi_offset = reference_power, attenuation, offset
    advert.rssi = rssi
    manager.invalidate_model()
    advert._update_raw_distance(reading_is_new=False)
    snapshot_row = snapshot_observations(manager._snapshot())[0]
    assert snapshot_row["status"] == "current"
    assert 0 < snapshot_row["raw_distance"] <= 1e6
    assert math.isfinite(snapshot_row["raw_distance"])
    assert snapshot_row["reference_power"] == reference_power
    assert snapshot_row["attenuation"] == attenuation
    assert snapshot_row["rssi_offset"] == offset

    await manager.async_dispatch("history_start", {"devices": [DEVICE]})
    await manager._after_update()
    assert manager.history.summary()["rows"] == 1
    assert manager.history.summary()["dropped_rows"] == 0
    assert not manager.last_result or manager.last_result.get("status") != "pending"
    await manager.async_dispatch("history_stop", {})
    captured = await manager.async_dispatch(
        "capture", {"device_id": DEVICE, "location_id": "legacy-model-site", "ranges": {SCANNER: 2.5}}
    )
    assert captured["captured"] == 1
    assert manager.history.summary()["rows"] == 1
    frozen = manager.history.frames()
    for field in (
        "rssi",
        "reference_power",
        "attenuation",
        "rssi_offset",
        "raw_distance",
        "filtered_distance",
        "stamp",
    ):
        assert frozen[0]["observations"][0][field] == snapshot_row[field]
    assert frozen[0]["truth"][0]["source"] == "measured"

    preview = await manager.async_dispatch("calibration_preview", {})
    assert preview["eligible"] is False
    assert preview["coverage"]["samples"] == 0
    assert preview["rejected"] == {"invalid_numeric_input": 1}
    assert preview["offsets"] == {}
    before = deepcopy(dict(runtime.entry.options))
    with pytest.raises(RuntimeError, match="current verified calibration preview"):
        await manager.async_dispatch("calibration_apply", {})
    with pytest.raises(ValueError, match="outside safe bounds"):
        await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 101.0}})
    with pytest.raises(ValueError, match="finite number between"):
        await manager.async_dispatch(
            "profile",
            {
                "kind": "receiver",
                "id": SCANNER,
                "profile": {"reference_power": reference_power, "attenuation": attenuation, "rssi_offset": offset},
            },
        )
    assert dict(runtime.entry.options) == before

    for redact in (False, True):
        exported = await manager.async_dispatch("history_export", {"redact": redact})
        row = exported["frames"][0]["observations"][0]
        assert (row["reference_power"], row["attenuation"], row["rssi_offset"]) == (
            reference_power,
            attenuation,
            offset,
        )
        imported = await manager.async_dispatch("history_import", {"data": json.dumps(exported, allow_nan=False)})
        assert imported["rows"] == 1
        assert manager.history.frames() == exported["frames"]
    assert dict(runtime.entry.options) == before


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


async def test_legacy_power_change_rejects_validated_inflight_shadow_promotion_without_applying(runtime):
    manager = runtime.manager
    original_version = manager.version()
    snapshot = manager._snapshot()
    current = snapshot_observations(snapshot)[0]
    expected_distance = current["raw_distance"] / 2
    candidate_offset = current["rssi_offset"] + 10 * current["attenuation"] * math.log10(2)
    history = DiagnosticHistory(enabled=True)
    for index in range(6):
        captured = snapshot["captured"] - 6 + index
        observation = {**deepcopy(current), "stamp": SOURCE_STAMP - 6 + index, "observed": captured, "age": 0.0}
        history.record(
            [observation],
            captured_at=captured,
            model_version=original_version,
            truth=[
                {
                    "source": "measured",
                    "device_id": DEVICE,
                    "location_id": "synthetic_site_a" if index < 3 else "synthetic_site_b",
                    "ranges": {SCANNER: expected_distance},
                    "accuracy_m": 0.0,
                    "observed": captured,
                    "independent": True,
                }
            ],
        )
    shadow = manager.shadow = ShadowSession(
        [], model_version=original_version, proposed_offsets={SCANNER: candidate_offset}
    )
    for frame in history.frames():
        assert shadow.add_frame(frame) == 1
    check = shadow.promotion_check(current_model_version=original_version)
    assert check["eligible"] is True, check["reasons"]
    before_options = deepcopy(dict(runtime.entry.options))
    before_offsets = deepcopy(runtime.coordinator.options[CONF_RSSI_OFFSETS])
    before_saved = manager.store.async_save.await_count
    started, release = threading.Event(), threading.Event()
    original_check = ShadowSession.promotion_check
    validated = []

    def slow_check(session, *, current_model_version):
        result = original_check(session, current_model_version=current_model_version)
        validated.append(result)
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("Synthetic shadow promotion was not released")
        return result

    async def wait_until_started():
        while not started.is_set():
            await asyncio.sleep(0.005)

    with patch.object(ShadowSession, "promotion_check", new=slow_check):
        task = asyncio.create_task(manager.async_dispatch("shadow_promote", {}))
        try:
            await asyncio.wait_for(wait_until_started(), timeout=1)
            assert validated[0]["eligible"] is True
            assert validated[0]["model_version"] == original_version
            number = BermudaNumber(runtime.coordinator, runtime.entry, DEVICE)
            with patch.object(number, "async_write_ha_state"):
                await number.async_set_native_value(-62)
            assert manager.version() != original_version
            assert shadow.state == "cancelled"
            release.set()
            with pytest.raises(RuntimeError, match="live model"):
                await asyncio.wait_for(task, timeout=1)
            assert dict(runtime.entry.options) == before_options
            assert runtime.coordinator.options[CONF_RSSI_OFFSETS] == before_offsets
            assert manager.store.async_save.await_count == before_saved
            assert manager.trial is None
            assert shadow.state == "cancelled"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


def _add_receiver(coordinator, device, scanner_id, stamp):
    """Create a real second receiver/advert for fingerprint coverage."""
    scanner = BermudaDevice(scanner_id, coordinator)
    scanner._is_scanner = True
    scanner._is_remote_scanner = True
    scanner.stamps = {DEVICE.upper(): stamp}
    scanner.last_seen = stamp
    coordinator.devices[scanner_id] = scanner
    coordinator.scanner_list_add(scanner)
    data = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=-68 if scanner_id == SCANNER else -72,
        platform_data=(),
    )
    advert = BermudaAdvert(device, data, coordinator.options, scanner)
    device.adverts[(DEVICE, scanner_id)] = advert
    advert.calculate_data()
    return advert


@pytest.mark.parametrize("changed_boot", [False, True])
async def test_local_store_reload_resumes_clock_epochs_only_after_host_boot_change(runtime, monkeypatch, changed_boot):
    second_scanner = "aa:bb:cc:dd:ee:22"
    old = runtime.manager
    _add_receiver(runtime.coordinator, runtime.device, second_scanner, SOURCE_STAMP)
    area = runtime.coordinator.ar.async_create("Synthetic Persisted Fingerprint Room")
    await old.async_dispatch("history_start", {"clear": True})
    await old._after_update()
    trained = await old.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area.id})
    assert trained["accepted"] is True and trained["added_samples"] == 1
    assert old.history.summary()["rows"] == 2
    old._boot_id = "synthetic-boot-a"
    saved = deepcopy(old._store_payload())
    saved_history_epoch = saved["history"]["session_epoch"]
    saved_fingerprint_epoch = saved["fingerprints"]["active_session_epoch"]
    await old.async_close()
    current_stamp = 5.0 if changed_boot else SOURCE_STAMP + 5
    source_stamp = 5.0 if changed_boot else SOURCE_STAMP
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: current_stamp)
    monkeypatch.setattr(
        "custom_components.bermuda.toolkit._host_clock_id",
        lambda: "synthetic-boot-b" if changed_boot else "synthetic-boot-a",
    )
    token = current_entry.set(runtime.entry)
    try:
        coordinator = BermudaDataUpdateCoordinator(runtime.hass, runtime.entry)
    finally:
        current_entry.reset(token)
    resumed = coordinator.toolkit = ToolkitManager(coordinator)
    resumed.store = MagicMock(spec=Store)
    resumed.store.async_load = AsyncMock(return_value=saved)
    resumed.store.async_save = AsyncMock()
    runtime.entry.runtime_data = BermudaData(coordinator)
    try:
        await resumed.async_load()
        assert resumed.history.summary()["rows"] == 2
        assert resumed.history.session_epoch == saved_history_epoch + int(changed_boot)
        assert resumed.fingerprints.export_state()["active_session_epoch"] == saved_fingerprint_epoch + int(
            changed_boot
        )
        device = BermudaDevice(DEVICE, coordinator)
        device.create_sensor = True
        coordinator.devices[DEVICE] = device
        _add_receiver(coordinator, device, SCANNER, source_stamp)
        _add_receiver(coordinator, device, second_scanner, source_stamp)
        await resumed._after_update()
        first = await resumed.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area.id})
        assert first["accepted"] is changed_boot
        assert first["added_samples"] == int(changed_boot)
        assert first["samples"] == 1 + int(changed_boot)
        assert resumed.history.summary()["rows"] == 2 + 2 * int(changed_boot)
        rows_before = resumed.history.frames()
        await resumed._after_update()
        duplicate = await resumed.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area.id})
        assert duplicate["accepted"] is False
        assert duplicate["added_samples"] == 0
        assert duplicate["samples"] == 1 + int(changed_boot)
        assert resumed.history.frames() == rows_before
        epochs = [frame["session_epoch"] for frame in resumed.history.frames()]
        assert epochs == ([saved_history_epoch, saved_history_epoch + 1] if changed_boot else [saved_history_epoch])
    finally:
        await resumed.async_close()


async def test_optional_filter_interval_history_lists_latest_approach_readings_first(runtime, monkeypatch):
    manager, advert = runtime.manager, runtime.advert
    await manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet"}}})
    advert.hist_distance_by_interval.clear()
    distances = []
    for index, expected_distance in enumerate([12.0] * 40 + [8.0] * 6, start=1):
        stamp = SOURCE_STAMP + index
        monkeypatch.setattr("custom_components.bermuda.toolkit.monotonic_time_coarse", lambda: stamp)
        runtime.scanner.stamps[DEVICE.upper()] = stamp
        rssi = -55 - 30 * math.log10(expected_distance) - 1.5
        data = AdvertisementData(
            local_name="Synthetic Beacon",
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            tx_power=-59,
            rssi=rssi,
            platform_data=(),
        )
        advert.update_advertisement(data, runtime.scanner)
        advert.calculate_data()
        distances.append(advert.rssi_distance)
    assert advert.rssi_distance_raw == pytest.approx(8.0)
    assert 8.0 < advert.rssi_distance < 9.0
    assert len(advert.hist_distance_by_interval) == 32
    assert advert.hist_distance_by_interval[:5] == list(reversed(distances[-5:]))
    assert advert.hist_distance_by_interval[0] < advert.hist_distance_by_interval[4] < 12.0
    assert advert.hist_distance_by_interval[-1] == pytest.approx(12.0)


async def test_external_options_edit_wins_while_toolkit_settings_persistence_is_paused(runtime):
    manager = runtime.manager
    baseline = deepcopy(dict(runtime.entry.options))
    entered, release = asyncio.Event(), asyncio.Event()
    durable_payloads = []

    async def save_while_paused(payload):
        durable_payloads.append(deepcopy(payload))
        if len(durable_payloads) == 1:
            entered.set()
            await release.wait()

    manager.store.async_save.side_effect = save_while_paused
    action = asyncio.create_task(manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 4.0}}}))
    listener = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        external = {**baseline, CONF_RSSI_OFFSETS: {SCANNER: 7.0, OFFLINE: 7.0}}
        runtime.hass.config_entries.async_update_entry(runtime.entry, options=external)
        listener = asyncio.create_task(manager.async_options_changed(runtime.entry))
        await asyncio.sleep(0)
        assert listener.done() is False
        assert runtime.coordinator.options[CONF_RSSI_OFFSETS][SCANNER] == 1.5
        release.set()
        with pytest.raises(RuntimeError, match="changed while saving"):
            await asyncio.wait_for(action, timeout=1)
        assert await asyncio.wait_for(listener, timeout=1) is True
        committed = deepcopy(dict(runtime.entry.options))
        assert committed.pop(OPTIONS_KEY) == {"_commit_id": manager._saved_options[OPTIONS_KEY]["_commit_id"]}
        assert committed == external
        assert runtime.coordinator.options[CONF_RSSI_OFFSETS] == external[CONF_RSSI_OFFSETS]
        assert manager.settings["receiver_limits"] == {}
        assert runtime.advert.conf_rssi_offset == 7.0
        assert len(durable_payloads) == 4
        assert durable_payloads[-1]["trial"] is None
    finally:
        release.set()
        await asyncio.gather(action, *([listener] if listener is not None else []), return_exceptions=True)


async def test_queued_options_listeners_adopt_latest_entry_options_after_acquiring_lock(runtime):
    manager = runtime.manager
    baseline = deepcopy(dict(runtime.entry.options))
    intermediate = {**baseline, CONF_RSSI_OFFSETS: {SCANNER: 7.0, OFFLINE: 7.0}}
    latest = {**baseline, CONF_RSSI_OFFSETS: {SCANNER: 9.0, OFFLINE: 7.0}}
    observed = []
    original_adopt = manager._adopt_settings

    def adopt(settings, options):
        observed.append(deepcopy(options))
        original_adopt(settings, options)

    first = second = None
    await manager._lock.acquire()
    lock_held = True
    with patch.object(manager, "_adopt_settings", new=adopt):
        try:
            runtime.hass.config_entries.async_update_entry(runtime.entry, options=intermediate)
            first = asyncio.create_task(manager.async_options_changed(runtime.entry))
            await asyncio.sleep(0)
            assert first.done() is False
            runtime.hass.config_entries.async_update_entry(runtime.entry, options=latest)
            second = asyncio.create_task(manager.async_options_changed(runtime.entry))
            await asyncio.sleep(0)
            assert second.done() is False
            manager._lock.release()
            lock_held = False
            assert await asyncio.wait_for(asyncio.gather(first, second), timeout=1) == [True, True]
            assert len(observed) == 1
            committed = deepcopy(dict(runtime.entry.options))
            assert committed.pop(OPTIONS_KEY) == {"_commit_id": manager._saved_options[OPTIONS_KEY]["_commit_id"]}
            assert committed == latest
            assert observed[0] == dict(runtime.entry.options)
            assert runtime.coordinator.options[CONF_RSSI_OFFSETS] == latest[CONF_RSSI_OFFSETS]
            assert runtime.advert.conf_rssi_offset == 9.0
        finally:
            if lock_held:
                manager._lock.release()
            await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
