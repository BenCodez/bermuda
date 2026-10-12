"""Live optional RSSI filters and snapshots share the source-clock skew policy."""

from __future__ import annotations

import math

import pytest
from bleak.backends.scanner import AdvertisementData

from custom_components.bermuda import api
from custom_components.bermuda.api import snapshot_observations
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.signal_quality import FilterState, SOURCE_CLOCK_TOLERANCE_S
from tests.test_toolkit_model_rebase import model_runtime
from tests.test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, runtime


def advertisement(rssi=-68):
    return AdvertisementData(
        local_name="Synthetic Clock Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=rssi,
        platform_data=(),
    )


async def optional_advert(runtime, mode, seeded):
    await runtime.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: mode}}})
    runtime.scanner.stamps[DEVICE.upper()] = SOURCE_STAMP
    advert = BermudaAdvert(runtime.device, advertisement(), runtime.coordinator.options, runtime.scanner)
    runtime.device.adverts[(DEVICE, SCANNER)] = advert
    if seeded:
        advert.calculate_data()
        assert advert.rssi_distance is not None
    return advert


def reading(runtime):
    snapshot = runtime.manager._snapshot()
    return snapshot, snapshot_observations(snapshot)[0]


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("skew", [0.01, 0.1, 0.79, SOURCE_CLOCK_TOLERANCE_S])
async def test_small_source_skew_is_accepted_in_live_filter_and_api_without_freshening_cached_polls(
    model_runtime, mode, seeded, skew
):
    runtime = model_runtime
    advert = await optional_advert(runtime, mode, seeded)
    stamp = SOURCE_STAMP + skew
    runtime.scanner.stamps[DEVICE.upper()] = stamp
    advert.update_advertisement(advertisement(), runtime.scanner)
    advert.calculate_data()
    assert advert.new_stamp is None
    assert advert.stamp == stamp
    assert advert.hist_stamp[0] == stamp
    assert advert.rssi_distance == pytest.approx(advert.rssi_distance_raw)
    state = runtime.manager._states[(DEVICE, SCANNER)]
    quality = state.snapshot(SOURCE_STAMP)
    assert quality["status"] == "current"
    assert quality["age"] == 0
    assert quality["last_raw_stamp"] == quality["last_valid_stamp"] == stamp
    assert quality["samples"] == (2 if seeded else 1)
    snapshot, row = reading(runtime)
    assert row["stamp"] == stamp
    assert row["status"] == "current"
    assert row["age"] == 0
    assert row["observed"] <= snapshot["captured"]
    assert row["filtered_distance"] == advert.rssi_distance
    assert row["quality"]["last_valid_stamp"] == stamp
    source_history = list(advert.hist_stamp), list(advert.hist_rssi), list(advert.hist_distance)
    interval_history = list(advert.hist_distance_by_interval)
    samples = quality["samples"]
    for now in (SOURCE_STAMP + 1, SOURCE_STAMP + 2, stamp + 29):
        runtime.clock["now"] = now
        advert.update_advertisement(advertisement(), runtime.scanner)  # same actual source packet
        advert.calculate_data()
        quality = state.snapshot(now)
        assert quality["samples"] == samples
        assert quality["last_valid_stamp"] == stamp
        assert quality["age"] == pytest.approx(now - stamp)
        assert advert.rssi_distance is not None
        assert not quality["fresh"]
    assert (advert.hist_stamp, advert.hist_rssi, advert.hist_distance) == source_history
    assert advert.hist_distance_by_interval == interval_history
    runtime.clock["now"] = stamp + 31
    advert.calculate_data()
    assert advert.rssi_distance is None
    assert state.snapshot(runtime.clock["now"])["last_valid_stamp"] == stamp
    assert state.snapshot(runtime.clock["now"])["samples"] == samples
    _, row = reading(runtime)
    assert row["status"] == "stale"
    assert row["filtered_distance"] is None
    assert advert.stamp == stamp


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("stamp", [math.nextafter(SOURCE_STAMP + SOURCE_CLOCK_TOLERANCE_S, math.inf), 101, 105])
async def test_source_beyond_tolerance_is_rejected_without_adding_or_retrying_measurements(
    model_runtime, mode, seeded, stamp
):
    runtime = model_runtime
    advert = await optional_advert(runtime, mode, seeded)
    previous_distance = advert.rssi_distance
    runtime.scanner.stamps[DEVICE.upper()] = stamp
    advert.update_advertisement(advertisement(-67), runtime.scanner)
    advert.calculate_data()
    state = runtime.manager._states[(DEVICE, SCANNER)]
    quality = state.snapshot(SOURCE_STAMP)
    assert quality["samples"] == (1 if seeded else 0)
    assert quality["last_raw_stamp"] == quality["last_valid_stamp"] == (SOURCE_STAMP if seeded else None)
    assert advert.rssi_distance == previous_distance
    assert advert.stamp == stamp
    assert advert.rssi == -67
    assert advert.new_stamp is None
    assert "future_source_stamp" in {
        reason["code"] for reason in runtime.manager._quality[(DEVICE, SCANNER)]["reasons"]
    }
    _, row = reading(runtime)
    assert row["status"] == "stale"
    assert row["filtered_distance"] is None
    assert "source_clock_in_future" in row["quality"]["rejection_reasons"]
    # Once the poll clock catches up, cached data stays rejected by the filter;
    # no consumed-packet retry creates an observation or advances last-valid time.
    runtime.clock["now"] = stamp + 1
    advert.update_advertisement(advertisement(-67), runtime.scanner)
    advert.calculate_data()
    quality = state.snapshot(runtime.clock["now"])
    assert quality["samples"] == (1 if seeded else 0)
    assert quality["last_valid_stamp"] == (SOURCE_STAMP if seeded else None)
    assert advert.rssi_distance == previous_distance
    runtime.clock["now"] = stamp + 31
    advert.calculate_data()
    assert advert.rssi_distance is None
    assert state.snapshot(runtime.clock["now"])["samples"] == (1 if seeded else 0)


