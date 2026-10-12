"""Synthetic bounded diagnostic recording and import tests."""

from copy import deepcopy
import json

import pytest

from custom_components.bermuda.recording import DiagnosticHistory


def observation(t=1000.0, *, device="beacon-a", scanner="scanner-a", distance=3.0, stamp=None):
    return {
        "device_id": device,
        "scanner_id": scanner,
        "observed": t,
        "stamp": t if stamp is None else stamp,
        "rssi": -69.5,
        "rssi_offset": 0.0,
        "reference_power": -59.0,
        "attenuation": 2.0,
        "raw_distance": distance,
        "filtered_distance": distance,
        "age": 0.0,
        "status": "current",
        "quality": {},
        "model_version": "model-1",
    }


def truth(t=1000.0, *, device="beacon-a", location="site-a", distance=2.0, source="measured"):
    return {
        "source": source,
        "device_id": device,
        "location_id": location,
        "ranges": {"scanner-a": distance},
        "accuracy_m": 0.1,
        "observed": t,
        "independent": True,
    }


def record(history, row, labels=None):
    return history.record([row], captured_at=row["observed"], model_version="model-1", truth=labels)


def test_history_disabled_and_explicit_label_capture():
    history = DiagnosticHistory()
    assert record(history, observation()) == 0
    assert history.record([observation()], captured_at=1000, model_version="model-1", truth=[truth()], force=True) == 1
    assert history.enabled is False
    with pytest.raises(ValueError, match="requires independent truth"):
        history.record([observation(1001)], captured_at=1001, model_version="model-1", force=True)


def test_detached_selected_fresh_observations_and_dedup():
    history = DiagnosticHistory(enabled=True, selected_devices=["beacon-a"])
    row = observation()
    assert record(history, row, [truth()]) == 1
    row["quality"]["mutated"] = True
    assert record(history, observation(1001, stamp=1000)) == 0
    assert record(history, observation(1001, device="beacon-b")) == 0
    stale = observation(1001)
    stale["status"] = "stale"
    assert record(history, stale) == 0
    assert history.frames()[0]["observations"][0]["quality"] == {}
    returned = history.frames()
    returned[0]["truth"].clear()
    assert history.frames()[0]["truth"]
    assert record(history, observation(1002)) == 1
    assert history.frames()[-1]["truth"] == []


def test_clock_reset_requires_new_epoch_and_no_fictitious_repeats_after_eviction():
    history = DiagnosticHistory(enabled=True, max_frames=1)
    assert record(history, observation(1000, stamp=50)) == 1
    assert record(history, observation(1001, stamp=51)) == 1
    assert record(history, observation(1002, stamp=50)) == 0
    epoch = history.session_epoch
    history.start(clear=False)
    assert history.session_epoch != epoch
    assert record(history, observation(1002, stamp=1)) == 1


def test_time_frame_row_byte_and_clock_metadata_bounds():
    history = DiagnosticHistory(enabled=True, window_s=2, max_frames=2, max_rows=2, max_bytes=1500)
    for t in range(1000, 1010):
        record(history, observation(t))
    assert len(history.frames()) <= 2
    assert history.summary()["rows"] <= 2
    assert history.summary()["bytes"] <= 1500
    assert history.frames()[0]["captured_at"] >= 1007
    assert history.dropped_rows > 0
    record(history, observation(1010, device="b"))
    assert record(history, observation(1011, device="c")) == 0
    assert len(history._clocks) <= 2


def test_instant_truth_cannot_label_future_samples_fixed_truth_explicit():
    history = DiagnosticHistory(enabled=True)
    record(history, observation(), [truth()])
    record(history, observation(1001))
    assert not history.frames()[1]["truth"]
    with pytest.raises(ValueError, match="belong to this capture"):
        record(history, observation(1010), [truth()])
    assert record(history, observation(1010), [truth(source="fixed_reference")]) == 1
    with pytest.raises(ValueError, match="independent"):
        bad = truth(1011)
        bad["independent"] = False
        record(history, observation(1011), [bad])


def test_redacted_export_keeps_cross_links_and_never_retains_private_keys():
    history = DiagnosticHistory(enabled=True, selected_devices=["beacon-a"])
    row = observation()
    row["quality"] = {"IRK": "synthetic-secret", "nested": {"privateKey": "synthetic-private", "variance": 3}}
    record(history, row, [truth()])
    data = history.export()
    text = json.dumps(data)
    assert "beacon-a" not in text and "scanner-a" not in text and "site-a" not in text
    assert "synthetic-secret" not in text and "synthetic-private" not in text
    frame = data["frames"][0]
    assert frame["observations"][0]["device_id"] == frame["truth"][0]["device_id"] == data["selected_devices"][0]
    assert frame["observations"][0]["scanner_id"] in frame["truth"][0]["ranges"]
    assert history.export(redact=False)["frames"][0]["observations"][0]["device_id"] == "beacon-a"
    assert "synthetic-private" not in json.dumps(history.frames())


