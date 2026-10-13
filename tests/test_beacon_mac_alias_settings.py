"""Accepted beacon MAC representations share the effective model and saved owner."""

from copy import deepcopy

import pytest

from custom_components.bermuda.toolkit import OPTIONS_KEY, ToolkitManager

from .test_profile_kind_boundaries import evidence, evidence_runtime
from .test_toolkit_runtime import DEVICE, SCANNER, runtime
from .test_toolkit_store import durable_runtime, real_store


@pytest.mark.parametrize(("first", "second"), [(DEVICE.upper(), DEVICE), (DEVICE, DEVICE.upper())])
async def test_beacon_mac_profile_applies_saves_reloads_merges_and_removes(durable_runtime, tmp_path, first, second):
    runtime = durable_runtime
    manager = runtime.manager
    baseline = manager.version()
    stamp, rssi = runtime.advert.stamp, runtime.advert.rssi
    profile = {"reference_power": -62.0, "attenuation": 2.7, "reference_power_verified": True}
    result = await manager.async_dispatch("profile", {"kind": "beacon", "id": first, "profile": profile})
    assert result["profile"] == profile
    assert manager.settings["beacon_profiles"] == {DEVICE: profile}
    assert runtime.entry.options[OPTIONS_KEY]["beacon_profiles"] == {DEVICE: profile}
    assert manager.effective_advert_model(runtime.advert)["reference_power"] == -62.0
    assert runtime.advert.rssi_distance_raw == pytest.approx(10 ** ((-62.0 - (rssi + 1.5)) / 27.0))
    version = manager.version()
    assert version != baseline
    result = await manager.async_dispatch("profile", {"kind": "beacon", "id": second, "profile": {"notes": "Measured"}})
    assert result["profile"] == {**profile, "notes": "Measured"}
    assert manager.version() == version
    saved = await manager.store.async_load()
    assert saved["options_journal"]["desired"][OPTIONS_KEY]["beacon_profiles"] == manager.settings["beacon_profiles"]
    await manager.async_close()
    resumed = ToolkitManager(runtime.coordinator)
    resumed.store = real_store(runtime.hass, tmp_path, manager.store.key)
    runtime.coordinator.toolkit = resumed
    try:
        await resumed.async_load()
        assert resumed.settings["beacon_profiles"] == {DEVICE: {**profile, "notes": "Measured"}}
        assert resumed.effective_advert_model(runtime.advert)["reference_power"] == -62.0
        result = await resumed.async_dispatch(
            "profile",
            {
                "kind": "beacon",
                "id": first,
                "profile": {},
                "remove": ["reference_power", "attenuation", "reference_power_verified"],
            },
        )
        assert result["profile"] == {"notes": "Measured"}
        assert resumed.version() == baseline
        assert resumed.effective_advert_model(runtime.advert)["reference_power"] == -55.0
        assert (runtime.advert.stamp, runtime.advert.rssi) == (stamp, rssi)
    finally:
        await resumed.async_close()
        runtime.coordinator.toolkit = manager


@pytest.mark.parametrize(
    "field,value",
    [
        ("beacon_profiles", {"reference_power": -61.0}),
        ("filters", "pet"),
        ("fixed_references", {"location_id": "Synthetic site", "ranges": {SCANNER: 2.5}}),
    ],
)
async def test_settings_canonicalize_single_and_equal_mac_aliases_before_response(runtime, field, value):
    result = await runtime.manager.async_dispatch(
        "settings", {"settings": {field: {DEVICE.upper(): value, DEVICE: value}}}
    )
    assert list(result["options"][field]) == [DEVICE]
    assert list(runtime.entry.options[OPTIONS_KEY][field]) == [DEVICE]
    if field == "beacon_profiles":
        assert runtime.manager.effective_advert_model(runtime.advert)["reference_power"] == -61.0
    elif field == "filters":
        assert runtime.manager.settings["filters"][DEVICE] == "pet"
    else:
        assert result["options"][field][DEVICE]["device_id"] == DEVICE
        await runtime.manager._after_update()
        assert runtime.manager._labelled_rows()[0]["expected_distance"] == 2.5


@pytest.mark.parametrize(
    "field,left,right",
    [
        ("beacon_profiles", {"reference_power": -61.0}, {"reference_power": -62.0}),
        ("filters", "pet", "legacy"),
        (
            "fixed_references",
            {"location_id": "Synthetic site", "ranges": {SCANNER: 2.5}},
            {"location_id": "Synthetic site", "ranges": {SCANNER: 3.0}},
        ),
    ],
)
async def test_conflicting_beacon_mac_aliases_reject_atomically_with_existing_evidence(
    evidence_runtime, field, left, right
):
    state, manager = evidence_runtime, evidence_runtime.manager
    before = evidence(state)
    options, settings = deepcopy(dict(state.entry.options)), deepcopy(manager.settings)
    saved = manager.store.async_save.await_count
    with pytest.raises(ValueError, match="same beacon"):
        await manager.async_dispatch("settings", {"settings": {field: {DEVICE: left, DEVICE.upper(): right}}})
    assert dict(state.entry.options) == options
    assert manager.settings == settings
    assert manager.store.async_save.await_count == saved
    assert evidence(state) == before


async def test_loaded_uppercase_beacon_settings_and_opaque_ids_keep_their_owners(runtime):
    manager = runtime.manager
    options = deepcopy(dict(runtime.entry.options))
    options[OPTIONS_KEY] = {
        "beacon_profiles": {
            DEVICE.upper(): {"reference_power": -63.0},
            "Opaque/Beacon": {"reference_power": -60.0},
            "opaque/beacon": {"reference_power": -65.0},
        }
    }
    runtime.hass.config_entries.async_update_entry(runtime.entry, options=options)
    assert await manager.async_options_changed(runtime.entry)
    assert manager.effective_advert_model(runtime.advert)["reference_power"] == -63.0
    assert DEVICE in runtime.entry.options[OPTIONS_KEY]["beacon_profiles"]
    assert DEVICE.upper() not in runtime.entry.options[OPTIONS_KEY]["beacon_profiles"]
    assert manager.settings["beacon_profiles"] == {
        DEVICE: {"reference_power": -63.0},
        "Opaque/Beacon": {"reference_power": -60.0},
        "opaque/beacon": {"reference_power": -65.0},
    }
    result = await manager.async_dispatch(
        "profile", {"kind": "beacon", "id": "Opaque/Beacon", "profile": {"notes": "Exact"}}
    )
    assert result["profile"] == {"reference_power": -60.0, "notes": "Exact"}
    assert manager.settings["beacon_profiles"]["opaque/beacon"] == {"reference_power": -65.0}
