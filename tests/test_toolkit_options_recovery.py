"""Probe real HA delayed options files against durable toolkit commit records."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import time
from types import MethodType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util.file import WriteError

from custom_components.bermuda.const import CONF_ATTENUATION, CONF_REF_POWER, CONF_RSSI_OFFSETS
from custom_components.bermuda.api import snapshot_observations
from custom_components.bermuda.toolkit import COMMIT_KEY, OPTIONS_KEY, ToolkitManager

from .test_toolkit_runtime import SCANNER, _proposal, runtime
from .test_toolkit_store import _REAL_WRITE, durable_runtime


@pytest.fixture
async def disk(durable_runtime, tmp_path, monkeypatch):
    state = durable_runtime
    monkeypatch.setattr(state.hass.config, "config_dir", str(tmp_path))
    core = state.hass.config_entries._store
    core.path = state.hass.config.path(".storage", "core.config_entries")
    monkeypatch.setattr(core, "_async_write_data", MethodType(_REAL_WRITE, core))
    await core.async_save(state.hass.config_entries._data_to_save())
    state.core_store = core
    return state


def core_options(state):
    entries = json.loads(Path(state.core_store.path).read_text())["data"]["entries"]
    return next(entry["options"] for entry in entries if entry["entry_id"] == state.entry.entry_id)


def core_modified(state):
    entries = json.loads(Path(state.core_store.path).read_text())["data"]["entries"]
    return next(entry["modified_at"] for entry in entries if entry["entry_id"] == state.entry.entry_id)


async def flush_core(state):
    await state.core_store._async_handle_write_data()


async def restart_from_disk(state):
    # A process restart reconstructs the config entry from HA's actual file, not
    # its more recent RAM. Its API schedules the eventual normal recovery write.
    old = state.manager
    state.hass.config_entries.async_update_entry(state.entry, options=core_options(state))
    object.__setattr__(state.entry, "modified_at", datetime.fromisoformat(core_modified(state)))
    state.core_store._async_cleanup_delay_listener()
    state.core_store._async_cleanup_final_write_listener()
    state.core_store._data = None
    manager = ToolkitManager(state.coordinator)
    manager.store = old.store
    state.coordinator.toolkit = manager
    await manager.async_load()
    return manager


async def apply(state, value=-2.0):
    state.manager.proposal = _proposal(state.manager, {SCANNER: value})
    return await state.manager.async_dispatch("calibration_apply", {})


async def test_apply_recovers_accepted_intent_before_and_after_ha_delayed_write(disk):
    original = core_options(disk)
    await apply(disk)
    desired = deepcopy(dict(disk.entry.options))
    assert core_options(disk) == original
    saved = json.loads(Path(disk.manager.store.path).read_text())["data"]
    assert saved["options_journal"]["phase"] == "accepted"
    recovered = await restart_from_disk(disk)
    assert dict(disk.entry.options) == desired
    assert recovered.trial["offsets"] == {SCANNER: -2.0}
    await flush_core(disk)
    assert core_options(disk) == desired
    second = await restart_from_disk(disk)
    assert dict(disk.entry.options) == desired
    assert second.trial["offsets"] == {SCANNER: -2.0}


async def test_auto_rollback_keeps_disable_intent_and_old_trial_across_core_failure(disk, monkeypatch):
    manager = disk.manager
    await manager.async_dispatch("settings", {"settings": {"auto_calibration": True}})
    baseline = deepcopy(dict(disk.entry.options[CONF_RSSI_OFFSETS]))
    await manager._apply({SCANNER: -2.0}, automatic=True)
    await flush_core(disk)
    applied_disk = core_options(disk)
    with patch.object(
        disk.hass.config_entries, "async_update_entry", wraps=disk.hass.config_entries.async_update_entry
    ) as update:
        await manager._rollback(disable_auto=True)
        assert update.call_count == 1
    assert core_options(disk) == applied_disk
    monkeypatch.setattr(disk.core_store, "_async_write_data", AsyncMock(side_effect=WriteError("core failed")))
    await flush_core(disk)  # Core intentionally suppresses its own write failure.
    assert core_options(disk) == applied_disk
    recovered = await restart_from_disk(disk)
    assert recovered.settings["auto_calibration"] is False
    assert disk.entry.options[CONF_RSSI_OFFSETS] == baseline
    assert recovered.trial is None
    evidence = json.loads(Path(manager.store.path).read_text())["data"]["options_journal"]
    assert evidence["backup_trial"]["offsets"] == {SCANNER: -2.0}


async def test_successive_unflushed_commits_recover_latest_and_more_than_sixteen_changes(disk):
    original = core_options(disk)
    for index in range(20):
        await disk.manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: float(index + 1)}}})
    desired = deepcopy(dict(disk.entry.options))
    # Writes may flush naturally during a slower runner. Both earliest and later
    # acknowledged files are valid ancestry for the latest accepted intent.
    recovered = await restart_from_disk(disk)
    assert recovered.settings["receiver_limits"][SCANNER] == 20.0
    assert dict(disk.entry.options) == desired
    assert COMMIT_KEY not in recovered.settings


async def test_external_edits_retaining_marker_are_preserved_without_stale_replay(disk):
    await apply(disk)
    await flush_core(disk)
    external = deepcopy(dict(disk.entry.options))
    external["legacy_unrelated"] = "external-new-value"
    external[OPTIONS_KEY]["receiver_limits"] = {SCANNER: 19.0}
    disk.hass.config_entries.async_update_entry(disk.entry, options=external)
    await flush_core(disk)
    recovered = await restart_from_disk(disk)
    assert dict(disk.entry.options) == external
    assert recovered.settings["receiver_limits"] == {SCANNER: 19.0}


async def test_external_undo_witness_retags_and_recovers_later_legacy_changes(disk):
    original = deepcopy(dict(disk.entry.options))
    await apply(disk)
    applied_marker = disk.entry.options[OPTIONS_KEY][COMMIT_KEY]
    undo = deepcopy(original)
    undo[OPTIONS_KEY] = {**disk.manager.settings, COMMIT_KEY: applied_marker}
    undo[CONF_REF_POWER] = -63.0
    undo[CONF_ATTENUATION] = 2.4
    disk.hass.config_entries.async_update_entry(disk.entry, options=undo)
    assert await disk.manager.async_options_changed(disk.entry) is False
    final = deepcopy(dict(disk.entry.options))
    assert final[OPTIONS_KEY][COMMIT_KEY] != applied_marker
    recovered = await restart_from_disk(disk)
    assert dict(disk.entry.options) == final
    assert recovered.coordinator.options[CONF_REF_POWER] == -63.0
    assert recovered.coordinator.options[CONF_ATTENUATION] == 2.4
    assert recovered.coordinator.options[CONF_RSSI_OFFSETS] == original[CONF_RSSI_OFFSETS]


async def test_prepared_rejected_intent_never_replays_when_conflict_repair_fails(disk, monkeypatch):
    old = deepcopy(dict(disk.entry.options))
    actual = disk.manager.store._async_write_data
    calls = 0

    async def race(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WriteError("repair fails")
        await actual(data)
        changed = deepcopy(old)
        changed["legacy_unrelated"] = "concurrent"
        disk.hass.config_entries.async_update_entry(disk.entry, options=changed)

    monkeypatch.setattr(disk.manager.store, "_async_write_data", race)
    with pytest.raises(OSError):
        await disk.manager.async_dispatch("settings", {"settings": {"auto_calibration": True}})
    recovered = await restart_from_disk(disk)
    assert recovered.settings["auto_calibration"] is False
    assert dict(disk.entry.options) == old


async def test_accepted_phase_failure_compensates_live_trial_distances_and_histories(disk, monkeypatch):
    manager, advert = disk.manager, disk.advert
    before_options = deepcopy(dict(disk.entry.options))
    before_distance = advert.rssi_distance
    before_raw = advert.rssi_distance_raw
    before_history = deepcopy(advert.hist_distance)
    before_filtered = deepcopy(advert.hist_distance_by_interval)
    device_options = disk.device.options
    actual = manager.store._async_write_data
    calls = 0

    async def fail_accept(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WriteError("accepted phase fails")
        await actual(data)

    monkeypatch.setattr(manager.store, "_async_write_data", fail_accept)
    with pytest.raises(OSError):
        await apply(disk)
    assert dict(disk.entry.options) == before_options
    assert manager.trial is None
    assert advert.rssi_distance == pytest.approx(before_distance)
    assert advert.rssi_distance_raw == pytest.approx(before_raw)
    assert advert.hist_distance == pytest.approx(before_history)
    assert advert.hist_distance_by_interval == pytest.approx(before_filtered)
    assert disk.device.options is device_options is disk.coordinator.options
    recovered = await restart_from_disk(disk)
    assert recovered.trial is None
    assert disk.entry.options[CONF_RSSI_OFFSETS] == before_options[CONF_RSSI_OFFSETS]


async def test_cancelled_public_caller_still_finishes_commit_and_busy_guard(disk, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    disk.io_releases.append(release)
    actual = disk.manager.store._async_write_data

    async def gated(data):
        entered.set()
        await release.wait()
        await actual(data)

    monkeypatch.setattr(disk.manager.store, "_async_write_data", gated)
    caller = asyncio.create_task(disk.manager.async_dispatch("settings", {"settings": {"auto_calibration": True}}))
    await asyncio.wait_for(entered.wait(), 5)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    owned = disk.manager._action_task
    with pytest.raises(RuntimeError, match="already running"):
        await disk.manager.async_dispatch("settings", {"settings": {"auto_calibration": False}})
    release.set()
    await owned
    recovered = await restart_from_disk(disk)
    assert recovered.settings["auto_calibration"] is True


async def test_trial_is_retained_until_late_number_reference_restore(disk):
    disk.device.ref_power = disk.advert.ref_power = -62.0
    disk.manager.invalidate_model()
    await apply(disk)
    expected = disk.manager.trial["model_version"]
    disk.device.ref_power = disk.advert.ref_power = 0.0
    recovered = await restart_from_disk(disk)
    assert recovered.trial["model_version"] == expected
    assert recovered.version() != expected
    disk.device.ref_power = disk.advert.ref_power = -62.0
    recovered.invalidate_model()
    assert recovered.version() == expected


async def test_commit_marker_and_full_options_journal_are_private(disk):
    await apply(disk)
    state = await disk.manager.async_dispatch("state", {})
    encoded = json.dumps(state)
    assert COMMIT_KEY not in encoded
    assert "options_journal" not in encoded
    assert "previous_options" not in encoded


async def test_external_aba_retaining_latest_token_restores_monitoring_and_preserves_undo(disk):
    manager = disk.manager
    await manager.async_dispatch("settings", {"settings": {"auto_calibration": True}})
    await manager._apply({SCANNER: -2.0}, automatic=True)
    applied = deepcopy(dict(disk.entry.options))
    await flush_core(disk)
    await manager._rollback(disable_auto=True)
    rollback_token = disk.entry.options[OPTIONS_KEY][COMMIT_KEY]
    undo = deepcopy(applied)
    undo[OPTIONS_KEY][COMMIT_KEY] = rollback_token
    disk.hass.config_entries.async_update_entry(disk.entry, options=undo)
    await manager.async_options_changed(disk.entry)
    desired = deepcopy(dict(disk.entry.options))
    assert desired[OPTIONS_KEY][COMMIT_KEY] != rollback_token
    assert manager.trial["offsets"] == {SCANNER: -2.0}
    recovered = await restart_from_disk(disk)
    assert dict(disk.entry.options) == desired
    assert recovered.trial["offsets"] == {SCANNER: -2.0}


@pytest.mark.parametrize("legacy", [False, True])
async def test_external_controls_still_adopt_or_reload_when_witness_write_fails(disk, monkeypatch, legacy):
    options = deepcopy(dict(disk.entry.options))
    options[CONF_REF_POWER if legacy else CONF_RSSI_OFFSETS] = -61.0 if legacy else {SCANNER: 3.0}
    disk.hass.config_entries.async_update_entry(disk.entry, options=options)
    monkeypatch.setattr(disk.manager.store, "_async_write_data", AsyncMock(side_effect=WriteError("witness fails")))
    handled = await disk.manager.async_options_changed(disk.entry)
    assert handled is (not legacy)
    assert dict(disk.entry.options) == options
    if not legacy:
        assert disk.coordinator.options[CONF_RSSI_OFFSETS] == {SCANNER: 3.0}
        assert disk.advert.conf_rssi_offset == 3.0


async def test_external_latest_options_win_if_changed_again_during_witness_await(disk, monkeypatch):
    original = deepcopy(dict(disk.entry.options))
    first = deepcopy(original)
    first[CONF_RSSI_OFFSETS] = {SCANNER: 2.0}
    last = deepcopy(original)
    last[CONF_RSSI_OFFSETS] = {SCANNER: 5.0}
    disk.hass.config_entries.async_update_entry(disk.entry, options=first)
    actual = disk.manager.store._async_write_data
    once = False

    async def race(data):
        nonlocal once
        await actual(data)
        if not once:
            once = True
            disk.hass.config_entries.async_update_entry(disk.entry, options=last)

    monkeypatch.setattr(disk.manager.store, "_async_write_data", race)
    assert await disk.manager.async_options_changed(disk.entry)
    assert dict(disk.entry.options) == last
    assert disk.coordinator.options[CONF_RSSI_OFFSETS] == {SCANNER: 5.0}


async def test_external_old_token_aba_after_failed_witness_is_not_stale_replayed(disk, monkeypatch):
    await apply(disk)
    older = deepcopy(dict(disk.entry.options))
    await flush_core(disk)
    await disk.manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 9.0}}})
    disk.hass.config_entries.async_update_entry(disk.entry, options=older)
    monkeypatch.setattr(
        disk.manager.store, "_async_write_data", AsyncMock(side_effect=WriteError("witness unavailable"))
    )
    await disk.manager.async_options_changed(disk.entry)
    await flush_core(disk)
    recovered = await restart_from_disk(disk)
    assert dict(disk.entry.options) == older
    assert recovered.settings["receiver_limits"] == {}
    assert recovered.trial["offsets"] == {SCANNER: -2.0}


async def test_history_saved_after_window_commit_survives_restart_and_later_external_edit(disk):
    manager = disk.manager
    await manager.async_dispatch("history_start", {})
    await manager.async_dispatch("settings", {"settings": {"history_window_s": 120.0}})
    row = snapshot_observations(manager._snapshot())[0]
    row["stamp"] += 1
    now = time.time()
    assert manager.history.record([row], captured_at=now, model_version=manager.version()) == 1
    await manager.store.async_save(manager._store_payload())
    frames = manager.history.frames()
    clocks = manager.history.export_clocks()
    await flush_core(disk)
    external = deepcopy(dict(disk.entry.options))
    external["legacy_unrelated"] = "later-external"
    disk.hass.config_entries.async_update_entry(disk.entry, options=external)
    await flush_core(disk)
    recovered = await restart_from_disk(disk)
    assert recovered.history.frames() == frames
    assert recovered.history.export_clocks() == clocks


async def test_near_budget_history_does_not_block_profiles_settings_and_calibration(disk):
    manager = disk.manager
    await manager.async_dispatch("history_start", {})
    row = snapshot_observations(manager._snapshot())[0]
    start = time.time() - 600
    for index in range(600):
        captured = start + index
        packet = {
            **row,
            "observed": captured,
            "age": 0.0,
            "stamp": float(index + 1),
            "quality": {"synthetic_padding": ["x" * 2000, "y" * 2000]},
        }
        manager.history.record([packet], captured_at=captured, model_version=manager.version())
    assert manager.history.summary()["bytes"] > 1_400_000
    frames = manager.history.frames()
    for action, payload in (
        ("profile", {"kind": "receiver", "id": SCANNER, "profile": {"environment": "indoor"}}),
        ("settings", {"settings": {"receiver_limits": {SCANNER: 7.0}}}),
    ):
        await manager.async_dispatch(action, payload)
    await apply(disk)
    assert manager.history.frames() == frames
    assert len(json.dumps(manager._options_journal).encode()) < 100_000
    await flush_core(disk)
    await manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 8.0}}})
    prepared = json.loads(Path(manager.store.path).read_text())["data"]["options_journal"]
    assert "fallback" not in prepared
