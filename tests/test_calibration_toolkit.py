"""Physical synthetic evidence for receiver calibration safety policies."""

from copy import deepcopy
import math

import pytest

from custom_components.bermuda.calibration import CalibrationEngine, CalibrationPolicy, range_from_rssi


def observations(*, devices=2, verified=True, offsets=None, baselines=None, tx_bias=0.0):
    offsets = offsets or {"receiver-a": 4.0, "receiver-b": -3.0}
    baselines = baselines or {scanner: 0.0 for scanner in offsets}
    rows = []
    for device in range(devices):
        for location in range(2):
            for scanner_index, (scanner, offset) in enumerate(offsets.items()):
                distance = 2.0 + location * 2.0 + scanner_index + device * 0.2
                for packet, noise in enumerate((0.1, -0.1, 0.2, -0.2)):
                    rows.append(
                        {
                            "device_id": f"beacon-{device}",
                            "scanner_id": scanner,
                            "location_id": f"measured-{location}",
                            "observed": 99.0 - packet * 0.1,
                            "stamp": 1000.0 + location * 10 + packet,
                            "rssi": -55.0 - 30.0 * math.log10(distance) - offset + tx_bias + noise,
                            "rssi_offset": baselines[scanner],
                            "reference_power": -55.0,
                            "attenuation": 3.0,
                            "expected_distance": distance,
                            "accuracy_m": 0.0,
                            "truth_source": "measured",
                            "independent": True,
                            "status": "current",
                            "age": 1.0 + packet * 0.1,
                            "quality": {"reliability": 1.0},
                            "reference_power_verified": verified,
                            "model_version": "model-one",
                        }
                    )
    return rows


def preview(rows, **kwargs):
    return CalibrationEngine().preview(rows, now=100.0, model_version="model-one", **kwargs)


def test_fixed_n_recovers_receiver_db_offsets_and_preserves_raw_input():
    rows = observations(baselines={"receiver-a": 1.0, "receiver-b": -1.0})
    frozen = deepcopy(rows)
    result = preview(rows)
    assert result["eligible"]
    assert result["offsets"] == pytest.approx({"receiver-a": 4.0, "receiver-b": -3.0})
    assert result["deltas"] == pytest.approx({"receiver-a": 3.0, "receiver-b": -2.0})
    assert rows == frozen
    assert result["metrics"]["all"]["raw_mae_m"] > result["metrics"]["all"]["current_mae_m"]
    assert result["metrics"]["all"]["current_mae_m"] > result["metrics"]["all"]["proposed_mae_m"]
    assert result["holdouts"]["location_id"] == "measured-1"
    assert result["holdouts"]["device_id"] == "beacon-1"
    assert result["holdouts"]["training_samples"] == 8


def test_path_loss_exponent_has_db_units_and_offset_direction():
    assert range_from_rssi(-85.0, -55.0, 3.0) == pytest.approx(10.0)
    assert range_from_rssi(-85.0, -55.0, 3.0, 30.0) == pytest.approx(1.0)
    assert range_from_rssi(-75.0, -55.0, 2.0) == pytest.approx(10.0)
    with pytest.raises(ValueError):
        range_from_rssi(True, -55.0, 3.0)


def test_independent_target_and_location_holdout_regression_rejects_fit():
    rows = observations(offsets={"receiver-a": 4.0})
    for row in rows:
        if row["device_id"] == "beacon-1" or row["location_id"] == "measured-1":
            row["rssi"] += 4.0
    result = preview(rows)
    assert not result["eligible"]
    assert result["offsets"]["receiver-a"] == pytest.approx(4.0)
    assert any("holdout_regression" in reason for reason in result["reasons"])
    assert "no_validated_improvement:target_holdout" in result["reasons"]


def test_single_unknown_transmitter_never_becomes_global_receiver_offset():
    result = preview(observations(devices=1, verified=False, tx_bias=8.0))
    assert not result["eligible"]
    assert result["offsets"] == {}
    assert result["mode"] == "relative"


