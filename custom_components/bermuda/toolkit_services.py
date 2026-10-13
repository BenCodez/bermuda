"""
Home Assistant service boundary for administrator toolkit operations.

User authorization comes exclusively from the actual service-call context. Calls
without a user ID are trusted local automations. Validation, conflict, persistence,
and unexpected failures use fixed messages so exception text cannot reveal private
identities or submitted payloads. Cancellation is allowed to propagate normally.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from . import api
from .const import DOMAIN
from .toolkit_http import MAX_BODY_BYTES, TOOLKIT_ACTIONS, _without_secret_fields

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

SERVICE_MEASUREMENT_TOOLKIT = "measurement_toolkit"
_LOGGER = logging.getLogger(__name__)


def _entry_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        msg = "Invalid configuration entry"
        raise vol.Invalid(msg)
    return value


def _payload(value: Any) -> dict:
    if not isinstance(value, dict):
        msg = "Toolkit payload must be an object"
        raise vol.Invalid(msg)
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as error:
        msg = "Toolkit payload must contain finite JSON values"
        raise vol.Invalid(msg) from error
    if len(encoded.encode()) > MAX_BODY_BYTES:
        msg = "Toolkit payload exceeds 2 MiB"
        raise vol.Invalid(msg)
    # Detach caller-owned objects before asynchronous manager processing.
    return json.loads(encoded)


TOOLKIT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): _entry_id,
        vol.Required("action"): vol.All(str, vol.In(TOOLKIT_ACTIONS)),
        vol.Optional("payload", default=dict): _payload,
    }
)


def _toolkit(hass: HomeAssistant, entry_id: str | None) -> Any:
    if entry_id is None:
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if api.async_get_coordinator(hass, entry.entry_id) is not None
        ]
        if len(entries) != 1:
            msg = "Select one loaded Bermuda configuration entry."
            raise ServiceValidationError(msg)
        entry_id = entries[0].entry_id
    coordinator = api.async_get_coordinator(hass, entry_id)
    toolkit = getattr(coordinator, "toolkit", None)
    if toolkit is None:
        msg = "Bermuda configuration entry is not loaded."
        raise ServiceValidationError(msg)
    return toolkit


@callback
def async_register_toolkit_services(hass: HomeAssistant) -> None:
    """Idempotently register the response-optional measurement toolkit service."""
    if hass.services.has_service(DOMAIN, SERVICE_MEASUREMENT_TOOLKIT):
        return

    async def handle(call: ServiceCall) -> dict:
        user_id = call.context.user_id
        if user_id is not None:
            user = await hass.auth.async_get_user(user_id)
            if user is None or user.is_admin is not True or user.is_active is not True:
                raise Unauthorized(context=call.context)
        toolkit = _toolkit(hass, call.data.get("entry_id"))
        action = call.data["action"]
        try:
            result = await toolkit.async_dispatch(action, call.data["payload"])
        except (ValueError, TypeError, KeyError):
            msg = "Invalid toolkit request."
            raise ServiceValidationError(msg) from None
        except RuntimeError:
            msg = "Toolkit operation is unavailable or conflicted."
            raise ServiceValidationError(msg) from None
        except OSError:
            msg = "Toolkit data could not be saved."
            raise HomeAssistantError(msg) from None
        except Exception:  # noqa: BLE001 - private exception payloads must not leave this boundary
            _LOGGER.error("Toolkit service action %s failed", action)  # noqa: TRY400 - exception text can contain secrets
            msg = "Toolkit operation failed."
            raise HomeAssistantError(msg) from None
        if not isinstance(result, dict):
            msg = "Toolkit operation returned an invalid response."
            raise HomeAssistantError(msg)
        return _without_secret_fields(result)

    hass.services.async_register(
        DOMAIN,
        SERVICE_MEASUREMENT_TOOLKIT,
        handle,
        schema=TOOLKIT_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
