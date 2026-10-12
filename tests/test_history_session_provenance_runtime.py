"""Real toolkit actions cannot turn cached source packets into new calibration data."""

from copy import deepcopy
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.storage import Store

from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.recording import DiagnosticHistory, MAX_CLOCK_EPOCH
from custom_components.bermuda.toolkit import ToolkitManager

from .test_calibration_signal_evidence_runtime import CORRECTIONS, advertisement, collect_evidence, evidence_runtime
from .test_toolkit_runtime import DEVICE, SCANNER, runtime


def measured(device=DEVICE, location="measured:one"):
    return {"device_id": device, "location_id": location, "ranges": {SCANNER: 2.0}, "accuracy_m": 0.01}


def advance(value, monkeypatch, stamp=101.0):
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: stamp)
    value.scanner.stamps[DEVICE.upper()] = stamp
    value.advert.update_advertisement(advertisement(-68), value.scanner)
    value.advert.calculate_data()


async def reload_manager(value, monkeypatch, *, changed_boot=False):
    old = value.manager
    old._boot_id = "provenance-boot-a"
    saved = deepcopy(old._store_payload())
    await old.async_close()
    monkeypatch.setattr(
        "custom_components.bermuda.toolkit._host_clock_id",
        lambda: "provenance-boot-b" if changed_boot else "provenance-boot-a",
    )
    manager = value.coordinator.toolkit = value.manager = ToolkitManager(value.coordinator)
    manager.store = MagicMock(spec=Store)
    manager.store.async_load = AsyncMock(return_value=saved)
    manager.store.async_save = AsyncMock()
    await manager.async_load()
    return manager


@pytest.mark.parametrize("transition", ["append", "identifier-import", "redacted-import", "empty-import"])
async def test_same_host_cached_packets_never_become_new_live_evidence(runtime, monkeypatch, transition):
    manager = runtime.manager
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1
    frames = manager.history.frames()
    previous_epoch = manager.history.session_epoch
    if transition == "append":
        await manager.async_dispatch("history_start", {"clear": False})
        assert manager.history.session_epoch == previous_epoch
    else:
        data = (
            DiagnosticHistory().export()
            if transition == "empty-import"
            else await manager.async_dispatch("history_export", {"redact": transition == "redacted-import"})
        )
        await manager.async_dispatch("history_import", {"data": data})
        assert manager.history.session_epoch > previous_epoch
    before = manager.history.export(redact=False), manager.history.export_clocks()
    for _ in range(2):
        with pytest.raises(ValueError, match="No fresh observations|wait for fresh source"):
            await manager.async_dispatch("capture", measured())
        assert (manager.history.export(redact=False), manager.history.export_clocks()) == before
    if transition in ("append", "identifier-import"):
        assert manager.history.frames() == frames
    advance(runtime, monkeypatch)
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1


@pytest.mark.parametrize("case", ["lower", "source-upper", "all-upper"])
async def test_import_mac_case_does_not_separate_cached_physical_source(runtime, case):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    data = await manager.async_dispatch("history_export", {"redact": False})
    for frame in data["frames"]:
        for row in frame["observations"]:
            if case != "lower":
                row["source_id"] = row["source_id"].upper()
            if case == "all-upper":
                row["device_id"], row["scanner_id"] = row["device_id"].upper(), row["scanner_id"].upper()
        if case == "all-upper":
            for label in frame["truth"]:
                label["device_id"] = label["device_id"].upper()
                label["ranges"] = {key.upper(): distance for key, distance in label["ranges"].items()}
    await manager.async_dispatch("history_import", {"data": data})
    before = manager.history.export(redact=False), manager.history.export_clocks()
    with pytest.raises(ValueError, match="wait for fresh source"):
        await manager.async_dispatch("capture", measured())
    assert (manager.history.export(redact=False), manager.history.export_clocks()) == before


async def test_foreign_monotonic_stamps_cannot_govern_current_host_live_capture(runtime, monkeypatch):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    data = await manager.async_dispatch("history_export", {"redact": False})
    for frame in data["frames"]:
        for row in frame["observations"]:
            row["stamp"] = 1e10
    await manager.async_dispatch("history_import", {"data": data})
    assert manager.history._source_clocks[(DEVICE, SCANNER)][0] == 100.0
    with pytest.raises(ValueError, match="wait for fresh source"):
        await manager.async_dispatch("capture", measured())
    advance(runtime, monkeypatch)
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1


