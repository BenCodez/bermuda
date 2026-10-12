"""Synthetic source packets cannot become independent truth through relabelling."""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.bermuda import api as api_module
from custom_components.bermuda import fingerprints
from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.fingerprints import FingerprintModel

from .test_fingerprint_live_freshness import PATTERN_A, SECOND_SCANNER, _receive_packet
from .test_fingerprints import DEVICE as PURE_DEVICE
from .test_fingerprints import ROOM_A, codes, snapshot
from .test_toolkit_runtime import DEVICE, SOURCE_STAMP, _add_receiver, runtime


def _train(model, label, stamps, *, device=PURE_DEVICE, version="synthetic-v1", observed=None, vector=ROOM_A):
    frames = [snapshot(stamp, vector, device=device, version=version) for stamp in stamps]
    if observed is not None:
        for rows in frames:
            for row in rows:
                row["observed"] = observed
    return model.train(
        device, label, frames, independent=True, now=observed if observed is not None else 1000 + max(stamps)
    )


@pytest.mark.parametrize("reload", [False, True])
def test_five_ready_snapshots_cannot_be_relabelled_into_a_second_ready_template(reload):
    model = FingerprintModel()
    assert _train(model, "area:a", range(1, 6))["ready"]
    if reload:
        restored = FingerprintModel()
        assert restored.import_state(json.loads(json.dumps(model.export_state())))["accepted"]
        model = restored
    before = model.export_state()
    duplicate = _train(model, "area:b", range(1, 6))
    assert duplicate["accepted"] is False
    assert duplicate["ready"] is False
    assert duplicate["added_samples"] == duplicate["samples"] == 0
    assert "insufficient_fresh_receivers" in codes(duplicate)
    assert model.export_state() == before
    suggestion = model.suggest(PURE_DEVICE, snapshot(6), now=1006)
    assert suggestion["accepted"] is True
    assert suggestion["suggested_area"] == "area:a"
    assert len(suggestion["scores"]) == 1
    # New independent packets can deliberately describe another area, even
    # when the physical RSSI patterns coincide and thus remain ambiguous.
    assert _train(model, "area:b", range(6, 11))["ready"]
    assert "ambiguous_locations" in codes(model.suggest(PURE_DEVICE, snapshot(11), now=1011))


def test_cross_label_mixed_receiver_snapshot_requires_two_fresh_receivers_and_stores_only_them():
    model = FingerprintModel()
    vector = {**ROOM_A, "scanner:three": -65}
    assert _train(model, "area:a", [1], vector=vector)["accepted"]
    rows = snapshot(2, vector)
    rows[0]["stamp"] = rows[1]["stamp"] = 1
    one_fresh = model.train(PURE_DEVICE, "area:b", rows, independent=True, now=1002)
    assert one_fresh["accepted"] is False
    assert one_fresh["added_samples"] == 0
    assert "insufficient_fresh_receivers" in codes(one_fresh)
    rows[1]["stamp"] = 2
    two_fresh = model.train(PURE_DEVICE, "area:b", rows, independent=True, now=1002)
    assert two_fresh["accepted"] is True
    frame = model.export_state()["labels"][1]["frames"][0]
    assert frame["stamps"] == {"scanner:two": 2, "scanner:three": 2}
    assert set(frame["values"]) == set(frame["observed"]) == set(frame["stamps"])
    assert not _train(model, "area:c", [2], vector=vector)["accepted"]


@pytest.mark.parametrize("scope", ["label", "device", "all"])
@pytest.mark.parametrize("reload", [False, True])
def test_clearing_templates_does_not_reset_source_provenance(scope, reload):
    model = FingerprintModel()
    assert _train(model, "area:a", range(1, 6))["ready"]
    arguments = {"label": (PURE_DEVICE, "area:a"), "device": (PURE_DEVICE,), "all": ()}[scope]
    assert model.clear(*arguments) == 1
    assert model.summary() == []
    if reload:
        restored = FingerprintModel()
        assert restored.import_state(model.export_state())["accepted"]
        model = restored
    assert not _train(model, "area:b", range(1, 6))["accepted"]
    assert _train(model, "area:b", range(6, 11))["ready"]


@pytest.mark.parametrize("reload", [False, True])
def test_evicted_receiver_frames_keep_high_water_marks_across_labels(reload):
    model = FingerprintModel()
    vector = {**ROOM_A, "scanner:evicted": -65}
    assert _train(model, "area:a", [1], vector=vector)["accepted"]
    for stamp in range(2, fingerprints.MAX_FRAMES_PER_LABEL + 3):
        assert _train(model, "area:a", [stamp])["accepted"]
    state = model.export_state()
    assert len(state["labels"][0]["frames"]) == fingerprints.MAX_FRAMES_PER_LABEL
    assert all("scanner:evicted" not in frame["stamps"] for frame in state["labels"][0]["frames"])
    assert state["source_clocks"][PURE_DEVICE]["scanner:evicted"] == 1
    if reload:
        restored = FingerprintModel()
        assert restored.import_state(state)["accepted"]
        model = restored
    result = _train(model, "area:b", [1], vector={"scanner:evicted": -65, "scanner:new": -60}, observed=1100)
    assert result["accepted"] is False
    assert "insufficient_fresh_receivers" in codes(result)
    # The rejected attempt must not consume the genuinely new receiver.
    assert _train(model, "area:b", [2], vector={"scanner:evicted": -65, "scanner:new": -60}, observed=1101)["accepted"]


