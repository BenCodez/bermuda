"""Real-manager replay keeps imported evidence independent of live identities."""

from copy import deepcopy
import time

import pytest

from .test_range_replay import frames
from .test_toolkit_runtime import DEVICE, SCANNER, runtime


async def captured_export(runtime, *, redact=True):
    manager, advert = runtime.manager, runtime.advert
    await manager.async_dispatch(
        "profile",
        {
            "kind": "receiver",
            "id": SCANNER,
            "profile": {"reference_power": -59, "attenuation": 2, "rssi_offset": 0},
        },
    )
    advert.rssi, advert.rssi_distance_raw, advert.rssi_distance = -79, 10, 10
    await manager.async_dispatch(
        "capture", {"device_id": DEVICE, "location_id": "measured-site", "ranges": {SCANNER: 1}, "accuracy_m": 0.1}
    )
    return await manager.async_dispatch("history_export", {} if redact else {"redact": False})


@pytest.mark.parametrize("redact", [True, False])
async def test_imported_redacted_or_offline_candidates_replay_without_live_roster(runtime, redact):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=redact)
    row = exported["frames"][0]["observations"][0]
    scanner, device = row["scanner_id"], row["device_id"]
    assert exported["redacted"] is True
    if redact:
        assert scanner != SCANNER and device != DEVICE
    else:
        assert scanner == SCANNER and device == DEVICE
    await manager.async_dispatch("history_import", {"data": exported})
    runtime.coordinator.scanner_list_del(runtime.scanner)
    runtime.coordinator.devices.pop(DEVICE)
    before = deepcopy(dict(runtime.entry.options)), manager.history.frames(), deepcopy(manager._quality)
    saved = manager.store.async_save.call_count
    report = await manager.async_dispatch("replay", {"candidate": {scanner: 20}, "filter_modes": {device: "pet"}})
    assert report["counts"]["labelled"] == 1
    assert report["observations"][0]["current"] == 10
    assert report["observations"][0]["candidate"] == pytest.approx(1)
    assert report["observations"][0]["candidate_pipeline"] == "replayed_pet_filter"
    assert report["range_metrics"]["candidate"]["mae_m"] == pytest.approx(0)
    with pytest.raises(ValueError, match="unknown receiver"):
        await manager.async_dispatch("shadow_start", {"candidate": {scanner: 20}})
    with pytest.raises(ValueError, match="unknown device"):
        await manager.async_dispatch("shadow_start", {"candidate": {}, "filter_modes": {device: "pet"}})
    with pytest.raises(ValueError, match="unknown receiver"):
        await manager._apply({scanner: 20})
    assert (dict(runtime.entry.options), manager.history.frames(), manager._quality) == before
    assert manager.store.async_save.call_count == saved
    assert manager.shadow is None


async def test_uppercase_mac_import_applies_lowercase_live_shadow_candidate(runtime):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    row, label = exported["frames"][0]["observations"][0], exported["frames"][0]["truth"][0]
    row.update(device_id=DEVICE.upper(), scanner_id=SCANNER.upper())
    label.update(device_id=DEVICE.upper(), ranges={SCANNER.upper(): 1})
    await manager.async_dispatch("history_import", {"data": exported})
    before = deepcopy(dict(runtime.entry.options)), manager.history.frames()
    report = await manager.async_dispatch("replay", {"candidate": {SCANNER: 20}, "filter_modes": {DEVICE: "pet"}})
    assert report["observations"][0]["candidate"] == pytest.approx(1)
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 20}, "filter_modes": {DEVICE: "pet"}})
    shadow_report = manager.shadow.evaluate()["report"]
    assert shadow_report["observations"][0]["current"] == 10
    assert shadow_report["observations"][0]["candidate"] == pytest.approx(1)
    assert shadow_report["observations"][0]["candidate_pipeline"] == "replayed_pet_filter"
    assert (dict(runtime.entry.options), manager.history.frames()) == before


async def test_imported_opaque_candidates_require_exact_frozen_case(runtime):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    scanner, device = "receiver:CaseSensitive", "beacon:CaseSensitive"
    row, label = exported["frames"][0]["observations"][0], exported["frames"][0]["truth"][0]
    row.update(scanner_id=scanner, device_id=device)
    label.update(device_id=device, ranges={scanner: 1})
    await manager.async_dispatch("history_import", {"data": exported})
    report = await manager.async_dispatch("replay", {"candidate": {scanner: 20}, "filter_modes": {device: "pet"}})
    assert report["observations"][0]["candidate"] == pytest.approx(1)
    with pytest.raises(ValueError, match="unknown receiver"):
        await manager.async_dispatch("replay", {"candidate": {scanner.lower(): 20}})
    with pytest.raises(ValueError, match="unknown device"):
        await manager.async_dispatch("replay", {"candidate": {}, "filter_modes": {device.lower(): "pet"}})


