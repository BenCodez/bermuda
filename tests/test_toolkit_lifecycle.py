"""Real Home Assistant entry lifecycle and toolkit panel cleanup checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda import async_reload_entry
from custom_components.bermuda.api import async_get_coordinator
from custom_components.bermuda.const import CONF_MAX_RADIUS, DOMAIN

from .const import MOCK_CONFIG


async def test_real_setup_reload_unload_retires_manager_and_panel(hass, bypass_get_data, setup_bermuda_entry):
    entry = setup_bermuda_entry
    manager = entry.runtime_data.coordinator.toolkit
    assert manager.closed is False
    assert hass.data[frontend.DATA_PANELS]["bermuda-toolkit"].require_admin is True
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert manager.closed is True
    successor = entry.runtime_data.coordinator.toolkit
    assert successor is not manager
    assert successor.closed is False
    assert hass.data[frontend.DATA_PANELS]["bermuda-toolkit"].config["entry_id"] == entry.entry_id
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert successor.closed is True
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert async_get_coordinator(hass, entry.entry_id) is None
    assert "bermuda-toolkit" not in hass.data[frontend.DATA_PANELS]


async def test_toolkit_options_update_in_place_and_global_options_still_reload(
    hass, bypass_get_data, setup_bermuda_entry
):
    entry = setup_bermuda_entry
    manager = entry.runtime_data.coordinator.toolkit
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "measurement_toolkit": {"auto_calibration": False}}
        )
        await hass.async_block_till_done()
        schedule_reload.assert_not_called()
        assert entry.runtime_data.coordinator.toolkit is manager
        assert manager.closed is False
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MAX_RADIUS: 25})
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)


async def test_failed_initial_refresh_closes_loaded_toolkit(hass, error_on_get_data):
    from custom_components.bermuda.toolkit import ToolkitManager

    managers = []

    def construct(coordinator):
        manager = ToolkitManager(coordinator)
        managers.append(manager)
        return manager

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="failed-setup")
    entry.add_to_hass(hass)
    with patch("custom_components.bermuda.toolkit.ToolkitManager", side_effect=construct):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert len(managers) == 1
    assert managers[0].closed is True
    assert "bermuda-toolkit" not in hass.data.get(frontend.DATA_PANELS, {})


async def test_failed_store_load_closes_partial_manager(hass):
    from custom_components.bermuda.toolkit import ToolkitManager

    managers = []

    def construct(coordinator):
        manager = ToolkitManager(coordinator)
        manager.async_load = AsyncMock(side_effect=OSError("synthetic store failure"))
        managers.append(manager)
        return manager

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="failed-store-load")
    entry.add_to_hass(hass)
    with patch("custom_components.bermuda.toolkit.ToolkitManager", side_effect=construct):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert managers[0].closed is True
    assert "bermuda-toolkit" not in hass.data.get(frontend.DATA_PANELS, {})


async def test_unsuccessful_platform_unload_preserves_manager_and_panel(hass, bypass_get_data, setup_bermuda_entry):
    from custom_components.bermuda import async_unload_entry

    entry = setup_bermuda_entry
    manager = entry.runtime_data.coordinator.toolkit
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert await async_unload_entry(hass, entry) is False
    assert manager.closed is False
    assert "bermuda-toolkit" in hass.data[frontend.DATA_PANELS]


async def test_missing_toolkit_options_owner_preserves_existing_reload_path(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="legacy-listener")
    with patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        await async_reload_entry(hass, entry)
    schedule_reload.assert_called_once_with(entry.entry_id)
