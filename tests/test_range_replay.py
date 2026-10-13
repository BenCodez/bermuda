"""Synthetic replay and nonmutating shadow safety tests."""

from copy import deepcopy
import math

import pytest

from custom_components.bermuda.recording import DiagnosticHistory
from custom_components.bermuda.replay import ReplayEngine, ShadowSession

from .test_diagnostic_history import observation, record, truth


def frames(*, count=6, distance=4.0, expected=2.0, labelled=True):
    history = DiagnosticHistory(enabled=True)
    for index in range(count):
        label = truth(1000 + index, location="site-a" if index < count // 2 else "site-b", distance=expected)
        record(history, observation(1000 + index, distance=distance), [label] if labelled else None)
    return history.frames()


def test_range_replay_raw_current_candidate_and_separate_labels():
    captured = frames()
    captured[0]["observations"][0]["filtered_distance"] = 3.0
    report = ReplayEngine().evaluate(
        captured, proposed_offsets={"scanner-a": 20 * math.log10(2)}, holdout_locations=["site-b"]
    )
    assert report["range_metrics"]["raw"]["mae_m"] == 2
    assert report["range_metrics"]["current"]["mae_m"] < 2
    assert report["range_metrics"]["candidate"]["mae_m"] == pytest.approx(0.5 / 6)
    assert report["observations"][0]["candidate"] == pytest.approx(1.5)
    assert report["holdout"]["range_metrics"]["candidate"]["mae_m"] < 1e-10
    assert report["per_device"]["beacon-a"]["raw"]["count"] == 6
    assert report["per_location"]["site-a"]["raw"]["count"] == 3
    assert report["holdout"]["count"] == 3
    assert report["remaining_labelled"]["count"] == 3
    assert report["position_accuracy"]["available"] is False
    assert captured[0]["observations"][0]["rssi"] == -69.5


@pytest.mark.parametrize("uppercase_observations", [False, True])
def test_mac_candidates_match_receiver_and_device_aliases_in_replay_and_shadow(uppercase_observations):
    scanner, device = "aa:bb:cc:dd:ee:12", "aa:bb:cc:dd:ee:01"
    observed_scanner = scanner.upper() if uppercase_observations else scanner
    observed_device = device.upper() if uppercase_observations else device
    candidate_scanner = scanner if uppercase_observations else scanner.upper()
    candidate_device = device if uppercase_observations else device.upper()
    captured = frames(distance=10, expected=1)
    for frame in captured:
        frame["observations"][0].update(device_id=observed_device, scanner_id=observed_scanner)
        frame["truth"][0].update(device_id=observed_device, ranges={observed_scanner: 1})
    offsets, modes = {candidate_scanner: 20}, {candidate_device: "pet"}
    original = deepcopy(captured)
    report = ReplayEngine().evaluate(captured, proposed_offsets=offsets, filter_modes=modes)
    assert all(row["current"] == 10 and row["candidate"] == pytest.approx(1) for row in report["observations"])
    assert all(row["candidate_pipeline"] == "replayed_pet_filter" for row in report["observations"])
    assert report["range_metrics"]["candidate"]["mae_m"] == pytest.approx(0)
    assert captured == original
    shadow = ShadowSession([], model_version="model-1", proposed_offsets=offsets, filter_modes=modes)
    for frame in captured:
        assert shadow.add_frame(frame) == 1
    assert shadow.promotion_check(current_model_version="model-1")["eligible"]


def test_mac_truth_aliases_label_corresponding_mixed_case_frozen_observations():
    scanner, device = "aa:bb:cc:dd:ee:12", "aa:bb:cc:dd:ee:01"
    captured = frames(count=1, distance=10, expected=1)
    row = captured[0]["observations"][0]
    row.update(device_id=device.upper(), scanner_id=scanner.upper())
    captured[0]["observations"].append(
        {**deepcopy(row), "device_id": device, "scanner_id": scanner, "stamp": row["stamp"] + 1}
    )
    captured[0]["truth"][0].update(device_id=device, ranges={scanner: 1})
    report = ReplayEngine().evaluate(captured, proposed_offsets={scanner: 20})
    assert report["counts"]["labelled"] == 2
    assert all(row["expected_distance"] == 1 and row["candidate"] == pytest.approx(1) for row in report["observations"])


@pytest.mark.parametrize("field", ["proposed_offsets", "filter_modes"])
def test_replay_rejects_conflicting_mac_candidate_aliases(field):
    identifier = "aa:bb:cc:dd:ee:12"
    first, second = (20, 19) if field == "proposed_offsets" else ("pet", "stationary")
    with pytest.raises(ValueError, match="conflicting candidate"):
        ReplayEngine().evaluate(frames(count=1), **{field: {identifier: first, identifier.upper(): second}})


def test_opaque_candidate_and_truth_identifiers_keep_exact_case():
    scanner, device = "receiver:CaseSensitive", "beacon:CaseSensitive"
    captured = frames(count=1, distance=10, expected=1)
    row = captured[0]["observations"][0]
    row.update(device_id=device, scanner_id=scanner)
    row["quality"]["mode"] = "pet"
    captured[0]["truth"][0].update(device_id=device, ranges={scanner: 1})
    report = ReplayEngine().evaluate(
        captured, proposed_offsets={scanner.lower(): 20}, filter_modes={device.lower(): "legacy"}
    )
    assert report["observations"][0]["candidate"] == 10
    assert report["observations"][0]["candidate_pipeline"] == "preserved_live_filter"
    captured[0]["observations"].append({**deepcopy(row), "device_id": device.lower(), "scanner_id": scanner.lower()})
    report = ReplayEngine().evaluate(captured)
    assert report["counts"]["labelled"] == 1
    assert report["observations"][1]["expected_distance"] is None


@pytest.mark.parametrize("field", ["scanner_id", "device_id"])
@pytest.mark.parametrize("identifier", ["aa:bb:cc:dd:ee:12", "Opaque:source"])
def test_replay_source_clock_aliases_only_mac_ids(field, identifier):
    captured = frames(count=2)
    for index, frame in enumerate(captured):
        row, label = frame["observations"][0], frame["truth"][0]
        row[field] = identifier if index == 0 else identifier.upper()
        row["stamp"] = 1000
        label.update(device_id=row["device_id"], ranges={row["scanner_id"]: 2})
    report = ReplayEngine().evaluate(captured)
    is_mac = identifier.startswith("aa:")
    assert report["counts"]["observations"] == (1 if is_mac else 2)
    assert report["counts"]["duplicates_ignored"] == (1 if is_mac else 0)
    assert report["observations"][0][field] == identifier


@pytest.mark.parametrize("attenuation", [50.0, 0.05, 0.0001, 5e-324])
def test_replay_preserves_supported_legacy_radio_baselines_without_a_candidate(attenuation):
    captured = frames(distance=1.0, expected=1.0)
    for frame in captured:
        row = frame["observations"][0]
        row.update(attenuation=attenuation, reference_power=25.0, rssi_offset=101.0, rssi=-76.0)
    report = ReplayEngine().evaluate(captured)
    assert report["range_metrics"]["current"]["mae_m"] == 0
    assert report["range_metrics"]["candidate"]["mae_m"] == 0
    assert all(row["candidate"] == 1 for row in report["observations"])


@pytest.mark.parametrize("attenuation", [0.0001, 5e-324])
@pytest.mark.parametrize("offset", [-1.0, 1.0])
def test_tiny_legacy_attenuation_rejects_unrepresentable_candidates_with_validation_error(attenuation, offset):
    captured = frames(distance=1.0, expected=1.0)
    for frame in captured:
        row = frame["observations"][0]
        row.update(attenuation=attenuation, reference_power=-59.0, rssi=-59.0, rssi_offset=0.0)
    with pytest.raises(ValueError, match="candidate range"):
        ReplayEngine().evaluate(captured, proposed_offsets={"scanner-a": offset})


def test_replay_rejects_a_positive_live_range_that_would_round_to_unavailable_zero():
    captured = frames(distance=5e-324, expected=0.05)
    for frame in captured:
        frame["observations"][0].update(rssi_offset=0.0, attenuation=2.0)
    with pytest.raises(ValueError, match="candidate range"):
        ReplayEngine().evaluate(captured, proposed_offsets={"scanner-a": 20.0})


def test_unlabelled_metrics_are_honestly_unavailable():
    report = ReplayEngine().evaluate(frames(labelled=False))
    assert report["range_metrics"] == {"raw": None, "current": None, "candidate": None}
    assert report["movement"]["available"] is False
    assert report["unavailable"]
    assert report["position_accuracy"]["available"] is False


def test_source_clocks_duplicates_packet_gaps_and_resets():
    captured = frames(count=3)
    captured[1]["observations"][0]["stamp"] = captured[0]["observations"][0]["stamp"]
    captured[2]["observations"][0]["stamp"] = 1010
    report = ReplayEngine().evaluate(captured)
    assert report["counts"]["duplicates_ignored"] == 1
    assert report["packet_gaps"]["max_s"] == 10
    assert [row["normalized_source_s"] for row in report["observations"]] == [0, 10]
    captured[2]["session_epoch"] = 2
    captured[2]["observations"][0]["stamp"] = 1
    report = ReplayEngine().evaluate(captured)
    assert report["packet_gaps"]["count"] == 0
    assert report["observations"][-1]["normalized_source_s"] == 0


def test_callback_detached_candidate_filter_and_label_lag_honesty():
    captured = frames(count=6, distance=2, expected=2)
    for frame in captured[3:]:
        frame["truth"][0]["ranges"]["scanner-a"] = 6
        frame["observations"][0]["raw_distance"] = 6
        frame["observations"][0]["filtered_distance"] = 6 if frame["sequence"] == 6 else 3
    original = deepcopy(captured)

    def candidate(row, elapsed):
        row["quality"]["mutated"] = True
        return row["candidate_raw_distance"]

    report = ReplayEngine().evaluate(captured, filter_callback=candidate)
    transition = report["movement"]["transitions"][0]
    assert transition["current_settling_upper_bound_s"] == 2
    assert transition["candidate_settling_upper_bound_s"] == 0
    assert report["movement"]["true_response_lag_available"] is False
    assert captured == original
    for frame in captured[4:]:
        frame["truth"] = []
    report = ReplayEngine().evaluate(captured)
    assert report["movement"]["transitions"][0]["current_settling_upper_bound_s"] is None


def test_shadow_never_mutates_live_and_requires_new_data_model_binding():
    baseline = frames(count=1)
    live = {"scanner-a": 0.0}
    proposed = {"scanner-a": 20 * math.log10(2)}
    original = deepcopy(baseline)
    shadow = ShadowSession(baseline, model_version="model-1", proposed_offsets=proposed)
    proposed["scanner-a"] = 99
    assert not shadow.promotion_check(current_model_version="model-1")["eligible"]
    for frame in frames(count=8)[1:]:
        shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert check["eligible"], check["reasons"]
    assert shadow.proposed_offsets["scanner-a"] == pytest.approx(20 * math.log10(2))
    assert baseline == original and live == {"scanner-a": 0}
    assert not shadow.promotion_check(current_model_version="model-2")["eligible"]
    shadow.mark_promoted(current_model_version="model-1")
    assert shadow.state == "promoted"
    assert not shadow.promotion_check(current_model_version="model-1")["eligible"]


def test_shadow_reports_location_coverage_across_multiple_devices():
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 20 * math.log10(2)})
    history = DiagnosticHistory(enabled=True)
    for device_index, (device, locations) in enumerate(
        (("beacon-a", ("site-a", "site-b")), ("beacon-b", ("site-c", "site-d")))
    ):
        for index in range(6):
            when = 1000 + device_index * 6 + index
            label = truth(when, device=device, location=locations[index >= 3], distance=2)
            record(history, observation(when, device=device, distance=4), [label])
    for frame in history.frames():
        assert shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert check["eligible"], check["reasons"]
    assert check["new_labelled_rows"] == 12
    assert check["new_locations"] == ["site-a", "site-b", "site-c", "site-d"]
    assert check["new_locations"] == sorted(check["evaluation"]["report"]["per_location"])