def test_multiple_unknown_beacons_cannot_create_global_blind_shift():
    result = preview(observations(devices=4, verified=False, tx_bias=6.0))
    assert result["offsets"] == pytest.approx({"receiver-a": 3.5, "receiver-b": -3.5})
    assert sum(result["deltas"].values()) == pytest.approx(0.0)
    assert not result["eligible"]


def test_consistent_unknown_targets_can_validate_relative_sensitivity():
    result = preview(observations(devices=4, verified=False, offsets={"receiver-a": 3.0, "receiver-b": -3.0}))
    assert result["eligible"]
    assert result["mode"] == "relative"
    assert result["offsets"] == pytest.approx({"receiver-a": 3.0, "receiver-b": -3.0})


def test_differential_target_bias_rejects_relative_correction():
    rows = observations(devices=4, verified=False)
    for row in rows:
        if row["device_id"] == "beacon-1" and row["scanner_id"] == "receiver-a":
            row["rssi"] += 10.0
    result = preview(rows)
    assert not result["eligible"]
    assert any("inconsistent_target_bias" in reason for reason in result["reasons"])


def test_unknown_targets_do_not_validate_absolute_offsets():
    rows = observations(devices=3)
    for row in rows:
        if row["device_id"] != "beacon-0":
            row["reference_power_verified"] = False
    result = preview(rows)
    assert not result["eligible"]
    assert result["coverage"]["devices"] == ["beacon-0"]
    assert result["rejected"]["unverified_reference_power"] == 32


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("observed", 50.0, "stale_observation"),
        ("age", 100.0, "stale_observation"),
        ("rssi", -110.0, "weak_signal"),
        ("rssi", True, "invalid_numeric_input"),
        ("attenuation", float("nan"), "invalid_numeric_input"),
        ("model_version", "other-model", "model_version_mismatch"),
        ("independent", False, "untrusted_truth"),
        ("truth_source", {"area": "predicted"}, "untrusted_truth"),
        ("status", "stale", "stale_observation"),
        ("quality", {"source_trusted": False}, "untrusted_source"),
        ("quality", {"clock_reset": True}, "untrusted_source"),
        ("quality", {"reliability": True}, "unreliable_signal"),
        ("quality", {"signal_reliability": 1.0, "reliable": False}, "untrusted_source"),
        ("quality", {"signal_reliability": 1.0, "source_trusted": False}, "untrusted_source"),
        ("quality", {"signal_reliability": 1.0, "clock_reset": True}, "untrusted_source"),
        ("accuracy_m", 100.0, "uncertain_truth"),
        ("stamp", True, "untrusted_source_clock"),
    ],
)
def test_bad_freshness_model_truth_and_weak_inputs_are_excluded(field, value, reason):
    rows = observations()
    for row in rows:
        row["quality"]["signal_reliability"] = 1.0
    rows[0][field] = value
    result = preview(rows)
    assert result["rejected"][reason] == 1


@pytest.mark.parametrize(
    "quality,accepted",
    [
        ({"signal_reliability": 1.0, "reliability": 0.01}, True),
        ({"signal_reliability": 0.1, "reliability": 1.0}, False),
        ({"signal_reliability": None, "reliability": 1.0}, False),
        ({"signal_reliability": True, "reliability": 1.0}, False),
        ({"signal_reliability": float("nan"), "reliability": 1.0}, False),
        ({"signal_reliability": float("inf"), "reliability": 1.0}, False),
        ({"signal_reliability": "1.0", "reliability": 1.0}, False),
        ({"signal_reliability": -0.1, "reliability": 1.0}, False),
        ({"signal_reliability": 1.1, "reliability": 1.0}, False),
        ({"reliability": 1.0}, True),
        ({"reliability": 0.1}, False),
        ({"reliability": True}, False),
        ({"reliability": None}, True),
        ({}, True),
    ],
)
def test_signal_reliability_is_authoritative_with_legacy_compatibility(quality, accepted):
    rows = observations()
    for row in rows:
        row["quality"] = deepcopy(quality)
    result = preview(rows)
    assert result["eligible"] is accepted
    assert result["coverage"]["samples"] == (len(rows) if accepted else 0)
    if not accepted:
        assert result["rejected"]["unreliable_signal"] == len(rows)


