"""A fake LumenPnP Marlin firmware, for developing and testing the Marlin driver without a
machine.

It answers the way a real LumenPnP v4 does in a captured session (see
docs/hardware/lumenpnp-v4.md): ``ok`` flow control, ``M114`` position reports, ``G28``
homing that leaves A and B alone, I2C vacuum sensor reads behind a multiplexer in both the
stock 8-bit and patched 24-bit forms, NeoPixel lights, RS-485 (always ``TIMEOUT``: no
feeders), and dwell notifications. Motion takes simulated time, so ``M400`` really waits.

It also models what the log could not show, as best guesses:

- ``M112`` halts the firmware (``Error:Printer halted. kill() called!``) and it stops
  answering. With ``auto_reboot`` (the default) it restarts after a moment, as if someone
  pressed reset; otherwise call ``hardware_reset``.
- ``M410`` stops motion at once and keeps running, like Marlin's quick stop.
- ``M112`` and ``M410`` are handled as soon as they arrive, even while another command is
  running, like Marlin's emergency parser.

Run it on a pseudo-terminal to talk to it like a serial port::

    python -m picnicplace.machine.marlin.fake
"""

import argparse
import asyncio
import contextlib
import logging
import math
import os
import re
import termios
import tty
from collections.abc import Callable
from dataclasses import dataclass, field

from picnicplace.machine.marlin.transport import LineTransport, TransportClosedError

log = logging.getLogger(__name__)

AXES: tuple[str, ...] = ("X", "Y", "Z", "A", "B")
HOMED_AXES: tuple[str, ...] = ("X", "Y", "Z")
STEPS_PER_UNIT = {"X": 80.0, "Y": 80.0, "Z": 40.0, "A": 4.44, "B": 4.44}

FIRMWARE = (
    "FIRMWARE_NAME:Marlin bugfix-2.1.x (fake) SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin "
    "PROTOCOL_VERSION:1.0 MACHINE_TYPE:LumenPnP KINEMATICS:Cartesian EXTRUDER_COUNT:0 "
    "AXIS_COUNT:5"
)
HALTED = "Error:Printer halted. kill() called!"

MUX_ADDRESS = 112
SENSOR_ADDRESS = 109
PRESSURE_REGISTER = 6


@dataclass
class VacuumChannel:
    """One nozzle's vacuum: its fan outputs, how its Z moves, and the sensor reading."""

    pump_fan: int
    valve_fan: int
    z_sign: int
    value: float = 900.0
    held: bool = False


@dataclass
class _Segment:
    start: dict[str, float]
    end: dict[str, float]
    t0: float
    t1: float


@dataclass
class FakeSettings:
    time_scale: float = 1.0
    home_duration_s: float = 5.0
    reboot_delay_s: float = 1.0
    auto_reboot: bool = True
    pick_depth: float = 20.0
    """A nozzle this far down with suction on picks up a part."""
    vacuum_rest: float = 900.0
    vacuum_open: float = -1_500_000.0
    vacuum_held: float = -2_200_000.0
    vacuum_tau_s: float = 0.15
    channels: dict[int, VacuumChannel] = field(
        default_factory=lambda: {
            1: VacuumChannel(pump_fan=1, valve_fan=0, z_sign=1),
            2: VacuumChannel(pump_fan=3, valve_fan=2, z_sign=-1),
        }
    )