def test_shadow_old_data_reuse_unlabelled_insufficient_and_version_change():
    initial = frames()
    shadow = ShadowSession(initial, model_version="model-1", proposed_offsets={"scanner-a": 6})
    assert sum(shadow.add_frame(frame) for frame in initial) == 0
    assert not shadow.promotion_check(current_model_version="model-1")["eligible"]
    changed = deepcopy(initial[-1])
    changed["model_version"] = "model-2"
    changed["observations"][0]["model_version"] = "model-2"
    assert shadow.add_frame(changed) == 0
    assert shadow.state == "cancelled"


def test_shadow_per_device_location_and_bias_regression_gate():
    initial = frames(count=1)
    shadow = ShadowSession(initial, model_version="model-1", proposed_offsets={"scanner-a": 6}, min_labelled_rows=6)
    new = frames(count=10)
    for frame in new[1:]:
        if frame["truth"][0]["location_id"] == "site-a":
            frame["observations"][0]["raw_distance"] = 2
            frame["observations"][0]["filtered_distance"] = 2
        shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert not check["eligible"]
    assert any("regression" in reason and "site-a" in reason for reason in check["reasons"])


def test_shadow_callback_failure_is_cancelled_and_nonmutation():
    baseline = frames(count=1)
    original = deepcopy(baseline)

    def broken(row, elapsed):
        raise RuntimeError("synthetic failure")

    shadow = ShadowSession(baseline, model_version="model-1", filter_callback=broken)
    check = shadow.promotion_check(current_model_version="model-1")
    assert not check["eligible"] and shadow.state == "cancelled"
    assert baseline == original


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "6", 101])
def test_replay_rejects_invalid_candidate_offsets(value):
    with pytest.raises(ValueError):
        ReplayEngine().evaluate(frames(), proposed_offsets={"scanner-a": value})


