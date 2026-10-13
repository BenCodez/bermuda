"""Tracked aliases own derived ranges while one physical advert owns raw evidence."""

from __future__ import annotations

from copy import deepcopy

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.helpers import area_registry as ar

from custom_components.bermuda.api import async_get_advert_snapshot, public_device_id, snapshot_observations
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import CONF_DEVICES, METADEVICE_TYPE_IBEACON_SOURCE
from custom_components.bermuda.number import BermudaNumber
from custom_components.bermuda.recording import DiagnosticHistory
from custom_components.bermuda.replay import ReplayEngine
from custom_components.bermuda.sensor import BermudaSensorRange, BermudaSensorScannerRange, BermudaSensorScannerRangeRaw
from custom_components.bermuda.util import rssi_to_metres
from tests.test_toolkit_model_rebase import model_runtime, source_state
from tests.test_toolkit_runtime import DEVICE, SCANNER, runtime

BEACON = "1234567890abcdef1234567890abcdef_1_1"


def register_alias(state, *, address=BEACON):
    coordinator, source = state.coordinator, state.device
    configured = [DEVICE.upper(), address.upper()]
    coordinator.options[CONF_DEVICES] = configured
    state.hass.config_entries.async_update_entry(
        state.entry, options={**deepcopy(dict(state.entry.options)), CONF_DEVICES: configured}
    )
    if address == BEACON:
        source.beacon_unique_id = address
        source.metadevice_type.add(METADEVICE_TYPE_IBEACON_SOURCE)
        coordinator.register_ibeacon_source(source)
        coordinator.update_metadevices()
        return coordinator.devices[address]
    # Private BLE skeletons use the same source-copy path after resolution.
    meta = BermudaDevice(address, coordinator)
    meta.create_sensor = True
    meta.metadevice_sources = [DEVICE]
    coordinator.devices[address] = coordinator.metadevices[address] = meta
    coordinator.update_metadevices()
    return meta


def batch(state, *, rssi=None, repeat_prepare=False):
    if rssi is not None:
        state.clock["now"] += 1
        state.scanner.stamps[DEVICE.upper()] = state.clock["now"]
        state.advert.update_advertisement(
            AdvertisementData(
                local_name="Synthetic Shared Beacon",
                manufacturer_data={},
                service_data={},
                service_uuids=[],
                tx_power=-59,
                rssi=rssi,
                platform_data=(),
            ),
            state.scanner,
        )
    state.coordinator.update_metadevices()
    state.manager.prepare_update()
    if repeat_prepare:
        state.manager.prepare_update()
    for device in state.coordinator.devices.values():
        device.calculate_data()
    state.coordinator._refresh_areas_by_min_distance()


async def profiles(state, address=BEACON):
    await state.manager.async_dispatch(
        "settings",
        {
            "settings": {
                "beacon_profiles": {
                    DEVICE: {"reference_power": -55, "attenuation": 3},
                    address: {"reference_power": -65, "attenuation": 2},
                }
            }
        },
    )


@pytest.mark.parametrize("address", [BEACON, "1234567890abcdef1234567890abcdef"])
async def test_tracked_source_and_meta_use_independent_models_and_live_sensor_outputs(model_runtime, address):
    state = model_runtime
    meta = register_alias(state, address=address)
    area = ar.async_get(state.hass).async_create("Synthetic Shared Beacon Room")
    state.scanner._update_area_and_floor(area.id)
    before = source_state(state.advert)
    await profiles(state, address)
    view = meta.get_scanner(SCANNER)
    assert view is not state.advert
    assert view._device is state.device
    assert view.device_address == DEVICE
    assert source_state(state.advert) == before
    batch(state)
    rows = snapshot_observations(state.manager._snapshot())
    assert len(rows) == 2
    for row in rows:
        expected = rssi_to_metres(row["rssi"] + row["rssi_offset"], row["reference_power"], row["attenuation"])
        assert row["raw_distance"] == pytest.approx(expected)
        assert row["filtered_distance"] == pytest.approx(expected)
        assert row["source_id"] == DEVICE
    assert state.device.area_advert is state.advert
    assert meta.area_advert is view
    for device in (state.device, meta):
        raw = BermudaSensorScannerRangeRaw(state.coordinator, state.entry, device.address, SCANNER)
        filtered = BermudaSensorScannerRange(state.coordinator, state.entry, device.address, SCANNER)
        closest = BermudaSensorRange(state.coordinator, state.entry, device.address)
        assert raw.native_value == round(device.get_scanner(SCANNER).rssi_distance_raw, 3)
        assert filtered.native_value == round(device.area_distance, 3)
        assert closest.native_value == round(device.area_distance, 1)
        assert raw.unique_id == f"{device.unique_id}_{state.scanner.entity_id_address}_range_raw"
    assert state.device.area_distance != meta.area_distance


