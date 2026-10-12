"""Synthetic packet sequences for optional signal filtering and range quality."""

import math

import pytest

from custom_components.bermuda.signal_quality import FilterState, receiver_quality


def reason_codes(result):
    return {reason["code"] for reason in result["reasons"]}


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("spike", [-100, -30])
def test_isolated_spike_preserves_raw_and_last_valid_timestamp(mode, spike):
    state = FilterState(mode)
    for stamp in range(10):
        state.process(-65, stamp, stamp, observed=1000 + stamp)
    result = state.process(spike, 10, 10, observed=1010)
    assert result["raw_rssi"] == spike
    assert result["filtered_rssi"] == -65
    assert result["fresh"] and not result["accepted"]
    assert result["last_raw_stamp"] == 10
    assert result["last_valid_stamp"] == 9
    assert result["last_raw_observed"] == 1010
    assert result["last_valid_observed"] == 1009
    assert result["age"] == 1
    assert "unconfirmed_jump" in reason_codes(result)
    recovered = state.process(-65, 11, 11)
    assert recovered["filtered_rssi"] == -65
    assert recovered["last_valid_stamp"] == 11


@pytest.mark.parametrize("mode,confirmations", [("pet", 3), ("stationary", 4)])
@pytest.mark.parametrize("target", [-40, -100])
def test_hundred_sample_real_step_adapts_within_bounded_fresh_samples(mode, confirmations, target):
    state = FilterState(mode)
    for stamp in range(10):
        state.process(-65, stamp, stamp)
    results = [state.process(target, stamp, stamp) for stamp in range(10, 110)]
    assert all(result["filtered_rssi"] == -65 for result in results[: confirmations - 1])
    assert results[confirmations - 1]["filtered_rssi"] == target
    assert "sustained_step_accepted" in reason_codes(results[confirmations - 1])
    assert results[-1]["filtered_rssi"] == target
    assert results[-1]["raw_rssi"] == target
    assert results[-1]["noise_db"] == 0
    assert results[-1]["reliability"] == pytest.approx(1)


def test_stationary_cached_packet_departure_does_not_extend_freshness_or_confirm_jump():
    state = FilterState("stationary")
    for stamp in range(8):
        state.process(-60, stamp, stamp)
    state.process(-30, 8, 8)
    for now in range(9, 39):
        result = state.process(-30, 8, now)
        assert not result["fresh"]
        assert result["last_valid_stamp"] == 7
        assert result["samples"] == 9
        assert "duplicate_stamp" in reason_codes(result)
    assert result["filtered_rssi"] is None
    assert result["status"] == "stale"
    assert result["reliability"] == 0
    assert "accepted_data_timeout" in reason_codes(result)


def test_noise_and_cadence_use_timestamped_raw_packets_not_polling():
    state = FilterState("pet")
    values = [-62, -58, -62, -58, -62]
    for value, stamp in zip(values, range(0, 50, 10), strict=True):
        state.process(value, stamp, stamp)
        for poll in range(stamp + 1, stamp + 10):
            state.snapshot(poll)
    result = state.snapshot(50)
    assert result["samples"] == 5
    assert result["cadence_s"] == 10
    assert result["timeout_s"] == 40
    assert result["variance_db2"] == pytest.approx(3.84)
    assert result["noise_db"] == pytest.approx(math.sqrt(3.84))
    assert result["last_valid_stamp"] == 40
    assert result["age"] == 10
    assert state.snapshot(81)["filtered_rssi"] is None


def test_cadence_timeout_bounded_and_legacy_timeout_unchanged():
    for mode in ("legacy", "pet", "stationary"):
        state = FilterState(mode)
        for stamp in (0, 10, 20, 30, 60, 90, 120, 150, 180):
            result = state.process(-60, stamp, stamp)
        assert result["timeout_s"] == (30 if mode == "legacy" else 120)
        assert "online" not in result
        assert "proxy_online" not in result


def test_time_aware_smoothing_uses_elapsed_packet_time():
    short = FilterState("stationary")
    long = FilterState("stationary")
    short.process(-60, 0, 0)
    long.process(-60, 0, 0)
    short_result = short.process(-68, 0.1, 0.1)
    long_result = long.process(-68, 8, 8)
    assert short_result["filtered_rssi"] == pytest.approx(-60 - 8 * (1 - math.exp(-0.1 / 8)))
    assert long_result["filtered_rssi"] == pytest.approx(-60 - 8 * (1 - math.exp(-1)))
    assert long_result["filtered_rssi"] < short_result["filtered_rssi"]


def test_dropout_reseeds_from_actual_packet_and_snapshot_does_not_create_data():
    state = FilterState("stationary")
    for stamp in range(10):
        state.process(-60, stamp, stamp)
    for now in (10, 20, 40, 100):
        result = state.snapshot(now)
        assert result["samples"] == 10
        assert result["last_raw_stamp"] == 9
    result = state.process(-90, 101, 101)
    assert result["accepted"]
    assert result["filtered_rssi"] == -90
    assert result["samples"] == 1
    assert result["last_valid_stamp"] == 101
    assert "reseed_after_gap" in reason_codes(result)


