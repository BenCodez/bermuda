"""Live fingerprint, API and history clocks agree on real synthetic packets."""

from __future__ import annotations

import math
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from bleak.backends.scanner import AdvertisementData

from custom_components.bermuda import api as api_module
from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.api import async_get_advert_snapshot, snapshot_observations
from custom_components.bermuda.sensor import BermudaSensorAreaSuggestion
from custom_components.bermuda.signal_quality import SOURCE_CLOCK_TOLERANCE_S, normalize_source_age

from .test_fingerprint_live_freshness import PATTERN_A, PATTERN_B, SECOND_SCANNER, _receive_packet
from .test_toolkit_runtime import DEVICE, SOURCE_STAMP, _add_receiver, runtime


@pytest.fixture(params=["legacy", "pet", "stationary"])
async def trained_runtime(runtime, monkeypatch, request):
    clock = SimpleNamespace(mono=SOURCE_STAMP, epoch=time.time())
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device", "coordinator"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: clock.mono)
    monkeypatch.setattr(api_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    monkeypatch.setattr(toolkit_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    _add_receiver(runtime.coordinator, runtime.device, SECOND_SCANNER, clock.mono)
    settings = {"fingerprint_area_selection": True, "filters": {DEVICE: request.param}}
    await runtime.manager.async_dispatch("settings", {"settings": settings})
    runtime.clock, runtime.mode = clock, request.param
    runtime.area_a = runtime.coordinator.ar.async_create("Synthetic Source Clock Area A")
    runtime.area_b = runtime.coordinator.ar.async_create("Synthetic Source Clock Area B")
    for area, pattern in ((runtime.area_a, PATTERN_A), (runtime.area_b, PATTERN_B)):
        for _ in range(5):
            _receive_packet(runtime, clock, pattern)
            result = await runtime.manager.async_dispatch(
                "fingerprint_train", {"device_id": DEVICE, "location_id": area.id}
            )
            assert result["accepted"] is True
        assert result["ready"] is True
    runtime.advisory = BermudaSensorAreaSuggestion(runtime.coordinator, runtime.entry, DEVICE)
    return runtime


def _deliver(runtime, skew, *, pattern=PATTERN_A, receivers=None):
    runtime.clock.mono += 1
    runtime.clock.epoch += 1
    stamp = (
        math.nextafter(runtime.clock.mono + SOURCE_CLOCK_TOLERANCE_S, math.inf)
        if skew == "beyond_boundary"
        else runtime.clock.mono + skew
    )
    for scanner_id, rssi in pattern.items():
        if receivers is not None and scanner_id not in receivers:
            continue
        scanner = runtime.coordinator.devices[scanner_id]
        scanner.stamps[DEVICE.upper()] = stamp
        scanner.last_seen = stamp
        advert = runtime.device.get_scanner(scanner_id)
        advert.update_advertisement(
            AdvertisementData(
                local_name="Synthetic Source Clock Beacon",
                manufacturer_data={},
                service_data={},
                service_uuids=[],
                tx_power=-59,
                rssi=rssi,
                platform_data=(),
            ),
            scanner,
        )
        advert.calculate_data()
        assert advert.stamp == stamp
    return stamp


def _snapshot(runtime):
    return async_get_advert_snapshot(runtime.hass, entry_id=runtime.entry.entry_id, include_history=True)


def _source_state(runtime):
    return {
        scanner: {
            "stamp": advert.stamp,
            "new_stamp": advert.new_stamp,
            "rssi": advert.rssi,
            "hist_stamp": list(advert.hist_stamp),
            "hist_rssi": list(advert.hist_rssi),
        }
        for (_source, scanner), advert in runtime.device.adverts.items()
    }


@pytest.mark.parametrize("skew", [0, 0.01, 0.1, 0.79, SOURCE_CLOCK_TOLERANCE_S])
async def test_supported_source_skew_agrees_across_api_live_advice_sensor_selection_and_history(trained_runtime, skew):
    runtime, manager = trained_runtime, trained_runtime.manager
    stamp = _deliver(runtime, skew)
    snapshot = _snapshot(runtime)
    rows = snapshot_observations(snapshot)
    for row in rows:
        assert row["status"] == "current"
        assert row["stamp"] == stamp
        assert row["age"] == 0
        assert row["observed"] == snapshot["captured"]
    api_result = manager.fingerprints.suggest(DEVICE, rows, now=snapshot["captured"])
    current = manager.area_suggestion(runtime.device)
    assert api_result["accepted"] is current["accepted"] is True
    assert api_result["suggested_area"] == current["suggested_area"] == runtime.area_a.id
    assert api_result["confidence"] == current["confidence"] == 1
    assert runtime.advisory.native_value == runtime.area_a.name
    runtime.coordinator._refresh_areas_by_min_distance()
    assert runtime.device.area_id == runtime.area_a.id
    state = await manager.async_dispatch("state", {})
    assert state["fingerprint_results"][DEVICE]["suggested_area"] == runtime.area_a.id
    for reading in snapshot["devices"][DEVICE]["scanners"].values():
        newest = reading["history"][0]
        assert newest["stamp"] == stamp
        assert newest["age"] == 0
        assert newest["observed"] == snapshot["captured"]
        assert all(row["observed"] <= snapshot["captured"] for row in reading["history"])

    # Advice/selection and cached packet polls must not learn or refresh source
    # evidence. The same clock skew packet remains usable once clocks catch up.
    model_before, source_before = manager.fingerprints.export_state(), _source_state(runtime)
    samples_before = {key: len(value._raw) for key, value in manager._states.items()}
    runtime.clock.mono = stamp + 1
    runtime.clock.epoch += 2
    for advert in runtime.device.adverts.values():
        advert.calculate_data()
    current = manager.area_suggestion(runtime.device)
    assert current["accepted"] is True and current["suggested_area"] == runtime.area_a.id
    assert manager.fingerprints.export_state() == model_before
    assert _source_state(runtime) == source_before
    assert {key: len(value._raw) for key, value in manager._states.items()} == samples_before
    # Fingerprint coverage expires at its independent 15-second input limit.
    runtime.clock.mono = stamp + 16
    runtime.clock.epoch += 15
    assert manager.area_suggestion(runtime.device)["accepted"] is False
    assert runtime.advisory.native_value is None
    runtime.coordinator._refresh_areas_by_min_distance()
    assert runtime.device.area_id is None
    assert manager.fingerprints.export_state() == model_before
    assert _source_state(runtime) == source_before
    assert all(row["status"] == "current" for row in snapshot_observations(_snapshot(runtime)))
    # Range freshness has a separate, longer timeout and also expires without
    # any source history or accepted-filter timestamp being manufactured.
    runtime.clock.mono = stamp + 121
    runtime.clock.epoch += 105
    for advert in runtime.device.adverts.values():
        advert.calculate_data()
    assert all(row["status"] == "stale" for row in snapshot_observations(_snapshot(runtime)))
    assert _source_state(runtime) == source_before


@pytest.mark.parametrize("skew", ["beyond_boundary", 1, 5])
async def test_unsupported_future_source_is_rejected_by_live_advice_and_api_without_cached_result_reuse(
    trained_runtime, skew
):
    runtime, manager = trained_runtime, trained_runtime.manager
    cached = deepcopy(manager.area_suggestion(runtime.device))
    assert cached["accepted"] is True and cached["suggested_area"] == runtime.area_b.id
    stamp = _deliver(runtime, skew)
    before = _source_state(runtime)
    model_before = manager.fingerprints.export_state()
    snapshot = _snapshot(runtime)
    rows = snapshot_observations(snapshot)
    assert all(row["status"] == "stale" and row["age"] < -SOURCE_CLOCK_TOLERANCE_S for row in rows)
    assert all(row["observed"] is None and row["stamp"] == stamp for row in rows)
    assert not manager.fingerprints.suggest(DEVICE, rows, now=snapshot["captured"])["accepted"]
    result = manager.area_suggestion(runtime.device)
    assert result["accepted"] is False and result["suggested_area"] is None
    assert result != cached
    assert runtime.advisory.native_value is None
    runtime.coordinator._refresh_areas_by_min_distance()
    assert runtime.device.area_id is None
    panel = await manager.async_dispatch("state", {})
    assert panel["fingerprint_results"][DEVICE]["accepted"] is False
    for reading in snapshot["devices"][DEVICE]["scanners"].values():
        assert reading["history"][0]["stamp"] == stamp
        assert reading["history"][0]["observed"] is None
        assert reading["history"][0]["age"] < -SOURCE_CLOCK_TOLERANCE_S
    assert _source_state(runtime) == before
    assert manager.fingerprints.export_state() == model_before


async def test_missing_second_receiver_keeps_fingerprint_unknown_even_with_supported_new_source_skew(trained_runtime):
    runtime = trained_runtime
    model_before = runtime.manager.fingerprints.export_state()
    runtime.clock.mono += 16
    runtime.clock.epoch += 16
    _deliver(runtime, 0.1, receivers={next(iter(PATTERN_A))})
    current = runtime.manager.area_suggestion(runtime.device)
    assert current["accepted"] is False
    assert {reason["code"] for reason in current["reasons"]} >= {
        "stale_receiver",
        "insufficient_receiver_coverage",
    }
    assert runtime.advisory.native_value is None
    assert runtime.manager.fingerprints.export_state() == model_before


async def test_unsupported_future_receiver_stays_ineligible_while_two_current_receivers_supply_coverage(
    trained_runtime,
):
    runtime = trained_runtime
    third_scanner = "aa:bb:cc:dd:ee:33"
    _add_receiver(runtime.coordinator, runtime.device, third_scanner, runtime.clock.mono)
    _deliver(runtime, 0.1)
    bad_stamp = _deliver(runtime, "beyond_boundary", pattern={third_scanner: -70})
    before = _source_state(runtime)
    snapshot = _snapshot(runtime)
    rows = snapshot_observations(snapshot)
    invalid = next(row for row in rows if row["scanner_id"] == third_scanner)
    assert invalid["stamp"] == bad_stamp
    assert invalid["age"] < -SOURCE_CLOCK_TOLERANCE_S
    assert invalid["status"] == "stale"
    assert invalid["observed"] is None
    current_rows = [row for row in rows if row["status"] == "current"]
    assert len(current_rows) == 2
    from_api = runtime.manager.fingerprints.suggest(DEVICE, current_rows, now=snapshot["captured"])
    live = runtime.manager.area_suggestion(runtime.device)
    assert from_api["accepted"] is live["accepted"] is True
    assert from_api["confidence"] == live["confidence"] == 1
    assert live["suggested_area"] == runtime.area_a.id
    assert "stale_receiver" in {reason["code"] for reason in live["reasons"]}
    assert runtime.advisory.native_value == runtime.area_a.name
    runtime.coordinator._refresh_areas_by_min_distance()
    assert runtime.device.area_id == runtime.area_a.id
    assert _source_state(runtime) == before


@pytest.mark.parametrize("age", [-0.8, -0.1, 0, 1, 15, 121])
def test_shared_source_age_normalization_keeps_supported_semantics(age):
    assert normalize_source_age(age) == max(0, age)


@pytest.mark.parametrize("age", [math.nextafter(-0.8, -math.inf), -1, True, None, math.nan, math.inf, "0"])
def test_shared_source_age_normalization_rejects_unsupported_values(age):
    assert normalize_source_age(age) is None
