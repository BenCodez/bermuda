"""Live radio-model adoption preserves the actual frozen filter pipeline."""

from __future__ import annotations

import math
from copy import deepcopy
from types import SimpleNamespace

import pytest
from bleak.backends.scanner import AdvertisementData

from custom_components.bermuda.api import snapshot_observations
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import CONF_ATTENUATION, CONF_RSSI_OFFSETS, DISTANCE_INFINITE
from custom_components.bermuda.recording import DiagnosticHistory
from custom_components.bermuda.replay import ReplayEngine
from custom_components.bermuda.toolkit import _rebase_distance
from custom_components.bermuda.util import rssi_to_metres
from tests.test_scanner_metadata_refresh import BackendScanner
from tests.test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _proposal, runtime

SECOND = "aa:bb:cc:dd:ee:02"


@pytest.fixture
async def model_runtime(runtime, monkeypatch):
    backend = BackendScanner(source=SCANNER, name="Synthetic Receiver")
    runtime.scanner._hascanner = backend
    runtime.coordinator._manager = SimpleNamespace(async_current_scanners=lambda: {backend})
    runtime.coordinator._hascanners = {backend}
    runtime.clock = {"now": SOURCE_STAMP}
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device", "coordinator"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: runtime.clock["now"])
    return runtime


def packet(runtime, rssi, advert=None):
    advert = advert or runtime.advert
    runtime.clock["now"] += 1
    runtime.scanner.stamps[advert.device_address.upper()] = runtime.clock["now"]
    data = AdvertisementData(
        local_name="Synthetic Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=rssi,
        platform_data=(),
    )
    advert.update_advertisement(data, runtime.scanner)
    advert.calculate_data()


def source_state(advert):
    return {
        "rssi": advert.rssi,
        "stamp": advert.stamp,
        "new_stamp": advert.new_stamp,
        "hist_rssi": list(advert.hist_rssi),
        "hist_stamp": list(advert.hist_stamp),
        "hist_interval": list(advert.hist_interval),
    }


def frozen_replay(runtime, offset):
    snapshot = runtime.manager._snapshot()
    rows = snapshot_observations(snapshot)
    history = DiagnosticHistory(enabled=True)
    history.record(rows, captured_at=snapshot["captured"], model_version=runtime.manager.version())
    report = ReplayEngine().evaluate(history.frames(), proposed_offsets={SCANNER: offset}, now=snapshot["captured"])
    return report["observations"][0]


@pytest.mark.parametrize("change", ["profile", "calibration_apply", "external_options"])
async def test_live_legacy_correction_matches_frozen_replay_and_keeps_filtered_output(model_runtime, change):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    packet(runtime, -80)  # Actual legacy retreat rejection produces filtered != raw.
    assert advert.rssi_distance != pytest.approx(advert.rssi_distance_raw)
    before = source_state(advert)
    old_model = manager.effective_advert_model(advert)
    old_filtered = advert.rssi_distance
    old_raw_history = list(advert.hist_distance)
    old_filtered_history = list(advert.hist_distance_by_interval)
    state = manager._states[(DEVICE, SCANNER)]
    quality = deepcopy(state.snapshot(runtime.clock["now"]))
    offset = old_model["rssi_offset"] + 3
    replay = frozen_replay(runtime, offset)
    if change == "profile":
        await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": offset}})
    elif change == "calibration_apply":
        manager.proposal = _proposal(manager, {SCANNER: offset})
        await manager.async_dispatch("calibration_apply", {})
    else:
        options = deepcopy(dict(runtime.entry.options))
        options[CONF_RSSI_OFFSETS][SCANNER] = offset
        runtime.hass.config_entries.async_update_entry(runtime.entry, options=options)
        assert await manager.async_options_changed(runtime.entry)
    scale = 10 ** ((old_model["rssi_offset"] - offset) / (10 * old_model["attenuation"]))
    assert advert.rssi_distance == pytest.approx(old_filtered * scale)
    assert advert.rssi_distance == pytest.approx(replay["candidate"])
    assert advert.rssi_distance != pytest.approx(advert.rssi_distance_raw)
    assert advert.hist_distance == pytest.approx([value * scale for value in old_raw_history])
    assert advert.hist_distance_by_interval == pytest.approx([value * scale for value in old_filtered_history])
    assert source_state(advert) == before
    assert manager._states[(DEVICE, SCANNER)] is state
    assert state.snapshot(runtime.clock["now"]) == quality