async def test_tracked_actual_source_and_meta_alias_cannot_recapture_one_packet_after_import(runtime, monkeypatch):
    manager = runtime.manager
    meta_id = "1234567890abcdef1234567890abcdef_1_1"
    meta = BermudaDevice(meta_id, runtime.coordinator)
    meta.create_sensor = True
    meta.adverts[(DEVICE, SCANNER)] = runtime.advert
    runtime.coordinator.devices[meta_id] = meta
    manager.prepare_update()
    await manager.async_dispatch("capture", measured())
    await manager.async_dispatch(
        "history_import", {"data": await manager.async_dispatch("history_export", {"redact": False})}
    )
    before = manager.history.summary()["rows"]
    with pytest.raises(ValueError, match="wait for fresh source"):
        await manager.async_dispatch("capture", measured(meta_id))
    assert manager.history.summary()["rows"] == before
    advance(runtime, monkeypatch)
    manager.prepare_update()
    meta.adverts[(DEVICE, SCANNER)].calculate_data()
    assert (await manager.async_dispatch("capture", measured(meta_id)))["captured"] == 1


@pytest.mark.parametrize("source_state", ["stopped", "disappeared", "stale"])
async def test_import_retains_cached_boundary_with_stopped_or_unavailable_sources(runtime, monkeypatch, source_state):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    data = await manager.async_dispatch("history_export", {"redact": False})
    adverts = runtime.device.adverts.copy()
    if source_state == "stopped":
        await manager.async_dispatch("history_stop", {})
        advance(runtime, monkeypatch)
    elif source_state == "disappeared":
        runtime.device.adverts.clear()
    else:
        for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
            monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: 150.0)
        assert manager._snapshot()["devices"][DEVICE]["scanners"][SCANNER]["status"] == "stale"
    await manager.async_dispatch("history_import", {"data": data})
    runtime.device.adverts.update(adverts)
    before = manager.history.export(redact=False), manager.history.export_clocks()
    with pytest.raises(ValueError, match="No fresh observations|wait for fresh source"):
        await manager.async_dispatch("capture", measured())
    assert (manager.history.export(redact=False), manager.history.export_clocks()) == before
    advance(runtime, monkeypatch, stamp=151.0 if source_state == "stale" else 102.0)
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1


@pytest.mark.parametrize("bad_stamp", [1e10, float("inf"), float("nan")])
async def test_bad_live_source_clock_at_import_cannot_poison_following_valid_source(runtime, monkeypatch, bad_stamp):
    manager = runtime.manager
    runtime.advert.stamp = bad_stamp
    await manager.async_dispatch("history_import", {"data": DiagnosticHistory().export()})
    assert manager.history.export_clocks()["source_clocks"] == []
    runtime.advert.stamp = 100.0
    advance(runtime, monkeypatch)
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1


async def test_fixed_reference_collector_skips_import_boundary_and_records_only_advanced_sources(runtime, monkeypatch):
    manager = runtime.manager
    await manager.async_dispatch(
        "settings", {"settings": {"fixed_references": {DEVICE: {"location_id": "fixed:one", "ranges": {SCANNER: 2.0}}}}}
    )
    await manager._after_update()
    assert manager.history.summary()["rows"] == 1
    await manager.async_dispatch(
        "history_import", {"data": await manager.async_dispatch("history_export", {"redact": False})}
    )
    frames = manager.history.frames()
    await manager._after_update()
    await manager._after_update()
    assert manager.history.frames() == frames
    assert not manager.last_result or manager.last_result.get("status") != "pending"
    advance(runtime, monkeypatch)
    await manager._after_update()
    assert manager.history.summary()["rows"] == 2
    assert manager.history.frames()[-1]["truth"][0]["source"] == "fixed_reference"


