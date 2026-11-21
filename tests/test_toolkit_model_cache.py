"""Regression coverage for the per-update effective radio-model cache."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from custom_components.bermuda import toolkit as toolkit_module
from custom_components.bermuda.const import CONF_REF_POWER, CONF_RSSI_OFFSETS
from custom_components.bermuda.profiles import effective_model
from tests.test_toolkit_model_rebase import model_runtime
from tests.test_toolkit_runtime import DEVICE, SCANNER, runtime


class _ModelAdvert:
    """Hashable advert-shaped value for model resolution only."""

    def __init__(self, *, scanner=SCANNER, device=DEVICE, power=0.0):
        self.scanner_address = scanner
        self.device_address = device
        self.ref_power = power


def test_effective_model_beacon_override_matches_a_materialized_profile_without_mutation():
    """Fixed references can avoid copying all settings while preserving precedence."""
    settings = {
        "receiver_profiles": {"receiver": {"attenuation": 2.5}},
        "beacon_profiles": {"beacon": {"reference_power": -61.0, "reference_power_verified": True}},
    }
    override = {"reference_power": -67.0, "reference_power_verified": False}
    expected_settings = {
        **settings,
        "beacon_profiles": {**settings["beacon_profiles"], "beacon": override},
    }
    kwargs = {"reference_power": -55.0, "attenuation": 3.0, "rssi_offset": 1.0, "device_reference_power": -59.0}

    assert effective_model(settings, "receiver", "beacon", beacon_profile=override, **kwargs) == effective_model(
        expected_settings, "receiver", "beacon", **kwargs
    )
    assert settings["beacon_profiles"]["beacon"]["reference_power"] == -61.0


async def test_batch_cache_matches_uncached_models_and_separates_owner_and_scanner(model_runtime, monkeypatch):
    """Cache keys retain both the tracked owner and packet source identity."""
    state = model_runtime
    manager = state.manager
    source = _ModelAdvert()
    alias = _ModelAdvert(device="1234567890abcdef1234567890abcdef_1_1")
    other_scanner = _ModelAdvert(scanner="aa:bb:cc:dd:ee:13")
    manager.settings["beacon_profiles"][alias.device_address] = {"reference_power": -67.0}
    manager.settings["receiver_profiles"][other_scanner.scanner_address] = {"attenuation": 2.0}
    manager._owners.update({source: DEVICE, alias: alias.device_address, other_scanner: DEVICE})
    uncached = {advert: manager.effective_advert_model(advert) for advert in (source, alias, other_scanner)}

    original = toolkit_module.effective_model
    calls = []

    def counted(*args, **kwargs):
        calls.append((args[1], args[2], kwargs["device_reference_power"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(toolkit_module, "effective_model", counted)
    with manager.advert_model_batch():
        for _ in range(3):
            assert manager.effective_advert_model(source) == uncached[source]
            assert manager.effective_advert_model(alias) == uncached[alias]
            assert manager.effective_advert_model(other_scanner) == uncached[other_scanner]
        manager._owners[source] = alias.device_address
        assert manager.effective_advert_model(source) == uncached[alias]

    assert calls == [
        (SCANNER, DEVICE, 0.0),
        (SCANNER, alias.device_address, 0.0),
        (other_scanner.scanner_address, DEVICE, 0.0),
    ]


async def test_batch_normalizes_legacy_power_and_reads_options_profiles_freshly(model_runtime, monkeypatch):
    """Only a same-batch model is reused; changing radio inputs gets a new model."""
    state = model_runtime
    manager = state.manager
    advert = _ModelAdvert(power=0)
    manager._owners[advert] = DEVICE

    original = toolkit_module.effective_model
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(toolkit_module, "effective_model", counted)
    with manager.advert_model_batch():
        initial = manager.effective_advert_model(advert)
        advert.ref_power = 0.0  # Legacy unset aliases share the normalized key.
        assert manager.effective_advert_model(advert) is initial
        advert.ref_power = -63.0  # Metadevice preparation can alter this mid-batch.
        calibrated = manager.effective_advert_model(advert)
        assert calibrated["reference_power"] == -63.0
    assert len(calls) == 2

    state.coordinator.options[CONF_REF_POWER] = -50.0
    advert.ref_power = 0.0
    with manager.advert_model_batch():
        assert manager.effective_advert_model(advert)["reference_power"] == -50.0

    await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"attenuation": 2.0}})
    state.coordinator.options[CONF_RSSI_OFFSETS][SCANNER] = 4.0
    with manager.advert_model_batch():
        refreshed = manager.effective_advert_model(advert)
    assert refreshed["attenuation"] == 2.0
    assert refreshed["rssi_offset"] == 4.0


async def test_batch_cache_stops_retaining_new_models_at_its_bound(model_runtime, monkeypatch):
    """A crowded update may recompute, but it must not grow the retained cache."""
    state = model_runtime
    manager = state.manager
    adverts = [_ModelAdvert(scanner=f"aa:bb:cc:dd:ee:{index:02x}") for index in range(3)]
    for advert in adverts:
        manager._owners[advert] = DEVICE

    original = toolkit_module.effective_model
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(toolkit_module, "MAX_MODELS_PER_UPDATE", 2)
    monkeypatch.setattr(toolkit_module, "effective_model", counted)
    with manager.advert_model_batch():
        for advert in adverts:
            manager.effective_advert_model(advert)
        manager.effective_advert_model(adverts[-1])
        assert len(manager._advert_models) == 2
    assert calls == 4


async def test_coordinator_batch_releases_on_failure_and_expired_measurement_stays_unavailable(
    model_runtime, monkeypatch
):
    """The real update pipeline bounds its cache and never refreshes stale range data."""
    state = model_runtime
    coordinator, manager, advert = state.coordinator, state.manager, state.advert
    coordinator._waitingfor_load_manufacturer_ids = False
    monkeypatch.setattr(coordinator, "_async_gather_advert_data", lambda: None)
    entered = exited = 0
    original_batch = manager.advert_model_batch

    @contextmanager
    def observed_batch():
        nonlocal entered, exited
        entered += 1
        with original_batch():
            try:
                yield
            finally:
                exited += 1

    monkeypatch.setattr(manager, "advert_model_batch", observed_batch)
    device_type = type(state.device)
    original_calculate = device_type.calculate_data

    def fail_calculation(_device):
        msg = "synthetic update failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(device_type, "calculate_data", fail_calculation)
    with pytest.raises(RuntimeError, match="synthetic"):
        coordinator._async_update_data_internal()
    assert (entered, exited) == (1, 1)
    assert manager._advert_models is None

    monkeypatch.setattr(device_type, "calculate_data", original_calculate)
    state.clock["now"] += 31
    coordinator._async_update_data_internal()
    assert (entered, exited) == (2, 2)
    assert manager._advert_models is None
    assert advert.rssi_distance is None
    assert advert.hist_distance_by_interval == []