@pytest.mark.parametrize("reload", [False, True])
def test_radio_model_changes_do_not_make_cached_packets_independent_training_samples(reload):
    model = FingerprintModel()
    assert _train(model, "area:a", [5])["accepted"]
    model.clear(PURE_DEVICE)
    if reload:
        restored = FingerprintModel()
        assert restored.import_state(model.export_state())["accepted"]
        model = restored
    assert not _train(model, "area:b", [5], version="synthetic-v2", observed=1200)["accepted"]
    assert _train(model, "area:b", [6], version="synthetic-v2", observed=1201)["accepted"]
    assert not _train(model, "area:c", [6], version="synthetic-v1", observed=1202)["accepted"]


def test_other_device_source_clocks_are_independent_and_new_session_resets_only_source_provenance():
    model = FingerprintModel()
    assert _train(model, "area:a", range(100, 105))["ready"]
    assert _train(model, "area:b", range(1, 6), device="opaque:other")["ready"]
    model.new_session()
    assert model.export_state()["source_clocks"] == {}
    assert all(label["ready"] for label in model.summary())
    assert _train(model, "area:c", [1], observed=1200)["accepted"]
    restored = FingerprintModel()
    assert restored.import_state(model.export_state())["accepted"]
    assert not _train(restored, "area:d", [1], observed=1201)["accepted"]
    assert not _train(restored, "area:d", [0], observed=1202)["accepted"]
    assert _train(restored, "area:d", [2], observed=1203)["accepted"]


@pytest.mark.parametrize("active_epoch", [False, True])
def test_old_schema_exports_recover_source_clocks_from_all_labels(active_epoch):
    model = FingerprintModel()
    _train(model, "area:a", [1])
    _train(model, "area:b", [2])
    state = model.export_state()
    del state["source_clocks"]
    if not active_epoch:
        del state["active_session_epoch"]
        for label in state["labels"]:
            for frame in label["frames"]:
                del frame["session_epoch"]
    restored = FingerprintModel()
    assert restored.import_state(state)["accepted"]
    assert not _train(restored, "area:c", [2])["accepted"]
    assert _train(restored, "area:c", [3])["accepted"]


def test_imported_clock_map_cannot_lower_provenance_present_in_active_frames():
    model = FingerprintModel()
    _train(model, "area:a", [10])
    state = model.export_state()
    state["source_clocks"][PURE_DEVICE] = {"scanner:one": 0}
    assert model.import_state(state)["accepted"]
    assert model.export_state()["source_clocks"][PURE_DEVICE] == {scanner: 10 for scanner in ROOM_A}
    assert not _train(model, "area:b", [10])["accepted"]


@pytest.mark.parametrize("invalid", [None, [], True, -1, math.nan, 1e13, "10"])
def test_source_clock_import_validation_is_atomic(invalid):
    model = FingerprintModel()
    _train(model, "area:a", [10])
    before = model.export_state()
    state = deepcopy(before)
    state["source_clocks"][PURE_DEVICE]["scanner:one"] = invalid
    assert model.import_state(state)["accepted"] is False
    assert model.export_state() == before


@pytest.mark.parametrize("kind", ["devices", "receivers", "total", "identifier", "map"])
def test_source_clock_import_budgets_and_schema_are_bounded(kind, monkeypatch):
    model = FingerprintModel()
    _train(model, "area:a", [1])
    before = model.export_state()
    state = deepcopy(before)
    if kind == "devices":
        state["source_clocks"] = {f"device:{i}": {"scanner:one": 1} for i in range(fingerprints.MAX_DEVICES + 1)}
    elif kind == "receivers":
        state["source_clocks"][PURE_DEVICE] = {f"scanner:{i}": 1 for i in range(fingerprints.MAX_CLOCKS_PER_DEVICE + 1)}
    elif kind == "total":
        monkeypatch.setattr(fingerprints, "MAX_TOTAL_SOURCE_CLOCKS", 1)
    elif kind == "identifier":
        state["source_clocks"][""] = {"scanner:one": 1}
    else:
        state["source_clocks"] = []
    assert not model.import_state(state)["accepted"]
    assert model.export_state() == before


