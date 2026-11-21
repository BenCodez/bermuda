# ruff: noqa: ANN204, D101, E402, INP001, SLF001, T201
"""
Reproducible synthetic Bermuda coordinator/event-loop benchmark.

This benchmark runs Bermuda's coordinator, devices, adverts, toolkit, and
profile resolution in a Home Assistant test runtime. Synthetic remote
scanners report a deterministic set of BLE advertisements. The benchmark
stubs hardware subscriptions and persistent storage.

Run from a Bermuda checkout with its test dependencies installed, for example:

    .venv/bin/python benchmarks/bermuda_event_loop_benchmark.py --scanners 12 --json result.json

Use the same arguments, virtual environment and machine for each revision.
To run against a second checkout, set PYTHONPATH to its root and pass this
script's absolute path. The JSON result includes the loaded source path,
Git revision and checkout status, fixture parameters, and runtime versions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from statistics import median
from time import perf_counter
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# A direct ``python benchmarks/...`` invocation otherwise puts only the
# benchmarks directory on sys.path.  Keep any caller-supplied PYTHONPATH ahead
# of this root so the same script can drive the detached baseline worktree.
REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if REPOSITORY_ROOT not in sys.path:
    sys.path.append(REPOSITORY_ROOT)

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BaseHaRemoteScanner
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.helpers import frame
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_test_home_assistant

import custom_components.bermuda.api as api_module
import custom_components.bermuda.bermuda_advert as advert_module
import custom_components.bermuda.bermuda_device as device_module
import custom_components.bermuda.coordinator as coordinator_module
import custom_components.bermuda.profiles as profiles_module
import custom_components.bermuda.toolkit as toolkit_module
from custom_components.bermuda import BermudaData
from custom_components.bermuda.const import (
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.bermuda.coordinator import BermudaDataUpdateCoordinator
from custom_components.bermuda.toolkit import OPTIONS_KEY, ToolkitManager


def _address(prefix: int, number: int) -> str:
    """Return deterministic, non-resolvable MAC addresses accepted by Bermuda."""
    return f"{prefix:02x}:00:00:{(number >> 16) & 255:02x}:{(number >> 8) & 255:02x}:{number & 255:02x}"


class SyntheticRemoteScanner(BaseHaRemoteScanner):
    """Minimal remote-scanner implementation for Bermuda's normal gather path."""

    def __init__(
        self, source: str, name: str, packets: dict[str, tuple[BLEDevice, AdvertisementData]], stamps: dict[str, float]
    ):
        self.source = source
        self.name = name
        self._packets = packets
        self._stamps = stamps

    def time_since_last_detection(self) -> float:
        return 0.0

    @property
    def discovered_device_timestamps(self) -> dict[str, float]:
        return self._stamps

    @property
    def discovered_devices_and_advertisement_data(self) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        return self._packets


@dataclass
class Calls:
    effective_advert_model: int = 0
    effective_model: int = 0
    receiver_value: int = 0


@dataclass
class RunResult:
    cycle_total_ms: float
    cycle_mean_ms: float
    cycle_p50_ms: float
    cycle_p95_ms: float
    cycle_p99_ms: float
    cycle_max_ms: float
    heartbeat_p50_lag_ms: float
    heartbeat_p95_lag_ms: float
    heartbeat_p99_lag_ms: float
    heartbeat_max_lag_ms: float
    effective_advert_model_calls: int
    effective_model_calls: int
    receiver_value_calls: int


def _percentile(values: list[float], percentile: float) -> float:
    """Linear-interpolated percentile with a harmless zero-sample fallback."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _summary(values: list[float], prefix: str) -> dict[str, float]:
    return {
        f"{prefix}_mean_ms": sum(values) * 1000 / len(values),
        f"{prefix}_p50_ms": _percentile(values, 50) * 1000,
        f"{prefix}_p95_ms": _percentile(values, 95) * 1000,
        f"{prefix}_p99_ms": _percentile(values, 99) * 1000,
        f"{prefix}_max_ms": max(values, default=0.0) * 1000,
    }


async def _heartbeat(stop: asyncio.Event, samples: list[float], period_s: float) -> None:
    """Independently sample scheduling delay while the synchronous cycles run."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + period_s
    while not stop.is_set():
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        now = loop.time()
        samples.append(max(0.0, now - deadline))
        deadline += period_s
        if deadline < now:
            deadline = now + period_s


