"""Synthetic public history round trips preserve receiver and source identity."""

from copy import deepcopy
import json
import time

import pytest

from custom_components.bermuda.recording import DiagnosticHistory
from custom_components.bermuda.replay import ShadowSession

from .test_diagnostic_history import observation, wire_bytes
from .test_toolkit_detached_replay import captured_export
from .test_toolkit_runtime import DEVICE, SCANNER, runtime


async def test_redacted_round_trip_keeps_mac_receiver_alias_range_truth(runtime):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    row = exported["frames"][0]["observations"][0]
    exported["frames"][0]["observations"].append(
        {**deepcopy(row), "scanner_id": SCANNER.upper(), "stamp": row["stamp"] + 1}
    )
    exported["selected_devices"] = [row["device_id"]]
    await manager.async_dispatch("history_import", {"data": exported})
    before = await manager.async_dispatch("replay", {"candidate": {}})
    assert before["counts"]["labelled"] == 2
    history, clocks = manager.history, manager.history.export_clocks()
    original, summary = manager.history.frames(), manager.history.summary()
    saved = manager.store.async_save.call_count
    redacted = await manager.async_dispatch("history_export", {})
    assert await manager.async_dispatch("history_export", {}) == redacted
    raw = await manager.async_dispatch("history_export", {"redact": False})
    assert raw["frames"] == exported["frames"] and raw["selected_devices"] == exported["selected_devices"]
    assert manager.history is history and history.frames() == original
    assert history.summary() == summary and history.export_clocks() == clocks
    assert manager.store.async_save.call_count == saved
    frame = redacted["frames"][0]
    scanners = {row["scanner_id"] for row in frame["observations"]}
    assert scanners == set(frame["truth"][0]["ranges"])
    assert redacted["selected_devices"] == [frame["truth"][0]["device_id"]]
    assert all(
        identifier not in json.dumps(redacted) for identifier in (DEVICE, SCANNER, SCANNER.upper(), "measured-site")
    )
    await manager.async_dispatch("history_import", {"data": redacted})
    after = await manager.async_dispatch("replay", {"candidate": {}})
    assert after["counts"] == before["counts"]
    assert [row["expected_distance"] for row in after["observations"]] == [1, 1]


@pytest.mark.parametrize("source_present", [True, False])
async def test_redacted_source_aliases_keep_physical_evidence_below_shadow_threshold(runtime, source_present):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    template = exported["frames"][0]
    base_time = time.time() - 4
    devices = [DEVICE.upper(), "ibeacon:SyntheticAlias"]
    exported["selected_devices"] = devices
    exported["frames"] = []
    for index in range(4):
        frame = deepcopy(template)
        frame.update(sequence=index + 1, captured_at=base_time + index)
        original, label = frame["observations"][0], frame["truth"][0]
        original.update(device_id=devices[0], observed=frame["captured_at"], stamp=original["stamp"] + index)
        if source_present:
            original["source_id"] = DEVICE.upper()
        else:
            original.pop("source_id", None)
        alias = {**deepcopy(original), "device_id": devices[1], "source_id": DEVICE, "scanner_id": SCANNER.upper()}
        label.update(device_id=devices[0], observed=frame["captured_at"], location_id=f"synthetic-site-{index % 2}")
        frame["observations"] = [original, alias]
        frame["truth"] = [label, {**deepcopy(label), "device_id": devices[1], "ranges": {SCANNER.upper(): 1}}]
        exported["frames"].append(frame)
    await manager.async_dispatch("history_import", {"data": exported})
    before = await manager.async_dispatch("replay", {"candidate": {SCANNER: 20}})

    def shadow_check(frames, scanner):
        shadow = ShadowSession([], model_version=manager.version(), proposed_offsets={scanner: 20})
        for frame in frames:
            assert shadow.add_frame(frame) == 2
        check = shadow.promotion_check(current_model_version=manager.version())
        assert check["new_labelled_rows"] == 4
        assert check["new_data_range_metrics"]["candidate"]["count"] == 8
        assert check["new_data_range_metrics"]["candidate"]["mae_m"] == pytest.approx(0)
        assert check["eligible"] is False
        assert "insufficient new independently labelled observations" in check["reasons"]
        return check

    shadow_check(manager.history.frames(), SCANNER)
    raw = await manager.async_dispatch("history_export", {"redact": False})
    assert ("source_id" in raw["frames"][0]["observations"][0]) is source_present
    assert raw["frames"][0]["observations"][0]["device_id"] == DEVICE.upper()
    redacted = await manager.async_dispatch("history_export", {})
    first, alias = redacted["frames"][0]["observations"]
    assert first["device_id"] != alias["device_id"]
    assert first["scanner_id"] == alias["scanner_id"]
    assert first["source_id"] == alias["source_id"]
    assert first["source_id"] != first["device_id"]
    await manager.async_dispatch("history_import", {"data": redacted})
    after = await manager.async_dispatch("replay", {"candidate": {first["scanner_id"]: 20}})
    assert after["counts"] == before["counts"]
    assert after["range_metrics"] == before["range_metrics"]
    shadow_check(manager.history.frames(), first["scanner_id"])