@pytest.mark.parametrize("mode", ["pet", "stationary", "legacy"])
def test_builtin_filters_have_fresh_independent_repeatable_state(mode):
    captured = frames(count=6)
    captured[3]["observations"][0]["rssi"] -= 20
    captured[3]["observations"][0]["raw_distance"] *= 10
    first = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": mode})
    second = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": mode})
    assert first == second
    assert first["filter_modes"] == {"beacon-a": mode}
    assert first["observations"][0]["rssi"] == captured[0]["observations"][0]["rssi"]
    assert first["observations"][0]["age"] == captured[0]["observations"][0]["age"]
    captured[3]["session_epoch"] += 1
    for frame in captured[4:]:
        frame["session_epoch"] = captured[3]["session_epoch"]
    report = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": mode})
    assert report["observations"][3]["normalized_source_s"] == 0


def test_shadow_new_unlabelled_capture_can_receive_its_instant_label_once():
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 6})
    captured = frames(count=1, labelled=False)[0]
    assert shadow.add_frame(captured) == 1
    labelled = deepcopy(captured)
    labelled["sequence"] = 2
    labelled["captured_at"] += 1
    labelled["observations"][0]["age"] = 1
    labelled["truth"] = [truth(1001)]
    assert shadow.add_frame(labelled) == 1
    report = shadow.evaluate()["report"]
    assert report["counts"]["observations"] == 1
    assert report["counts"]["labelled"] == 1
    assert shadow.summary()["new_frames"] == 1


