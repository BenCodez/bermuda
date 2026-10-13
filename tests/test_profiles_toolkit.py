"""Strict profile validation and legacy range model compatibility."""

from copy import deepcopy

import pytest

from custom_components.bermuda.profiles import (
    effective_model,
    finite_number,
    model_version,
    normalize_fixed_reference,
    normalize_profile,
    normalize_toolkit_settings,
    receiver_value,
)
from custom_components.bermuda.const import CONF_ATTENUATION, CONF_REF_POWER, CONF_RSSI_OFFSETS

from .test_toolkit_runtime import SCANNER, runtime


def test_defaults_preserve_legacy_behavior_and_off_optional_features():
    settings = normalize_toolkit_settings(None)
    assert settings["auto_calibration"] is False
    assert settings["history_enabled"] is False
    assert settings["reliability_weighting"] is False
    assert settings["fingerprint_area_selection"] is False
    assert effective_model(settings, "scanner", "beacon", reference_power=-61.0, attenuation=3.0, rssi_offset=2.0) == {
        "reference_power": -61.0,
        "attenuation": 3.0,
        "rssi_offset": 2.0,
        "reference_power_verified": False,
    }


@pytest.mark.parametrize(
    ("power", "exponent", "offset"), [(1.0, 12.0, 40.0), (-150.0, 0.05, -127.0), (3.0, 20.0, 127.0)]
)
def test_legacy_model_values_preserve_existing_broader_numeric_semantics(power, exponent, offset):
    model = effective_model(
        normalize_toolkit_settings(None),
        "scanner",
        "beacon",
        reference_power=power,
        attenuation=exponent,
        rssi_offset=offset,
    )
    assert model["reference_power"] == power
    assert model["attenuation"] == exponent
    assert model["rssi_offset"] == offset


def test_legacy_device_power_preserves_positive_and_low_finite_values():
    settings = normalize_toolkit_settings({"receiver_profiles": {"scanner": {"reference_power": -58}}})
    for power in (1.0, -150.0):
        model = effective_model(
            settings,
            "scanner",
            "beacon",
            reference_power=-55,
            attenuation=12,
            rssi_offset=40,
            device_reference_power=power,
        )
        assert model["reference_power"] == power


def test_legacy_invalid_model_values_follow_existing_safe_defaults():
    model = effective_model(
        normalize_toolkit_settings(None),
        "scanner",
        "beacon",
        reference_power=float("nan"),
        attenuation=0.0,
        rssi_offset=True,
    )
    assert model == {
        "reference_power": -55.0,
        "attenuation": 3.0,
        "rssi_offset": 0.0,
        "reference_power_verified": False,
    }


@pytest.mark.parametrize("field,value", [("reference_power", 1.0), ("attenuation", 12.0), ("rssi_offset", 40.0)])
def test_explicit_profiles_still_require_stricter_numeric_bounds(field, value):
    with pytest.raises(ValueError):
        normalize_profile({field: value})
    with pytest.raises(ValueError):
        effective_model(
            {"receiver_profiles": {"scanner": {field: value}}},
            "scanner",
            "beacon",
            reference_power=1,
            attenuation=12,
            rssi_offset=40,
        )


def test_metadata_does_not_invent_hardware_corrections():
    profile = {
        "hardware_type": "synthetic board",
        "antenna": "external",
        "antenna_gain_db": 4.0,
        "environment": "indoor",
        "distance_accuracy_m": 0.5,
    }
    settings = normalize_toolkit_settings({"receiver_profiles": {"scanner": profile}})
    assert settings["receiver_profiles"]["scanner"]["antenna_gain_dbi"] == 4.0
    model = effective_model(settings, "scanner", "beacon", reference_power=-55.0, attenuation=3.0, rssi_offset=1.0)
    assert model["rssi_offset"] == 1.0
    assert model["reference_power"] == -55.0


def test_null_profile_model_parameters_follow_existing_device_receiver_values():
    profile = normalize_profile({"rssi_offset": None, "reference_power": None, "attenuation": None})
    assert profile == {}
    model = effective_model(
        {"receiver_profiles": {"scanner": profile}},
        "scanner",
        "device",
        reference_power=-59,
        attenuation=2,
        rssi_offset=5,
    )
    assert model["rssi_offset"] == 5
    assert model["reference_power"] == -59


