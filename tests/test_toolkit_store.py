"""Real HA Store durability and owned toolkit action cancellation regressions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError

from custom_components.bermuda.api import snapshot_observations
from custom_components.bermuda.recording import DiagnosticHistory
from custom_components.bermuda.toolkit_store import DurableToolkitStore

from .test_toolkit_runtime import DEVICE, SCANNER, _proposal, runtime

# Capture core implementations before HA's hass_storage fixture replaces its
# writer/loader. These tests exercise the real atomic filesystem write path.
_REAL_WRITE = Store._async_write_data
_REAL_LOAD = Store._async_load


def real_store(hass, tmp_path, key="bermuda.durable-test"):
    store = DurableToolkitStore(hass, 1, key)
    store.path = str(tmp_path / ".storage" / key)
    store._async_write_data = MethodType(_REAL_WRITE, store)
    store._async_load = MethodType(_REAL_LOAD, store)
    return store


async def test_legacy_writer_path_signature_preserves_atomic_write(durable_store, monkeypatch):
    store = durable_store
    actual = store._async_write_data
    calls = []

    async def legacy_writer(path, data):
        calls.append(path)
        await actual(data)

    monkeypatch.setattr(store, "_async_write_data", legacy_writer)
    await store.async_save({"legacy_writer": True})
    assert calls == [store.path]
    assert json.loads(Path(store.path).read_text())["data"] == {"legacy_writer": True}


def test_legacy_constructor_does_not_forward_unsupported_default_options(hass, monkeypatch):
    actual = Store.__init__
    calls = []

    def legacy_init(
        self, hass, version, key, private=False, *, atomic_writes=False, encoder=None, minor_version=1, read_only=False
    ):
        calls.append(key)
        actual(
            self,
            hass,
            version,
            key,
            private,
            atomic_writes=atomic_writes,
            encoder=encoder,
            minor_version=minor_version,
            read_only=read_only,
        )

    monkeypatch.setattr(Store, "__init__", legacy_init)
    store = DurableToolkitStore(hass, 1, "legacy-compatible")
    assert calls == ["legacy-compatible"]
    assert store._atomic_writes is True
    with pytest.raises(ValueError, match="serialize_in_event_loop"):
        DurableToolkitStore(hass, 1, "unsupported-option", serialize_in_event_loop=False)
    with pytest.raises(ValueError, match="max_readable_version"):
        DurableToolkitStore(hass, 1, "unsupported-option", max_readable_version=2)


@pytest.fixture
def durable_store(hass, tmp_path):
    return real_store(hass, tmp_path)


@pytest.fixture
async def durable_runtime(runtime, tmp_path):
    manager = runtime.manager
    original = manager.store
    manager.store = real_store(runtime.hass, tmp_path, "bermuda.durable-runtime")
    runtime.io_releases = []
    await manager.store.async_save(manager._store_payload())
    try:
        yield runtime
    finally:
        for release in runtime.io_releases:
            release.set()
        manager.store._async_write_data = MethodType(_REAL_WRITE, manager.store)
        manager.shadow = None
        await manager.async_close()
        manager.store = original


async def test_actual_store_save_is_atomic_and_cleans_inherited_pending_listeners(durable_store):
    store = durable_store
    assert store._atomic_writes is True
    store.async_delay_save(lambda: {"value": "deferred"}, 3600)
    assert store._delay_handle is not None
    await store.async_save({"value": "durable"})
    assert json.loads(Path(store.path).read_text())["data"] == {"value": "durable"}
    assert await store.async_load() == {"value": "durable"}
    assert store._data is None
    assert store._delay_handle is None
    assert store._unsub_final_write_listener is None
    assert store.key in store._manager._invalidated


@pytest.mark.parametrize("error", [WriteError("private write detail"), SerializationError("private JSON detail")])
async def test_actual_write_failures_propagate_and_preserve_prior_disk(durable_store, monkeypatch, error):
    store = durable_store
    await store.async_save({"value": "prior"})
    prior = Path(store.path).read_bytes()
    monkeypatch.setattr(store, "_async_write_data", AsyncMock(side_effect=error))
    with pytest.raises(OSError, match="could not be durably saved") as caught:
        await store.async_save({"value": "candidate"})
    assert "private" not in str(caught.value)
    assert Path(store.path).read_bytes() == prior
    assert store._data is None
    assert await store.async_load() == {"value": "prior"}


async def test_actual_json_serialization_failure_is_observable(durable_store):
    store = durable_store
    await store.async_save({"value": "prior"})
    prior = Path(store.path).read_bytes()
    with pytest.raises(OSError):
        await store.async_save({"not_json": object()})
    assert Path(store.path).read_bytes() == prior


async def test_stopping_save_flushes_actual_disk_before_returning(hass, durable_store, monkeypatch):
    monkeypatch.setattr(hass, "state", CoreState.stopping)
    await durable_store.async_save({"during_shutdown": True})
    assert json.loads(Path(durable_store.path).read_text())["data"] == {"during_shutdown": True}
    assert durable_store._data is None
    assert durable_store._unsub_final_write_listener is None


async def test_stopping_save_does_not_suppress_actual_write_failure(hass, durable_store, monkeypatch):
    monkeypatch.setattr(hass, "state", CoreState.stopping)
    monkeypatch.setattr(durable_store, "_async_write_data", AsyncMock(side_effect=WriteError("failure")))
    with pytest.raises(OSError):
        await durable_store.async_save({"during_shutdown": True})


async def test_read_only_and_nonatomic_stores_cannot_claim_durable_success(hass, durable_store):
    with pytest.raises(ValueError, match="atomic"):
        DurableToolkitStore(hass, 1, "invalid", atomic_writes=False)
    durable_store.make_read_only()
    with pytest.raises(OSError, match="read-only"):
        await durable_store.async_save({"value": 1})


async def test_cancelled_write_keeps_inherited_lock_until_actual_write_finishes(durable_store, monkeypatch):
    store = durable_store
    await store.async_save({"value": "prior"})
    entered, release = asyncio.Event(), asyncio.Event()
    actual = store._async_write_data
    order = []

    async def gated(data):
        order.append(("start", data["data"]["value"]))
        if data["data"]["value"] == "first":
            entered.set()
            await release.wait()
        await actual(data)
        order.append(("end", data["data"]["value"]))

    monkeypatch.setattr(store, "_async_write_data", gated)
    first = asyncio.create_task(store.async_save({"value": "first"}))
    await entered.wait()
    first.cancel()
    second = asyncio.create_task(store.async_save({"value": "second"}))
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    assert store._write_lock.locked()
    assert json.loads(Path(store.path).read_text())["data"] == {"value": "prior"}
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert order == [("start", "first"), ("end", "first"), ("start", "second"), ("end", "second")]
    assert json.loads(Path(store.path).read_text())["data"] == {"value": "second"}


@pytest.mark.parametrize("error", [WriteError("disk failed"), SerializationError("JSON failed")])
@pytest.mark.parametrize("action", ["settings", "calibration_apply"])
async def test_actual_save_failure_prevents_live_options_and_trial_publication(
    durable_runtime, monkeypatch, error, action
):
    state = durable_runtime
    manager = state.manager
    options = deepcopy(dict(state.entry.options))
    settings = deepcopy(manager.settings)
    version = manager.version()
    distance = state.advert.rssi_distance_raw
    prior_disk = Path(manager.store.path).read_bytes()
    manager.proposal = _proposal(manager, {SCANNER: 4.0})
    payload = {"settings": {"receiver_limits": {SCANNER: 5.0}}} if action == "settings" else {}
    monkeypatch.setattr(manager.store, "_async_write_data", AsyncMock(side_effect=error))
    with pytest.raises(OSError):
        await manager.async_dispatch(action, payload)
    assert dict(state.entry.options) == options
    assert manager.settings == settings
    assert manager.version() == version
    assert manager.trial is None
    assert state.advert.rssi_distance_raw == distance
    assert Path(manager.store.path).read_bytes() == prior_disk


def replacement_history(manager):
    rows = snapshot_observations(manager._snapshot())
    rows[0]["stamp"] += 1.0
    history = DiagnosticHistory(enabled=True)
    history.record(rows, captured_at=time.time(), model_version=manager.version())
    return history.export(redact=False)


def seed_fingerprint(manager):
    now = time.time()
    rows = [
        {
            "device_id": DEVICE,
            "scanner_id": scanner,
            "rssi": -65,
            "stamp": 100,
            "observed": now,
            "age": 0,
            "status": "current",
            "model_version": manager.version(),
        }
        for scanner in (SCANNER, "synthetic-second-receiver")
    ]
    result = manager.fingerprints.train(DEVICE, "synthetic-area", rows, independent=True, now=now)
    assert result["accepted"]


@pytest.mark.parametrize("action", ["history_import", "fingerprint_clear"])
async def test_cancelled_public_mutation_commits_once_after_durable_save_finishes(durable_runtime, monkeypatch, action):
    manager = durable_runtime.manager
    if action == "fingerprint_clear":
        seed_fingerprint(manager)
    await manager.store.async_save(manager._store_payload())
    old_history = manager.history
    old_model = manager.fingerprints
    prior_disk = Path(manager.store.path).read_bytes()
    manager.proposal = _proposal(manager, {SCANNER: 4.0})
    manager.shadow = SimpleNamespace(state="active", cancel=MagicMock())
    entered, release = asyncio.Event(), asyncio.Event()
    durable_runtime.io_releases.append(release)
    actual = manager.store._async_write_data
    writes = []

    async def gated(data):
        writes.append(data)
        entered.set()
        await release.wait()
        await actual(data)

    monkeypatch.setattr(manager.store, "_async_write_data", gated)
    payload = {"data": replacement_history(manager)} if action == "history_import" else {}
    caller = asyncio.create_task(manager.async_dispatch(action, payload))
    await asyncio.wait_for(entered.wait(), timeout=5)
    owned = manager._action_task
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not owned.done()
    assert manager._action_task is owned
    assert manager.history is old_history
    assert manager.fingerprints is old_model
    assert manager.proposal is not None
    assert Path(manager.store.path).read_bytes() == prior_disk
    with pytest.raises(RuntimeError, match="already running"):
        await manager.async_dispatch("history_clear", {})
    release.set()
    await owned
    assert len(writes) == 1
    saved = json.loads(Path(manager.store.path).read_text())["data"]
    if action == "history_import":
        assert manager.history is not old_history
        assert manager.history.summary()["rows"] == 1
        assert saved["history"] == manager.history.export(redact=False)
        assert manager.proposal is None
        manager.shadow.cancel.assert_called_once()
    else:
        assert manager.fingerprints is not old_model
        assert manager.fingerprints.summary() == []
        assert saved["fingerprints"] == manager.fingerprints.export_state()


@pytest.mark.parametrize("action", ["history_import", "fingerprint_clear"])
async def test_failed_actual_io_retains_live_history_fingerprints_and_proposal(durable_runtime, monkeypatch, action):
    manager = durable_runtime.manager
    if action == "fingerprint_clear":
        seed_fingerprint(manager)
    await manager.store.async_save(manager._store_payload())
    old_history, old_model = manager.history, manager.fingerprints
    prior_disk = Path(manager.store.path).read_bytes()
    manager.proposal = _proposal(manager, {SCANNER: 4.0})
    payload = {"data": replacement_history(manager)} if action == "history_import" else {}
    monkeypatch.setattr(manager.store, "_async_write_data", AsyncMock(side_effect=WriteError("failed")))
    with pytest.raises(OSError):
        await manager.async_dispatch(action, payload)
    assert manager.history is old_history
    assert manager.fingerprints is old_model
    assert manager.proposal is not None
    assert Path(manager.store.path).read_bytes() == prior_disk