@pytest.mark.parametrize("scanner", ["receiver:CaseSensitive", "aa:bb:cc:dd:ee:gg"])
async def test_redaction_keeps_opaque_sources_receivers_and_logical_mac_ids_exact(runtime, scanner):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    frame = exported["frames"][0]
    row, label = frame["observations"][0], frame["truth"][0]
    row.update(scanner_id=scanner, source_id="source:CaseSensitive")
    row["quality"] = {"IRK": "synthetic-private", "nested": {"privateKey": "synthetic-key"}}
    label.update(ranges={scanner: 1}, location_id=DEVICE)
    frame["observations"].append(
        {
            **deepcopy(row),
            "device_id": DEVICE.upper(),
            "scanner_id": scanner.upper(),
            "source_id": "source:casesensitive",
        }
    )
    frame["truth"].append(
        {**deepcopy(label), "device_id": DEVICE.upper(), "ranges": {scanner.upper(): 2}, "location_id": DEVICE.upper()}
    )
    exported["selected_devices"] = [DEVICE, DEVICE.upper()]
    await manager.async_dispatch("history_import", {"data": exported})
    before = await manager.async_dispatch("replay", {"candidate": {}})
    redacted = await manager.async_dispatch("history_export", {})
    redacted_frame = redacted["frames"][0]
    for field in ("device_id", "scanner_id", "source_id"):
        assert len({row[field] for row in redacted_frame["observations"]}) == 2
    assert len({label["location_id"] for label in redacted_frame["truth"]}) == 2
    assert set(redacted["selected_devices"]) == {row["device_id"] for row in redacted_frame["observations"]}
    for row, label in zip(redacted_frame["observations"], redacted_frame["truth"], strict=True):
        assert row["device_id"] == label["device_id"]
        assert row["scanner_id"] in label["ranges"]
    text = json.dumps(redacted)
    assert all(
        identifier not in text
        for identifier in (
            DEVICE,
            DEVICE.upper(),
            scanner,
            scanner.upper(),
            "source:",
            "synthetic-private",
            "synthetic-key",
        )
    )
    await manager.async_dispatch("history_import", {"data": redacted})
    after = await manager.async_dispatch("replay", {"candidate": {}})
    assert after["counts"] == before["counts"]
    assert [row["expected_distance"] for row in after["observations"]] == [1, 2]


