"""Authenticated administrator HTTP boundary for the measurement toolkit."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import KEY_HASS, HomeAssistantView, StaticPathConfig
from homeassistant.setup import async_setup_component

from . import api

if TYPE_CHECKING:
    from aiohttp import web
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

TOOLKIT_ACTIONS = frozenset(
    {
        "state",
        "settings",
        "profile",
        "capture",
        "calibration_preview",
        "calibration_apply",
        "calibration_rollback",
        "history_start",
        "history_stop",
        "history_clear",
        "history_export",
        "history_import",
        "replay",
        "shadow_start",
        "shadow_stop",
        "shadow_promote",
        "fingerprint_train",
        "fingerprint_clear",
        "diagnostics",
    }
)
MAX_BODY_BYTES = 2 * 1024 * 1024
BODY_TIMEOUT_SECONDS = 15
NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_SECRET_FIELDS = frozenset(
    {"irk", "irks", "private_key", "private_keys", "identity_resolving_key", "identity_resolving_keys"}
)
_LOGGER = logging.getLogger(__name__)
_REGISTRATION_KEY = "bermuda_toolkit_http"
_PANEL_PATH = "bermuda-toolkit"


async def _register_panel(hass: HomeAssistant, entry_id: str) -> None:
    """Register one administrator sidebar panel bound to an explicit entry."""
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=_PANEL_PATH,
        webcomponent_name="bermuda-toolkit-panel",
        sidebar_title="Bermuda toolkit",
        sidebar_icon="mdi:bluetooth",
        module_url="/bermuda_toolkit/toolkit.js",
        config={"entry_id": entry_id},
        require_admin=True,
        config_panel_domain="bermuda",
    )


async def async_register_toolkit(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register HTTP/static/services once, keeping a single admin panel across reloads."""
    # Import here because the service schema shares this module's action allowlist.
    from .toolkit_services import async_register_toolkit_services  # noqa: PLC0415

    registry = hass.data.setdefault(_REGISTRATION_KEY, {"lock": asyncio.Lock(), "entries": set()})
    async with registry["lock"]:
        if not registry.get("components_ready"):
            if not await async_setup_component(hass, "panel_custom", {}):
                msg = "Home Assistant custom panel could not be loaded"
                raise RuntimeError(msg)
            registry["components_ready"] = True
        if not registry.get("http_registered"):
            hass.http.register_view(BermudaToolkitView(hass))
            registry["http_registered"] = True
        if not registry.get("static_registered"):
            await hass.http.async_register_static_paths(
                [
                    StaticPathConfig(
                        "/bermuda_toolkit/toolkit.js", str(Path(__file__).parent / "frontend" / "toolkit.js"), False
                    )
                ]
            )
            registry["static_registered"] = True
        if not registry.get("services_registered"):
            async_register_toolkit_services(hass)
            registry["services_registered"] = True
        if not registry.get("panel_entry_id"):
            await _register_panel(hass, entry.entry_id)
            registry["panel_entry_id"] = entry.entry_id
        registry["entries"].add(entry.entry_id)


async def async_unregister_toolkit(hass: HomeAssistant, entry_id: str) -> None:
    """Retire an entry's panel while retaining the safe static and HTTP registrations."""
    if not (registry := hass.data.get(_REGISTRATION_KEY)):
        return
    async with registry["lock"]:
        registry["entries"].discard(entry_id)
        if registry.get("panel_entry_id") != entry_id:
            return
        frontend.async_remove_panel(hass, _PANEL_PATH)
        registry["panel_entry_id"] = None
        if registry["entries"]:
            next_entry = sorted(registry["entries"])[0]
            await _register_panel(hass, next_entry)
            registry["panel_entry_id"] = next_entry


def _without_secret_fields(value: Any) -> Any:
    """Defense in depth: private key fields never leave the HTTP boundary."""
    if isinstance(value, dict):
        return {
            key: _without_secret_fields(item) for key, item in value.items() if str(key).lower() not in _SECRET_FIELDS
        }
    if isinstance(value, list):
        return [_without_secret_fields(item) for item in value]
    return value


def _reject_constant(value: str) -> None:
    """JSON NaN and infinities are not valid measurement input."""
    msg = "Nonfinite JSON number"
    raise ValueError(msg)


