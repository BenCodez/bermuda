"""Measured receiver truth stays consistent across accepted MAC representations."""

from copy import deepcopy

import pytest

from custom_components.bermuda.profiles import normalize_fixed_reference, normalize_toolkit_settings
from custom_components.bermuda.toolkit import OPTIONS_KEY

from .test_toolkit_runtime import DEVICE, SCANNER, runtime

MIXED = "Aa:Bb:Cc:Dd:Ee:12"
LOCATION = "Synthetic/MeasuredSite"


@pytest.mark.parametrize(
    "ranges",
    [
        {SCANNER: 2.0},
        {SCANNER.upper(): 2.0},
        {MIXED: 2.0},
        {SCANNER: 2, SCANNER.upper(): 2.0},
        {SCANNER.upper(): 2.0, SCANNER: 2},
        {MIXED: 2.0, SCANNER.upper(): 2.0},
    ],
)
def test_fixed_reference_valid_mac_aliases_have_one_detached_measured_truth(ranges):
    source = {"device_id": "CaseSensitive/Beacon", "location_id": LOCATION, "ranges": ranges}
    before = deepcopy(source)
    reference = normalize_fixed_reference(source)
    assert reference["ranges"] == {SCANNER: 2.0}
    assert reference["device_id"] == "CaseSensitive/Beacon"
    assert reference["location_id"] == LOCATION
    reference["ranges"][SCANNER] = 9.0
    assert source == before


@pytest.mark.parametrize(
    "ranges",
    [{SCANNER: 2.0, SCANNER.upper(): 6.0}, {SCANNER.upper(): 6.0, SCANNER: 2.0}, {MIXED: 2.0, SCANNER.upper(): 6.0}],
)
def test_fixed_reference_conflicting_mac_aliases_are_rejected_without_mutation(ranges):
    source = {"fixed_references": {"CaseSensitive/Beacon": {"location_id": LOCATION, "ranges": ranges}}}
    before = deepcopy(source)
    with pytest.raises(ValueError, match="conflicting receiver aliases"):
        normalize_toolkit_settings(source)
    assert source == before


def test_fixed_reference_opaque_receivers_and_logical_ids_remain_exact():
    ranges = {"CaseSensitive/Receiver": 2.0, "casesensitive/receiver": 6.0, "ZZ:BB:CC:DD:EE:12": 3.0}
    source = {"fixed_references": {"CaseSensitive/Beacon": {"location_id": LOCATION, "ranges": ranges}}}
    reference = normalize_toolkit_settings(source)["fixed_references"]["CaseSensitive/Beacon"]
    assert reference["device_id"] == "CaseSensitive/Beacon"
    assert reference["location_id"] == LOCATION
    assert reference["ranges"] == ranges


def _source_measurements(state):
    advert = state.advert
    return {
        "stamp": advert.stamp,
        "new_stamp": advert.new_stamp,
        "rssi": advert.rssi,
        "raw_distance": advert.rssi_distance_raw,
        "filtered_distance": advert.rssi_distance,
        "hist_stamp": list(advert.hist_stamp),
        "hist_rssi": list(advert.hist_rssi),
        "hist_distance": list(advert.hist_distance),
        "hist_interval": list(advert.hist_interval),
        "hist_filtered_distance": list(advert.hist_distance_by_interval),
        "scanner_stamps": dict(state.scanner.stamps),
        "scanner_last_seen": state.scanner.last_seen,
    }


@pytest.mark.parametrize("reverse", [False, True])
async def test_public_settings_reject_conflicting_fixed_truth_before_any_side_effect(runtime, reverse):
    manager = runtime.manager
    await manager.async_dispatch(
        "settings",
        {"settings": {"fixed_references": {DEVICE: {"location_id": LOCATION, "ranges": {SCANNER: 3.0}}}}},
    )
    await manager._after_update()
    assert manager.history.frames()
    options = deepcopy(dict(runtime.entry.options))
    coordinator_options = deepcopy(runtime.coordinator.options)
    settings = deepcopy(manager.settings)
    history = manager.history.export(redact=False)
    clocks = manager.history.export_clocks()
    source = _source_measurements(runtime)
    version, revision, generation = manager.version(), runtime.device.model_revision, manager._generation
    saves = manager.store.async_save.await_count
    ranges = {SCANNER: 2.0, SCANNER.upper(): 6.0}
    if reverse:
        ranges = dict(reversed(list(ranges.items())))
    with pytest.raises(ValueError, match="conflicting receiver aliases"):
        await manager.async_dispatch(
            "settings",
            {
                "settings": {
                    "fixed_references": {DEVICE: {"location_id": "Synthetic/InvalidSite", "ranges": ranges}},
                    "receiver_profiles": {SCANNER: {"rssi_offset": 8.0}},
                    "history_enabled": True,
                    "history_window_s": 120.0,
                    "auto_calibration": True,
                }
            },
        )
    assert dict(runtime.entry.options) == options
    assert runtime.coordinator.options == coordinator_options
    assert manager.settings == settings
    assert manager.history.export(redact=False) == history
    assert manager.history.export_clocks() == clocks
    assert manager.store.async_save.await_count == saves
    assert (manager.version(), runtime.device.model_revision, manager._generation) == (version, revision, generation)
    assert _source_measurements(runtime) == source


@pytest.mark.parametrize(
    "ranges",
    [
        {SCANNER: 2.0},
        {SCANNER.upper(): 2.0},
        {SCANNER: 2.0, SCANNER.upper(): 2.0},
        {SCANNER.upper(): 2.0, SCANNER: 2.0},
    ],
)
async def test_accepted_fixed_aliases_agree_in_live_quality_frozen_truth_preview_and_auto(runtime, ranges):
    manager = runtime.manager
    source = _source_measurements(runtime)
    result = await manager.async_dispatch(
        "settings",
        {
            "settings": {
                "fixed_references": {DEVICE: {"location_id": LOCATION, "ranges": ranges}},
                "auto_calibration": True,
            }
        },
    )
    configured = manager.settings["fixed_references"][DEVICE]
    assert configured["ranges"] == {SCANNER: 2.0}
    assert configured["location_id"] == LOCATION
    assert result["options"]["fixed_references"][DEVICE] == configured
    assert runtime.entry.options[OPTIONS_KEY]["fixed_references"][DEVICE] == configured
    quality = manager.snapshot_metadata(DEVICE, SCANNER, runtime.advert)["quality"]
    assert quality["range_error_m"] == pytest.approx(abs(runtime.advert.rssi_distance_raw - 2.0))
    await manager._after_update()
    frames = manager.history.frames()
    assert len(frames) == 1
    assert frames[0]["truth"][0]["ranges"] == {SCANNER: 2.0}
    assert frames[0]["truth"][0]["location_id"] == LOCATION
    assert manager._labelled_rows()[0]["expected_distance"] == 2.0
    assert manager._proposal_rows[0]["expected_distance"] == 2.0
    assert manager.last_result["apply"] is False
    await manager.async_dispatch("calibration_preview", {})
    assert manager._proposal_rows[0]["expected_distance"] == 2.0
    assert _source_measurements(runtime) == source
