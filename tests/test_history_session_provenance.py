"""Trusted live watermarks survive append/import without trusting foreign clocks."""

from copy import deepcopy

import pytest

from custom_components.bermuda.recording import DiagnosticHistory, MAX_CLOCK_EPOCH
from custom_components.bermuda.calibration import CalibrationEngine, range_from_rssi
from custom_components.bermuda.recording import OBSERVATION_FIELDS
from .test_calibration_physical_targets import aliases, physical_observations
from .test_diagnostic_history import observation, record, truth


def imported(previous, rows=(), *, now=1002, payload=None):
    replacement = DiagnosticHistory(enabled=True, max_rows=previous.max_rows)
    replacement.import_data(payload or previous.export(redact=False), now=now)
    replacement.retain_live_sources(previous, list(rows), now=now)
    return replacement


def captured(history, row, *, label=None):
    return history.record(
        [row], captured_at=row["observed"], model_version="model-1", truth=[label or truth(row["observed"])], force=True
    )


def test_append_resumes_epoch_watermarks_and_allows_one_unlabelled_cached_relocation():
    history = DiagnosticHistory(enabled=True)
    assert record(history, observation(stamp=10)) == 1
    epoch, clocks = history.session_epoch, history.export_clocks()
    history.stop()
    history.start(clear=False)
    assert history.enabled and history.session_epoch == epoch
    assert history.export_clocks() == clocks
    assert record(history, observation(1001, stamp=10)) == 0
    assert captured(history, observation(1001, stamp=10)) == 1
    assert history.summary()["rows"] == 1
    with pytest.raises(ValueError, match="already has independent truth"):
        captured(history, observation(1002, stamp=10))
    assert captured(history, observation(1002, stamp=11)) == 1


def test_append_keeps_evicted_packet_guard_and_selection_changes_atomic():
    history = DiagnosticHistory(enabled=True, max_frames=1, selected_devices=["beacon-a"])
    record(history, observation(stamp=10))
    record(history, observation(1001, stamp=11, device="other"))
    history.reconfigure(window_s=1, now=1002)
    assert history.frames() == []
    before = deepcopy(vars(history))
    with pytest.raises(ValueError, match="changing the selected devices"):
        history.start(clear=False, selected_devices=["other"])
    assert vars(history) == before
    history.start(clear=False)
    with pytest.raises(ValueError, match="evicted source"):
        captured(history, observation(1002, stamp=10))


@pytest.mark.parametrize("alias", ["beacon-a", "ibeacon:logical", "AA:BB:CC:DD:EE:01"])
@pytest.mark.parametrize("source_case", ["aa:bb:cc:dd:ee:01", "AA:BB:CC:DD:EE:01"])
def test_import_boundary_blocks_same_physical_packet_under_mac_and_logical_aliases(alias, source_case):
    history = DiagnosticHistory(enabled=True)
    row = {**observation(stamp=10), "source_id": "aa:bb:cc:dd:ee:01"}
    assert record(history, row) == 1
    replacement = imported(history)
    candidate = {**observation(1002, device=alias, stamp=10), "source_id": source_case}
    label = truth(1002, device=alias)
    before = replacement.export(redact=False), replacement.export_clocks()
    assert captured(replacement, candidate, label=label) == 0
    assert (replacement.export(redact=False), replacement.export_clocks()) == before
    candidate["stamp"] = 11
    assert captured(replacement, candidate, label=label) == 1


@pytest.mark.parametrize("status", ["current", "stale", "missing"])
def test_snapshot_boundary_accepts_clock_metadata_without_promoting_quality_or_truth(status):
    history = DiagnosticHistory()
    candidate = {**observation(stamp=10), "source_id": "physical-a", "status": status, "raw_distance": None}
    replacement = imported(history, [candidate])
    assert replacement.frames() == []
    assert record(replacement, {**observation(1002, stamp=10), "source_id": "physical-a"}) == 0
    assert record(replacement, {**observation(1002, stamp=11), "source_id": "physical-a"}) == 1


@pytest.mark.parametrize("age", [None, -1.0, float("nan"), float("inf"), True])
def test_unusable_snapshot_source_clock_cannot_poison_future_live_watermarks(age):
    history = DiagnosticHistory(enabled=True)
    bad = {**observation(stamp=1e10), "source_id": "physical-a", "age": age, "status": "stale"}
    replacement = imported(history, [bad])
    assert replacement.export_clocks()["source_clocks"] == []
    assert record(replacement, {**observation(1002, stamp=10), "source_id": "physical-a"}) == 1


