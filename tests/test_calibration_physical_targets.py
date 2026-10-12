"""Synthetic calibration evidence cannot turn logical aliases into beacons."""

from copy import deepcopy

import pytest

from custom_components.bermuda.calibration import CalibrationEngine, CalibrationPolicy
from tests.test_calibration_toolkit import observations, preview, trial


def physical_observations(**kwargs):
    rows = observations(**kwargs)
    for row in rows:
        row["source_id"] = f"physical-{row['device_id']}"
    return rows


def aliases(rows, count=2):
    return rows + [{**row, "device_id": f"alias-{index}-{row['device_id']}"} for index in range(count) for row in rows]


@pytest.mark.parametrize("verified,count", [(True, 1), (False, 3)])
def test_aliases_cannot_manufacture_absolute_or_relative_independent_targets(verified, count):
    rows = physical_observations(devices=1, verified=verified)
    result = preview(aliases(rows, count))
    assert not result["eligible"]
    assert result["offsets"] == {}
    assert result["reasons"] == ["insufficient_independent_locations_or_targets"]
    assert len(result["coverage"]["devices"]) == count + 1
    assert len(result["coverage"]["targets"]) == 1
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"]["duplicate_source_stamp"] == len(rows) * count


@pytest.mark.parametrize("verified,devices", [(True, 2), (False, 4)])
def test_genuine_physical_targets_with_aliases_still_calibrate(verified, devices):
    rows = physical_observations(devices=devices, verified=verified, offsets={"receiver-a": 3.0, "receiver-b": -3.0})
    evidence = aliases(rows)
    frozen = deepcopy(evidence)
    result = preview(evidence)
    original = preview(rows)
    assert result["eligible"]
    assert result["offsets"] == pytest.approx(original["offsets"])
    assert result["coverage"]["samples"] == len(rows)
    assert len(result["coverage"]["targets"]) == devices
    assert len(result["coverage"]["devices"]) == devices * 3
    assert result["data_id"] == original["data_id"]
    assert result["metrics"]["target_holdout"]["count"] == 16
    assert result["holdouts"]["training_samples"] == (devices - 1) * 8
    assert len(result["holdouts"]["device_ids"]) == 3
    assert result["metrics"]["per_device"].keys() == set(result["coverage"]["devices"])
    assert evidence == frozen


def test_stable_meta_alias_links_rotating_sources_into_one_target():
    rows = observations(devices=1)
    for row in rows:
        source = "aa:bb:cc:dd:ee:01" if row["location_id"] == "measured-0" else "aa:bb:cc:dd:ee:02"
        row.update(device_id=source, source_id=source)
    meta = [{**row, "device_id": "ibeacon:stable-meta"} for row in rows]
    result = preview(rows + meta)
    assert not result["eligible"]
    assert len(result["coverage"]["targets"]) == 1
    assert result["coverage"]["samples"] == len(rows)
    assert sorted(next(iter(result["target_groups"].values()))) == [
        "aa:bb:cc:dd:ee:01",
        "aa:bb:cc:dd:ee:02",
        "ibeacon:stable-meta",
    ]


@pytest.mark.parametrize(
    "rejected_field,rejected_value,reason",
    [
        ("age", 100.0, "stale_observation"),
        ("rssi", -110.0, "weak_signal"),
        ("independent", False, "untrusted_truth"),
        ("model_version", "replaced-model", "model_version_mismatch"),
    ],
)
def test_rejected_signal_bridge_cannot_split_one_physical_target(rejected_field, rejected_value, reason):
    rows = observations(devices=1)
    for row in rows:
        source = "source-one" if row["location_id"] == "measured-0" else "source-two"
        row.update(device_id=source, source_id=source)
    meta = [{**row, "device_id": "stable-meta"} for row in rows]
    for row in meta:
        if row["source_id"] == "source-two":
            row[rejected_field] = rejected_value
    result = preview(rows + meta)
    assert not result["eligible"]
    assert len(result["coverage"]["targets"]) == 1
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"][reason] == 8


def test_identity_links_obey_the_observation_bound():
    rows = physical_observations()
    bridges = [{**rows[0], "device_id": "meta"}, {**rows[-1], "device_id": "meta"}]
    result = CalibrationEngine(CalibrationPolicy(max_observations=len(rows))).preview(
        rows + bridges, now=100.0, model_version="model-one"
    )
    assert result["eligible"]
    assert len(result["coverage"]["targets"]) == 2
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"]["observation_limit"] == 1


def test_alias_packet_counts_cannot_manufacture_training_or_holdout_samples():
    rows = [row for row in physical_observations() if row["stamp"] in {1000.0, 1010.0}]
    result = preview(aliases(rows, 10))
    assert not result["eligible"]
    assert result["coverage"]["samples"] == 8
    assert result["holdouts"]["training_samples"] == 2
    assert "insufficient_training_samples" in result["reasons"]
    assert "insufficient_location_holdout" not in result["reasons"]
    assert result["metrics"]["target_holdout"]["count"] == 4


