"""Profile edits use the same receiver identity as the live radio model."""

import asyncio
import json
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from custom_components.bermuda.api import public_device_id
from custom_components.bermuda.const import CONF_RSSI_OFFSETS
from custom_components.bermuda.toolkit import OPTIONS_KEY

from .test_toolkit_runtime import DEVICE, OFFLINE, SCANNER, runtime

MIXED = "Aa:Bb:Cc:Dd:Ee:12"
ORIGINAL = {"reference_power": -62.0, "attenuation": 2.4, "rssi_offset": 5.0, "manufacturer": "Original"}


def radio(state):
    advert = state.advert
    return {
        "raw": advert.rssi_distance_raw,
        "filtered": advert.rssi_distance,
        "raw_history": list(advert.hist_distance),
        "filtered_history": list(advert.hist_distance_by_interval),
        "rssi": advert.rssi,
        "rssi_history": list(advert.hist_rssi),
        "stamp": advert.stamp,
    }


def assert_radio(state, expected):
    actual = radio(state)
    for key, value in expected.items():
        assert actual[key] == pytest.approx(value)


def rebased_radio(snapshot, old_model, new_model):
    """Independently invert each stored range to RSSI, then apply the new model."""

    def rebase(distance):
        implied_rssi = old_model["reference_power"] - 10 * old_model["attenuation"] * math.log10(distance)
        implied_rssi -= old_model["rssi_offset"]
        return 10 ** (
            (new_model["reference_power"] - implied_rssi - new_model["rssi_offset"]) / (10 * new_model["attenuation"])
        )

    expected = deepcopy(snapshot)
    for key in ("raw", "filtered"):
        expected[key] = rebase(expected[key])
    for key in ("raw_history", "filtered_history"):
        expected[key] = [rebase(value) for value in expected[key]]
    return expected


async def setup_profile(state, key=SCANNER.upper(), profile=None):
    await state.manager.async_dispatch(
        "profile", {"kind": "receiver", "id": key, "profile": profile if profile is not None else ORIGINAL}
    )
    advert = state.advert
    advert.rssi_distance = advert.rssi_distance_raw * 1.2
    advert.hist_distance[:] = [advert.rssi_distance_raw, advert.rssi_distance_raw * 1.4]
    advert.hist_distance_by_interval[:] = [advert.rssi_distance, advert.rssi_distance * 1.3]


@pytest.mark.parametrize(("stored", "requested"), [(SCANNER.upper(), SCANNER), (SCANNER, MIXED)])
async def test_alias_metadata_edit_preserves_key_numeric_model_and_live_outputs(runtime, stored, requested):
    manager = runtime.manager
    await setup_profile(runtime, stored)
    before, version = radio(runtime), manager.version()
    revision = runtime.device.model_revision
    legacy = deepcopy(dict(runtime.entry.options[CONF_RSSI_OFFSETS]))
    metadata = {"environment": "outdoor", "notes": "Measured installation", "antenna_gain_dbi": 2.0}
    result = await manager.async_dispatch("profile", {"kind": "receiver", "id": requested, "profile": metadata})
    assert result["profile"] == {**ORIGINAL, **metadata}
    assert manager.settings["receiver_profiles"] == {stored: {**ORIGINAL, **metadata}}
    assert runtime.entry.options[OPTIONS_KEY]["receiver_profiles"] == manager.settings["receiver_profiles"]
    assert manager.version() == version
    assert runtime.device.model_revision == revision
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 5.0
    assert manager.effective_advert_model(runtime.advert)["reference_power"] == -62.0
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == legacy
    assert_radio(runtime, before)
    result["profile"]["rssi_offset"] = -1.0
    assert manager.settings["receiver_profiles"][stored]["rssi_offset"] == 5.0


@pytest.mark.parametrize("field", ["reference_power", "attenuation", "rssi_offset"])
async def test_alias_explicit_remove_reverts_only_selected_numeric_to_legacy(runtime, field):
    manager = runtime.manager
    await setup_profile(runtime)
    before, version = radio(runtime), manager.version()
    old_model = manager.effective_advert_model(runtime.advert)
    legacy = deepcopy(dict(runtime.entry.options[CONF_RSSI_OFFSETS]))
    result = await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"notes": "Updated"}, "remove": [field]}
    )
    expected_profile = {**ORIGINAL, "notes": "Updated"}
    expected_profile.pop(field)
    assert result["profile"] == expected_profile
    assert manager.settings["receiver_profiles"] == {SCANNER.upper(): expected_profile}
    new_model = manager.effective_advert_model(runtime.advert)
    assert new_model[field] == {"reference_power": -55.0, "attenuation": 3.0, "rssi_offset": 1.5}[field]
    for retained in {"reference_power", "attenuation", "rssi_offset"} - {field}:
        assert new_model[retained] == old_model[retained]
    assert manager.version() != version
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == legacy
    assert_radio(runtime, rebased_radio(before, old_model, new_model))


