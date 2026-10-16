"""Exercise the actual authenticated Home Assistant toolkit HTTP boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.components import frontend
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.toolkit_http import (
    BermudaToolkitView,
    MAX_BODY_BYTES,
    async_register_toolkit,
    async_unregister_toolkit,
)


@pytest.fixture
async def toolkit_http(hass):
    assert await async_setup_component(hass, "http", {"http": {}})
    manager = SimpleNamespace(async_dispatch=AsyncMock(return_value={"history": {"running": False, "count": 0}}))
    coordinator = SimpleNamespace(toolkit=manager)
    hass.http.register_view(BermudaToolkitView())
    with patch(
        "custom_components.bermuda.toolkit_http.api.async_get_coordinator", return_value=coordinator
    ) as resolver:
        yield manager, resolver


async def test_admin_reads_only_state_and_has_no_store(hass_client, toolkit_http):
    manager, resolver = toolkit_http
    client = await hass_client()
    response = await client.get("/api/bermuda/toolkit?entry_id=entry-a")
    assert response.status == 200
    assert await response.json() == {"history": {"running": False, "count": 0}}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    manager.async_dispatch.assert_awaited_once_with("state", {})
    assert resolver.call_args.args[1] == "entry-a"


async def test_admin_post_resolves_entry_and_forwards_only_action_payload(hass_client, toolkit_http):
    manager, resolver = toolkit_http
    client = await hass_client()
    response = await client.post(
        "/api/bermuda/toolkit",
        json={"action": "settings", "entry_id": "entry-b", "settings": {"auto_calibration": False}},
    )
    assert response.status == 200
    assert resolver.call_args.args[1] == "entry-b"
    manager.async_dispatch.assert_awaited_once_with("settings", {"settings": {"auto_calibration": False}})


async def test_unauthenticated_requests_cannot_enter_dispatch(hass_client_no_auth, toolkit_http):
    manager, resolver = toolkit_http
    client = await hass_client_no_auth()
    for method in (client.get, client.post):
        response = await method("/api/bermuda/toolkit")
        assert response.status == 401
    manager.async_dispatch.assert_not_awaited()
    resolver.assert_not_called()


async def test_non_admin_cannot_spoof_permissions(hass_client, hass_admin_user, toolkit_http):
    manager, resolver = toolkit_http
    hass_admin_user.groups.clear()
    hass_admin_user.is_owner = False
    client = await hass_client()
    for method, kwargs in (
        (client.get, {}),
        (client.post, {"json": {"action": "history_clear", "is_admin": True, "context": {"user_id": "admin"}}}),
    ):
        response = await method("/api/bermuda/toolkit", **kwargs)
        assert response.status == 403
        assert (await response.json())["error"] == "admin_required"
        assert response.headers["Cache-Control"] == "no-store"
    manager.async_dispatch.assert_not_awaited()
    resolver.assert_not_called()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"{", "invalid_json"),
        (b"[]", "invalid_request"),
        (b"null", "invalid_request"),
        (b'{"action":"capture","ranges":{"scanner":NaN}}', "invalid_json"),
        (b'{"action":"capture","ranges":{"scanner":1e999}}', "invalid_json"),
        (b'{"action":true}', "invalid_action"),
        (b'{"action":"delete_everything"}', "invalid_action"),
        (b'{"action":"state","entry_id":123}', "invalid_request"),
        (b'{"action":"state","entry_id":""}', "invalid_request"),
    ],
)
async def test_malformed_envelopes_do_not_dispatch(hass_client, toolkit_http, body, expected):
    manager, resolver = toolkit_http
    client = await hass_client()
    response = await client.post("/api/bermuda/toolkit", data=body, headers={"Content-Type": "application/json"})
    assert response.status == 400
    assert (await response.json())["error"] == expected
    manager.async_dispatch.assert_not_awaited()
    resolver.assert_not_called()


async def test_requires_json_media_type(hass_client, toolkit_http):
    manager, _ = toolkit_http
    client = await hass_client()
    response = await client.post("/api/bermuda/toolkit", data='{"action":"state"}')
    assert response.status == 415
    manager.async_dispatch.assert_not_awaited()


@pytest.mark.parametrize("chunked", [False, True])
async def test_body_byte_limit_applies_with_and_without_content_length(hass_client, toolkit_http, chunked):
    manager, _ = toolkit_http
    client = await hass_client()
    body = b'{"action":"state","padding":"' + b"x" * MAX_BODY_BYTES + b'"}'

    async def chunks():
        for offset in range(0, len(body), 32 * 1024):
            yield body[offset : offset + 32 * 1024]

    response = await client.post(
        "/api/bermuda/toolkit", data=chunks() if chunked else body, headers={"Content-Type": "application/json"}
    )
    assert response.status == 413
    assert (await response.json())["error"] == "body_too_large"
    manager.async_dispatch.assert_not_awaited()


async def test_exact_byte_bound_is_accepted(hass_client, toolkit_http):
    manager, _ = toolkit_http
    client = await hass_client()
    prefix = b'{"action":"state","padding":"'
    suffix = b'"}'
    body = prefix + b"x" * (MAX_BODY_BYTES - len(prefix) - len(suffix)) + suffix
    response = await client.post("/api/bermuda/toolkit", data=body, headers={"Content-Type": "application/json"})
    assert response.status == 200
    manager.async_dispatch.assert_awaited_once()


async def test_unloaded_entry_is_not_found(hass_client, toolkit_http):
    manager, resolver = toolkit_http
    resolver.return_value = None
    client = await hass_client()
    response = await client.get("/api/bermuda/toolkit?entry_id=unloaded")
    assert response.status == 404
    manager.async_dispatch.assert_not_awaited()


async def test_ambiguous_entry_requires_explicit_scope(hass_client, toolkit_http):
    manager, resolver = toolkit_http
    resolver.side_effect = ValueError("Ambiguous entry")
    client = await hass_client()
    response = await client.post("/api/bermuda/toolkit", json={"action": "history_clear"})
    assert response.status == 400
    assert (await response.json())["error"] == "entry_required"
    assert response.headers["Cache-Control"] == "no-store"
    manager.async_dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ValueError("private payload must not leak"), 400, "invalid_request"),
        (RuntimeError("private payload must not leak"), 409, "operation_conflict"),
        (OSError("private path must not leak"), 503, "persistence_failed"),
        (Exception("private payload must not leak"), 500, "operation_failed"),
    ],
)
async def test_manager_errors_are_safe_and_not_cached(hass_client, toolkit_http, error, status, code):
    manager, _ = toolkit_http
    manager.async_dispatch.side_effect = error
    client = await hass_client()
    response = await client.post("/api/bermuda/toolkit", json={"action": "calibration_apply"})
    assert response.status == status
    assert (await response.json())["error"] == code
    assert "private" not in await response.text()
    assert response.headers["Cache-Control"] == "no-store"


async def test_private_key_fields_never_leave_http_boundary(hass_client, toolkit_http):
    manager, _ = toolkit_http
    manager.async_dispatch.return_value = {
        "model_version": "synthetic",
        "irk": "synthetic-secret",
        "devices": [{"device_id": "opaque-safe-id", "PRIVATE_KEY": "synthetic-secret"}],
        "diagnostics": {"identity_resolving_keys": ["synthetic-secret"]},
    }
    client = await hass_client()
    response = await client.get("/api/bermuda/toolkit")
    assert response.status == 200
    assert await response.json() == {
        "model_version": "synthetic",
        "devices": [{"device_id": "opaque-safe-id"}],
        "diagnostics": {},
    }
    assert "synthetic-secret" not in await response.text()


async def test_stalled_body_is_bounded_before_dispatch(monkeypatch, toolkit_http):
    manager, resolver = toolkit_http
    monkeypatch.setattr("custom_components.bermuda.toolkit_http.BODY_TIMEOUT_SECONDS", 0.001)

    class StalledContent:
        async def iter_chunked(self, chunk_size):
            yield b'{"action":'
            await asyncio.Event().wait()

    class Request(dict):
        content_type = "application/json"
        content_length = None
        content = StalledContent()

    response = await BermudaToolkitView().post(Request(hass_user=SimpleNamespace(is_admin=True)))
    assert response.status == 408
    manager.async_dispatch.assert_not_awaited()
    resolver.assert_not_called()


async def test_panel_static_http_and_services_register_once_and_survive_reload(hass, hass_client_no_auth):
    entry = MockConfigEntry(domain="bermuda", data={}, entry_id="synthetic-entry")
    await asyncio.gather(async_register_toolkit(hass, entry), async_register_toolkit(hass, entry))
    panel = hass.data[frontend.DATA_PANELS]["bermuda-toolkit"]
    assert panel.require_admin is True
    assert panel.sidebar_title is None
    assert panel.sidebar_icon is None
    assert panel.to_response()["config_panel_domain"] is None
    assert panel.config["entry_id"] == "synthetic-entry"
    assert panel.config["_panel_custom"]["module_url"] == "/bermuda_toolkit/toolkit.js"
    assert panel.config["_panel_custom"]["name"] == "bermuda-toolkit-panel"
    assert hass.services.has_service("bermuda", "measurement_toolkit")
    client = await hass_client_no_auth()
    response = await client.get("/bermuda_toolkit/toolkit.js")
    assert response.status == 200
    assert "BermudaToolkitPanel" in await response.text()
    await async_unregister_toolkit(hass, entry.entry_id)
    assert "bermuda-toolkit" not in hass.data[frontend.DATA_PANELS]
    await async_register_toolkit(hass, entry)
    reloaded_panel = hass.data[frontend.DATA_PANELS]["bermuda-toolkit"]
    assert reloaded_panel.config["entry_id"] == entry.entry_id
    assert reloaded_panel.to_response()["config_panel_domain"] is None


@pytest.mark.parametrize(
    ("step", "fields"),
    [
        (
            "globalopts",
            {
                "max_area_radius",
                "max_velocity",
                "devtracker_nothome_timeout",
                "update_interval",
                "smoothing_samples",
                "attenuation",
                "ref_power",
            },
        ),
        ("selectdevices", {"configured_devices"}),
        ("calibration1_global", {"configured_devices", "configured_scanners", "attenuation", "ref_power"}),
        ("calibration2_scanners", {"configured_devices", "scanner_info"}),
    ],
)
async def test_toolkit_keeps_configure_on_original_options_forms(hass, setup_bermuda_entry, step, fields):
    """The frontend can reach global, beacon and scanner controls while the toolkit is loaded."""
    panel = hass.data[frontend.DATA_PANELS]["bermuda-toolkit"].to_response()
    # HA's Configure button follows a panel claiming this integration domain.
    assert panel["config_panel_domain"] is None
    assert panel["title"] is None

    menu = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    assert menu["type"] == FlowResultType.MENU
    assert set(menu["menu_options"]) == {
        "measurement_toolkit",
        "globalopts",
        "selectdevices",
        "calibration1_global",
        "calibration2_scanners",
    }
    result = await hass.config_entries.options.async_configure(menu["flow_id"], user_input={"next_step_id": step})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == step
    assert fields <= {marker.schema for marker in result["data_schema"].schema}


async def test_panel_moves_to_remaining_entry_and_retires_on_last_unload(hass):
    first = MockConfigEntry(domain="bermuda", data={}, entry_id="entry-a")
    second = MockConfigEntry(domain="bermuda", data={}, entry_id="entry-b")
    await async_register_toolkit(hass, first)
    await async_register_toolkit(hass, second)
    assert hass.data[frontend.DATA_PANELS]["bermuda-toolkit"].config["entry_id"] == "entry-a"
    await async_unregister_toolkit(hass, first.entry_id)
    remaining_panel = hass.data[frontend.DATA_PANELS]["bermuda-toolkit"]
    assert remaining_panel.config["entry_id"] == "entry-b"
    assert remaining_panel.to_response()["config_panel_domain"] is None
    await async_unregister_toolkit(hass, second.entry_id)
    assert "bermuda-toolkit" not in hass.data[frontend.DATA_PANELS]


async def test_real_http_manager_options_and_store_roundtrip(hass, hass_client, bypass_get_data, setup_bermuda_entry):
    entry = setup_bermuda_entry
    manager = entry.runtime_data.coordinator.toolkit
    client = await hass_client()
    url = f"/api/bermuda/toolkit?entry_id={entry.entry_id}"
    with patch.object(manager.store, "async_save", wraps=manager.store.async_save) as save:
        response = await client.get(url)
        assert response.status == 200
        state = await response.json()
        assert state["history"]["running"] is False
        assert state["options"]["auto_calibration"] is False
        save.assert_not_awaited()
        response = await client.post(
            "/api/bermuda/toolkit",
            json={"action": "settings", "entry_id": entry.entry_id, "settings": {"reliability_weighting": True}},
        )
        assert response.status == 200
        assert entry.options["measurement_toolkit"]["reliability_weighting"] is True
        save.assert_awaited()
    response = await client.post(
        "/api/bermuda/toolkit",
        json={
            "action": "profile",
            "entry_id": entry.entry_id,
            "kind": "receiver",
            "id": "synthetic-receiver",
            "profile": {"model": "Synthetic receiver", "attenuation": 2.5},
        },
    )
    assert response.status == 200
    response = await client.post(
        "/api/bermuda/toolkit",
        json={
            "action": "profile",
            "entry_id": entry.entry_id,
            "kind": "receiver",
            "id": "synthetic-receiver",
            "profile": {},
            "remove": ["attenuation"],
        },
    )
    assert response.status == 200
    assert entry.options["measurement_toolkit"]["receiver_profiles"]["synthetic-receiver"] == {
        "model": "Synthetic receiver"
    }
    response = await client.post(
        "/api/bermuda/toolkit", json={"action": "history_import", "entry_id": entry.entry_id, "data": {"invalid": True}}
    )
    assert response.status == 400
    assert manager.history.frames() == []
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.toolkit is manager
