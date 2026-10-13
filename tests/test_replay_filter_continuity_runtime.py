"""Synthetic imported mode transitions preserve one offline candidate pipeline."""

from copy import deepcopy
import math
import time

import pytest

from custom_components.bermuda.replay import ReplayEngine

from .test_range_replay import frames
from .test_toolkit_detached_replay import captured_export
from .test_toolkit_runtime import DEVICE, SCANNER, runtime


def filter_history(exported, modes):
    """Build accepted raw samples across frozen model and filter-mode changes."""
    template = exported["frames"][0]
    captured = time.time() - len(modes)
    exported = deepcopy(exported)
    exported["frames"] = []
    for index, mode in enumerate(modes):
        frame = deepcopy(template)
        frame.update(sequence=index + 1, captured_at=captured + index, model_version=f"synthetic-{mode}")
        row, label = frame["observations"][0], frame["truth"][0]
        distance = 1 if index == 0 else 10
        scanner = SCANNER if index % 2 else SCANNER.upper()
        row.update(
            scanner_id=scanner,
            observed=frame["captured_at"],
            stamp=row["stamp"] + index,
            rssi=-59 if index == 0 else -79,
            raw_distance=distance,
            filtered_distance=distance,
            model_version=frame["model_version"],
        )
        row["quality"]["mode"] = mode
        label.update(observed=frame["captured_at"], ranges={scanner: distance})
        exported["frames"].append(frame)
    return exported


def replay_snapshot(runtime):
    """Observe the live pipeline, history and storage without reading fresh samples."""
    manager, advert = runtime.manager, runtime.advert
    return (
        deepcopy(dict(runtime.entry.options)),
        manager.history.export(redact=False),
        manager.history.export_clocks(),
        deepcopy(manager._quality),
        {key: (id(state), deepcopy(vars(state))) for key, state in manager._states.items()},
        (advert.rssi, advert.stamp, advert.rssi_distance_raw, advert.rssi_distance, dict(runtime.scanner.stamps)),
        manager.store.async_save.call_count,
    )


@pytest.mark.parametrize("candidate_mode", ["pet", "stationary"])
@pytest.mark.parametrize("starts_matching", [True, False])
async def test_imported_mixed_modes_replay_one_continuous_requested_policy(runtime, candidate_mode, starts_matching):
    manager = runtime.manager
    other_mode = "stationary" if candidate_mode == "pet" else "pet"
    recorded_modes = (
        [candidate_mode, other_mode, other_mode, other_mode, candidate_mode]
        if starts_matching
        else ["legacy", candidate_mode, candidate_mode, candidate_mode, other_mode]
    )
    exported = filter_history(await captured_export(runtime, redact=False), recorded_modes)
    await manager.async_dispatch("history_import", {"data": exported})
    before = replay_snapshot(runtime)
    payload = {"candidate": {}, "filter_modes": {DEVICE.upper(): candidate_mode}}
    report = await manager.async_dispatch("replay", payload)
    assert await manager.async_dispatch("replay", payload) == report
    expected = [1, 1, 1, 10, 10] if candidate_mode == "pet" else [1, 1, 1, 1, 10]
    assert [row["raw"] for row in report["observations"]] == [1, 10, 10, 10, 10]
    assert [row["current"] for row in report["observations"]] == [1, 10, 10, 10, 10]
    assert [row["candidate"] for row in report["observations"]] == pytest.approx(expected)
    assert all(row["candidate_pipeline"] == f"replayed_{candidate_mode}_filter" for row in report["observations"])
    assert replay_snapshot(runtime) == before


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("changed_offset", [True, False])
async def test_uniform_same_policy_preserves_frozen_warmup_jitter_and_noise(runtime, mode, changed_offset):
    manager = runtime.manager
    exported = filter_history(await captured_export(runtime, redact=False), [mode] * 5)
    current = [None, 6.7, 6.7, 7.1, 6.9]
    for index, frame in enumerate(exported["frames"]):
        row = frame["observations"][0]
        row["filtered_distance"] = current[index]
        row["quality"].update(
            noise_db=None if index < 2 else 3.5,
            reasons=[{"code": "insufficient_quality_samples", "samples": index + 1}] if index < 4 else [],
        )
    await manager.async_dispatch("history_import", {"data": exported})
    before = replay_snapshot(runtime)
    candidate = {SCANNER: 20 * math.log10(2)} if changed_offset else {}
    report = await manager.async_dispatch("replay", {"candidate": candidate, "filter_modes": {DEVICE: mode}})
    scale = 0.5 if changed_offset else 1
    assert [row["current"] for row in report["observations"]] == current
    assert report["observations"][0]["candidate"] is None
    assert [row["candidate"] for row in report["observations"][1:]] == pytest.approx(
        [distance * scale for distance in current[1:]]
    )
    assert all(row["candidate_pipeline"] == "preserved_live_filter" for row in report["observations"])
    assert replay_snapshot(runtime) == before


