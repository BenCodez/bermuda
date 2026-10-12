"""Synthetic independently labelled receiver snapshots for area fingerprints."""

import copy
import json

import pytest

from custom_components.bermuda import fingerprints
from custom_components.bermuda.fingerprints import FingerprintModel


DEVICE = "opaque:ibeacon:synthetic-identity"
ROOM_A = {"scanner:one": -45, "scanner:two": -80}
ROOM_B = {"scanner:one": -80, "scanner:two": -45}


def snapshot(stamp, vector=ROOM_A, *, device=DEVICE, version="synthetic-v1"):
    return [
        {
            "device_id": device,
            "scanner_id": scanner,
            "stamp": stamp,
            "observed": 1000 + stamp,
            "rssi": rssi,
            "age": 0,
            "status": "current",
            "model_version": version,
        }
        for scanner, rssi in vector.items()
    ]


def train(model, location="area:a", vector=ROOM_A, *, start=0, device=DEVICE, count=5):
    return model.train(
        device,
        location,
        [snapshot(stamp, vector, device=device) for stamp in range(start, start + count)],
        independent=True,
        now=1000 + start + count,
    )


def codes(result):
    return {reason["code"] for reason in result["reasons"]}


def test_explicit_independent_labels_train_per_device_and_suggest_without_self_training():
    model = FingerprintModel()
    assert train(model)["ready"]
    assert train(model, "area:b", ROOM_B, start=10)["ready"]
    original = model.export_state()
    live = snapshot(20)
    result = model.suggest(DEVICE, live, now=1020)
    assert result["accepted"]
    assert result["suggested_area"] == "area:a"
    assert result["confidence"] == pytest.approx(1)
    assert result["margin"] > 0.5
    assert model.export_state() == original
    assert "live_area" not in result
    assert live == snapshot(20)


@pytest.mark.parametrize(
    "independent,source", [(False, "measured"), (1, "measured"), (True, "current_area"), (True, {}), (True, [])]
)
def test_current_prediction_or_non_independent_truth_cannot_train(independent, source):
    model = FingerprintModel()
    result = model.train(DEVICE, "area:a", snapshot(1), independent=independent, truth_source=source, now=1001)
    assert not result["accepted"]
    assert "independent_label_required" in codes(result)
    assert model.export_state()["labels"] == []


def test_single_snapshot_is_saved_but_cannot_produce_ready_or_high_confidence_model():
    model = FingerprintModel()
    result = model.train(DEVICE, "area:a", snapshot(1), independent=True, now=1001)
    assert result["accepted"]
    assert not result["ready"]
    assert result["samples"] == 1
    assert "insufficient_training_samples" in codes(result)
    result = model.suggest(DEVICE, snapshot(2), now=1002)
    assert not result["accepted"]
    assert result["suggested_area"] is None
    assert "insufficient_training_samples" in codes(result)


def test_cached_packets_cannot_make_overfit_single_snapshot_model_ready():
    model = FingerprintModel()
    for _poll in range(100):
        result = model.train(DEVICE, "area:a", snapshot(1), independent=True, now=1001)
    assert result["samples"] == 1
    assert not result["accepted"]
    assert "insufficient_fresh_receivers" in codes(result)
    assert model.summary()[0]["samples"] == 1
    assert not model.suggest(DEVICE, snapshot(2), now=1002)["accepted"]


def test_reusing_one_receivers_cached_packet_does_not_count_coverage():
    model = FingerprintModel()
    model.train(DEVICE, "area:a", snapshot(1), independent=True, now=1001)
    for stamp in range(2, 10):
        rows = snapshot(stamp)
        rows[0]["stamp"] = 1
        rows[0]["observed"] = 1001
        result = model.train(DEVICE, "area:a", rows, independent=True, now=1000 + stamp)
    assert result["samples"] == 1
    assert "insufficient_fresh_receivers" in codes(result)