def test_foreign_pseudonymous_and_empty_imports_never_seed_live_clock_watermarks():
    foreign = DiagnosticHistory(enabled=True)
    record(foreign, {**observation(stamp=999999), "source_id": "physical-a"})
    for payload in (foreign.export(redact=False), foreign.export(), DiagnosticHistory().export()):
        live = DiagnosticHistory(enabled=True)
        replacement = imported(live, [{**observation(stamp=10), "source_id": "physical-a"}], payload=payload)
        assert record(replacement, {**observation(1002, stamp=10), "source_id": "physical-a"}) == 0
        assert record(replacement, {**observation(1002, stamp=11), "source_id": "physical-a"}) == 1
        assert replacement._source_clocks[("physical-a", "scanner-a")][0] == 11


def test_evicted_source_watermarks_and_boundaries_survive_import_and_local_reload():
    history = DiagnosticHistory(enabled=True, max_frames=1)
    record(history, {**observation(stamp=10), "source_id": "physical-a"})
    record(history, {**observation(1001, device="other", stamp=20), "source_id": "physical-b"})
    assert len(history.frames()) == 1
    replacement = imported(history, payload=DiagnosticHistory().export())
    assert record(replacement, {**observation(1002, stamp=10), "source_id": "physical-a"}) == 0
    resumed = DiagnosticHistory(enabled=True)
    resumed.import_data(replacement.export(redact=False), now=1002, resume=True)
    resumed.restore_clocks(replacement.export_clocks(), now=1002)
    assert resumed.export_clocks() == replacement.export_clocks()
    assert record(resumed, {**observation(1002, stamp=10), "source_id": "physical-a"}) == 0
    assert record(resumed, {**observation(1002, stamp=11), "source_id": "physical-a"}) == 1


def test_repeated_imports_cannot_move_trusted_watermarks_or_epochs_backwards():
    history = DiagnosticHistory(enabled=True)
    captured(history, observation(stamp=10))
    old = history.export(redact=False)
    for _ in range(3):
        previous_epoch = history.session_epoch
        history = imported(history, payload=old)
        assert history.session_epoch == previous_epoch + 1
        assert captured(history, observation(1002, stamp=10)) == 0
        assert history.summary()["rows"] == 1


def test_confirmed_host_clock_reset_accepts_lower_actual_source_clock_in_new_epoch():
    history = DiagnosticHistory(enabled=True)
    captured(history, observation(stamp=10))
    replacement = imported(history)
    epoch = replacement.session_epoch
    replacement.new_source_session()
    assert replacement.session_epoch == epoch + 1
    assert replacement.export_clocks()["source_clocks"] == []
    assert replacement.export_clocks()["live_boundaries"] == []
    assert captured(replacement, observation(1002, stamp=1)) == 1
    assert [frame["session_epoch"] for frame in replacement.frames()] == [1, epoch + 1]


def test_physical_source_and_boundary_watermark_limits_are_bounded_and_atomic():
    history = DiagnosticHistory(enabled=True, max_rows=1)
    assert record(history, {**observation(stamp=10), "source_id": "physical-a"}) == 1
    assert record(history, {**observation(1001, stamp=11), "source_id": "physical-b"}) == 0
    replacement = DiagnosticHistory(max_rows=1)
    replacement.import_data(DiagnosticHistory().export(), now=1002)
    before = deepcopy(vars(replacement))
    with pytest.raises(ValueError, match="row budget"):
        replacement.retain_live_sources(history, [{**observation(stamp=10), "source_id": "physical-b"}], now=1002)
    assert vars(replacement) == before


@pytest.mark.parametrize("field", ["source_clocks", "live_boundaries"])
@pytest.mark.parametrize("change", ["future", "backwards", "case-duplicate", "extra", "uncovered"])
def test_invalid_local_provenance_metadata_is_rejected_atomically(field, change):
    history = DiagnosticHistory(enabled=True)
    row = {**observation(stamp=10), "source_id": "aa:bb:cc:dd:ee:01"}
    record(history, row)
    replacement = imported(history)
    markers = replacement.export_clocks()
    malformed = deepcopy(markers)
    if change == "future":
        malformed[field][0][3] = 1003
    elif change == "backwards":
        malformed[field][0][2] = 9
    elif change == "case-duplicate":
        malformed[field].append([*malformed[field][0]])
        malformed[field][-1][0] = malformed[field][-1][0].upper()
    elif change == "extra":
        malformed[field][0].append("unexpected")
    else:
        malformed[field][0][2] = 11
        if field == "source_clocks":
            malformed["live_boundaries"][0][2] = 12
    with pytest.raises(ValueError):
        replacement.restore_clocks(malformed, now=1002)
    assert replacement.export_clocks() == markers


