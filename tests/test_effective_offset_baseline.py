"""Calibration baselines agree with live receiver identity and radio models."""

import math
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.bermuda.api import public_device_id
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import CONF_RSSI_OFFSETS
from custom_components.bermuda.profiles import normalize_toolkit_settings
from custom_components.bermuda.util import rssi_to_metres

from .test_toolkit_runtime import DEVICE, OFFLINE, SCANNER, runtime

MIXED = "Aa:Bb:Cc:Dd:Ee:12"
SECOND = "aa:bb:cc:dd:ee:02"


@pytest.mark.parametrize(
    ("legacy", "profiles", "expected"),
    [
        ({SCANNER: 1.5}, {SCANNER.upper(): {"rssi_offset": 5}}, 5),
        ({SCANNER.upper(): 1.5}, {SCANNER: {"rssi_offset": 5}}, 5),
        ({SCANNER: 1.5}, {SCANNER.upper(): {"rssi_offset": 5}, MIXED: {"rssi_offset": 6}}, 1.5),
        ({SCANNER: 1.5}, {SCANNER: {"rssi_offset": 7}, SCANNER.upper(): {"rssi_offset": 5}}, 7),
        ({SCANNER.upper(): 1.5, MIXED: 2.5}, {}, 0),
        ({SCANNER: 1.5, SCANNER.upper(): 2.5}, {}, 1.5),
        ({SCANNER.upper(): 1.5}, {SCANNER.upper(): {"manufacturer": "Metadata only"}}, 1.5),
        ({SCANNER.upper(): float("nan")}, {}, 0),
        ({SCANNER: float("inf")}, {}, 0),
        ({SCANNER: True}, {}, 0),
        ({SCANNER: 40.0}, {}, 40.0),
    ],
)
async def test_derived_baseline_matches_live_alias_precedence_without_changing_options(
    runtime, legacy, profiles, expected
):
    manager = runtime.manager
    runtime.coordinator.options[CONF_RSSI_OFFSETS] = {**legacy, OFFLINE: 7.0}
    manager.settings = normalize_toolkit_settings({"receiver_profiles": profiles})
    original = deepcopy(runtime.coordinator.options)
    baseline = manager._current_offsets()
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == expected
    assert baseline[SCANNER] == expected
    assert baseline[OFFLINE] == 7.0
    for source in (*legacy, *profiles):
        advert = SimpleNamespace(scanner_address=source, device_address=DEVICE, ref_power=0.0)
        assert (
            baseline[source]
            == manager.effective_advert_model(advert, device_id=public_device_id(DEVICE))["rssi_offset"]
        )
    assert runtime.coordinator.options.keys() == original.keys()
    for source, value in original[CONF_RSSI_OFFSETS].items():
        actual = runtime.coordinator.options[CONF_RSSI_OFFSETS][source]
        assert (
            (math.isnan(value) and math.isnan(actual))
            if type(value) is float and math.isnan(value)
            else actual == value
        )
    baseline[OFFLINE] = -10.0
    assert runtime.coordinator.options[CONF_RSSI_OFFSETS][OFFLINE] == 7.0


async def test_inactive_mac_canonical_and_opaque_case_sensitive_baselines(runtime):
    manager = runtime.manager
    runtime.coordinator.scanner_list_del(runtime.scanner)
    runtime.coordinator.options[CONF_RSSI_OFFSETS] = {
        SCANNER.upper(): 1.5,
        "receiver/Case": 2.0,
        "receiver/case": 8.0,
    }
    manager.settings = normalize_toolkit_settings(
        {"receiver_profiles": {MIXED: {"rssi_offset": 5.0}, "receiver/Case": {"rssi_offset": 4.0}}}
    )
    before = deepcopy(runtime.coordinator.options)
    version = manager.version()
    baseline = manager._current_offsets()
    assert baseline[SCANNER] == 5.0
    assert baseline[SCANNER.upper()] == 5.0
    assert baseline[MIXED] == 5.0
    assert baseline["receiver/Case"] == 4.0
    assert baseline["receiver/case"] == 8.0
    assert "receiver/CASE" not in baseline
    for source in (SCANNER, SCANNER.upper(), MIXED, "receiver/Case", "receiver/case"):
        advert = SimpleNamespace(scanner_address=source, device_address=DEVICE, ref_power=0.0)
        assert (
            baseline[source]
            == manager.effective_advert_model(advert, device_id=public_device_id(DEVICE))["rssi_offset"]
        )
    assert runtime.coordinator.options == before
    assert manager.version() == version


async def measured_scene(state):
    """Two verified beacons at two independently measured physical locations."""
    second = BermudaDevice(SECOND, state.coordinator)
    second.create_sensor = True
    state.coordinator.devices[SECOND] = second
    await state.manager.async_dispatch(
        "settings",
        {
            "settings": {
                "auto_calibration": True,
                "receiver_profiles": {SCANNER.upper(): {"rssi_offset": 5.0, "manufacturer": "Receiver"}},
                "beacon_profiles": {
                    beacon: {"reference_power": -55.0, "reference_power_verified": True} for beacon in (DEVICE, SECOND)
                },
            }
        },
    )
    record_scene(state, start=time.time() - 20.0, first_stamp=80.0)