@pytest.mark.parametrize("mode", ["pet", "stationary"])
async def test_accepted_rssi_and_pending_spike_survive_radio_change_without_reseeding(model_runtime, mode):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    await manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: mode}}})
    for _index in range(6):
        packet(runtime, -68)
    packet(runtime, -38)
    state = manager._states[(DEVICE, SCANNER)]
    before_source = source_state(advert)
    before_quality = deepcopy(state.snapshot(runtime.clock["now"]))
    before_candidate = list(state._candidate)
    old_filtered = advert.rssi_distance
    old_raw = advert.rssi_distance_raw
    old_model = manager.effective_advert_model(advert)
    offset = old_model["rssi_offset"] + 4
    replay = frozen_replay(runtime, offset)
    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": offset}})
    assert manager._states[(DEVICE, SCANNER)] is state
    assert state.snapshot(runtime.clock["now"]) == before_quality
    assert state._candidate == before_candidate
    assert source_state(advert) == before_source
    scale = 10 ** (-4 / (10 * old_model["attenuation"]))
    assert advert.rssi_distance == pytest.approx(old_filtered * scale)
    assert advert.rssi_distance_raw == pytest.approx(old_raw * scale)
    assert advert.rssi_distance == pytest.approx(replay["candidate"])
    filtered_history = list(advert.hist_distance_by_interval)
    advert.calculate_data()  # Cached packet keeps the accepted value, not the spike.
    assert advert.rssi_distance == pytest.approx(old_filtered * scale)
    assert advert.hist_distance_by_interval == filtered_history
    assert state.snapshot(runtime.clock["now"]) == before_quality


@pytest.mark.parametrize(
    "profile", [{"attenuation": 2.2}, {"reference_power": -62}, {"attenuation": 2.2, "reference_power": -62}]
)
async def test_changed_reference_and_exponent_reproject_filtered_and_all_distance_history(model_runtime, profile):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    packet(runtime, -80)
    before_source = source_state(advert)
    old_model = manager.effective_advert_model(advert)
    old_filtered = advert.rssi_distance
    raw_history = list(advert.hist_distance)
    filtered_history = list(advert.hist_distance_by_interval)

    def expected(distance, new_model):
        equivalent = old_model["reference_power"] - old_model["rssi_offset"]
        equivalent -= 10 * old_model["attenuation"] * math.log10(distance)
        return 10 ** (
            (new_model["reference_power"] - equivalent - new_model["rssi_offset"]) / (10 * new_model["attenuation"])
        )

    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": profile})
    new_model = manager.effective_advert_model(advert)
    assert advert.rssi_distance == pytest.approx(expected(old_filtered, new_model))
    assert advert.hist_distance == pytest.approx([expected(value, new_model) for value in raw_history])
    assert advert.hist_distance_by_interval == pytest.approx([expected(value, new_model) for value in filtered_history])
    assert advert.rssi_distance_raw == rssi_to_metres(
        advert.rssi + new_model["rssi_offset"], new_model["reference_power"], new_model["attenuation"]
    )
    assert source_state(advert) == before_source


@pytest.mark.parametrize("mode", ["legacy", "pet", "stationary"])
async def test_expired_filtered_none_stays_unavailable_after_live_radio_change(model_runtime, mode):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    if mode != "legacy":
        await manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: mode}}})
        packet(runtime, -68)
    runtime.clock["now"] += 121
    advert.calculate_data()
    assert advert.rssi_distance is None
    before_source = source_state(advert)
    state = manager._states[(DEVICE, SCANNER)]
    last_valid = state.last_valid_stamp
    before_count = state.snapshot(runtime.clock["now"])["samples"]
    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 5}})
    assert advert.rssi_distance is None
    assert source_state(advert) == before_source
    assert manager._states[(DEVICE, SCANNER)] is state
    assert state.last_valid_stamp == last_valid
    assert state.snapshot(runtime.clock["now"])["samples"] == before_count


async def test_shared_metadevice_advert_is_rebased_once(model_runtime):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    packet(runtime, -80)
    runtime.coordinator.devices["synthetic-shared-meta"] = SimpleNamespace(
        create_sensor=False, ref_power=0, adverts={"shared": advert}
    )
    old_filtered = advert.rssi_distance
    old_history = list(advert.hist_distance_by_interval)
    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 4.5}})
    scale = 10 ** (-3 / 30)
    assert advert.rssi_distance == pytest.approx(old_filtered * scale)
    assert advert.hist_distance_by_interval == pytest.approx([value * scale for value in old_history])


