"""Existing adverts follow scanner registry/binding metadata without new packets."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.helpers import area_registry as ar
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, runtime


class BackendScanner(SimpleNamespace):
    def __hash__(self):
        return id(self)


@pytest.fixture
async def metadata_runtime(runtime, monkeypatch):
    # Registry notifications use the real coordinator refresh path. Keep this
    # synthetic receiver present in its backend roster rather than demoting it.
    backend = BackendScanner(source=SCANNER, name="Synthetic Receiver")
    runtime.scanner._hascanner = backend
    runtime.coordinator._manager = SimpleNamespace(async_current_scanners=lambda: {backend})
    runtime.coordinator._hascanners = {backend}
    runtime.clock = {"now": SOURCE_STAMP}
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device", "coordinator"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: runtime.clock["now"])
    return runtime


def fresh_packet(runtime):
    runtime.clock["now"] += 1
    runtime.scanner.stamps[DEVICE.upper()] = runtime.clock["now"]
    packet = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=runtime.advert.rssi,
        platform_data=(),
    )
    runtime.advert.update_advertisement(packet, runtime.scanner)
    runtime.advert.calculate_data()


def refresh_receiver_registry(runtime):
    runtime.coordinator._refresh_scanners(force=True)


def source_measurements(advert):
    return {
        "stamp": advert.stamp,
        "new_stamp": advert.new_stamp,
        "rssi": advert.rssi,
        "raw_distance": advert.rssi_distance_raw,
        "hist_stamp": list(advert.hist_stamp),
        "hist_rssi": list(advert.hist_rssi),
        "hist_distance": list(advert.hist_distance),
        "hist_interval": list(advert.hist_interval),
    }


def next_area_cycle(runtime):
    runtime.device.calculate_data()
    runtime.coordinator._refresh_area_by_min_distance(runtime.device)


async def test_actual_manager_manual_binding_refreshes_existing_advert_and_next_area(metadata_runtime, device_registry):
    runtime = metadata_runtime
    registry = ar.async_get(runtime.hass)
    area_a = registry.async_create("Synthetic original room")
    area_b = registry.async_create("Synthetic rebound room")
    device_registry.async_update_device(runtime.registry_device.id, area_id=area_a.id)
    refresh_receiver_registry(runtime)
    next_area_cycle(runtime)
    assert runtime.advert.area_id == area_a.id
    assert runtime.device.area_id == area_a.id
    original_identity = runtime.scanner.unique_id, runtime.scanner.entity_id_address
    original_source = source_measurements(runtime.advert)
    original_scanner_stamp = runtime.scanner.last_seen
    manual_entry = MockConfigEntry(domain="bluetooth", entry_id="synthetic-manual-receiver", data={})
    manual_entry.add_to_hass(runtime.hass)
    manual = device_registry.async_get_or_create(
        config_entry_id=manual_entry.entry_id,
        connections={("bluetooth", "AA:BB:CC:DD:EE:13")},
        name="Synthetic rebound receiver",
    )
    device_registry.async_update_device(manual.id, area_id=area_b.id)
    await runtime.manager.async_dispatch(
        "settings", {"settings": {"scanner_bindings": {SCANNER: {"device_registry_id": manual.id}}}}
    )
    assert runtime.scanner.entry_id == manual.id
    assert runtime.scanner.area_id == area_b.id
    assert runtime.scanner.scanner_binding["method"] == "manual"
    assert runtime.advert.area_id == area_b.id  # explicit rebinding refreshes existing observations immediately
    next_area_cycle(runtime)
    assert runtime.advert.scanner_device is runtime.scanner
    assert runtime.advert.area_id == area_b.id
    assert runtime.advert.area_name == area_b.name
    assert runtime.advert.name == runtime.scanner.name == "Synthetic rebound receiver"
    assert runtime.device.area_id == area_b.id
    assert runtime.device.area_advert is runtime.advert
    assert source_measurements(runtime.advert) == original_source
    assert runtime.scanner.last_seen == original_scanner_stamp == SOURCE_STAMP
    assert (runtime.scanner.unique_id, runtime.scanner.entity_id_address) == original_identity


@pytest.mark.parametrize("mode", ["legacy", "stationary"])
async def test_registry_late_area_assignment_and_area_rename_refresh_cached_advert(
    metadata_runtime, device_registry, mode
):
    runtime = metadata_runtime
    if mode != "legacy":
        await runtime.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: mode}}})
        fresh_packet(runtime)
    assert runtime.advert.area_id is None
    assert runtime.device.area_id is None
    original_source = source_measurements(runtime.advert)
    area = ar.async_get(runtime.hass).async_create("Synthetic newly assigned room")
    device_registry.async_update_device(runtime.registry_device.id, area_id=area.id, name_by_user="Renamed receiver")
    refresh_receiver_registry(runtime)
    next_area_cycle(runtime)
    assert runtime.advert.area_id == area.id
    assert runtime.advert.area_name == area.name
    assert runtime.advert.name == "Renamed receiver"
    assert runtime.device.area_id == area.id
    assert runtime.device.area_name == area.name
    assert source_measurements(runtime.advert) == original_source
    renamed = ar.async_get(runtime.hass).async_update(area.id, name="Synthetic renamed room")
    refresh_receiver_registry(runtime)
    next_area_cycle(runtime)
    assert runtime.advert.area_name == renamed.name
    assert runtime.device.area_name == renamed.name
    assert source_measurements(runtime.advert) == original_source


async def test_metadata_changes_do_not_add_optional_filter_measurements_or_filtered_history(
    metadata_runtime, device_registry
):
    runtime = metadata_runtime
    await runtime.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "stationary"}}})
    fresh_packet(runtime)
    original_source = source_measurements(runtime.advert)
    state = runtime.manager._states[(DEVICE, SCANNER)]
    original_quality = deepcopy(state.snapshot(runtime.clock["now"]))
    original_filtered_history = list(runtime.advert.hist_distance_by_interval)
    area = ar.async_get(runtime.hass).async_create("Synthetic metadata-only room")
    device_registry.async_update_device(runtime.registry_device.id, area_id=area.id, name_by_user="Metadata receiver")
    refresh_receiver_registry(runtime)
    runtime.scanner._is_remote_scanner = False
    next_area_cycle(runtime)
    assert runtime.advert.name == "Metadata receiver"
    assert runtime.advert.area_id == area.id
    assert runtime.advert.scanner_sends_stamps is False
    assert runtime.device.area_id == area.id
    assert source_measurements(runtime.advert) == original_source
    assert runtime.advert.hist_distance_by_interval == original_filtered_history
    assert state.snapshot(runtime.clock["now"]) == original_quality


async def test_removing_area_from_existing_scanner_clears_elected_device_without_new_packet(
    metadata_runtime, device_registry
):
    runtime = metadata_runtime
    area = ar.async_get(runtime.hass).async_create("Synthetic assigned room")
    device_registry.async_update_device(runtime.registry_device.id, area_id=area.id)
    refresh_receiver_registry(runtime)
    next_area_cycle(runtime)
    assert runtime.device.area_id == area.id
    original_source = source_measurements(runtime.advert)
    device_registry.async_update_device(runtime.registry_device.id, area_id=None)
    refresh_receiver_registry(runtime)
    next_area_cycle(runtime)
    assert runtime.advert.area_id is None
    assert runtime.advert.area_name is None
    assert runtime.device.area_id is None
    assert source_measurements(runtime.advert) == original_source