def test_shadow_receiver_changes_need_coverage_for_each_changed_receiver():
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 6, "scanner-unseen": 6})
    for frame in frames():
        shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert not check["eligible"]
    assert any("scanner-unseen" in reason for reason in check["reasons"])


def test_shadow_per_beacon_regression_cannot_be_hidden_by_other_beacon_improvement():
    history = DiagnosticHistory(enabled=True)
    for index in range(6):
        when = 1000 + index
        labels = [
            truth(when, device=device, location="site-a" if index < 3 else "site-b")
            for device in ("beacon-a", "beacon-b")
        ]
        rows = [observation(when, device="beacon-a", distance=4), observation(when, device="beacon-b", distance=2)]
        history.record(rows, captured_at=when, model_version="model-1", truth=labels)
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 6})
    for frame in history.frames():
        shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert check["new_data_range_metrics"]["candidate"]["mae_m"] < check["new_data_range_metrics"]["current"]["mae_m"]
    assert not check["eligible"]
    assert any("beacon-b" in reason and "regression" in reason for reason in check["reasons"])


def test_shadow_baseline_source_is_not_new_evidence_in_an_independent_recording_epoch():
    initial = frames(count=1)
    initial[0]["session_epoch"] = 2
    shadow = ShadowSession(initial, model_version="model-1", proposed_offsets={"scanner-a": 6})
    same_source = deepcopy(initial[0])
    same_source["session_epoch"] = 1
    assert shadow.add_frame(same_source) == 0
    assert shadow.summary()["new_frames"] == 0
    assert not shadow.promotion_check(current_model_version="model-1")["eligible"]