@pytest.mark.parametrize("change", [{"rssi_offset": 6.0}, {"reference_power": -61.0}, {"attenuation": 2.1}])
async def test_alias_numeric_edit_updates_existing_profile_and_reprojects_histories(runtime, change):
    manager = runtime.manager
    await setup_profile(runtime)
    before, version = radio(runtime), manager.version()
    old_model = manager.effective_advert_model(runtime.advert)
    legacy = deepcopy(dict(runtime.entry.options[CONF_RSSI_OFFSETS]))
    result = await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": change})
    assert result["profile"] == {**ORIGINAL, **change}
    assert manager.settings["receiver_profiles"] == {SCANNER.upper(): {**ORIGINAL, **change}}
    assert manager.version() != version
    assert runtime.entry.options[CONF_RSSI_OFFSETS] == legacy
    new_model = manager.effective_advert_model(runtime.advert)
    assert_radio(runtime, rebased_radio(before, old_model, new_model))


@pytest.mark.parametrize("requested", [SCANNER, SCANNER.upper()])
async def test_duplicate_aliases_keep_exact_slot_precedence(runtime, requested):
    manager = runtime.manager
    profiles = {SCANNER: {"rssi_offset": 3.0}, SCANNER.upper(): {"rssi_offset": 5.0}}
    await manager.async_dispatch("settings", {"settings": {"receiver_profiles": profiles}})
    before, version = radio(runtime), manager.version()
    result = await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": requested, "profile": {"notes": "Exact slot"}}
    )
    expected = deepcopy(profiles)
    expected[requested]["notes"] = "Exact slot"
    assert result["profile"] == expected[requested]
    assert manager.settings["receiver_profiles"] == expected
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 3.0
    assert manager.version() == version
    assert_radio(runtime, before)


async def test_ambiguous_alias_without_exact_slot_follows_live_legacy_fallback(runtime):
    manager = runtime.manager
    profiles = {SCANNER.upper(): {"rssi_offset": 5.0}, MIXED: {"rssi_offset": 6.0}}
    await manager.async_dispatch("settings", {"settings": {"receiver_profiles": profiles}})
    before, version = radio(runtime), manager.version()
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 1.5
    result = await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": SCANNER, "profile": {"notes": "Independent exact slot"}}
    )
    assert result["profile"] == {"notes": "Independent exact slot"}
    assert manager.settings["receiver_profiles"] == {**profiles, SCANNER: result["profile"]}
    assert manager.effective_advert_model(runtime.advert)["rssi_offset"] == 1.5
    assert manager.version() == version
    assert_radio(runtime, before)


@pytest.mark.parametrize("requested", ["receiver/Case", "receiver/case"])
async def test_opaque_source_profile_ids_remain_case_sensitive(runtime, requested):
    manager = runtime.manager
    await setup_profile(runtime, "receiver/Case", {"rssi_offset": 5.0})
    result = await manager.async_dispatch(
        "profile", {"kind": "receiver", "id": requested, "profile": {"notes": "Opaque source"}}
    )
    expected = {"receiver/Case": {"rssi_offset": 5.0}}
    expected[requested] = {**expected.get(requested, {}), "notes": "Opaque source"}
    assert manager.settings["receiver_profiles"] == expected
    assert result["profile"] == expected[requested]
    source = SimpleNamespace(scanner_address="receiver/Case", device_address=DEVICE, ref_power=0.0)
    assert manager.effective_advert_model(source, device_id=DEVICE)["rssi_offset"] == 5.0


@pytest.mark.parametrize("fail_write", [1, 2])
@pytest.mark.parametrize("change", [{"notes": "Metadata"}, {"rssi_offset": 6.0}])
async def test_alias_edit_save_failure_restores_options_profile_and_all_radio_outputs(runtime, fail_write, change):
    manager = runtime.manager
    await setup_profile(runtime)
    before, version = radio(runtime), manager.version()
    options = deepcopy(dict(runtime.entry.options))
    saves = []

    async def save(payload):
        saves.append(deepcopy(payload))
        if len(saves) == fail_write:
            raise OSError("Synthetic alias profile write failure")

    manager.store.async_save.side_effect = save
    with pytest.raises(OSError, match="alias profile write failure"):
        await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": change})
    assert dict(runtime.entry.options) == options
    assert manager.settings["receiver_profiles"] == {SCANNER.upper(): ORIGINAL}
    assert manager.version() == version
    assert manager.trial is None
    assert_radio(runtime, before)


