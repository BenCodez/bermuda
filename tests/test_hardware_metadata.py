"""Proven hardware descriptions stay separate from physical radio corrections."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.bermuda.api import async_get_advert_snapshot
from custom_components.bermuda.hardware import hardware_metadata, hardware_text
from custom_components.bermuda.profiles import normalize_profile
from custom_components.bermuda.toolkit import ToolkitManager
from tests.test_scanner_binding_toolkit import BASE, SOURCE, _entry, _scanner
from tests.test_scanner_metadata_refresh import metadata_runtime
from tests.test_toolkit_runtime import DEVICE, SCANNER, runtime

UNKNOWN = {"detected": {}, "effective": {}, "provenance": "unknown"}


@pytest.mark.parametrize("kind", ["receiver", "beacon"])
def test_external_antenna_is_optional_strict_boolean_metadata(kind):
    assert "external_antenna" not in normalize_profile({}, kind=kind)
    for external in (False, True):
        assert normalize_profile({"external_antenna": external}, kind=kind) == {"external_antenna": external}
    for invalid in (None, "true", "false", 0, 1, [], {}):
        with pytest.raises(ValueError, match="external_antenna must be a boolean"):
            normalize_profile({"external_antenna": invalid}, kind=kind)


def test_matched_registry_and_existing_esphome_info_report_only_whitelisted_fields(hass, device_registry):
    entry = _entry(hass, "esphome", "hardware-proxy", unique_id=BASE, data={"bluetooth_mac_address": SOURCE})
    entry.runtime_data = SimpleNamespace(
        device_info=SimpleNamespace(
            bluetooth_mac_address=SOURCE,
            mac_address=BASE,
            manufacturer="Runtime manufacturer",
            model="Generic runtime model",
            platform="ESP32",
            name="Name is not a model",
            api_password="synthetic-api-password",
            encryption_key="synthetic-encryption-key",
            host="192.0.2.5",
        ),
        client=SimpleNamespace(password="synthetic-client-password"),
    )
    proxy = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={("mac", BASE)},
        manufacturer="Synthetic board maker",
        model="Synthetic receiver board",
    )
    scanner, coordinator = _scanner(hass, device_registry)
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.scanner_binding["device_registry_id"] == proxy.id
    hardware = hardware_metadata(coordinator, scanner)
    assert hardware == {
        "detected": {
            "manufacturer": "Synthetic board maker",
            "model": "Synthetic receiver board",
            "hardware_type": "ESP32",
        },
        "effective": {
            "manufacturer": "Synthetic board maker",
            "model": "Synthetic receiver board",
            "hardware_type": "ESP32",
        },
        "provenance": "esphome",
    }
    encoded = json.dumps(hardware)
    for private in (
        BASE,
        SOURCE,
        "synthetic-api-password",
        "synthetic-encryption-key",
        "synthetic-client-password",
        "192.0.2.5",
        "Name is not a model",
    ):
        assert private not in encoded


def test_esphome_runtime_can_fill_missing_registry_model_without_creating_a_connection(hass, device_registry):
    class RuntimeInfo:
        device_info = SimpleNamespace(
            bluetooth_mac_address=SOURCE, mac_address=BASE, manufacturer="Board maker", model="Reported board"
        )

        @property
        def client(self):
            raise AssertionError("Hardware detection must not access the ESPHome API client")

    entry = _entry(hass, "esphome", "hardware-runtime", unique_id=BASE)
    entry.runtime_data = RuntimeInfo()
    device_registry.async_get_or_create(config_entry_id=entry.entry_id, connections={("mac", BASE)})
    scanner, coordinator = _scanner(hass, device_registry)
    scanner.async_as_scanner_resolve_device_entries()
    hardware = hardware_metadata(coordinator, scanner)
    assert hardware["detected"] == {"manufacturer": "Board maker", "model": "Reported board"}
    assert hardware["provenance"] == "esphome"
    assert "hardware_type" not in hardware["detected"]  # a reported board model does not imply its chip family
    entry.runtime_data.device_info.bluetooth_mac_address = "aa:bb:cc:dd:ee:90"
    assert hardware_metadata(coordinator, scanner) == UNKNOWN  # stale explicit identity evidence is not reused


def test_names_oui_and_neighboring_mac_matches_cannot_identify_physical_hardware(hass, device_registry):
    entry = _entry(hass, "esphome", "hardware-neighbour", unique_id="aa:bb:cc:dd:ee:10")
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={("mac", "aa:bb:cc:dd:ee:10")},
        name="Synthetic Scanner ESP32",
        manufacturer="Espressif",
        model="Unproven adjacent board",
    )
    scanner, coordinator = _scanner(hass, device_registry)
    scanner.manufacturer = "Espressif"  # Bluetooth company/OUI lookup is insufficient
    scanner.async_as_scanner_resolve_device_entries()
    assert scanner.scanner_binding["method"] == "legacy_mac_offset"
    assert hardware_metadata(coordinator, scanner) == UNKNOWN


async def test_shared_api_and_toolkit_diagnostics_refresh_registry_metadata_without_new_packets(
    metadata_runtime, device_registry
):
    runtime = metadata_runtime
    scanner = runtime.scanner
    version = runtime.manager.version()
    original = (
        runtime.advert.stamp,
        runtime.advert.rssi,
        runtime.advert.rssi_distance_raw,
        list(runtime.advert.hist_rssi),
    )
    device_registry.async_update_device(
        runtime.registry_device.id, manufacturer="Board maker", model="Reported receiver"
    )
    snapshot = async_get_advert_snapshot(runtime.hass, include_empty=True)
    hardware = snapshot["scanners"][SCANNER]["hardware"]
    assert hardware == {
        "detected": {"manufacturer": "Board maker", "model": "Reported receiver"},
        "effective": {"manufacturer": "Board maker", "model": "Reported receiver"},
        "provenance": "home_assistant_device_registry",
    }
    assert snapshot["devices"][DEVICE]["scanners"][SCANNER]["hardware"] == hardware
    snapshot["scanners"][SCANNER]["hardware"]["detected"]["model"] = "Caller mutation"
    diagnostics = await runtime.manager.async_dispatch("diagnostics", {})
    assert diagnostics["diagnostics"]["scanner_hardware"][SCANNER]["detected"]["model"] == "Reported receiver"
    device_registry.async_update_device(runtime.registry_device.id, manufacturer=None, model=None)
    assert async_get_advert_snapshot(runtime.hass)["scanners"][SCANNER]["hardware"] == UNKNOWN
    assert runtime.manager.version() == version
    assert (
        runtime.advert.stamp,
        runtime.advert.rssi,
        runtime.advert.rssi_distance_raw,
        list(runtime.advert.hist_rssi),
    ) == original
    assert scanner.address == SCANNER


def test_manual_binding_rebind_and_removal_never_retain_last_hardware(hass, device_registry):
    scanner, coordinator = _scanner(hass, device_registry)
    first_entry = _entry(hass, "esphome", "hardware-manual-a")
    second_entry = _entry(hass, "esphome", "hardware-manual-b")
    first = device_registry.async_get_or_create(
        config_entry_id=first_entry.entry_id, identifiers={("esphome", "manual-a")}, model="Board A"
    )
    second = device_registry.async_get_or_create(
        config_entry_id=second_entry.entry_id, identifiers={("esphome", "manual-b")}, model="Board B"
    )
    coordinator.options["measurement_toolkit"] = {"scanner_bindings": {SOURCE: {"device_registry_id": first.id}}}
    scanner.async_as_scanner_resolve_device_entries()
    identity = scanner.unique_id, scanner.entity_id_address
    assert hardware_metadata(coordinator, scanner)["detected"] == {"model": "Board A"}
    coordinator.options["measurement_toolkit"]["scanner_bindings"][SOURCE]["device_registry_id"] = second.id
    assert hardware_metadata(coordinator, scanner) == UNKNOWN  # even before the identity owner refreshes
    scanner.async_as_scanner_resolve_device_entries()
    assert hardware_metadata(coordinator, scanner)["detected"] == {"model": "Board B"}
    device_registry.async_remove_device(second.id)
    assert hardware_metadata(coordinator, scanner) == UNKNOWN
    coordinator.options["measurement_toolkit"]["scanner_bindings"] = {}
    assert hardware_metadata(coordinator, scanner) == UNKNOWN
    scanner.async_as_scanner_resolve_device_entries()
    assert hardware_metadata(coordinator, scanner) == UNKNOWN
    assert (scanner.unique_id, scanner.entity_id_address) == identity


async def test_profile_overrides_and_antenna_save_reload_clear_preserve_radio_model(metadata_runtime, device_registry):
    runtime = metadata_runtime
    device_registry.async_update_device(runtime.registry_device.id, manufacturer="Auto maker", model="Auto board")
    manager = runtime.manager
    version = manager.version()
    original_model = manager.effective_advert_model(runtime.advert)
    original_range = runtime.advert.rssi_distance_raw, runtime.advert.rssi_distance
    proposal = manager.proposal = {"eligible": True, "model_version": version, "metadata_test": True}
    saved_before = manager.store.async_save.await_count
    result = await manager.async_dispatch(
        "profile",
        {
            "kind": "receiver",
            "id": SCANNER.upper(),
            "profile": {"manufacturer": "Manual maker", "model": "Manual board", "external_antenna": False},
        },
    )
    assert result["profile"]["external_antenna"] is False
    assert runtime.entry.options["measurement_toolkit"]["receiver_profiles"][SCANNER.upper()] == result["profile"]
    assert manager.store.async_save.await_count > saved_before
    hardware = async_get_advert_snapshot(runtime.hass)["scanners"][SCANNER]["hardware"]
    assert hardware["detected"] == {"manufacturer": "Auto maker", "model": "Auto board"}
    assert hardware["effective"] == {"manufacturer": "Manual maker", "model": "Manual board", "external_antenna": False}
    reloaded = ToolkitManager(runtime.coordinator)
    assert reloaded.settings["receiver_profiles"][SCANNER.upper()]["external_antenna"] is False
    assert manager.version() == reloaded.version() == version
    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"external_antenna": True}})
    assert (
        async_get_advert_snapshot(runtime.hass)["scanners"][SCANNER]["hardware"]["effective"]["external_antenna"]
        is True
    )
    await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {}, "remove": ["model", "external_antenna"]}
    )
    effective = async_get_advert_snapshot(runtime.hass)["scanners"][SCANNER]["hardware"]["effective"]
    assert effective == {"manufacturer": "Manual maker", "model": "Auto board"}
    assert "external_antenna" not in ToolkitManager(runtime.coordinator).settings["receiver_profiles"][SCANNER.upper()]
    assert manager.effective_advert_model(runtime.advert) == original_model
    assert (runtime.advert.rssi_distance_raw, runtime.advert.rssi_distance) == original_range
    assert manager.version() == version
    assert manager.proposal is proposal


async def test_empty_manual_metadata_is_an_override_and_field_removal_restores_detection(
    metadata_runtime, device_registry
):
    runtime = metadata_runtime
    device_registry.async_update_device(runtime.registry_device.id, model="Auto board")
    await runtime.manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"model": ""}})
    hardware = async_get_advert_snapshot(runtime.hass)["scanners"][SCANNER]["hardware"]
    assert hardware["detected"]["model"] == "Auto board"
    assert hardware["effective"]["model"] == ""
    await runtime.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {}, "remove": ["model"]}
    )
    assert (
        async_get_advert_snapshot(runtime.hass)["scanners"][SCANNER]["hardware"]["effective"]["model"] == "Auto board"
    )


async def test_tracked_beacon_requires_one_exact_external_registry_identity(metadata_runtime, device_registry):
    runtime = metadata_runtime
    entry = _entry(runtime.hass, "bluetooth", "hardware-beacon")
    registry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={("bluetooth", DEVICE.upper())},
        manufacturer="Beacon maker",
        model="Beacon model",
    )
    hardware = async_get_advert_snapshot(runtime.hass)["devices"][DEVICE]["hardware"]
    assert hardware["detected"] == {"manufacturer": "Beacon maker", "model": "Beacon model"}
    await runtime.manager.async_dispatch(
        "profile", {"kind": "beacon", "id": DEVICE, "profile": {"model": "Manual beacon", "external_antenna": True}}
    )
    assert async_get_advert_snapshot(runtime.hass)["devices"][DEVICE]["hardware"]["effective"] == {
        "manufacturer": "Beacon maker",
        "model": "Manual beacon",
        "external_antenna": True,
    }
    uuid_beacon = SimpleNamespace(
        address="0123456789abcdef0123456789abcdef_1_2", manufacturer="Dog Collar", name="Dog Collar"
    )
    assert hardware_metadata(runtime.coordinator, uuid_beacon, kind="beacon") == UNKNOWN
    device_registry.async_remove_device(registry.id)
    assert async_get_advert_snapshot(runtime.hass)["devices"][DEVICE]["hardware"]["detected"] == {}


@pytest.mark.parametrize(
    "text",
    [
        "http://192.0.2.1",
        "192.0.2.1",
        "aa:bb:cc:dd:ee:ff",
        "2001:db8::1",
        "0123456789abcdef0123456789abcdef",
        ["Board"],
        {"model": "Board"},
    ],
)
def test_hardware_text_does_not_expose_network_or_identity_values(text):
    assert hardware_text(text) is None


def test_hardware_text_is_bounded_and_control_characters_are_removed():
    assert hardware_text("\x00  Board\n\t Model \x01") == "Board Model"
    assert len(hardware_text("x" * 1024)) == 512


@pytest.mark.parametrize(
    "namespace",
    [{"scanner_bindings": None}, {"scanner_bindings": []}, {"receiver_profiles": None}, {"beacon_profiles": []}],
)
def test_malformed_optional_metadata_is_unknown_without_breaking_snapshot(hass, device_registry, namespace):
    scanner, coordinator = _scanner(hass, device_registry, {"measurement_toolkit": namespace})
    scanner.scanner_binding = {"device_registry_id": ["malformed"]}
    assert hardware_metadata(coordinator, scanner) == UNKNOWN
    assert hardware_metadata(coordinator, scanner, kind="beacon") == UNKNOWN


async def test_effective_receiver_summary_shows_legacy_offsets_without_copying_them(metadata_runtime):
    runtime = metadata_runtime
    state = await runtime.manager.async_dispatch("state", {})
    assert state["receiver_models"][SCANNER] == {
        "rssi_offset": 1.5,
        "reference_power": -55.0,
        "attenuation": 3.0,
        "sources": {
            "rssi_offset": "legacy_receiver_offset",
            "reference_power": "global_default",
            "attenuation": "global_default",
        },
    }
    assert state["options"]["receiver_profiles"] == {}
    await runtime.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 4.0}}
    )
    state = await runtime.manager.async_dispatch("state", {})
    assert state["receiver_models"][SCANNER]["rssi_offset"] == 4.0
    assert state["receiver_models"][SCANNER]["sources"]["rssi_offset"] == "receiver_profile"
    await runtime.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {}, "remove": ["rssi_offset"]}
    )
    state = await runtime.manager.async_dispatch("state", {})
    assert state["receiver_models"][SCANNER]["rssi_offset"] == 1.5
    assert state["receiver_models"][SCANNER]["sources"]["rssi_offset"] == "legacy_receiver_offset"
    assert "rssi_offset" not in state["options"]["receiver_profiles"][SCANNER]