@pytest.mark.parametrize("failure", ["worker", "store"])
async def test_failed_import_leaves_evidence_watermarks_options_model_and_quality_unchanged(
    runtime, monkeypatch, failure
):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    data = await manager.async_dispatch("history_export", {"redact": False})
    # Inspecting an unprocessed later clock must not advance FilterState._last_now.
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: 101.0)
    before = (
        manager.history.export(redact=False),
        manager.history.export_clocks(),
        deepcopy(dict(runtime.entry.options)),
        manager.version(),
        deepcopy(manager._quality),
        deepcopy(vars(manager._states[(DEVICE, SCANNER)])),
    )
    if failure == "worker":
        context = patch.object(manager, "_run_worker", side_effect=RuntimeError("synthetic worker failure"))
    else:
        context = patch.object(manager.store, "async_save", side_effect=OSError("synthetic store failure"))
    with context, pytest.raises((RuntimeError, OSError), match="synthetic"):
        await manager.async_dispatch("history_import", {"data": data})
    assert (
        manager.history.export(redact=False),
        manager.history.export_clocks(),
        dict(runtime.entry.options),
        manager.version(),
        manager._quality,
        vars(manager._states[(DEVICE, SCANNER)]),
    ) == before
    advance(runtime, monkeypatch, stamp=102.0)
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1


async def test_failed_capture_after_import_does_not_advance_durable_boundary(runtime, monkeypatch):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    await manager.async_dispatch(
        "history_import", {"data": await manager.async_dispatch("history_export", {"redact": False})}
    )
    advance(runtime, monkeypatch)
    before = manager.history.export(redact=False), manager.history.export_clocks()
    with patch.object(manager.store, "async_save", side_effect=OSError("synthetic capture failure")):
        with pytest.raises(OSError, match="synthetic"):
            await manager.async_dispatch("capture", measured())
    assert (manager.history.export(redact=False), manager.history.export_clocks()) == before
    assert (await manager.async_dispatch("capture", measured()))["captured"] == 1


async def test_import_boundary_watermarks_persist_across_same_host_local_reload(runtime, monkeypatch):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    await manager.async_dispatch("history_import", {"data": DiagnosticHistory().export()})
    markers = manager.history.export_clocks()
    manager = await reload_manager(runtime, monkeypatch)
    try:
        assert manager.history.export_clocks() == markers
        with pytest.raises(ValueError, match="wait for fresh source"):
            await manager.async_dispatch("capture", measured())
        advance(runtime, monkeypatch)
        assert (await manager.async_dispatch("capture", measured()))["captured"] == 1
    finally:
        await manager.async_close()


async def test_valid_new_boot_clears_same_host_boundary_and_accepts_lower_source_stamp(runtime, monkeypatch):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    await manager.async_dispatch(
        "history_import", {"data": await manager.async_dispatch("history_export", {"redact": False})}
    )
    epoch = manager.history.session_epoch
    manager = await reload_manager(runtime, monkeypatch, changed_boot=True)
    try:
        assert manager.history.session_epoch == epoch + 1
        assert manager.history.export_clocks()["live_boundaries"] == []
        for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
            monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: 1.0)
        runtime.scanner.stamps[DEVICE.upper()] = 1.0
        runtime.advert = BermudaAdvert(runtime.device, advertisement(-68), runtime.coordinator.options, runtime.scanner)
        runtime.device.adverts[(DEVICE, SCANNER)] = runtime.advert
        runtime.advert.calculate_data()
        assert (await manager.async_dispatch("capture", measured()))["captured"] == 1
        assert manager.history.summary()["rows"] == 2
    finally:
        await manager.async_close()


async def test_maximum_epoch_changed_boot_compacts_retained_evidence_and_accepts_new_source(runtime, monkeypatch):
    manager = runtime.manager
    data = DiagnosticHistory().export()
    data["session_epoch"] = MAX_CLOCK_EPOCH - 1
    await manager.async_dispatch("history_import", {"data": data})
    advance(runtime, monkeypatch)
    await manager.async_dispatch("capture", measured())
    assert manager.history.session_epoch == MAX_CLOCK_EPOCH
    frames = manager.history.frames()
    manager = await reload_manager(runtime, monkeypatch, changed_boot=True)
    try:
        assert manager.history.session_epoch == 2
        assert manager.last_result == {"reasons": ["history_clock_epochs_rebased"], "status": "recovered"}
        assert manager._history_epoch_rebased_at is None
        for before, after in zip(frames, manager.history.frames(), strict=True):
            assert {key: value for key, value in before.items() if key != "session_epoch"} == {
                key: value for key, value in after.items() if key != "session_epoch"
            }
        for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
            monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: 1.0)
        runtime.scanner.stamps[DEVICE.upper()] = 1.0
        runtime.advert = BermudaAdvert(runtime.device, advertisement(-68), runtime.coordinator.options, runtime.scanner)
        runtime.device.adverts[(DEVICE, SCANNER)] = runtime.advert
        runtime.advert.calculate_data()
        assert (await manager.async_dispatch("capture", measured()))["captured"] == 1
        assert [frame["session_epoch"] for frame in manager.history.frames()] == [1, 2]
        trusted = DiagnosticHistory()
        trusted.import_data(manager.history.export(redact=False), now=manager._snapshot()["captured"], resume=True)
        assert trusted.frames() == manager.history.frames()
    finally:
        await manager.async_close()


