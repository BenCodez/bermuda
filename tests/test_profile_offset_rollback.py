"""Prior receiver overrides survive metadata-only deletion and alias renaming."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from custom_components.bermuda.const import CONF_RSSI_OFFSETS
from custom_components.bermuda.toolkit import OPTIONS_KEY

from .test_toolkit_runtime import OFFLINE, SCANNER, _proposal, runtime


def radio(advert):
    return {
        "raw": advert.rssi_distance_raw,
        "filtered": advert.rssi_distance,
        "raw_history": list(advert.hist_distance),
        "filtered_history": list(advert.hist_distance_by_interval),
    }


async def setup_trial(state, *, profile_key=SCANNER, override=5.0):
    manager, advert = state.manager, state.advert
    await manager.async_dispatch(
        "profile",
        {
            "kind": "receiver",
            "id": profile_key,
            "profile": {"rssi_offset": override, "manufacturer": "Original metadata"},
        },
    )
    advert.rssi_distance = advert.rssi_distance_raw * 1.2
    advert.hist_distance[:] = [advert.rssi_distance_raw, advert.rssi_distance_raw * 1.4]
    advert.hist_distance_by_interval[:] = [advert.rssi_distance, advert.rssi_distance * 1.3]
    baseline = radio(advert)
    legacy = deepcopy(dict(state.entry.options[CONF_RSSI_OFFSETS]))
    manager.proposal = _proposal(manager, {SCANNER: 2.0})
    await manager.async_dispatch("calibration_apply", {})
    assert manager.effective_advert_model(advert)["rssi_offset"] == 2.0
    return baseline, legacy


def assert_radio_matches(advert, baseline):
    current = radio(advert)
    for key in current:
        assert current[key] == pytest.approx(baseline[key])


@pytest.mark.parametrize("prior_key", [SCANNER, SCANNER.upper()])
@pytest.mark.parametrize("override", [0.0, 5.0, -5.0, 30.0])
async def test_removed_profile_restores_prior_override_and_all_live_ranges(runtime, prior_key, override):
    manager = runtime.manager
    baseline, legacy = await setup_trial(runtime, profile_key=prior_key, override=override)
    applied_version = manager.version()
    await manager.async_dispatch("settings", {"settings": {"receiver_profiles": {}}})
    assert manager.version() == applied_version
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 2.0
    result = await manager.async_dispatch("calibration_rollback", {})
    assert result["rolled_back"]
    assert manager.trial is None
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == override
    assert manager.settings["receiver_profiles"] == {prior_key: {"rssi_offset": override}}
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == legacy
    assert runtime.entry.options[CONF_RSSI_OFFSETS][OFFLINE] == 7.0
    assert_radio_matches(runtime.advert, baseline)


@pytest.mark.parametrize(("prior_key", "current_key"), [(SCANNER, SCANNER.upper()), (SCANNER.upper(), SCANNER)])
async def test_case_alias_metadata_edit_keeps_current_metadata_and_one_profile(runtime, prior_key, current_key):
    manager = runtime.manager
    baseline, legacy = await setup_trial(runtime, profile_key=prior_key)
    metadata = {"environment": "outdoor", "notes": "Later independently edited metadata"}
    applied_version = manager.version()
    await manager.async_dispatch("settings", {"settings": {"receiver_profiles": {current_key: metadata}}})
    assert manager.version() == applied_version
    await manager.async_dispatch("calibration_rollback", {})
    assert manager.settings["receiver_profiles"] == {current_key: {**metadata, "rssi_offset": 5.0}}
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 5.0
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == legacy
    assert_radio_matches(runtime.advert, baseline)


async def test_legacy_baseline_stays_legacy_and_keeps_new_metadata(runtime):
    manager = runtime.manager
    baseline = radio(runtime.advert)
    manager.proposal = _proposal(manager, {SCANNER: 2.0})
    await manager.async_dispatch("calibration_apply", {})
    metadata = {"manufacturer": "Later metadata"}
    await manager.async_dispatch("settings", {"settings": {"receiver_profiles": {SCANNER.upper(): metadata}}})
    await manager.async_dispatch("calibration_rollback", {})
    assert manager.settings["receiver_profiles"] == {SCANNER.upper(): metadata}
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 1.5
    assert_radio_matches(runtime.advert, baseline)


@pytest.mark.parametrize("fail_write", [1, 2])
async def test_deleted_profile_rollback_save_failure_retains_applied_radio_and_trial(runtime, fail_write):
    manager = runtime.manager
    await setup_trial(runtime)
    await manager.async_dispatch("settings", {"settings": {"receiver_profiles": {}}})
    applied = radio(runtime.advert)
    options = deepcopy(dict(runtime.entry.options))
    trial = deepcopy(manager.trial)
    saves = []

    async def failing(payload):
        saves.append(deepcopy(payload))
        if len(saves) == fail_write:
            raise OSError("Synthetic rollback persistence failure")

    manager.store.async_save.side_effect = failing
    with pytest.raises(OSError):
        await manager.async_dispatch("calibration_rollback", {})
    assert dict(runtime.entry.options) == options
    assert manager.settings["receiver_profiles"] == {}
    assert manager.trial == trial
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 2.0
    assert_radio_matches(runtime.advert, applied)


async def test_ambiguous_new_case_aliases_reject_atomically_instead_of_inventing_profile(runtime):
    manager = runtime.manager
    await setup_trial(runtime)
    alternate = "Aa:Bb:Cc:Dd:Ee:12"
    await manager.async_dispatch(
        "settings",
        {
            "settings": {
                "receiver_profiles": {SCANNER.upper(): {"manufacturer": "One"}, alternate: {"manufacturer": "Two"}}
            }
        },
    )
    options = deepcopy(dict(runtime.entry.options))
    trial = deepcopy(manager.trial)
    before = radio(runtime.advert)
    save_count = manager.store.async_save.await_count
    with pytest.raises(RuntimeError, match="Ambiguous receiver"):
        await manager.async_dispatch("calibration_rollback", {})
    assert dict(runtime.entry.options) == options
    assert manager.trial == trial
    assert manager.store.async_save.await_count == save_count
    assert_radio_matches(runtime.advert, before)
