"""The control lease (spec sections 5.2 and 5.3).

Any number of clients may watch, but only the lease holder may move the machine or switch
outputs. A client that commands motion while no one holds the lease takes it implicitly.
Taking it from another client needs ``force``, which clients should confirm with the user.
When the holder disconnects, the lease is released and its manual motion stops.
"""

import logging
from collections.abc import Awaitable, Callable

from picnicplace.events import EventBus
from picnicplace.machine.errors import ClientRequiredError, LeaseHeldError
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import ClientInfo, LeaseEvent, LeaseState

log = logging.getLogger(__name__)


class LeaseManager:
    def __init__(self, bus: EventBus, presence: PresenceTracker) -> None:
        self._bus = bus
        self._presence = presence
        self._holder: ClientInfo | None = None
        self.on_holder_left: list[Callable[[], Awaitable[None]]] = []
        presence.on_remove.append(self._on_client_removed)

    @property
    def state(self) -> LeaseState:
        return LeaseState(holder=self._holder)

    def acquire(self, client_id: str | None, *, force: bool = False) -> LeaseState:
        """Take the lease for a connected client. Raises if another client holds it."""
        client = self._connected(client_id)
        if self._holder is not None and self._holder.id != client.id:
            if not force:
                raise LeaseHeldError(
                    f"{_describe(self._holder)} has control of the machine; take control first"
                )
            log.warning("%s took control from %s", _describe(client), _describe(self._holder))
        if self._holder is None or self._holder.id != client.id:
            self._set(client)
        return self.state

    def release(self, client_id: str | None) -> LeaseState:
        client = self._connected(client_id)
        if self._holder is not None and self._holder.id == client.id:
            self._set(None)
        return self.state

    def require(self, client_id: str | None) -> ClientInfo:
        """Check that a client may command motion, taking a free lease for it."""
        self.acquire(client_id)
        assert self._holder is not None
        return self._holder

    def _connected(self, client_id: str | None) -> ClientInfo:
        client = self._presence.get(client_id) if client_id else None
        if client is None:
            raise ClientRequiredError(
                "motion commands must come from a client connected to the event socket; "
                "send its client ID in the X-PicnicPlace-Client header"
            )
        return client

    def _set(self, holder: ClientInfo | None) -> None:
        self._holder = holder
        self._bus.publish(LeaseEvent(lease=self.state))

    async def _on_client_removed(self, client: ClientInfo) -> None:
        if self._holder is None or self._holder.id != client.id:
            return
        log.info("lease holder %s disconnected; releasing control", _describe(client))
        self._set(None)
        for callback in self.on_holder_left:
            await callback()


def _describe(client: ClientInfo) -> str:
    return f"{client.name} ({client.address})" if client.address else client.name