def test_distinct_packets_from_many_aliases_do_not_unbalance_target_fit():
    rows = physical_observations(devices=3, offsets={"receiver-a": 4.0})
    for row in rows:
        if row["device_id"] == "beacon-0":
            row["rssi"] += 2.0
        elif row["device_id"] == "beacon-1":
            row["rssi"] -= 2.0
    extra = [
        {**row, "device_id": f"alias-{alias}", "stamp": row["stamp"] + 100.0 * alias}
        for alias in range(1, 10)
        for row in rows
        if row["device_id"] == "beacon-0"
    ]
    result = preview(rows + extra)
    assert result["eligible"]
    assert result["offsets"] == pytest.approx({"receiver-a": 4.0})
    assert len(result["coverage"]["targets"]) == 3


@pytest.mark.parametrize(
    "field,change",
    [
        ("rssi", 1.0),
        ("reference_power", 1.0),
        ("attenuation", 0.2),
        ("expected_distance", 1.0),
        ("accuracy_m", 0.1),
        ("rssi_offset", 1.0),
        ("location_id", "other-measured-location"),
    ],
)
def test_contradictory_alias_packet_model_or_truth_is_rejected_independent_of_order(field, change):
    rows = physical_observations()
    duplicates = [{**row, "device_id": f"alias-{row['device_id']}"} for row in rows]
    for row in duplicates:
        row[field] = change if isinstance(change, str) else row[field] + change
    forward = preview(rows + duplicates)
    reverse = preview(duplicates + rows)
    assert forward == reverse
    assert not forward["eligible"]
    assert forward["coverage"]["samples"] == 0
    assert forward["rejected"]["conflicting_source_packet"] == len(rows) * 2


@pytest.mark.parametrize("field", ["source_id", "device_id", "scanner_id"])
def test_mac_case_aliases_are_canonical_for_targets_and_packets(field):
    rows = observations(devices=1, offsets={"aa:bb:cc:dd:ee:03": 4.0})
    for row in rows:
        row.update(device_id="aa:bb:cc:dd:ee:01", source_id="aa:bb:cc:dd:ee:02")
    duplicates = [{**row, field: row[field].upper()} for row in rows]
    result = preview(rows + duplicates)
    assert not result["eligible"]
    assert len(result["coverage"]["targets"]) == 1
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"]["duplicate_source_stamp"] == len(rows)
    assert result["coverage"]["receivers"] == ["aa:bb:cc:dd:ee:03"]


def test_opaque_source_and_device_ids_are_case_sensitive():
    rows = physical_observations()
    for row in rows:
        upper = row["device_id"] == "beacon-0"
        row["device_id"] = "Beacon/Case" if upper else "beacon/case"
        row["source_id"] = "Source/Case" if upper else "source/case"
    result = preview(rows)
    assert result["eligible"]
    assert result["coverage"]["targets"] == ["Beacon/Case", "beacon/case"]
    assert result["coverage"]["samples"] == len(rows)


def test_old_evidence_without_physical_sources_does_not_invent_alias_links():
    rows = observations(devices=1)
    evidence = rows + [{**row, "device_id": "second-independent-legacy-beacon"} for row in rows]
    result = preview(evidence)
    assert result["eligible"]
    assert len(result["coverage"]["targets"]) == 2
    assert result["coverage"]["samples"] == len(evidence)


@pytest.mark.parametrize(
    "legacy_device,explicit_source,linked",
    [
        ("aa:bb:cc:dd:ee:01", "AA:BB:CC:DD:EE:01", True),
        ("Source/Case", "Source/Case", True),
        ("Source/Case", "source/case", False),
    ],
)
def test_mixed_legacy_and_new_evidence_links_explicit_source_to_legacy_device(legacy_device, explicit_source, linked):
    rows = observations(devices=1, verified=False)
    for row in rows:
        row["device_id"] = legacy_device
    meta = [{**row, "device_id": "ibeacon:physical-one", "source_id": explicit_source} for row in rows]
    engine = CalibrationEngine()
    kwargs = {
        "now": 100.0,
        "model_version": "model-one",
        "verified_reference_devices": [legacy_device, "ibeacon:physical-one"],
    }
    result = engine.preview(rows + meta, **kwargs)
    assert result["eligible"] is not linked
    assert result["mode"] == "absolute"
    assert len(result["coverage"]["targets"]) == (1 if linked else 2)
    assert result["coverage"]["samples"] == len(rows) * (1 if linked else 2)
    if linked:
        assert result["rejected"]["duplicate_source_stamp"] == len(rows)
        assert result["data_id"] == engine.preview(rows, **kwargs)["data_id"]
        assert result["data_id"] == engine.preview(meta, **kwargs)["data_id"]