async def test_rebased_consumed_calibration_waits_for_new_eligible_source_observation(evidence_runtime, monkeypatch):
    value, manager = evidence_runtime, evidence_runtime.manager
    await collect_evidence(value, fixed=False)
    data = await manager.async_dispatch("history_export", {"redact": False})
    for frame in data["frames"]:
        frame["session_epoch"] = MAX_CLOCK_EPOCH - 1
    data["session_epoch"] = MAX_CLOCK_EPOCH - 1
    await manager.async_dispatch("history_import", {"data": data})
    previous = await manager.async_dispatch("calibration_preview", {})
    assert previous["eligible"]
    manager.last_data_id = previous["data_id"]
    # Represent durably consumed evidence; the earlier settings journal must
    # carry the same committed marker rather than recover its pre-consumption ID.
    manager._options_journal["after"]["last_data_id"] = previous["data_id"]
    original_rows = manager._labelled_rows()
    manager = await reload_manager(value, monkeypatch, changed_boot=True)
    try:
        cutoff = manager._history_epoch_rebased_at
        assert cutoff == value.clock.epoch
        rebased = await manager.async_dispatch("calibration_preview", {})
        assert rebased["eligible"]
        assert rebased["coverage"]["samples"] == 32
        assert rebased["data_id"] != previous["data_id"]
        assert rebased["metrics"] == previous["metrics"]
        assert manager.engine.evaluate_trial(
            original_rows,
            previous_offsets={scanner: 0.0 for scanner in CORRECTIONS},
            proposed_offsets=CORRECTIONS,
            now=value.clock.epoch,
            model_version=manager.version(),
            verified_reference_devices=manager._verified_devices(),
        ) == manager.engine.evaluate_trial(
            manager._labelled_rows(),
            previous_offsets={scanner: 0.0 for scanner in CORRECTIONS},
            proposed_offsets=CORRECTIONS,
            now=value.clock.epoch,
            model_version=manager.version(),
            verified_reference_devices=manager._verified_devices(),
        )
        await manager._automatic(value.clock.epoch)
        assert not manager.last_result["apply"]
        assert "no_new_independent_data" in manager.last_result["reasons"]
        assert manager.trial is None

        # The cutoff is local-only and survives normal same-host save/reload.
        assert "history_epoch_rebased_at" not in manager.history.export()
        manager = await reload_manager(value, monkeypatch)
        assert manager._history_epoch_rebased_at == cutoff
        await manager._automatic(value.clock.epoch)
        assert "no_new_independent_data" in manager.last_result["reasons"]
        with patch.object(manager.store, "async_save", side_effect=OSError("synthetic rebased import failure")):
            with pytest.raises(OSError, match="synthetic rebased"):
                await manager.async_dispatch("history_import", {"data": manager.history.export(redact=False)})
        assert manager._history_epoch_rebased_at == cutoff
        assert manager._store_payload()["history_epoch_rebased_at"] == cutoff

        # Rejected post-reboot evidence cannot unlock the renumbered data ID.
        frame = manager.history.frames()[-1]
        bad = deepcopy(frame["observations"])
        value.clock.epoch += 0.1
        for row in bad:
            row.update(stamp=row["stamp"] + 1, observed=value.clock.epoch, age=0)
            row["quality"]["source_trusted"] = False
        labels = deepcopy(frame["truth"])
        for label in labels:
            label["observed"] = value.clock.epoch
        manager.history.record(
            bad, captured_at=value.clock.epoch, model_version=manager.version(), truth=labels, force=True
        )
        await manager._automatic(value.clock.epoch)
        assert "no_new_independent_data" in manager.last_result["reasons"]

        # A genuinely advanced eligible source packet can resume automatic work.
        value.clock.epoch += 0.1
        good = deepcopy(bad)
        for row in good:
            row.update(stamp=row["stamp"] + 1, observed=value.clock.epoch)
            row["quality"].pop("source_trusted")
        for label in labels:
            label["observed"] = value.clock.epoch
        manager.history.record(
            good, captured_at=value.clock.epoch, model_version=manager.version(), truth=labels, force=True
        )
        await manager._automatic(value.clock.epoch)
        assert manager.last_result["applied"]
        assert manager.trial["automatic"]
    finally:
        await manager.async_close()