def record_scene(state, *, start, first_stamp, fresh=False):
    """Measured ranges are independent of receiver predictions; true bias is 3 dB."""
    manager = state.manager
    sequence = 0
    for location in range(2):
        for beacon_index, beacon in enumerate((DEVICE, SECOND)):
            measured_distance = 2.0 + 2.0 * location + 0.2 * beacon_index
            physical_rssi = -55.0 - 30.0 * math.log10(measured_distance) - 3.0
            model = manager.effective_advert_model(state.advert, device_id=public_device_id(beacon))
            for packet in range(4):
                captured = time.time() if fresh else start + sequence * 0.5
                raw_distance = rssi_to_metres(physical_rssi + model["rssi_offset"], -55.0, 3.0)
                row = {
                    "device_id": public_device_id(beacon),
                    "scanner_id": SCANNER,
                    "observed": captured,
                    "stamp": first_stamp + sequence,
                    "rssi": physical_rssi,
                    "rssi_offset": model["rssi_offset"],
                    "reference_power": model["reference_power"],
                    "attenuation": model["attenuation"],
                    "raw_distance": raw_distance,
                    "filtered_distance": raw_distance * 0.9,
                    "age": 0.0,
                    "status": "current",
                    "quality": {"source_trusted": True},
                    "model_version": manager.version(),
                }
                truth = {
                    "source": "measured",
                    "device_id": public_device_id(beacon),
                    "location_id": f"measured-location-{location}",
                    "ranges": {SCANNER: measured_distance},
                    "accuracy_m": 0.0,
                    "observed": captured,
                    "independent": True,
                }
                assert (
                    manager.history.record(
                        [row], captured_at=captured, model_version=manager.version(), truth=[truth], force=True
                    )
                    == 1
                )
                sequence += 1


@pytest.mark.parametrize("inactive", [False, True])
async def test_measured_preview_uses_actual_profile_baseline_for_live_and_frozen_offline_sources(runtime, inactive):
    await measured_scene(runtime)
    if inactive:
        runtime.coordinator.scanner_list_del(runtime.scanner)
    manager = runtime.manager
    previous_options = deepcopy(dict(runtime.entry.options))
    proposal = await manager.async_dispatch("calibration_preview", {})
    assert proposal["eligible"], proposal
    assert proposal["coverage"]["samples"] == 16
    assert proposal["baseline_offsets"] == {SCANNER: 5.0}
    assert proposal["offsets"] == pytest.approx({SCANNER: 3.0})
    assert proposal["deltas"] == pytest.approx({SCANNER: -2.0})
    assert proposal["rejected"].get("offset_baseline_mismatch", 0) == 0
    for name in ("location_holdout", "target_holdout"):
        metrics = proposal["metrics"][name]
        assert metrics["proposed_mae_m"] < 1e-10
        assert metrics["current_mae_m"] > 0.1
    assert dict(runtime.entry.options) == previous_options


async def test_automatic_trial_compares_saved_effective_baseline_and_rebases_live_outputs(runtime, monkeypatch):
    await measured_scene(runtime)
    manager, advert = runtime.manager, runtime.advert
    physical_rssi = -55.0 - 30.0 * math.log10(2.0) - 3.0
    advert.rssi = physical_rssi
    advert._update_raw_distance(reading_is_new=False)
    advert.rssi_distance = advert.rssi_distance_raw * 0.9
    advert.hist_distance[:] = [advert.rssi_distance_raw, advert.rssi_distance_raw * 1.2]
    advert.hist_distance_by_interval[:] = [advert.rssi_distance, advert.rssi_distance * 1.3]
    before = (
        advert.rssi_distance_raw,
        advert.rssi_distance,
        list(advert.hist_distance),
        list(advert.hist_distance_by_interval),
    )
    await manager._automatic(time.time())
    assert manager.trial["automatic"]
    assert manager.trial["previous_offsets"][SCANNER] == 5.0
    assert manager.trial["offsets"] == pytest.approx({SCANNER: 4.0})
    assert manager._current_offsets()[SCANNER] == pytest.approx(4.0)
    assert manager.effective_advert_model(advert)["rssi_offset"] == pytest.approx(4.0)
    assert manager.settings["receiver_profiles"][SCANNER.upper()] == {"manufacturer": "Receiver"}
    assert runtime.entry.options[CONF_RSSI_OFFSETS][SCANNER] == pytest.approx(4.0)
    assert runtime.entry.options[CONF_RSSI_OFFSETS][OFFLINE] == 7.0
    factor = 10 ** (1.0 / 30.0)
    after = (advert.rssi_distance_raw, advert.rssi_distance, advert.hist_distance, advert.hist_distance_by_interval)
    for old, new in zip(before, after, strict=True):
        assert new == pytest.approx([value * factor for value in old] if isinstance(old, list) else old * factor)
    assert advert.rssi == physical_rssi

    record_scene(runtime, start=time.time(), first_stamp=200.0, fresh=True)
    evaluated = []
    evaluate = manager.engine.evaluate_trial

    def monitored(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        evaluated.append(result)
        return result

    monkeypatch.setattr(manager.engine, "evaluate_trial", monitored)
    await manager._automatic(time.time())
    assert len(evaluated) == 1
    result = evaluated[0]
    assert result["accepted"] and not result["rollback"], result
    mean_distance = (2.0 + 2.2 + 4.0 + 4.2) / 4.0
    assert result["metrics"]["all"]["current_mae_m"] == pytest.approx(mean_distance * (1 - 10 ** (-2.0 / 30.0)))
    assert result["metrics"]["all"]["proposed_mae_m"] == pytest.approx(mean_distance * (1 - 10 ** (-1.0 / 30.0)))
    assert manager.trial["previous_offsets"][SCANNER] == 5.0
    assert manager._current_offsets()[SCANNER] == pytest.approx(4.0)
