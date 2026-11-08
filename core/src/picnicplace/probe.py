"""``picnicplace-probe``: collect what PicnicPlace needs to know about a machine, safely.

Run it once when bringing up a new machine or firmware. It never sends a motion command.
It records every line the firmware sends into a transcript, then prints a summary:

- firmware identity and capabilities (``M115``), including the emergency parser
- endstop states (``M119``), stored settings (``M503``), and the position (``M114``)
- vacuum sensor reads in every firmware variant the machine profile knows, to show which
  variant this firmware speaks
- with ``--with-pumps``: vacuum readings with each nozzle's pump and valve on briefly
- on Linux: the cameras under /dev/v4l/by-id and their formats (if v4l2-ctl is installed)

Share the transcript file when asking for help with a machine.
"""

import argparse
import asyncio
import contextlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from picnicplace.machine.marlin.settings import MarlinSettings
from picnicplace.machine.marlin.transport import LineTransport, SerialTransport
from picnicplace.machine.profile import MachineProfile, load_profile

REPLY_TIMEOUT_S = 5.0
SUCTION_SETTLE_S = 1.0

MOTION_WORDS = {"G0", "G1", "G2", "G3", "G28", "G29", "G30", "M410", "M112"}
"""Commands the probe refuses to send, as a guard against profile mistakes."""


@dataclass
class Exchange:
    command: str
    replies: list[str]
    ok: bool
    """Whether the firmware confirmed the command with ``ok`` in time."""


@dataclass
class ProbeReport:
    transcript: list[str] = field(default_factory=list[str])
    firmware: str | None = None
    machine_type: str | None = None
    capabilities: dict[str, str] = field(default_factory=dict[str, str])
    endstops: list[str] = field(default_factory=list[str])
    position: str | None = None
    vacuum: dict[str, dict[str, float | str]] = field(
        default_factory=dict[str, dict[str, float | str]]
    )
    """Variant name to {input ID: value, or the reason there is none}."""
    suction: dict[str, dict[str, float | str]] = field(
        default_factory=dict[str, dict[str, float | str]]
    )
    cameras: list[str] = field(default_factory=list[str])

    def log(self, line: str) -> None:
        self.transcript.append(f"{time.strftime('%H:%M:%S')} {line}")


class Prober:
    def __init__(self, transport: LineTransport, report: ProbeReport) -> None:
        self._transport = transport
        self._report = report
        self._lines: asyncio.Queue[str] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        with contextlib.suppress(ConnectionError):
            while True:
                line = await self._transport.read_line()
                self._report.log(f"<< {line}")
                await self._lines.put(line.strip())

    async def drain(self, quiet_s: float = 0.5) -> list[str]:
        """Collect anything the firmware sends on its own (boot messages)."""
        lines: list[str] = []
        with contextlib.suppress(TimeoutError):
            while True:
                lines.append(await asyncio.wait_for(self._lines.get(), quiet_s))
        return lines

    async def send(self, command: str) -> Exchange:
        word = command.split(";", 1)[0].split()[0].upper() if command.strip() else ""
        if word in MOTION_WORDS:
            raise ValueError(f"the probe does not send motion commands ({command!r})")
        self._report.log(f">> {command}")
        await self._transport.write_line(command)
        replies: list[str] = []
        try:
            while True:
                line = await asyncio.wait_for(self._lines.get(), REPLY_TIMEOUT_S)
                if line.startswith("ok"):
                    return Exchange(command, replies, ok=True)
                replies.append(line)
        except TimeoutError:
            self._report.log(f"!! no ok for {command!r} within {REPLY_TIMEOUT_S:.0f} s")
            return Exchange(command, replies, ok=False)

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader
        await self._transport.close()


async def run_probe(
    transport: LineTransport,
    profile: MachineProfile,
    *,
    connect_wait_s: float = 3.0,
    with_pumps: bool = False,
) -> ProbeReport:
    """Probe a machine over an open transport. Never moves it."""
    report = ProbeReport()
    settings = profile.marlin or MarlinSettings()
    prober = Prober(transport, report)
    try:
        if connect_wait_s > 0:
            await asyncio.sleep(connect_wait_s)
        await prober.drain()

        info = await prober.send("M115")
        for line in info.replies:
            if "FIRMWARE_NAME:" in line:
                name = re.search(r"FIRMWARE_NAME:(.*?)(?: [A-Z_]+:|$)", line)
                machine = re.search(r"MACHINE_TYPE:(\S+)", line)
                report.firmware = name.group(1).strip() if name else line
                report.machine_type = machine.group(1) if machine else None
            elif line.startswith("Cap:"):
                key, _, value = line.removeprefix("Cap:").partition(":")
                report.capabilities[key] = value

        report.endstops = (await prober.send("M119")).replies
        await prober.send("M503")
        position = await prober.send("M114")
        report.position = next((r for r in position.replies if "X:" in r), None)

        # The connect sequence configures sensors (no motion; checked by send()).
        for command in settings.connect:
            await prober.send(command)

        variants: list[str | None] = list(settings.variants) or [None]
        for name in variants:
            resolved = settings.resolved(name)
            label = name or "default"
            report.vacuum[label] = await _read_inputs(prober, resolved)

        if with_pumps:
            for nozzle in profile.nozzles:
                outputs = [o for o in (nozzle.pump_output, nozzle.valve_output) if o]
                specs = [settings.outputs[o] for o in outputs if o in settings.outputs]
                if not specs or nozzle.vacuum_input is None:
                    continue
                for spec in specs:
                    for command in spec.on:
                        await prober.send(command)
                await asyncio.sleep(SUCTION_SETTLE_S)
                for name in variants:
                    resolved = settings.resolved(name)
                    values = await _read_inputs(prober, resolved, only=nozzle.vacuum_input)
                    report.suction.setdefault(name or "default", {}).update(values)
                for spec in specs:
                    for command in spec.off:
                        await prober.send(command)
    finally:
        await prober.close()
    return report


