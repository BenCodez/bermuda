"""Durable evidence survives externally visible entry setup and failed loading."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.const import DOMAIN

from .test_toolkit_store import real_store

DEVICE = "aa:bb:cc:dd:ee:01"
SCANNER = "aa:bb:cc:dd:ee:12"
SECOND_SCANNER = "aa:bb:cc:dd:ee:13"
IDENTITY = {"unique_id": "synthetic-historic-uid", "entity_id_address": "synthetic-historic-address"}


@pytest.fixture
async def saved_toolkit(hass, tmp_path, bypass_get_data, setup_bermuda_entry):
    """Leave actual toolkit bytes on disk after unloading its first real entry."""
    entry = setup_bermuda_entry
    manager = entry.runtime_data.coordinator.toolkit
    manager.store = real_store(hass, tmp_path, manager.store.key)
    await manager.async_dispatch(
        "settings", {"settings": {"scanner_bindings": {SCANNER: {"device_registry_id": "synthetic-registry"}}}}
    )
    now, stamp = time.time() - 1, monotonic_time_coarse() - 1
    rows = [
        {
            "device_id": DEVICE,
            "scanner_id": scanner,
            "observed": now,
            "stamp": stamp,
            "rssi": -65,
            "rssi_offset": 0,
            "reference_power": -59,
            "attenuation": 2,
            "raw_distance": 2,
            "filtered_distance": 2,
            "age": 0,
            "status": "current",
            "quality": {"fresh": True},
            "model_version": manager.version(),
        }
        for scanner in (SCANNER, SECOND_SCANNER)
    ]
    truth = {
        "source": "measured",
        "device_id": DEVICE,
        "location_id": "synthetic-area",
        "ranges": {SCANNER: 2, SECOND_SCANNER: 2},
        "accuracy_m": 0.1,
        "observed": now,
        "independent": True,
    }
    assert (
        manager.history.record(rows, captured_at=now, model_version=manager.version(), truth=[truth], force=True) == 2
    )
    trained = manager.fingerprints.train(DEVICE, "synthetic-area", rows, independent=True, now=now)
    assert trained["accepted"] is True
    manager._identities[SCANNER] = deepcopy(IDENTITY)
    await manager.store.async_save(manager._store_payload())
    expected_history = manager.history.frames()
    expected_fingerprints = manager.fingerprints.export_state()
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    path = Path(manager.store.path)
    saved = SimpleNamespace(
        entry=entry,
        path=path,
        bytes=path.read_bytes(),
        history=expected_history,
        fingerprints=expected_fingerprints,
        options=deepcopy(dict(entry.options)),
        directory=tmp_path,
    )
    yield saved
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)


def restored_managers(hass, saved, *, fail_load=False):
    """Use real managers and real Store I/O, observing any attempted durable save."""
    manager_class = toolkit_module.ToolkitManager
    managers = []

    def construct(coordinator):
        manager = manager_class(coordinator)
        manager.store = real_store(hass, saved.directory, manager.store.key)
        manager.store.async_save = AsyncMock(wraps=manager.store.async_save)
        if fail_load:
            manager.store.async_load = AsyncMock(side_effect=OSError("synthetic load failure"))
        managers.append(manager)
        return manager

    return managers, patch.object(toolkit_module, "ToolkitManager", side_effect=construct)


def gated_host_executor(hass):
    """Hold the first initialization await while allowing unrelated executor work."""
    entered, release = asyncio.Event(), asyncio.Event()
    actual = hass.async_add_executor_job

    async def host_job(function, args):
        entered.set()
        await release.wait()
        return await actual(function, *args)

    def executor(function, *args):
        if function is toolkit_module._host_clock_id:
            return asyncio.create_task(host_job(function, args))
        return actual(function, *args)

    return entered, release, patch.object(hass, "async_add_executor_job", side_effect=executor)


async def test_retained_http_and_service_cannot_mutate_loading_entry(hass, hass_client, hass_admin_user, saved_toolkit):
    saved = saved_toolkit
    client = await hass_client()
    managers, manager_patch = restored_managers(hass, saved)
    entered, release, executor_patch = gated_host_executor(hass)
    with manager_patch, executor_patch:
        setup = asyncio.create_task(hass.config_entries.async_setup(saved.entry.entry_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert saved.entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            manager = managers[0]
            assert saved.entry.runtime_data.coordinator.toolkit is manager
            response = await client.get(f"/api/bermuda/toolkit?entry_id={saved.entry.entry_id}")
            assert response.status == 409
            assert (await response.json())["error"] == "operation_conflict"
            response = await client.post(
                "/api/bermuda/toolkit",
                json={"action": "settings", "entry_id": saved.entry.entry_id, "settings": {"auto_calibration": True}},
            )
            assert response.status == 409
            for action, payload in (("state", {}), ("settings", {"auto_calibration": True})):
                with pytest.raises(ServiceValidationError, match="unavailable or conflicted"):
                    await hass.services.async_call(
                        DOMAIN,
                        "measurement_toolkit",
                        {"entry_id": saved.entry.entry_id, "action": action, "payload": {"settings": payload}},
                        blocking=True,
                        return_response=True,
                        context=Context(user_id=hass_admin_user.id),
                    )
            manager.after_update(0.001)
            assert manager._background is None
            await manager._after_update()
            manager.store.async_save.assert_not_awaited()
            assert saved.path.read_bytes() == saved.bytes
            assert dict(saved.entry.options) == saved.options
            release.set()
            assert await asyncio.wait_for(setup, 3) is True
            response = await client.get(f"/api/bermuda/toolkit?entry_id={saved.entry.entry_id}")
            assert response.status == 200
            state = await response.json()
            assert state["history"]["rows"] == 2
            assert state["options"]["auto_calibration"] is False
            assert state["options"]["scanner_bindings"][SCANNER] == {"device_registry_id": "synthetic-registry"}
            assert manager.history.frames() == saved.history
            assert manager.fingerprints.export_state() == saved.fingerprints
            assert manager.scanner_identity(SCANNER, "replacement-uid") == IDENTITY
            manager.store.async_save.assert_not_awaited()
            assert saved.path.read_bytes() == saved.bytes
        finally:
            release.set()
            if not setup.done():
                setup.cancel()
                await asyncio.gather(setup, return_exceptions=True)


async def test_cancelled_loading_entry_preserves_exact_durable_file(hass, saved_toolkit):
    saved = saved_toolkit
    managers, manager_patch = restored_managers(hass, saved)
    entered, release, executor_patch = gated_host_executor(hass)
    with manager_patch, executor_patch:
        setup = asyncio.create_task(hass.config_entries.async_setup(saved.entry.entry_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert saved.entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            setup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await setup
            assert managers[0].closed is True
            managers[0].store.async_save.assert_not_awaited()
            assert saved.path.read_bytes() == saved.bytes
            assert dict(saved.entry.options) == saved.options
        finally:
            release.set()
            if not setup.done():
                setup.cancel()
                await asyncio.gather(setup, return_exceptions=True)


async def test_store_load_failure_cleanup_preserves_exact_durable_file(hass, saved_toolkit):
    saved = saved_toolkit
    managers, manager_patch = restored_managers(hass, saved, fail_load=True)
    with manager_patch:
        assert await hass.config_entries.async_setup(saved.entry.entry_id) is False
    assert saved.entry.state is ConfigEntryState.SETUP_RETRY
    assert managers[0].closed is True
    managers[0].store.async_load.assert_awaited_once()
    managers[0].store.async_save.assert_not_awaited()
    assert saved.path.read_bytes() == saved.bytes
    assert dict(saved.entry.options) == saved.options
