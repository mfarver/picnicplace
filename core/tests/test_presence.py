import asyncio

from picnicplace.events import EventBus
from picnicplace.presence import PresenceTracker


async def _flush() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_grace_period_fires_only_when_nobody_returns() -> None:
    async def scenario() -> None:
        bus = EventBus()
        sub = bus.subscribe()
        tracker = PresenceTracker(bus, grace_period_s=0.1)
        calls: list[str] = []

        async def on_empty() -> None:
            calls.append("empty")

        async def on_grace() -> None:
            calls.append("grace")

        tracker.on_empty.append(on_empty)
        tracker.on_grace_expired.append(on_grace)

        tracker.add("a")
        tracker.remove("a")
        await _flush()
        assert calls == ["empty"]
        tracker.add("b")  # returns within the grace period
        await asyncio.sleep(0.2)
        assert calls == ["empty"]

        tracker.remove("b")
        await asyncio.sleep(0.2)
        assert calls == ["empty", "empty", "grace"]

        types: list[str] = []
        while True:
            try:
                types.append((await asyncio.wait_for(sub.get(), 0.01)).type)
            except TimeoutError:
                break
        assert types[-1] == "presence.lost"
        await tracker.shutdown()

    asyncio.run(scenario())


def test_removing_unknown_client_is_ignored() -> None:
    async def scenario() -> None:
        tracker = PresenceTracker(EventBus(), grace_period_s=0.1)
        tracker.remove("nobody")
        assert not tracker.present
        await tracker.shutdown()

    asyncio.run(scenario())