def test_duplicate_packets_cannot_invent_training_evidence():
    rows = observations(devices=1)
    result = preview(rows * 20)
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"]["duplicate_source_stamp"] == len(rows) * 19
    assert not result["eligible"]


def test_current_receiver_baseline_must_match_frozen_capture():
    result = preview(observations(), current_offsets={"receiver-a": 2.0, "receiver-b": 0.0})
    assert result["rejected"]["offset_baseline_mismatch"] == 16
    assert "receiver-a" not in result["coverage"]["receivers"]


def test_bound_and_no_improvement_are_not_validated():
    result = CalibrationEngine(CalibrationPolicy(min_rssi=-127.0)).preview(
        observations(offsets={"receiver-a": 25.0}), now=100.0, model_version="model-one"
    )
    assert not result["eligible"]
    assert "offset_out_of_bounds" in result["reasons"]
    exact = observations(offsets={"receiver-a": 0.0, "receiver-b": 0.0})
    result = preview(exact)
    assert not result["eligible"]
    assert "no_validated_improvement:location_holdout" in result["reasons"]


def test_capped_step_is_validated_before_automatic_apply():
    engine = CalibrationEngine()
    rows = observations()
    proposal = engine.preview(rows, now=100.0, model_version="model-one")
    kwargs = {"now": 100.0, "model_version": "model-one", "current_offsets": {"receiver-a": 0.0, "receiver-b": 0.0}}
    missing = engine.automatic_decision(proposal, **kwargs)
    assert not missing["apply"]
    assert "capped_step_requires_validation" in missing["reasons"]
    decision = engine.automatic_decision(proposal, observations=rows, **kwargs)
    assert decision["apply"]
    assert decision["deltas"] == pytest.approx({"receiver-a": 1.0, "receiver-b": -0.75})
    assert (
        decision["step_metrics"]["target_holdout"]["proposed_mae_m"]
        < decision["step_metrics"]["target_holdout"]["current_mae_m"]
    )


def test_relative_cap_retains_zero_mean_gauge():
    engine = CalibrationEngine()
    rows = observations(devices=4, verified=False, offsets={"receiver-a": 5.0, "receiver-b": -5.0})
    proposal = engine.preview(rows, now=100.0, model_version="model-one")
    decision = engine.automatic_decision(
        proposal,
        now=100.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
        observations=rows,
    )
    assert decision["apply"]
    assert sum(decision["deltas"].values()) == pytest.approx(0.0)
    assert max(abs(delta) for delta in decision["deltas"].values()) <= 1.0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"now": 200.0}, "stale_proposal"),
        ({"last_applied": 99.0}, "cooldown"),
        ({"last_data_id": "same"}, "no_new_independent_data"),
        ({"model_version": "next-model"}, "model_version_mismatch"),
        ({"current_offsets": {"receiver-a": 1.0, "receiver-b": 0.0}}, "offset_baseline_mismatch"),
    ],
)
def test_automatic_stale_cooldown_new_data_version_and_baseline_gates(overrides, reason):
    rows = observations()
    engine = CalibrationEngine()
    proposal = engine.preview(rows, now=100.0, model_version="model-one")
    kwargs = {
        "now": 100.0,
        "model_version": "model-one",
        "current_offsets": {"receiver-a": 0.0, "receiver-b": 0.0},
        "observations": rows,
    }
    if overrides.get("last_data_id") == "same":
        overrides = {"last_data_id": proposal["data_id"]}
    kwargs.update(overrides)
    result = engine.automatic_decision(proposal, **kwargs)
    assert not result["apply"]
    assert reason in result["reasons"]