def test_maximum_epoch_is_reloadable_and_exhausted_transitions_are_atomic():
    payload = DiagnosticHistory().export()
    payload["session_epoch"] = MAX_CLOCK_EPOCH - 1
    history = DiagnosticHistory(enabled=True)
    history.import_data(payload, now=1000)
    assert history.session_epoch == MAX_CLOCK_EPOCH
    captured(history, observation(stamp=10))
    resumed = DiagnosticHistory()
    resumed.import_data(history.export(redact=False), now=1001, resume=True)
    resumed.restore_clocks(history.export_clocks(), now=1001)
    assert resumed.frames() == history.frames()
    before = deepcopy(vars(history))
    for transition in (
        history.clear,
        history.new_source_session,
        lambda: history.start(clear=True, selected_devices=["other"]),
    ):
        with pytest.raises(ValueError, match="epoch exhausted"):
            transition()
        assert vars(history) == before
    history.start(clear=False)
    assert history.session_epoch == MAX_CLOCK_EPOCH
    assert captured(history, observation(1001, stamp=11)) == 1
    replacement = DiagnosticHistory()
    replacement.import_data(DiagnosticHistory().export(), now=1001)
    detached = deepcopy(vars(replacement))
    with pytest.raises(ValueError, match="session_epoch"):
        replacement.retain_live_sources(history, [], now=1001)
    assert vars(replacement) == detached
    assert replacement.export()["frames"] == []


def test_confirmed_reboot_compacts_only_epoch_numbers_and_keeps_alias_packet_identity():
    history = DiagnosticHistory(enabled=True)
    for epoch in (MAX_CLOCK_EPOCH - 2, MAX_CLOCK_EPOCH):
        history.session_epoch = epoch
        history._clocks.clear()
        assert captured(history, observation(1000 if epoch < MAX_CLOCK_EPOCH else 1001, stamp=10)) == 1
    frames = history.frames()
    assert history.new_source_session(compact=True)
    assert history.session_epoch == 3
    rebased = history.frames()
    assert [frame["session_epoch"] for frame in rebased] == [1, 2]
    for before, after in zip(frames, rebased, strict=True):
        assert {key: value for key, value in before.items() if key != "session_epoch"} == {
            key: value for key, value in after.items() if key != "session_epoch"
        }
    assert history.export_clocks()["clocks"] == []
    assert captured(history, observation(1002, stamp=1)) == 1
    trusted = DiagnosticHistory()
    trusted.import_data(history.export(redact=False), now=1002, resume=True)
    assert trusted.frames() == history.frames()


def test_epoch_compaction_preserves_physical_alias_deduplication_and_conflicts():
    evidence = aliases(physical_observations(), count=1)
    conflict = {**evidence[0], "device_id": "conflicting-alias", "rssi": evidence[0]["rssi"] + 0.1}
    evidence.append(conflict)
    history = DiagnosticHistory(enabled=True)
    for epoch in (MAX_CLOCK_EPOCH - 2, MAX_CLOCK_EPOCH):
        history.session_epoch = epoch
        history._clocks.clear()
        for stamp in sorted({row["stamp"] for row in evidence}):
            rows, labels = [], {}
            for row in evidence:
                if row["stamp"] != stamp:
                    continue
                normalized = {key: value for key, value in row.items() if key in OBSERVATION_FIELDS}
                distance = range_from_rssi(row["rssi"], row["reference_power"], row["attenuation"])
                normalized.update(observed=99.0, age=1.0, raw_distance=distance, filtered_distance=distance)
                rows.append(normalized)
                label = labels.setdefault(
                    row["device_id"],
                    {
                        "source": "measured",
                        "device_id": row["device_id"],
                        "location_id": row["location_id"],
                        "ranges": {},
                        "accuracy_m": 0.0,
                        "observed": 100.0,
                        "independent": True,
                    },
                )
                label["ranges"][row["scanner_id"]] = row["expected_distance"]
            assert history.record(
                rows, captured_at=100.0, model_version="model-one", truth=list(labels.values())
            ) == len(rows)

    def labelled():
        rows = []
        for frame in history.frames():
            for row in frame["observations"]:
                label = next(label for label in frame["truth"] if label["device_id"] == row["device_id"])
                rows.append(
                    {
                        **row,
                        "session_epoch": frame["session_epoch"],
                        "location_id": label["location_id"],
                        "expected_distance": label["ranges"][row["scanner_id"]],
                        "truth_source": "measured",
                        "independent": True,
                    }
                )
        return rows

    before = CalibrationEngine().preview(labelled(), now=100.0, model_version="model-one")
    history.new_source_session(compact=True)
    after = CalibrationEngine().preview(labelled(), now=100.0, model_version="model-one")
    assert before["eligible"] and after["eligible"]
    assert after["coverage"] == before["coverage"]
    assert after["metrics"] == before["metrics"]
    assert after["offsets"] == before["offsets"]
    assert after["rejected"] == before["rejected"]
    assert after["rejected"]["conflicting_source_packet"] == 6
    assert after["rejected"]["duplicate_source_stamp"] == 62
