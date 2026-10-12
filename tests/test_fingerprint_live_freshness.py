"""Live fingerprint consumers use current source data while persistence is busy."""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from bleak.backends.scanner import AdvertisementData

from custom_components.bermuda import api as api_module
from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.sensor import BermudaSensorAreaSuggestion

from .test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _add_receiver, runtime

SECOND_SCANNER = "aa:bb:cc:dd:ee:22"
PATTERN_A = {SCANNER: -55, SECOND_SCANNER: -85}
PATTERN_B = {SCANNER: -85, SECOND_SCANNER: -55}


def _receive_packet(runtime, clock, pattern):
    """Deliver actual adverts with coherent newer source and publication clocks."""
    clock.mono += 1
    clock.epoch += 1
    for scanner_id, rssi in pattern.items():
        scanner = runtime.coordinator.devices[scanner_id]
        scanner.stamps[DEVICE.upper()] = clock.mono
        scanner.last_seen = clock.mono
        advert = runtime.device.get_scanner(scanner_id)
        advert.update_advertisement(
            AdvertisementData(
                local_name="Synthetic Beacon",
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
        assert advert.stamp == clock.mono
        assert advert.rssi == rssi


@pytest.mark.parametrize("changed_input", ["expired", "moved"])
async def test_live_area_sensor_and_panel_refresh_current_radio_data_during_blocked_store(
    runtime, monkeypatch, changed_input
):
    coordinator, manager = runtime.coordinator, runtime.manager
    clock = SimpleNamespace(mono=SOURCE_STAMP, epoch=time.time())
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device", "coordinator"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: clock.mono)
    # Replace only these modules' clock references; HA's global wall clock stays intact.
    monkeypatch.setattr(api_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    monkeypatch.setattr(toolkit_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    _add_receiver(coordinator, runtime.device, SECOND_SCANNER, clock.mono)
    area_a = coordinator.ar.async_create("Synthetic Training Area A")
    area_b = coordinator.ar.async_create("Synthetic Training Area B")
    await manager.async_dispatch("settings", {"settings": {"fingerprint_area_selection": True}})
    assert manager.fingerprint_area_selection is True

    # Train through the real manager snapshot and durable model publication paths.
    # Five independent frames over four seconds make both distinct templates ready.
    for area, pattern in ((area_a, PATTERN_A), (area_b, PATTERN_B)):
        for _ in range(5):
            _receive_packet(runtime, clock, pattern)
            trained = await manager.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area.id})
            assert trained["accepted"] is True
            assert trained["added_samples"] == 1
        assert trained["ready"] is True
    templates_before = deepcopy(manager.fingerprints.export_state())
    assert all(label["samples"] == 5 and label["ready"] for label in manager.fingerprints.summary())

    _receive_packet(runtime, clock, PATTERN_A)
    coordinator._refresh_areas_by_min_distance()
    advisory = BermudaSensorAreaSuggestion(coordinator, runtime.entry, DEVICE)
    assert runtime.device.area_id == area_a.id
    assert advisory.native_value == area_a.name
    cached = deepcopy(manager._suggestions[DEVICE])
    assert cached["accepted"] is True
    assert cached["suggested_area"] == area_a.id
    assert cached["confidence"] == 1

    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_save(payload):
        entered.set()
        await release.wait()

    manager.store.async_save.side_effect = blocked_save
    pending = asyncio.create_task(manager.async_dispatch("settings", {"settings": {"history_enabled": True}}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert manager._lock.locked()
        assert not pending.done()
        if changed_input == "expired":
            # Still within the legacy range timeout, but too old for a fingerprint.
            clock.mono += 16
            clock.epoch += 16
            expected_area, expected_name = None, None
        else:
            _receive_packet(runtime, clock, PATTERN_B)
            expected_area, expected_name = area_b.id, area_b.name
        # The recorder has not run, and its previous A result is still cached.
        assert manager._suggestions[DEVICE] == cached

        coordinator._refresh_areas_by_min_distance()
        assert runtime.device.area_id == expected_area
        assert advisory.native_value == expected_name
        current = manager.area_suggestion(runtime.device)
        assert current["accepted"] is (changed_input == "moved")
        if changed_input == "expired":
            assert "stale_receiver" in {reason["code"] for reason in current["reasons"]}
        else:
            assert current["suggested_area"] == area_b.id
            assert current["confidence"] == 1

        state = await manager.async_dispatch("state", {})
        panel_result = state["fingerprint_results"][DEVICE]
        assert panel_result["accepted"] is (changed_input == "moved")
        if changed_input == "moved":
            assert panel_result["suggested_area"] == area_b.id
        assert not pending.done()
        assert manager._lock.locked()
        # Queries neither record training frames nor change independent labels.
        assert manager.fingerprints.export_state() == templates_before
    finally:
        release.set()
        await pending
        manager.store.async_save.side_effect = None