def test_trial_detects_new_holdout_regression_and_requests_rollback():
    engine = CalibrationEngine()
    rows = observations(offsets={"receiver-a": 0.0, "receiver-b": 0.0})
    result = engine.evaluate_trial(
        rows,
        previous_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
        proposed_offsets={"receiver-a": 4.0, "receiver-b": -3.0},
        now=100.0,
        model_version="model-one",
    )
    assert result["sufficient"]
    assert result["rollback"]
    assert not result["accepted"]
    assert any("regression:per_device" in reason for reason in result["reasons"])


def test_trial_needs_independent_locations_and_targets():
    result = CalibrationEngine().evaluate_trial(
        observations(devices=1),
        previous_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
        proposed_offsets={"receiver-a": 4.0, "receiver-b": -3.0},
        now=100.0,
        model_version="model-one",
    )
    assert not result["sufficient"]
    assert not result["rollback"]
    assert not result["accepted"]


def trial(rows, previous, proposed):
    return CalibrationEngine().evaluate_trial(
        rows, previous_offsets=previous, proposed_offsets=proposed, now=100.0, model_version="model-one"
    )


@pytest.mark.parametrize("prior", [30.0, -30.0, 40.0, -127.0])
def test_trial_allows_finite_inherited_target_baselines_outside_new_offset_bounds(prior):
    rows = observations(offsets={"receiver-a": 4.0})
    result = trial(rows, {"receiver-a": prior}, {"receiver-a": 4.0})
    assert result["sufficient"]
    assert result["accepted"]
    assert not result["rollback"]
    assert result["metrics"]["all"]["current_mae_m"] > result["metrics"]["all"]["proposed_mae_m"]


def test_trial_ignores_unrelated_offline_and_invalid_legacy_offsets():
    rows = observations(offsets={"receiver-a": 4.0})
    previous = {
        "receiver-a": 0.0,
        "offline-receiver": 30.0,
        "other": float("nan"),
        "another": True,
        "invalid": {"legacy": "data"},
    }
    frozen = dict(previous)
    result = trial(rows, previous, {"receiver-a": 4.0})
    assert result["accepted"]
    assert result["metrics"]["per_receiver"].keys() == {"receiver-a"}
    assert previous == frozen


@pytest.mark.parametrize("baseline", [float("nan"), float("inf"), -float("inf"), True, None, "30"])
def test_trial_rejects_nonfinite_boolean_or_nonnumeric_used_baseline(baseline):
    with pytest.raises(ValueError, match="used previous"):
        trial(observations(offsets={"receiver-a": 4.0}), {"receiver-a": baseline}, {"receiver-a": 4.0})


@pytest.mark.parametrize("candidate", [30.0, -21.0, float("nan"), float("inf"), True, None, "4"])
def test_trial_rejects_unsafe_new_candidate_even_without_evaluation_rows(candidate):
    with pytest.raises(ValueError, match="Proposed trial"):
        trial([], {"receiver-a": 0.0}, {"receiver-a": candidate})


def test_trial_mac_aliases_match_prior_and_candidate_and_coverage():
    lower = "aa:bb:cc:dd:ee:ff"
    rows = observations(offsets={lower: 4.0})
    for row in rows[::2]:
        row["scanner_id"] = lower.upper()
    result = trial(rows, {lower.upper(): 30.0, "offline": 30.0}, {lower.upper(): 4.0, lower: 4.0})
    assert result["accepted"]
    assert list(result["metrics"]["per_receiver"]) == [lower]
    assert result["metrics"]["all"]["count"] == len(rows)
    with pytest.raises(ValueError, match="conflict"):
        trial(rows, {lower: 0.0}, {lower: 4.0, lower.upper(): 5.0})


def test_trial_alias_duplicate_packets_cannot_manufacture_holdout_coverage():
    lower = "aa:bb:cc:dd:ee:ff"
    rows = observations(offsets={lower: 4.0})
    duplicates = [{**row, "scanner_id": lower.upper()} for row in rows]
    result = trial(rows + duplicates, {lower: 0.0}, {lower: 4.0})
    assert result["accepted"]
    assert result["metrics"]["all"]["count"] == len(rows)
    assert result["rejected"]["duplicate_source_stamp"] == len(rows)