def test_new_receivers_count_but_old_receiver_packets_do_not_inflate_training_coverage():
    model = FingerprintModel()
    vector = {**ROOM_A, "scanner:three": -65}
    for stamp in range(5):
        rows = snapshot(stamp, vector)
        rows[-1]["stamp"] = 0
        rows[-1]["observed"] = 1000
        result = model.train(DEVICE, "area:a", rows, independent=True, now=1000 + stamp)
    assert result["ready"]
    assert result["receivers"] == 2
    state = model.export_state()
    assert "scanner:three" in state["labels"][0]["frames"][0]["values"]
    assert "scanner:three" not in state["labels"][0]["frames"][1]["values"]


def test_source_regressions_do_not_generate_new_samples():
    model = FingerprintModel()
    assert train(model)["ready"]
    result = model.train(DEVICE, "area:a", snapshot(2), independent=True, now=1006)
    assert not result["accepted"]
    assert result["samples"] == 5


def test_training_requires_elapsed_fresh_packet_span():
    model = FingerprintModel()
    rows = [snapshot(index / 1000) for index in range(5)]
    result = model.train(DEVICE, "area:a", rows, independent=True, now=1001)
    assert result["accepted"]
    assert not result["ready"]
    assert "insufficient_training_span" in codes(result)


@pytest.mark.parametrize("receiver_count", [0, 1])
def test_minimum_fresh_receiver_coverage_required_for_training(receiver_count):
    model = FingerprintModel()
    rows = snapshot(1)[:receiver_count]
    result = model.train(DEVICE, "area:a", rows, independent=True, now=1001)
    assert not result["accepted"]
    assert "insufficient_receiver_coverage" in codes(result)
    assert model.summary() == []


def test_one_missing_receiver_rejects_and_never_improves_match():
    model = FingerprintModel()
    train(model)
    full = model.suggest(DEVICE, snapshot(10), now=1010)
    missing = model.suggest(DEVICE, snapshot(10)[:1], now=1010)
    assert full["accepted"]
    assert not missing["accepted"]
    assert missing["suggested_area"] is None
    assert "insufficient_receiver_coverage" in codes(missing)


def test_missing_receiver_penalty_degrades_supported_match_and_retains_coverage():
    model = FingerprintModel()
    vector = {**ROOM_A, "scanner:three": -55, "scanner:four": -70}
    train(model, vector=vector)
    live = {key: value for key, value in vector.items() if key != "scanner:four"}
    result = model.suggest(DEVICE, snapshot(10, live), now=1010)
    assert result["coverage"] == 0.75
    assert result["scores"][0]["missing_receivers"] == 1
    assert result["scores"][0]["score"] > 0
    assert not result["accepted"]
    assert "low_confidence" in codes(result)


def test_extra_receiver_penalty_degrades_match():
    model = FingerprintModel()
    train(model)
    result = model.suggest(DEVICE, snapshot(10, {**ROOM_A, "scanner:unexpected": -45}), now=1010)
    assert result["scores"][0]["extra_receivers"] == 1
    assert result["scores"][0]["score"] > 0
    assert result["confidence"] < 0.75
    assert not result["accepted"]


def test_dropped_known_receivers_cannot_match_sparse_common_subset():
    model = FingerprintModel()
    vector = {**ROOM_A, "scanner:three": -60, "scanner:four": -65}
    train(model, vector=vector)
    result = model.suggest(DEVICE, snapshot(10, ROOM_A), now=1010)
    assert not result["accepted"]
    assert "insufficient_matching_coverage" in codes(result)


def test_wrong_device_cannot_train_or_use_another_beacons_fingerprint():
    model = FingerprintModel()
    result = model.train(DEVICE, "area:a", snapshot(1, device="opaque:other"), independent=True, now=1001)
    assert not result["accepted"]
    assert "wrong_device" in codes(result)
    train(model)
    result = model.suggest("opaque:other", snapshot(10, device="opaque:other"), now=1010)
    assert not result["accepted"]
    assert "no_trained_device_model" in codes(result)
    result = model.suggest(DEVICE, snapshot(10, device="opaque:other"), now=1010)
    assert "wrong_device" in codes(result)


def test_two_close_locations_reject_ambiguous_match():
    model = FingerprintModel()
    train(model, vector={"scanner:one": -50, "scanner:two": -60})
    train(model, "area:b", {"scanner:one": -51, "scanner:two": -61}, start=10)
    result = model.suggest(DEVICE, snapshot(20, {"scanner:one": -50.5, "scanner:two": -60.5}), now=1020)
    assert not result["accepted"]
    assert result["suggested_area"] is None
    assert result["margin"] == 0
    assert "ambiguous_locations" in codes(result)