def test_import_is_atomic_bounded_and_creates_new_live_clock_epoch():
    history = DiagnosticHistory(enabled=True)
    record(history, observation(), [truth()])
    exported = history.export(redact=False)
    restored = DiagnosticHistory(enabled=True)
    restored.import_data(json.dumps(exported), now=1001)
    assert restored.frames() == history.frames()
    assert restored.session_epoch > history.session_epoch
    assert record(restored, observation(1001, stamp=1)) == 1
    original = restored.frames()
    with pytest.raises(ValueError, match="future"):
        restored.import_data(exported, now=999)
    assert restored.frames() == original


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(path="/tmp/arbitrary"),
        lambda data: data.update(session_epoch=True),
        lambda data: data["frames"][0]["observations"][0].update(rssi=float("nan")),
        lambda data: data["frames"][0]["observations"][0].update(stamp=-1),
        lambda data: data["frames"][0]["observations"][0].update(scanner_id=""),
        lambda data: data["frames"][0]["observations"][0].update(age=True),
        lambda data: data["frames"][0]["observations"][0].update(observed_clock="unknown_clock"),
        lambda data: data["frames"][0]["truth"][0].update(source="predicted_area"),
        lambda data: data["frames"][0]["truth"][0]["ranges"].update(**{"scanner-a": -1}),
    ],
)
def test_import_rejects_abuse(change):
    history = DiagnosticHistory(enabled=True)
    record(history, observation(), [truth()])
    data = deepcopy(history.export(redact=False))
    change(data)
    with pytest.raises(ValueError):
        DiagnosticHistory().import_data(data, now=1001)


def test_import_rejects_paths_nonfinite_nested_metadata_and_duplicate_source():
    history = DiagnosticHistory(enabled=True)
    record(history, observation())
    data = history.export(redact=False)
    with pytest.raises(ValueError, match="JSON data"):
        history.import_data("/tmp/history.json", now=1001)
    with pytest.raises(ValueError):
        history.import_data(json.dumps(data).replace('"rssi": -69.5', '"rssi": NaN'), now=1001)
    second = deepcopy(data["frames"][0])
    second["sequence"] = 2
    second["captured_at"] = 1001
    data["frames"].append(second)
    with pytest.raises(ValueError, match="duplicate"):
        history.import_data(data, now=1001)


def test_clearing_reset_on_reload_and_import_are_explicit():
    first = DiagnosticHistory(enabled=True)
    record(first, observation())
    assert DiagnosticHistory().frames() == []
    epoch = first.session_epoch
    first.clear()
    assert first.frames() == [] and first.session_epoch > epoch


def test_explicit_capture_moves_an_unlabelled_packet_without_duplicate_evidence():
    history = DiagnosticHistory(enabled=True)
    record(history, observation())
    row = observation()
    row["age"] = 1
    assert history.record([row], captured_at=1001, model_version="model-1", truth=[truth(1001)], force=True) == 1
    assert len(history.frames()) == 1
    assert history.frames()[0]["observations"][0]["stamp"] == 1000
    assert history.frames()[0]["truth"][0]["observed"] == 1001
    restored = DiagnosticHistory()
    restored.import_data(history.export(), now=1001)
    assert restored.summary()["rows"] == 1
    with pytest.raises(ValueError, match="already has independent truth"):
        history.record([row], captured_at=1002, model_version="model-1", truth=[truth(1002)], force=True)


def test_import_duplicate_json_keys_huge_numbers_and_deep_metadata_rejected():
    history = DiagnosticHistory(enabled=True)
    record(history, observation())
    serialized = json.dumps(history.export())
    with pytest.raises(ValueError, match="duplicate object keys"):
        history.import_data(
            serialized.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'), now=1001
        )
    bad = observation()
    bad["rssi"] = 10**1000
    with pytest.raises(ValueError, match="finite"):
        record(history, bad)
    bad = observation(1001)
    nested = {}
    for _ in range(10):
        nested = {"nested": nested}
    bad["quality"] = nested
    with pytest.raises(ValueError, match="deeply nested"):
        record(history, bad)


def test_full_redacted_export_round_trips_under_byte_budget():
    history = DiagnosticHistory(enabled=True, max_bytes=1500)
    for index in range(10):
        record(history, observation(1000 + index), [truth(1000 + index)])
    assert len(json.dumps(history.export(), separators=(",", ":")).encode()) <= 1500
    restored = DiagnosticHistory(max_bytes=1500)
    restored.import_data(history.export(), now=1010)
    assert restored.summary()["rows"] == history.summary()["rows"]


