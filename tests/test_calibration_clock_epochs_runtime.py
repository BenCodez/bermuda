"""Real measured captures preserve calibration evidence across clock epochs."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.bermuda.bermuda_advert import BermudaAdvert
from custom_components.bermuda.toolkit import ToolkitManager

from .test_calibration_signal_evidence_runtime import CORRECTIONS, advertisement, collect_evidence, evidence_runtime
from .test_toolkit_runtime import SOURCE_STAMP, runtime


@pytest.mark.parametrize("transition", ["reboot", "history-import"])
@pytest.mark.parametrize("noise_db", [0.0, 0.1])
async def test_reused_live_source_clocks_keep_both_measured_scenes(evidence_runtime, monkeypatch, transition, noise_db):
    value = evidence_runtime
    old = value.manager
    first_rows = await collect_evidence(value, fixed=False)
    first_frames = old.history.frames()
    first = await old.async_dispatch("calibration_preview", {})
    first_epoch = old.history.session_epoch
    assert len(first_rows) == 32
    assert first["eligible"]
    assert {row["session_epoch"] for row in first_rows} == {first_epoch}

    old._boot_id = "synthetic-calibration-boot-a"
    saved = deepcopy(old._store_payload())
    exported = await old.async_dispatch("history_export", {"redact": False})
    await old.async_close()
    manager = value.coordinator.toolkit = value.manager = ToolkitManager(value.coordinator)
    manager.store = MagicMock(spec=Store)
    manager.store.async_load = AsyncMock(return_value=saved)
    manager.store.async_save = AsyncMock()
    # A genuinely different host has new advert owners and its own live clock
    # before importing recordings; imported stamps never establish that clock.
    value.clock.mono = SOURCE_STAMP
    value.clock.epoch += 1.0
    for beacon in value.beacons:
        for scanner in value.receivers:
            scanner.stamps[beacon.address.upper()] = SOURCE_STAMP
            advert = BermudaAdvert(beacon, advertisement(-68), value.coordinator.options, scanner)
            beacon.adverts[(beacon.address, scanner.address)] = advert
            advert.calculate_data()
    if transition == "reboot":
        monkeypatch.setattr("custom_components.bermuda.toolkit._host_clock_id", lambda: "synthetic-calibration-boot-b")
        await manager.async_load()
    else:
        await manager.async_dispatch("history_import", {"data": exported})

    try:
        assert manager.history.session_epoch == first_epoch + 1
        assert manager.history.frames() == first_frames
        assert manager._labelled_rows() == first_rows
        assert (await manager.async_dispatch("calibration_preview", {}))["data_id"] == first["data_id"]

        # The second host/session reuses precisely the same source monotonic
        # stamps, while the later UTC capture remains independently fresh.
        labelled = await collect_evidence(value, fixed=False, noise_db=noise_db)
        assert len(labelled) == 64
        assert {row["session_epoch"] for row in labelled} == {first_epoch, first_epoch + 1}
        old_stamps = {(row["device_id"], row["scanner_id"], row["stamp"]) for row in labelled[:32]}
        new_stamps = {(row["device_id"], row["scanner_id"], row["stamp"]) for row in labelled[32:]}
        assert old_stamps == new_stamps
        proposal = await manager.async_dispatch("calibration_preview", {})
        assert proposal["eligible"]
        assert proposal["coverage"]["samples"] == 64
        assert proposal["coverage"]["targets"] == first["coverage"]["targets"]
        assert proposal["offsets"] == pytest.approx(CORRECTIONS, abs=0.1)
        assert proposal["rejected"].get("conflicting_source_packet", 0) == 0
        assert proposal["rejected"].get("duplicate_source_stamp", 0) == 0
        assert proposal["data_id"] != first["data_id"]
        decision = manager.engine.automatic_decision(
            proposal,
            now=value.clock.epoch,
            model_version=manager.version(),
            current_offsets=manager._current_offsets(),
            last_data_id=first["data_id"],
            observations=manager._proposal_rows,
            verified_reference_devices=manager._verified_devices(),
        )
        assert decision["apply"]
        assert decision["step_metrics"]["location_holdout"]["count"] == 32
        assert decision["step_metrics"]["target_holdout"]["count"] == 32

        frozen = manager.history.frames()
        exported = await manager.async_dispatch("history_export", {"redact": False})
        await manager.async_dispatch("history_import", {"data": exported})
        assert manager.history.frames() == frozen
        assert (await manager.async_dispatch("calibration_preview", {}))["data_id"] == proposal["data_id"]
    finally:
        await manager.async_close()
