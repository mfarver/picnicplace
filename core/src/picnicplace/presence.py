"""Tracks which clients are connected and enforces the presence rules (spec section 5.3).

- Motion is allowed only while at least one client is connected.
- When the last client leaves, ``on_empty`` callbacks run right away (stop manual motion).
- If no client returns within the grace period, ``on_grace_expired`` callbacks run
  (pause running jobs).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from picnicplace.events import EventBus
from picnicplace.schemas import PresenceEvent, PresenceLostEvent, PresenceState

log = logging.getLogger(__name__)

AsyncCallback = Callable[[], Awaitable[None]]


class PresenceTracker:
    def __init__(self, bus: EventBus, grace_period_s: float) -> None:
        self._bus = bus
        self._grace_period_s = grace_period_s
        self._clients: set[str] = set()
        self._grace_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.on_change: list[Callable[[], None]] = []
        self.on_empty: list[AsyncCallback] = []
        self.on_grace_expired: list[AsyncCallback] = []

    @property
    def present(self) -> bool:
        return bool(self._clients)

    @property
    def state(self) -> PresenceState:
        return PresenceState(clients=len(self._clients), present=self.present)

    def add(self, client_id: str) -> None:
        self._clients.add(client_id)
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
        self._changed()

    def remove(self, client_id: str) -> None:
        if client_id not in self._clients:
            return
        self._clients.remove(client_id)
        self._changed()
        if not self._clients:
            log.info("last client disconnected")
            for callback in self.on_empty:
                self._spawn(callback())
            self._grace_task = self._spawn(self._grace_timer())

    async def shutdown(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _changed(self) -> None:
        self._bus.publish(PresenceEvent(presence=self.state))
        for callback in self.on_change:
            callback()

    async def _grace_timer(self) -> None:
        await asyncio.sleep(self._grace_period_s)
        self._grace_task = None
        log.warning("no clients for %.1f s; pausing jobs", self._grace_period_s)
        self._bus.publish(PresenceLostEvent(grace_period_s=self._grace_period_s))
        for callback in self.on_grace_expired:
            await self._run_logged(callback())

    def _spawn(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(self._run_logged(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _run_logged(coro: Awaitable[None]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("presence callback failed")
