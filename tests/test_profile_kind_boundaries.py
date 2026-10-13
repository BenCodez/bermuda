"""Profile kinds accept only effective radio fields and preserve rejected edits."""

from copy import deepcopy

import pytest

from custom_components.bermuda.profiles import (
    effective_model,
    model_version,
    normalize_profile,
    normalize_toolkit_settings,
)

from .test_fixed_reference_aliases import _source_measurements
from .test_toolkit_runtime import DEVICE, SCANNER, SOURCE_STAMP, _add_receiver, runtime

SECOND_SCANNER = "aa:bb:cc:dd:ee:22"
METADATA = {
    "hardware_type": "Synthetic hardware",
    "manufacturer": "Synthetic manufacturer",
    "model": "Synthetic model",
    "antenna": "Synthetic antenna",
    "environment": "indoor",
    "notes": "Independent hardware description",
    "height_m": 1.5,
    "antenna_gain_db": 2.0,
    "distance_accuracy_m": 0.1,
}


@pytest.mark.parametrize(
    ("kind", "unsupported"),
    [
        ("beacon", {"rssi_offset": 5.0}),
        ("beacon", {"rssi_offset": None}),
        ("receiver", {"reference_power_verified": False}),
        ("receiver", {"reference_power_verified": True, "reference_power": -61.0}),
    ],
)
def test_profile_kind_rejects_inapplicable_model_fields_at_all_validation_boundaries(kind, unsupported):
    source = {f"{kind}_profiles": {"CaseSensitive/Source": unsupported}}
    before = deepcopy(source)
    with pytest.raises(ValueError, match="profile fields"):
        normalize_profile(unsupported, kind=kind)
    with pytest.raises(ValueError, match="profile fields"):
        normalize_toolkit_settings(source)
    with pytest.raises(ValueError, match="profile fields"):
        model_version(source)
    assert source == before


@pytest.mark.parametrize("kind", ["receiver", "beacon"])
def test_profile_kind_keeps_shared_metadata_supported_models_and_null_inheritance(kind):
    profile = {**METADATA, "reference_power": -61.0, "attenuation": 2.4}
    profile["rssi_offset" if kind == "receiver" else "reference_power_verified"] = 5.0 if kind == "receiver" else True
    settings = normalize_toolkit_settings({f"{kind}_profiles": {"CaseSensitive/Source": profile}})
    normalized = settings[f"{kind}_profiles"]["CaseSensitive/Source"]
    assert normalized == {
        **{key: value for key, value in profile.items() if key != "antenna_gain_db"},
        "antenna_gain_dbi": 2.0,
    }
    assert profile["antenna_gain_db"] == 2.0
    model = effective_model(
        settings,
        "CaseSensitive/Source",
        "CaseSensitive/Source",
        reference_power=-55.0,
        attenuation=3.0,
        rssi_offset=1.5,
    )
    assert model == {
        "reference_power": -61.0,
        "attenuation": 2.4,
        "rssi_offset": 5.0 if kind == "receiver" else 1.5,
        "reference_power_verified": kind == "beacon",
    }
    assert normalize_profile({"reference_power": None, "attenuation": None}, kind=kind) == {}
    assert normalize_profile({}, kind=kind) == {}


def test_default_profile_kind_keeps_receiver_offset_and_beacon_verification_is_optional():
    assert normalize_profile({"rssi_offset": 5.0}) == {"rssi_offset": 5.0}
    assert normalize_profile({"rssi_offset": None}) == {}
    assert normalize_profile({"reference_power_verified": False}, kind="beacon") == {"reference_power_verified": False}


@pytest.fixture
async def evidence_runtime(runtime):
    manager = runtime.manager
    _add_receiver(runtime.coordinator, runtime.device, SECOND_SCANNER, SOURCE_STAMP)
    await manager.async_dispatch(
        "settings",
        {"settings": {"receiver_profiles": {SCANNER: {"reference_power": -61.0}}}},
    )
    area = runtime.coordinator.ar.async_create("Synthetic Kind Boundary Site")
    captured = await manager.async_dispatch(
        "capture",
        {"device_id": DEVICE, "location_id": area.id, "ranges": {SCANNER: 2.5, SECOND_SCANNER: 3.0}},
    )
    assert captured["captured"] == 2
    trained = await manager.async_dispatch("fingerprint_train", {"device_id": DEVICE, "location_id": area.id})
    assert trained["accepted"] is True and trained["added_samples"] == 1
    await manager.async_dispatch("calibration_preview", {})
    await manager.async_dispatch("shadow_start", {"candidate": {SCANNER: 2.0}})
    assert len(manager._labelled_rows()) == 2
    assert manager.proposal is not None and manager.shadow.state == "active"
    return runtime


