"""Reference Power Numbers retain configured election policy in legacy mode."""

from __future__ import annotations

from copy import deepcopy

import pytest

from custom_components.bermuda.api import async_get_advert_snapshot
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.calibration import range_from_rssi
from custom_components.bermuda.const import AREA_MAX_AD_AGE
from custom_components.bermuda.number import BermudaNumber
from custom_components.bermuda.sensor import BermudaSensorRange, BermudaSensorScannerRange
from tests.test_number_filtered_power import SECOND_SCANNER, advertisement, bind_area, change_number
from tests.test_toolkit_model_rebase import model_runtime, packet, source_state
from tests.test_toolkit_runtime import DEVICE, SCANNER, runtime


@pytest.fixture
async def policy_runtime(model_runtime, monkeypatch):
    state = model_runtime
    for module in ("entity", "sensor"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: state.clock["now"])
    bind_area(state)
    return state


def device_snapshot(state):
    return async_get_advert_snapshot(state.hass, entry_id=state.entry.entry_id)["devices"][DEVICE]


@pytest.mark.parametrize("mode", ["legacy", "pet", "stationary"])
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("cap", [0.5, 2.5])
async def test_number_respects_explicit_receiver_cap_immediately_and_after_cached_processing(
    policy_runtime, mode, restored, cap
):
    state = policy_runtime
    await state.manager.async_dispatch(
        "settings", {"settings": {"filters": {DEVICE: mode}, "receiver_limits": {SCANNER.upper(): cap}}}
    )
    for _index in range(6):
        packet(state, -68)
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert (state.device.area_advert is state.advert) == (cap == 2.5)
    nearest_sensor = BermudaSensorRange(state.coordinator, state.entry, DEVICE)
    receiver_sensor = BermudaSensorScannerRange(state.coordinator, state.entry, DEVICE, SCANNER)
    assert nearest_sensor.native_value == (round(state.advert.rssi_distance, 1) if cap == 2.5 else None)
    assert receiver_sensor.native_value == round(state.advert.rssi_distance, 3)
    filtered = state.manager._states[(DEVICE, SCANNER)]
    before_quality = deepcopy(filtered.snapshot(state.clock["now"]))
    before_source = source_state(state.advert)
    before_history_lengths = len(state.advert.hist_distance), len(state.advert.hist_distance_by_interval)
    revision, generation = state.device.model_revision, state.manager._generation
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54, restored=restored)
    expected = range_from_rssi(-68, -54, 3, 1.5)
    assert expected > cap
    assert state.advert.rssi_distance == pytest.approx(expected)
    assert state.advert.rssi_distance_raw == pytest.approx(expected)
    assert state.device.area_advert is None
    assert state.device.area_distance is None
    assert nearest_sensor.native_value is None
    assert receiver_sensor.native_value == round(expected, 3)
    snapshot = device_snapshot(state)
    assert snapshot["area_id"] is None
    assert snapshot["scanners"][SCANNER]["filtered_distance"] == pytest.approx(expected)
    assert snapshot["scanners"][SCANNER]["raw_distance"] == pytest.approx(expected)
    assert source_state(state.advert) == before_source
    assert state.manager._states[(DEVICE, SCANNER)] is filtered
    assert filtered.snapshot(state.clock["now"]) == before_quality
    assert (len(state.advert.hist_distance), len(state.advert.hist_distance_by_interval)) == before_history_lengths
    assert state.device.model_revision == revision + 1
    assert state.manager._generation == generation + 1
    state.advert.calculate_data()
    state.coordinator._refresh_area_by_min_distance(state.device)
    # Legacy cached smoothing can move the range back inside a boundary cap.
    # Every later election must judge its current filtered range normally.
    current = state.advert.rssi_distance
    assert state.device.area_advert is (state.advert if current <= cap else None)
    assert source_state(state.advert) == before_source
    assert filtered.snapshot(state.clock["now"]) == before_quality
    assert nearest_sensor.native_value == (round(current, 1) if current <= cap else None)
    assert receiver_sensor.native_value == round(current, 3)
    assert device_snapshot(state)["area_id"] == (state.scanner.area_id if current <= cap else None)
    packet(state, -68)
    state.coordinator._refresh_area_by_min_distance(state.device)
    current = state.advert.rssi_distance
    assert state.device.area_advert is (state.advert if current <= cap else None)
    assert filtered.snapshot(state.clock["now"])["samples"] == before_quality["samples"] + 1
    assert nearest_sensor.native_value == (round(current, 1) if current <= cap else None)
    assert receiver_sensor.native_value == round(current, 3)


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"receiver_limits": {SECOND_SCANNER: 0.5}},
        {"receiver_limits": {SCANNER: 0}},
        {"reliability_weighting": False},
    ],
    ids=["default", "unrelated-cap", "inherited-cap", "disabled-weighting"],
)
async def test_default_legacy_number_keeps_raw_shortcut_when_no_attached_policy_applies(
    policy_runtime, restored, settings
):
    state = policy_runtime
    await state.manager.async_dispatch("settings", {"settings": settings})
    packet(state, -68)
    state.clock["now"] += AREA_MAX_AD_AGE + 1
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert state.device.area_advert is None
    source = source_state(state.advert)
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54, restored=restored)
    expected = range_from_rssi(-68, -54, 3, 1.5)
    # The historical unconfigured calibration shortcut still applies the raw
    # nearest range immediately, even between normal area-election passes.
    assert state.device.area_advert is state.advert
    assert state.device.area_distance == expected
    assert state.advert.rssi_distance == state.advert.rssi_distance_raw == expected
    assert source_state(state.advert) == source
    assert BermudaSensorRange(state.coordinator, state.entry, DEVICE).native_value == round(expected, 1)
    assert device_snapshot(state)["area_id"] == state.scanner.area_id


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("policy", ["cap", "weighting"])
async def test_configured_legacy_number_does_not_admit_an_aged_challenger(policy_runtime, restored, policy):
    state = policy_runtime
    settings = {"receiver_limits": {SCANNER: 10}} if policy == "cap" else {"reliability_weighting": True}
    await state.manager.async_dispatch("settings", {"settings": settings})
    packet(state, -68)
    state.clock["now"] += AREA_MAX_AD_AGE + 1
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert state.device.area_advert is None
    source = source_state(state.advert)
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54, restored=restored)
    assert state.advert.rssi_distance is not None
    assert state.advert.rssi_distance < 10
    assert state.device.area_advert is None
    assert state.device.area_distance is None
    assert source_state(state.advert) == source
    assert BermudaSensorRange(state.coordinator, state.entry, DEVICE).native_value is None
    assert device_snapshot(state)["area_id"] is None
    packet(state, -68)
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert state.device.area_advert is state.advert


