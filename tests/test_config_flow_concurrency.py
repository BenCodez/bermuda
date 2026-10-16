"""HA options flows preserve concurrent toolkit and independent receiver edits."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.const import (
    BDADDR_TYPE_OTHER,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_MAX_RADIUS,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNER_INFO,
    CONF_SCANNERS,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)

from .const import MOCK_OPTIONS_GLOBALS

SCANNER_A = "aa:bb:cc:dd:ee:1a"
SCANNER_B = "aa:bb:cc:dd:ee:1b"
OFFLINE = "aa:bb:cc:dd:ee:ff"
BEACON_A = "aa:bb:cc:dd:ee:01"
BEACON_B = "aa:bb:cc:dd:ee:02"
TOOLKIT = "measurement_toolkit"


@pytest.fixture
def flow_entry(hass, device_registry):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="concurrent-options-flow",
        data={"source": "user"},
        options={
            **MOCK_OPTIONS_GLOBALS,
            CONF_DEVICES: [BEACON_A.upper()],
            CONF_RSSI_OFFSETS: {SCANNER_A: 1.0, SCANNER_B: 2.0, OFFLINE: 9.0},
            TOOLKIT: {"auto_calibration": False},
            "unrelated_legacy_value": "opening",
        },
    )
    entry.add_to_hass(hass)
    devices = {
        SCANNER_A: SimpleNamespace(name="Receiver A", unique_id=SCANNER_A, entry_id="registry-a", is_scanner=True),
        SCANNER_B: SimpleNamespace(name="Receiver B", unique_id=SCANNER_B, entry_id="registry-b", is_scanner=True),
        BEACON_A: SimpleNamespace(name="Beacon A", address=BEACON_A, address_type=BDADDR_TYPE_OTHER, is_scanner=False),
        BEACON_B: SimpleNamespace(name="Beacon B", address=BEACON_B, address_type=BDADDR_TYPE_OTHER, is_scanner=False),
    }
    coordinator = SimpleNamespace(
        devices=devices,
        scanner_list=[SCANNER_A, SCANNER_B],
        count_active_devices=lambda: 2,
        count_active_scanners=lambda: 2,
        get_active_scanner_summary=lambda: [],
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    target = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, connections={("bluetooth", BEACON_A.upper())}, name="Synthetic Tracked Beacon"
    )
    return entry, target.id


async def _open(hass, entry, step):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] == FlowResultType.MENU
    shown = await hass.config_entries.options.async_configure(menu["flow_id"], user_input={"next_step_id": step})
    assert shown["type"] == FlowResultType.FORM
    return shown


def _shown_values(result):
    return {
        marker.schema: deepcopy(marker.default())
        for marker in result["data_schema"].schema
        if marker.default is not vol.UNDEFINED
    }


@pytest.mark.parametrize("step", ["globalopts", "calibration1_global", "calibration2_scanners", "selectdevices"])
async def test_finishing_each_legacy_flow_preserves_concurrent_toolkit_and_unedited_options(hass, flow_entry, step):
    entry, target_id = flow_entry
    shown = await _open(hass, entry, step)
    submitted = _shown_values(shown)
    latest = {
        **deepcopy(dict(entry.options)),
        TOOLKIT: {"auto_calibration": True, "filters": {BEACON_A: "pet"}},
        CONF_RSSI_OFFSETS: {SCANNER_A: 1.0, SCANNER_B: 7.0, OFFLINE: 11.0},
        CONF_REF_POWER: -61.0,
        CONF_ATTENUATION: 2.5,
        CONF_UPDATE_INTERVAL: 25.0,
        CONF_DEVICES: [BEACON_A.upper(), BEACON_B.upper()],
        "unrelated_legacy_value": "latest",
    }
    hass.config_entries.async_update_entry(entry, options=latest)
    if step == "globalopts":
        submitted[CONF_MAX_RADIUS] = 22.0
        expected = {**latest, CONF_MAX_RADIUS: 22.0}
    elif step == "calibration1_global":
        submitted.update(
            {
                CONF_DEVICES: target_id,
                CONF_SCANNERS: SCANNER_A,
                CONF_REF_POWER: -59.0,
                CONF_SAVE_AND_CLOSE: True,
            }
        )
        expected = {**latest, CONF_REF_POWER: -59.0}
    elif step == "calibration2_scanners":
        submitted.update({CONF_DEVICES: target_id, CONF_SAVE_AND_CLOSE: True})
        submitted[CONF_SCANNER_INFO]["Receiver A"] = 3.0
        expected = {**latest, CONF_RSSI_OFFSETS: {SCANNER_A: 3.0, SCANNER_B: 7.0, OFFLINE: 11.0}}
    else:
        # An unchanged device selection must not remove a newly tracked beacon.
        expected = latest
    finished = await hass.config_entries.options.async_configure(shown["flow_id"], user_input=submitted)
    assert finished["type"] == FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == expected
    assert entry.options[TOOLKIT]["auto_calibration"] is True


async def test_explicit_device_selection_edit_preserves_latest_namespace(hass, flow_entry):
    entry, _ = flow_entry
    shown = await _open(hass, entry, "selectdevices")
    latest = {**deepcopy(dict(entry.options)), TOOLKIT: {"auto_calibration": True}, CONF_REF_POWER: -63.0}
    hass.config_entries.async_update_entry(entry, options=latest)
    result = await hass.config_entries.options.async_configure(shown["flow_id"], user_input={CONF_DEVICES: []})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {**latest, CONF_DEVICES: []}


async def test_receiver_edit_wins_same_receiver_conflict_without_reverting_other_receiver_edits(hass, flow_entry):
    entry, target_id = flow_entry
    shown = await _open(hass, entry, "calibration2_scanners")
    submitted = _shown_values(shown)
    submitted.update({CONF_DEVICES: target_id, CONF_SAVE_AND_CLOSE: True})
    submitted[CONF_SCANNER_INFO]["Receiver A"] = -3.0
    latest = {
        **deepcopy(dict(entry.options)),
        CONF_RSSI_OFFSETS: {SCANNER_A: 4.0, SCANNER_B: 8.0, OFFLINE: 10.0},
        TOOLKIT: {"auto_calibration": True},
    }
    hass.config_entries.async_update_entry(entry, options=latest)
    finished = await hass.config_entries.options.async_configure(shown["flow_id"], user_input=submitted)
    assert finished["type"] == FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {**latest, CONF_RSSI_OFFSETS: {SCANNER_A: -3.0, SCANNER_B: 8.0, OFFLINE: 10.0}}


async def test_receiver_merge_keeps_existing_uppercase_offset_keys(hass, flow_entry):
    entry, target_id = flow_entry
    opening = {**deepcopy(dict(entry.options)), CONF_RSSI_OFFSETS: {SCANNER_A.upper(): 1.0, SCANNER_B.upper(): 2.0}}
    hass.config_entries.async_update_entry(entry, options=opening)
    shown = await _open(hass, entry, "calibration2_scanners")
    submitted = _shown_values(shown)
    assert submitted[CONF_SCANNER_INFO] == {"Receiver A": 1.0, "Receiver B": 2.0}
    submitted.update({CONF_DEVICES: target_id, CONF_SAVE_AND_CLOSE: True})
    submitted[CONF_SCANNER_INFO]["Receiver A"] = 3.0
    latest = {**opening, CONF_RSSI_OFFSETS: {SCANNER_A.upper(): 1.0, SCANNER_B.upper(): 7.0}}
    hass.config_entries.async_update_entry(entry, options=latest)
    result = await hass.config_entries.options.async_configure(shown["flow_id"], user_input=submitted)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {**latest, CONF_RSSI_OFFSETS: {SCANNER_A.upper(): 3.0, SCANNER_B.upper(): 7.0}}
