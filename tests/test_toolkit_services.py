"""Exercise the actual HA service registry, context users, and response protocol."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.const import DOMAIN
from custom_components.bermuda.toolkit_services import (
    SERVICE_MEASUREMENT_TOOLKIT,
    TOOLKIT_ACTIONS,
    async_register_toolkit_services,
)


@pytest.fixture
def toolkit_service(hass):
    manager = SimpleNamespace(async_dispatch=AsyncMock(return_value={"status": "ready"}))
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="synthetic-entry")
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(toolkit=manager))
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    async_register_toolkit_services(hass)
    return manager, entry


async def service_call(hass, data, *, user=None, response=True):
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_MEASUREMENT_TOOLKIT,
        data,
        blocking=True,
        return_response=response,
        context=Context(user_id=user.id if user else None),
    )


async def test_admin_dispatches_selected_entry_and_receives_response(hass, hass_admin_user, toolkit_service):
    manager, entry = toolkit_service
    response = await service_call(
        hass,
        {"entry_id": entry.entry_id, "action": "settings", "payload": {"settings": {"auto_calibration": False}}},
        user=hass_admin_user,
    )
    assert response == {"status": "ready"}
    manager.async_dispatch.assert_awaited_once_with("settings", {"settings": {"auto_calibration": False}})


async def test_local_automation_can_resolve_single_entry_with_default_payload(hass, toolkit_service):
    manager, entry = toolkit_service
    assert await service_call(hass, {"action": "state"}) == {"status": "ready"}
    manager.async_dispatch.assert_awaited_once_with("state", {})


async def test_service_response_is_optional_and_registration_idempotent(hass, toolkit_service):
    manager, entry = toolkit_service
    assert hass.services.supports_response(DOMAIN, SERVICE_MEASUREMENT_TOOLKIT) is SupportsResponse.OPTIONAL
    async_register_toolkit_services(hass)
    async_register_toolkit_services(hass)
    assert await service_call(hass, {"action": "diagnostics"}, response=False) is None
    manager.async_dispatch.assert_awaited_once_with("diagnostics", {})


async def test_nonadmin_user_cannot_spoof_payload_authority(hass, hass_admin_user, toolkit_service):
    manager, entry = toolkit_service
    hass_admin_user.is_owner = False
    hass_admin_user.groups.clear()
    with pytest.raises(Unauthorized):
        await service_call(
            hass,
            {"action": "history_clear", "payload": {"is_admin": True, "context": {"user_id": "admin"}}},
            user=hass_admin_user,
        )
    manager.async_dispatch.assert_not_awaited()


async def test_unknown_or_inactive_context_user_is_rejected(hass, hass_admin_user, toolkit_service):
    manager, entry = toolkit_service
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_MEASUREMENT_TOOLKIT,
            {"action": "state"},
            blocking=True,
            context=Context(user_id="unknown-user"),
        )
    hass_admin_user.is_active = False
    with pytest.raises(Unauthorized):
        await service_call(hass, {"action": "state"}, user=hass_admin_user)
    manager.async_dispatch.assert_not_awaited()


async def test_multiple_loaded_entries_require_selection_and_missing_entry_fails(hass, toolkit_service):
    manager, entry = toolkit_service
    second = MockConfigEntry(domain=DOMAIN, data={}, entry_id="second-entry")
    second.add_to_hass(hass)
    second_manager = SimpleNamespace(async_dispatch=AsyncMock(return_value={"entry": "second"}))
    second.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(toolkit=second_manager))
    second._async_set_state(hass, ConfigEntryState.LOADED, None)
    with pytest.raises(ServiceValidationError, match="Select one loaded"):
        await service_call(hass, {"action": "state"})
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await service_call(hass, {"action": "state", "entry_id": "missing-entry"})
    assert await service_call(hass, {"action": "state", "entry_id": second.entry_id}) == {"entry": "second"}
    manager.async_dispatch.assert_not_awaited()
    second_manager.async_dispatch.assert_awaited_once_with("state", {})


async def test_entry_without_runtime_manager_fails_safely(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="not-loaded")
    entry.add_to_hass(hass)
    async_register_toolkit_services(hass)
    with pytest.raises(ServiceValidationError):
        await service_call(hass, {"action": "state"})


async def test_unloaded_entry_with_stale_runtime_data_does_not_make_selection_ambiguous(hass, toolkit_service):
    manager, entry = toolkit_service
    unloaded = MockConfigEntry(domain=DOMAIN, data={}, entry_id="unloaded-with-runtime")
    unloaded.add_to_hass(hass)
    unloaded.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(toolkit=SimpleNamespace(async_dispatch=AsyncMock()))
    )
    assert await service_call(hass, {"action": "state"}) == {"status": "ready"}
    manager.async_dispatch.assert_awaited_once_with("state", {})


@pytest.mark.parametrize(
    "data",
    [
        {"action": "unknown"},
        {"action": True},
        {"action": "state", "entry_id": ""},
        {"action": "state", "entry_id": 12},
        {"action": "state", "payload": []},
        {"action": "capture", "payload": {"ranges": {"scanner": float("nan")}}},
        {"action": "state", "is_admin": True},
        {"action": "state", "payload": {"text": "x" * (2 * 1024 * 1024 + 1)}},
    ],
)
async def test_invalid_service_envelopes_do_not_dispatch(hass, toolkit_service, data):
    manager, entry = toolkit_service
    with pytest.raises(vol.Invalid):
        await service_call(hass, data)
    manager.async_dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (ValueError("sensitive-input"), ServiceValidationError, "Invalid toolkit request."),
        (RuntimeError("sensitive-input"), ServiceValidationError, "Toolkit operation is unavailable or conflicted."),
        (OSError("sensitive-input"), HomeAssistantError, "Toolkit data could not be saved."),
        (Exception("sensitive-input"), HomeAssistantError, "Toolkit operation failed."),
    ],
)
async def test_dispatch_errors_do_not_disclose_payload_or_exception_text(
    hass, toolkit_service, error, expected, message
):
    manager, entry = toolkit_service
    manager.async_dispatch.side_effect = error
    with pytest.raises(expected) as caught:
        await service_call(hass, {"action": "state"})
    assert str(caught.value) == message
    assert "sensitive" not in str(caught.value)


async def test_service_response_redacts_private_key_fields(hass, toolkit_service):
    manager, entry = toolkit_service
    manager.async_dispatch.return_value = {
        "status": "ready",
        "private_key": "secret",
        "nested": [{"irk": "secret", "count": 2}],
    }
    assert await service_call(hass, {"action": "diagnostics"}) == {"status": "ready", "nested": [{"count": 2}]}


async def test_every_dispatch_action_is_registered_and_described(hass, toolkit_service):
    manager, entry = toolkit_service
    for action in TOOLKIT_ACTIONS:
        await service_call(hass, {"action": action})
    assert manager.async_dispatch.await_count == len(TOOLKIT_ACTIONS)
    descriptions = yaml.safe_load((Path(__file__).parents[1] / "custom_components/bermuda/services.yaml").read_text())
    options = descriptions[SERVICE_MEASUREMENT_TOOLKIT]["fields"]["action"]["selector"]["select"]["options"]
    assert set(options) == TOOLKIT_ACTIONS
