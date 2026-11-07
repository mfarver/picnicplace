import asyncio

import pytest

from picnicplace.events import EventBus, SubscriptionOverflowError
from picnicplace.schemas import ErrorEvent


def test_slow_subscriber_overflows_without_affecting_others() -> None:
    async def scenario() -> None:
        bus = EventBus(queue_size=3)
        slow = bus.subscribe()
        fast = bus.subscribe()
        for i in range(3):
            bus.publish(ErrorEvent(message=str(i)))
            assert (await fast.get()).type == "error"
        bus.publish(ErrorEvent(message="overflow"))
        with pytest.raises(SubscriptionOverflowError):
            await slow.get()
        assert isinstance(await fast.get(), ErrorEvent)

    asyncio.run(scenario())