async def test_filter_mode_change_resets_only_selected_device_pipeline(model_runtime):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    second = BermudaDevice(SECOND, runtime.coordinator)
    second.create_sensor = True
    runtime.coordinator.devices[SECOND] = second
    runtime.scanner.stamps[SECOND.upper()] = SOURCE_STAMP
    data = AdvertisementData(
        local_name="Synthetic Second Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=-68,
        platform_data=(),
    )
    second_advert = BermudaAdvert(second, data, runtime.coordinator.options, runtime.scanner)
    second.adverts[(SECOND, SCANNER)] = second_advert
    await manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet", SECOND: "stationary"}}})
    packet(runtime, -68)
    packet(runtime, -68, second_advert)
    first_state = manager._states[(DEVICE, SCANNER)]
    second_state = manager._states[(SECOND, SCANNER)]
    before_second = source_state(second_advert)
    second_distance = second_advert.rssi_distance
    second_history = list(second_advert.hist_distance_by_interval)
    before_second_quality = deepcopy(second_state.snapshot(runtime.clock["now"]))
    await manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "legacy", SECOND: "stationary"}}})
    assert (DEVICE, SCANNER) not in manager._states
    assert manager._states[(SECOND, SCANNER)] is second_state
    assert second_state.snapshot(runtime.clock["now"]) == before_second_quality
    assert source_state(second_advert) == before_second
    assert second_advert.rssi_distance == second_distance
    assert second_advert.hist_distance_by_interval == second_history
    assert advert.rssi_distance is None
    assert advert.hist_distance_by_interval == []
    assert first_state is not second_state


async def test_radius_and_metadata_changes_keep_all_distance_and_filter_state_exact(model_runtime):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    packet(runtime, -80)
    before = source_state(advert)
    before_distances = advert.rssi_distance_raw, advert.rssi_distance
    histories = list(advert.hist_distance), list(advert.hist_distance_by_interval)
    state = manager._states[(DEVICE, SCANNER)]
    await manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 15}}})
    await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"manufacturer": "Synthetic Hardware"}}
    )
    assert source_state(advert) == before
    assert (advert.rssi_distance_raw, advert.rssi_distance) == before_distances
    assert (advert.hist_distance, advert.hist_distance_by_interval) == histories
    assert manager._states[(DEVICE, SCANNER)] is state


@pytest.mark.parametrize("last_rssi", [-30, -80])
async def test_legacy_underflow_correction_keeps_numeric_source_aligned_history_without_accepting_retreat(
    model_runtime, last_rssi
):
    runtime = model_runtime
    manager, advert = runtime.manager, runtime.advert
    options = {**deepcopy(dict(runtime.entry.options)), CONF_ATTENUATION: 0.001}
    runtime.hass.config_entries.async_update_entry(runtime.entry, options=options)
    runtime.coordinator.options.update(options)
    advert.conf_attenuation = 0.001
    manager.invalidate_model()
    for history in (advert.hist_distance, advert.hist_stamp, advert.hist_rssi, advert.hist_distance_by_interval):
        history.clear()
    advert.rssi_distance = None
    packet(runtime, -30)
    packet(runtime, last_rssi)
    assert advert.rssi_distance == 0.0
    assert advert.hist_distance == [0.0 if last_rssi == -30 else DISTANCE_INFINITE, 0.0]
    before_source = source_state(advert)
    filtered = list(advert.hist_distance_by_interval)

    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"attenuation": 2.0}})
    model = manager.effective_advert_model(advert)
    expected = [
        rssi_to_metres(rssi + model["rssi_offset"], model["reference_power"], model["attenuation"])
        for rssi in advert.hist_rssi
    ]
    assert advert.hist_distance == pytest.approx(expected)
    assert len(advert.hist_distance) == len(advert.hist_rssi) == len(advert.hist_stamp)
    assert advert.rssi_distance_raw == expected[0]
    assert advert.rssi_distance == 0.0
    assert advert.hist_distance_by_interval == filtered
    assert source_state(advert) == before_source
    advert.calculate_data()  # A cached cycle must neither raise nor accept the rejected retreat.
    if last_rssi == -80:
        assert advert.rssi_distance == 0.0
    else:
        assert 0 <= advert.rssi_distance <= advert.rssi_distance_raw
    assert advert.hist_distance == pytest.approx(expected)
    assert source_state(advert) == before_source


@pytest.mark.parametrize("value", [None, DISTANCE_INFINITE])
def test_distance_rebase_keeps_unavailable_values(value):
    model = {"reference_power": -59, "attenuation": 2, "rssi_offset": 0}
    assert _rebase_distance(value, model, {**model, "rssi_offset": 10}) == value