def _finite_float(value: str) -> float:
    """Exponent overflow is also rejected instead of becoming infinity."""
    result = float(value)
    if not math.isfinite(result):
        _reject_constant(value)
    return result


class BermudaToolkitView(HomeAssistantView):
    """Expose toolkit operations only to authenticated Home Assistant admins."""

    url = "/api/bermuda/toolkit"
    name = "api:bermuda:toolkit"
    requires_auth = True

    def __init__(self, hass: HomeAssistant | None = None) -> None:
        self._hass = hass

    def _error(self, status: HTTPStatus, code: str, message: str) -> web.Response:
        return self.json({"error": code, "message": message}, status_code=status, headers=NO_STORE_HEADERS)

    def _is_admin(self, request: web.Request) -> bool:
        user = request.get("hass_user")
        return user is not None and user.is_admin is True

    async def get(self, request: web.Request) -> web.Response:
        """Read state without starting recordings, trials, or other mutations."""
        if not self._is_admin(request):
            return self._error(HTTPStatus.FORBIDDEN, "admin_required", "Administrator access is required.")
        entry_id = request.query.get("entry_id")
        if entry_id is not None and (not entry_id or len(entry_id) > 128):
            return self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "Invalid configuration entry.")
        return await self._dispatch(request, "state", entry_id, {})

    async def post(self, request: web.Request) -> web.Response:  # noqa: PLR0911
        """Validate a bounded JSON envelope before invoking the central manager."""
        if not self._is_admin(request):
            return self._error(HTTPStatus.FORBIDDEN, "admin_required", "Administrator access is required.")
        if request.content_type != "application/json":
            return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required", "Send an application/json body.")
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "Request body exceeds 2 MiB.")
        try:
            body = bytearray()
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                async for chunk in request.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        return self._error(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "Request body exceeds 2 MiB."
                        )
            payload = json.loads(body, parse_constant=_reject_constant, parse_float=_finite_float)
        except TimeoutError:
            return self._error(HTTPStatus.REQUEST_TIMEOUT, "body_timeout", "Request body timed out.")
        except (ValueError, UnicodeError, RecursionError):
            return self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "Invalid JSON body.")
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "Request body must be an object.")
        action = payload.pop("action", None)
        entry_id = payload.pop("entry_id", None)
        if not isinstance(action, str) or action not in TOOLKIT_ACTIONS:
            return self._error(HTTPStatus.BAD_REQUEST, "invalid_action", "Unknown toolkit action.")
        if entry_id is not None and (not isinstance(entry_id, str) or not entry_id or len(entry_id) > 128):
            return self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "Invalid configuration entry.")
        return await self._dispatch(request, action, entry_id, payload)

    async def _dispatch(  # noqa: PLR0911
        self, request: web.Request, action: str, entry_id: str | None, payload: dict[str, Any]
    ) -> web.Response:
        hass = self._hass if self._hass is not None else request.app[KEY_HASS]
        try:
            coordinator = api.async_get_coordinator(hass, entry_id)
            if inspect.isawaitable(coordinator):
                coordinator = await coordinator
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "entry_required", "Select a Bermuda configuration entry.")
        except LookupError:
            return self._error(HTTPStatus.NOT_FOUND, "entry_not_loaded", "Bermuda configuration entry is not loaded.")
        toolkit = getattr(coordinator, "toolkit", None)
        if toolkit is None:
            return self._error(HTTPStatus.NOT_FOUND, "entry_not_loaded", "Bermuda configuration entry is not loaded.")
        try:
            result = await toolkit.async_dispatch(action, payload)
        except (ValueError, TypeError, KeyError):
            return self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "Invalid toolkit request.")
        except RuntimeError:
            return self._error(
                HTTPStatus.CONFLICT, "operation_conflict", "Toolkit operation is unavailable or conflicted."
            )
        except OSError:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "persistence_failed", "Toolkit data could not be saved.")
        except Exception:  # noqa: BLE001 - the HTTP boundary must not expose exception payloads
            _LOGGER.error("Toolkit action %s failed", action)  # noqa: TRY400 - exception text can contain secrets
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "operation_failed", "Toolkit operation failed.")
        return self.json(_without_secret_fields(result), headers=NO_STORE_HEADERS)
