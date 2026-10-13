"""Calibration packet evidence is independent across validated recording epochs."""

from copy import deepcopy
import hashlib
import json

import pytest

from custom_components.bermuda.calibration import CalibrationEngine
from custom_components.bermuda.recording import MAX_CLOCK_EPOCH
from tests.test_calibration_physical_targets import aliases, physical_observations
from tests.test_calibration_toolkit import observations, preview


def epoch_observations(epoch, **kwargs):
    return [{**row, "session_epoch": epoch} for row in physical_observations(**kwargs)]


@pytest.mark.parametrize("noise_db", [0.0, 0.1])
@pytest.mark.parametrize("new_location", [False, True])
def test_reused_source_stamps_are_distinct_packets_across_epochs(noise_db, new_location):
    first = epoch_observations(1)
    second = epoch_observations(2)
    for row in second:
        row["rssi"] += noise_db
        if new_location:
            row["location_id"] = f"later-{row['location_id']}"
    evidence = first + second
    frozen = deepcopy(evidence)
    result = preview(evidence)
    assert preview(first)["eligible"]
    assert preview(second)["eligible"]
    assert result["eligible"]
    assert result["coverage"]["samples"] == 64
    assert result["coverage"]["targets"] == ["beacon-0", "beacon-1"]
    assert result["rejected"].get("duplicate_source_stamp", 0) == 0
    assert result["rejected"].get("conflicting_source_packet", 0) == 0
    assert result == preview(list(reversed(evidence)))
    assert evidence == frozen


@pytest.mark.parametrize("epoch", [1, MAX_CLOCK_EPOCH])
def test_aliases_share_one_packet_only_within_the_same_valid_epoch(epoch):
    first = epoch_observations(epoch)
    other = epoch_observations(2 if epoch == 1 else 1)
    result = preview(aliases(first) + aliases(other))
    assert result["eligible"]
    assert result["coverage"]["samples"] == 64
    assert result["rejected"]["duplicate_source_stamp"] == 128
    assert result["data_id"] == preview(first + other)["data_id"]
    assert len(result["coverage"]["targets"]) == 2


@pytest.mark.parametrize("field,value", [("rssi", 0.1), ("location_id", "conflicting-location")])
def test_contradictory_alias_packets_in_one_epoch_remain_rejected(field, value):
    first = epoch_observations(1)
    conflict = [{**row, "device_id": f"alias-{row['device_id']}"} for row in first]
    for row in conflict:
        row[field] = row[field] + value if field == "rssi" else value
    second = epoch_observations(2)
    result = preview(first + conflict + second)
    assert result["eligible"]
    assert result["coverage"]["samples"] == 32
    assert result["rejected"]["conflicting_source_packet"] == 64
    assert result["data_id"] == preview(second)["data_id"]
    assert result == preview(list(reversed(first + conflict + second)))


@pytest.mark.parametrize(
    "epoch", [None, True, False, 0, -1, 1.0, "1", float("nan"), float("inf"), [], {}, MAX_CLOCK_EPOCH + 1]
)
def test_invalid_epochs_cannot_supply_evidence_or_separate_alias_packets(epoch):
    first = epoch_observations(1)
    invalid = [{**row, "device_id": f"alias-{row['device_id']}", "session_epoch": epoch} for row in first]
    result = preview(first + invalid)
    assert result["eligible"]
    assert result["coverage"]["samples"] == 32
    assert result["data_id"] == preview(first)["data_id"]
    assert result["rejected"]["invalid_session_epoch"] == 32
    rejected = preview(invalid)
    assert not rejected["eligible"]
    assert rejected["coverage"]["samples"] == 0
    assert rejected["rejected"]["invalid_session_epoch"] == 32


def test_legacy_rows_keep_original_signature_and_accept_missing_source_identity():
    rows = observations()
    original = deepcopy(rows)
    payload = [
        [row["device_id"], row["scanner_id"], row["stamp"], row["observed"], row["location_id"], row["rssi"]]
        for row in rows
    ]
    legacy_id = hashlib.sha256(json.dumps(sorted(payload), separators=(",", ":")).encode()).hexdigest()[:24]
    result = preview(rows)
    assert result["eligible"]
    assert result["data_id"] == legacy_id
    assert result["coverage"]["samples"] == 32
    assert preview(rows + deepcopy(rows))["data_id"] == legacy_id
    assert rows == original
    direct = [{key: value for key, value in row.items() if key != "stamp"} for row in rows]
    for row in direct:
        row["observed"] -= 2.0 if row["location_id"] == "measured-1" else 0.0
        row["age"] = 100.0 - row["observed"]
    assert preview(direct)["eligible"]


@pytest.mark.parametrize("source_identity", [False, True])
def test_mixed_legacy_and_epoch_rows_are_distinct_and_sort_deterministically(source_identity):
    legacy = physical_observations() if source_identity else observations()
    # Observed=epoch used to make naive optional-key payload sorting compare
    # a legacy location string with a new row's observed float.
    current = [{**row, "session_epoch": 99} for row in legacy]
    result = preview(legacy + current)
    assert result["eligible"]
    assert result["coverage"]["samples"] == 64
    assert result["rejected"].get("duplicate_source_stamp", 0) == 0
    assert result == preview(list(reversed(legacy + current)))
    assert len({preview(legacy)["data_id"], preview(current)["data_id"], result["data_id"]}) == 3


def test_new_epoch_changes_data_freshness_even_with_identical_radio_values_and_clocks():
    engine = CalibrationEngine()
    first = epoch_observations(1)
    second = epoch_observations(2)
    previous = preview(first)
    current = preview(second)
    assert previous["data_id"] != current["data_id"]
    assert current["data_id"] == preview(aliases(second))["data_id"]
    kwargs = {
        "now": 100.0,
        "model_version": "model-one",
        "current_offsets": {"receiver-a": 0.0, "receiver-b": 0.0},
        "last_data_id": previous["data_id"],
    }
    duplicate = engine.automatic_decision(previous, observations=aliases(first), **kwargs)
    assert not duplicate["apply"]
    assert "no_new_independent_data" in duplicate["reasons"]
    decision = engine.automatic_decision(current, observations=aliases(second), **kwargs)
    assert decision["apply"]
    assert decision["step_metrics"]["location_holdout"]["count"] == 16
    assert decision["step_metrics"]["target_holdout"]["count"] == 16


def test_multiple_epochs_do_not_create_independent_targets():
    result = preview(epoch_observations(1, devices=1) + epoch_observations(2, devices=1))
    assert not result["eligible"]
    assert result["coverage"]["samples"] == 32
    assert len(result["coverage"]["targets"]) == 1
    assert result["reasons"] == ["insufficient_independent_locations_or_targets"]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("age", 100.0, "stale_observation"),
        ("quality", {"source_trusted": False}, "untrusted_source"),
        ("model_version", "other-model", "model_version_mismatch"),
    ],
)
def test_distinct_epoch_does_not_bypass_source_freshness_quality_or_model(field, value, reason):
    first = epoch_observations(1)
    second = [{**row, field: value} for row in epoch_observations(2)]
    result = preview(first + second)
    assert result["coverage"]["samples"] == 32
    assert result["data_id"] == preview(first)["data_id"]
    assert result["rejected"][reason] == 32