def test_unknown_rf_pattern_rejects_even_when_one_label_is_nearest():
    model = FingerprintModel()
    train(model)
    train(model, "area:b", ROOM_B, start=10)
    result = model.suggest(DEVICE, snapshot(20, {"scanner:one": -110, "scanner:two": -115}), now=1020)
    assert not result["accepted"]
    assert result["suggested_area"] is None
    assert "unknown_pattern" in codes(result)


def test_quantized_identical_training_retains_noise_floor_and_single_label_confidence_cap():
    model = FingerprintModel()
    train(model)
    exact = model.suggest(DEVICE, snapshot(10), now=1010)
    assert exact["accepted"]
    assert exact["confidence"] <= 0.85
    close = model.suggest(DEVICE, snapshot(11, {key: value + 2 for key, value in ROOM_A.items()}), now=1011)
    assert close["accepted"]
    assert close["scores"][0]["score"] == pytest.approx(0.5)
    far = model.suggest(DEVICE, snapshot(12, {key: value + 20 for key, value in ROOM_A.items()}), now=1012)
    assert not far["accepted"]
    assert "unknown_pattern" in codes(far)


def test_unstable_broad_training_cannot_inflate_variance_to_accept_everywhere():
    model = FingerprintModel()
    frames = [snapshot(stamp, {key: -95 + stamp * 15 for key in ROOM_A}) for stamp in range(5)]
    result = model.train(DEVICE, "area:a", frames, independent=True, now=1005)
    assert result["accepted"]
    assert not result["ready"]
    assert "insufficient_stable_training_coverage" in codes(result)


def test_model_version_change_requires_deliberate_retraining():
    model = FingerprintModel()
    train(model)
    result = model.train(DEVICE, "area:a", snapshot(10, version="synthetic-v2"), independent=True, now=1010)
    assert not result["accepted"]
    assert result["samples"] == 5
    assert "model_version_mismatch" in codes(result)
    result = model.suggest(DEVICE, snapshot(11, version="synthetic-v2"), now=1011)
    assert not result["accepted"]
    assert "model_version_mismatch" in codes(result)


@pytest.mark.parametrize("change", ["status", "observed", "future", "age"])
def test_stale_and_future_receiver_data_cannot_be_training_truth_or_match(change):
    rows = snapshot(1)
    for row in rows:
        if change == "status":
            row["status"] = "stale"
        elif change == "observed":
            row["observed"] -= 100
        elif change == "future":
            row["observed"] += 100
        else:
            row["age"] = 100
    model = FingerprintModel()
    result = model.train(DEVICE, "area:a", rows, independent=True, now=1001)
    assert not result["accepted"]
    assert "insufficient_receiver_coverage" in codes(result)
    train(model)
    assert not model.suggest(DEVICE, rows, now=1001)["accepted"]


@pytest.mark.parametrize("field", ["rssi", "stamp", "observed", "age"])
@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"), "-60", 10**1000])
def test_nonfinite_boolean_and_non_numeric_fields_rejected(field, invalid):
    rows = snapshot(1)
    for row in rows:
        row[field] = invalid
    model = FingerprintModel()
    result = model.train(DEVICE, "area:a", rows, independent=True, now=1001)
    assert not result["accepted"]
    assert model.export_state()["labels"] == []


def test_opaque_ids_retained_verbatim_and_input_observations_detached():
    opaque = "irk:key:aB+/=|iBeacon:any/identity"
    model = FingerprintModel()
    rows = [snapshot(stamp, device=opaque) for stamp in range(5)]
    original = copy.deepcopy(rows)
    assert model.train(opaque, "area:label", rows, independent=True, now=1005)["ready"]
    assert rows == original
    rows[0][0]["rssi"] = -120
    state = model.export_state()
    assert state["labels"][0]["device_id"] == opaque
    assert state["labels"][0]["frames"][0]["values"]["scanner:one"] == -45
    state["labels"][0]["frames"][0]["values"]["scanner:one"] = -120
    assert model.export_state()["labels"][0]["frames"][0]["values"]["scanner:one"] == -45


