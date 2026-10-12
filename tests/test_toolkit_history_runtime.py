"""Real HA manager lifecycle tests using synthetic source observations."""

from copy import deepcopy
import math
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.bermuda.api import async_get_advert_snapshot
from custom_components.bermuda.const import CONF_RSSI_OFFSETS
from custom_components.bermuda.toolkit import ToolkitManager

from .test_measurement_api import DEVICE, SCANNER, _coordinator


class SyntheticAdvert(SimpleNamespace):
    """Identity semantics match production advertisement objects used as owners."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__


@pytest.fixture
async def runtime(hass, monkeypatch):
    coordinator, device, advert, scanner = _coordinator(hass, entry_id="history-runtime")
    advert = SyntheticAdvert(**vars(advert))
    device.adverts = {key: advert for key in device.adverts}
    coordinator.hass = hass
    coordinator.get_scanners = [scanner]
    coordinator.ar = ar.async_get(hass)
    coordinator.dr = dr.async_get(hass)
    scanner.async_update_scanner_device = MagicMock()
    advert._update_raw_distance = MagicMock()
    advert.rssi_distance_raw = advert.rssi_distance = 4.0
    advert.rssi = -59 - 20 * math.log10(4)
    advert.conf_rssi_offset = 0.0
    manager = coordinator.toolkit = ToolkitManager(coordinator)
    clock = {"utc": time.time() - 10, "stamp": 100.0}

    def snapshot():
        with patch("custom_components.bermuda.api.monotonic_time_coarse", return_value=clock["stamp"]):
            return async_get_advert_snapshot(
                hass, entry_id=coordinator.config_entry.entry_id, now=clock["utc"], include_empty=True
            )

    monkeypatch.setattr(manager, "_snapshot", snapshot)

    def advance(*, new_source=True):
        clock["utc"] += 1
        clock["stamp"] += 1
        if new_source:
            advert.stamp = clock["stamp"]

    value = SimpleNamespace(
        manager=manager,
        coordinator=coordinator,
        device=device,
        advert=advert,
        scanner=scanner,
        clock=clock,
        advance=advance,
    )
    yield value
    await manager.async_close()


def measured(location="site-a"):
    return {"device_id": DEVICE, "location_id": location, "ranges": {SCANNER: 2}, "accuracy_m": 0.1}


async def complete_update(manager):
    manager.after_update(0.001)
    assert manager._background is not None
    await manager._background


async def test_actual_api_runtime_records_source_once_across_poll_cycles(runtime):
    manager = runtime.manager
    await manager.async_dispatch("history_start", {"devices": [DEVICE]})
    await complete_update(manager)
    original = manager.history.frames()[0]["observations"][0]
    runtime.advance(new_source=False)
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 1
    assert manager.history.frames()[0]["observations"][0] == original
    assert original["stamp"] == 98
    assert original["observed"] < manager.history.frames()[0]["captured_at"]
    assert original["observed_clock"] == "derived_monotonic"
    runtime.advance()
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 2
    assert manager.history.frames()[-1]["truth"] == []


async def test_manual_capture_with_history_off_freezes_instant_truth(runtime):
    manager = runtime.manager
    assert manager.history.enabled is False
    captured = await manager.async_dispatch("capture", measured())
    assert captured["captured"] == 1
    frozen = manager.history.frames()[0]
    assert frozen["truth"][0]["source"] == "measured"
    assert frozen["truth"][0]["observed"] == frozen["captured_at"]
    runtime.advance()
    await complete_update(manager)
    assert manager.history.enabled is False
    assert manager.history.frames() == [frozen]
    assert len(manager._labelled_rows()) == 1
    assert manager._labelled_rows()[0]["stamp"] == frozen["observations"][0]["stamp"]


async def test_manual_capture_while_enabled_labels_one_original_packet(runtime):
    manager = runtime.manager
    await manager.async_dispatch("history_start", {})
    await complete_update(manager)
    source = manager.history.frames()[0]["observations"][0]["stamp"]
    runtime.advance(new_source=False)
    result = await manager.async_dispatch("capture", measured())
    assert result["captured"] == 1
    assert manager.history.summary()["rows"] == 1
    assert manager.history.frames()[0]["observations"][0]["stamp"] == source
    runtime.advance()
    await complete_update(manager)
    assert len(manager.history.frames()) == 2
    assert manager.history.frames()[-1]["truth"] == []
    assert len(manager._labelled_rows()) == 1


async def test_shadow_collects_fresh_sources_and_only_manual_captures_supply_truth(runtime):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    options = deepcopy(dict(manager.entry.options))
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 6}})
    for _ in range(3):
        runtime.advance()
        await complete_update(manager)
    report = manager.shadow.evaluate()["report"]
    new = [row for row in report["observations"] if row["stamp"] != 98]
    assert len(new) == 3 and all(row["expected_distance"] is None for row in new)
    rejected = await manager.async_dispatch("shadow_promote", {})
    assert rejected["eligible"] is False
    assert dict(manager.entry.options) == options
    assert manager.shadow.state == "active"
    for index in range(6):
        runtime.advance()
        await complete_update(manager)
        await manager.async_dispatch("capture", measured("site-a" if index < 3 else "site-b"))
    assert manager.shadow.state == "active"
    check = manager.shadow.promotion_check(current_model_version=manager.version())
    assert check["eligible"], check["reasons"]
    assert check["new_labelled_rows"] == 6
    assert dict(manager.entry.options) == options
    promoted = await manager.async_dispatch("shadow_promote", {})
    assert promoted["promoted"] is True
    assert manager.entry.options[CONF_RSSI_OFFSETS][SCANNER] == 6
    assert manager.shadow.state == "promoted"


async def test_fixed_references_label_only_new_packets_and_duplicate_cycles_remain_healthy(runtime):
    manager = runtime.manager
    await manager.async_dispatch(
        "settings",
        {
            "settings": {
                "fixed_references": {DEVICE: {"location_id": "reference-a", "ranges": {SCANNER: 2}, "accuracy_m": 0.1}}
            }
        },
    )
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 1
    assert manager.history.frames()[0]["truth"][0]["source"] == "fixed_reference"
    runtime.advance(new_source=False)
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 1
    assert not manager.last_result or manager.last_result.get("status") != "pending"
    runtime.advance()
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 2
    assert all(frame["truth"][0]["source"] == "fixed_reference" for frame in manager.history.frames())


async def test_redacted_or_unknown_imports_remain_replay_only_until_new_live_validation(runtime):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    exported = await manager.async_dispatch("history_export", {})
    options = deepcopy(dict(manager.entry.options))
    await manager.async_dispatch("history_import", {"data": exported})
    assert manager._labelled_rows() == []
    replay = await manager.async_dispatch("replay", {})
    assert replay["counts"]["labelled"] == 1
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 6}})
    check = await manager.async_dispatch("shadow_promote", {})
    assert check["eligible"] is False
    assert dict(manager.entry.options) == options
    redacted_scanner = exported["frames"][0]["observations"][0]["scanner_id"]
    with pytest.raises(ValueError, match="unknown receiver"):
        await manager.async_dispatch("shadow_start", {"candidate": {redacted_scanner: 6}})
    unknown = deepcopy(manager.history.export(redact=False))
    unknown["frames"][0]["observations"][0]["device_id"] = "unknown-device"
    unknown["frames"][0]["truth"][0]["device_id"] = "unknown-device"
    await manager.async_dispatch("history_import", {"data": unknown})
    assert manager._labelled_rows() == []


async def test_failed_history_import_restores_memory_and_live_settings(runtime):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    original = manager.history.frames()
    options = deepcopy(dict(manager.entry.options))
    data = manager.history.export(redact=False)
    with patch.object(manager.store, "async_save", side_effect=OSError("synthetic persistence failure")):
        with pytest.raises(OSError):
            await manager.async_dispatch("history_import", {"data": data})
    assert manager.history.frames() == original
    assert dict(manager.entry.options) == options


async def test_manual_capture_store_restore_uses_validated_explicit_import(runtime):
    manager = runtime.manager
    await manager.async_dispatch("capture", measured())
    frozen = manager.history.frames()
    restored = ToolkitManager(runtime.coordinator)
    await restored.async_load()
    assert restored.history.frames() == frozen
    assert restored.history.enabled is False
    await restored.async_close()


async def test_failed_measured_capture_persistence_leaves_history_and_shadow_unchanged(runtime):
    manager = runtime.manager
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 6}})
    runtime.advance()
    await complete_update(manager)
    history, shadow, options = (
        manager.history.frames(),
        manager.shadow.evaluate(),
        deepcopy(dict(manager.entry.options)),
    )
    with patch.object(manager.store, "async_save", side_effect=OSError("synthetic capture save failure")):
        with pytest.raises(OSError):
            await manager.async_dispatch("capture", measured())
    assert manager.history.frames() == history
    assert manager.shadow.evaluate() == shadow
    assert dict(manager.entry.options) == options


async def test_failed_shadow_promotion_persistence_cancels_without_live_mutation(runtime):
    manager = runtime.manager
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 6}})
    for index in range(6):
        runtime.advance()
        await complete_update(manager)
        await manager.async_dispatch("capture", measured("site-a" if index < 3 else "site-b"))
    assert manager.shadow.promotion_check(current_model_version=manager.version())["eligible"]
    options = deepcopy(dict(manager.entry.options))
    with patch.object(manager.store, "async_save", side_effect=OSError("synthetic promotion save failure")):
        with pytest.raises(OSError):
            await manager.async_dispatch("shadow_promote", {})
    assert dict(manager.entry.options) == options
    assert manager.coordinator.options.get(CONF_RSSI_OFFSETS, {}) == options.get(CONF_RSSI_OFFSETS, {})
    assert manager.shadow.state == "cancelled"


async def test_explicit_capture_other_device_after_selected_history_stops(runtime):
    manager = runtime.manager
    other_id = "aa:bb:cc:dd:ee:02"
    other_advert = SyntheticAdvert(**vars(runtime.advert))
    other_advert.device_address = other_id
    other_device = SimpleNamespace(**vars(runtime.device))
    other_device.address = other_device.unique_id = other_id
    other_device.adverts = {(other_id, SCANNER): other_advert}
    runtime.coordinator.devices[other_id] = other_device
    await manager.async_dispatch("history_start", {"devices": [DEVICE]})
    await complete_update(manager)
    await manager.async_dispatch("history_stop", {})
    captured = await manager.async_dispatch("capture", {**measured(), "device_id": other_id})
    assert captured["captured"] == 1
    assert manager.history.enabled is False
    assert manager.history.selected_devices == [DEVICE]
    labelled = manager._labelled_rows()
    assert len(labelled) == 1 and labelled[0]["device_id"] == other_id
    restored = deepcopy(manager.history.export(redact=False))
    await manager.async_dispatch("history_import", {"data": restored})
    assert len(manager._labelled_rows()) == 1


async def test_fixed_reference_beacons_with_staggered_source_cadence_keep_fresh_labels(runtime):
    manager = runtime.manager
    other_id = "aa:bb:cc:dd:ee:02"
    other_advert = SyntheticAdvert(**vars(runtime.advert))
    other_advert.device_address = other_id
    other_device = SimpleNamespace(**vars(runtime.device))
    other_device.address = other_device.unique_id = other_id
    other_device.adverts = {(other_id, SCANNER): other_advert}
    runtime.coordinator.devices[other_id] = other_device
    await manager.async_dispatch(
        "settings",
        {
            "settings": {
                "fixed_references": {
                    device: {"location_id": location, "ranges": {SCANNER: 2}, "accuracy_m": 0.1}
                    for device, location in ((DEVICE, "reference-a"), (other_id, "reference-b"))
                }
            }
        },
    )
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 2
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 6}})
    runtime.advance(new_source=False)
    other_advert.stamp = runtime.clock["stamp"]
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 3
    assert [label["device_id"] for label in manager.history.frames()[-1]["truth"]] == [other_id]
    runtime.advance()
    await complete_update(manager)
    assert manager.history.summary()["rows"] == 4
    assert [label["device_id"] for label in manager.history.frames()[-1]["truth"]] == [DEVICE]
    assert len(manager._labelled_rows()) == 4
    assert not manager.last_result or manager.last_result.get("status") != "pending"
    assert manager.shadow.state == "active"
    report = manager.shadow.evaluate()["report"]
    assert report["counts"]["observations"] == 4
    assert report["counts"]["labelled"] == 4