def test_candidate_jumps_separated_by_packet_gap_do_not_count_as_sustained():
    state = FilterState("pet")
    for stamp in range(5):
        state.process(-60, stamp, stamp)
    state.process(-30, 5, 5)
    result = state.process(-30, 20, 20)
    assert not result["accepted"]
    assert "jump_confirmation_gap" in reason_codes(result)
    assert state.process(-30, 21, 21)["filtered_rssi"] == -60
    assert state.process(-30, 22, 22)["filtered_rssi"] == -30


def test_out_of_order_source_and_clock_regression_cannot_rewrite_state():
    state = FilterState("pet")
    state.process(-60, 10, 10)
    result = state.process(-90, 9, 11)
    assert not result["fresh"]
    assert "out_of_order_stamp" in reason_codes(result)
    assert result["last_raw_stamp"] == 10
    assert result["samples"] == 1
    state.snapshot(20)
    result = state.process(-90, 12, 12)
    assert "current_clock_regression" in reason_codes(result)
    assert result["filtered_rssi"] is None
    assert result["reliability"] == 0
    assert state.snapshot(21)["filtered_rssi"] == -60


def test_explicit_reset_allows_new_source_clock_without_old_cadence_or_filter():
    state = FilterState("stationary")
    for stamp in range(100, 140, 10):
        state.process(-60, stamp, stamp)
    assert state.cadence_s == 10
    state.reset("pet")
    result = state.process(-90, 0, 0)
    assert result["filtered_rssi"] == -90
    assert result["last_valid_stamp"] == 0
    assert result["age"] == 0
    assert result["samples"] == 1
    assert result["cadence_s"] is None
    assert result["mode"] == "pet"


@pytest.mark.parametrize("field", ["rssi", "stamp", "now", "observed"])
@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"), "-60", 10**1000])
def test_invalid_numeric_observations_cannot_enter_state(field, invalid):
    state = FilterState("pet")
    fields = {"rssi": -60, "stamp": 1, "now": 1, "observed": 1001}
    fields[field] = invalid
    result = state.process(**fields)
    assert not result["fresh"]
    assert result["last_raw_stamp"] is None
    assert result["samples"] == 0
    assert "invalid_observation" in reason_codes(result)


@pytest.mark.parametrize("stamp,now,reason", [(5, 0, "future_source_stamp"), (1, 60, "stale_observation")])
def test_future_and_already_stale_packets_are_not_accepted(stamp, now, reason):
    state = FilterState("pet")
    result = state.process(-60, stamp, now)
    assert not result["fresh"]
    assert reason in reason_codes(result)
    assert result["filtered_rssi"] is None


def test_legacy_quality_is_raw_pass_through_and_does_not_modify_input():
    state = FilterState()
    observation = {"rssi": -100, "stamp": 1, "now": 1}
    state.process(-60, 0, 0)
    assert state.process(**observation)["filtered_rssi"] == -100
    assert observation == {"rssi": -100, "stamp": 1, "now": 1}
    assert state.snapshot(32)["filtered_rssi"] is None


def test_history_and_filter_state_are_bounded():
    state = FilterState("pet", window_size=5)
    for stamp in range(1000):
        result = state.process(-60, stamp, stamp)
    assert result["samples"] == 5
    assert len(state._intervals) == 5
    assert len(state._accepted) == 5


def test_measured_range_accuracy_covariance_and_reliability_are_distinct_from_position():
    quality = receiver_quality(noise_db=0, distance=10, attenuation=2, expected_distance=5, expected_accuracy_m=1)
    assert quality["range_error_m"] == 5
    assert quality["within_expected_accuracy"] is False
    assert quality["distance_variance_m2"] == 16
    assert 0 < quality["accuracy_reliability"] < 1
    assert "position_accuracy" not in quality
    extreme = receiver_quality(noise_db=170, distance=1e6, attenuation=0.1)
    assert 0 < extreme["distance_variance_m2"] <= 1e12
    assert math.isfinite(extreme["distance_variance_m2"])
    unknown = receiver_quality(noise_db=None, distance=10)
    assert unknown["distance_variance_m2"] > 0
    assert unknown["range_error_m"] is None


@pytest.mark.parametrize("distance,attenuation", [(True, 2), (float("nan"), 2), (1, False), (1, 0)])
def test_invalid_physical_inputs_do_not_claim_covariance(distance, attenuation):
    quality = receiver_quality(noise_db=4, distance=distance, attenuation=attenuation)
    assert quality["distance_variance_m2"] is None


def test_filter_reports_accuracy_effect_separately_and_keeps_raw_value():
    state = FilterState("pet")
    for stamp in range(5):
        result = state.process(-60, stamp, stamp, raw_distance=10, expected_distance=5, expected_accuracy_m=1)
    assert result["raw_rssi"] == -60
    assert result["range_error_m"] == 5
    assert result["reliability"] < 0.1


@pytest.mark.parametrize("mode,window", [("moving", 32), ({}, 32), ("pet", True), ("pet", 1), ("pet", 1000)])
def test_invalid_policy_configuration_rejected(mode, window):
    with pytest.raises(ValueError):
        FilterState(mode, window_size=window)
