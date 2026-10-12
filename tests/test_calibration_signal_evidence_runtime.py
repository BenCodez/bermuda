"""Real manager calibration separates stable signal evidence from model accuracy."""

from copy import deepcopy
import math
import time
from types import SimpleNamespace

import pytest
from bleak.backends.scanner import AdvertisementData

from custom_components.bermuda import api as api_module
from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.api import snapshot_observations
from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.bermuda_device import BermudaDevice
from custom_components.bermuda.const import CONF_RSSI_OFFSETS

from .test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _add_receiver, runtime

SECOND_DEVICE = "aa:bb:cc:dd:ee:02"
SECOND_SCANNER = "aa:bb:cc:dd:ee:22"
CORRECTIONS = {SCANNER: 4.0, SECOND_SCANNER: -3.0}


def advertisement(rssi):
    return AdvertisementData(
        local_name="Synthetic Calibration Beacon",
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-59,
        rssi=rssi,
        platform_data=(),
    )


@pytest.fixture
async def evidence_runtime(runtime, monkeypatch):
    clock = runtime.clock = SimpleNamespace(mono=SOURCE_STAMP, epoch=time.time())
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: clock.mono)
    # Replace module references, preserving the HA event loop's wall clock.
    for module in (api_module, toolkit_module):
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock.epoch))
    _add_receiver(runtime.coordinator, runtime.device, SECOND_SCANNER, clock.mono)
    second = BermudaDevice(SECOND_DEVICE, runtime.coordinator)
    second.create_sensor = True
    runtime.coordinator.devices[SECOND_DEVICE] = second
    runtime.beacons = [runtime.device, second]
    runtime.receivers = [runtime.scanner, runtime.coordinator.devices[SECOND_SCANNER]]
    for scanner in runtime.receivers:
        scanner.stamps[SECOND_DEVICE.upper()] = clock.mono
        advert = BermudaAdvert(second, advertisement(-68), runtime.coordinator.options, scanner)
        second.adverts[(SECOND_DEVICE, scanner.address)] = advert
        advert.calculate_data()
    await runtime.manager._persist_options(
        {**dict(runtime.entry.options), CONF_RSSI_OFFSETS: {scanner: 0.0 for scanner in CORRECTIONS}}
    )
    return runtime


async def collect_evidence(runtime, *, fixed, mode="legacy", noise_db=0.0):
    manager = runtime.manager
    await manager.async_dispatch(
        "settings",
        {
            "settings": {
                "beacon_profiles": {
                    beacon.address: {"reference_power": -55.0, "reference_power_verified": True}
                    for beacon in runtime.beacons
                },
                "filters": {beacon.address: mode for beacon in runtime.beacons},
                "reliability_weighting": True,
            }
        },
    )
    for location in range(2):
        ranges = {
            beacon.address: {
                scanner.address: 2.0 + location * 2 + scanner_index + beacon_index * 0.2
                for scanner_index, scanner in enumerate(runtime.receivers)
            }
            for beacon_index, beacon in enumerate(runtime.beacons)
        }
        if fixed:
            await manager.async_dispatch(
                "settings",
                {
                    "settings": {
                        "fixed_references": {
                            beacon.address: {
                                "location_id": f"synthetic-site-{location}",
                                "ranges": ranges[beacon.address],
                                "accuracy_m": 0.01,
                            }
                            for beacon in runtime.beacons
                        }
                    }
                },
            )
        # A full constant window removes movement and startup noise before capture.
        for packet in range(36):
            runtime.clock.mono += 0.1
            runtime.clock.epoch += 0.1
            for beacon in runtime.beacons:
                for scanner in runtime.receivers:
                    distance = ranges[beacon.address][scanner.address]
                    rssi = -55.0 - 30.0 * math.log10(distance) - CORRECTIONS[scanner.address]
                    rssi += noise_db if packet % 2 else -noise_db
                    scanner.stamps[beacon.address.upper()] = runtime.clock.mono
                    advert = beacon.adverts[(beacon.address, scanner.address)]
                    advert.update_advertisement(advertisement(rssi), scanner)
                    advert.calculate_data()
            if packet < 32:
                continue
            if fixed:
                manager.after_update(0.001)
                await manager._background
            else:
                for beacon in runtime.beacons:
                    await manager.async_dispatch(
                        "capture",
                        {
                            "device_id": beacon.address,
                            "location_id": f"synthetic-site-{location}",
                            "ranges": ranges[beacon.address],
                            "accuracy_m": 0.01,
                        },
                    )
    return manager._labelled_rows()


