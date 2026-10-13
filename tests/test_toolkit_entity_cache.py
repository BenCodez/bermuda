"""Radio/pipeline adoption invalidates real entity caches without freshening source data."""

from __future__ import annotations

import math
from copy import deepcopy

import pytest
from homeassistant.helpers import area_registry as ar

from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import CONF_RSSI_OFFSETS
from custom_components.bermuda.number import BermudaNumber
from custom_components.bermuda.sensor import BermudaSensorRange, BermudaSensorScannerRange
from tests.test_toolkit_model_rebase import model_runtime, packet, source_state
from tests.test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _proposal, runtime


@pytest.fixture
async def cache_runtime(model_runtime, monkeypatch):
    runtime = model_runtime
    for module in ("sensor", "entity"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: runtime.clock["now"])
    # One integer RSSI packet has an exact synthetic 2 m model baseline.
    reference = -66 + 1.5 + 30 * math.log10(2)
    await runtime.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"reference_power": reference}}
    )
    area = ar.async_get(runtime.hass).async_create("Synthetic entity cache room")
    runtime.scanner._update_area_and_floor(area.id)
    packet(runtime, -66)
    select_devices(runtime)
    return runtime


def select_devices(runtime):
    for device in runtime.coordinator.devices.values():
        if getattr(device, "create_sensor", False):
            device.calculate_data()
            runtime.coordinator._refresh_area_by_min_distance(device)


def distance_entity(runtime, device=None):
    device = device or runtime.device
    entity = BermudaSensorRange(runtime.coordinator, runtime.entry, device.address)
    entity.bermuda_update_interval = 60
    assert entity.native_value == pytest.approx(round(device.area_distance, 1))
    return entity


async def change_offset(runtime, action, offset):
    if action == "profile":
        await runtime.manager.async_dispatch(
            "profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": offset}}
        )
    elif action == "legacy_options":
        options = deepcopy(dict(runtime.entry.options))
        options[CONF_RSSI_OFFSETS][SCANNER] = offset
        runtime.hass.config_entries.async_update_entry(runtime.entry, options=options)
        assert await runtime.manager.async_options_changed(runtime.entry)
    else:
        runtime.manager.proposal = _proposal(runtime.manager, {SCANNER: offset})
        await runtime.manager.async_dispatch("calibration_apply", {})


@pytest.mark.parametrize("action", ["profile", "legacy_options", "calibration_apply"])
@pytest.mark.parametrize("delay", [0, 3, 20])
async def test_sub_deadband_radio_correction_reaches_actual_distance_entity_after_delayed_read(
    cache_runtime, action, delay
):
    runtime = cache_runtime
    entity = distance_entity(runtime)
    assert entity.native_value == 2.0
    before_source = source_state(runtime.advert)
    state = runtime.manager._states[(DEVICE, SCANNER)]
    before_samples = state.snapshot(runtime.clock["now"])["samples"]
    old_revision = runtime.device.model_revision
    await change_offset(runtime, action, 2.0)  # +0.5 dB gives about 1.925 m, published as 1.9.
    assert runtime.device.model_revision == old_revision + 1
    assert runtime.device.model_selection_pending
    runtime.clock["now"] += delay
    # A property read before selection sees the previous selected value and must
    # not spend the model correction's persistent invalidation marker.
    assert entity.native_value == 2.0
    assert entity.bermuda_model_revision == old_revision
    select_devices(runtime)
    assert not runtime.device.model_selection_pending
    assert round(runtime.device.area_distance, 1) == 1.9
    assert entity.native_value == 1.9
    assert entity.bermuda_model_revision == runtime.device.model_revision
    assert entity._distance_readings == []
    assert source_state(runtime.advert) == before_source
    assert runtime.manager._states[(DEVICE, SCANNER)] is state
    assert state.snapshot(runtime.clock["now"])["samples"] == before_samples


async def test_apply_and_rollback_in_one_clock_tick_both_invalidate_cached_distance(cache_runtime):
    runtime = cache_runtime
    entity = distance_entity(runtime)
    before_source = source_state(runtime.advert)
    original_revision = runtime.device.model_revision
    await change_offset(runtime, "calibration_apply", 2.0)
    select_devices(runtime)
    assert entity.native_value == 1.9
    await runtime.manager.async_dispatch("calibration_rollback", {})
    # Same monotonic tick: revision must still distinguish rollback from apply.
    assert runtime.device.model_revision == original_revision + 2
    assert entity.native_value == 1.9
    runtime.clock["now"] += 3
    select_devices(runtime)
    assert entity.native_value == 2.0
    assert source_state(runtime.advert) == before_source


