"""Reference Power Numbers preserve accepted RSSI and physical packet provenance."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.number import NumberExtraStoredData
from homeassistant.helpers import area_registry as ar

from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.calibration import range_from_rssi
from custom_components.bermuda.const import CONF_DEVICES
from custom_components.bermuda.entity import BermudaEntity
from custom_components.bermuda.number import BermudaNumber
from custom_components.bermuda.sensor import BermudaSensorRange, BermudaSensorScannerRange, BermudaSensorScannerRangeRaw
from tests.test_toolkit_model_rebase import model_runtime, packet, source_state
from tests.test_toolkit_runtime import DEVICE, SCANNER, _proposal, runtime
from tests.test_toolkit_shared_owners import BEACON, batch, register_alias

SECOND_SCANNER = "aa:bb:cc:dd:ee:13"


def bind_area(state, scanner=None):
    scanner = scanner or state.scanner
    area = ar.async_get(state.hass).async_create(f"Synthetic Number room {scanner.address}")
    scanner._update_area_and_floor(area.id)
    for device in state.coordinator.devices.values():
        for advert in device.adverts.values():
            if advert.scanner_address == scanner.address:
                advert.apply_new_scanner(scanner)


def advertisement(rssi):
    return AdvertisementData(
        local_name="Synthetic Number Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=rssi,
        platform_data=(),
    )


async def change_number(number, value, *, restored=False):
    if restored:
        data = NumberExtraStoredData(
            native_max_value=0,
            native_min_value=-127,
            native_step=1,
            native_unit_of_measurement="dBm",
            native_value=value,
        )
        with (
            patch.object(BermudaEntity, "async_added_to_hass", new=AsyncMock()),
            patch.object(number, "async_get_last_number_data", new=AsyncMock(return_value=data)),
        ):
            await number.async_added_to_hass()
    else:
        with patch.object(number, "async_write_ha_state"):
            await number.async_set_native_value(value)


async def accepted_then_spike(state, mode):
    bind_area(state)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: mode}}})
    for _index in range(6):
        packet(state, -68)
    packet(state, -38)
    state.coordinator._refresh_area_by_min_distance(state.device)
    return state.manager._states[(DEVICE, SCANNER)]


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("restored", [False, True])
async def test_number_reprojects_accepted_and_raw_history_without_promoting_spike(
    model_runtime, monkeypatch, mode, restored
):
    state = model_runtime
    for module in ("entity", "sensor"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: state.clock["now"])
    filtered = await accepted_then_spike(state, mode)
    before_quality = deepcopy(filtered.snapshot(state.clock["now"]))
    before_candidate = list(filtered._candidate)
    assert before_quality["filtered_rssi"] == -68
    before_source = source_state(state.advert)
    raw_history = list(state.advert.hist_distance)
    interval_history = list(state.advert.hist_distance_by_interval)
    old_model = state.manager.effective_advert_model(state.advert)
    scanner_sensor = BermudaSensorScannerRange(state.coordinator, state.entry, DEVICE, SCANNER)
    raw_sensor = BermudaSensorScannerRangeRaw(state.coordinator, state.entry, DEVICE, SCANNER)
    closest_sensor = BermudaSensorRange(state.coordinator, state.entry, DEVICE)
    assert scanner_sensor.native_value == round(state.advert.rssi_distance, 3)
    assert closest_sensor.native_value == round(state.device.area_distance, 1)
    old_version, generation = state.manager.version(), state.manager._generation
    state.manager.proposal = _proposal(state.manager, {SCANNER: 1})
    state.manager._suggestions[DEVICE] = {"accepted": True, "suggested_area": state.scanner.area_id}
    state.manager.shadow = shadow = MagicMock()
    cancel = shadow.cancel
    number = BermudaNumber(state.coordinator, state.entry, DEVICE)
    election_evidence = []
    normal_selection = state.coordinator._refresh_area_by_min_distance

    def election_after_mutation(device):
        election_evidence.append(
            (
                state.manager._generation,
                state.manager.version(),
                state.manager.proposal,
                dict(state.manager._suggestions),
            )
        )
        normal_selection(device)

    with patch.object(state.coordinator, "_refresh_area_by_min_distance", side_effect=election_after_mutation):
        await change_number(number, old_model["reference_power"] + 1, restored=restored)
    new_model = state.manager.effective_advert_model(state.advert)
    expected = range_from_rssi(-68, new_model["reference_power"], new_model["attenuation"], new_model["rssi_offset"])
    raw_expected = range_from_rssi(
        -38, new_model["reference_power"], new_model["attenuation"], new_model["rssi_offset"]
    )
    scale = 10 ** (1 / (10 * old_model["attenuation"]))
    assert number.native_value == old_model["reference_power"] + 1
    assert state.advert.rssi_distance == pytest.approx(expected)
    assert state.advert.rssi_distance_raw == pytest.approx(raw_expected)
    assert state.advert.hist_distance == pytest.approx([value * scale for value in raw_history])
    assert state.advert.hist_distance_by_interval == pytest.approx([value * scale for value in interval_history])
    assert source_state(state.advert) == before_source
    assert state.manager._states[(DEVICE, SCANNER)] is filtered
    assert filtered.snapshot(state.clock["now"]) == before_quality
    assert filtered._candidate == before_candidate
    assert state.device.area_advert is state.advert
    assert state.device.area_distance == pytest.approx(expected)
    assert scanner_sensor.native_value == round(expected, 3)
    assert raw_sensor.native_value == round(raw_expected, 3)
    assert closest_sensor.native_value == round(expected, 1)
    assert state.manager.version() != old_version
    assert state.manager.version() == state.manager._version_for(state.manager.settings, state.coordinator.options)
    assert state.manager._generation == generation + 1
    assert election_evidence == [(generation + 1, state.manager.version(), None, {})]
    assert state.manager.proposal is None
    cancel.assert_called_once_with("the radio model changed")
    state.manager.shadow = None
    rebased_history = list(state.advert.hist_distance_by_interval)
    state.advert.calculate_data()
    assert state.advert.rssi_distance == pytest.approx(expected)
    assert state.advert.hist_distance_by_interval == rebased_history
    assert source_state(state.advert) == before_source
    assert filtered.snapshot(state.clock["now"]) == before_quality
    state.clock["now"] += 3
    assert scanner_sensor.native_value == round(expected, 3)
    assert closest_sensor.native_value == round(expected, 1)
    packet(state, -68)
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert filtered.snapshot(state.clock["now"])["samples"] == before_quality["samples"] + 1
    assert state.advert.hist_distance_by_interval[1:] == rebased_history
    assert state.advert.hist_distance_by_interval[0] == pytest.approx(expected)
    assert scanner_sensor.native_value == round(expected, 3)
    assert closest_sensor.native_value == round(expected, 1)


@pytest.mark.parametrize("mode", ["pet", "stationary"])
async def test_unchanged_number_preserves_filter_and_evidence_exactly(model_runtime, mode):
    state = model_runtime
    filtered = await accepted_then_spike(state, mode)
    before = (
        source_state(state.advert),
        deepcopy(filtered.snapshot(state.clock["now"])),
        list(filtered._candidate),
        list(state.advert.hist_distance),
        list(state.advert.hist_distance_by_interval),
        state.advert.rssi_distance,
        state.advert.rssi_distance_raw,
        state.device.model_revision,
        state.manager._generation,
        state.manager.version(),
    )
    proposal = state.manager.proposal = _proposal(state.manager, {SCANNER: 1})
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), state.device.ref_power)
    state.advert.set_ref_power(state.advert.ref_power)
    assert (
        source_state(state.advert),
        filtered.snapshot(state.clock["now"]),
        filtered._candidate,
        state.advert.hist_distance,
        state.advert.hist_distance_by_interval,
        state.advert.rssi_distance,
        state.advert.rssi_distance_raw,
        state.device.model_revision,
        state.manager._generation,
        state.manager.version(),
    ) == before
    assert state.manager.proposal is proposal


async def test_legacy_number_keeps_immediate_raw_distance_calibration_behavior(model_runtime):
    state = model_runtime
    bind_area(state)
    packet(state, -80)
    assert state.advert.rssi_distance != pytest.approx(state.advert.rssi_distance_raw)
    source = source_state(state.advert)
    raw_history = list(state.advert.hist_distance)
    interval_history = list(state.advert.hist_distance_by_interval)
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54)
    expected = range_from_rssi(-80, -54, 3, 1.5)
    assert state.advert.rssi_distance == state.advert.rssi_distance_raw == expected
    assert state.device.area_advert is state.advert
    assert state.device.area_distance == expected
    assert state.advert.hist_distance == [expected, *raw_history[1:]]
    assert state.advert.hist_distance_by_interval == [expected, *interval_history[1:]]
    assert source_state(state.advert) == source


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("profile_kind", ["beacon", "receiver"])
async def test_effective_profile_power_can_mask_number_change_without_touching_ranges(
    model_runtime, mode, profile_kind
):
    state = model_runtime
    number = BermudaNumber(state.coordinator, state.entry, DEVICE)
    if profile_kind == "receiver":
        await change_number(number, -55)
    await state.manager.async_dispatch(
        "profile",
        {
            "kind": profile_kind,
            "id": DEVICE if profile_kind == "beacon" else SCANNER,
            "profile": {"reference_power": -55},
        },
    )
    filtered = await accepted_then_spike(state, mode)
    quality = deepcopy(filtered.snapshot(state.clock["now"]))
    before_model = state.manager.effective_advert_model(state.advert)
    before_distances = state.advert.rssi_distance, state.advert.rssi_distance_raw
    raw_history, interval_history = list(state.advert.hist_distance), list(state.advert.hist_distance_by_interval)
    source = source_state(state.advert)
    await change_number(number, -54 if profile_kind == "beacon" else 0)
    assert state.manager.effective_advert_model(state.advert) == before_model
    assert (state.advert.rssi_distance, state.advert.rssi_distance_raw) == before_distances
    assert state.advert.hist_distance == raw_history
    assert state.advert.hist_distance_by_interval == interval_history
    assert source_state(state.advert) == source
    assert filtered.snapshot(state.clock["now"]) == quality


@pytest.mark.parametrize("mode", ["pet", "stationary"])
async def test_number_preserves_unconsumed_packet_and_candidate_processing(model_runtime, mode):
    state = model_runtime
    filtered = await accepted_then_spike(state, mode)
    packet(state, -68)  # Clear the prior candidate.
    state.clock["now"] += 1
    state.scanner.stamps[DEVICE.upper()] = state.clock["now"]
    state.advert.update_advertisement(advertisement(-38), state.scanner)
    source = source_state(state.advert)
    quality = deepcopy(filtered.snapshot(state.clock["now"]))
    assert source["new_stamp"] is not None
    number = BermudaNumber(state.coordinator, state.entry, DEVICE)
    await change_number(number, -54)
    assert source_state(state.advert) == source
    assert filtered.snapshot(state.clock["now"]) == quality
    state.advert.calculate_data()
    current = filtered.snapshot(state.clock["now"])
    assert current["samples"] == quality["samples"] + 1
    assert current["filtered_rssi"] == -68
    assert state.advert.rssi_distance == pytest.approx(range_from_rssi(-68, -54, 3, 1.5))
    state.advert.calculate_data()
    assert filtered.snapshot(state.clock["now"]) == current


@pytest.mark.parametrize("mode", ["pet", "stationary"])
async def test_number_keeps_expired_filtered_distance_unavailable(model_runtime, mode):
    state = model_runtime
    filtered = await accepted_then_spike(state, mode)
    state.clock["now"] += 121
    state.advert.calculate_data()
    assert state.advert.rssi_distance is None
    source, quality = source_state(state.advert), deepcopy(filtered.snapshot(state.clock["now"]))
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54)
    assert state.advert.rssi_distance is None
    assert state.device.area_advert is None
    assert state.device.area_distance is None
    assert source_state(state.advert) == source
    assert filtered.snapshot(state.clock["now"]) == quality


@pytest.mark.parametrize("target", [DEVICE, BEACON])
@pytest.mark.parametrize("restored", [False, True])
async def test_number_updates_only_its_tracked_owner_after_both_reject_spike(model_runtime, target, restored):
    state = model_runtime
    meta = register_alias(state)
    bind_area(state)
    for owner, power in ((DEVICE, -55), (BEACON, -65)):
        await change_number(BermudaNumber(state.coordinator, state.entry, owner), power)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet", BEACON: "stationary"}}})
    for _index in range(6):
        batch(state, rssi=-68)
    batch(state, rssi=-38)
    adverts = {DEVICE: state.advert, BEACON: meta.get_scanner(SCANNER)}
    devices = {DEVICE: state.device, BEACON: meta}
    before = {}
    for owner, advert in adverts.items():
        filtered = state.manager._states[(owner, SCANNER)]
        before[owner] = {
            "source": source_state(advert),
            "filtered": advert.rssi_distance,
            "raw": advert.rssi_distance_raw,
            "raw_history": list(advert.hist_distance),
            "interval_history": list(advert.hist_distance_by_interval),
            "quality": deepcopy(filtered.snapshot(state.clock["now"])),
            "candidate": list(filtered._candidate),
            "revision": devices[owner].model_revision,
            "power": advert.ref_power,
        }
    entities = {owner: BermudaSensorScannerRange(state.coordinator, state.entry, owner, SCANNER) for owner in adverts}
    for entity in entities.values():
        entity.native_value
    await change_number(
        BermudaNumber(state.coordinator, state.entry, target), before[target]["power"] + 1, restored=restored
    )
    for owner, advert in adverts.items():
        prior = before[owner]
        scale = 10 ** (1 / 30) if owner == target else 1
        assert advert.rssi_distance == pytest.approx(prior["filtered"] * scale)
        assert advert.rssi_distance_raw == pytest.approx(prior["raw"] * scale)
        assert advert.hist_distance == pytest.approx([value * scale for value in prior["raw_history"]])
        assert advert.hist_distance_by_interval == pytest.approx([value * scale for value in prior["interval_history"]])
        assert source_state(advert) == prior["source"]
        filtered = state.manager._states[(owner, SCANNER)]
        assert filtered.snapshot(state.clock["now"]) == prior["quality"]
        assert filtered._candidate == prior["candidate"]
        assert devices[owner].model_revision == prior["revision"] + (owner == target)
        assert entities[owner].native_value == round(prior["filtered"] * scale, 3)
    batch(state)
    for owner, advert in adverts.items():
        scale = 10 ** (1 / 30) if owner == target else 1
        assert advert.rssi_distance == pytest.approx(before[owner]["filtered"] * scale)
        assert source_state(advert) == before[owner]["source"]
        assert (
            state.manager._states[(owner, SCANNER)].snapshot(state.clock["now"])["samples"]
            == before[owner]["quality"]["samples"]
        )


async def test_sole_meta_number_reprojects_shared_filter_once_and_keeps_power_inheritance(model_runtime):
    state = model_runtime
    meta = register_alias(state)
    state.device.create_sensor = False
    state.coordinator.options[CONF_DEVICES] = [BEACON.upper()]
    bind_area(state)
    await state.manager.async_dispatch("settings", {"settings": {"filters": {BEACON: "pet"}}})
    for _index in range(6):
        batch(state, rssi=-68)
    batch(state, rssi=-38)
    assert meta.get_scanner(SCANNER) is state.advert
    source = source_state(state.advert)
    before = state.advert.rssi_distance
    await change_number(BermudaNumber(state.coordinator, state.entry, BEACON), -54)
    expected = before * 10 ** (1 / 30)
    assert state.advert.rssi_distance == pytest.approx(expected)
    batch(state)
    assert state.advert.rssi_distance == pytest.approx(expected)
    assert source_state(state.advert) == source
    assert state.device.ref_power == meta.ref_power == state.advert.ref_power == -54


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("excluded", ["radius", "area", "age"])
async def test_number_selection_respects_ordinary_receiver_eligibility(model_runtime, mode, excluded):
    state = model_runtime
    await accepted_then_spike(state, mode)
    state.device.apply_scanner_selection(None)
    if excluded == "radius":
        await state.manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 0.5}}})
    elif excluded == "area":
        state.scanner._update_area_and_floor(None)
        state.advert.apply_new_scanner(state.scanner)
    else:
        state.clock["now"] += 31
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54)
    assert state.device.area_advert is None
    assert state.device.area_distance is None


@pytest.mark.parametrize("mode", ["pet", "stationary"])
@pytest.mark.parametrize("weighted", [False, True])
async def test_number_uses_filtered_distance_and_reliability_for_receiver_election(model_runtime, mode, weighted):
    state = model_runtime
    await accepted_then_spike(state, mode)
    receiver = BermudaDevice(SECOND_SCANNER, state.coordinator)
    receiver._is_scanner = receiver._is_remote_scanner = True
    receiver.stamps = {DEVICE.upper(): state.clock["now"]}
    state.coordinator.devices[SECOND_SCANNER] = receiver
    bind_area(state, receiver)
    second = BermudaAdvert(
        state.device, advertisement(-77 if not weighted else -70), state.coordinator.options, receiver
    )
    state.device.adverts[(DEVICE, SECOND_SCANNER)] = second
    for _index in range(6):
        state.clock["now"] += 1
        receiver.stamps[DEVICE.upper()] = state.clock["now"]
        second.update_advertisement(advertisement(-77 if not weighted else -70), receiver)
        second.calculate_data()
        packet(state, -68)
    if weighted:
        await state.manager.async_dispatch("settings", {"settings": {"reliability_weighting": True}})
        state.manager._quality[(DEVICE, SCANNER)]["reliability"] = 0.2
        state.manager._quality[(DEVICE, SECOND_SCANNER)]["reliability"] = 1.0
        winner = second
    else:
        state.clock["now"] += 1
        receiver.stamps[DEVICE.upper()] = state.clock["now"]
        second.update_advertisement(advertisement(-38), receiver)
        second.calculate_data()
        winner = state.advert
    state.coordinator._refresh_area_by_min_distance(state.device)
    assert state.device.area_advert is winner
    await change_number(BermudaNumber(state.coordinator, state.entry, DEVICE), -54)
    assert state.device.area_advert is winner
    assert state.device.area_distance == winner.rssi_distance