@asynccontextmanager
async def _runtime(devices: int, scanners: int, fixed_references: int):
    """Build a fresh real Bermuda runtime around deterministic scanner packets."""
    async with async_test_home_assistant(load_registries=True, initial_state=ConfigEntryState.LOADED) as hass:
        # DataUpdateCoordinator records integration-frame usage in current HA.
        # pytest's hass fixture normally performs this setup for us.
        frame.async_setup(hass)
        # A-F hex digits exercise the case-insensitive MAC profile path.
        device_addresses = [_address(0xA0, value + 1) for value in range(devices)]
        scanner_addresses = [_address(0xB0, value + 1) for value in range(scanners)]
        options = {
            CONF_DEVICES: [address.upper() for address in device_addresses],
            CONF_REF_POWER: -59.0,
            CONF_ATTENUATION: 2.4,
            CONF_RSSI_OFFSETS: {
                # Mixed-case keys deliberately exercise receiver_value's MAC alias path.
                (address.upper() if index % 2 else address): (index % 5) - 2.0
                for index, address in enumerate(scanner_addresses)
            },
            CONF_DEVTRACK_TIMEOUT: 30,
            CONF_MAX_RADIUS: 20.0,
            CONF_MAX_VELOCITY: 3.0,
            CONF_SMOOTHING_SAMPLES: 20,
            # Preserve the user's 3-second publication setting, although this
            # benchmark invokes cycles explicitly rather than waiting for it.
            CONF_UPDATE_INTERVAL: 3.0,
            OPTIONS_KEY: {
                "receiver_profiles": {
                    (address.upper() if index % 2 else address): {
                        "reference_power": -58.0 - (index % 3),
                        "attenuation": 2.0 + (index % 3) / 10,
                        "rssi_offset": (index % 5) - 2.0,
                    }
                    for index, address in enumerate(scanner_addresses)
                },
                "beacon_profiles": {
                    address: {
                        "reference_power": -60.0 - (index % 4),
                        "attenuation": 2.1 + (index % 3) / 10,
                        "reference_power_verified": True,
                    }
                    for index, address in enumerate(device_addresses)
                    if index % 3 == 1
                },
                "fixed_references": {
                    address: {
                        "device_id": address,
                        "location_id": f"benchmark-location-{index}",
                        "ranges": dict.fromkeys(scanner_addresses, 1.5 + index / 10),
                        "accuracy_m": 0.25,
                        "reference_power": -57.0 - (index % 3),
                        "reference_power_verified": True,
                    }
                    for index, address in enumerate(device_addresses[: min(fixed_references, devices)])
                },
            },
        }
        entry = MockConfigEntry(domain=DOMAIN, entry_id="benchmark", data={"source": "benchmark"}, options=options)
        entry.add_to_hass(hass)

        # Source timestamps and Bermuda's monotonic clock share the same
        # controlled domain.  The asyncio heartbeat deliberately keeps using
        # the real event-loop clock, so its lag remains an independent metric.
        clock = SimpleNamespace(value=1000.0)

        def monotonic() -> float:
            return clock.value

        # The cycle benchmark intentionally excludes one-off YAML I/O and HA's
        # real bluetooth subscription; the coordinator's gather/process/area path
        # itself remains unmodified.
        with (
            patch.object(BermudaDataUpdateCoordinator, "async_load_manufacturer_ids", new=AsyncMock()),
            patch("custom_components.bermuda.coordinator.bluetooth.async_register_callback", return_value=lambda: None),
            patch.object(coordinator_module, "_get_manager", return_value=SimpleNamespace()),
            patch.object(coordinator_module, "monotonic_time_coarse", monotonic),
            patch.object(advert_module, "monotonic_time_coarse", monotonic),
            patch.object(device_module, "monotonic_time_coarse", monotonic),
            patch.object(toolkit_module, "monotonic_time_coarse", monotonic),
            patch.object(api_module, "monotonic_time_coarse", monotonic),
        ):
            token = current_entry.set(entry)
            try:
                coordinator = BermudaDataUpdateCoordinator(hass, entry)
            finally:
                current_entry.reset(token)
            coordinator._waitingfor_load_manufacturer_ids = False
            coordinator._scanner_init_pending = False
            coordinator._do_private_device_init = False
            coordinator._manager = SimpleNamespace(async_current_scanners=lambda: coordinator._hascanners)
            manager = coordinator.toolkit = ToolkitManager(coordinator)
            manager.store = MagicMock()
            manager.store.async_load = AsyncMock(return_value=None)
            manager.store.async_save = AsyncMock()
            entry.runtime_data = BermudaData(coordinator)
            entry._async_set_state(hass, ConfigEntryState.LOADED, None)

            synthetic_scanners = set()
            for scanner_index, scanner_address in enumerate(scanner_addresses):
                packets: dict[str, tuple[BLEDevice, AdvertisementData]] = {}
                stamps: dict[str, float] = {}
                for device_index, device_address in enumerate(device_addresses):
                    packets[device_address] = (
                        BLEDevice(device_address, f"Benchmark beacon {device_index}", {}),
                        AdvertisementData(
                            local_name="Benchmark beacon",
                            manufacturer_data={},
                            service_data={},
                            service_uuids=[],
                            tx_power=-59,
                            rssi=-45 - ((device_index * 7 + scanner_index * 11) % 35),
                            platform_data=(),
                        ),
                    )
                    # Bermuda's remote-scanner accessor indexes these exact
                    # timestamps with address.upper().
                    stamps[device_address.upper()] = clock.value
                synthetic_scanners.add(
                    SyntheticRemoteScanner(scanner_address, f"Benchmark receiver {scanner_index}", packets, stamps)
                )
            coordinator._hascanners = synthetic_scanners
            coordinator._scanner_list = set(scanner_addresses)

            # Seed real devices/adverts once, then advance source and Bermuda
            # time together so all measured cycles ingest fresh packet rows.
            coordinator._async_update_data_internal()
            yield coordinator, manager, synthetic_scanners, clock
            await manager.async_close()