def test_mixed_legacy_rotating_sources_and_new_meta_remain_one_target():
    rows = observations(devices=1)
    for row in rows:
        row["device_id"] = "source-one" if row["location_id"] == "measured-0" else "source-two"
    meta = [{**row, "device_id": "stable-meta", "source_id": row["device_id"]} for row in rows]
    result = preview(rows + meta)
    assert not result["eligible"]
    assert len(result["coverage"]["targets"]) == 1
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"]["duplicate_source_stamp"] == len(rows)


def test_legacy_meta_packets_and_raw_source_packets_deduplicate_through_rejected_identity_bridge():
    rows = observations()
    for row in rows:
        if row["device_id"] == "beacon-0":
            row["device_id"] = "ibeacon:physical-one"
    legacy = [row for row in rows if row["device_id"] == "ibeacon:physical-one"]
    raw = [{**row, "device_id": "aa:bb:cc:dd:ee:01", "source_id": "aa:bb:cc:dd:ee:01"} for row in legacy]
    bridge = {**legacy[0], "source_id": "aa:bb:cc:dd:ee:01", "status": "stale"}
    evidence = rows + raw + [bridge]
    result = preview(evidence)
    assert result["eligible"]
    assert result["coverage"]["samples"] == 32
    assert len(result["coverage"]["targets"]) == 2
    assert result["holdouts"]["training_samples"] == 8
    assert result["rejected"]["duplicate_source_stamp"] == 16
    assert result["rejected"]["stale_observation"] == 1
    assert result["metrics"]["all"]["count"] == 32
    assert result == preview(list(reversed(evidence)))


@pytest.mark.parametrize("minimum", ["training", "target_holdout", "location_holdout"])
def test_legacy_meta_alias_packets_cannot_manufacture_minimum_fit_or_holdout_counts(minimum):
    rows = observations()
    for row in rows:
        row["device_id"] = "00:11:22:33:44:55" if row["device_id"] == "beacon-0" else "ibeacon:physical-one"
    if minimum == "training":
        rows = [row for row in rows if row["stamp"] in {1000.0, 1010.0}]
    elif minimum == "target_holdout":
        rows = [row for row in rows if row["device_id"] == "00:11:22:33:44:55" or row["stamp"] == 1010.0]
    else:
        rows = [
            row
            for row in rows
            if row["location_id"] == "measured-0"
            or (row["device_id"] == "ibeacon:physical-one" and row["stamp"] == 1010.0)
        ]
    legacy = [row for row in rows if row["device_id"] == "ibeacon:physical-one"]
    raw = [{**row, "device_id": "aa:bb:cc:dd:ee:01", "source_id": "aa:bb:cc:dd:ee:01"} for row in legacy]
    bridge = {**legacy[0], "source_id": "aa:bb:cc:dd:ee:01", "status": "stale"}
    result = preview(rows + raw * 10 + [bridge])
    assert not result["eligible"]
    assert result["coverage"]["samples"] == len(rows)
    reason = "insufficient_training_samples" if minimum == "training" else f"insufficient_{minimum}"
    assert reason in result["reasons"]


def test_rotating_sources_with_distinct_stamps_preserve_distinct_packet_evidence():
    rows = physical_observations()
    for row in rows:
        row["source_id"] += f"-rotation-{row['location_id']}"
    result = preview(rows)
    assert result["eligible"]
    assert result["coverage"]["samples"] == len(rows)
    assert len(result["coverage"]["targets"]) == 2


def test_rotating_source_clock_collision_cannot_invent_matching_packet_evidence():
    rows = physical_observations(devices=1)
    duplicates = [{**row, "source_id": "rotated-source"} for row in rows]
    result = preview(rows + duplicates)
    assert result["coverage"]["samples"] == len(rows)
    assert result["rejected"]["duplicate_source_stamp"] == len(rows)
    assert not result["eligible"]


def test_rotating_source_clock_collision_with_conflicting_truth_rejects_ambiguity():
    rows = physical_observations(devices=1)
    for row in rows:
        if row["location_id"] == "measured-1":
            row["source_id"] = "rotated-source"
            row["stamp"] -= 10.0
    result = preview(rows)
    assert result["coverage"]["samples"] == 0
    assert result["rejected"]["conflicting_source_packet"] == len(rows)
    assert not result["eligible"]


def test_mixed_legacy_packet_with_conflicting_alias_rejects_both_evidence_rows():
    rows = observations(devices=1)
    meta = [
        {**row, "device_id": "stable-meta", "source_id": row["device_id"], "reference_power": -54.0} for row in rows
    ]
    result = preview(rows + meta)
    assert not result["eligible"]
    assert result["coverage"]["samples"] == 0
    assert result["rejected"]["conflicting_source_packet"] == len(rows) * 2
    assert result == preview(meta + rows)