def test_profile_numeric_precedence_preserves_per_device_reference_power():
    settings = normalize_toolkit_settings(
        {
            "receiver_profiles": {"scanner": {"reference_power": -58, "attenuation": 2, "rssi_offset": 3}},
            "beacon_profiles": {"beacon": {"reference_power": -63, "reference_power_verified": True}},
        }
    )
    model = effective_model(settings, "scanner", "beacon", reference_power=-60, attenuation=3, rssi_offset=1)
    assert model == {"reference_power": -63, "attenuation": 2, "rssi_offset": 3, "reference_power_verified": True}


def test_legacy_device_power_precedes_receiver_profile_unless_explicit_beacon_override():
    settings = normalize_toolkit_settings({"receiver_profiles": {"scanner": {"reference_power": -58}}})
    kwargs = {"reference_power": -55, "attenuation": 3, "rssi_offset": 0}
    assert (
        effective_model(settings, "scanner", "beacon", device_reference_power=-64, **kwargs)["reference_power"] == -64
    )
    assert effective_model(settings, "scanner", "beacon", device_reference_power=0, **kwargs)["reference_power"] == -58
    settings["beacon_profiles"] = {"beacon": {"reference_power": -62}}
    assert (
        effective_model(settings, "scanner", "beacon", device_reference_power=-64, **kwargs)["reference_power"] == -62
    )


def test_fixed_references_require_explicit_labels_ranges_and_measured_power_assertion():
    source = {
        "fixed_references": {
            "beacon": {
                "location_id": "measured shelf",
                "ranges": {"scanner": 2.5},
                "accuracy_m": 0.1,
                "reference_power": -61,
                "reference_power_verified": True,
            }
        }
    }
    old = deepcopy(source)
    result = normalize_toolkit_settings(source)
    assert source == old
    assert result["fixed_references"]["beacon"]["device_id"] == "beacon"
    result["fixed_references"]["beacon"]["ranges"]["scanner"] = 5
    assert source == old
    with pytest.raises(ValueError):
        normalize_fixed_reference({"location_id": "shelf", "ranges": {"scanner": 1}, "reference_power_verified": True})


@pytest.mark.parametrize(
    "source",
    [
        {"receiver_profiles": {"scanner": {"rssi_offset": True}}},
        {"beacon_profiles": {"beacon": {"reference_power": float("nan")}}},
        {"beacon_profiles": {"beacon": {"attenuation": float("inf")}}},
        {"receiver_profiles": {"scanner": {"environment": "unmeasured room name"}}},
        {"auto_calibration": 1},
        {"receiver_limits": {"scanner": False}},
        {"history_window_s": 0},
        {"filters": {"beacon": "unknown"}},
        {"unrecognized_option": True},
        {"fixed_references": {"beacon": {"location_id": "shelf", "ranges": {"scanner": 0}}}},
        {"scanner_bindings": {"scanner": {"device_registry_id": "reg", "preserved_uid": "forged"}}},
    ],
)
def test_namespace_rejects_nonfinite_booleans_unknown_fields_and_invalid_limits(source):
    with pytest.raises(ValueError):
        normalize_toolkit_settings(source)


def test_namespace_limits_are_enforced_and_opaque_public_device_ids_preserved():
    source = {
        "filters": {"public/device:opaque-id": "pet"},
        "receiver_limits": {"scanner": 0},
        "scanner_bindings": {"scanner": {"device_registry_id": "registry-id"}},
    }
    result = normalize_toolkit_settings(source)
    assert result["filters"] == source["filters"]
    assert result["receiver_limits"]["scanner"] == 0
    assert result["scanner_bindings"] == source["scanner_bindings"]
    with pytest.raises(ValueError):
        normalize_toolkit_settings({"beacon_profiles": {str(i): {} for i in range(129)}})


def test_legacy_uppercase_mac_lookup_preserves_opaque_scanner_ids():
    offsets = {"AA:BB:CC:DD:EE:FF": 3.0, "CaseSensitive/Source": 4.0}
    assert receiver_value(offsets, "aa:bb:cc:dd:ee:ff", 0) == 3.0
    assert receiver_value(offsets, "casesensitive/source", 0) == 0
    assert offsets == {"AA:BB:CC:DD:EE:FF": 3.0, "CaseSensitive/Source": 4.0}
    settings = normalize_toolkit_settings({"receiver_profiles": {"AA:BB:CC:DD:EE:FF": {"attenuation": 2}}})
    assert (
        effective_model(settings, "aa:bb:cc:dd:ee:ff", "beacon", reference_power=-60, attenuation=3, rssi_offset=3)[
            "attenuation"
        ]
        == 2
    )


