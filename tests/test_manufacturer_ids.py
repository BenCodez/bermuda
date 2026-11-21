"""Tests for loading Bluetooth SIG manufacturer name files."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from custom_components.bermuda import coordinator as coordinator_module
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator

COMPONENT_DIR = Path(coordinator_module.__file__).parent
DATA_DIR = COMPONENT_DIR / "manufacturer_identification"


@pytest.fixture(name="skip_yaml_data_load", autouse=True)
def skip_yaml_data_load():
    """Run the real loader instead of the global opt-out fixture."""
    yield


def _coordinator(hass):
    hass.config.config_dir = str(COMPONENT_DIR.parents[1])
    return SimpleNamespace(hass=hass, member_uuids={}, company_uuids={}, _waitingfor_load_manufacturer_ids=True)


async def test_load_manufacturer_ids(hass):
    coordinator = _coordinator(hass)

    await BermudaDataUpdateCoordinator.async_load_manufacturer_ids(coordinator)

    with (DATA_DIR / "member_uuids.yaml").open(encoding="utf-8") as file:
        expected_members = {member["uuid"]: member["name"] for member in yaml.safe_load(file)["uuids"]}
    with (DATA_DIR / "company_identifiers.yaml").open(encoding="utf-8") as file:
        expected_companies = {
            company["value"]: company["name"] for company in yaml.safe_load(file)["company_identifiers"]
        }

    assert coordinator.member_uuids == expected_members
    assert coordinator.company_uuids == expected_companies
    assert coordinator._waitingfor_load_manufacturer_ids is False


async def test_load_manufacturer_ids_parses_off_event_loop(hass, monkeypatch):
    threads = []
    original = yaml.load

    def recording_load(*args, **kwargs):
        threads.append(threading.current_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(yaml, "load", recording_load)
    await BermudaDataUpdateCoordinator.async_load_manufacturer_ids(_coordinator(hass))

    assert len(threads) == 2
    assert all(thread.ident != hass.loop_thread_id for thread in threads)


async def test_load_manufacturer_ids_clears_waiting_flag_on_error(hass, monkeypatch):
    def broken_load(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(yaml, "load", broken_load)
    coordinator = _coordinator(hass)

    with pytest.raises(OSError, match="unreadable"):
        await BermudaDataUpdateCoordinator.async_load_manufacturer_ids(coordinator)

    assert coordinator._waitingfor_load_manufacturer_ids is False