@pytest.mark.parametrize("field", ["device_id", "scanner_id"])
@pytest.mark.parametrize("identifier", ["aa:bb:cc:dd:ee:12", "Opaque:Source"])
def test_shadow_baseline_alias_exclusion_preserves_opaque_case_and_accepts_fresh_stamps(field, identifier):
    initial = frames(count=1)
    initial[0]["session_epoch"] = 2
    row = initial[0]["observations"][0]
    row[field] = identifier.upper()
    initial[0]["truth"][0].update(device_id=row["device_id"], ranges={row["scanner_id"]: 2})
    original = deepcopy(initial)
    shadow = ShadowSession(initial, model_version="model-1", proposed_offsets={row["scanner_id"]: 6})
    cached = deepcopy(initial[0])
    cached["session_epoch"] = 1
    cached["observations"][0][field] = identifier
    cached_row = cached["observations"][0]
    cached["truth"][0].update(device_id=cached_row["device_id"], ranges={cached_row["scanner_id"]: 2})
    is_mac = identifier.startswith("aa:")
    assert shadow.add_frame(cached) == (0 if is_mac else 1)
    assert shadow.summary()["new_frames"] == (0 if is_mac else 1)
    fresh = deepcopy(cached)
    fresh["sequence"] += 1
    fresh["captured_at"] += 1
    fresh["observations"][0]["stamp"] += 1
    fresh["observations"][0]["observed"] += 1
    fresh["truth"][0]["observed"] += 1
    assert shadow.add_frame(fresh) == 1
    assert shadow.promotion_check(current_model_version="model-1")["new_labelled_rows"] == (1 if is_mac else 2)
    assert initial == original


@pytest.mark.parametrize("alias_field", ["device_id", "scanner_id"])
def test_shadow_mac_alias_can_receive_truth_once_and_cannot_evade_conflicting_relabel_guard(alias_field):
    captured = frames(count=1, labelled=False)[0]
    row = captured["observations"][0]
    row.update(device_id="aa:bb:cc:dd:ee:01", scanner_id="aa:bb:cc:dd:ee:12")
    row[alias_field] = row[alias_field].upper()
    original = deepcopy(captured)
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={row["scanner_id"]: 6})
    assert shadow.add_frame(captured) == 1
    labelled = deepcopy(captured)
    labelled_row = labelled["observations"][0]
    labelled_row[alias_field] = labelled_row[alias_field].lower()
    labelled["truth"] = [{**truth(1000, device=labelled_row["device_id"]), "ranges": {labelled_row["scanner_id"]: 2}}]
    assert shadow.add_frame(labelled) == 1
    assert shadow.summary()["new_frames"] == 1
    report = shadow.evaluate()["report"]
    assert report["counts"]["observations"] == report["counts"]["labelled"] == 1
    assert report["observations"][0][alias_field] == labelled_row[alias_field]
    repeat = deepcopy(labelled)
    repeat["observations"][0][alias_field] = row[alias_field]
    repeat["truth"][0].update(device_id=row["device_id"], ranges={row["scanner_id"]: 2})
    assert shadow.add_frame(repeat) == 0
    assert shadow.state == "active"
    repeat["truth"][0]["location_id"] = "different-site"
    assert shadow.add_frame(repeat) == 0
    assert shadow.state == "cancelled"
    assert "different independent truth" in shadow.cancel_reason
    assert captured == original


