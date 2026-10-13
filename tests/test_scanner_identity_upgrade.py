"""First-upgrade identities require exact source evidence or an explicit association."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import DOMAIN
from custom_components.bermuda.sensor import BermudaSensorScannerRange, BermudaSensorScannerRangeRaw
from custom_components.bermuda.toolkit import OPTIONS_KEY, ToolkitManager

from .test_toolkit_store import real_store

SOURCE = "dc:06:75:4e:89:4a"
NETWORK = "dc:06:75:4e:89:48"
OLD = "dc:06:75:4e:89:4c"
DEVICE = "aa:bb:cc:dd:ee:01"


async def make_upgrade(hass, device_registry):
    entry = MockConfigEntry(domain=DOMAIN, entry_id="synthetic-first-upgrade", data={})
    entry.add_to_hass(hass)
    proxy_entry = MockConfigEntry(
        domain="esphome", entry_id="synthetic-proven-proxy", unique_id=NETWORK, data={"bluetooth_mac_address": SOURCE}
    )
    proxy_entry.add_to_hass(hass)
    proxy = device_registry.async_get_or_create(
        config_entry_id=proxy_entry.entry_id, connections={("mac", NETWORK)}, name="Synthetic correct receiver"
    )
    light_entry = MockConfigEntry(domain="esphome", entry_id="synthetic-neighbor-light", data={})
    light_entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=light_entry.entry_id, connections={("mac", OLD)}, name="Synthetic unrelated light"
    )
    bluetooth_entry = MockConfigEntry(domain="bluetooth", entry_id="synthetic-bluetooth-source", data={})
    bluetooth_entry.add_to_hass(hass)
    source = device_registry.async_get_or_create(
        config_entry_id=bluetooth_entry.entry_id,
        connections={("bluetooth", SOURCE.upper())},
        name="Synthetic Bluetooth source",
    )
    beacon = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, DEVICE)}, name="Synthetic tracked beacon"
    )
    registry = er.async_get(hass)
    entities = {}
    for suffix in ("range", "range_raw"):
        entity = registry.async_get_or_create(
            "sensor", DOMAIN, f"{DEVICE}_{OLD}_{suffix}", config_entry=entry, device_id=beacon.id
        )
        entities[suffix] = registry.async_update_entity(
            entity.entity_id, new_entity_id=f"sensor.synthetic_retained_{suffix}"
        )
    coordinator = SimpleNamespace(
        hass=hass, config_entry=entry, options={}, devices={}, get_scanners=[], dr=device_registry, er=registry
    )
    manager = coordinator.toolkit = ToolkitManager(coordinator)
    manager.store = MagicMock(spec=Store)
    manager.store.async_load = AsyncMock(return_value=None)
    manager.store.async_save = AsyncMock()
    scanner = BermudaDevice(SOURCE, coordinator)
    scanner._hascanner = SimpleNamespace(source=SOURCE, name="Synthetic scanner")
    scanner._is_scanner = scanner._is_remote_scanner = True
    coordinator.devices = {SOURCE: scanner, DEVICE: BermudaDevice(DEVICE, coordinator)}
    coordinator.get_scanners = [scanner]
    result = SimpleNamespace(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        manager=manager,
        scanner=scanner,
        entities=entities,
        proxy=proxy,
        source=source,
        beacon=beacon,
    )
    return result


@pytest.fixture
async def upgrade(hass, device_registry):
    result = await make_upgrade(hass, device_registry)
    yield result
    await result.manager.async_close()


@pytest.fixture
async def upgrade_with_core(hass, device_registry, setup_bermuda_entry):
    assert await hass.config_entries.async_unload(setup_bermuda_entry.entry_id)
    result = await make_upgrade(hass, device_registry)
    yield result
    await result.manager.async_close()


def assert_retained(upgrade, unique_id=OLD):
    scanner = upgrade.scanner
    assert scanner.unique_id == unique_id
    assert scanner.entity_id_address == OLD
    assert scanner.address == scanner.address_ble_mac == SOURCE
    assert scanner.address_wifi_mac == NETWORK
    assert scanner.entry_id == upgrade.source.id
    assert scanner.name_devreg == "Synthetic correct receiver"
    for sensor_type, suffix in ((BermudaSensorScannerRange, "range"), (BermudaSensorScannerRangeRaw, "range_raw")):
        sensor = sensor_type(upgrade.coordinator, upgrade.entry, DEVICE, SOURCE)
        assert "Synthetic correct receiver" in sensor.name
        assert sensor.unique_id == upgrade.entities[suffix].unique_id
        assert (
            upgrade.coordinator.er.async_get_entity_id("sensor", DOMAIN, sensor.unique_id)
            == upgrade.entities[suffix].entity_id
        )


@pytest.mark.parametrize("restored", [False, True])
async def test_no_store_exact_range_source_evidence_retains_real_entity_ids_and_metadata(upgrade, restored):
    states = [
        State(entity.entity_id, "2.5", {"area_scanner_mac": SOURCE.upper()}) for entity in upgrade.entities.values()
    ]
    if restored:
        mock_restore_cache(upgrade.hass, states)
    else:
        for state in states:
            upgrade.hass.states.async_set(state.entity_id, state.state, state.attributes)
    await upgrade.manager.async_load()
    assert_retained(upgrade)
    assert upgrade.manager._identities[SOURCE] == {"unique_id": OLD, "entity_id_address": OLD}
    assert upgrade.manager._identity_dirty is True


@pytest.mark.parametrize("evidence", ["none", "wrong_source", "cross_entry", "ambiguous"])
async def test_suffix_or_neighbor_is_not_automatic_identity_proof(upgrade, device_registry, evidence):
    if evidence == "wrong_source":
        for entity in upgrade.entities.values():
            upgrade.hass.states.async_set(entity.entity_id, "2.5", {"area_scanner_mac": "dc:06:75:4e:89:4e"})
    elif evidence == "cross_entry":
        foreign = MockConfigEntry(domain=DOMAIN, entry_id="synthetic-foreign-entry", data={})
        foreign.add_to_hass(upgrade.hass)
        for entity in upgrade.entities.values():
            upgrade.coordinator.er.async_update_entity(entity.entity_id, config_entry_id=foreign.entry_id)
            upgrade.hass.states.async_set(entity.entity_id, "2.5", {"area_scanner_mac": SOURCE})
    elif evidence == "ambiguous":
        for identity in (OLD, "dc:06:75:4e:89:47"):
            for suffix in ("range", "range_raw"):
                entity = upgrade.coordinator.er.async_get_or_create(
                    "sensor",
                    DOMAIN,
                    f"{DEVICE}_{identity}_{suffix}",
                    config_entry=upgrade.entry,
                    device_id=upgrade.beacon.id,
                )
                upgrade.hass.states.async_set(entity.entity_id, "2.5", {"area_scanner_mac": SOURCE})
    await upgrade.manager.async_load()
    assert upgrade.scanner.entity_id_address == NETWORK
    assert upgrade.manager._identities[SOURCE]["entity_id_address"] == NETWORK
    assert all(upgrade.coordinator.er.async_get(entity.entity_id) is not None for entity in upgrade.entities.values())


@pytest.mark.parametrize("saved", [False, True])
async def test_saved_store_and_exact_scanner_registry_identity_precede_range_state(upgrade, device_registry, saved):
    preferred = "synthetic-evidenced-scanner-identity"
    mock_restore_cache(
        upgrade.hass,
        [State(entity.entity_id, "2.5", {"area_scanner_mac": SOURCE}) for entity in upgrade.entities.values()],
    )
    if saved:
        upgrade.manager.store.async_load.return_value = {
            "identities": {SOURCE: {"unique_id": preferred, "entity_id_address": preferred}}
        }
    else:
        device_registry.async_update_device(upgrade.source.id, new_identifiers={(DOMAIN, preferred)})
    await upgrade.manager.async_load()
    assert upgrade.scanner.unique_id == upgrade.scanner.entity_id_address == preferred


@pytest.mark.parametrize("suffix", ["range", "range_raw"])
async def test_explicit_recovery_overrides_provisional_store_only_after_durable_save_and_reload(
    upgrade, tmp_path, monkeypatch, suffix
):
    manager = upgrade.manager
    manager.store = real_store(upgrade.hass, tmp_path, f"{DOMAIN}.measurement_toolkit.{upgrade.entry.entry_id}")
    await manager.async_load()
    assert upgrade.scanner.entity_id_address == NETWORK
    old_ids = deepcopy(manager._identities)
    schedule = MagicMock()
    monkeypatch.setattr(upgrade.hass.config_entries, "async_schedule_reload", schedule)
    recovery = {SOURCE.upper(): {"entity_id": upgrade.entities[suffix].entity_id}}
    result = await manager.async_dispatch("settings", {"settings": {"scanner_identity_recovery": recovery}})
    assert result["reload_scheduled"] is True
    schedule.assert_called_once_with(upgrade.entry.entry_id)
    assert manager._identities == old_ids
    assert upgrade.scanner.entity_id_address == NETWORK
    assert upgrade.entry.options[OPTIONS_KEY]["scanner_identity_recovery"] == {SOURCE: recovery[SOURCE.upper()]}
    await manager.async_close()
    resumed = upgrade.coordinator.toolkit = ToolkitManager(upgrade.coordinator)
    resumed.store = real_store(upgrade.hass, tmp_path, manager.store.key)
    scanner = upgrade.scanner = BermudaDevice(SOURCE, upgrade.coordinator)
    scanner._hascanner = SimpleNamespace(source=SOURCE, name="Synthetic scanner")
    scanner._is_scanner = scanner._is_remote_scanner = True
    upgrade.coordinator.devices[SOURCE] = scanner
    upgrade.coordinator.get_scanners = [scanner]
    await resumed.async_load()
    assert_retained(upgrade)
    assert resumed._identities[SOURCE] == {"unique_id": OLD, "entity_id_address": OLD}


@pytest.mark.parametrize("failure", [1, 2])
async def test_recovery_save_failure_preserves_live_identity_options_and_does_not_reload(upgrade, monkeypatch, failure):
    manager = upgrade.manager
    await manager.async_load()
    options, identities = deepcopy(dict(upgrade.entry.options)), deepcopy(manager._identities)
    schedule = MagicMock()
    monkeypatch.setattr(upgrade.hass.config_entries, "async_schedule_reload", schedule)
    count = 0

    async def fail(payload):
        nonlocal count
        count += 1
        if count == failure:
            raise OSError("Synthetic recovery failure")

    manager.store.async_save.side_effect = fail
    with pytest.raises(OSError):
        await manager.async_dispatch(
            "settings",
            {"settings": {"scanner_identity_recovery": {SOURCE: {"entity_id": upgrade.entities["range"].entity_id}}}},
        )
    assert dict(upgrade.entry.options) == options
    assert manager._identities == identities
    assert upgrade.scanner.entity_id_address == NETWORK
    schedule.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown_source",
        "foreign_entry",
        "closest_range",
        "another_claim",
        "duplicate_claim",
        "conflicting_alias",
        "different_source_proof",
    ],
)
async def test_identity_recovery_rejects_unowned_or_conflicting_associations_atomically(upgrade, monkeypatch, invalid):
    manager = upgrade.manager
    await manager.async_load()
    recovery = {SOURCE: {"entity_id": upgrade.entities["range"].entity_id}}
    if invalid == "unknown_source":
        recovery = {"dc:06:75:4e:89:5a": recovery[SOURCE]}
    elif invalid == "foreign_entry":
        foreign = MockConfigEntry(domain=DOMAIN, entry_id="synthetic-foreign-entry", data={})
        foreign.add_to_hass(upgrade.hass)
        upgrade.coordinator.er.async_update_entity(
            upgrade.entities["range"].entity_id, config_entry_id=foreign.entry_id
        )
    elif invalid == "closest_range":
        entity = upgrade.coordinator.er.async_get_or_create(
            "sensor", DOMAIN, f"{DEVICE}_range", config_entry=upgrade.entry, device_id=upgrade.beacon.id
        )
        recovery[SOURCE]["entity_id"] = entity.entity_id
    elif invalid in ("another_claim", "duplicate_claim"):
        other = "dc:06:75:4e:89:4e"
        if invalid == "another_claim":
            manager._identities[other] = {"unique_id": OLD, "entity_id_address": OLD}
        else:
            upgrade.coordinator.get_scanners.append(SimpleNamespace(address=other))
            recovery[other] = {"entity_id": upgrade.entities["range_raw"].entity_id}
    elif invalid == "conflicting_alias":
        recovery[SOURCE.upper()] = {"entity_id": upgrade.entities["range_raw"].entity_id}
    else:
        upgrade.hass.states.async_set(
            upgrade.entities["range"].entity_id, "2.5", {"area_scanner_mac": "dc:06:75:4e:89:4e"}
        )
    before_options, before_settings = deepcopy(dict(upgrade.entry.options)), deepcopy(manager.settings)
    identities = deepcopy(manager._identities)
    saves = manager.store.async_save.await_count
    schedule = MagicMock()
    monkeypatch.setattr(upgrade.hass.config_entries, "async_schedule_reload", schedule)
    with pytest.raises(ValueError):
        await manager.async_dispatch("settings", {"settings": {"scanner_identity_recovery": recovery}})
    assert dict(upgrade.entry.options) == before_options
    assert manager.settings == before_settings
    assert manager._identities == identities
    assert manager.store.async_save.await_count == saves
    schedule.assert_not_called()


@pytest.mark.parametrize("phase", [1, 2])
async def test_cancelled_recovery_caller_keeps_one_durable_owned_save_before_reload(
    upgrade, tmp_path, monkeypatch, phase
):
    manager = upgrade.manager
    manager.store = real_store(upgrade.hass, tmp_path, f"{DOMAIN}.measurement_toolkit.{upgrade.entry.entry_id}")
    await manager.async_load()
    entered, release = asyncio.Event(), asyncio.Event()
    actual = manager.store._async_write_data
    count = 0

    async def gated(payload):
        nonlocal count
        count += 1
        if count == phase:
            entered.set()
            await release.wait()
        await actual(payload)

    monkeypatch.setattr(manager.store, "_async_write_data", gated)
    schedule = MagicMock()
    monkeypatch.setattr(upgrade.hass.config_entries, "async_schedule_reload", schedule)
    caller = asyncio.create_task(
        manager.async_dispatch(
            "settings",
            {"settings": {"scanner_identity_recovery": {SOURCE: {"entity_id": upgrade.entities["range"].entity_id}}}},
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.settings["scanner_identity_recovery"] == {}
        assert upgrade.scanner.entity_id_address == NETWORK
        schedule.assert_not_called()
        owned = manager._action_task
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert owned is not None and not owned.done()
        release.set()
        result = await asyncio.wait_for(owned, 2)
        assert result["reload_scheduled"] is True
        schedule.assert_called_once_with(upgrade.entry.entry_id)
        assert (await manager.store.async_load())["options_journal"]["desired"][OPTIONS_KEY][
            "scanner_identity_recovery"
        ] == {SOURCE: {"entity_id": upgrade.entities["range"].entity_id}}
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("saved_identity", [False, True])
async def test_actual_first_install_and_reload_reuse_both_registered_range_entities(
    upgrade_with_core, tmp_path, monkeypatch, bypass_get_data, saved_identity
):
    from bluetooth_data_tools import monotonic_time_coarse
    from homeassistant.components import bluetooth
    from homeassistant.components.bluetooth import BaseHaRemoteScanner, BluetoothChange
    from custom_components.bermuda import toolkit as toolkit_module
    from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
    from custom_components.bermuda.const import CONF_DEVICES

    upgrade = upgrade_with_core
    upgrade.hass.config_entries.async_update_entry(upgrade.entry, options={CONF_DEVICES: [DEVICE.upper()]})
    states = [State(entity.entity_id, "2.5", {"area_scanner_mac": SOURCE}) for entity in upgrade.entities.values()]
    mock_restore_cache(upgrade.hass, states)
    manager_class = ToolkitManager
    expected_uid = "synthetic-saved-scanner-uid" if saved_identity else OLD
    if saved_identity:
        store = real_store(upgrade.hass, tmp_path, f"{DOMAIN}.measurement_toolkit.{upgrade.entry.entry_id}")
        await store.async_save({"identities": {SOURCE: {"unique_id": expected_uid, "entity_id_address": OLD}}})

    def construct(coordinator):
        manager = manager_class(coordinator)
        manager.store = real_store(upgrade.hass, tmp_path, manager.store.key)
        return manager

    async def manufacturers_loaded(coordinator):
        coordinator._waitingfor_load_manufacturer_ids = False

    monkeypatch.setattr(toolkit_module, "ToolkitManager", construct)
    monkeypatch.setattr(BermudaDataUpdateCoordinator, "async_load_manufacturer_ids", manufacturers_loaded)
    remote = BaseHaRemoteScanner(SOURCE, SOURCE)
    stop = remote.async_setup()
    unregister = bluetooth.async_register_scanner(upgrade.hass, remote)
    try:
        stamp = monotonic_time_coarse()
        remote._async_on_advertisement(DEVICE.upper(), -65, "Synthetic beacon", [], {}, {}, -59, {}, stamp)
        for _ in range(2):
            assert await upgrade.hass.config_entries.async_setup(upgrade.entry.entry_id)
            coordinator = upgrade.entry.runtime_data.coordinator
            coordinator.stamp_last_update = 0
            coordinator.async_handle_advert(
                remote._previous_service_info[DEVICE.upper()], BluetoothChange.ADVERTISEMENT
            )
            await upgrade.hass.async_block_till_done()
            upgrade.coordinator = coordinator
            upgrade.scanner = coordinator.devices[SOURCE]
            assert coordinator.toolkit._identities[SOURCE] == {"unique_id": expected_uid, "entity_id_address": OLD}
            assert coordinator.scanner_identity_pending is False
            assert_retained(upgrade, expected_uid)
            assert coordinator.devices[DEVICE].get_scanner(SOURCE).stamp == stamp
            for suffix in ("range", "range_raw"):
                assert coordinator.er.async_get_entity_id("sensor", DOMAIN, f"{DEVICE}_{NETWORK}_{suffix}") is None
                assert upgrade.hass.states.get(upgrade.entities[suffix].entity_id) is not None
            assert await upgrade.hass.config_entries.async_unload(upgrade.entry.entry_id)
    finally:
        unregister()
        stop()


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
async def test_constructor_buffered_scanner_waits_for_identity_load_and_retries_after_interruption(
    upgrade_with_core, tmp_path, monkeypatch, bypass_get_data, outcome
):
    from bluetooth_data_tools import monotonic_time_coarse
    from homeassistant.components import bluetooth
    from homeassistant.components.bluetooth import BaseHaRemoteScanner
    from custom_components.bermuda import toolkit as toolkit_module
    from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
    from custom_components.bermuda.const import CONF_DEVICES
    from pathlib import Path

    upgrade = upgrade_with_core
    upgrade.hass.config_entries.async_update_entry(upgrade.entry, options={CONF_DEVICES: [DEVICE.upper()]})
    expected = {"unique_id": "synthetic-saved-scanner-uid", "entity_id_address": OLD}
    key = f"{DOMAIN}.measurement_toolkit.{upgrade.entry.entry_id}"
    store = real_store(upgrade.hass, tmp_path, key)
    await store.async_save({"identities": {SOURCE: expected}})
    before = Path(store.path).read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()
    managers = []

    def construct(coordinator):
        manager = ToolkitManager(coordinator)
        manager.store = real_store(upgrade.hass, tmp_path, key)
        actual = manager.store.async_load
        first = not managers

        async def load():
            if first:
                entered.set()
                await release.wait()
                if outcome == "failure":
                    raise OSError("Synthetic interrupted identity load")
            return await actual()

        manager.store.async_load = load
        managers.append(manager)
        return manager

    async def manufacturers_loaded(coordinator):
        coordinator._waitingfor_load_manufacturer_ids = False

    monkeypatch.setattr(toolkit_module, "ToolkitManager", construct)
    monkeypatch.setattr(BermudaDataUpdateCoordinator, "async_load_manufacturer_ids", manufacturers_loaded)
    remote = BaseHaRemoteScanner(SOURCE, SOURCE)
    stop = remote.async_setup()
    unregister = bluetooth.async_register_scanner(upgrade.hass, remote)
    setup = None
    try:
        remote._async_on_advertisement(
            DEVICE.upper(), -65, "Synthetic beacon", [], {}, {}, -59, {}, monotonic_time_coarse()
        )
        setup = asyncio.create_task(upgrade.hass.config_entries.async_setup(upgrade.entry.entry_id))
        await asyncio.wait_for(entered.wait(), 2)
        manager = managers[0]
        scanner = manager.coordinator.devices[SOURCE]
        assert manager.coordinator.scanner_identity_pending is True
        assert scanner._scanner_identity_resolved is False
        assert scanner.entity_id_address is None
        assert scanner.address_wifi_mac == NETWORK
        assert manager._identities == {} and manager._identity_dirty is False
        assert manager.coordinator.er.async_get_entity_id("sensor", DOMAIN, f"{DEVICE}_{NETWORK}_range") is None
        if outcome == "cancel":
            setup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await setup
        else:
            release.set()
            assert await asyncio.wait_for(setup, 3) is False
        assert manager.closed is True
        assert Path(store.path).read_bytes() == before
        assert manager.coordinator.scanner_identity_pending is True
        release.set()
        assert await upgrade.hass.config_entries.async_reload(upgrade.entry.entry_id)
        coordinator = upgrade.entry.runtime_data.coordinator
        assert coordinator.scanner_identity_pending is False
        assert coordinator.devices[SOURCE].unique_id == expected["unique_id"]
        assert coordinator.devices[SOURCE].entity_id_address == OLD
        assert await upgrade.hass.config_entries.async_unload(upgrade.entry.entry_id)
    finally:
        release.set()
        if setup is not None and not setup.done():
            setup.cancel()
            await asyncio.gather(setup, return_exceptions=True)
        unregister()
        stop()


@pytest.mark.parametrize("witness_fails", [False, True])
async def test_external_recovery_option_uses_normal_reload_once_without_changing_live_identity(
    upgrade, monkeypatch, witness_fails
):
    from custom_components.bermuda import async_reload_entry

    manager = upgrade.manager
    await manager.async_load()
    schedule = MagicMock()
    monkeypatch.setattr(upgrade.hass.config_entries, "async_schedule_reload", schedule)
    upgrade.entry.runtime_data = SimpleNamespace(coordinator=upgrade.coordinator)
    options = deepcopy(dict(upgrade.entry.options))
    options[OPTIONS_KEY] = {
        **manager.settings,
        "scanner_identity_recovery": {SOURCE: {"entity_id": upgrade.entities["range"].entity_id}},
    }
    upgrade.hass.config_entries.async_update_entry(upgrade.entry, options=options)
    if witness_fails:
        manager.store.async_save.side_effect = OSError("Synthetic external recovery witness failure")
    try:
        await async_reload_entry(upgrade.hass, upgrade.entry)
        schedule.assert_called_once_with(upgrade.entry.entry_id)
        assert upgrade.scanner.entity_id_address == NETWORK
        assert manager.settings["scanner_identity_recovery"] == options[OPTIONS_KEY]["scanner_identity_recovery"]
    finally:
        manager.store.async_save.side_effect = None


async def test_actual_standalone_coordinator_keeps_legacy_identity_behavior_without_toolkit(
    upgrade_with_core, monkeypatch
):
    from bluetooth_data_tools import monotonic_time_coarse
    from homeassistant.components import bluetooth
    from homeassistant.components.bluetooth import BaseHaRemoteScanner
    from homeassistant.config_entries import current_entry
    from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

    upgrade = upgrade_with_core

    async def manufacturers_loaded(coordinator):
        coordinator._waitingfor_load_manufacturer_ids = False

    monkeypatch.setattr(BermudaDataUpdateCoordinator, "async_load_manufacturer_ids", manufacturers_loaded)
    remote = BaseHaRemoteScanner(SOURCE, SOURCE)
    stop = remote.async_setup()
    unregister = bluetooth.async_register_scanner(upgrade.hass, remote)
    coordinator = None
    try:
        remote._async_on_advertisement(
            DEVICE.upper(), -65, "Synthetic beacon", [], {}, {}, -59, {}, monotonic_time_coarse()
        )
        token = current_entry.set(upgrade.entry)
        try:
            coordinator = BermudaDataUpdateCoordinator(upgrade.hass, upgrade.entry)
        finally:
            current_entry.reset(token)
        assert coordinator.scanner_identity_pending is False
        scanner = coordinator.devices[SOURCE]
        assert scanner._scanner_identity_resolved is True
        assert scanner.unique_id == scanner.entity_id_address == NETWORK
        assert scanner.name_devreg == "Synthetic correct receiver"
    finally:
        if coordinator is not None and coordinator._ad_listener_cancel:
            coordinator._ad_listener_cancel()
        unregister()
        stop()