def test_trial_opaque_sources_remain_case_sensitive_and_missing_baselines_are_insufficient():
    rows = observations(offsets={"Receiver/Case": 4.0})
    result = trial(rows, {"receiver/case": 30.0}, {"Receiver/Case": 4.0})
    assert not result["sufficient"]
    assert not result["accepted"]
    assert not result["rollback"]
    assert result["metrics"]["all"]["count"] == 0


def test_trial_unobserved_baseline_invalidity_does_not_raise_or_claim_success():
    rows = observations(offsets={"receiver-a": 4.0})
    result = trial(rows, {"receiver-a": 0.0, "offline": float("nan")}, {"receiver-a": 4.0, "offline": 2.0})
    assert not result["sufficient"]
    assert not result["accepted"]
    assert not result["rollback"]


def test_trial_unsafe_prediction_from_extreme_finite_baseline_never_claims_acceptance():
    result = trial(observations(offsets={"receiver-a": 4.0}), {"receiver-a": 10000.0}, {"receiver-a": 4.0})
    assert result["sufficient"]
    assert not result["accepted"]
    assert any(reason.startswith("unsafe_prediction") for reason in result["reasons"])


@pytest.mark.parametrize("identifier", [None, 42, True, "", "bad\x00receiver"])
def test_trial_rejects_invalid_new_receiver_identifiers(identifier):
    with pytest.raises(ValueError, match="Proposed trial"):
        trial([], {}, {identifier: 4.0})


def test_policy_rejects_nonfinite_boolean_and_unbounded_overrides():
    for kwargs in (
        {"max_auto_step_db": True},
        {"max_offset_db": 50},
        {"cooldown_s": float("inf")},
        {"min_training_samples": False},
    ):
        with pytest.raises(ValueError):
            CalibrationPolicy(**kwargs)


def test_independently_frozen_locations_captured_minutes_apart_can_preview():
    rows = observations()
    for row in rows:
        capture_time = 100.0 if row["location_id"] == "measured-0" else 200.0
        row["observed"] += capture_time - 100.0
        row["captured_at"] = capture_time
        row["frozen"] = True
    engine = CalibrationEngine()
    proposal = engine.preview(rows, now=201.0, model_version="model-one")
    assert proposal["eligible"]
    assert proposal["coverage"]["samples"] == len(rows)
    assert proposal["latest_observed"] == pytest.approx(199.0)
    decision = engine.automatic_decision(
        proposal,
        now=260.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
        observations=rows,
    )
    assert not decision["apply"]
    assert "stale_proposal" in decision["reasons"]


def test_freezing_a_packet_that_was_stale_at_capture_never_freshens_it():
    rows = observations()
    for row in rows:
        row.update({"frozen": True, "captured_at": 100.0})
    rows[0].update({"frozen": True, "captured_at": 150.0, "age": 1.0})
    result = CalibrationEngine().preview(rows, now=151.0, model_version="model-one")
    assert result["rejected"]["stale_observation"] == 1
    assert result["coverage"]["samples"] == len(rows) - 1


@pytest.mark.parametrize("capture_time", [True, float("nan"), 50.0, 100000.0])
def test_untrusted_or_future_frozen_capture_clocks_are_rejected(capture_time):
    rows = observations()
    rows[0].update({"frozen": True, "captured_at": capture_time})
    result = preview(rows)
    assert result["rejected"]["untrusted_capture_clock"] == 1


def test_frozen_capture_history_has_a_bounded_validation_window():
    rows = observations()
    for row in rows:
        row.update({"frozen": True, "captured_at": 100.0})
    result = CalibrationEngine().preview(rows, now=86600.0, model_version="model-one")
    assert not result["eligible"]
    assert result["rejected"]["untrusted_capture_clock"] == len(rows)