def test_export_import_roundtrip_preserves_predictions_and_dedup():
    model = FingerprintModel()
    train(model)
    train(model, "area:b", ROOM_B, start=10)
    encoded = json.dumps(model.export_state(), allow_nan=False)
    restored = FingerprintModel()
    assert restored.import_state(json.loads(encoded))["accepted"]
    assert restored.suggest(DEVICE, snapshot(20), now=1020) == model.suggest(DEVICE, snapshot(20), now=1020)
    result = restored.train(DEVICE, "area:a", snapshot(4), independent=True, now=1005)
    assert not result["accepted"]
    assert result["samples"] == 5


@pytest.mark.parametrize("corruption", ["provenance", "duplicate", "numeric", "schema", "boolean_version", "timestamp"])
def test_import_invalid_state_rejected_atomically(corruption):
    model = FingerprintModel()
    train(model)
    good = model.export_state()
    state = copy.deepcopy(good)
    frames = state["labels"][0]["frames"]
    if corruption == "provenance":
        frames[0]["independent"] = False
    elif corruption == "duplicate":
        state["labels"].append(copy.deepcopy(state["labels"][0]))
    elif corruption == "numeric":
        frames[0]["values"]["scanner:one"] = float("nan")
    elif corruption == "schema":
        state["private_unexpected"] = "not stored"
    elif corruption == "boolean_version":
        state["schema_version"] = True
    else:
        frames[1]["stamps"]["scanner:one"] = frames[0]["stamps"]["scanner:one"]
    assert not model.import_state(state)["accepted"]
    assert model.export_state() == good


@pytest.mark.parametrize("kind", ["receivers", "snapshots", "labels", "frames", "id"])
def test_input_and_persisted_state_budgets(kind):
    model = FingerprintModel()
    if kind == "receivers":
        rows = snapshot(1, {f"scanner:{i}": -60 for i in range(fingerprints.MAX_RECEIVERS + 1)})
        result = model.train(DEVICE, "area:a", rows, independent=True, now=1001)
    elif kind == "snapshots":
        result = model.train(
            DEVICE,
            "area:a",
            [snapshot(1)] * (fingerprints.MAX_SNAPSHOTS_PER_CALL + 1),
            independent=True,
            now=1001,
        )
    elif kind == "id":
        result = model.train("x" * 257, "area:a", snapshot(1), independent=True, now=1001)
    else:
        train(model)
        state = model.export_state()
        if kind == "labels":
            state["labels"] *= fingerprints.MAX_LABELS + 1
        else:
            state["labels"][0]["frames"] *= fingerprints.MAX_FRAMES_PER_LABEL + 1
        result = model.import_state(state)
    assert not result["accepted"]


def test_training_frame_window_is_bounded_and_state_byte_limit_is_enforced(monkeypatch):
    model = FingerprintModel()
    for stamp in range(200):
        result = model.train(DEVICE, "area:a", snapshot(stamp), independent=True, now=1000 + stamp)
    assert result["samples"] == fingerprints.MAX_FRAMES_PER_LABEL
    assert len(model.export_state()["labels"][0]["frames"]) == fingerprints.MAX_FRAMES_PER_LABEL
    state = model.export_state()
    monkeypatch.setattr(fingerprints, "MAX_STATE_BYTES", 1)
    result = model.train(DEVICE, "area:a", snapshot(201), independent=True, now=1201)
    assert not result["accepted"]
    assert "model_byte_budget" in codes(result)
    assert model.export_state() == state
    assert not model.import_state(state)["accepted"]
    assert model.export_state() == state


def test_explicit_clear_scopes_device_and_label_and_resets_stamp_history():
    model = FingerprintModel()
    train(model)
    train(model, "area:b", ROOM_B, start=10)
    train(model, device="opaque:other")
    assert model.clear(DEVICE, "area:a") == 1
    assert len(model.summary()) == 2
    assert train(model)["ready"]
    assert model.clear(DEVICE) == 2
    assert model.summary()[0]["device_id"] == "opaque:other"
    assert model.clear() == 1
    assert model.summary() == []
