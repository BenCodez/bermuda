"""Accepted live bindings backfill actual HA receiver entities exactly once."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BaseHaRemoteScanner, BluetoothChange
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.const import CONF_DEVICES, DOMAIN, SIGNAL_SCANNERS_CHANGED
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
from custom_components.bermuda.toolkit import OPTIONS_KEY

from .test_toolkit_store import real_store

DEVICES = ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02")
SOURCE = "aa:bb:cc:dd:ee:12"
NETWORK = "11:22:33:44:55:66"


def _ranges(backfill):
    return {
        f"{device}_{backfill.identity['entity_id_address']}_{suffix}": backfill.coordinator.er.async_get_entity_id(
            "sensor", DOMAIN, f"{device}_{backfill.identity['entity_id_address']}_{suffix}"
        )
        for device in DEVICES
        for suffix in ("range", "range_raw")
    }


def _assert_bound_entities(backfill):
    assert backfill.scanner.entry_id == backfill.target.id
    assert backfill.scanner.address_wifi_mac == NETWORK
    assert backfill.scanner.address == backfill.scanner.address_ble_mac == SOURCE
    assert backfill.scanner.area_id == backfill.target.area_id
    assert backfill.scanner.name_by_user == "Synthetic Bound User Name"
    assert backfill.scanner.name == "Synthetic Bound User Name"
    assert backfill.scanner.unique_id == backfill.identity["unique_id"]
    assert backfill.scanner.entity_id_address == backfill.identity["entity_id_address"]
    assert backfill.manager._identities[SOURCE] == backfill.identity
    entities = _ranges(backfill)
    assert len(entities) == 4
    assert all(entities.values())
    assert len(set(entities.values())) == 4
    for unique_id, entity_id in entities.items():
        registered = backfill.coordinator.er.async_get(entity_id)
        assert registered.unique_id == unique_id
        assert registered.config_entry_id == backfill.entry.entry_id
    for device, advert in backfill.adverts.items():
        assert backfill.coordinator.devices[device].get_scanner(SOURCE) is advert
        assert advert.scanner_address == SOURCE
        assert advert.area_id == backfill.target.area_id
        assert advert.name == "Synthetic Bound User Name"
        assert (advert.stamp, advert.rssi) == backfill.sources[device]
    return entities


@pytest.fixture
async def backfill(hass, device_registry, tmp_path, monkeypatch, setup_bermuda_entry, bypass_get_data):
    entry = setup_bermuda_entry
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={CONF_DEVICES: [device.upper() for device in DEVICES]})

    async def manufacturers_loaded(coordinator):
        coordinator._waitingfor_load_manufacturer_ids = False

    monkeypatch.setattr(BermudaDataUpdateCoordinator, "async_load_manufacturer_ids", manufacturers_loaded)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    manager = coordinator.toolkit
    source_entry = MockConfigEntry(domain="bluetooth", entry_id="synthetic-source-registry", data={})
    source_entry.add_to_hass(hass)
    source_registry = device_registry.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        connections={("bluetooth", SOURCE.upper())},
        name="Synthetic Source Receiver",
    )
    remote = BaseHaRemoteScanner(SOURCE, SOURCE)
    stop_remote = remote.async_setup()
    unregister = bluetooth.async_register_scanner(hass, remote)
    notices = []
    stop_notices = lambda: None
    try:
        stamp = monotonic_time_coarse()
        for device, rssi in zip(DEVICES, (-65, -67), strict=True):
            remote._async_on_advertisement(device.upper(), rssi, "Synthetic Beacon", [], {}, {}, -59, {}, stamp)
        coordinator.stamp_last_update = 0
        coordinator.async_handle_advert(
            remote._previous_service_info[DEVICES[0].upper()], BluetoothChange.ADVERTISEMENT
        )
        await hass.async_block_till_done()
        scanner = coordinator.devices[SOURCE]
        assert scanner.is_remote_scanner is True
        assert scanner.address_wifi_mac is None
        assert all(coordinator.devices[device].create_all_done for device in DEVICES)
        # Keep its evidenced historical identity while reproducing a source
        # whose previous registry association is currently unavailable.
        device_registry.async_remove_device(source_registry.id)

        # This distant network address has no automatic proof linking it to the
        # receiver source. The explicit binding is what makes it resolvable.
        proxy_entry = MockConfigEntry(domain="esphome", entry_id="synthetic-manual-proxy", unique_id=NETWORK, data={})
        proxy_entry.add_to_hass(hass)
        target = device_registry.async_get_or_create(
            config_entry_id=proxy_entry.entry_id,
            connections={("mac", NETWORK)},
            name="Synthetic Bound Receiver",
        )
        target = device_registry.async_update_device(
            target.id,
            area_id=coordinator.ar.async_create("Synthetic Receiver Area").id,
            name_by_user="Synthetic Bound User Name",
        )
        await hass.async_block_till_done()
        manager.store = real_store(hass, tmp_path, manager.store.key)
        await manager.store.async_save(manager._store_payload())
        stop_notices = async_dispatcher_connect(hass, SIGNAL_SCANNERS_CHANGED, lambda: notices.append(True))
        result = SimpleNamespace(
            hass=hass,
            entry=entry,
            manager=manager,
            coordinator=coordinator,
            scanner=scanner,
            target=target,
            identity=deepcopy(manager._identities[SOURCE]),
            adverts={device: coordinator.devices[device].get_scanner(SOURCE) for device in DEVICES},
            notices=notices,
            bindings={SOURCE: {"device_registry_id": target.id}},
        )
        result.sources = {device: (advert.stamp, advert.rssi) for device, advert in result.adverts.items()}
        assert all(entity is None for entity in _ranges(result).values())
        yield result
    finally:
        stop_notices()
        unregister()
        stop_remote()
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("binding_key", [SOURCE, SOURCE.upper()])
async def test_binding_backfills_only_after_durable_acceptance_and_repeated_save_has_no_duplicates(
    backfill, monkeypatch, binding_key
):
    manager = backfill.manager
    bindings = {binding_key: {"device_registry_id": backfill.target.id}}
    actual = manager.store._async_write_data
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def gated(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        await actual(data)

    monkeypatch.setattr(manager.store, "_async_write_data", gated)
    pending = asyncio.create_task(manager.async_dispatch("settings", {"settings": {"scanner_bindings": bindings}}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert backfill.scanner.address_wifi_mac is None
        assert manager.settings["scanner_bindings"] == {}
        assert backfill.notices == []
        assert all(entity is None for entity in _ranges(backfill).values())
    finally:
        release.set()
        await pending
    await backfill.hass.async_block_till_done()
    assert manager.settings["scanner_bindings"] == bindings
    assert backfill.entry.options[OPTIONS_KEY]["scanner_bindings"] == bindings
    entities = _assert_bound_entities(backfill)
    assert backfill.notices == [True]
    await manager.async_dispatch("settings", {"settings": {"scanner_bindings": bindings}})
    await backfill.hass.async_block_till_done()
    assert _assert_bound_entities(backfill) == entities
    assert backfill.notices == [True]


async def test_late_ordinary_registry_resolution_backfills_same_backend_once(backfill, device_registry):
    matching_mac = "aa:bb:cc:dd:ee:10"
    entry = MockConfigEntry(domain="esphome", entry_id="synthetic-late-network-proof", unique_id=matching_mac, data={})
    entry.add_to_hass(backfill.hass)
    target = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={("mac", matching_mac)},
        name="Synthetic Late Network Receiver",
    )
    area = backfill.coordinator.ar.async_create("Synthetic Late Receiver Area")
    target = device_registry.async_update_device(target.id, area_id=area.id, name_by_user="Synthetic Late User Name")
    await backfill.hass.async_block_till_done()
    assert backfill.scanner.address_wifi_mac is None
    assert all(entity is None for entity in _ranges(backfill).values())
    backend = backfill.scanner._hascanner
    source_connections = set(target.connections)
    backfill.notices.clear()

    backfill.coordinator._refresh_scanners(force=True)
    await backfill.hass.async_block_till_done()
    assert backfill.scanner._hascanner is backend
    assert backfill.scanner.entry_id == target.id
    assert backfill.scanner.address_wifi_mac == matching_mac
    assert backfill.scanner.address == SOURCE
    assert backfill.scanner.scanner_binding["method"] == "legacy_mac_offset"
    assert backfill.scanner.area_id == area.id
    assert backfill.scanner.name == "Synthetic Late User Name"
    assert backfill.scanner.unique_id == backfill.identity["unique_id"]
    assert backfill.scanner.entity_id_address == backfill.identity["entity_id_address"]
    assert backfill.manager._identities[SOURCE] == backfill.identity
    assert backfill.manager.settings["scanner_bindings"] == {}
    assert device_registry.async_get(target.id).connections == source_connections
    entities = _ranges(backfill)
    assert all(entities.values())
    assert len(set(entities.values())) == 4
    assert backfill.notices == [True]
    for device, advert in backfill.adverts.items():
        assert backfill.coordinator.devices[device].get_scanner(SOURCE) is advert
        assert advert.scanner_address == SOURCE
        assert advert.area_id == area.id
        assert advert.name == "Synthetic Late User Name"
        assert (advert.stamp, advert.rssi) == backfill.sources[device]

    backfill.coordinator._refresh_scanners(force=True)
    await backfill.hass.async_block_till_done()
    assert _ranges(backfill) == entities
    assert backfill.notices == [True]


@pytest.mark.parametrize("failed_phase", [1, 2])
async def test_failed_binding_save_has_no_entities_and_retry_preserves_source_identity(
    backfill, monkeypatch, failed_phase
):
    manager = backfill.manager
    original = deepcopy(dict(backfill.entry.options))
    actual = manager.store._async_write_data
    calls = 0

    async def fail(data):
        nonlocal calls
        calls += 1
        if calls == failed_phase:
            raise WriteError("synthetic binding commit failure")
        await actual(data)

    monkeypatch.setattr(manager.store, "_async_write_data", fail)
    with pytest.raises(OSError):
        await manager.async_dispatch("settings", {"settings": {"scanner_bindings": backfill.bindings}})
    await backfill.hass.async_block_till_done()
    assert dict(backfill.entry.options) == original
    assert manager.settings["scanner_bindings"] == {}
    assert backfill.scanner.address_wifi_mac is None
    assert backfill.scanner.entry_id != backfill.target.id
    assert backfill.scanner.area_id is None
    assert backfill.scanner.name_by_user is None
    assert backfill.scanner.unique_id == backfill.identity["unique_id"]
    assert backfill.scanner.entity_id_address == backfill.identity["entity_id_address"]
    assert manager._identities[SOURCE] == backfill.identity
    assert backfill.notices == []
    assert all(entity is None for entity in _ranges(backfill).values())
    # A later ordinary roster signal must not expose a failed manual association.
    async_dispatcher_send(backfill.hass, SIGNAL_SCANNERS_CHANGED)
    await backfill.hass.async_block_till_done()
    assert all(entity is None for entity in _ranges(backfill).values())
    backfill.notices.clear()

    monkeypatch.setattr(manager.store, "_async_write_data", actual)
    await manager.async_dispatch("settings", {"settings": {"scanner_bindings": backfill.bindings}})
    await backfill.hass.async_block_till_done()
    entities = _assert_bound_entities(backfill)
    assert backfill.notices == [True]
    await manager.async_dispatch("settings", {"settings": {"scanner_bindings": backfill.bindings}})
    await backfill.hass.async_block_till_done()
    assert _assert_bound_entities(backfill) == entities
    assert backfill.notices == [True]


@pytest.mark.parametrize("replacement", ["removed", "missing"])
async def test_removed_or_missing_binding_clears_metadata_without_replacing_existing_entities(backfill, replacement):
    manager = backfill.manager
    await manager.async_dispatch("settings", {"settings": {"scanner_bindings": backfill.bindings}})
    await backfill.hass.async_block_till_done()
    entities = _assert_bound_entities(backfill)
    assert backfill.scanner.area_id == backfill.target.area_id
    assert backfill.scanner.name_by_user == "Synthetic Bound User Name"
    aliases = list(backfill.scanner.metadevice_sources)
    bindings = {} if replacement == "removed" else {SOURCE: {"device_registry_id": "synthetic-missing-registry"}}
    await manager.async_dispatch("settings", {"settings": {"scanner_bindings": bindings}})
    await backfill.hass.async_block_till_done()
    assert manager.settings["scanner_bindings"] == bindings
    assert backfill.scanner.entry_id is None
    assert backfill.scanner.address_wifi_mac is None
    assert backfill.scanner.address_ble_mac == SOURCE
    assert backfill.scanner.scanner_binding is None
    assert backfill.scanner.area_id is None
    assert backfill.scanner.name_by_user is None
    assert backfill.scanner.name_devreg is None
    assert backfill.scanner.unique_id == backfill.identity["unique_id"]
    assert backfill.scanner.entity_id_address == backfill.identity["entity_id_address"]
    assert manager._identities[SOURCE] == backfill.identity
    assert backfill.scanner.metadevice_sources == aliases
    assert _ranges(backfill) == entities
    for device, advert in backfill.adverts.items():
        assert advert.area_id is None
        assert (advert.stamp, advert.rssi) == backfill.sources[device]
    await manager.async_dispatch("settings", {"settings": {"scanner_bindings": backfill.bindings}})
    await backfill.hass.async_block_till_done()
    assert _assert_bound_entities(backfill) == entities
    assert backfill.notices == [True, True, True]


@pytest.mark.parametrize("failed_witness_phase", [None, 1, 2])
async def test_external_ha_binding_backfills_even_when_its_store_witness_fails(
    backfill, monkeypatch, failed_witness_phase
):
    manager = backfill.manager
    actual = manager.store._async_write_data
    calls = 0

    async def witness(data):
        nonlocal calls
        calls += 1
        if calls == failed_witness_phase:
            raise WriteError("synthetic external binding witness failure")
        await actual(data)

    monkeypatch.setattr(manager.store, "_async_write_data", witness)
    options = deepcopy(dict(backfill.entry.options))
    options[OPTIONS_KEY] = {**deepcopy(manager.settings), "scanner_bindings": backfill.bindings}
    backfill.hass.config_entries.async_update_entry(backfill.entry, options=options)
    await backfill.hass.async_block_till_done()
    assert backfill.entry.state is ConfigEntryState.LOADED
    assert backfill.entry.options[OPTIONS_KEY]["scanner_bindings"] == backfill.bindings
    assert manager.settings["scanner_bindings"] == backfill.bindings
    entities = _assert_bound_entities(backfill)
    assert backfill.notices == [True]
    assert await manager.async_options_changed(backfill.entry)
    await backfill.hass.async_block_till_done()
    assert _assert_bound_entities(backfill) == entities
    assert backfill.notices == [True]
