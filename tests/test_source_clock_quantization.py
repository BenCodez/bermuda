"""Small source-clock skew cannot poison healthy receiver recording batches."""

from __future__ import annotations

from copy import deepcopy
import time
from types import SimpleNamespace

import pytest

from custom_components.bermuda import api as api_module
from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.api import snapshot_observations

from .test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _add_receiver, runtime

NEAR_FUTURE = "aa:bb:cc:dd:ee:22"
FAR_FUTURE = "aa:bb:cc:dd:ee:32"


@pytest.mark.parametrize("source_skew", [0.1, 0.8, 0.800001])
async def test_quantized_future_sources_record_and_capture_without_poisoning_healthy_receivers(
    runtime, monkeypatch, source_skew
):
    clock = SimpleNamespace(mono=SOURCE_STAMP, epoch=time.time())
    monkeypatch.setattr(api_module, "monotonic_time_coarse", lambda: clock.mono)
    monkeypatch.setattr(toolkit_module, "monotonic_time_coarse", lambda: clock.mono)
    monkeypatch.setattr(api_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    monkeypatch.setattr(toolkit_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    manager = runtime.manager
    _add_receiver(runtime.coordinator, runtime.device, NEAR_FUTURE, SOURCE_STAMP + source_skew)
    _add_receiver(runtime.coordinator, runtime.device, FAR_FUTURE, SOURCE_STAMP + 2)
    await manager.async_dispatch("history_start", {})

    rows = {row["scanner_id"]: row for row in snapshot_observations(manager._snapshot())}
    accepted = source_skew <= 0.8
    near = rows[NEAR_FUTURE]
    assert near["stamp"] == SOURCE_STAMP + source_skew
    assert near["status"] == ("current" if accepted else "stale")
    assert near["observed"] == (clock.epoch if accepted else None)
    assert near["age"] == (0 if accepted else pytest.approx(-source_skew))
    assert rows[FAR_FUTURE]["status"] == "stale"
    assert "source_clock_in_future" in rows[FAR_FUTURE]["quality"]["rejection_reasons"]

    await manager._after_update()
    recorded = [row for frame in manager.history.frames() for row in frame["observations"]]
    expected_scanners = {SCANNER, NEAR_FUTURE} if accepted else {SCANNER}
    assert {row["scanner_id"] for row in recorded} == expected_scanners
    assert all(row["observed"] <= clock.epoch for row in recorded)

    result = await manager.async_dispatch(
        "capture",
        {
            "device_id": DEVICE,
            "location_id": "synthetic-measured-site",
            "ranges": {SCANNER: 2.0, NEAR_FUTURE: 3.0, FAR_FUTURE: 4.0},
        },
    )
    assert result["captured"] == len(expected_scanners)
    labelled = manager.history.frames()
    assert len(labelled) == 1
    assert {row["scanner_id"] for row in labelled[0]["observations"]} == expected_scanners
    assert len(labelled[0]["truth"]) == 1
    assert all(row["stamp"] == rows[row["scanner_id"]]["stamp"] for row in labelled[0]["observations"])
    frozen = deepcopy(labelled)

    # Publishing again does not turn the same raw source stamps into new data.
    # Crossing the coarse-clock boundary leaves already recorded UTC unchanged.
    if accepted:
        clock.mono += 1
        clock.epoch += 1
        await manager._after_update()
        assert manager.history.frames() == frozen
        later = {row["scanner_id"]: row for row in snapshot_observations(manager._snapshot())}
        assert later[NEAR_FUTURE]["age"] == pytest.approx(1 - source_skew)
        assert later[SCANNER]["observed"] == rows[SCANNER]["observed"]
        assert later[FAR_FUTURE]["status"] == "stale"
