"""Scanner identity and calibration labels use synthetic registry devices."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.config_flow import BermudaOptionsFlowHandler
from custom_components.bermuda.const import (
    CONF_DEVICES,
    CONF_RSSI_OFFSETS,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNER_INFO,
    DOMAIN,
)
from custom_components.bermuda.sensor import BermudaSensorScannerRange, BermudaSensorScannerRangeRaw

SOURCE = "aa:bb:cc:dd:ee:12"
BASE = "11:22:33:44:55:66"


def _entry(hass, domain, entry_id, **kwargs):
    entry = MockConfigEntry(domain=domain, entry_id=entry_id, data=kwargs.pop("data", {}), **kwargs)
    entry.add_to_hass(hass)
    return entry


def _scanner(hass, device_registry, options=None, toolkit=None):
    coordinator = SimpleNamespace(
        hass=hass,
        dr=device_registry,
        options=options or {},
        irk_manager=SimpleNamespace(check_mac=lambda address: None),
        get_manufacturer_from_id=lambda address: (None, False),
        toolkit=toolkit,
    )
    scanner = BermudaDevice(SOURCE, coordinator)
    scanner._hascanner = SimpleNamespace(source=SOURCE, name="Synthetic Scanner")
    return scanner, coordinator


def test_esphome_config_entry_bluetooth_mac_outweighs_neighbours(hass, device_registry):
    proxy_entry = _entry(hass, "esphome", "proxy", unique_id=BASE, data={"bluetooth_mac_address": SOURCE.upper()})
    proxy = device_registry.async_get_or_create(
        config_entry_id=proxy_entry.entry_id,
        connections={("mac", BASE)},
        name="Synthetic Proven Receiver",
    )
    wrong_entry = _entry(hass, "esphome", "neighbour", unique_id="aa:bb:cc:dd:ee:10")
    device_registry.async_get_or_create(
        config_entry_id=wrong_entry.entry_id,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        name="Synthetic Neighbour",
    )
    scanner, _ = _scanner(hass, device_registry)
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.entry_id == proxy.id
    assert scanner.name_devreg == "Synthetic Proven Receiver"
    assert scanner.address_wifi_mac == BASE
    assert scanner.address_ble_mac == SOURCE
    assert scanner.scanner_binding["method"] == "esphome_bluetooth_mac"
    assert scanner.address == SOURCE
    assert scanner.metadevice_sources == []  # no fabricated WiFi +/- aliases


def test_esphome_runtime_info_is_an_explicit_mac_proof(hass, device_registry):
    entry = _entry(hass, "esphome", "proxy-runtime", unique_id=BASE)
    entry.runtime_data = SimpleNamespace(device_info=SimpleNamespace(bluetooth_mac_address=SOURCE, mac_address=BASE))
    proxy = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, connections={("mac", BASE)}, name="Synthetic Runtime Receiver"
    )
    scanner, _ = _scanner(hass, device_registry)
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.entry_id == proxy.id
    assert scanner.scanner_binding["method"] == "esphome_bluetooth_mac"


def test_friendly_name_alone_cannot_bind_an_esphome_receiver(hass, device_registry):
    entry = _entry(hass, "esphome", "unproven", title="Synthetic Scanner", unique_id=BASE)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, connections={("mac", BASE)}, name="Synthetic Scanner"
    )
    scanner, _ = _scanner(hass, device_registry)
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.entry_id is None
    assert scanner.address_wifi_mac is None


def test_manual_binding_uses_registry_id_and_retains_resolved_identity(hass, device_registry):
    legacy = _entry(hass, "esphome", "legacy", unique_id="aa:bb:cc:dd:ee:10")
    device_registry.async_get_or_create(
        config_entry_id=legacy.entry_id,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        name="Synthetic Legacy Receiver",
    )
    manual_entry = _entry(hass, "esphome", "manual", unique_id=BASE)
    manual = device_registry.async_get_or_create(
        config_entry_id=manual_entry.entry_id, connections={("mac", BASE)}, name="Synthetic Manual Receiver"
    )
    scanner, coordinator = _scanner(hass, device_registry)
    scanner.async_as_scanner_resolve_device_entries()
    original_uid = scanner.unique_id
    original_entity_address = scanner.entity_id_address
    coordinator.options["measurement_toolkit"] = {"scanner_bindings": {SOURCE: {"device_registry_id": manual.id}}}
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.entry_id == manual.id
    assert scanner.name_devreg == "Synthetic Manual Receiver"
    assert scanner.address_wifi_mac == BASE
    assert scanner.unique_id == original_uid
    assert scanner.entity_id_address == original_entity_address
    assert scanner.address == SOURCE
    assert scanner.scanner_binding["method"] == "manual"


@pytest.mark.parametrize("manual_connections", ["mac", "bluetooth", "none"])
def test_manual_registry_metadata_overrides_old_bluetooth_source_and_refreshes_adverts(
    hass, device_registry, manual_connections
):
    old_area = ar.async_get(hass).async_create("Synthetic Old Area")
    intended_area = ar.async_get(hass).async_create("Synthetic Intended Area")
    old_bt_entry = _entry(hass, "bluetooth", "old-bt")
    old_bt = device_registry.async_get_or_create(
        config_entry_id=old_bt_entry.entry_id,
        connections={("bluetooth", SOURCE.upper())},
        name="Synthetic Old Receiver",
    )
    old_bt = device_registry.async_update_device(old_bt.id, name_by_user="Old User Name", area_id=old_area.id)
    legacy_entry = _entry(hass, "esphome", "old-network", unique_id="aa:bb:cc:dd:ee:10")
    device_registry.async_get_or_create(
        config_entry_id=legacy_entry.entry_id,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        name="Synthetic Legacy Network Receiver",
    )
    manual_entry = _entry(hass, "esphome", "new-network", unique_id=BASE)
    connections = (
        {(manual_connections, BASE.upper() if manual_connections == "bluetooth" else BASE)}
        if manual_connections != "none"
        else set()
    )
    manual = device_registry.async_get_or_create(
        config_entry_id=manual_entry.entry_id,
        connections=connections,
        identifiers={("esphome", "synthetic-explicit-receiver")},
        name="Synthetic Intended Receiver",
    )
    manual = device_registry.async_update_device(manual.id, name_by_user="Intended User Name", area_id=intended_area.id)
    scanner, coordinator = _scanner(hass, device_registry)
    scanner._is_scanner = scanner._is_remote_scanner = True
    scanner.async_as_scanner_resolve_device_entries()
    preserved_uid, preserved_entity_address = scanner.unique_id, scanner.entity_id_address
    assert scanner.area_id == old_area.id
    assert scanner.entry_id == old_bt.id
    assert scanner.name == "Old User Name"
    target_address = "aa:bb:cc:dd:ee:01"
    target = BermudaDevice(target_address, coordinator)
    scanner.stamps = {target_address.upper(): 100.0}
    data = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=-68,
        platform_data=(),
    )
    advert = BermudaAdvert(target, data, coordinator.options, scanner)
    target.adverts[(target_address, SOURCE)] = advert
    coordinator.devices = {SOURCE: scanner, target_address: target}
    original_stamp, original_rssi = advert.stamp, advert.rssi
    source_connections = set(old_bt.connections)
    coordinator.options["measurement_toolkit"] = {"scanner_bindings": {SOURCE: {"device_registry_id": manual.id}}}
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.area_id == intended_area.id
    assert scanner.area_name == intended_area.name
    assert scanner.entry_id == manual.id
    assert scanner.name_by_user == "Intended User Name"
    assert scanner.name == "Intended User Name"
    assert scanner.name_devreg == "Synthetic Intended Receiver"
    assert scanner.scanner_binding["device_registry_id"] == manual.id
    assert scanner.scanner_binding["method"] == "manual"
    assert scanner.scanner_binding["status"] == "resolved"
    assert scanner.address == scanner.address_ble_mac == SOURCE
    assert scanner.unique_id == preserved_uid
    assert scanner.entity_id_address == preserved_entity_address
    assert device_registry.async_get(old_bt.id).connections == source_connections
    assert advert.name == "Intended User Name"
    assert advert.area_id == intended_area.id
    assert advert.area_name == intended_area.name
    assert advert.scanner_device is scanner
    assert advert.scanner_address == SOURCE
    assert advert.stamp == original_stamp and advert.rssi == original_rssi
    with patch.object(advert, "apply_new_scanner", wraps=advert.apply_new_scanner) as refresh:
        scanner.async_as_scanner_resolve_device_entries()
        refresh.assert_not_called()
    device_registry.async_update_device(manual.id, area_id=None, name_by_user=None)
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.area_id is None
    assert scanner.name_by_user is None
    assert scanner.name == "Synthetic Intended Receiver"
    assert scanner.entry_id == manual.id
    assert advert.area_id is None
    assert advert.name == "Synthetic Intended Receiver"
    assert scanner.unique_id == preserved_uid and scanner.entity_id_address == preserved_entity_address


def test_identity_hook_restores_legacy_entity_keys_on_reload(hass, device_registry):
    entry = _entry(hass, "esphome", "reload", unique_id=BASE, data={"bluetooth_mac_address": SOURCE})
    device_registry.async_get_or_create(config_entry_id=entry.entry_id, connections={("mac", BASE)}, name="Receiver")
    stable_identity = {"unique_id": "aa:bb:cc:dd:ee:10", "entity_id_address": "aa:bb:cc:dd:ee:10"}
    toolkit = SimpleNamespace(scanner_identity=lambda source, proposed: stable_identity)
    scanner, coordinator = _scanner(hass, device_registry, toolkit=toolkit)
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.unique_id == "aa:bb:cc:dd:ee:10"
    assert scanner.entity_id_address == "aa:bb:cc:dd:ee:10"
    assert scanner.address_wifi_mac == BASE
    target = SimpleNamespace(unique_id="target", name="Target")
    coordinator.devices = {"target": target, SOURCE: scanner}
    for sensor_class, suffix in [(BermudaSensorScannerRange, "range"), (BermudaSensorScannerRangeRaw, "range_raw")]:
        sensor = object.__new__(sensor_class)
        sensor._device = target
        sensor._scanner = scanner
        assert sensor.unique_id == f"target_aa:bb:cc:dd:ee:10_{suffix}"


def test_missing_manual_binding_is_reported_without_rekey(hass, device_registry):
    entry = _entry(hass, "esphome", "legacy-missing", unique_id="aa:bb:cc:dd:ee:10")
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, connections={("mac", "aa:bb:cc:dd:ee:10")}, name="Receiver"
    )
    scanner, _ = _scanner(
        hass,
        device_registry,
        {"measurement_toolkit": {"scanner_bindings": {SOURCE: {"device_registry_id": "missing"}}}},
    )
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.scanner_binding["status"] == "missing_manual_binding"
    assert scanner.address == SOURCE


def _flow(hass, names=("Shared Name", "Shared Name"), saved=None):
    scanners = {
        f"aa:bb:cc:dd:ee:{index + 1:02x}": SimpleNamespace(
            name=name, unique_id=f"receiver-{index}", entry_id=f"reg-{index}"
        )
        for index, name in enumerate(names)
    }
    entry = _entry(hass, DOMAIN, "calibration", options={CONF_RSSI_OFFSETS: saved or {}})
    coordinator = SimpleNamespace(devices=scanners, scanner_list=list(scanners))
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    flow = BermudaOptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.coordinator = coordinator
    flow.devices = coordinator.devices
    return flow, coordinator


def _defaults(result):
    return next(key.default() for key in result["data_schema"].schema if key.schema == CONF_SCANNER_INFO)


async def test_duplicate_friendly_names_keep_independent_saved_offsets(hass):
    offline = "aa:bb:cc:dd:ee:ff"
    flow, coordinator = _flow(hass, saved={offline: 9.0})
    shown = await flow.async_step_calibration2_scanners()
    defaults = _defaults(shown)
    addresses = list(coordinator.scanner_list)
    assert list(defaults) == [f"Shared Name [{address}]" for address in addresses]
    labels = list(defaults)
    result = await flow.async_step_calibration2_scanners(
        {CONF_DEVICES: "target", CONF_SCANNER_INFO: {labels[0]: 2.0, labels[1]: -3.0}, CONF_SAVE_AND_CLOSE: True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_RSSI_OFFSETS] == {offline: 9.0, addresses[0]: 2.0, addresses[1]: -3.0}


async def test_frozen_labels_survive_renames_and_ignore_new_scanners(hass):
    flow, coordinator = _flow(hass, names=("One", "Two"), saved={"new": 7.0})
    defaults = _defaults(await flow.async_step_calibration2_scanners())
    coordinator.devices[coordinator.scanner_list[0]].name = "Changed Name"
    coordinator.devices["new"] = SimpleNamespace(name="New", unique_id="new", entry_id="new")
    coordinator.scanner_list.append("new")
    result = await flow.async_step_calibration2_scanners(
        {CONF_DEVICES: "target", CONF_SCANNER_INFO: {label: 1.0 for label in defaults}, CONF_SAVE_AND_CLOSE: True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_RSSI_OFFSETS]["new"] == 7.0
    assert all(result["data"][CONF_RSSI_OFFSETS][address] == 1.0 for address in coordinator.scanner_list[:2])


@pytest.mark.parametrize("change", ["missing", "rebound", "network_mac"])
async def test_disappeared_or_rebound_scanner_cannot_save_frozen_form(hass, change):
    flow, coordinator = _flow(hass)
    defaults = _defaults(await flow.async_step_calibration2_scanners())
    if change == "missing":
        coordinator.scanner_list.pop()
    elif change == "rebound":
        coordinator.devices[coordinator.scanner_list[0]].entry_id = "different-registry-owner"
    else:
        coordinator.devices[coordinator.scanner_list[0]].address_wifi_mac = "different-network-interface"
    result = await flow.async_step_calibration2_scanners(
        {CONF_DEVICES: "target", CONF_SCANNER_INFO: defaults, CONF_SAVE_AND_CLOSE: True}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "scanner_changed"}
    assert flow.options[CONF_RSSI_OFFSETS] == {}


@pytest.mark.parametrize("invalid", [True, "5", float("nan"), float("inf"), None])
async def test_invalid_or_partial_offsets_do_not_silently_zero_receivers(hass, invalid):
    flow, _ = _flow(hass, names=("One",), saved={"unrelated": 8.0})
    defaults = _defaults(await flow.async_step_calibration2_scanners())
    submitted = {label: invalid for label in defaults}
    result = await flow.async_step_calibration2_scanners(
        {CONF_DEVICES: "target", CONF_SCANNER_INFO: submitted, CONF_SAVE_AND_CLOSE: True}
    )
    assert result["errors"] == {CONF_SCANNER_INFO: "invalid_scanner_offsets"}
    assert flow.options[CONF_RSSI_OFFSETS] == {"unrelated": 8.0}
    result = await flow.async_step_calibration2_scanners(
        {CONF_DEVICES: "target", CONF_SCANNER_INFO: {}, CONF_SAVE_AND_CLOSE: True}
    )
    assert result["errors"] == {CONF_SCANNER_INFO: "invalid_scanner_offsets"}


async def test_dynamic_markdown_labels_cannot_add_table_columns_or_html(hass):
    flow, _ = _flow(hass, names=("<img src=x> | **Receiver**\nInjected",))
    defaults = _defaults(await flow.async_step_calibration2_scanners())
    target = SimpleNamespace(get_scanner=lambda address: SimpleNamespace(hist_rssi=[-65]))
    with patch.object(flow, "_get_bermuda_device_from_registry", return_value=target):
        result = await flow.async_step_calibration2_scanners(
            {CONF_DEVICES: "target", CONF_SCANNER_INFO: defaults, CONF_SAVE_AND_CLOSE: False}
        )
    suffix = result["description_placeholders"]["suffix"]
    assert "<img" not in suffix
    assert "&lt;img" in suffix
    assert "\\|" in suffix
    assert "\\*\\*Receiver\\*\\*" in suffix
    assert "\nInjected" not in suffix
    json.dumps(result["description_placeholders"])