async def test_frozen_mac_aliases_reject_conflicting_candidate_offsets_and_filters(runtime):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    row = exported["frames"][0]["observations"][0]
    exported["frames"][0]["observations"].append(
        {**deepcopy(row), "scanner_id": SCANNER.upper(), "device_id": DEVICE.upper(), "stamp": row["stamp"] + 1}
    )
    await manager.async_dispatch("history_import", {"data": exported})
    before, saved = manager.history.frames(), manager.store.async_save.call_count
    with pytest.raises(ValueError, match="Conflicting offsets"):
        await manager.async_dispatch("replay", {"candidate": {SCANNER: 20, SCANNER.upper(): 19}})
    with pytest.raises(ValueError, match="Conflicting filter modes"):
        await manager.async_dispatch(
            "replay", {"candidate": {}, "filter_modes": {DEVICE: "pet", DEVICE.upper(): "stationary"}}
        )
    report = await manager.async_dispatch(
        "replay",
        {
            "candidate": {SCANNER: 20, SCANNER.upper(): 20},
            "filter_modes": {DEVICE: "pet", DEVICE.upper(): "pet"},
        },
    )
    assert report["proposed_offsets"] == {SCANNER: 20}
    assert report["filter_modes"] == {DEVICE: "pet"}
    assert report["counts"]["labelled"] == 2
    assert all(row["candidate"] == pytest.approx(1) for row in report["observations"])
    assert manager.history.frames() == before
    assert manager.store.async_save.call_count == saved


@pytest.mark.parametrize("offset", [True, False, float("nan"), float("inf"), -float("inf"), 21, -21, "20"])
async def test_detached_candidates_reject_invalid_offsets_without_writes(runtime, offset):
    manager = runtime.manager
    exported = await captured_export(runtime)
    await manager.async_dispatch("history_import", {"data": exported})
    scanner = exported["frames"][0]["observations"][0]["scanner_id"]
    before, saved = manager.history.frames(), manager.store.async_save.call_count
    with pytest.raises(ValueError, match="outside safe bounds"):
        await manager.async_dispatch("replay", {"candidate": {scanner: offset}})
    assert manager.history.frames() == before
    assert manager.store.async_save.call_count == saved


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"candidate": {"unknown-receiver": 20}}, "unknown receiver"),
        ({"candidate": {SCANNER: 20}}, "unknown receiver"),
        ({"candidate": {f"scanner-{index}": 0 for index in range(129)}}, "bounded object"),
        ({"candidate": {}, "filter_modes": {"unknown-device": "pet"}}, "unknown device"),
        ({"candidate": {}, "filter_modes": {DEVICE: "pet"}}, "unknown device"),
    ],
)
async def test_detached_candidates_reject_unknown_frozen_identities_and_oversized_maps(runtime, payload, error):
    manager = runtime.manager
    await manager.async_dispatch("history_import", {"data": await captured_export(runtime)})
    with pytest.raises(ValueError, match=error):
        await manager.async_dispatch("replay", payload)


async def test_shadow_filter_promotion_revalidates_live_device_before_persisting(runtime):
    manager = runtime.manager
    exported = await captured_export(runtime, redact=False)
    baseline = exported["frames"][0]
    baseline["captured_at"] = baseline["observations"][0]["observed"] = baseline["truth"][0]["observed"] = (
        time.time() - 10
    )
    await manager.async_dispatch("history_import", {"data": exported})
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 20}, "filter_modes": {DEVICE: "pet"}})
    captured = frames(distance=10, expected=1)
    now, version = time.time() - len(captured), manager.version()
    for index, frame in enumerate(captured):
        frame.update(captured_at=now + index, model_version=version)
        row, label = frame["observations"][0], frame["truth"][0]
        row.update(device_id=DEVICE, scanner_id=SCANNER, observed=now + index, model_version=version, rssi=-79)
        label.update(device_id=DEVICE, ranges={SCANNER: 1}, observed=now + index)
        assert manager.shadow.add_frame(frame) == 1
    check = manager.shadow.promotion_check(current_model_version=version)
    assert check["eligible"], check["reasons"]
    runtime.coordinator.devices.pop(DEVICE)
    before, saved = deepcopy(dict(runtime.entry.options)), manager.store.async_save.call_count
    with pytest.raises(ValueError, match="unknown device"):
        await manager.async_dispatch("shadow_promote", {})
    assert dict(runtime.entry.options) == before
    assert manager.store.async_save.call_count == saved
    assert manager.shadow.state == "cancelled"
