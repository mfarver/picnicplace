"""The control panel's link: a touchscreen on the machine, on a USB serial port.

The panel (``panel/`` in the repository, and docs/hardware/control-panel.md) keeps no
state of its own: the core sends it the machine's ``state`` at least once a second and on
every change, and an error ``message`` when an operation fails. The panel sends ``hello``
when it starts, ``ping`` every second, a ``press`` for each key, and the E-stop's sensing
pole as ``estop``. Messages are JSON, one per line.

While it answers, the panel is a connected client like the Studio: it counts as presence,
and a key that moves the machine needs the control lease, taken as the Studio takes it (a
free lease implicitly; one the Studio holds, not at all). Pause and Stop work from any
client, as in the API. The panel can do nothing the API cannot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from picnicplace.events import EventBus, Subscription
from picnicplace.job.run import JobRunner
from picnicplace.lease import LeaseManager
from picnicplace.machine.errors import MachineError
from picnicplace.machine.marlin.transport import LineTransport, SerialTransport
from picnicplace.machine.service import MachineService
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import ClientInfo, LeaseEvent, MachineStateEvent, OperationEvent

log = logging.getLogger(__name__)

STATE_EVERY_S = 0.5
"""The panel says it has no connection after two seconds without a state."""
STATE_MIN_GAP_S = 0.1
"""At most ten states a second, however fast the machine's state changes."""
SILENCE_S = 3.0
"""A panel that sends nothing (it pings every second) for this long is gone."""
REOPEN_S = 2.0
"""How often to try a port that is missing or was unplugged."""
KEYS = ("home", "start", "pause", "resume", "stop")

Opener = Callable[[], Awaitable[LineTransport]]


