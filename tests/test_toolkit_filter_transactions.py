"""Rejected durable commits preserve the real accepted live filter pipeline."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from homeassistant.util.file import WriteError

from custom_components.bermuda.const import CONF_RSSI_OFFSETS
from custom_components.bermuda.number import BermudaNumber
from custom_components.bermuda.sensor import BermudaSensorRange
from custom_components.bermuda.toolkit import OPTIONS_KEY
from tests.test_toolkit_model_rebase import model_runtime, packet, source_state
from tests.test_toolkit_options_recovery import disk, flush_core, restart_from_disk
from tests.test_toolkit_runtime import DEVICE, SCANNER, _proposal, runtime
from tests.test_toolkit_store import durable_runtime


def select(runtime):
    runtime.device.calculate_data()
    runtime.coordinator._refresh_area_by_min_distance(runtime.device)


@pytest.fixture
async def stationary_runtime(disk, model_runtime, monkeypatch):
    assert disk is model_runtime
    state = model_runtime
    for module in ("sensor", "entity"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: state.clock["now"])
    await state.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "stationary"}}})
    await flush_core(state)
    area = state.coordinator.ar.async_create("Synthetic filter transaction room")
    state.scanner._update_area_and_floor(area.id)
    for _ in range(6):
        packet(state, -68)
    packet(state, -38)  # A pending jump is distinct from the still-accepted range.
    select(state)
    assert state.device.area_id == area.id
    assert state.advert.rssi_distance != state.advert.rssi_distance_raw
    state.area = area
    state.entity = BermudaSensorRange(state.coordinator, state.entry, DEVICE)
    state.entity.bermuda_update_interval = 60
    assert state.entity.native_value == round(state.advert.rssi_distance, 1)
    return state


def gate_acceptance(state, monkeypatch, *, failure=None):
    entered, release = asyncio.Event(), asyncio.Event()
    state.io_releases.append(release)
    actual = state.manager.store._async_write_data
    calls = 0

    async def gated(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
            if failure is not None:
                raise failure
        await actual(data)

    monkeypatch.setattr(state.manager.store, "_async_write_data", gated)
    return entered, release


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("radio_change", [False, True])
@pytest.mark.parametrize("failure", [WriteError("accepted write failed"), asyncio.CancelledError()])
async def test_rejected_filter_acceptance_keeps_selected_area_and_packets(
    stationary_runtime, monkeypatch, fresh, radio_change, failure
):
    state = stationary_runtime
    manager, advert = state.manager, state.advert
    baseline = deepcopy(dict(state.entry.options))
    pipeline = manager._states[(DEVICE, SCANNER)]
    revision = state.device.model_revision
    distance = advert.rssi_distance
    change = {"filters": {DEVICE: "pet"}}
    if radio_change:
        change["receiver_profiles"] = {SCANNER: {"rssi_offset": 5.0}}
    entered, release = gate_acceptance(state, monkeypatch, failure=failure)
    pending = asyncio.create_task(manager.async_dispatch("settings", {"settings": change}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.settings["filters"] == {DEVICE: "stationary"}
        assert manager._states[(DEVICE, SCANNER)] is pipeline
        assert advert.rssi_distance == distance
        if fresh:
            packet(state, -38)  # The existing stationary candidate gains real evidence.
        select(state)
        assert state.device.area_id == state.area.id
        assert state.entity.native_value == round(distance, 1)
        live_source = source_state(advert)
        live_quality = deepcopy(pipeline.snapshot(state.clock["now"]))
        live_histories = (
            list(advert.hist_distance),
            list(advert.hist_distance_by_interval),
            list(advert.hist_velocity),
        )
    finally:
        release.set()
        with pytest.raises(OSError if isinstance(failure, WriteError) else asyncio.CancelledError):
            await pending
    assert dict(state.entry.options) == baseline
    assert manager.settings["filters"] == {DEVICE: "stationary"}
    assert manager._states[(DEVICE, SCANNER)] is pipeline
    assert source_state(advert) == live_source
    assert pipeline.snapshot(state.clock["now"]) == live_quality
    assert (advert.hist_distance, advert.hist_distance_by_interval, advert.hist_velocity) == live_histories
    select(state)  # Ordinary cached processing must retain the accepted election.
    assert state.device.area_id == state.area.id
    assert advert.rssi_distance == distance
    assert state.entity.native_value == round(distance, 1)
    assert state.device.model_revision == revision
    assert pipeline.snapshot(state.clock["now"])["samples"] == 7 + fresh
    assert len(pipeline._candidate) == 1 + fresh
    journal = json.loads(Path(manager.store.path).read_text())["data"]["options_journal"]
    assert journal["phase"] == "prepared"
    assert journal["desired"][OPTIONS_KEY]["filters"] == {DEVICE: "pet"}
    # A normal diagnostic save must retain compensation provenance for restart.
    await manager.store.async_save(manager._store_payload())
    recovered = await restart_from_disk(state)
    assert recovered.settings["filters"] == {DEVICE: "stationary"}
    assert dict(state.entry.options) == baseline
    state.coordinator.toolkit = manager
    for _ in range(3 - fresh):
        packet(state, -38)
    assert advert.rssi_distance == pytest.approx(advert.rssi_distance_raw)
    assert manager._states[(DEVICE, SCANNER)] is pipeline
    assert pipeline._candidate == []


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_successful_filter_acceptance_waits_for_durable_acknowledgement(
    stationary_runtime, monkeypatch, cancel_caller
):
    state = stationary_runtime
    manager = state.manager
    pipeline = manager._states[(DEVICE, SCANNER)]
    entered, release = gate_acceptance(state, monkeypatch)
    pending = asyncio.create_task(manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet"}}}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owned = manager._action_task
        if cancel_caller:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        packet(state, -38)
        select(state)
        assert manager._states[(DEVICE, SCANNER)] is pipeline
        assert manager.settings["filters"] == {DEVICE: "stationary"}
        assert state.device.area_id == state.area.id
        assert len(pipeline._candidate) == 2
    finally:
        release.set()
        await (owned if cancel_caller else pending)
    assert manager.settings["filters"] == {DEVICE: "pet"}
    assert (DEVICE, SCANNER) not in manager._states
    assert state.advert.rssi_distance is None
    assert state.advert.hist_distance_by_interval == []
    assert json.loads(Path(manager.store.path).read_text())["data"]["options_journal"]["phase"] == "accepted"
    packet(state, -38)
    select(state)
    assert state.device.area_id == state.area.id
    assert manager._states[(DEVICE, SCANNER)].mode == "pet"
    assert manager._states[(DEVICE, SCANNER)].snapshot(state.clock["now"])["samples"] == 1


async def test_rejected_filter_commit_preserves_number_change_during_accepted_write(stationary_runtime, monkeypatch):
    state = stationary_runtime
    manager, advert = state.manager, state.advert
    pipeline = manager._states[(DEVICE, SCANNER)]
    entered, release = gate_acceptance(state, monkeypatch, failure=WriteError("accepted write failed"))
    pending = asyncio.create_task(manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet"}}}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        BermudaNumber(state.coordinator, state.entry, DEVICE)._set_ref_power_and_invalidate(-61.0)
        packet(state, -38)
        select(state)
        source = source_state(advert)
        distance = advert.rssi_distance
        histories = list(advert.hist_distance), list(advert.hist_distance_by_interval)
        version = manager.version()
    finally:
        release.set()
        with pytest.raises(OSError):
            await pending
    assert manager._states[(DEVICE, SCANNER)] is pipeline
    assert manager.effective_advert_model(advert)["reference_power"] == -61.0
    assert manager.version() == version
    assert source_state(advert) == source
    assert (advert.hist_distance, advert.hist_distance_by_interval) == histories
    select(state)
    assert advert.rssi_distance == distance
    assert state.device.area_id == state.area.id
    assert state.entity.native_value == round(distance, 1)


@pytest.mark.parametrize("action", ["profile", "calibration_apply", "calibration_rollback"])
async def test_rejected_radio_and_rollback_actions_keep_the_current_filter_pipeline(
    stationary_runtime, monkeypatch, action
):
    state = stationary_runtime
    manager, advert = state.manager, state.advert
    payload = {}
    if action == "calibration_rollback":
        # A promoted trial changes both radio settings and filtering. Rollback
        # must retain that accepted pipeline until its own acknowledgement.
        await manager._apply({SCANNER: 4.5}, extra_settings={"filters": {DEVICE: "pet"}})
        for _ in range(6):
            packet(state, -68)
        packet(state, -38)
        select(state)
    elif action == "calibration_apply":
        manager.proposal = _proposal(manager, {SCANNER: 4.5})
    else:
        payload = {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 4.5}}
    pipeline = manager._states[(DEVICE, SCANNER)]
    settings = deepcopy(manager.settings)
    trial = deepcopy(manager.trial)
    options = deepcopy(dict(state.entry.options))
    entered, release = gate_acceptance(state, monkeypatch, failure=WriteError("accepted failed"))
    pending = asyncio.create_task(manager.async_dispatch(action, payload))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.settings == settings
        assert manager.trial == trial
        packet(state, -38)
        select(state)
        source = source_state(advert)
        quality = deepcopy(pipeline.snapshot(state.clock["now"]))
        distance = advert.rssi_distance
        histories = list(advert.hist_distance), list(advert.hist_distance_by_interval)
    finally:
        release.set()
        with pytest.raises(OSError):
            await pending
    assert dict(state.entry.options) == options
    assert manager.settings == settings
    assert manager.trial == trial
    assert manager._states[(DEVICE, SCANNER)] is pipeline
    assert source_state(advert) == source
    assert pipeline.snapshot(state.clock["now"]) == quality
    assert (advert.hist_distance, advert.hist_distance_by_interval) == histories
    select(state)
    assert advert.rssi_distance == distance
    assert state.device.area_id == state.area.id


@pytest.mark.parametrize("fail", [False, True])
async def test_later_external_policy_survives_accepted_write_without_stale_filter_reset(
    stationary_runtime, monkeypatch, fail
):
    state = stationary_runtime
    manager, advert = state.manager, state.advert
    baseline = deepcopy(dict(state.entry.options))
    pipeline = manager._states[(DEVICE, SCANNER)]
    distance = advert.rssi_distance
    entered, release = gate_acceptance(state, monkeypatch, failure=WriteError("accepted failed") if fail else None)
    pending = asyncio.create_task(manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet"}}}))
    listener = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        external = deepcopy(baseline)
        external[CONF_RSSI_OFFSETS] = {SCANNER: 4.5}
        external["legacy_unrelated"] = "later external edit"
        state.hass.config_entries.async_update_entry(state.entry, options=external)
        listener = asyncio.create_task(manager.async_options_changed(state.entry))
        packet(state, -38)
        source = source_state(advert)
    finally:
        release.set()
        if fail:
            with pytest.raises(OSError):
                await pending
        else:
            await pending
        if listener is not None:
            assert await listener is False  # The unrelated legacy edit still requests reload.
    assert state.entry.options["legacy_unrelated"] == "later external edit"
    assert state.entry.options[CONF_RSSI_OFFSETS] == {SCANNER: 4.5}
    assert manager.settings["filters"] == {DEVICE: "stationary"}
    assert manager._states[(DEVICE, SCANNER)] is pipeline
    assert len(pipeline._candidate) == 2
    assert source_state(advert) == source
    select(state)
    assert state.device.area_id == state.area.id
    assert advert.rssi_distance == pytest.approx(distance * 10 ** (-3 / 30))
    assert state.entity.native_value == round(advert.rssi_distance, 1)