def test_shadow_truth_pair_lookup_labels_mac_aliases_without_dropping_their_label():
    captured = frames(count=1, labelled=False)[0]
    row = captured["observations"][0]
    device, scanner = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:12"
    row.update(device_id=device.upper(), scanner_id=scanner.upper())
    captured["observations"].append({**deepcopy(row), "device_id": device, "scanner_id": scanner, "stamp": 1001})
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={scanner: 6})
    assert shadow.add_frame(captured) == 2
    labelled = deepcopy(captured)
    labelled["truth"] = [{**truth(1000, device=device), "ranges": {scanner: 2}}]
    assert shadow.add_frame(labelled) == 2
    assert shadow.summary()["new_frames"] == 1
    assert shadow.evaluate()["report"]["counts"]["labelled"] == 2


@pytest.mark.parametrize("baseline_has_source", [False, True])
def test_shadow_physical_source_alias_cannot_relabel_baseline_as_new_evidence(baseline_has_source):
    source, meta = "aa:bb:cc:dd:ee:01", "ibeacon:physical-one"
    captured = frames(count=3)
    for index, frame in enumerate(captured):
        distance = 2 if index == 0 else 2.2
        frame["session_epoch"] = 2
        row = frame["observations"][0]
        row.update(device_id=meta, source_id=source.upper(), raw_distance=distance, filtered_distance=1)
        row["rssi"] = -59 - 20 * math.log10(distance)
        frame["observations"].append({**deepcopy(row), "scanner_id": "scanner-b"})
        frame["truth"][0].update(device_id=meta, ranges={"scanner-a": distance, "scanner-b": distance})
    baseline = deepcopy(captured[0])
    baseline["session_epoch"] = 1
    for row in baseline["observations"]:
        row["device_id"] = source
        if not baseline_has_source:
            row.pop("source_id")
    baseline["truth"][0]["device_id"] = source
    original = deepcopy(captured)
    shadow = ShadowSession([baseline], model_version="model-1", filter_modes={meta: "pet"})
    assert shadow.add_frame(captured[0]) == 0
    for frame in captured[1:]:
        assert shadow.add_frame(frame) == 2
    check = shadow.promotion_check(current_model_version="model-1")
    assert not check["eligible"]
    assert check["new_labelled_rows"] == 4
    assert check["new_locations"] == ["site-b"]
    fresh = deepcopy(captured[-1])
    fresh["sequence"] += 1
    fresh["captured_at"] += 1
    fresh["truth"][0].update(location_id="another-fresh-site", observed=fresh["captured_at"])
    for row in fresh["observations"]:
        row["stamp"] += 1
        row["observed"] += 1
    assert shadow.add_frame(fresh) == 2
    check = shadow.promotion_check(current_model_version="model-1")
    assert check["eligible"], check["reasons"]
    assert check["new_labelled_rows"] == 6
    assert captured == original


@pytest.mark.parametrize("source", ["aa:bb:cc:dd:ee:01", "Opaque:Source"])
def test_shadow_physical_source_ids_share_only_valid_mac_case_aliases(source):
    baseline = frames(count=1)[0]
    baseline["observations"][0]["source_id"] = source
    incoming = deepcopy(baseline)
    incoming["session_epoch"] += 1
    incoming["observations"][0].update(device_id="ibeacon:alias", source_id=source.upper())
    incoming["truth"][0]["device_id"] = "ibeacon:alias"
    shadow = ShadowSession([baseline], model_version="model-1", proposed_offsets={"scanner-a": 6})
    assert shadow.add_frame(incoming) == (0 if source.startswith("aa:") else 1)
    assert shadow.state == "active"


def test_shadow_source_less_baseline_keeps_logical_exclusion_when_physical_identity_becomes_known():
    baseline = frames(count=1)[0]
    incoming = deepcopy(baseline)
    incoming["session_epoch"] += 1
    incoming["observations"][0]["source_id"] = "aa:bb:cc:dd:ee:01"
    shadow = ShadowSession([baseline], model_version="model-1", proposed_offsets={"scanner-a": 6})
    assert shadow.add_frame(incoming) == 0
    assert shadow.summary()["new_frames"] == 0