class PanelLink:
    """Serve one control panel, reopening its port whenever it goes away."""

    def __init__(
        self,
        machine: MachineService,
        lease: LeaseManager,
        presence: PresenceTracker,
        bus: EventBus,
        runner: Callable[[], JobRunner | None],
        open_port: Opener,
        label: str,
    ) -> None:
        self._machine = machine
        self._lease = lease
        self._presence = presence
        self._bus = bus
        self._runner = runner
        self._open = open_port
        self._label = label
        self._client: ClientInfo | None = None
        self._transport: LineTransport | None = None
        self._task: asyncio.Task[None] | None = None
        self.firmware: str | None = None
        self.estop_pressed: bool | None = None
        """The E-stop's sensing pole, as the panel last said; None until it does."""

    @classmethod
    def serial(cls, path: str, **kwargs: Any) -> PanelLink:
        async def open_port() -> LineTransport:
            return await SerialTransport.open(path)

        return cls(open_port=open_port, label=path, **kwargs)

    @property
    def client(self) -> ClientInfo | None:
        """The panel as a connected client, while it answers."""
        return self._client

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        warned = False
        while True:
            try:
                transport = await self._open()
            except OSError as e:
                if not warned:
                    log.warning("control panel: cannot open %s (%s); retrying", self._label, e)
                    warned = True
                await asyncio.sleep(REOPEN_S)
                continue
            warned = False
            log.info("control panel: opened %s", self._label)
            try:
                await self._serve(transport)
            except Exception:
                log.exception("control panel link failed")
            finally:
                self._gone()
                with contextlib.suppress(Exception):
                    await transport.close()
            await asyncio.sleep(REOPEN_S)

    async def _serve(self, transport: LineTransport) -> None:
        self._transport = transport
        sub = self._bus.subscribe()
        changed = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as tg:
                reader = tg.create_task(self._read(transport, changed))
                sender = tg.create_task(self._send_states(transport, changed))
                watcher = tg.create_task(self._watch(sub, transport, changed))
                await reader  # the port closed, or the panel fell silent
                sender.cancel()
                watcher.cancel()
        finally:
            self._bus.unsubscribe(sub)
            self._transport = None

    async def _read(self, transport: LineTransport, changed: asyncio.Event) -> None:
        while True:
            try:
                async with asyncio.timeout(SILENCE_S if self._client else None):
                    line = await transport.read_line()
            except TimeoutError:
                log.warning("control panel: silent for %.0f s", SILENCE_S)
                return
            except ConnectionError:
                return
            if not line.startswith("{"):
                continue  # its log, when the link shares the UART
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if self._client is None:
                self._arrived()
                changed.set()
            try:
                await self._handle(message, transport)  # type: ignore[arg-type]
            except ConnectionError:
                return

    def _arrived(self) -> None:
        self._client = ClientInfo(
            id=f"panel-{uuid.uuid4().hex[:6]}", name="Control panel", address=None
        )
        self._presence.add(self._client)
        log.info("control panel connected on %s", self._label)

    def _gone(self) -> None:
        if self._client is not None:
            log.info("control panel disconnected")
            self._presence.remove(self._client.id)
            self._client = None

    async def _handle(self, message: dict[str, Any], transport: LineTransport) -> None:
        kind = message.get("type")
        if kind == "hello":
            firmware = message.get("firmware")
            self.firmware = firmware if isinstance(firmware, str) else None
            log.info("control panel firmware %s", self.firmware)
        elif kind == "estop":
            pressed = bool(message.get("pressed"))
            if pressed != self.estop_pressed:
                log.warning("control panel: E-stop %s", "pressed" if pressed else "released")
            self.estop_pressed = pressed
        elif kind == "press":
            key = message.get("key")
            error = self.press(key if isinstance(key, str) else "")
            if error is not None:
                await self._send(transport, {"type": "message", "level": "error", "text": error})

    def press(self, key: str) -> str | None:
        """Act on a key, as the Studio's button does: None, or why it was refused."""
        if key not in KEYS:
            return f"the core does not know the key '{key}'"
        client = self._client
        if client is None:
            return "the panel is not connected"
        runner = self._runner()
        run = runner.state() if runner is not None else None
        try:
            if key == "home":
                self._lease.require(client.id)
                self._machine.start_home()
            elif key == "start":
                return "start a job from the Studio; the panel cannot start one yet"
            elif key == "pause":
                if runner is not None and run is not None and run.status == "running":
                    self._lease.require(client.id)
                    runner.pause()
                else:
                    self._machine.pause()
            elif key == "resume":
                self._lease.require(client.id)
                if self._machine.paused:
                    self._machine.resume()
                elif runner is not None and run is not None and run.status == "paused":
                    runner.resume()
            elif runner is not None and run is not None and run.status in ("running", "paused"):
                self._lease.require(client.id)
                runner.stop()
            else:
                self._machine.request_stop()
        except MachineError as e:
            return str(e)
        return None

    def state_message(self) -> dict[str, Any]:
        """What the panel draws: the machine's state, as it stands now."""
        m = self._machine.state
        runner = self._runner()
        run = runner.state() if runner is not None else None
        if m.request is not None:
            status = "waiting"
        elif m.paused or (run is not None and run.status == "paused"):
            status = "paused"
        elif m.busy or (run is not None and run.status == "running"):
            status = "running"
        else:
            status = "ready"
        holder = self._lease.state.holder
        if holder is None:
            control = None
        elif self._client is not None and holder.id == self._client.id:
            control = "panel"
        else:
            control = holder.name
        return {
            "type": "state",
            "connected": m.connected,
            "homed": m.homed,
            "control": control,
            "run": status,
            "position": {axis: round(v, 3) for axis, v in m.position.items()},
            "tips": {n.label: n.tip or "" for n in self._machine.profile.nozzles},
        }

    async def _send(self, transport: LineTransport, message: dict[str, Any]) -> None:
        await transport.write_line(json.dumps(message, separators=(",", ":")))

    async def _send_states(self, transport: LineTransport, changed: asyncio.Event) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(STATE_EVERY_S):
                        await changed.wait()
                changed.clear()
                if self._client is not None:
                    await self._send(transport, self.state_message())
                await asyncio.sleep(STATE_MIN_GAP_S)

    async def _watch(
        self, sub: Subscription, transport: LineTransport, changed: asyncio.Event
    ) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                event = await sub.get()
                if isinstance(event, (MachineStateEvent, LeaseEvent)):
                    changed.set()
                elif isinstance(event, OperationEvent):
                    changed.set()
                    op = event.operation
                    if op.status == "failed" and op.error and self._client is not None:
                        await self._send(
                            transport, {"type": "message", "level": "error", "text": op.error}
                        )