class FakeMarlin:
    """The firmware state machine. Feed it lines with ``receive``; it calls ``send``."""

    def __init__(self, send: Callable[[str], None], settings: FakeSettings | None = None):
        self._send = send
        self.settings = settings or FakeSettings()
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._reboot: asyncio.Task[None] | None = None
        self._quickstop = asyncio.Event()
        self._boot()

    # --- Lifecycle -----------------------------------------------------------------------

    def _boot(self) -> None:
        self.halted = False
        self.target: dict[str, float] = dict.fromkeys(AXES, 0.0)
        self._segments: list[_Segment] = []
        self.fans: dict[int, int] = {}
        self.leds: dict[int, int] = {}
        self.absolute = True
        self._mux_channel = 0
        self._i2c_address: int | None = None
        self._i2c_bytes: list[int] = []
        self._register = 0
        self._vacuum_time = self._now()
        for ch in self.settings.channels.values():
            ch.value, ch.held = self.settings.vacuum_rest, False
        self.received: list[str] = []
        """Every command line received, for tests."""

    def start(self) -> None:
        self._worker = asyncio.create_task(self._work())

    async def stop(self) -> None:
        for task in (self._worker, self._reboot):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def hardware_reset(self) -> None:
        """Press the reset button: reboot with everything off and positions zeroed."""
        while not self._queue.empty():
            self._queue.get_nowait()
        self._boot()
        self._send("start")
        self._send("echo:Marlin bugfix-2.1.x (fake)")

    def _now(self) -> float:
        return self._loop.time() * self.settings.time_scale

    async def _sleep(self, seconds: float) -> bool:
        """Sleep simulated seconds. False if a quick stop or halt cut it short."""
        try:
            await asyncio.wait_for(self._quickstop.wait(), seconds / self.settings.time_scale)
        except TimeoutError:
            return True
        return False

    # --- Input ---------------------------------------------------------------------------

    def receive(self, line: str) -> None:
        """A line from the host. Emergency commands act at once; others queue."""
        code = line.split(";", 1)[0].strip()
        if not code:
            return
        if self.halted:
            return
        word = code.split()[0].upper()
        if word == "M112":
            self._halt()
        else:
            if word == "M410":
                # Acted on now; the line still runs through the queue and gets its ok in
                # order, as with Marlin's emergency parser.
                self._quick_stop()
            self._queue.put_nowait(code)

    def _halt(self) -> None:
        self.halted = True
        self._freeze_motion()
        self.fans.clear()
        self.leds.clear()
        self._quickstop.set()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._send(HALTED)
        if self.settings.auto_reboot:
            self._reboot = asyncio.create_task(self._reboot_later())

    async def _reboot_later(self) -> None:
        await asyncio.sleep(self.settings.reboot_delay_s / self.settings.time_scale)
        self.hardware_reset()

    def _quick_stop(self) -> None:
        self._freeze_motion()
        self._quickstop.set()

    def _freeze_motion(self) -> None:
        self.target = self.actual()
        self._segments.clear()

    async def _work(self) -> None:
        while True:
            line = await self._queue.get()
            if self.halted:
                continue
            if not line.upper().startswith("M410"):
                self._quickstop.clear()
            self.received.append(line)
            try:
                replies = await self._execute(line)
            except Exception as e:  # a bug in the fake, not a firmware behavior
                log.exception("fake Marlin failed on %r", line)
                replies = [f"echo:fake error: {e}"]
            if self.halted:
                continue
            for reply in replies:
                self._send(reply)
            self._send("ok")

    # --- Commands ------------------------------------------------------------------------

    async def _execute(self, line: str) -> list[str]:
        word, args = _parse(line)
        handler = getattr(self, f"_cmd_{word.lower()}", None)
        if handler is None:
            return [f'echo:Unknown command: "{line}"']
        return await handler(line, args)

    async def _cmd_m115(self, _line: str, _args: dict[str, str]) -> list[str]:
        return [FIRMWARE, "Cap:EMERGENCY_PARSER:1"]

    async def _cmd_g90(self, _line: str, _args: dict[str, str]) -> list[str]:
        self.absolute = True
        return []

    async def _cmd_g91(self, _line: str, _args: dict[str, str]) -> list[str]:
        self.absolute = False
        return []

    async def _ok(self, _line: str, _args: dict[str, str]) -> list[str]:
        return []

    _cmd_m113 = _cmd_m201 = _cmd_m203 = _cmd_m204 = _cmd_m205 = _cmd_m410 = _ok

    async def _cmd_g0(self, _line: str, args: dict[str, str]) -> list[str]:
        start = dict(self.target)
        end = dict(start)
        for axis in AXES:
            if axis in args:
                value = float(args[axis])
                end[axis] = value if self.absolute else start[axis] + value
        feed = float(args.get("F", "6000")) / 60.0  # mm/min to mm/s
        distance = math.sqrt(sum((end[a] - start[a]) ** 2 for a in AXES))
        t0 = max(self._now(), self._segments[-1].t1 if self._segments else 0.0)
        self._segments.append(_Segment(start, end, t0, t0 + distance / max(feed, 1e-6)))
        self.target = end
        return []

    _cmd_g1 = _cmd_g0

    async def _cmd_m400(self, _line: str, _args: dict[str, str]) -> list[str]:
        if self._segments:
            await self._sleep(max(0.0, self._segments[-1].t1 - self._now()))
            self._segments.clear()
        return []

    async def _cmd_g28(self, _line: str, args: dict[str, str]) -> list[str]:
        axes = [a for a in HOMED_AXES if a in args] or list(HOMED_AXES)
        if not await self._sleep(self.settings.home_duration_s):
            return []
        for axis in axes:
            self.target[axis] = 0.0
        self._segments.clear()
        return [self._position_report()]

    async def _cmd_g92(self, _line: str, args: dict[str, str]) -> list[str]:
        for axis in AXES:
            if axis in args:
                self.target[axis] = float(args[axis])
        self._segments.clear()
        return []

    async def _cmd_g4(self, _line: str, args: dict[str, str]) -> list[str]:
        seconds = float(args["P"]) / 1000 if "P" in args else float(args.get("S", "0"))
        await self._sleep(seconds)
        return ["//action:notification Sleep..."]

    async def _cmd_m114(self, _line: str, _args: dict[str, str]) -> list[str]:
        return [self._position_report()]

    async def _cmd_m119(self, _line: str, _args: dict[str, str]) -> list[str]:
        # Marlin's format. Which switches a LumenPnP reports is not known yet.
        at_home = {a: abs(self.target[a]) < 1e-6 for a in HOMED_AXES}
        return ["Reporting endstop status"] + [
            f"{a.lower()}_min: {'TRIGGERED' if at_home[a] else 'open'}" for a in HOMED_AXES
        ]

    async def _cmd_m503(self, _line: str, _args: dict[str, str]) -> list[str]:
        steps = " ".join(f"{a}{STEPS_PER_UNIT[a]:.2f}" for a in AXES)
        return [
            "echo:; Steps per unit:",
            f"echo:  M92 {steps}",
            "echo:; Max feedrates (units/s):",
            "echo:  M203 X2000.00 Y2000.00 Z600.00 A60000.00 B60000.00",
        ]

    async def _cmd_m106(self, _line: str, args: dict[str, str]) -> list[str]:
        self._update_vacuum()
        self.fans[int(args.get("P", "0"))] = int(float(args.get("S", "255")))
        self._update_vacuum()
        return []

    async def _cmd_m107(self, _line: str, args: dict[str, str]) -> list[str]:
        self._update_vacuum()
        self.fans[int(args.get("P", "0"))] = 0
        self._update_vacuum()
        return []

    async def _cmd_m150(self, _line: str, args: dict[str, str]) -> list[str]:
        self.leds[int(args.get("S", "0"))] = int(args.get("P", "255"))
        return []

    async def _cmd_m485(self, _line: str, _args: dict[str, str]) -> list[str]:
        return ["rs485-reply: TIMEOUT"]

    async def _cmd_m260(self, _line: str, args: dict[str, str]) -> list[str]:
        # Arguments apply in order: A sets the address, B queues a byte, S1 sends.
        if "A" in args:
            self._i2c_address = int(args["A"])
            self._i2c_bytes = []
        if "B" in args:
            self._i2c_bytes.append(int(args["B"]))
        if args.get("S") == "1":
            self._i2c_send()
        return []

    def _i2c_send(self) -> None:
        data, self._i2c_bytes = self._i2c_bytes, []
        if self._i2c_address == MUX_ADDRESS and data:
            self._mux_channel = data[0]
        elif self._i2c_address == SENSOR_ADDRESS and data:
            self._register = data[0]

    async def _cmd_m261(self, _line: str, args: dict[str, str]) -> list[str]:
        address = int(args.get("A", "0"))
        count = int(args.get("B", "1"))
        style = int(args.get("S", "0"))
        value = 0
        if address == SENSOR_ADDRESS and self._register == PRESSURE_REGISTER:
            value = self._sensor_counts(self._mux_channel)
        if count >= 3 and style == 4:
            data = str(value)  # the patched firmware: signed 24-bit decimal
        else:
            raw = (value & 0xFFFFFF).to_bytes(3, "big")[:count]
            data = " ".join(str(b) for b in raw) if style == 2 else raw.hex()
        return [f"echo:i2c-reply: from:{address} bytes:{count} data:{data}"]

    # --- Models --------------------------------------------------------------------------

    def actual(self) -> dict[str, float]:
        """Where the axes really are, part way along the current move."""
        now = self._now()
        for seg in self._segments:
            if now < seg.t1:
                if now <= seg.t0:
                    return dict(seg.start)
                frac = (now - seg.t0) / (seg.t1 - seg.t0)
                return {a: seg.start[a] + (seg.end[a] - seg.start[a]) * frac for a in AXES}
        return dict(self.target)

    def _position_report(self) -> str:
        pos = " ".join(f"{a}:{self.target[a]:.2f}" for a in AXES)
        actual = self.actual()
        counts = " ".join(f"{a}:{round(actual[a] * STEPS_PER_UNIT[a])}" for a in AXES)
        return f"{pos} Count {counts}"

    def _suction(self, ch: VacuumChannel) -> bool:
        return self.fans.get(ch.pump_fan, 0) > 0 and self.fans.get(ch.valve_fan, 0) > 0

    def _update_vacuum(self) -> None:
        """Advance every channel's reading to now, and apply the pick and drop rules."""
        now = self._now()
        dt = now - self._vacuum_time
        self._vacuum_time = now
        z = self.actual()["Z"]
        s = self.settings
        for ch in s.channels.values():
            if not self._suction(ch):
                ch.held = False
            elif ch.z_sign * z <= -s.pick_depth:
                ch.held = True
            if not self._suction(ch):
                goal = s.vacuum_rest
            else:
                goal = s.vacuum_held if ch.held else s.vacuum_open
            ch.value = goal + (ch.value - goal) * math.exp(-dt / s.vacuum_tau_s)

    def _sensor_counts(self, channel: int) -> int:
        self._update_vacuum()
        ch = self.settings.channels.get(channel)
        return round(ch.value) if ch else 0