def test_model_signature_changes_only_for_range_affecting_configuration():
    settings = normalize_toolkit_settings({})
    signature = model_version(settings, {"scanner": 2}, -55, 3)
    settings["receiver_profiles"] = {"scanner": {"hardware_type": "synthetic"}}
    assert model_version(settings, {"scanner": 2}, -55, 3) == signature
    settings["receiver_profiles"]["scanner"]["attenuation"] = 2
    assert model_version(settings, {"scanner": 2}, -55, 3) != signature
    assert model_version(normalize_toolkit_settings({}), {"scanner": 3}, -55, 3) != signature
    assert model_version(settings, device_reference_powers={"beacon": -61}) != model_version(
        settings, device_reference_powers={"beacon": -62}
    )


def test_huge_integers_and_booleans_are_not_finite_radio_numbers():
    assert not finite_number(10**10000)
    assert not finite_number(True)
    assert not finite_number(float("nan"))


def test_profile_verification_requires_an_explicit_reference_power():
    with pytest.raises(ValueError):
        normalize_profile({"reference_power_verified": True}, kind="beacon")
    with pytest.raises(ValueError):
        normalize_profile({"antenna_gain_db": 1, "antenna_gain_dbi": 1})


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), True, None])
def test_model_signature_normalizes_corrupt_legacy_values_to_effective_fallbacks(invalid):
    settings = normalize_toolkit_settings(None)
    actual = model_version(
        settings, {"scanner": invalid}, invalid, invalid, device_reference_powers={"beacon": invalid}
    )
    fallback = model_version(settings, {"scanner": 0.0}, -55.0, 3.0, device_reference_powers={"beacon": 0.0})
    assert actual == fallback
    assert actual == model_version(settings, {"scanner": 0.0}, -55.0, 3.0)
    model = effective_model(
        settings,
        "scanner",
        "beacon",
        reference_power=invalid,
        attenuation=invalid,
        rssi_offset=invalid,
        device_reference_power=invalid,
    )
    assert model == {
        "reference_power": -55.0,
        "attenuation": 3.0,
        "rssi_offset": 0.0,
        "reference_power_verified": False,
    }


def test_invalid_legacy_device_power_is_unset_and_follows_receiver_profile():
    settings = normalize_toolkit_settings({"receiver_profiles": {"scanner": {"reference_power": -62}}})
    model = effective_model(
        settings,
        "scanner",
        "beacon",
        reference_power=-59,
        attenuation=3,
        rssi_offset=0,
        device_reference_power=float("nan"),
    )
    assert model["reference_power"] == -62
    assert model_version(settings, device_reference_powers={"beacon": float("nan")}) == model_version(settings)


@pytest.mark.parametrize(
    "field,value", [("reference_power", float("nan")), ("attenuation", float("inf")), ("rssi_offset", 40.0)]
)
def test_signature_does_not_sanitize_invalid_explicit_profile_values(field, value):
    with pytest.raises(ValueError):
        model_version({"receiver_profiles": {"scanner": {field: value}}})


async def test_actual_manager_diagnostics_work_with_corrupt_legacy_radio_options(runtime):
    manager = runtime.manager
    runtime.coordinator.options[CONF_REF_POWER] = float("nan")
    runtime.coordinator.options[CONF_ATTENUATION] = float("inf")
    runtime.coordinator.options[CONF_RSSI_OFFSETS][SCANNER] = float("nan")
    runtime.device.ref_power = float("nan")
    runtime.advert.ref_power = float("nan")
    manager.invalidate_model()
    version = manager.version()
    state = await manager.async_dispatch("state", {})
    assert state["diagnostics"]["model_version"] == version
    assert manager.effective_advert_model(runtime.advert) == {
        "reference_power": -55.0,
        "attenuation": 3.0,
        "rssi_offset": 0.0,
        "reference_power_verified": False,
    }