async def test_each_filter_sees_fresh_packets_once_and_cached_batches_preserve_source_history(model_runtime):
    state = model_runtime
    meta = register_alias(state)
    await profiles(state)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet", BEACON: "stationary"}}})
    for _index in range(6):
        batch(state, rssi=-68, repeat_prepare=True)
    view = meta.get_scanner(SCANNER)
    first = state.manager._states[(DEVICE, SCANNER)]
    second = state.manager._states[(BEACON, SCANNER)]
    assert first.mode == "pet" and second.mode == "stationary"
    assert first.snapshot(state.clock["now"])["samples"] == 6
    assert second.snapshot(state.clock["now"])["samples"] == 6
    assert view.hist_rssi == state.advert.hist_rssi
    assert view.hist_rssi is not state.advert.hist_rssi
    assert view.hist_stamp is not state.advert.hist_stamp
    frozen = source_state(state.advert)
    histories = list(state.advert.hist_distance_by_interval), list(view.hist_distance_by_interval)
    for _index in range(3):
        batch(state, repeat_prepare=True)
    assert meta.get_scanner(SCANNER) is view
    assert source_state(state.advert) == frozen
    assert first.snapshot(state.clock["now"])["samples"] == 6
    assert second.snapshot(state.clock["now"])["samples"] == 6
    assert (state.advert.hist_distance_by_interval, view.hist_distance_by_interval) == histories
    batch(state, rssi=-38)
    assert state.advert.rssi == view.rssi == -38
    assert first.snapshot(state.clock["now"])["samples"] == 7
    assert second.snapshot(state.clock["now"])["samples"] == 7
    for row in snapshot_observations(state.manager._snapshot()):
        assert row["filtered_rssi"] != row["rssi"]
        expected = rssi_to_metres(row["filtered_rssi"] + row["rssi_offset"], row["reference_power"], row["attenuation"])
        assert row["filtered_distance"] == pytest.approx(expected)


@pytest.mark.parametrize("mode", ["legacy", "pet", "stationary"])
async def test_each_live_correction_matches_its_frozen_replay_and_preserves_filter_state(model_runtime, mode):
    state = model_runtime
    meta = register_alias(state)
    await profiles(state)
    if mode != "legacy":
        await state.manager.async_dispatch("settings", {"settings": {"filters": {BEACON: mode}}})
    for _index in range(6):
        batch(state, rssi=-68)
    batch(state, rssi=-80)
    view = meta.get_scanner(SCANNER)
    frozen_source = source_state(state.advert)
    filters = dict(state.manager._states)
    qualities = {key: deepcopy(value.snapshot(state.clock["now"])) for key, value in filters.items()}
    snapshot = state.manager._snapshot()
    history = DiagnosticHistory(enabled=True)
    history.record(
        snapshot_observations(snapshot), captured_at=snapshot["captured"], model_version=state.manager.version()
    )
    report = ReplayEngine().evaluate(history.frames(), proposed_offsets={SCANNER: 4.5}, now=snapshot["captured"])
    predictions = {row["device_id"]: row["candidate"] for row in report["observations"]}
    await state.manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 4.5}})
    for row in snapshot_observations(state.manager._snapshot()):
        assert row["filtered_distance"] == pytest.approx(predictions[row["device_id"]])
    assert meta.get_scanner(SCANNER) is view
    assert source_state(state.advert) == frozen_source
    assert state.manager._states == filters
    assert {key: value.snapshot(state.clock["now"]) for key, value in filters.items()} == qualities


