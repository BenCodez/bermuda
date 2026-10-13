"""Live advert/coordinator hooks tested with small independent toolkit stand-ins."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.helpers import area_registry as ar

from custom_components.bermuda import bermuda_advert as advert_module
from custom_components.bermuda import coordinator as coordinator_module
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.const import (
    CONF_ATTENUATION,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SMOOTHING_SAMPLES,
    HIST_KEEP_COUNT,
)
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
from custom_components.bermuda.signal_quality import FilterState
from custom_components.bermuda.util import rssi_to_metres


def advertisement(rssi=-70):
    return AdvertisementData(
        local_name=None,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )


def make_advert(toolkit=None):
    coordinator = SimpleNamespace(toolkit=toolkit)
    parent = SimpleNamespace(
        _coordinator=coordinator,
        address="opaque:synthetic-beacon",
        ref_power=-59,
        name="Synthetic beacon",
        name_bt_local_name=None,
        create_sensor=True,
        process_manufacturer_data=lambda advert: None,
        make_name=lambda: None,
    )
    scanner = SimpleNamespace(
        address="scanner:synthetic",
        name="Synthetic receiver",
        area_id="area:a",
        area_name="Synthetic room",
        is_remote_scanner=True,
        last_seen=100,
        packet_stamp=100,
    )
    scanner.async_as_scanner_get_stamp = lambda device_id: scanner.packet_stamp
    options = {
        CONF_ATTENUATION: 2.0,
        CONF_REF_POWER: -59,
        CONF_RSSI_OFFSETS: {},
        CONF_MAX_VELOCITY: 3.0,
        CONF_SMOOTHING_SAMPLES: 5,
    }
    return BermudaAdvert(parent, advertisement(), options, scanner)


def test_receiver_beacon_model_changes_raw_conversion_without_overwriting_raw_rssi_or_stamp():
    toolkit = SimpleNamespace(
        effective_advert_model=lambda advert: {"rssi_offset": 5, "reference_power": -61, "attenuation": 3}
    )
    advert = make_advert(toolkit)
    assert advert.rssi == -70
    assert advert.hist_rssi == [-70]
    assert advert.rssi_distance_raw == rssi_to_metres(-65, -61, 3)
    assert advert.hist_distance == [advert.rssi_distance_raw]
    assert advert.stamp == 100
    assert advert.new_stamp == 100
    assert advert.conf_rssi_offset == 0
    assert advert.ref_power == -59
    assert advert.conf_attenuation == 2


@pytest.mark.parametrize("model", [None, {}, {"attenuation": 3}])
def test_absent_or_partial_profile_uses_existing_model_defaults(model):
    toolkit = SimpleNamespace(effective_advert_model=lambda advert: model)
    advert = make_advert(toolkit)
    assert advert.rssi_distance_raw == rssi_to_metres(-70, -59, 3 if model and "attenuation" in model else 2)
    assert advert.rssi == -70


def test_optional_handler_receives_consumed_source_stamp_and_bounds_raw_histories():
    calls = []

    def handle(advert, source_stamp):
        assert advert.new_stamp is None
        calls.append(source_stamp)
        advert.rssi_distance = 1.25
        return True

    advert = make_advert(SimpleNamespace(process_advert=handle))
    for field in ("hist_distance", "hist_rssi", "hist_stamp", "hist_interval", "hist_velocity"):
        setattr(advert, field, list(range(HIST_KEEP_COUNT + 50)))
    raw = advert.rssi_distance_raw
    advert.calculate_data()
    advert.calculate_data()
    assert calls == [100, None]
    assert advert.rssi_distance == 1.25
    assert advert.rssi_distance_raw == raw
    assert advert.rssi == -70
    assert advert.stamp == 100
    assert advert.new_stamp is None
    assert all(
        len(getattr(advert, field)) == HIST_KEEP_COUNT
        for field in ("hist_distance", "hist_rssi", "hist_stamp", "hist_interval", "hist_velocity")
    )


@pytest.mark.parametrize("hook_result", [False, None, 1, {"handled": True}])
def test_only_literal_true_handles_cycle_otherwise_legacy_arrival_and_expiry(hook_result, monkeypatch):
    toolkit = SimpleNamespace(process_advert=lambda advert, stamp: hook_result)
    advert = make_advert(toolkit)
    monkeypatch.setattr(advert_module, "monotonic_time_coarse", lambda: 100)
    advert.calculate_data()
    assert advert.rssi_distance == advert.rssi_distance_raw
    monkeypatch.setattr(advert_module, "monotonic_time_coarse", lambda: 131)
    advert.calculate_data()
    assert advert.rssi_distance is None
    assert advert.hist_distance_by_interval == []
    assert advert.stamp == 100


def test_stationary_live_path_rejects_spike_accepts_step_and_expires_cached_packets(monkeypatch):
    clock = {"now": 100.0}
    state = FilterState("stationary")

    def process(advert, stamp):
        if stamp is None:
            quality = state.snapshot(clock["now"])
        else:
            quality = state.process(advert.rssi, stamp, clock["now"], raw_distance=advert.rssi_distance_raw)
        advert.signal_quality = quality
        filtered = quality["filtered_rssi"]
        advert.rssi_distance = rssi_to_metres(filtered, advert.ref_power, advert.conf_attenuation) if filtered else None
        return True

    toolkit = SimpleNamespace(process_advert=process)
    advert = make_advert(toolkit)
    monkeypatch.setattr(advert_module, "monotonic_time_coarse", lambda: clock["now"])

    def packet(stamp, rssi):
        clock["now"] = stamp
        advert.scanner_device.packet_stamp = stamp
        advert.update_advertisement(advertisement(rssi), advert.scanner_device)
        advert.calculate_data()

    advert.calculate_data()
    for stamp in range(101, 110):
        packet(stamp, -70)
    packet(110, -40)
    assert advert.rssi == -40
    assert advert.rssi_distance_raw == rssi_to_metres(-40, -59, 2)
    assert advert.rssi_distance == rssi_to_metres(-70, -59, 2)
    assert advert.signal_quality["last_valid_stamp"] == 109
    for stamp in range(111, 114):
        packet(stamp, -40)
    assert advert.rssi_distance == advert.rssi_distance_raw
    for poll in range(114, 145):
        clock["now"] = poll
        # Even Bluetooth's cached advertisement callback must retain source age.
        advert.update_advertisement(advertisement(-40), advert.scanner_device)
        advert.calculate_data()
    assert advert.rssi_distance is None
    assert advert.rssi_distance_raw == rssi_to_metres(-40, -59, 2)
    assert advert.stamp == 113
    assert advert.signal_quality["last_valid_stamp"] == 113


class AreaDevice(SimpleNamespace):
    def _update_area_and_floor(self, area_id):
        area = self.registry.async_get_area(area_id) if area_id is not None else None
        self.area_id = area_id
        self.area_name = area.name if area else None
        self.area_icon = area.icon if area else None

    def apply_scanner_selection(self, advert):
        self.area_advert = advert
        self._update_area_and_floor(advert.area_id if advert else None)
        self.area_distance = advert.rssi_distance if advert else None
        self.area_rssi = advert.rssi if advert else None


def contender(scanner, area, distance, *, history=None, stamp=100):
    return SimpleNamespace(
        scanner_address=scanner,
        scanner_device=SimpleNamespace(last_seen=100),
        area_id=area.id,
        area_name=area.name,
        name=scanner,
        stamp=stamp,
        rssi=-70,
        rssi_distance=distance,
        rssi_distance_raw=distance + 0.125,
        hist_distance_by_interval=[distance] if history is None else history,
    )


def area_case(hass, monkeypatch, *, toolkit=None, global_radius=20):
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: 100)
    registry = ar.async_get(hass)
    a = registry.async_create("Synthetic room A")
    b = registry.async_create("Synthetic room B")
    coordinator = SimpleNamespace(
        options={CONF_MAX_RADIUS: global_radius},
        toolkit=toolkit,
        ar=registry,
        AreaTests=BermudaDataUpdateCoordinator.AreaTests,
    )
    device = AreaDevice(name="Synthetic beacon", area_advert=None, adverts={}, registry=registry)
    return coordinator, device, a, b


@pytest.mark.parametrize("enabled", [False, True])
def test_default_area_hysteresis_and_incumbent_global_radius_behavior_unchanged(hass, monkeypatch, enabled):
    toolkit = (
        SimpleNamespace(receiver_limit=lambda scanner, radius: radius, receiver_has_limit=lambda scanner: False)
        if enabled
        else None
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    incumbent = contender("scanner:incumbent", area_a, 25)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is incumbent  # legacy does not globally cap incumbent
    assert device.area_distance == 25
    incumbent.rssi_distance = 10
    challenger = contender("scanner:challenger", area_b, 9)
    device.adverts["challenger"] = challenger
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is incumbent  # 10 vs 9 is inside legacy hysteresis
    challenger.rssi_distance = 5
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is challenger


@pytest.mark.parametrize("scoped_radius", [10, 20])
def test_explicit_receiver_radius_evicts_incumbent_even_equal_global(hass, monkeypatch, scoped_radius):
    toolkit = SimpleNamespace(
        receiver_limit=lambda scanner, radius: scoped_radius,
        receiver_has_limit=lambda scanner: True,
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    incumbent = contender("scanner:incumbent", area_a, 25)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is None
    assert incumbent.rssi_distance == 25
    assert incumbent.rssi_distance_raw == 25.125


def test_receiver_radius_rejects_challenger_and_allows_explicit_wider_scope(hass, monkeypatch):
    limits = {"scanner:challenger": 4}
    toolkit = SimpleNamespace(
        receiver_limit=lambda scanner, radius: limits.get(scanner, radius),
        receiver_has_limit=lambda scanner: scanner in limits,
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    incumbent = contender("scanner:incumbent", area_a, 10)
    challenger = contender("scanner:challenger", area_b, 5)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent, "challenger": challenger}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is incumbent
    limits["scanner:challenger"] = 40
    challenger.rssi_distance = 30
    incumbent.rssi_distance = None
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is challenger
    assert device.area_distance == 30
    assert challenger.rssi_distance_raw == 5.125


def test_receiver_cap_cannot_be_bypassed_by_reliability_weight(hass, monkeypatch):
    toolkit = SimpleNamespace(
        receiver_limit=lambda scanner, radius: 4,
        receiver_has_limit=lambda scanner: True,
        weighted_distance=lambda advert, distance: distance / 10,
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    challenger = contender("scanner:challenger", area_b, 5)
    device.adverts = {"challenger": challenger}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is None
    assert challenger.rssi_distance == 5


def test_reliability_weight_changes_comparison_without_mutating_distance_outputs(hass, monkeypatch):
    toolkit = SimpleNamespace(
        weighted_distance=lambda advert, distance: distance * (3 if advert.area_id.endswith("a") else 1)
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    incumbent = contender("scanner:incumbent", area_a, 5)
    challenger = contender("scanner:challenger", area_b, 7)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent, "challenger": challenger}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is challenger
    assert device.area_distance == 7
    assert challenger.rssi_distance == 7
    assert challenger.rssi_distance_raw == 7.125
    assert incumbent.rssi_distance == 5
    assert incumbent.hist_distance_by_interval == [5]


def test_reliability_weight_applies_to_historical_hysteresis_comparisons(hass, monkeypatch):
    toolkit = SimpleNamespace(
        weighted_distance=lambda advert, distance: distance * (2 if advert.area_id.endswith("a") else 1)
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    incumbent = contender("scanner:incumbent", area_a, 5, history=[5] * 5)
    challenger = contender("scanner:challenger", area_b, 8, history=[8] * 5)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent, "challenger": challenger}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is challenger
    assert "historical min/max" in device.diag_area_switch
    assert incumbent.hist_distance_by_interval == [5] * 5
    assert challenger.hist_distance_by_interval == [8] * 5


@pytest.mark.parametrize("invalid_weight", [True, None, float("nan"), float("inf"), -1, 0])
def test_bad_weight_hook_output_falls_back_to_existing_distance(hass, monkeypatch, invalid_weight):
    toolkit = SimpleNamespace(weighted_distance=lambda advert, distance: invalid_weight)
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    incumbent = contender("scanner:incumbent", area_a, 5)
    challenger = contender("scanner:challenger", area_b, 7)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent, "challenger": challenger}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_advert is incumbent


@pytest.mark.parametrize(
    "enabled,accepted,confidence,valid_area,suggestion_enabled,should_override",
    [
        (False, True, 0.99, True, True, False),
        (True, False, 0.99, True, True, False),
        (True, True, 0.85, True, True, False),
        (True, True, 0.99, False, True, False),
        (True, True, float("nan"), True, True, False),
        (True, True, True, True, True, False),
        (True, True, 1.01, True, True, False),
        (True, True, 0.99, True, False, False),
        (True, True, 0.9, True, True, True),
    ],
)
def test_fingerprint_is_advisory_until_explicit_high_confidence_valid_area_override(
    hass, monkeypatch, enabled, accepted, confidence, valid_area, suggestion_enabled, should_override
):
    calls = []
    suggestion = {"accepted": accepted, "confidence": confidence, "enabled": suggestion_enabled}
    toolkit = SimpleNamespace(
        fingerprint_area_selection=enabled,
        area_suggestion=lambda device: calls.append(device) or suggestion,
    )
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    suggestion["suggested_area"] = area_b.id if valid_area else "missing:area"
    incumbent = contender("scanner:incumbent", area_a, 5)
    device.area_advert = incumbent
    device.adverts = {"incumbent": incumbent}
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert calls == [device]
    assert device.area_id == (area_b.id if should_override else area_a.id)
    assert device.area_advert is incumbent
    assert incumbent.rssi_distance == 5
    assert incumbent.rssi_distance_raw == 5.125
    assert incumbent.stamp == 100
    assert device.area_distance == (None if should_override else 5)
    if should_override:
        assert device.area_last_seen_id == area_b.id


def test_fingerprint_can_suggest_area_without_a_local_scanner_but_cannot_invent_range(hass, monkeypatch):
    suggestion = {"accepted": True, "confidence": 0.99}
    toolkit = SimpleNamespace(fingerprint_area_selection=True, area_suggestion=lambda device: suggestion)
    coordinator, device, area_a, area_b = area_case(hass, monkeypatch, toolkit=toolkit)
    suggestion["suggested_area"] = area_b.id
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coordinator, device)
    assert device.area_id == area_b.id
    assert device.area_advert is None
    assert device.area_distance is None
    assert device.area_rssi is None


def update_coordinator(toolkit, events, *, skip=None, failure=None):
    def gather():
        events.append("gather")
        return True

    def prune():
        events.append("prune")
        if failure:
            raise RuntimeError("synthetic failure")

    device = SimpleNamespace(create_sensor=False, calculate_data=lambda: events.append("calculate"))
    return SimpleNamespace(
        toolkit=toolkit,
        _waitingfor_load_manufacturer_ids=skip == "waiting",
        update_in_progress=skip == "busy",
        devices={"opaque:synthetic": device},
        options={},
        _async_gather_advert_data=gather,
        update_metadevices=lambda: events.append("metadevices"),
        _refresh_areas_by_min_distance=lambda: events.append("areas"),
        prune_devices=prune,
    )


def test_completed_update_duration_hook_runs_after_calculation_area_and_prune(monkeypatch):
    events = []
    timings = iter([10, 10.025])
    monkeypatch.setattr(coordinator_module, "perf_counter", lambda: next(timings))

    def after_update(duration):
        assert events == ["gather", "metadevices", "calculate", "areas", "prune"]
        assert coordinator.update_in_progress is False
        assert coordinator.last_update_success is True
        assert duration == pytest.approx(0.025)
        events.append("completed")

    coordinator = update_coordinator(SimpleNamespace(after_update=after_update), events)
    assert BermudaDataUpdateCoordinator._async_update_data_internal(coordinator) is True
    assert events[-1] == "completed"


@pytest.mark.parametrize("skip", ["waiting", "busy"])
def test_skipped_update_does_not_schedule_toolkit_work(skip):
    events = []
    toolkit = SimpleNamespace(after_update=lambda duration: events.append("completed"))
    coordinator = update_coordinator(toolkit, events, skip=skip)
    BermudaDataUpdateCoordinator._async_update_data_internal(coordinator)
    assert events == []


def test_failed_update_cleans_lock_without_reporting_completed_toolkit_batch():
    events = []
    toolkit = SimpleNamespace(after_update=lambda duration: events.append("completed"))
    coordinator = update_coordinator(toolkit, events, failure=True)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        BermudaDataUpdateCoordinator._async_update_data_internal(coordinator)
    assert coordinator.update_in_progress is False
    assert "completed" not in events