def test_training_clock_budget_rejection_does_not_consume_any_packets(monkeypatch):
    model = FingerprintModel()
    assert _train(model, "area:a", [1])["accepted"]
    before = model.export_state()
    monkeypatch.setattr(fingerprints, "MAX_CLOCKS_PER_DEVICE", 2)
    vector = {"scanner:one": -45, "scanner:three": -70}
    result = _train(model, "area:b", [2], vector=vector)
    assert not result["accepted"]
    assert "source_clock_budget" in codes(result)
    assert model.export_state() == before
    monkeypatch.setattr(fingerprints, "MAX_CLOCKS_PER_DEVICE", 3)
    assert _train(model, "area:b", [2], vector=vector)["accepted"]


@pytest.mark.parametrize("budget", ["devices", "total"])
def test_cleared_devices_keep_bounded_clocks_without_eviction(budget, monkeypatch):
    model = FingerprintModel()
    assert _train(model, "area:a", [1])["accepted"]
    model.clear()
    before = model.export_state()
    if budget == "devices":
        monkeypatch.setattr(fingerprints, "MAX_DEVICES", 1)
    else:
        monkeypatch.setattr(fingerprints, "MAX_TOTAL_SOURCE_CLOCKS", 3)
    rejected = _train(model, "area:b", [1], device="opaque:other")
    assert not rejected["accepted"]
    assert "source_clock_budget" in codes(rejected)
    assert model.export_state() == before
    assert not _train(model, "area:b", [1])["accepted"]
    assert _train(model, "area:b", [2])["accepted"]
    model.clear()
    model.new_session()
    assert _train(model, "area:c", [1], device="opaque:other")["accepted"]


def test_persisted_clock_only_state_uses_the_existing_byte_budget(monkeypatch):
    model = FingerprintModel()
    assert _train(model, "area:a", [1])["accepted"]
    model.clear()
    state = model.export_state()
    assert state["labels"] == []
    full_size = len(json.dumps(state, allow_nan=False).encode())
    monkeypatch.setattr(fingerprints, "MAX_STATE_BYTES", full_size - 1)
    assert not model.import_state(state)["accepted"]
    assert model.export_state() == state


@pytest.fixture
async def labelled_runtime(runtime, monkeypatch):
    clock = SimpleNamespace(mono=SOURCE_STAMP, epoch=time.time())
    for module in ("api", "toolkit", "bermuda_advert", "bermuda_device", "coordinator"):
        monkeypatch.setattr(f"custom_components.bermuda.{module}.monotonic_time_coarse", lambda: clock.mono)
    monkeypatch.setattr(api_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    monkeypatch.setattr(toolkit_module, "time", SimpleNamespace(time=lambda: clock.epoch))
    _add_receiver(runtime.coordinator, runtime.device, SECOND_SCANNER, clock.mono)
    runtime.clock = clock
    return runtime


@pytest.mark.parametrize("change", ["label", "clear", "model", "reload"])
async def test_public_manager_training_cannot_relabel_cached_receiver_packets(labelled_runtime, change):
    runtime = labelled_runtime
    manager = runtime.manager
    area_a = runtime.coordinator.ar.async_create("Synthetic Independent Area A")
    area_b = runtime.coordinator.ar.async_create("Synthetic Independent Area B")
    for _ in range(5):
        _receive_packet(runtime, runtime.clock, PATTERN_A)
        first = await manager.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area_a.id})
        assert first["accepted"] is True
    assert first["ready"] is True
    if change == "clear":
        result = await manager.async_dispatch("fingerprint_clear", {"device_id": DEVICE})
        assert result["cleared"] == 1
    elif change == "model":
        await manager.async_dispatch("profile", {"kind": "beacon", "id": DEVICE, "profile": {"reference_power": -56}})
    elif change == "reload":
        saved = deepcopy(manager.store.async_save.call_args.args[0]["fingerprints"])
        restored = FingerprintModel()
        assert restored.import_state(saved)["accepted"]
        manager.fingerprints = restored
    before = manager.fingerprints.export_state()
    source_before = {scanner: advert.stamp for (_device, scanner), advert in runtime.device.adverts.items()}
    for _ in range(5):
        # A later user click/publication is not a new source observation.
        runtime.clock.epoch += 1
        duplicate = await manager.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area_b.id})
        assert duplicate["accepted"] is False
        assert duplicate["added_samples"] == duplicate["samples"] == 0
        assert duplicate["ready"] is False
        assert "insufficient_fresh_receivers" in codes(duplicate)
    assert manager.fingerprints.export_state() == before
    assert {scanner: advert.stamp for (_device, scanner), advert in runtime.device.adverts.items()} == source_before
    for _ in range(5):
        _receive_packet(runtime, runtime.clock, PATTERN_A)
        fresh = await manager.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area_b.id})
        assert fresh["accepted"] is True
    assert fresh["ready"] is True and fresh["samples"] == 5