@pytest.mark.parametrize("remove", ["source", "meta"])
async def test_owner_removal_keeps_surviving_output_and_untracked_api_uses_applied_model(model_runtime, remove):
    state = model_runtime
    meta = register_alias(state)
    await profiles(state)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {BEACON: "pet"}}})
    for _index in range(4):
        batch(state, rssi=-68)
    retained = meta if remove == "source" else state.device
    removed = state.device if remove == "source" else meta
    owner = retained.address
    old_output = retained.get_scanner(SCANNER).rssi_distance
    old_filter = state.manager._states[(owner, SCANNER)]
    frozen = source_state(state.advert)
    removed.create_sensor = False
    state.coordinator.options[CONF_DEVICES] = [owner.upper()]
    batch(state)
    assert retained.get_scanner(SCANNER) is state.advert
    assert meta.get_scanner(SCANNER) is state.advert
    assert state.advert.rssi_distance == pytest.approx(old_output)
    assert state.manager._states[(owner, SCANNER)] is old_filter
    assert (removed.address, SCANNER) not in state.manager._states
    assert source_state(state.advert) == frozen
    snapshot = async_get_advert_snapshot(state.hass, entry_id=state.entry.entry_id, tracked_only=False)
    rows = snapshot_observations(snapshot)
    assert {row["device_id"] for row in rows} == {DEVICE, BEACON}
    for row in rows:
        actual = state.manager.effective_advert_model(state.advert)
        assert row["reference_power"] == actual["reference_power"]
        assert row["attenuation"] == actual["attenuation"]
        assert row["quality"]["mode"] == old_filter.mode
        assert row["source_id"] == DEVICE
        expected = rssi_to_metres(row["rssi"] + row["rssi_offset"], row["reference_power"], row["attenuation"])
        assert row["raw_distance"] == pytest.approx(expected)


@pytest.mark.parametrize("rssi", [-72, -110])
async def test_source_replacement_replaces_derived_view_and_does_not_retain_old_owner_metadata(model_runtime, rssi):
    state = model_runtime
    meta = register_alias(state)
    await profiles(state)
    area = ar.async_get(state.hass).async_create("Synthetic Replacement Source Room")
    state.scanner._update_area_and_floor(area.id)
    batch(state, rssi=-68)
    original, old_view = state.advert, meta.get_scanner(SCANNER)
    meta.area_advert = old_view
    state.clock["now"] += 1
    state.scanner.stamps[DEVICE.upper()] = state.clock["now"]
    state.advert = BermudaAdvert(
        state.device,
        AdvertisementData(
            local_name="Synthetic Replacement",
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            tx_power=-59,
            rssi=rssi,
            platform_data=(),
        ),
        state.coordinator.options,
        state.scanner,
    )
    state.device.adverts[(DEVICE, SCANNER)] = state.advert
    batch(state)
    new_view = meta.get_scanner(SCANNER)
    assert new_view is not old_view
    assert new_view._source_advert is state.advert
    assert meta.area_advert is (new_view if rssi == -72 else None)
    assert new_view.rssi == state.advert.rssi == rssi
    assert original not in state.manager._owners
    assert old_view not in state.manager._owners
    assert len(state.manager._owners) == 2
    for row in snapshot_observations(state.manager._snapshot()):
        expected = rssi_to_metres(row["rssi"] + row["rssi_offset"], row["reference_power"], row["attenuation"])
        assert row["raw_distance"] == pytest.approx(expected)


