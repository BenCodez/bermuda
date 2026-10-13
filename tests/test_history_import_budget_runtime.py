"""Real HA Store tests for normalized diagnostic import byte boundaries."""

from copy import deepcopy

import pytest

from custom_components.bermuda.toolkit import HISTORY_BYTES, ToolkitManager

from .test_diagnostic_history import integer_history_payload, wire_bytes
from .test_toolkit_history_runtime import measured, runtime


@pytest.mark.parametrize("device", ["device-abcdef", "d"])
async def test_manager_rejects_normalized_or_redacted_overflow_without_replacing_live_history_or_store(runtime, device):
    manager = runtime.manager
    assert manager.history.max_bytes == HISTORY_BYTES == 1_500_000
    await manager.async_dispatch("capture", measured())
    history = manager.history
    original = history.export(redact=False)
    clocks = history.export_clocks()
    stored = deepcopy(await manager.store.async_load())
    options = deepcopy(dict(manager.entry.options))
    payload = integer_history_payload(device=device)
    assert len(wire_bytes(payload)) < HISTORY_BYTES
    with pytest.raises(ValueError, match="normalized import exceeds"):
        await manager.async_dispatch("history_import", {"data": payload})
    assert manager.history is history
    assert history.export(redact=False) == original and history.export_clocks() == clocks
    assert dict(manager.entry.options) == options
    assert await manager.store.async_load() == stored


async def test_near_limit_successful_manager_import_and_both_exports_survive_real_store_reload(runtime):
    manager = runtime.manager
    payload = integer_history_payload(frame_count=480)
    imported = await manager.async_dispatch("history_import", {"data": payload})
    assert imported["rows"] == 4800
    canonical = await manager.async_dispatch("history_export", {"redact": False})
    redacted = await manager.async_dispatch("history_export", {})
    assert len(wire_bytes(canonical)) <= HISTORY_BYTES
    assert HISTORY_BYTES - 30_000 < len(wire_bytes(redacted)) <= HISTORY_BYTES
    stored = await manager.store.async_load()
    assert stored["history"] == canonical
    restored = ToolkitManager(runtime.coordinator)
    try:
        await restored.async_load()
        assert restored.history.frames() == manager.history.frames()
        assert restored.history.summary()["rows"] == 4800
        assert not restored.last_result or "saved_history_invalid" not in restored.last_result.get("reasons", [])
        assert len(wire_bytes(restored.history.export(redact=False))) <= HISTORY_BYTES
        assert len(wire_bytes(restored.history.export())) <= HISTORY_BYTES
    finally:
        await restored.async_close()