@pytest.mark.parametrize("mode", ["pet", "stationary"])
async def test_distinct_source_packets_inside_one_coarse_tick_keep_actual_stamps_and_cadence(model_runtime, mode):
    runtime = model_runtime
    advert = await optional_advert(runtime, mode, False)
    stamps = [SOURCE_STAMP + step / 10 for step in range(1, 5)]
    for stamp in stamps:
        runtime.scanner.stamps[DEVICE.upper()] = stamp
        advert.update_advertisement(advertisement(), runtime.scanner)
        advert.calculate_data()
    state = runtime.manager._states[(DEVICE, SCANNER)]
    quality = state.snapshot(SOURCE_STAMP)
    assert quality["samples"] == 4
    assert quality["last_valid_stamp"] == stamps[-1]
    assert quality["cadence_s"] == pytest.approx(0.1)
    assert list(state._raw) == [(stamp, -68) for stamp in stamps]
    assert advert.hist_stamp[:4] == list(reversed(stamps))


@pytest.mark.parametrize("now", [100.0, 1234.1, 100000000.4])
@pytest.mark.parametrize("side", [-math.inf, math.inf])
def test_quality_boundary_uses_same_subtraction_predicate_as_api(now, side):
    assert api.SOURCE_CLOCK_TOLERANCE_S == SOURCE_CLOCK_TOLERANCE_S
    stamp = math.nextafter(now + SOURCE_CLOCK_TOLERANCE_S, side)
    age = now - stamp
    expected = age >= -api.SOURCE_CLOCK_TOLERANCE_S
    result = FilterState("pet").process(-68, stamp, now)
    assert result["accepted"] is expected
    assert (result["filtered_rssi"] is not None) is expected