@pytest.mark.parametrize("cutoff", [None, -1, True, float("nan"), float("inf"), "1000"])
async def test_malformed_local_rebase_cutoff_does_not_restore(runtime, monkeypatch, cutoff):
    old = runtime.manager
    old._boot_id = "provenance-boot-a"
    saved = old._store_payload()
    saved["history_epoch_rebased_at"] = cutoff
    await old.async_close()
    monkeypatch.setattr("custom_components.bermuda.toolkit._host_clock_id", lambda: "provenance-boot-a")
    manager = runtime.coordinator.toolkit = ToolkitManager(runtime.coordinator)
    manager.store = MagicMock(spec=Store)
    manager.store.async_load = AsyncMock(return_value=saved)
    manager.store.async_save = AsyncMock()
    try:
        await manager.async_load()
        assert manager._history_epoch_rebased_at is None
    finally:
        await manager.async_close()


async def test_export_import_round_trip_cannot_manufacture_missing_training_packet(evidence_runtime):
    value, manager = evidence_runtime, evidence_runtime.manager
    await collect_evidence(value, fixed=False)
    exported = await manager.async_dispatch("history_export", {"redact": False})
    exported["frames"] = [frame for frame in exported["frames"] if frame["sequence"] in {7, 8, 15, 16}]
    await manager.async_dispatch("history_import", {"data": exported})
    ranges = {scanner.address: 2.0 + index for index, scanner in enumerate(value.receivers)}
    for _ in range(36):
        value.clock.mono += 0.1
        value.clock.epoch += 0.1
        for scanner in value.receivers:
            scanner.stamps[DEVICE.upper()] = value.clock.mono
            advert = value.device.adverts[(DEVICE, scanner.address)]
            rssi = -55.0 - 30.0 * math.log10(ranges[scanner.address]) - CORRECTIONS[scanner.address]
            advert.update_advertisement(advertisement(rssi), scanner)
            advert.calculate_data()
    capture = {"device_id": DEVICE, "location_id": "synthetic-site-0", "ranges": ranges, "accuracy_m": 0.01}
    assert (await manager.async_dispatch("capture", capture))["captured"] == 2
    before = await manager.async_dispatch("calibration_preview", {})
    assert not before["eligible"]
    assert before["coverage"]["samples"] == 10
    assert "insufficient_training_samples" in before["reasons"]
    radio = [(advert.rssi, advert.stamp) for advert in value.device.adverts.values()]
    for _ in range(3):
        await manager.async_dispatch(
            "history_import", {"data": await manager.async_dispatch("history_export", {"redact": False})}
        )
        with pytest.raises(ValueError, match="wait for fresh source"):
            await manager.async_dispatch("capture", capture)
        after = await manager.async_dispatch("calibration_preview", {})
        assert after["coverage"]["samples"] == 10
        assert after["data_id"] == before["data_id"]
        assert not after["eligible"]
        decision = manager.engine.automatic_decision(
            after,
            now=value.clock.epoch,
            model_version=manager.version(),
            current_offsets=manager._current_offsets(),
            last_data_id=before["data_id"],
            observations=manager._proposal_rows,
            verified_reference_devices=manager._verified_devices(),
        )
        assert not decision["apply"]
        assert "no_new_independent_data" in decision["reasons"]
    assert radio == [(advert.rssi, advert.stamp) for advert in value.device.adverts.values()]