_WORD = re.compile(r"([A-Za-z])\s*([-+]?[0-9.]*)")


def _parse(line: str) -> tuple[str, dict[str, str]]:
    """Split a G-code line into its command word and arguments, in order."""
    parts = _WORD.findall(line)
    if not parts:
        return "", {}
    word = parts[0][0].upper() + parts[0][1]
    return word, {letter.upper(): value for letter, value in parts[1:]}


# --- Connecting the fake to a transport -----------------------------------------------


class FakeMarlinLink:
    """Runs a FakeMarlin on the firmware end of a line transport."""

    def __init__(self, transport: LineTransport, settings: FakeSettings | None = None):
        self._transport = transport
        self._pending: set[asyncio.Task[None]] = set()
        self.firmware = FakeMarlin(self._send, settings)
        self._reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.firmware.start()
        self._reader = asyncio.create_task(self._read())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        await self.firmware.stop()
        await self._transport.close()

    def _send(self, line: str) -> None:
        task = asyncio.ensure_future(self._write(line))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _write(self, line: str) -> None:
        with contextlib.suppress(TransportClosedError):
            await self._transport.write_line(line)

    async def _read(self) -> None:
        with contextlib.suppress(TransportClosedError):
            while True:
                self.firmware.receive(await self._transport.read_line())


