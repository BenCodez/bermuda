"""
Durable atomic storage for toolkit settings, evidence, and rollback records.

Home Assistant's Store intentionally logs and suppresses write/serialization
failures and defers stopping-state saves. Toolkit transactions require an actual
completed write, so this narrow override keeps Store's envelope, cache invalidation,
pending-data consumption, listener cleanup, and inherited write lock while making
failures observable. These private Store hooks must be checked against supported
Home Assistant releases whenever its storage helper changes.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError

if TYPE_CHECKING:
    from json import JSONEncoder

    from homeassistant.core import HomeAssistant


class DurableToolkitStore(Store[dict]):
    """An atomic Store whose save succeeds only after durable write completion."""

    def __init__(
        self,
        hass: HomeAssistant,
        version: int,
        key: str,
        private: bool = False,  # noqa: FBT001 - preserve the Store constructor API
        *,
        atomic_writes: bool = True,
        encoder: type[JSONEncoder] | None = None,
        max_readable_version: int | None = None,
        minor_version: int = 1,
        read_only: bool = False,
        serialize_in_event_loop: bool = True,
    ) -> None:
        if atomic_writes is not True:
            msg = "Toolkit storage requires atomic writes"
            raise ValueError(msg)
        options = {"atomic_writes": True, "encoder": encoder, "minor_version": minor_version, "read_only": read_only}
        supported = inspect.signature(Store.__init__).parameters
        for name, value, default in (
            ("max_readable_version", max_readable_version, None),
            ("serialize_in_event_loop", serialize_in_event_loop, True),
        ):
            if name in supported:
                options[name] = value
            elif value != default:
                msg = f"This Home Assistant release does not support {name}"
                raise ValueError(msg)
        super().__init__(hass, version, key, private, **options)

    async def async_save(self, data: dict) -> None:
        """Flush stopping-state saves as well as normal Store saves."""
        await super().async_save(data)
        if self.hass.state is CoreState.stopping:
            await self._async_handle_write_data()

    async def _async_handle_write_data(self, *_args: Any) -> None:
        """Keep core's write lifecycle but propagate failures and drain cancelled I/O."""
        async with self._write_lock:
            self._manager.async_invalidate(self.key)
            self._async_cleanup_delay_listener()
            self._async_cleanup_final_write_listener()
            if self._data is None:
                return
            data, self._data = self._data, None
            if self._read_only:
                msg = "Toolkit storage is read-only"
                raise OSError(msg)
            # Cancellation of an executor await cannot stop the disk write. Keep
            # the lock until it finishes so a later rollback never races that I/O.
            writer = self._async_write_data
            args = (self.path, data) if "path" in inspect.signature(writer).parameters else (data,)
            task = asyncio.create_task(writer(*args))
            cancelled = False
            try:
                while True:
                    try:
                        await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        if task.done():
                            task.result()
                            break
            except (SerializationError, WriteError):
                msg = "Toolkit data could not be durably saved"
                raise OSError(msg) from None
            if cancelled:
                raise asyncio.CancelledError
