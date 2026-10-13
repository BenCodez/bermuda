"""Atomic target patches retain unrelated current settings and legacy replacement."""

from copy import deepcopy

import pytest

from .test_toolkit_runtime import DEVICE, SCANNER, runtime  # noqa: F401

OTHER_DEVICE = "aa:bb:cc:dd:ee:02"
OTHER_SCANNER = "aa:bb:cc:dd:ee:34"


@pytest.mark.parametrize(
    ("field", "first", "second", "initial", "external", "edited"),
    [
        ("receiver_limits", SCANNER, OTHER_SCANNER, 7, 17, 10),
        ("filters", DEVICE, OTHER_DEVICE, "pet", "stationary", "pet"),
    ],
)
async def test_target_patch_retains_a_later_change_to_another_target(
    runtime, field, first, second, initial, external, edited
):
    manager = runtime.manager
    await manager.async_dispatch("settings", {"settings": {field: {second: initial}}})
    cached_panel_map = deepcopy(manager.settings[field])
    await manager.async_dispatch("settings", {"settings": {field: {second: external}}})
    assert cached_panel_map[second] != manager.settings[field][second]
    other_settings = {key: deepcopy(value) for key, value in manager.settings.items() if key != field}
    result = await manager.async_dispatch("settings", {"settings": {field: {first: edited}}, "patch_maps": True})
    assert result["options"][field] == {first: edited, second: external}
    assert {key: value for key, value in manager.settings.items() if key != field} == other_settings


@pytest.mark.parametrize("patch_maps", [False, None])
async def test_whole_map_replacement_remains_available(runtime, patch_maps):
    manager = runtime.manager
    await manager.async_dispatch("settings", {"settings": {"receiver_limits": {OTHER_SCANNER: 17}}})
    payload = {"settings": {"receiver_limits": {SCANNER: 10}}}
    if patch_maps is not None:
        payload["patch_maps"] = patch_maps
    result = await manager.async_dispatch("settings", payload)
    assert result["options"]["receiver_limits"] == {SCANNER: 10}


async def test_patch_mac_case_updates_the_existing_slot_and_opaque_case_stays_distinct(runtime):
    manager = runtime.manager
    await manager.async_dispatch("settings", {"settings": {"receiver_limits": {SCANNER.upper(): 3, "Opaque": 4}}})
    result = await manager.async_dispatch(
        "settings", {"settings": {"receiver_limits": {SCANNER: 8, "opaque": 9}}, "patch_maps": True}
    )
    assert result["options"]["receiver_limits"] == {SCANNER.upper(): 8, "Opaque": 4, "opaque": 9}


@pytest.mark.parametrize("second_value", [8, 9])
async def test_duplicate_mac_patch_targets_coalesce_or_reject_conflicts(runtime, second_value):
    manager = runtime.manager
    before = deepcopy(dict(runtime.entry.options))
    payload = {"settings": {"receiver_limits": {SCANNER: 8, SCANNER.upper(): second_value}}, "patch_maps": True}
    if second_value == 9:
        with pytest.raises(ValueError, match="conflicting receiver aliases"):
            await manager.async_dispatch("settings", payload)
        assert dict(runtime.entry.options) == before
        assert manager.store.async_save.await_count == 0
    else:
        result = await manager.async_dispatch("settings", payload)
        assert result["options"]["receiver_limits"] == {SCANNER: 8}


@pytest.mark.parametrize("flag", [None, 0, 1, "true", [], {}])
async def test_invalid_patch_flags_leave_options_and_store_unchanged(runtime, flag):
    manager = runtime.manager
    before = deepcopy(dict(runtime.entry.options))
    with pytest.raises(ValueError, match="patch_maps"):
        await manager.async_dispatch("settings", {"settings": {}, "patch_maps": flag})
    assert dict(runtime.entry.options) == before
    assert manager.store.async_save.await_count == 0


@pytest.mark.parametrize(
    "change",
    [{"auto_calibration": True}, {"receiver_profiles": {}}, {"receiver_limits": []}, {"filters": {DEVICE: "bad"}}],
)
async def test_invalid_patch_content_cannot_publish_partial_changes(runtime, change):
    manager = runtime.manager
    before = deepcopy(dict(runtime.entry.options))
    with pytest.raises(ValueError):
        await manager.async_dispatch("settings", {"settings": change, "patch_maps": True})
    assert dict(runtime.entry.options) == before
    assert manager.store.async_save.await_count == 0