@pytest.mark.parametrize("distance", [1, 2])
@pytest.mark.parametrize("uppercase_first", [True, False])
async def test_mac_truth_alias_collision_rejects_import_atomically(runtime, distance, uppercase_first):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    await manager.async_dispatch("shadow_start", {"candidate": {}})
    history, shadow = manager.history, manager.shadow
    original, clocks = history.export(redact=False), history.export_clocks()
    options, quality = deepcopy(dict(runtime.entry.options)), deepcopy(manager._quality)
    saved = manager.store.async_save.call_count
    ranges = [(SCANNER, 1), (SCANNER.upper(), distance)]
    exported["frames"][0]["truth"][0]["ranges"] = dict(reversed(ranges) if uppercase_first else ranges)
    with pytest.raises(ValueError, match="truth ranges contain MAC case aliases that cannot be redacted"):
        await manager.async_dispatch("history_import", {"data": exported})
    assert manager.history is history and history.export(redact=False) == original
    assert history.export_clocks() == clocks
    assert manager.shadow is shadow and shadow.state == "active"
    assert dict(runtime.entry.options) == options and manager._quality == quality
    assert manager.store.async_save.call_count == saved


@pytest.mark.parametrize("stamp_delta", [0, -1])
@pytest.mark.parametrize("separate_frames", [True, False])
async def test_receiver_mac_alias_clock_collision_rejects_import_atomically(runtime, stamp_delta, separate_frames):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    history = manager.history
    original, clocks = history.export(redact=False), history.export_clocks()
    saved = manager.store.async_save.call_count
    row = exported["frames"][0]["observations"][0]
    alias = {**deepcopy(row), "scanner_id": SCANNER.upper(), "stamp": row["stamp"] + stamp_delta}
    if separate_frames:
        frame = {**deepcopy(exported["frames"][0]), "sequence": 2, "observations": [alias], "truth": []}
        exported["frames"].append(frame)
    else:
        exported["frames"][0]["observations"].append(alias)
    with pytest.raises(ValueError, match="receiver clocks after MAC normalization"):
        await manager.async_dispatch("history_import", {"data": exported})
    assert manager.history is history and history.export(redact=False) == original
    assert history.export_clocks() == clocks
    assert manager.store.async_save.call_count == saved


async def test_legacy_receiver_clock_collision_keeps_raw_export_and_rejects_redaction_without_mutation(runtime):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    frame = deepcopy(exported["frames"][0])
    row, label = frame["observations"][0], frame["truth"][0]
    row["stamp"] += 1
    alias = {**deepcopy(row), "scanner_id": SCANNER.upper()}
    assert (
        manager.history.record(
            [row, alias],
            captured_at=frame["captured_at"],
            model_version=frame["model_version"],
            truth=[label],
            force=True,
        )
        == 2
    )
    before, clocks = manager.history.export(redact=False), manager.history.export_clocks()
    saved = manager.store.async_save.call_count
    assert await manager.async_dispatch("history_export", {"redact": False}) == before
    with pytest.raises(ValueError, match="receiver clocks after MAC normalization"):
        await manager.async_dispatch("history_export", {})
    assert manager.history.export(redact=False) == before and manager.history.export_clocks() == clocks
    assert manager.store.async_save.call_count == saved


def test_added_legacy_source_link_counts_toward_import_budget_without_replacing_history():
    producer = DiagnosticHistory(enabled=True)
    row = observation(device=DEVICE.upper(), scanner=SCANNER)
    assert producer.record([row], captured_at=1000, model_version="model-1") == 1
    raw, redacted = producer.export(redact=False), producer.export()
    assert "source_id" not in raw["frames"][0]["observations"][0]
    assert "source_id" in redacted["frames"][0]["observations"][0]
    raw_size, redacted_size = len(wire_bytes(raw)), len(wire_bytes(redacted))
    assert raw_size < redacted_size
    history = DiagnosticHistory(enabled=True, max_bytes=max(512, redacted_size - 1))
    assert history.record([observation()], captured_at=1000, model_version="model-1") == 1
    before, clocks = history.export(redact=False), history.export_clocks()
    assert raw_size <= history.max_bytes < redacted_size
    with pytest.raises(ValueError, match="normalized import exceeds the history byte budget"):
        history.import_data(raw, now=1001)
    assert history.export(redact=False) == before
    assert history.export_clocks() == clocks
