"""Foreground Bluetooth callbacks cannot freeze identities before Store loading."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BaseHaRemoteScanner, BluetoothChange
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.const import CONF_DEVICES, DOMAIN
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
from custom_components.bermuda.sensor import BermudaSensorScannerRange, BermudaSensorScannerRangeRaw

from .test_toolkit_initialization import gated_host_executor
from .test_toolkit_store import real_store

DEVICE = "aa:bb:cc:dd:ee:01"
SCANNERS = {
    "aa:bb:cc:dd:ee:12": "11:22:33:44:55:66",
    "aa:bb:cc:dd:ee:22": "11:22:33:44:55:76",
}
HISTORIC = {"unique_id": "22:33:44:55:66:70", "entity_id_address": "22:33:44:55:66:71"}


@pytest.mark.parametrize("gate", ["host", "store"])
@pytest.mark.parametrize("saved_identity", [False, True])
async def test_gated_actual_reload_preserves_saved_ids_and_all_receiver_packets(
    hass, device_registry, tmp_path, setup_bermuda_entry, bypass_get_data, gate, saved_identity
):
    entry = setup_bermuda_entry
    old = entry.runtime_data.coordinator.toolkit
    old.store = real_store(hass, tmp_path, old.store.key)
    first = next(iter(SCANNERS))
    if saved_identity:
        old._identities[first] = deepcopy(HISTORIC)
    await old.store.async_save(old._store_payload())
    assert await hass.config_entries.async_unload(entry.entry_id)
    # A configured tracked device ensures the resumed platform creates its ranges.
    hass.config_entries.async_update_entry(entry, options={**dict(entry.options), CONF_DEVICES: [DEVICE.upper()]})
    for source, network in SCANNERS.items():
        proxy_entry = MockConfigEntry(
            domain="esphome",
            entry_id=f"synthetic-proxy-{source}",
            unique_id=network,
            data={"bluetooth_mac_address": source},
        )
        proxy_entry.add_to_hass(hass)
        device_registry.async_get_or_create(
            config_entry_id=proxy_entry.entry_id,
            connections={("mac", network)},
            name=f"Synthetic Receiver {source}",
        )

    entered, release = asyncio.Event(), asyncio.Event()
    if gate == "host":
        entered, release, executor_patch = gated_host_executor(hass)
    else:
        executor_patch = nullcontext()
    manager_class, managers = toolkit_module.ToolkitManager, []

    def construct(coordinator):
        manager = manager_class(coordinator)
        manager.store = real_store(hass, tmp_path, manager.store.key)
        if gate == "store":
            actual_load = manager.store.async_load

            async def gated_load():
                entered.set()
                await release.wait()
                return await actual_load()

            manager.store.async_load = AsyncMock(side_effect=gated_load)
        managers.append(manager)
        return manager

    async def manufacturers_loaded(coordinator):
        coordinator._waitingfor_load_manufacturer_ids = False

    scanners, cleanup = [], []
    with (
        patch.object(toolkit_module, "ToolkitManager", side_effect=construct),
        patch.object(BermudaDataUpdateCoordinator, "async_load_manufacturer_ids", new=manufacturers_loaded),
        executor_patch,
    ):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            manager = managers[0]
            coordinator = manager.coordinator
            assert coordinator._waitingfor_load_manufacturer_ids is False
            assert manager.scanner_identity_ready is False
            assert manager.scanner_identity(first, "premature-choice") == {}
            for source in SCANNERS:
                scanner = BaseHaRemoteScanner(source, source)
                cleanup.append(scanner.async_setup())
                cleanup.append(bluetooth.async_register_scanner(hass, scanner))
                scanners.append(scanner)
            source_stamp = monotonic_time_coarse()
            for scanner in scanners:
                scanner._async_on_advertisement(
                    DEVICE.upper(), -65, "Synthetic Beacon", [], {}, {}, -59, {}, source_stamp
                )
            # Exercise the actual foreground callback after both backend buffers
            # are populated, even when HA suppresses an unchanged advertisement.
            coordinator.stamp_last_update = 0
            coordinator.async_handle_advert(
                scanners[0]._previous_service_info[DEVICE.upper()], BluetoothChange.ADVERTISEMENT
            )
            device = coordinator.devices[DEVICE]
            buffers = dict(device.adverts)
            assert len(buffers) == 2
            assert manager._identities == {}
            assert manager._identity_dirty is False
            for source in SCANNERS:
                assert coordinator.devices[source]._scanner_identity_resolved is False
                advert = device.get_scanner(source)
                assert advert.stamp == source_stamp
                assert advert.rssi == -65
            assert manager._background is None

            release.set()
            assert await asyncio.wait_for(setup, 3) is True
            assert manager.scanner_identity_ready is True
            assert device.adverts == buffers
            coordinator.stamp_last_update = 0
            coordinator.async_handle_advert(
                scanners[0]._previous_service_info[DEVICE.upper()], BluetoothChange.ADVERTISEMENT
            )
            await hass.async_block_till_done()
            for source, network in SCANNERS.items():
                scanner = coordinator.devices[source]
                expected = (
                    HISTORIC
                    if saved_identity and source == first
                    else {
                        "unique_id": network,
                        "entity_id_address": network,
                    }
                )
                assert scanner._scanner_identity_resolved is True
                assert scanner.unique_id == expected["unique_id"]
                assert scanner.entity_id_address == expected["entity_id_address"]
                assert manager._identities[source] == expected
                advert = device.get_scanner(source)
                assert advert is buffers[(DEVICE, source)]
                assert advert.stamp == source_stamp
                assert advert.rssi == -65
                for sensor_type in (BermudaSensorScannerRange, BermudaSensorScannerRangeRaw):
                    sensor = sensor_type(coordinator, entry, DEVICE, source)
                    suffix = "_range" if sensor_type is BermudaSensorScannerRange else "_range_raw"
                    assert sensor.unique_id == f"{device.unique_id}_{expected['entity_id_address']}{suffix}"
                    registered = coordinator.er.async_get_entity_id("sensor", DOMAIN, sensor.unique_id)
                    assert registered is not None
                    assert coordinator.er.async_get(registered).config_entry_id == entry.entry_id
        finally:
            release.set()
            if not setup.done():
                setup.cancel()
                await asyncio.gather(setup, return_exceptions=True)
            for callback in reversed(cleanup):
                callback()
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