class FakeMarlinMemory:
    """A fake firmware reached through in-memory links. Each ``open`` is a new connection,
    like reopening a serial port; the firmware and its state carry over."""

    def __init__(self, settings: FakeSettings | None = None) -> None:
        self._settings = settings
        self._firmware: FakeMarlin | None = None
        self._device: LineTransport | None = None
        self._reader: asyncio.Task[None] | None = None
        self._writes: set[asyncio.Task[None]] = set()

    @property
    def firmware(self) -> FakeMarlin:
        assert self._firmware is not None, "open() first"
        return self._firmware

    async def open(self) -> LineTransport:
        from picnicplace.machine.marlin.transport import memory_pair

        if self._firmware is None:
            self._firmware = FakeMarlin(self._send, self._settings)
            self._firmware.start()
        await self._detach()
        host, device = memory_pair()
        self._device = device
        self._reader = asyncio.create_task(self._read(device))
        return host

    async def _detach(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._device is not None:
            await self._device.close()
        self._reader = self._device = None

    def _send(self, line: str) -> None:
        device = self._device
        if device is None:
            return
        task = asyncio.ensure_future(self._write(device, line))
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    @staticmethod
    async def _write(device: LineTransport, line: str) -> None:
        with contextlib.suppress(TransportClosedError):
            await device.write_line(line)

    async def _read(self, device: LineTransport) -> None:
        with contextlib.suppress(TransportClosedError):
            while True:
                self.firmware.receive(await device.read_line())

    async def stop(self) -> None:
        await self._detach()
        if self._firmware is not None:
            await self._firmware.stop()


class FakeMarlinPty:
    """A FakeMarlin behind a pseudo-terminal: open ``path`` like a serial port."""

    def __init__(self, settings: FakeSettings | None = None) -> None:
        self._settings = settings
        self._master: int | None = None
        self._slave: int | None = None
        self.path = ""
        self._link: FakeMarlinLink | None = None

    async def start(self) -> str:
        from picnicplace.machine.marlin.transport import SerialTransport

        master, slave = os.openpty()
        tty.setraw(slave)
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        os.set_blocking(master, False)
        self._master, self._slave = master, slave
        self.path = os.ttyname(slave)
        # The slave end stays open here so the pty survives the driver reconnecting.
        self._link = FakeMarlinLink(SerialTransport(master), self._settings)
        self._link.start()
        return self.path

    @property
    def firmware(self) -> FakeMarlin:
        assert self._link is not None
        return self._link.firmware

    async def stop(self) -> None:
        if self._link is not None:
            await self._link.stop()  # closes the master
        if self._slave is not None:
            with contextlib.suppress(OSError):
                os.close(self._slave)
            self._slave = None


async def _main(args: argparse.Namespace) -> None:
    fake = FakeMarlinPty(FakeSettings(time_scale=args.time_scale))
    path = await fake.start()
    print(f"Fake LumenPnP Marlin on {path} (Ctrl-C to stop)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await fake.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a fake LumenPnP Marlin on a pty.")
    parser.add_argument("--time-scale", type=float, default=1.0)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