def test_shadow_logical_aliases_preserve_model_rows_without_multiplying_receiver_evidence():
    captured = frames(count=6)
    devices = ("ibeacon:first", "private:second", "beacon:third")
    for index, frame in enumerate(captured):
        original = frame["observations"][0]
        original["scanner_id"] = "scanner-a" if index < 3 else "scanner-b"
        frame["observations"] = [
            {**deepcopy(original), "device_id": device, "source_id": "physical-one"} for device in devices
        ]
        frame["truth"] = [
            {
                **truth(frame["captured_at"], device=device, location="site-a" if index % 2 else "site-b"),
                "ranges": {original["scanner_id"]: 2},
            }
            for device in devices
        ]
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 6, "scanner-b": 6})
    for frame in captured:
        assert shadow.add_frame(frame) == 3
    check = shadow.promotion_check(current_model_version="model-1")
    assert check["new_data_range_metrics"]["current"]["count"] == 18
    assert check["new_labelled_rows"] == 6
    assert not check["eligible"]
    assert all(
        any(scanner in reason and "sufficient independent coverage" in reason for reason in check["reasons"])
        for scanner in ("scanner-a", "scanner-b")
    )


@pytest.mark.parametrize("conflict", ["location", "range"])
@pytest.mark.parametrize("same_frame", [False, True])
def test_shadow_physical_packet_cannot_have_conflicting_truth_through_logical_aliases(conflict, same_frame):
    captured = frames(count=1)[0]
    row = captured["observations"][0]
    row["source_id"] = "aa:bb:cc:dd:ee:01"
    alias = {**deepcopy(row), "device_id": "ibeacon:alias", "source_id": row["source_id"].upper()}
    label = {**deepcopy(captured["truth"][0]), "device_id": alias["device_id"]}
    if conflict == "location":
        label["location_id"] = "different-site"
    else:
        label["ranges"]["scanner-a"] = 3
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 6})
    if same_frame:
        captured["observations"].append(alias)
        captured["truth"].append(label)
    else:
        assert shadow.add_frame(captured) == 1
        captured = {**captured, "observations": [alias], "truth": [label]}
    assert shadow.add_frame(captured) == 0
    assert shadow.state == "cancelled"
    assert "physical source observation already has different independent truth" in shadow.cancel_reason


@pytest.mark.parametrize("scanner", ["aa:bb:cc:dd:ee:12", "Opaque:Receiver"])
def test_shadow_truth_ranges_cannot_conflict_for_valid_receiver_mac_aliases(scanner):
    captured = frames(count=1)[0]
    captured["observations"][0]["scanner_id"] = scanner
    captured["truth"][0]["ranges"] = {scanner: 2, scanner.upper(): 3}
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={scanner: 6})
    is_mac = scanner.startswith("aa:")
    assert shadow.add_frame(captured) == (0 if is_mac else 1)
    assert shadow.state == ("cancelled" if is_mac else "active")


def test_missing_live_output_is_not_replaced_by_raw_range_or_promotable():
    captured = frames()
    for frame in captured:
        frame["observations"][0]["filtered_distance"] = None
    report = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": "legacy"})
    assert report["range_metrics"]["raw"] is not None
    assert report["range_metrics"]["current"] is None
    assert report["range_metrics"]["candidate"] is None
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 6})
    for frame in captured:
        shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert not check["eligible"]
    assert any("unavailable" in reason for reason in check["reasons"])


@pytest.mark.parametrize("live_mode", [None, "legacy", "pet", "stationary"])
@pytest.mark.parametrize("explicit_same_mode", [False, True])
def test_shadow_zero_offset_cannot_promote_raw_accuracy_as_unchanged_live_filter_improvement(
    live_mode, explicit_same_mode
):
    captured = frames(distance=10, expected=10)
    for frame in captured:
        row = frame["observations"][0]
        row["filtered_distance"] = 6
        if live_mode is not None:
            row["quality"]["mode"] = live_mode
    modes = {"beacon-a": live_mode or "legacy"} if explicit_same_mode else {}
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 0}, filter_modes=modes)
    for frame in captured:
        shadow.add_frame(frame)
    check = shadow.promotion_check(current_model_version="model-1")
    assert check["new_data_range_metrics"]["raw"]["mae_m"] == 0
    assert check["new_data_range_metrics"]["current"]["mae_m"] == 4
    assert check["new_data_range_metrics"]["candidate"]["mae_m"] == 4
    assert not check["eligible"]
    assert any("improvement" in reason for reason in check["reasons"])
    assert all(
        row["candidate_pipeline"] == "preserved_live_filter" for row in check["evaluation"]["report"]["observations"]
    )


