"""In-process publish/subscribe bus that feeds the WebSocket event stream."""

import asyncio
import logging

from picnicplace.schemas import Event

log = logging.getLogger(__name__)


class SubscriptionOverflowError(Exception):
    """The subscriber fell too far behind and its events were dropped."""


class _Overflow:
    pass


_OVERFLOW = _Overflow()


class Subscription:
    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[Event | _Overflow] = asyncio.Queue(maxsize)
        self._overflowed = False

    def push(self, event: Event) -> None:
        """Queue an event for this subscriber without blocking."""
        if self._overflowed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop everything and tell the reader to disconnect; the client will reconnect
            # and receive fresh state in its hello message.
            self._overflowed = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(_OVERFLOW)

    async def get(self) -> Event:
        item = await self._queue.get()
        if isinstance(item, _Overflow):
            raise SubscriptionOverflowError
        return item


class EventBus:
    def __init__(self, queue_size: int = 512) -> None:
        self._queue_size = queue_size
        self._subscriptions: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        sub = Subscription(self._queue_size)
        self._subscriptions.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscriptions.discard(sub)

    def publish(self, event: Event) -> None:
        for sub in tuple(self._subscriptions):
            sub.push(event)