async def test_single_tracked_metadevice_retains_shared_advert_and_legacy_power_default(model_runtime):
    state = model_runtime
    meta = register_alias(state)
    state.device.create_sensor = False
    state.coordinator.options[CONF_DEVICES] = [BEACON.upper()]
    meta.set_ref_power(-62)
    batch(state)
    assert meta.get_scanner(SCANNER) is state.advert
    assert state.device.ref_power == state.advert.ref_power == -62
    assert state.manager._owners == {state.advert: BEACON}
    row = snapshot_observations(state.manager._snapshot())[0]
    assert row["reference_power"] == -62
    assert row["attenuation"] == 3
    assert row["quality"]["mode"] == "legacy"


async def test_pending_physical_packet_survives_profile_changes_before_the_batch(model_runtime):
    state = model_runtime
    meta = register_alias(state)
    await profiles(state)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet", BEACON: "stationary"}}})
    batch(state, rssi=-68)
    before = {key: value.snapshot(state.clock["now"])["samples"] for key, value in state.manager._states.items()}
    state.clock["now"] += 1
    state.scanner.stamps[DEVICE.upper()] = state.clock["now"]
    state.advert.update_advertisement(
        AdvertisementData(
            local_name="Synthetic Pending Packet",
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            tx_power=-59,
            rssi=-70,
            platform_data=(),
        ),
        state.scanner,
    )
    frozen = source_state(state.advert)
    await state.manager.async_dispatch("profile", {"kind": "beacon", "id": BEACON, "profile": {"attenuation": 2.5}})
    await state.manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 3}})
    assert source_state(state.advert) == frozen
    batch(state, repeat_prepare=True)
    assert meta.get_scanner(SCANNER).new_stamp is None
    for key, value in state.manager._states.items():
        assert value.snapshot(state.clock["now"])["samples"] == before[key] + 1
    for row in snapshot_observations(state.manager._snapshot()):
        expected = rssi_to_metres(row["rssi"] + row["rssi_offset"], row["reference_power"], row["attenuation"])
        assert row["raw_distance"] == pytest.approx(expected)


async def test_source_shared_by_two_metadevices_preserves_each_surviving_owner(model_runtime):
    state = model_runtime
    ibeacon = register_alias(state)
    private = register_alias(state, address="1234567890abcdef1234567890abcdef")
    private_owner = public_device_id(private)
    await state.manager.async_dispatch(
        "settings",
        {
            "settings": {
                "beacon_profiles": {
                    DEVICE: {"reference_power": -55},
                    BEACON: {"reference_power": -60},
                    private_owner: {"reference_power": -65},
                },
                "filters": {DEVICE: "legacy", BEACON: "pet", private_owner: "stationary"},
            }
        },
    )
    for _index in range(3):
        batch(state, rssi=-68)
    first, second = ibeacon.get_scanner(SCANNER), private.get_scanner(SCANNER)
    assert len({id(state.advert), id(first), id(second)}) == 3
    before = (first.rssi_distance, second.rssi_distance)
    state.device.create_sensor = False
    state.coordinator.options[CONF_DEVICES] = [BEACON.upper(), private.address.upper()]
    batch(state)
    assert ibeacon.get_scanner(SCANNER) is state.advert
    assert private.get_scanner(SCANNER) is second
    assert state.advert.rssi_distance == before[0]
    assert second.rssi_distance == before[1]
    assert len(state.manager._owners) == 2