def evidence(state):
    manager = state.manager
    return {
        "version": manager.version(),
        "model": manager.effective_advert_model(state.advert),
        "revision": state.device.model_revision,
        "source": _source_measurements(state),
        "history": manager.history.export(redact=False),
        "clocks": manager.history.export_clocks(),
        "labelled": manager._labelled_rows(),
        "proposal": deepcopy(manager.proposal),
        "proposal_rows": deepcopy(manager._proposal_rows),
        "fingerprints": manager.fingerprints.export_state(),
        "shadow": deepcopy(manager.shadow.summary()),
        "shadow_baseline": deepcopy(manager.shadow._baseline),
        "shadow_new": deepcopy(manager.shadow._new),
        "shadow_history": manager._shadow_history.export(redact=False),
        "shadow_clocks": manager._shadow_history.export_clocks(),
    }


@pytest.mark.parametrize("action", ["profile", "settings"])
@pytest.mark.parametrize(
    ("kind", "unsupported"),
    [
        ("beacon", {"rssi_offset": 5.0}),
        ("receiver", {"reference_power_verified": False}),
        ("receiver", {"reference_power_verified": True}),
    ],
)
async def test_public_inapplicable_profile_is_atomic_and_preserves_all_existing_evidence(
    evidence_runtime, action, kind, unsupported
):
    state, manager = evidence_runtime, evidence_runtime.manager
    before = evidence(state)
    options = deepcopy(dict(state.entry.options))
    coordinator_options = deepcopy(state.coordinator.options)
    settings = deepcopy(manager.settings)
    generation = manager._generation
    saves = [(deepcopy(call.args), deepcopy(call.kwargs)) for call in manager.store.async_save.await_args_list]
    shadow = manager.shadow
    identifier = SCANNER if kind == "receiver" else DEVICE
    payload = (
        {"kind": kind, "id": identifier, "profile": unsupported}
        if action == "profile"
        else {"settings": {f"{kind}_profiles": {identifier: {"reference_power": -61.0, **unsupported}}}}
    )
    with pytest.raises(ValueError, match="profile fields"):
        await manager.async_dispatch(action, payload)
    assert dict(state.entry.options) == options
    assert state.coordinator.options == coordinator_options
    assert manager.settings == settings
    assert manager._generation == generation
    assert [(call.args, call.kwargs) for call in manager.store.async_save.await_args_list] == saves
    assert manager.shadow is shadow
    assert evidence(state) == before


@pytest.mark.parametrize("kind", ["receiver", "beacon"])
async def test_accepted_hardware_metadata_keeps_real_calibration_fingerprint_and_shadow_evidence(
    evidence_runtime, kind
):
    state, manager = evidence_runtime, evidence_runtime.manager
    before = evidence(state)
    identifier = SCANNER if kind == "receiver" else DEVICE
    saved = manager.store.async_save.await_count
    result = await manager.async_dispatch("profile", {"kind": kind, "id": identifier, "profile": METADATA})
    assert result["profile"]["height_m"] == 1.5
    assert result["profile"]["antenna_gain_dbi"] == 2.0
    assert result["profile"]["distance_accuracy_m"] == 0.1
    assert manager.store.async_save.await_count > saved
    assert evidence(state) == before


async def test_actual_history_action_and_capture_replies_use_enabled_summary_contract(runtime):
    manager = runtime.manager
    started = await manager.async_dispatch("history_start", {"devices": [DEVICE]})
    assert started == manager.history.summary()
    assert started["enabled"] is True and "running" not in started
    assert isinstance(started["frames"], int) and isinstance(started["rows"], int)
    capture = await manager.async_dispatch(
        "capture", {"device_id": DEVICE, "location_id": "Synthetic Contract Site", "ranges": {SCANNER: 2.5}}
    )
    assert capture["history"] == manager.history.summary()
    assert capture["history"]["enabled"] is True and capture["history"]["rows"] == 1
    exported = await manager.async_dispatch("history_export", {"redact": False})
    stopped = await manager.async_dispatch("history_stop", {})
    assert stopped == manager.history.summary() and stopped["enabled"] is False
    cleared = await manager.async_dispatch("history_clear", {})
    assert cleared == manager.history.summary() and cleared["rows"] == 0
    imported = await manager.async_dispatch("history_import", {"data": exported})
    assert imported == manager.history.summary() and imported["enabled"] is False and imported["rows"] == 1
    settings = await manager.async_dispatch(
        "settings", {"settings": {"history_enabled": True, "history_window_s": 120.0}}
    )
    assert settings["options"]["history_enabled"] is manager.history.enabled is True
    assert settings["options"]["history_window_s"] == manager.history.window_s == 120.0
