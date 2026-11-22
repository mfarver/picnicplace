"""The control panel's link: a stand-in panel on an in-memory line link, against the core
with the simulator."""

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from picnicplace import panel as panel_module
from picnicplace.api import create_app
from picnicplace.api.context import AppContext
from picnicplace.machine.marlin.transport import LineTransport, MemoryTransport, memory_pair
from picnicplace.panel import PanelLink
from picnicplace.schemas import Operation, OperationEvent
from tests.conftest import connect, fast_config, wait_for


class FakePanel:
    """The panel's end of the link, driven from the test's thread."""

    def __init__(self, client: TestClient, end: MemoryTransport) -> None:
        assert client.portal is not None
        self._portal = client.portal
        self._end = end

    def send(self, message: dict[str, Any] | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        self._portal.call(self._end.write_line, line)

    def receive(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 3.0) -> Any:
        """The next message from the core that matches ``predicate``."""

        async def read() -> dict[str, Any]:
            async with asyncio.timeout(timeout):
                while True:
                    message = json.loads(await self._end.read_line())
                    if predicate(message):
                        return message

        return self._portal.call(read)

    def state(self, predicate: Callable[[dict[str, Any]], bool] = lambda _: True) -> Any:
        return self.receive(lambda m: m["type"] == "state" and predicate(m))

    def message(self) -> Any:
        return self.receive(lambda m: m["type"] == "message")


@contextmanager
def with_panel(client: TestClient) -> Iterator[tuple[FakePanel, PanelLink]]:
    """A panel link on the running app, its port an in-memory link to a stand-in panel."""
    ctx: AppContext = client.app.state.ctx  # type: ignore[attr-defined]
    assert client.portal is not None
    core_end, panel_end = client.portal.call(_pair)

    async def open_port() -> LineTransport:
        return core_end

    link = PanelLink(
        machine=ctx.machine,
        lease=ctx.lease,
        presence=ctx.presence,
        bus=ctx.bus,
        runner=lambda: ctx.runner,
        open_port=open_port,
        label="memory",
    )
    client.portal.call(_start, link)
    try:
        yield FakePanel(client, panel_end), link
    finally:
        client.portal.call(link.stop)


async def _pair() -> tuple[MemoryTransport, MemoryTransport]:
    return memory_pair()


async def _start(link: PanelLink) -> None:
    link.start()


HELLO = {"type": "hello", "firmware": "0.1.0", "screen": {"width": 1024, "height": 600}}


def test_a_panel_that_says_hello_is_a_client_and_gets_the_state() -> None:
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, link):
        panel.send('I (895) gt911: product ID "911"')  # its log, when on the UART
        panel.send(HELLO)
        state = panel.state()
        assert link.firmware == "0.1.0"
        assert client.get("/api/v1/presence").json()["clients"] == 1
        assert state["connected"] is True and state["homed"] is False
        assert state["control"] is None and state["run"] == "ready"
        assert set(state["position"]) >= {"X", "Y", "Z"}
        assert isinstance(state["tips"], dict) and state["tips"]


def test_states_come_at_least_twice_a_second() -> None:
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, _):
        panel.send(HELLO)
        panel.state()
        for _ in range(3):
            panel.state()  # each within the receive's timeout, and soon


def test_home_from_the_panel_takes_control_and_homes() -> None:
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, link):
        panel.send(HELLO)
        panel.state()
        panel.send({"type": "press", "key": "home"})
        state = panel.state(lambda s: s["homed"])
        assert state["control"] == "panel"
        assert link.client is not None
        assert client.get("/api/v1/lease").json()["holder"]["name"] == "Control panel"


def test_a_key_is_refused_while_the_studio_has_control() -> None:
    with TestClient(create_app(fast_config())) as client, connect(client, "Studio") as s:
        s.home()  # the Studio takes control by moving the machine
        with with_panel(client) as (panel, _):
            panel.send(HELLO)
            assert panel.state()["control"] == "Studio"
            panel.send({"type": "press", "key": "home"})
            message = panel.message()
            assert message["level"] == "error"
            assert "Studio" in message["text"] and "take control" in message["text"]


def test_pause_works_without_control_and_resume_takes_it() -> None:
    with TestClient(create_app(fast_config())) as client, connect(client, "Studio") as s:
        s.home()
        s.delete("/api/v1/lease")
        with with_panel(client) as (panel, _):
            panel.send(HELLO)
            panel.send({"type": "press", "key": "pause"})
            assert panel.state(lambda m: m["run"] == "paused")["control"] is None
            panel.send({"type": "press", "key": "resume"})
            state = panel.state(lambda m: m["run"] == "ready")
            assert state["control"] == "panel"


def test_a_failed_operation_is_sent_as_a_message() -> None:
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, _):
        ctx: AppContext = client.app.state.ctx  # type: ignore[attr-defined]
        panel.send(HELLO)
        panel.state()
        failed = Operation(
            id="op1",
            kind="home",
            status="failed",
            error="the safe-Z sensor did not trigger",
            started_at=datetime.now(UTC),
        )
        ctx.bus.publish(OperationEvent(operation=failed))
        message = panel.message()
        assert message == {
            "type": "message",
            "level": "error",
            "text": "the safe-Z sensor did not trigger",
        }


def test_an_unknown_key_is_refused_with_a_message() -> None:
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, _):
        panel.send(HELLO)
        panel.send({"type": "press", "key": "jog"})
        assert "jog" in panel.message()["text"]


def test_a_silent_panel_is_no_longer_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_module, "SILENCE_S", 0.3)
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, link):
        panel.send(HELLO)
        panel.state()
        assert client.get("/api/v1/presence").json()["clients"] == 1
        wait_for(lambda: link.client is None)
        assert client.get("/api/v1/presence").json()["clients"] == 0


def test_the_estop_pole_is_kept() -> None:
    with TestClient(create_app(fast_config())) as client, with_panel(client) as (panel, link):
        panel.send(HELLO)
        panel.send({"type": "estop", "pressed": True})
        wait_for(lambda: link.estop_pressed is True)
        panel.send({"type": "estop", "pressed": False})
        wait_for(lambda: link.estop_pressed is False)