@pytest.mark.parametrize("live_mode", ["legacy", "pet", "stationary"])
@pytest.mark.parametrize("explicit_same_mode", [False, True])
def test_changed_absolute_offset_scales_frozen_current_filter_output(live_mode, explicit_same_mode):
    captured = frames(distance=10, expected=3)
    for frame in captured:
        row = frame["observations"][0]
        row["filtered_distance"] = 6
        row["rssi_offset"] = 4
        row["quality"]["mode"] = live_mode
    proposed = {"scanner-a": 4 + 20 * math.log10(2)}
    modes = {"beacon-a": live_mode} if explicit_same_mode else {}
    report = ReplayEngine().evaluate(captured, proposed_offsets=proposed, filter_modes=modes)
    assert all(row["candidate"] == pytest.approx(3) and row["raw"] == 10 for row in report["observations"])
    assert report["range_metrics"]["candidate"]["mae_m"] < 1e-10
    shadow = ShadowSession([], model_version="model-1", proposed_offsets=proposed, filter_modes=modes)
    for frame in captured:
        shadow.add_frame(frame)
    assert shadow.promotion_check(current_model_version="model-1")["eligible"]


@pytest.mark.parametrize("live_mode", ["pet", "stationary"])
def test_switch_to_legacy_reports_unavailable_history_and_cannot_promote(live_mode):
    captured = frames(distance=10, expected=10)
    for frame in captured:
        row = frame["observations"][0]
        row["filtered_distance"] = 6
        row["quality"]["mode"] = live_mode
    report = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": "legacy"})
    assert report["range_metrics"]["candidate"] is None
    assert all(
        row["candidate"] is None
        and row["candidate_pipeline"] == "unavailable_legacy_history"
        and row["candidate_unavailable_reason"]
        for row in report["observations"]
    )
    shadow = ShadowSession([], model_version="model-1", filter_modes={"beacon-a": "legacy"})
    for frame in captured:
        shadow.add_frame(frame)
    assert not shadow.promotion_check(current_model_version="model-1")["eligible"]


@pytest.mark.parametrize("candidate_mode", ["pet", "stationary"])
def test_explicit_changed_filter_replays_actual_raw_rssi_pipeline(candidate_mode):
    captured = frames(distance=4, expected=4)
    spike = captured[3]["observations"][0]
    spike["rssi"] -= 20
    spike["raw_distance"] *= 10
    report = ReplayEngine().evaluate(captured, filter_modes={"beacon-a": candidate_mode})
    filtered_spike = report["observations"][3]
    assert filtered_spike["raw"] == 40
    assert filtered_spike["candidate"] == pytest.approx(4)
    assert filtered_spike["candidate_pipeline"] == f"replayed_{candidate_mode}_filter"


def test_explicit_callback_always_receives_raw_candidate_instead_of_preserved_filtered_candidate():
    captured = frames(distance=10, expected=5)
    for frame in captured:
        frame["observations"][0]["filtered_distance"] = 6
    report = ReplayEngine().evaluate(
        captured,
        proposed_offsets={"scanner-a": 20 * math.log10(2)},
        filter_callback=lambda row, elapsed: row["candidate_raw_distance"],
    )
    assert all(row["candidate"] == pytest.approx(5) for row in report["observations"])
    assert all(row["candidate_pipeline"] == "explicit_filter_callback" for row in report["observations"])


def test_unchanged_perfect_current_pipeline_has_no_range_improvement_to_promote():
    shadow = ShadowSession([], model_version="model-1", proposed_offsets={"scanner-a": 0})
    for frame in frames(distance=2, expected=2):
        shadow.add_frame(frame)
    assert not shadow.promotion_check(current_model_version="model-1")["eligible"]
