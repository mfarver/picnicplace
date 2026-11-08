"""The Marlin motion driver (spec section 7.2).

Talks G-code over a line transport, one command at a time: each command is sent and its
reply lines are collected until ``ok``. That is simple and robust, and fast enough for
manual control; streaming several commands ahead can come later for jobs.

What the driver does, and the G-code it uses (the machine profile's ``[marlin]`` table
supplies the machine-specific parts):

- Connect: open the port, wait for the board, ``M115`` to identify the firmware, the
  profile's connect commands, all outputs off, then ``M114`` for the position.
- Home: the profile's home commands (``G28``), then ``M114``.
- Move: ``G0`` with only the axes that change and a feed rate, then ``M400`` to wait for
  the move to finish, then ``M114``. While a move runs, the reported position is
  interpolated so clients see the head travel; ``M114`` corrects it at the end.
- Outputs: the profile's on and off commands.
- Analog inputs (vacuum sensors): polled with the profile's read commands, faster while
  any output is on.
- Stop: the profile's stop command (``M410``), sent at once, bypassing the queue.
- Emergency stop: ``M112``, sent at once. The firmware halts; ``reset`` reconnects and
  waits for it to come back (someone may need to press its reset button).
"""

import asyncio
import contextlib
import logging
import math
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import MachineError, MotionInterruptedError, NotConnectedError
from picnicplace.machine.marlin.settings import MarlinInput, MarlinSettings
from picnicplace.machine.marlin.transport import LineTransport, TransportClosedError
from picnicplace.machine.profile import MachineProfile

log = logging.getLogger(__name__)

OpenTransport = Callable[[], Awaitable[LineTransport]]

POLL_ACTIVE_S = 0.1
"""Analog input polling interval while any output is on (vacuum in use)."""

POLL_IDLE_S = 1.0
"""Analog input polling interval otherwise."""

INTERPOLATE_S = 0.05
"""How often the estimated position is updated during a move."""

HOME_TIMEOUT_S = 120.0
RESET_TIMEOUT_S = 30.0
"""How long ``reset`` waits for the firmware to answer again after an emergency stop."""


class FirmwareError(MachineError):
    code = "firmware_error"


@dataclass
class _Pending:
    """A command waiting for its ``ok``. Commands sent outside the queue that still get an
    ``ok`` in order (``M410``) have no future: their ``ok`` is consumed silently."""

    line: str
    future: asyncio.Future[list[str]] | None
    lines: list[str] = field(default_factory=list[str])