async def _run_once(
    devices: int,
    scanners: int,
    fixed_references: int,
    warmup: int,
    cycles: int,
    heartbeat_ms: float,
    inter_cycle_yield_ms: float,
) -> RunResult:
    calls = Calls()
    original_model = ToolkitManager.effective_advert_model
    original_effective = toolkit_module.effective_model
    original_receiver = profiles_module.receiver_value

    def count_model(self: ToolkitManager, advert: Any, device_id: str | None = None) -> dict:
        calls.effective_advert_model += 1
        return original_model(self, advert, device_id)

    def count_effective(*args: Any, **kwargs: Any) -> dict:
        calls.effective_model += 1
        return original_effective(*args, **kwargs)

    def count_receiver(values: Any, scanner_id: str, default: Any = None) -> Any:
        calls.receiver_value += 1
        return original_receiver(values, scanner_id, default)

    # These modules imported receiver_value directly; patch each call site so
    # the report includes the coordinator cycle as well as profile resolution.
    with (
        patch.object(ToolkitManager, "effective_advert_model", count_model),
        patch.object(toolkit_module, "effective_model", count_effective),
        patch.object(profiles_module, "receiver_value", count_receiver),
        patch.object(toolkit_module, "receiver_value", count_receiver),
        patch.object(device_module, "receiver_value", count_receiver),
    ):
        async with _runtime(devices, scanners, fixed_references) as (coordinator, manager, synthetic_scanners, clock):
            scanners_by_address = {scanner.source: scanner for scanner in synthetic_scanners}

            def advance_sources(cycle: int) -> None:
                # 1.05 seconds keeps all live stamps well within Bermuda's
                # normal freshness thresholds while preserving a 3-second
                # configured publication option.
                clock.value += 1.05
                for scanner in scanners_by_address.values():
                    for address in scanner.discovered_device_timestamps:
                        scanner.discovered_device_timestamps[address] = clock.value

            for cycle in range(warmup):
                advance_sources(cycle)
                coordinator._async_update_data_internal()
                await asyncio.sleep(inter_cycle_yield_ms / 1000)

            rows = api_module.snapshot_observations(manager._snapshot())
            expected_rows = devices * scanners
            if len(rows) != expected_rows or any(row["status"] != "current" for row in rows):
                msg = f"fixture did not retain {expected_rows} current advertisements ({len(rows)} found)"
                raise RuntimeError(msg)

            calls.effective_advert_model = 0
            calls.effective_model = 0
            calls.receiver_value = 0
            heartbeat_samples: list[float] = []
            stop = asyncio.Event()
            task = asyncio.create_task(_heartbeat(stop, heartbeat_samples, heartbeat_ms / 1000))
            await asyncio.sleep(0)  # ensure the independent monitor has scheduled its first deadline
            cycle_samples: list[float] = []
            for cycle in range(cycles):
                advance_sources(warmup + cycle)
                started = perf_counter()
                coordinator._async_update_data_internal()
                cycle_samples.append(perf_counter() - started)
                await asyncio.sleep(inter_cycle_yield_ms / 1000)
            await asyncio.sleep(heartbeat_ms / 1000)
            stop.set()
            await task

    cycle_summary = _summary(cycle_samples, "cycle")
    heartbeat_summary = _summary(heartbeat_samples, "heartbeat")
    return RunResult(
        **cycle_summary,
        cycle_total_ms=sum(cycle_samples) * 1000,
        heartbeat_p50_lag_ms=heartbeat_summary["heartbeat_p50_ms"],
        heartbeat_p95_lag_ms=heartbeat_summary["heartbeat_p95_ms"],
        heartbeat_p99_lag_ms=heartbeat_summary["heartbeat_p99_ms"],
        heartbeat_max_lag_ms=heartbeat_summary["heartbeat_max_ms"],
        effective_advert_model_calls=calls.effective_advert_model,
        effective_model_calls=calls.effective_model,
        receiver_value_calls=calls.receiver_value,
    )