@pytest.mark.parametrize("restored", [False, True])
async def test_legacy_number_keeps_receiver_selected_by_actual_signal_reliability(policy_runtime, restored):
    state = policy_runtime
    second_scanner = BermudaDevice(SECOND_SCANNER, state.coordinator)
    second_scanner._is_scanner = second_scanner._is_remote_scanner = True
    second_scanner.stamps = {DEVICE.upper(): state.clock["now"]}
    state.coordinator.devices[SECOND_SCANNER] = second_scanner
    state.coordinator.scanner_list_add(second_scanner)
    bind_area(state, second_scanner)
    second = BermudaAdvert(state.device, advertisement(-70), state.coordinator.options, second_scanner)
    state.device.adverts[(DEVICE, SECOND_SCANNER)] = second
    # Both receivers consume real packets; the nearer receiver has noisy RSSI
    # while the farther receiver has stable RSSI. Do not replace quality hooks.
    for rssi in (-58, -78, -58, -78, -58, -68):
        packet(state, rssi)
        second_scanner.stamps[DEVICE.upper()] = state.clock["now"]
        second.update_advertisement(advertisement(-70), second_scanner)
        second.calculate_data()
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -55)
    assert state.advert.rssi_distance < second.rssi_distance
    await state.manager.async_dispatch("settings", {"settings": {"reliability_weighting": True}})
    assert state.manager.weighted_distance(state.advert, state.advert.rssi_distance) > state.manager.weighted_distance(
        second, second.rssi_distance
    )
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert state.device.area_advert is second
    entity = BermudaSensorRange(state.coordinator, state.entry, DEVICE)
    first_sensor = BermudaSensorScannerRange(state.coordinator, state.entry, DEVICE, SCANNER)
    second_sensor = BermudaSensorScannerRange(state.coordinator, state.entry, DEVICE, SECOND_SCANNER)
    assert entity.native_value == round(second.rssi_distance, 1)
    first_sensor.native_value
    second_sensor.native_value
    adverts = (state.advert, second)
    before_sources = [source_state(advert) for advert in adverts]
    filters = [state.manager._states[(DEVICE, advert.scanner_address)] for advert in adverts]
    before_quality = [deepcopy(filtered.snapshot(state.clock["now"])) for filtered in filters]
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54, restored=restored)
    expected = range_from_rssi(-70, -54, 3, 0)
    assert state.device.area_advert is second
    assert state.device.area_distance == pytest.approx(expected)
    assert second.rssi_distance == pytest.approx(expected)
    assert entity.native_value == round(expected, 1)
    assert first_sensor.native_value == round(state.advert.rssi_distance, 3)
    assert second_sensor.native_value == round(expected, 3)
    assert [source_state(advert) for advert in adverts] == before_sources
    assert [filtered.snapshot(state.clock["now"]) for filtered in filters] == before_quality
    assert device_snapshot(state)["area_id"] == second_scanner.area_id
    for advert in adverts:
        advert.calculate_data()
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert state.device.area_advert is second
    assert entity.native_value == round(second.rssi_distance, 1)
    assert [source_state(advert) for advert in adverts] == before_sources
    assert [filtered.snapshot(state.clock["now"]) for filtered in filters] == before_quality