def test_explicit_capture_bypasses_selection_only_for_its_independently_labelled_pairs():
    history = DiagnosticHistory(selected_devices=["beacon-a"])
    rows = [
        observation(device="beacon-b"),
        observation(device="beacon-c"),
        observation(device="beacon-b", scanner="scanner-unlabelled"),
    ]
    assert (
        history.record(rows, captured_at=1000, model_version="model-1", truth=[truth(device="beacon-b")], force=True)
        == 1
    )
    assert history.selected_devices == ["beacon-a"]
    assert history.frames()[0]["observations"][0]["device_id"] == "beacon-b"
    restored = DiagnosticHistory(selected_devices=["beacon-a"])
    restored.import_data(history.export(), now=1001)
    assert restored.summary()["rows"] == 1
    with pytest.raises(ValueError, match="already has independent truth"):
        history.record(rows, captured_at=1000, model_version="model-1", truth=[truth(device="beacon-b")], force=True)


@pytest.mark.parametrize("force", [False, True])
def test_staggered_fixed_references_label_only_sources_surviving_dedup(force):
    history = DiagnosticHistory(enabled=not force)
    first = [observation(device="beacon-a"), observation(device="beacon-b")]
    labels = [truth(device=device, source="fixed_reference") for device in ("beacon-a", "beacon-b")]
    assert history.record(first, captured_at=1000, model_version="model-1", truth=labels, force=force) == 2
    second = [observation(device="beacon-a"), observation(1001, device="beacon-b")]
    second[0]["age"] = 1
    labels = [truth(1001, device=device, source="fixed_reference") for device in ("beacon-a", "beacon-b")]
    assert history.record(second, captured_at=1001, model_version="model-1", truth=labels, force=force) == 1
    frame = history.frames()[-1]
    assert [row["device_id"] for row in frame["observations"]] == ["beacon-b"]
    assert [label["device_id"] for label in frame["truth"]] == ["beacon-b"]
    assert history.summary()["rows"] == 3
    assert history.record(second, captured_at=1001, model_version="model-1", truth=labels, force=force) == 0


def test_trusted_same_host_resume_restores_deduplication_and_only_current_epoch_clocks():
    history = DiagnosticHistory(enabled=True)
    initial = [observation(stamp=100), observation(device="beacon-b", stamp=200)]
    assert history.record(initial, captured_at=1000, model_version="model-1") == 2
    resumed = DiagnosticHistory(enabled=True)
    resumed.import_data(history.export(redact=False), now=1001, resume=True)
    assert resumed.session_epoch == history.session_epoch
    assert record(resumed, observation(1001, stamp=100)) == 0
    assert record(resumed, observation(1001, stamp=99)) == 0
    assert record(resumed, observation(1001, stamp=101)) == 1
    history.start(clear=False)
    assert record(history, observation(1001, stamp=10)) == 1
    resumed.import_data(history.export(redact=False), now=1002, resume=True)
    assert record(resumed, observation(1002, stamp=9)) == 0
    assert record(resumed, observation(1002, device="beacon-b", stamp=1)) == 1
    assert record(resumed, observation(1002, stamp=11)) == 1
    # A deliberate new live session permits a genuine lower source clock.
    resumed.start(clear=False)
    assert record(resumed, observation(1003, stamp=1)) == 1


@pytest.mark.parametrize("resume", [None, "true", 1, 0])
def test_import_resume_flag_is_strict_and_failed_validation_is_atomic(resume):
    history = DiagnosticHistory(enabled=True)
    record(history, observation())
    original = history.frames()
    with pytest.raises(ValueError, match="resume must be a boolean"):
        history.import_data(history.export(redact=False), now=1001, resume=resume)
    assert history.frames() == original


def test_shrinking_a_stopped_history_window_retains_latest_valid_reloadable_evidence():
    history = DiagnosticHistory(enabled=True, window_s=10)
    for index in range(6):
        record(history, observation(1000 + index), [truth(1000 + index)])
    history.stop()
    epoch = history.session_epoch
    history.reconfigure(window_s=3, now=1005)
    assert history.enabled is False and history.session_epoch == epoch
    assert [frame["captured_at"] for frame in history.frames()] == [1002, 1003, 1004, 1005]
    assert history.summary()["rows"] == 4
    assert history.dropped_rows == 2
    assert history.frames()[-1]["truth"][0]["observed"] == 1005
    resumed = DiagnosticHistory(window_s=3, enabled=True)
    resumed.import_data(history.export(redact=False), now=1005, resume=True)
    assert resumed.frames() == history.frames()
    assert record(resumed, observation(1006, stamp=1005)) == 0
    history.reconfigure(window_s=3, now=1010)
    assert history.frames() == [] and history.summary()["rows"] == 0


