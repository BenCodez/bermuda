"""The optional fingerprint sensor reports advice without changing tracking."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.bermuda.api import public_device_id
from custom_components.bermuda.const import SIGNAL_DEVICE_NEW
from custom_components.bermuda.sensor import BermudaSensorAreaSuggestion, async_setup_entry

DEVICE = "aa:bb:cc:dd:ee:01"


def _make(hass, suggestion=None, *, key=DEVICE):
    device = SimpleNamespace(address=key, unique_id=key, name="Synthetic Beacon", area_id="existing_live_area")
    coordinator = SimpleNamespace(
        hass=hass,
        devices={key: device},
        have_floors=False,
        scanner_list=set(),
        get_scanners=[],
        sensor_created=lambda address: None,
    )
    if suggestion is not None:
        coordinator.toolkit = SimpleNamespace(area_suggestion=lambda device: suggestion)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator), options={}, async_on_unload=lambda cb: None
    )
    return coordinator, entry, device


async def test_dispatcher_creates_one_disabled_advisory_for_a_tracked_device(hass):
    coordinator, entry, _ = _make(hass)
    added = []
    await async_setup_entry(hass, entry, lambda entities, update_before_add=False: added.extend(entities))
    async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, DEVICE)
    await hass.async_block_till_done()
    async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, DEVICE)
    await hass.async_block_till_done()
    advisory = [entity for entity in added if isinstance(entity, BermudaSensorAreaSuggestion)]
    assert len(advisory) == 1
    assert advisory[0].unique_id == f"{DEVICE}_area_suggestion"
    assert advisory[0].name == "Suggested Area"
    assert advisory[0].entity_category == EntityCategory.DIAGNOSTIC
    assert advisory[0].entity_registry_enabled_default is False
    assert advisory[0].native_value is None
    assert advisory[0].extra_state_attributes["rejection_reasons"] == ["fingerprint_model_unavailable"]


def test_valid_supervised_advice_exposes_area_confidence_coverage_without_mutation(hass):
    area = ar.async_get(hass).async_create("Synthetic Training Room")
    suggestion = {
        "accepted": True,
        "suggested_area": area.id,
        "confidence": 0.96,
        "coverage": 0.75,
        "reasons": [],
    }
    coordinator, entry, device = _make(hass, suggestion)
    advisory = BermudaSensorAreaSuggestion(coordinator, entry, DEVICE)
    assert advisory.native_value == "Synthetic Training Room"
    attributes = advisory.extra_state_attributes
    assert attributes["suggested_area_id"] == area.id
    assert attributes["confidence"] == 0.96
    assert attributes["coverage"] == 0.75
    assert attributes["device_id"] == DEVICE
    assert device.area_id == "existing_live_area"
    assert suggestion["reasons"] == []


def test_rejected_suggestion_remains_unknown_and_reports_reason_codes(hass):
    area = ar.async_get(hass).async_create("Synthetic Ambiguous Room")
    suggestion = {
        "accepted": False,
        "suggested_area": area.id,
        "confidence": 0.2,
        "coverage": 0.5,
        "reasons": [{"code": "ambiguous_location", "margin": 0.01}],
    }
    coordinator, entry, _ = _make(hass, suggestion)
    advisory = BermudaSensorAreaSuggestion(coordinator, entry, DEVICE)
    assert advisory.native_value is None
    assert advisory.extra_state_attributes["suggested_area_id"] is None
    assert advisory.extra_state_attributes["rejection_reasons"] == ["ambiguous_location"]


def test_missing_or_deleted_home_assistant_area_cannot_be_reported_as_valid(hass):
    area = ar.async_get(hass).async_create("Synthetic Deleted Room")
    suggestion = {"accepted": True, "suggested_area": area.id, "confidence": 0.95, "coverage": 1.0}
    coordinator, entry, _ = _make(hass, suggestion)
    advisory = BermudaSensorAreaSuggestion(coordinator, entry, DEVICE)
    assert advisory.native_value == area.name
    ar.async_get(hass).async_delete(area.id)
    assert advisory.native_value is None
    assert advisory.extra_state_attributes["rejection_reasons"] == ["invalid_home_assistant_area"]


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), 10**1000, -0.1, "0.9"])
def test_advisory_does_not_export_invalid_confidence_or_coverage(hass, invalid):
    coordinator, entry, _ = _make(hass, {"accepted": False, "confidence": invalid, "coverage": invalid})
    advisory = BermudaSensorAreaSuggestion(coordinator, entry, DEVICE)
    assert advisory.extra_state_attributes["confidence"] is None
    assert advisory.extra_state_attributes["coverage"] is None


def test_private_identity_is_public_in_advisory_attributes(hass):
    private_key = "1234567890abcdef1234567890abcdef"
    coordinator, entry, _ = _make(hass, {"accepted": False, "coverage": 0.0}, key=private_key)
    advisory = BermudaSensorAreaSuggestion(coordinator, entry, private_key)
    assert advisory.extra_state_attributes["device_id"] == public_device_id(private_key)