def _median_result(results: list[RunResult]) -> dict[str, float]:
    fields = RunResult.__dataclass_fields__
    return {field: median(getattr(result, field) for result in results) for field in fields}


def _source_metadata() -> dict[str, str | bool | None]:
    """Identify the checkout that supplied the imported Bermuda code."""
    source = Path(coordinator_module.__file__).resolve()
    result: dict[str, str | bool | None] = {
        "bermuda_coordinator": str(source),
        "bermuda_revision": None,
        "bermuda_dirty": None,
    }
    git = shutil.which("git")
    if git is None:
        return result
    try:
        status = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, "status", "--porcelain=v2", "--branch"],
            cwd=source.parents[2],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return result
    result["bermuda_revision"] = next(
        (line.removeprefix("# branch.oid ") for line in status if line.startswith("# branch.oid ")), None
    )
    result["bermuda_dirty"] = any(not line.startswith("# ") for line in status)
    return result


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    results = [
        await _run_once(
            args.devices,
            args.scanners,
            args.fixed_references,
            args.warmup,
            args.cycles,
            args.heartbeat_ms,
            args.inter_cycle_yield_ms,
        )
        for _ in range(args.runs)
    ]
    return {
        "fixture": {
            "devices": args.devices,
            "scanners": args.scanners,
            "packets_per_cycle": args.devices * args.scanners,
            "fixed_references": args.fixed_references,
            "warmup_cycles": args.warmup,
            "measured_cycles_per_run": args.cycles,
            "runs": args.runs,
            "heartbeat_period_ms": args.heartbeat_ms,
            "inter_cycle_yield_ms": args.inter_cycle_yield_ms,
            "path": (
                "real BermudaDataUpdateCoordinator._async_update_data_internal -> gather -> "
                "device/adverts -> ToolkitManager/profile -> area refresh"
            ),
        },
        "median": _median_result(results),
        "runs": [asdict(result) for result in results],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", type=int, default=32)
    parser.add_argument("--scanners", type=int, default=6)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--fixed-references", type=int, default=4)
    parser.add_argument("--heartbeat-ms", type=float, default=1.0)
    parser.add_argument("--inter-cycle-yield-ms", type=float, default=1.0)
    parser.add_argument("--json", type=Path, help="write the complete result as JSON")
    args = parser.parse_args()
    if (
        min(args.devices, args.scanners, args.warmup, args.cycles, args.runs) < 1
        or min(args.heartbeat_ms, args.inter_cycle_yield_ms) <= 0
    ):
        parser.error("devices, scanners, warmup, cycles, runs and timing periods must be positive")
    if not 0 <= args.fixed_references <= args.devices:
        parser.error("fixed reference count must be between zero and device count")
    return args


if __name__ == "__main__":
    arguments = _parse_args()
    runtime = {
        **_source_metadata(),
        "python": sys.version,
        "home_assistant": version("homeassistant"),
        "platform": platform.platform(),
    }
    report = asyncio.run(_main(arguments))
    report["runtime"] = runtime
    print(json.dumps(report, indent=2, sort_keys=True))
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