async def test_shared_source_and_metadevice_each_publish_radio_correction_once(cache_runtime):
    runtime = cache_runtime
    meta = BermudaDevice("synthetic-cache-metadevice", runtime.coordinator)
    meta.create_sensor = True
    meta.adverts[(DEVICE, SCANNER)] = runtime.advert
    runtime.coordinator.devices[meta.address] = meta
    select_devices(runtime)
    source_entity = distance_entity(runtime)
    meta_entity = distance_entity(runtime, meta)
    before = runtime.device.model_revision, meta.model_revision
    await change_offset(runtime, "profile", 2.0)
    assert (runtime.device.model_revision, meta.model_revision) == (before[0] + 1, before[1] + 1)
    assert source_entity.native_value == meta_entity.native_value == 2.0
    runtime.clock["now"] += 4
    select_devices(runtime)
    assert source_entity.native_value == meta_entity.native_value == 1.9


async def test_per_scanner_cache_sees_small_rising_correction_without_waiting_for_selection(cache_runtime):
    runtime = cache_runtime
    entity = BermudaSensorScannerRange(runtime.coordinator, runtime.entry, DEVICE, SCANNER)
    entity.bermuda_update_interval = 60
    before = entity.native_value
    await change_offset(runtime, "profile", 1.0)  # -0.5 dB increases range, normally held by generic rate limit.
    runtime.clock["now"] += 4
    assert runtime.device.model_selection_pending
    assert entity.native_value == round(runtime.advert.rssi_distance, 3)
    assert entity.native_value > before


async def test_number_power_change_bypasses_distance_deadband_after_delayed_read(cache_runtime):
    runtime = cache_runtime
    entity = distance_entity(runtime)
    number = BermudaNumber(runtime.coordinator, runtime.entry, DEVICE)
    previous = runtime.manager.effective_advert_model(runtime.advert)["reference_power"]
    before_source = source_state(runtime.advert)
    number._set_ref_power_and_invalidate(previous + 0.5)
    assert round(runtime.device.area_distance, 1) == 2.1
    runtime.clock["now"] += 4
    assert entity.native_value == 2.1
    assert source_state(runtime.advert) == before_source


async def test_metadata_and_radius_updates_leave_model_revision_pipeline_and_deadband_unchanged(cache_runtime):
    runtime = cache_runtime
    entity = distance_entity(runtime)
    revision = runtime.device.model_revision
    pending = runtime.device.model_selection_pending
    state = runtime.manager._states[(DEVICE, SCANNER)]
    before_source = source_state(runtime.advert)
    before_histories = list(runtime.advert.hist_distance), list(runtime.advert.hist_distance_by_interval)
    await runtime.manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER: 15}}})
    await runtime.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"notes": "Synthetic metadata"}}
    )
    assert runtime.device.model_revision == revision
    assert runtime.device.model_selection_pending == pending
    assert runtime.manager._states[(DEVICE, SCANNER)] is state
    assert source_state(runtime.advert) == before_source
    assert (runtime.advert.hist_distance, runtime.advert.hist_distance_by_interval) == before_histories
    # Ordinary sub-deadband motion still gets the recorder-saving policy.
    runtime.clock["now"] += 70
    runtime.device.area_distance = 1.9
    assert entity.native_value == 2.0


async def test_filter_only_switch_invalidates_cached_distance_after_new_pipeline_selection(cache_runtime):
    runtime = cache_runtime
    await runtime.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "pet"}}})
    packet(runtime, -66)
    select_devices(runtime)
    entity = distance_entity(runtime)
    assert entity.native_value == 2.0
    packet(runtime, -67)
    select_devices(runtime)
    assert entity.native_value == 2.0  # ordinary tiny change is held by deadband
    revision = runtime.device.model_revision
    await runtime.manager.async_dispatch("settings", {"settings": {"filters": {DEVICE: "legacy"}}})
    assert runtime.device.model_revision == revision + 1
    assert runtime.device.model_selection_pending
    assert entity.native_value == 2.0  # still the old selected output; invalidation remains pending
    runtime.clock["now"] += 3
    select_devices(runtime)
    assert round(runtime.device.area_distance, 1) == 2.2
    assert entity.native_value == 2.2