async def test_offset_only_mixed_history_preserves_each_actual_live_filter(runtime):
    manager = runtime.manager
    exported = filter_history(
        await captured_export(runtime, redact=False), ["legacy", "pet", "pet", "pet", "stationary"]
    )
    await manager.async_dispatch("history_import", {"data": exported})
    before = replay_snapshot(runtime)
    report = await manager.async_dispatch("replay", {"candidate": {SCANNER: 20 * math.log10(2)}})
    assert [row["candidate"] for row in report["observations"]] == pytest.approx([0.5, 5, 5, 5, 5])
    assert all(row["candidate_pipeline"] == "preserved_live_filter" for row in report["observations"])
    assert replay_snapshot(runtime) == before


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("next_epoch_matching", [True, False])
async def test_candidate_source_epoch_restarts_and_classifies_its_own_policy(runtime, mode, next_epoch_matching):
    manager = runtime.manager
    other_mode = "stationary" if mode == "pet" else "pet"
    exported = filter_history(await captured_export(runtime, redact=False), ["legacy", mode, mode, mode, other_mode])
    next_frame = deepcopy(exported["frames"][-1])
    next_frame.update(
        sequence=6, session_epoch=next_frame["session_epoch"] + 1, captured_at=next_frame["captured_at"] + 1
    )
    row, label = next_frame["observations"][0], next_frame["truth"][0]
    row.update(stamp=1, observed=next_frame["captured_at"], rssi=-99, raw_distance=100, filtered_distance=6.7)
    row["quality"]["mode"] = mode if next_epoch_matching else other_mode
    label.update(observed=next_frame["captured_at"], ranges={row["scanner_id"]: 100})
    exported["frames"].append(next_frame)
    exported["session_epoch"] = next_frame["session_epoch"]
    await manager.async_dispatch("history_import", {"data": exported})
    before = replay_snapshot(runtime)
    report = await manager.async_dispatch("replay", {"candidate": {}, "filter_modes": {DEVICE: mode}})
    last = report["observations"][-1]
    assert report["counts"]["observations"] == 6 and report["counts"]["duplicates_ignored"] == 0
    assert last["normalized_source_s"] == 0 and last["stamp"] == 1
    assert last["candidate"] == pytest.approx(6.7 if next_epoch_matching else 100)
    assert last["candidate_pipeline"] == ("preserved_live_filter" if next_epoch_matching else f"replayed_{mode}_filter")
    assert replay_snapshot(runtime) == before


@pytest.mark.parametrize("ignored", ["stale", "duplicate", "backwards_stamp", "backwards_observed"])
def test_ignored_policy_metadata_cannot_reseed_a_uniform_candidate(ignored):
    captured = frames(count=3, distance=10)
    for index, frame in enumerate(captured):
        row, label = frame["observations"][0], frame["truth"][0]
        scanner = SCANNER if index % 2 else SCANNER.upper()
        row.update(scanner_id=scanner, filtered_distance=6.7)
        row["quality"]["mode"] = "pet"
        label["ranges"] = {scanner: 2}
    rejected = captured[1]["observations"][0]
    rejected["quality"]["mode"] = "stationary"
    if ignored == "stale":
        rejected["status"] = "stale"
    elif ignored == "backwards_observed":
        rejected["observed"] = 999
    else:
        rejected["stamp"] = 1000 if ignored == "duplicate" else 999
    original = deepcopy(captured)
    report = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": "pet"})
    assert report["counts"]["observations"] == 2 and report["counts"]["duplicates_ignored"] == 1
    assert [row["candidate"] for row in report["observations"]] == [6.7, 6.7]
    assert all(row["candidate_pipeline"] == "preserved_live_filter" for row in report["observations"])
    assert captured == original


@pytest.mark.parametrize("field", ["device_id", "scanner_id"])
def test_opaque_case_distinct_streams_classify_filter_changes_independently(field):
    captured = frames(count=5, distance=10)
    for index, frame in enumerate(captured):
        row, label = frame["observations"][0], frame["truth"][0]
        row.update(rssi=-59 if index == 0 else -79, raw_distance=1 if index == 0 else 10)
        row[field] = "Opaque:Case"
        row["quality"]["mode"] = "legacy" if index == 0 else "pet"
        alias = {**deepcopy(row), field: "opaque:case", "rssi": -79, "raw_distance": 10, "filtered_distance": 6.7}
        alias["quality"]["mode"] = "pet"
        label.update(device_id=row["device_id"], ranges={row["scanner_id"]: 2})
        frame["observations"] = [row, alias]
        if field == "device_id":
            frame["truth"].append({**deepcopy(label), "device_id": alias["device_id"]})
        else:
            label["ranges"][alias["scanner_id"]] = 2
    modes = {row["device_id"]: "pet", alias["device_id"]: "pet"}
    report = ReplayEngine().evaluate(captured, filter_modes=modes)
    changed = [row for row in report["observations"] if row[field] == "Opaque:Case"]
    unchanged = [row for row in report["observations"] if row[field] == "opaque:case"]
    assert [row["candidate"] for row in changed] == pytest.approx([1, 1, 1, 10, 10])
    assert all(row["candidate_pipeline"] == "replayed_pet_filter" for row in changed)
    assert [row["candidate"] for row in unchanged] == [6.7] * 5
    assert all(row["candidate_pipeline"] == "preserved_live_filter" for row in unchanged)
