"""Real-manager source aliases cannot turn baseline packets into new truth."""

from copy import deepcopy
import math

import pytest

from .test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _add_receiver, runtime
from .test_toolkit_shared_owners import BEACON, register_alias

SECOND_SCANNER = "aa:bb:cc:dd:ee:22"


@pytest.mark.parametrize("uppercase", [(), ("device_id",), ("scanner_id",), ("device_id", "scanner_id")])
@pytest.mark.parametrize("device_id", [DEVICE, BEACON])
async def test_imported_source_baseline_requires_new_two_location_filter_evidence(
    runtime, monkeypatch, uppercase, device_id
):
    manager = runtime.manager
    adverts = [runtime.advert, _add_receiver(runtime.coordinator, runtime.device, SECOND_SCANNER, SOURCE_STAMP)]
    if device_id == BEACON:
        meta = register_alias(runtime)
        assert meta.metadevice_sources == [DEVICE]
    clock = {"stamp": SOURCE_STAMP}
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: clock["stamp"])
    for scanner in (SCANNER, SECOND_SCANNER):
        await manager.async_dispatch(
            "profile",
            {
                "kind": "receiver",
                "id": scanner,
                "profile": {"reference_power": -59, "attenuation": 2, "rssi_offset": 0},
            },
        )

    def set_packet(stamp):
        clock["stamp"] = stamp
        for advert in adverts:
            advert.stamp = stamp
            advert.rssi = -59 - 20 * math.log10(2)
            advert.rssi_distance_raw, advert.rssi_distance = 2, 4
            advert.scanner_device.stamps[DEVICE.upper()] = stamp
            advert.scanner_device.last_seen = stamp
        runtime.coordinator.update_metadevices()
        manager.prepare_update()

    async def capture(location, *, device=device_id):
        return await manager.async_dispatch(
            "capture",
            {
                "device_id": device,
                "location_id": location,
                "ranges": {SCANNER: 2, SECOND_SCANNER: 2},
                "accuracy_m": 0.1,
            },
        )

    set_packet(SOURCE_STAMP)
    await manager.async_dispatch("history_start", {"devices": [DEVICE]})
    assert (await capture("cached-site", device=DEVICE))["captured"] == 2
    exported = await manager.async_dispatch("history_export", {"redact": False})
    assert exported["frames"][0]["session_epoch"] == 2
    for frame in exported["frames"]:
        for row in frame["observations"]:
            for field in uppercase:
                row[field] = row[field].upper()
        for label in frame["truth"]:
            if "device_id" in uppercase:
                label["device_id"] = label["device_id"].upper()
            if "scanner_id" in uppercase:
                label["ranges"] = {scanner.upper(): distance for scanner, distance in label["ranges"].items()}
    await manager.async_dispatch("history_import", {"data": exported})
    assert manager.history.session_epoch == 3
    imported = manager.history.frames()
    candidate_device = DEVICE.upper() if device_id == DEVICE else device_id
    await manager.async_dispatch("shadow_start", {"candidate": {}, "filter_modes": {candidate_device: "pet"}})
    assert manager.shadow.filter_modes == {device_id: "pet"}
    assert manager._shadow_history.session_epoch == 1
    assert (
        manager.shadow.evaluate()["report"]["observations"][0]["device_id"]
        == imported[0]["observations"][0]["device_id"]
    )
    with pytest.raises(ValueError, match="wait for fresh source"):
        await capture("cached-site")
    assert manager.history.frames() == imported
    assert manager.shadow.summary()["new_frames"] == 0
    assert manager.shadow.promotion_check(current_model_version=manager.version())["new_labelled_rows"] == 0

    for stamp in (SOURCE_STAMP + 1, SOURCE_STAMP + 2):
        set_packet(stamp)
        assert (await capture("fresh-site"))["captured"] == 2
        if device_id == BEACON:
            assert (await capture("fresh-site", device=DEVICE))["captured"] == 2
    options = deepcopy(dict(runtime.entry.options))
    settings = deepcopy(manager.settings)
    saved = manager.store.async_save.await_count
    rejected = await manager.async_dispatch("shadow_promote", {})
    assert rejected["eligible"] is False
    assert rejected["new_labelled_rows"] == 4
    assert rejected["new_data_range_metrics"]["current"]["count"] == (8 if device_id == BEACON else 4)
    assert rejected["new_locations"] == ["fresh-site"]
    assert dict(runtime.entry.options) == options
    assert manager.settings == settings
    assert manager.store.async_save.await_count == saved
    assert manager.shadow.state == "active"

    set_packet(SOURCE_STAMP + 3)
    assert (await capture("other-fresh-site"))["captured"] == 2
    if device_id == BEACON:
        assert (await capture("other-fresh-site", device=DEVICE))["captured"] == 2
    promoted = await manager.async_dispatch("shadow_promote", {})
    assert promoted["promoted"] is True
    assert promoted["validation"]["new_labelled_rows"] == 6
    assert promoted["validation"]["new_locations"] == ["fresh-site", "other-fresh-site"]
    assert manager.settings["filters"] == {device_id: "pet"}
    assert manager.shadow.state == "promoted"