@pytest.mark.parametrize("fixed", [False, True], ids=["measured-capture", "fixed-reference-background"])
@pytest.mark.parametrize("mode", ["legacy", "pet", "stationary"])
async def test_stable_biased_live_ranges_preview_and_guarded_auto_apply(evidence_runtime, fixed, mode):
    runtime, manager = evidence_runtime, evidence_runtime.manager
    rows = await collect_evidence(runtime, fixed=fixed, mode=mode)
    assert len(rows) == 32
    assert all(row["quality"]["signal_reliability"] == pytest.approx(1.0) for row in rows)
    assert all(row["quality"]["noise_db"] == 0 for row in rows)
    if fixed:
        assert all(row["quality"]["reliability"] < manager.engine.policy.minimum_reliability for row in rows)
        assert all(row["quality"]["within_expected_accuracy"] is False for row in rows)
        assert manager.weighted_distance(runtime.advert, 3.0) == pytest.approx(15.0)
    else:
        assert all(row["quality"]["reliability"] == row["quality"]["signal_reliability"] for row in rows)
        assert manager.weighted_distance(runtime.advert, 3.0) == pytest.approx(3.0)
    frozen = manager.history.frames()
    exported = await manager.async_dispatch("history_export", {"redact": False})
    await manager.async_dispatch("history_import", {"data": exported})
    assert manager.history.frames() == frozen
    proposal = await manager.async_dispatch("calibration_preview", {})
    assert proposal["eligible"] is True
    assert proposal["offsets"] == pytest.approx(CORRECTIONS)
    assert proposal["coverage"]["samples"] == 32
    assert proposal["rejected"].get("unreliable_signal", 0) == 0
    decision = manager.engine.automatic_decision(
        proposal,
        now=runtime.clock.epoch,
        model_version=manager.version(),
        current_offsets=manager._current_offsets(),
        observations=manager._proposal_rows,
        verified_reference_devices=manager._verified_devices(),
    )
    assert decision["apply"] is True
    assert decision["offsets"] == pytest.approx({SCANNER: 1.0, SECOND_SCANNER: -0.75})
    assert decision["step_scale"] == pytest.approx(0.25)
    for holdout in ("location_holdout", "target_holdout"):
        metric = decision["step_metrics"][holdout]
        assert metric["proposed_mae_m"] < metric["current_mae_m"]
    await manager.async_dispatch("settings", {"settings": {"auto_calibration": True}})
    manager.after_update(0.001)
    await manager._background
    assert manager.last_result["applied"] is True
    assert manager.trial["automatic"] is True
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == pytest.approx(decision["offsets"])


async def test_noisy_live_signal_cannot_propose_or_apply_a_correction(evidence_runtime):
    runtime, manager = evidence_runtime, evidence_runtime.manager
    rows = await collect_evidence(runtime, fixed=True, noise_db=18.0)
    assert len(rows) == 32
    assert all(row["quality"]["signal_reliability"] < manager.engine.policy.minimum_reliability for row in rows)
    proposal = await manager.async_dispatch("calibration_preview", {})
    assert proposal["eligible"] is False
    assert proposal["coverage"]["samples"] == 0
    assert proposal["rejected"]["unreliable_signal"] > 0
    assert sum(proposal["rejected"].values()) == 32
    await manager.async_dispatch("settings", {"settings": {"auto_calibration": True}})
    await manager._after_update()
    assert manager.last_result["apply"] is False
    assert manager.trial is None
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == {scanner: 0.0 for scanner in CORRECTIONS}


async def test_imported_legacy_quality_keeps_safe_fallback_and_replay(evidence_runtime):
    runtime, manager = evidence_runtime, evidence_runtime.manager
    await collect_evidence(runtime, fixed=True)
    exported = await manager.async_dispatch("history_export", {"redact": False})
    legacy = deepcopy(exported)
    for frame in legacy["frames"]:
        for row in frame["observations"]:
            row["quality"].pop("signal_reliability")
    await manager.async_dispatch("history_import", {"data": legacy})
    rejected = await manager.async_dispatch("calibration_preview", {})
    assert rejected["eligible"] is False
    assert rejected["rejected"]["unreliable_signal"] == 32
    replay = await manager.async_dispatch("replay", {"candidate": CORRECTIONS})
    assert replay["counts"]["labelled"] == 32
    assert manager.history.frames() == legacy["frames"]
    for frame in legacy["frames"]:
        for row in frame["observations"]:
            row["quality"]["reliability"] = 1.0
    await manager.async_dispatch("history_import", {"data": legacy})
    assert (await manager.async_dispatch("calibration_preview", {}))["eligible"] is True
    for frame in exported["frames"]:
        for row in frame["observations"]:
            row["quality"]["reliable"] = False
    await manager.async_dispatch("history_import", {"data": exported})
    untrusted = await manager.async_dispatch("calibration_preview", {})
    assert untrusted["eligible"] is False
    assert untrusted["rejected"]["untrusted_source"] == 32


async def test_signal_freshness_expires_without_polling_refresh(evidence_runtime):
    runtime, manager = evidence_runtime, evidence_runtime.manager
    await collect_evidence(runtime, fixed=True)
    frozen = manager.history.frames()
    samples = {key: state.snapshot(runtime.clock.mono)["samples"] for key, state in manager._states.items()}
    runtime.clock.mono += 31
    runtime.clock.epoch += 31
    for beacon in runtime.beacons:
        for advert in beacon.adverts.values():
            advert.calculate_data()
    rows = snapshot_observations(manager._snapshot())
    assert all(row["status"] == "stale" and row["quality"]["signal_reliability"] == 0 for row in rows)
    assert all(manager._states[key].snapshot(runtime.clock.mono)["samples"] == count for key, count in samples.items())
    proposal = await manager.async_dispatch("calibration_preview", {})
    assert proposal["eligible"] is True  # Fresh at capture remains valid frozen evidence.
    decision = manager.engine.automatic_decision(
        proposal,
        now=runtime.clock.epoch,
        model_version=manager.version(),
        current_offsets=manager._current_offsets(),
        observations=manager._proposal_rows,
    )
    assert decision["apply"] is False
    assert "stale_proposal" in decision["reasons"]
    await manager._after_update()
    assert manager.history.frames() == frozen