class MarlinDriver(MotionDriver):
    name = "marlin"

    def __init__(
        self,
        profile: MachineProfile,
        settings: MarlinSettings,
        open_transport: OpenTransport,
        *,
        connect_wait_s: float = 3.0,
        command_timeout_s: float = 10.0,
    ) -> None:
        super().__init__()
        self._profile = profile
        self._settings = settings
        self._open_transport = open_transport
        self._connect_wait_s = connect_wait_s
        self._timeout_s = command_timeout_s
        self._axes = [a.name for a in profile.axes]
        self._speeds = {a.name: a.max_speed for a in profile.axes}
        self._position = dict.fromkeys(self._axes, 0.0)
        self._outputs = {o.id: False for o in profile.outputs}
        self._inputs: dict[str, bool | float] = {
            i.id: (0.0 if i.kind == "analog" else False) for i in profile.inputs
        }
        self._input_patterns = {k: re.compile(v.pattern) for k, v in settings.inputs.items()}
        self._digital = {i.id for i in profile.inputs if i.kind == "digital"}

        self._transport: LineTransport | None = None
        self._reader: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._pending: deque[_Pending] = deque()
        self._connected = False
        self._halted = False
        self._stopping = False
        self.firmware: str | None = None
        self.machine_type: str | None = None
        self._warnings: dict[str, str] = {}

    # --- State ---------------------------------------------------------------------------

    @property
    def axes(self) -> Sequence[str]:
        return list(self._axes)

    @property
    def position(self) -> Mapping[str, float]:
        return dict(self._position)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def inputs(self) -> Mapping[str, bool | float]:
        return dict(self._inputs)

    @property
    def outputs(self) -> Mapping[str, bool]:
        return dict(self._outputs)

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def warnings(self) -> Sequence[str]:
        return list(self._warnings.values())

    def _warn(self, key: str, message: str | None) -> None:
        """Set (or with None, clear) a warning for the operator."""
        if message is None:
            if self._warnings.pop(key, None) is not None:
                self._notify()
            return
        if self._warnings.get(key) != message:
            log.warning("%s", message)
            self._warnings[key] = message
            self._notify()

    # --- Connection ----------------------------------------------------------------------

    async def connect(self) -> None:
        await self._stop_poller()
        await self._close()
        await self._open()
        try:
            if self._connect_wait_s > 0:
                # Many boards reset when the port opens and ignore input while booting.
                await asyncio.sleep(self._connect_wait_s)
            await self._handshake(timeout_s=self._timeout_s)
        except BaseException:
            await self._close()
            raise
        self._poller = asyncio.create_task(self._poll_inputs())

    async def disconnect(self) -> None:
        await self._stop_poller()
        await self._close()

    async def _stop_poller(self) -> None:
        poller, self._poller = self._poller, None
        if poller is not None and poller is not asyncio.current_task():
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller

    async def _open(self) -> None:
        self._transport = await self._open_transport()
        self._reader = asyncio.create_task(self._read_lines(self._transport))

    async def _close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        self._fail_pending(NotConnectedError("the machine disconnected"))
        if self._connected:
            self._connected = False
            self._notify()

    async def _handshake(self, timeout_s: float) -> None:
        """Identify the firmware, configure it, and learn the position."""
        reply = await self._command("M115", timeout_s=timeout_s)
        info = next((line for line in reply if "FIRMWARE_NAME:" in line), "")
        name = re.search(r"FIRMWARE_NAME:(.*?)(?: [A-Z_]+:|$)", info)
        machine = re.search(r"MACHINE_TYPE:(\S+)", info)
        self.firmware = name.group(1).strip() if name else None
        self.machine_type = machine.group(1) if machine else None
        expected = self._settings.expected_machine_type
        self._warn(
            "machine_type",
            f"the firmware reports machine type {self.machine_type!r}, but the machine "
            f"profile expects {expected!r}"
            if expected and self.machine_type != expected
            else None,
        )
        no_parser = any("Cap:EMERGENCY_PARSER:0" in line for line in reply)
        self._warn(
            "emergency_parser",
            "the firmware has no emergency parser: STOP takes effect only after the current "
            "move finishes. Keep a hand on the machine's E-stop."
            if no_parser
            else None,
        )
        log.info("connected to %s (%s)", self.firmware, self.machine_type)
        for line in self._settings.connect:
            await self._command(line)
        for output_id, spec in self._settings.outputs.items():
            for line in spec.off:
                await self._command(line)
            self._outputs[output_id] = False
        self._halted = False
        self._connected = True
        self._warn("link", None)
        await self._sync_position()
        self._notify()

    # --- Motion --------------------------------------------------------------------------

    async def home(self) -> None:
        self._stopping = False
        for line in self._settings.home:
            await self._command(line, timeout_s=HOME_TIMEOUT_S)
        self._check_stopped()
        await self._sync_position()

    async def move_to(self, target: Mapping[str, float], speed: float | None) -> None:
        self._stopping = False
        start = dict(self._position)
        moves = {a: v for a, v in target.items() if abs(v - start[a]) > 1e-6}
        if not moves:
            return
        feed = min(self._speeds[a] for a in moves)
        if speed is not None:
            feed = min(feed, speed)
        words = " ".join(f"{a}{v:.4f}" for a, v in moves.items())
        distance = math.sqrt(sum((v - start[a]) ** 2 for a, v in moves.items()))
        duration = distance / feed

        await self._command(f"G0 {words} F{feed * 60:.0f}")
        follow = asyncio.create_task(self._interpolate(start, {**start, **moves}, duration))
        try:
            await self._command("M400", timeout_s=duration * 2 + self._timeout_s)
        finally:
            follow.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follow
        if self._connected and not self._halted:
            await self._sync_position()
        self._check_stopped()

    async def _interpolate(
        self, start: dict[str, float], end: dict[str, float], duration: float
    ) -> None:
        """Estimate the position during a move, for display."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while True:
            frac = 1.0 if duration <= 0 else min(1.0, (loop.time() - t0) / duration)
            self._position = {a: start[a] + (end[a] - start[a]) * frac for a in start}
            self._notify()
            if frac >= 1.0:
                return
            await asyncio.sleep(INTERPOLATE_S)

    def _check_stopped(self) -> None:
        if self._stopping or self._halted:
            self._stopping = False
            raise MotionInterruptedError("motion was stopped")

    async def stop(self) -> None:
        if not self._connected:
            return
        self._stopping = True
        await self._send_now(self._settings.stop, expects_ok=True)

    async def emergency_stop(self) -> None:
        self._halted = True
        self._stopping = True
        with contextlib.suppress(TransportClosedError, NotConnectedError):
            await self._send_now(self._settings.estop, expects_ok=False)
        self._fail_pending(MotionInterruptedError("emergency stop"))
        for output_id in self._outputs:
            self._outputs[output_id] = False
        self._notify()

    async def reset(self) -> None:
        """Reconnect after an emergency stop, waiting for the firmware to answer again."""
        await self._stop_poller()
        await self._close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESET_TIMEOUT_S
        while True:
            try:
                await self._open()
                await self._handshake(timeout_s=2.0)
                break
            except (TimeoutError, TransportClosedError, OSError, MachineError) as e:
                await self._close()
                if loop.time() > deadline:
                    raise FirmwareError(
                        "the controller does not answer after the emergency stop; press its "
                        "reset button or power-cycle it, then reset again"
                    ) from e
                await asyncio.sleep(0.5)
        self._stopping = False
        self._poller = asyncio.create_task(self._poll_inputs())

    async def set_position(self, position: Mapping[str, float]) -> None:
        words = " ".join(f"{axis}{value:.4f}" for axis, value in position.items())
        await self._command(f"G92 {words}")
        await self._sync_position()

    async def _sync_position(self) -> None:
        reply = await self._command("M114")
        report = next((line for line in reply if "X:" in line), "")
        logical = report.split(" Count ", 1)[0]
        for axis in self._axes:
            m = re.search(rf"\b{axis}:(-?\d+(?:\.\d+)?)", logical)
            if m:
                self._position[axis] = float(m.group(1))
        self._notify()

    # --- I/O -----------------------------------------------------------------------------

    async def rs485(self, packet: bytes) -> bytes | None:
        if self._settings.rs485 is None:
            return await super().rs485(packet)
        lines = await self._command(f"{self._settings.rs485} {packet.hex().upper()}")
        for line in lines:
            if line.startswith("rs485-reply:"):
                reply = line.removeprefix("rs485-reply:").strip()
                if reply == "TIMEOUT" or not reply:
                    return None
                try:
                    return bytes.fromhex(reply)
                except ValueError:
                    raise FirmwareError(f"unreadable feeder bus reply: {reply!r}") from None
        raise FirmwareError(f"{self._settings.rs485} gave no rs485-reply line")

    async def set_output(self, output_id: str, on: bool) -> None:
        spec = self._settings.outputs.get(output_id)
        if output_id not in self._outputs:
            raise KeyError(output_id)
        if spec is None:
            raise FirmwareError(f"output {output_id!r} has no G-code in the machine profile")
        for line in spec.on if on else spec.off:
            await self._command(line)
        self._outputs[output_id] = on
        self._notify()

    async def _poll_inputs(self) -> None:
        if not self._settings.inputs:
            return
        while self._connected:
            for input_id, spec in self._settings.inputs.items():
                try:
                    value = await self._read_input(input_id, spec)
                except MachineError as e:
                    log.debug("reading %s failed: %s", input_id, e)
                    continue
                if value is not None and value != self._inputs.get(input_id):
                    self._inputs[input_id] = value
                    self._notify()
            await asyncio.sleep(POLL_ACTIVE_S if any(self._outputs.values()) else POLL_IDLE_S)

    async def refresh_input(self, input_id: str) -> None:
        spec = self._settings.inputs.get(input_id)
        if spec is None:
            return
        value = await self._read_input(input_id, spec)
        if value is not None and value != self._inputs.get(input_id):
            self._inputs[input_id] = value
            self._notify()

    async def _read_input(self, input_id: str, spec: MarlinInput) -> float | bool | None:
        if not self._connected or self._halted:
            return None
        async with self._lock:
            reply: list[str] = []
            for line in spec.read:
                reply = await self._command_locked(line, self._timeout_s)
        pattern = self._input_patterns[input_id]
        for line in reply:
            m = pattern.search(line)
            if m:
                self._warn(f"input:{input_id}", None)
                raw = int(m.group("value"))
                if spec.sign_bits is not None and raw >= 1 << (spec.sign_bits - 1):
                    raw -= 1 << spec.sign_bits
                if input_id in self._digital:
                    return bool(raw)
                return raw * spec.scale
        command = spec.read[-1].split(";")[0].strip()
        if any("Unknown command" in line for line in reply):
            problem = f"the firmware does not understand {command!r}"
        else:
            problem = f"unexpected reply to {command!r}: {' '.join(reply) or '(none)'}"
        self._warn(
            f"input:{input_id}",
            f"{input_id}: {problem}. Check machine.marlin.variant in the config.",
        )
        return None

    # --- Serial protocol -----------------------------------------------------------------

    async def _command(self, line: str, timeout_s: float | None = None) -> list[str]:
        async with self._lock:
            return await self._command_locked(line, timeout_s or self._timeout_s)

    async def _command_locked(self, line: str, timeout_s: float) -> list[str]:
        if self._transport is None:
            raise NotConnectedError("the machine is not connected")
        if self._halted and self._connected:
            raise MotionInterruptedError("the controller is halted after an emergency stop")
        entry = _Pending(line, asyncio.get_running_loop().create_future())
        assert entry.future is not None
        self._pending.append(entry)
        log.debug(">> %s", line)
        try:
            await self._transport.write_line(line)
            return await asyncio.wait_for(asyncio.shield(entry.future), timeout_s)
        except TimeoutError:
            # If this ok arrives later it would be credited to the wrong command, and
            # positions and readings would be off by one from then on. Drop the link
            # instead; reconnecting starts clean.
            command = line.split(";")[0].strip()
            with contextlib.suppress(ValueError):
                self._pending.remove(entry)
            entry.future.cancel()  # nobody waits for it now
            self._warn("link", f"the controller did not answer {command!r}; reconnect it")
            await self._close()
            raise FirmwareError(f"no answer to {command!r}; the machine was disconnected") from None
        except TransportClosedError as e:
            raise NotConnectedError("the machine disconnected") from e

    async def _send_now(self, line: str, *, expects_ok: bool) -> None:
        """Write a line immediately, outside the one-at-a-time queue."""
        if self._transport is None:
            raise NotConnectedError("the machine is not connected")
        log.debug(">> %s (immediate)", line)
        if expects_ok:
            self._pending.append(_Pending(line, None))
        await self._transport.write_line(line)

    async def _read_lines(self, transport: LineTransport) -> None:
        try:
            while True:
                line = (await transport.read_line()).strip()
                if line:
                    self._on_line(line)
        except TransportClosedError:
            if self._transport is transport:
                log.warning("serial connection lost")
                self._transport = None
                await transport.close()
                self._fail_pending(NotConnectedError("the machine disconnected"))
                self._connected = False
                self._notify()

    def _on_line(self, line: str) -> None:
        log.debug("<< %s", line)
        if "kill() called" in line or line.startswith("Error:Printer halted"):
            self._halted = True
            self._fail_pending(MotionInterruptedError("the controller halted"))
            self._notify()
            return
        if not self._pending:
            log.debug("unsolicited: %s", line)
            return
        head = self._pending[0]
        if line.startswith("ok"):
            self._pending.popleft()
            if head.future is not None and not head.future.done():
                head.future.set_result(head.lines)
        elif line.startswith("Error:"):
            head.lines.append(line)
            if head.future is not None and not head.future.done():
                head.future.set_exception(FirmwareError(line.removeprefix("Error:").strip()))
        else:
            head.lines.append(line)

    def _fail_pending(self, error: Exception) -> None:
        while self._pending:
            entry = self._pending.popleft()
            if entry.future is not None and not entry.future.done():
                entry.future.set_exception(error)