def test_mixed_legacy_and_new_genuine_targets_still_calibrate_and_validate_capped_step():
    rows = observations()
    meta = [{**row, "device_id": f"meta-{row['device_id']}", "source_id": row["device_id"]} for row in rows]
    engine = CalibrationEngine()
    result = preview(rows + meta)
    assert result["eligible"]
    assert result["offsets"] == pytest.approx({"receiver-a": 4.0, "receiver-b": -3.0})
    assert result["coverage"]["samples"] == len(rows)
    assert len(result["coverage"]["targets"]) == 2
    assert result["holdouts"]["training_samples"] == 8
    assert result["metrics"]["target_holdout"]["count"] == 16
    decision = engine.automatic_decision(
        result,
        observations=rows + meta,
        now=100.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
    )
    assert decision["apply"]
    assert decision["step_metrics"]["target_holdout"]["count"] == 16


def test_mixed_legacy_trial_cannot_claim_two_independent_targets():
    rows = observations(devices=1)
    meta = [{**row, "device_id": "stable-meta", "source_id": row["device_id"]} for row in rows]
    result = trial(rows + meta, {"receiver-a": 0.0, "receiver-b": 0.0}, {"receiver-a": 4.0, "receiver-b": -3.0})
    assert not result["sufficient"]
    assert not result["accepted"]
    assert result["metrics"]["all"]["count"] == len(rows)


def test_mixed_legacy_capped_validation_cannot_claim_two_independent_targets():
    rows = observations()
    result = preview(rows)
    evidence = [row for row in rows if row["device_id"] == "beacon-1"]
    meta = [{**row, "device_id": "stable-meta", "source_id": row["device_id"]} for row in evidence]
    decision = CalibrationEngine().automatic_decision(
        result,
        observations=evidence + meta,
        now=100.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
    )
    assert not decision["apply"]
    assert "insufficient_independent_locations_or_targets" in decision["reasons"]
    assert decision["step_metrics"]["target_holdout"]["count"] == len(evidence)


@pytest.mark.parametrize("devices,sufficient", [(1, False), (2, True)])
def test_trial_sufficiency_and_metrics_use_physical_targets(devices, sufficient):
    rows = physical_observations(devices=devices)
    result = trial(aliases(rows, 3), {"receiver-a": 0.0, "receiver-b": 0.0}, {"receiver-a": 4.0, "receiver-b": -3.0})
    assert result["sufficient"] is sufficient
    assert result["accepted"] is sufficient
    assert not result["rollback"]
    assert result["metrics"]["all"]["count"] == len(rows)
    assert len(result["metrics"]["per_target"]) == devices


@pytest.mark.parametrize("verified,devices", [(True, 2), (False, 4)])
def test_capped_auto_requires_genuinely_independent_targets_in_validation_evidence(verified, devices):
    engine = CalibrationEngine()
    rows = physical_observations(devices=devices, verified=verified, offsets={"receiver-a": 5.0, "receiver-b": -5.0})
    proposal = preview(rows)
    assert proposal["eligible"]
    one_target = [row for row in rows if row["device_id"] == f"beacon-{devices - 1}"]
    evidence = one_target + [
        {**row, "device_id": f"beacon-{device}"} for device in range(devices - 1) for row in one_target
    ]
    decision = engine.automatic_decision(
        proposal,
        observations=evidence,
        now=100.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
    )
    assert not decision["apply"]
    assert "insufficient_independent_locations_or_targets" in decision["reasons"]


def test_capped_auto_packet_duplicates_do_not_manufacture_target_holdout_samples():
    rows = physical_observations()
    proposal = preview(rows)
    evidence = [row for row in rows if row["device_id"] == "beacon-0"] + [
        row for row in rows if row["device_id"] == "beacon-1" and row["stamp"] == 1010.0
    ]
    decision = CalibrationEngine().automatic_decision(
        proposal,
        observations=aliases(evidence, 10),
        now=100.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
    )
    assert not decision["apply"]
    assert "insufficient_target_holdout" in decision["reasons"]
    assert decision["step_metrics"]["target_holdout"]["count"] == 2


def test_capped_auto_resolves_holdout_through_aliases_added_since_preview():
    rows = physical_observations()
    proposal = preview(rows)
    decision = CalibrationEngine().automatic_decision(
        proposal,
        observations=aliases(rows),
        now=100.0,
        model_version="model-one",
        current_offsets={"receiver-a": 0.0, "receiver-b": 0.0},
    )
    assert decision["apply"]
    assert decision["step_metrics"]["target_holdout"]["count"] == 16
    assert decision["step_metrics"]["per_target"].keys() == {"alias-0-beacon-0", "alias-0-beacon-1"}