async def _read_inputs(
    prober: Prober, settings: MarlinSettings, only: str | None = None
) -> dict[str, float | str]:
    values: dict[str, float | str] = {}
    for input_id, spec in settings.inputs.items():
        if only is not None and input_id != only:
            continue
        exchange: Exchange | None = None
        for command in spec.read:
            exchange = await prober.send(command)
        if exchange is None or not exchange.ok:
            values[input_id] = "no answer"
            continue
        joined = " ".join(exchange.replies)
        match = re.search(spec.pattern, joined)
        if match is None:
            values[input_id] = f"unexpected reply: {joined or '(none)'}"
            continue
        raw = int(match.group("value"))
        if spec.sign_bits is not None and raw >= 1 << (spec.sign_bits - 1):
            raw -= 1 << spec.sign_bits
        values[input_id] = raw * spec.scale
    return values


def list_cameras(report: ProbeReport) -> None:
    by_id = Path("/dev/v4l/by-id")
    if not by_id.is_dir():
        report.cameras.append("no /dev/v4l/by-id (not Linux, or no cameras)")
        return
    v4l2 = shutil.which("v4l2-ctl")
    for path in sorted(by_id.iterdir()):
        report.cameras.append(f"{path} -> {path.resolve()}")
        if v4l2 is None:
            continue
        result = subprocess.run(
            [v4l2, "--list-formats-ext", "-d", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        report.log(f"$ v4l2-ctl --list-formats-ext -d {path}")
        report.transcript.extend(f"    {line}" for line in result.stdout.splitlines())
        formats = sorted(set(re.findall(r"'(\w+)'", result.stdout)))
        report.cameras.append(f"    formats: {', '.join(formats) or 'unknown'}")
    if v4l2 is None:
        report.cameras.append("install v4l-utils to list camera formats")


def summarize(report: ProbeReport, profile: MachineProfile) -> str:
    lines = [f"Firmware:      {report.firmware or 'no answer to M115'}"]
    lines.append(f"Machine type:  {report.machine_type or 'unknown'}")
    expected = profile.marlin.expected_machine_type if profile.marlin else None
    if expected and report.machine_type != expected:
        lines.append(f"  ! expected {expected!r} for profile {profile.id!r}")
    parser = report.capabilities.get("EMERGENCY_PARSER")
    lines.append(
        "Emergency parser: "
        + {"1": "yes (STOP acts mid-move)", "0": "NO: STOP waits for the current move"}.get(
            parser or "", "not reported"
        )
    )
    lines.append(f"Position:      {report.position or 'unknown'}")
    lines.append("Endstops:")
    lines.extend(f"  {e}" for e in report.endstops or ["(no reply)"])
    for title, table in (("Vacuum at rest", report.vacuum), ("Vacuum, suction on", report.suction)):
        if not table:
            continue
        lines.append(f"{title}, by firmware variant:")
        for variant, values in table.items():
            shown = ", ".join(
                f"{k}={v:,.0f}" if isinstance(v, float) else f"{k}: {v}" for k, v in values.items()
            )
            lines.append(f"  {variant:14} {shown}")
    if report.cameras:
        lines.append("Cameras:")
        lines.extend(f"  {c}" for c in report.cameras)
    return "\n".join(lines)


CHECKLIST = """
First motion checklist
  1. Keep a hand on the machine's physical E-stop.
  2. Start picnicd with machine.speed_scale = 0.25 (slow moves).
  3. Home, and watch that every axis reaches its switch.
  4. Jog X and Y by 1 mm, then 10 mm; check the direction matches the arrows.
  5. Jog each nozzle down 1 mm and back up; check N1 and N2 move the right way.
  6. Turn each light, pump, and valve on and off from the head inset.
  7. Press STOP during a slow 50 mm move; note whether it stops at once, and what it
     takes to reset (Reset E-stop, the board's reset button, or a power cycle).
"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="picnicplace-probe",
        description="Collect firmware and camera details from a machine, without moving it.",
    )
    parser.add_argument("--serial-port", required=True, help="such as /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--profile", default="lumenpnp-v4.1", help="machine profile ID or path")
    parser.add_argument("--connect-wait", type=float, default=3.0, help="seconds after opening")
    parser.add_argument(
        "--with-pumps",
        action="store_true",
        help="also switch each nozzle's pump and valve on for about a second to read vacuum",
    )
    parser.add_argument("-o", "--output", type=Path, help="transcript file")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    output: Path = args.output or Path(f"picnicplace-probe-{time.strftime('%Y%m%d-%H%M%S')}.txt")

    async def go() -> ProbeReport:
        transport = await SerialTransport.open(args.serial_port, args.baud)
        return await run_probe(
            transport, profile, connect_wait_s=args.connect_wait, with_pumps=args.with_pumps
        )

    try:
        report = asyncio.run(go())
    except OSError as e:
        sys.exit(f"picnicplace-probe: cannot open {args.serial_port}: {e.strerror or e}")
    list_cameras(report)
    summary = summarize(report, profile)
    output.write_text(summary + "\n\nTranscript\n" + "\n".join(report.transcript) + "\n")
    print(summary)
    print(CHECKLIST)
    print(f"Transcript saved to {output}")


if __name__ == "__main__":
    main()