@pytest.mark.parametrize("mode", ["legacy", "pet"])
async def test_existing_reference_power_numbers_remain_independent_across_batches(model_runtime, mode):
    state = model_runtime
    meta = register_alias(state)
    source_number = BermudaNumber(state.coordinator, state.entry, DEVICE)
    meta_number = BermudaNumber(state.coordinator, state.entry, BEACON)
    # The real Number updates run before the first ownership batch, with no
    # toolkit reference-power overrides masking their legacy values.
    source_number._set_ref_power_and_invalidate(-58)
    meta_number._set_ref_power_and_invalidate(-65)
    if mode != "legacy":
        await state.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: mode, BEACON: mode}}})
    for _index in range(6):
        batch(state, rssi=-68)
    batch(state, rssi=-80)
    view = meta.get_scanner(SCANNER)
    revisions = state.device.model_revision, meta.model_revision
    source_filtered, meta_filtered = state.advert.rssi_distance, view.rssi_distance
    source_history, meta_history = list(state.advert.hist_distance_by_interval), list(view.hist_distance_by_interval)
    assert source_filtered != pytest.approx(state.advert.rssi_distance_raw)
    assert meta_filtered != pytest.approx(view.rssi_distance_raw)
    batch(state)
    assert source_number.native_value == state.advert.ref_power == -58
    assert meta_number.native_value == view.ref_power == -65
    assert (state.device.model_revision, meta.model_revision) == revisions
    if mode != "legacy":
        assert state.advert.rssi_distance == source_filtered
        assert view.rssi_distance == meta_filtered
        assert state.advert.hist_distance_by_interval == source_history
        assert view.hist_distance_by_interval == meta_history
    else:
        assert state.advert.hist_distance_by_interval[1:] == source_history
        assert view.hist_distance_by_interval[1:] == meta_history
    for row in snapshot_observations(state.manager._snapshot()):
        assert row["reference_power"] == (-58 if row["device_id"] == DEVICE else -65)
        expected = rssi_to_metres(row["rssi"] + row["rssi_offset"], row["reference_power"], row["attenuation"])
        assert row["raw_distance"] == pytest.approx(expected)


async def test_number_owner_removal_preserves_surviving_legacy_smoothing(model_runtime):
    state = model_runtime
    meta = register_alias(state)
    BermudaNumber(state.coordinator, state.entry, DEVICE)._set_ref_power_and_invalidate(-58)
    BermudaNumber(state.coordinator, state.entry, BEACON)._set_ref_power_and_invalidate(-65)
    for _index in range(6):
        batch(state, rssi=-68)
    batch(state, rssi=-80)
    view = meta.get_scanner(SCANNER)
    history = list(view.hist_distance_by_interval)
    assert view.rssi_distance != pytest.approx(view.rssi_distance_raw)
    state.device.create_sensor = False
    state.coordinator.options[CONF_DEVICES] = [BEACON.upper()]
    batch(state)
    assert meta.get_scanner(SCANNER) is state.advert
    assert state.advert.ref_power == state.device.ref_power == meta.ref_power == -65
    assert state.advert.rssi_distance != pytest.approx(state.advert.rssi_distance_raw)
    # The shared single-owner path calculates twice, retaining its old default.
    assert state.advert.hist_distance_by_interval[2:] == history


async def test_receiver_object_replacement_refreshes_view_metadata_without_new_samples(model_runtime):
    state = model_runtime
    meta = register_alias(state)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {BEACON: "pet"}}})
    batch(state, rssi=-68)
    view = meta.get_scanner(SCANNER)
    frozen = source_state(state.advert)
    samples = state.manager._states[(BEACON, SCANNER)].snapshot(state.clock["now"])["samples"]
    replacement = BermudaDevice(SCANNER, state.coordinator)
    replacement._is_scanner = replacement._is_remote_scanner = True
    replacement.stamps = dict(state.scanner.stamps)
    replacement.name = "Synthetic replacement receiver"
    area = ar.async_get(state.hass).async_create("Synthetic replacement room")
    replacement._update_area_and_floor(area.id)
    state.coordinator.devices[SCANNER] = replacement
    state.advert.apply_new_scanner(replacement)
    batch(state)
    assert view.scanner_device is replacement
    assert view.area_id == area.id
    assert view.name == replacement.name
    assert source_state(state.advert) == frozen
    assert state.manager._states[(BEACON, SCANNER)].snapshot(state.clock["now"])["samples"] == samples