@pytest.mark.parametrize("window,now", [(True, 1005), (0, 1005), (86401, 1005), (3, float("nan")), (3, 999)])
def test_history_window_reconfiguration_validates_before_mutating(window, now):
    history = DiagnosticHistory(enabled=True, window_s=10)
    record(history, observation())
    original = history.frames()
    with pytest.raises(ValueError):
        history.reconfigure(window_s=window, now=now)
    assert history.window_s == 10 and history.frames() == original


def test_local_clock_watermarks_preserve_evicted_current_source_dedup_on_same_host_reload():
    history = DiagnosticHistory(enabled=True, max_frames=1)
    record(history, observation(device="beacon-a"))
    record(history, observation(1001, device="beacon-b"))
    assert history.frames()[0]["observations"][0]["device_id"] == "beacon-b"
    cached = observation(device="beacon-a")
    cached["age"] = 2
    assert history.record([cached], captured_at=1002, model_version="model-1") == 0
    markers = history.export_clocks()
    assert len(markers["clocks"]) == 2
    restored = DiagnosticHistory(enabled=True, max_frames=1)
    restored.import_data(history.export(redact=False), now=1002, resume=True)
    restored.restore_clocks(markers, now=1002)
    assert restored.export_clocks() == markers
    assert restored.record([cached], captured_at=1002, model_version="model-1") == 0
    assert record(restored, observation(1003, device="beacon-a")) == 1


def test_clock_watermarks_are_detached_bounded_local_metadata_not_public_history_fields():
    history = DiagnosticHistory(enabled=True, max_rows=2)
    record(history, observation())
    markers = history.export_clocks()
    assert set(markers) == {"session_epoch", "clocks"}
    assert len(markers["clocks"]) <= history.max_rows
    markers["clocks"][0][2] = 999
    assert history.export_clocks()["clocks"][0][2] == 1000
    for redact in (False, True):
        public = history.export(redact=redact)
        assert set(public) == {"schema_version", "session_epoch", "selected_devices", "redacted", "frames"}
        assert "clocks" not in public and "history_clocks" not in public
        with pytest.raises(ValueError, match="unsupported history import fields"):
            history.import_data({**public, "history_clocks": history.export_clocks()}, now=1001)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(path="/tmp/clocks.json"),
        lambda data: data.update(session_epoch=True),
        lambda data: data.update(session_epoch=data["session_epoch"] + 1),
        lambda data: data.update(clocks="/tmp/clocks.json"),
        lambda data: data["clocks"].append(data["clocks"][0].copy()),
        lambda data: data["clocks"][0].append("extra"),
        lambda data: data["clocks"][0].__setitem__(0, ""),
        lambda data: data["clocks"][0].__setitem__(1, None),
        lambda data: data["clocks"][0].__setitem__(2, True),
        lambda data: data["clocks"][0].__setitem__(2, float("nan")),
        lambda data: data["clocks"][0].__setitem__(2, -1),
        lambda data: data["clocks"][0].__setitem__(2, 1e13),
        lambda data: data["clocks"][0].__setitem__(2, 999),
        lambda data: data["clocks"][0].__setitem__(3, float("inf")),
        lambda data: data["clocks"][0].__setitem__(3, 1002),
        lambda data: data["clocks"][0].__setitem__(3, 999),
    ],
)
def test_local_clock_restore_rejects_malformed_future_or_backward_markers_atomically(change):
    history = DiagnosticHistory(enabled=True)
    record(history, observation())
    original = history.export_clocks()
    malformed = deepcopy(original)
    change(malformed)
    with pytest.raises(ValueError):
        history.restore_clocks(malformed, now=1001)
    assert history.export_clocks() == original
    assert record(history, observation(1001, stamp=1000)) == 0


def test_local_clock_restore_never_drops_resumed_watermarks_and_caps_merged_sources():
    history = DiagnosticHistory(enabled=True, max_rows=2)
    record(history, observation())
    epoch = history.session_epoch
    history.restore_clocks({"session_epoch": epoch, "clocks": []}, now=1001)
    assert record(history, observation(1001, stamp=1000)) == 0
    history.restore_clocks({"session_epoch": epoch, "clocks": [["beacon-b", "scanner-a", 1001, 1001]]}, now=1001)
    original = history.export_clocks()
    with pytest.raises(ValueError, match="row budget"):
        history.restore_clocks({"session_epoch": epoch, "clocks": [["beacon-c", "scanner-a", 1001, 1001]]}, now=1001)
    assert history.export_clocks() == original
    with pytest.raises(ValueError, match="row budget"):
        history.restore_clocks(
            {"session_epoch": epoch, "clocks": [["a", "s", 1, 1], ["b", "s", 1, 1], ["c", "s", 1, 1]]}, now=1001
        )