async def test_external_profile_radio_edit_wins_during_paused_alias_metadata_save(runtime):
    manager = runtime.manager
    await setup_profile(runtime)
    before = radio(runtime)
    old_model = manager.effective_advert_model(runtime.advert)
    baseline = deepcopy(dict(runtime.entry.options))
    entered, release = asyncio.Event(), asyncio.Event()
    saves = []

    async def save(payload):
        saves.append(deepcopy(payload))
        if len(saves) == 1:
            entered.set()
            await release.wait()

    manager.store.async_save.side_effect = save
    action = asyncio.create_task(
        manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"notes": "Old edit"}})
    )
    listener = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        external = deepcopy(baseline)
        latest_profile = {**ORIGINAL, "reference_power": -65.0, "rssi_offset": 7.0, "notes": "Latest external edit"}
        external[OPTIONS_KEY]["receiver_profiles"] = {SCANNER.upper(): latest_profile}
        runtime.hass.config_entries.async_update_entry(runtime.entry, options=external)
        listener = asyncio.create_task(manager.async_options_changed(runtime.entry))
        release.set()
        with pytest.raises(RuntimeError, match="changed while saving"):
            await asyncio.wait_for(action, timeout=1)
        assert await asyncio.wait_for(listener, timeout=1)
        assert manager.settings["receiver_profiles"] == {SCANNER.upper(): latest_profile}
        assert runtime.entry.options[OPTIONS_KEY]["receiver_profiles"] == manager.settings["receiver_profiles"]
        assert runtime.entry.options[CONF_RSSI_OFFSETS] == baseline[CONF_RSSI_OFFSETS]
        assert runtime.entry.options[CONF_RSSI_OFFSETS][OFFLINE] == 7.0
        assert_radio(runtime, rebased_radio(before, old_model, manager.effective_advert_model(runtime.advert)))
    finally:
        release.set()
        await asyncio.gather(action, *([listener] if listener else []), return_exceptions=True)


async def test_cancelled_alias_metadata_caller_preserves_one_owned_action_and_live_model(runtime):
    manager = runtime.manager
    await setup_profile(runtime)
    before, version = radio(runtime), manager.version()
    entered, release = asyncio.Event(), asyncio.Event()
    saves = []

    async def save(payload):
        saves.append(deepcopy(payload))
        if len(saves) == 1:
            entered.set()
            await release.wait()

    manager.store.async_save.side_effect = save
    caller = asyncio.create_task(
        manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"notes": "Accepted"}})
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        owned = manager._action_task
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert owned is not None and not owned.done()
        with pytest.raises(RuntimeError, match="already running"):
            await manager.async_dispatch("profile", {"kind": "receiver", "id": SCANNER, "profile": {"rssi_offset": 9}})
        assert manager.settings["receiver_profiles"] == {SCANNER.upper(): ORIGINAL}
        assert_radio(runtime, before)
        release.set()
        result = await asyncio.wait_for(owned, timeout=1)
        assert result["profile"] == {**ORIGINAL, "notes": "Accepted"}
        assert manager.settings["receiver_profiles"] == {SCANNER.upper(): result["profile"]}
        assert manager.version() == version
        assert_radio(runtime, before)
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_raw_private_beacon_id_merges_and_returns_only_public_profile(runtime):
    manager = runtime.manager
    private = "1234567890abcdef1234567890abcdef"
    public = public_device_id(private)
    original = {"reference_power": -61.0, "reference_power_verified": True}
    result = await manager.async_dispatch("profile", {"kind": "beacon", "id": private, "profile": original})
    assert result["profile"] == original
    assert manager.settings["beacon_profiles"] == {public: original}
    version = manager.version()
    metadata = {"manufacturer": "Measured beacon", "notes": "Independent reference"}
    result = await manager.async_dispatch("profile", {"kind": "beacon", "id": private.upper(), "profile": metadata})
    assert result["profile"] == {**original, **metadata}
    assert manager.settings["beacon_profiles"] == {public: result["profile"]}
    assert runtime.entry.options[OPTIONS_KEY]["beacon_profiles"] == manager.settings["beacon_profiles"]
    assert manager.version() == version
    assert manager.effective_advert_model(runtime.advert, device_id=public)["reference_power"] == -61.0
    assert private not in json.dumps(await manager.async_dispatch("state", {}))
    assert private not in json.dumps(result)
    assert private not in json.dumps(dict(runtime.entry.options))
