"""Calibrating the nozzle offsets (spec §7.7, line 6): where each nozzle really is from
the top camera.

A small round disc, such as one punched from paper, lies on the datum board. The top
camera finds it; a nozzle picks it up where the camera found it, turns it, and sets it
down; the camera finds it again. A nozzle exactly where its offset says turns the disc
about the disc's own center, and the disc stays put. A nozzle off by ``e`` turns it about
a point ``e`` from its center, which moves the disc by ``(I - R) e``, ``R`` being the
turn. Several turns give ``e`` by least squares, which averages out how unevenly the disc
is let go; what is left over says how well they agree.

What is found is the nozzle's turning axis, which is what placing needs: a part is
turned about it.

The height to pick the disc at is found by sniffing: each nozzle, with suction on, steps
down onto the datum board's center until the vacuum says the tip has sealed on it. The
disc's top is that plus its thickness. The nozzle never goes more than
``SNIFF_BELOW_MM`` below the profile's height for the board.

The same steps measure each nozzle's vacuum levels (line 9): open, before the sniff;
sealed on the board; and holding the disc. Its threshold, from then on, is halfway from
open to whichever of the other two is nearer.
"""

import asyncio
import math
from dataclasses import dataclass

import numpy as np

from picnicplace.calibration import datum_key, nozzle_key, vacuum_key
from picnicplace.machine.errors import InvalidTargetError, MachineError, VisionError
from picnicplace.machine.profile import Location, Nozzle, Point, VacuumLevels
from picnicplace.machine.service import MachineService
from picnicplace.machine.sniff import SniffError, VacuumMeasure, floor_message, sniff
from picnicplace.schemas import Operation

TURNS = (90.0, -90.0, 180.0)
"""The turns each nozzle gives the disc, in degrees."""
DISC_DIAMETER_MM = (2.0, 12.0)
"""Discs the finder takes, smallest to largest, when the request gives no diameter."""
SEARCH_FRACTION = 0.4
"""The disc is looked for within this fraction of the top camera's smaller field of view."""
CENTER_PASSES = 2
"""The camera is centered on the disc this many times before it is measured, so it is
measured in the middle of the image, where the camera's scale matters least."""
PICK_DWELL_S = 0.2
PLACE_DWELL_S = 0.2
VACUUM_TIMEOUT_S = 1.0
SPREAD_LIMIT_MM = 0.15
"""Turns that disagree by more than this (root mean square) mean the disc slipped."""
SNIFF_ABOVE_MM = 2.0
"""Sniffing starts this far above the profile's height for the datum board."""
SNIFF_BELOW_MM = 1.5
"""...and stops, with nothing found, this far below it (or at the bottom of travel)."""
PAPER_MM = 0.1
"""A paper disc's thickness, when the request gives none."""
HELD_SETTLE_S = 1.0
"""How long the vacuum is given to settle while holding the disc, for its level."""
VACUUM_GAP_MIN = 0.05
"""A nozzle whose reading holding the disc is less than this (as a fraction of its
sensor's range) from its reading open cannot tell a part held from none."""


class NozzleOffsetError(MachineError):
    """The nozzle offsets could not be measured."""


@dataclass(frozen=True)
class NozzleFix:
    nozzle: Nozzle
    x: float
    """The nozzle's offset from the top camera, as measured."""
    y: float
    spread: float
    """How well the turns agreed: the root mean square of what is left over, in mm."""


def _datum(machine: MachineService) -> Location:
    fiducial = next(
        (loc for loc in machine.profile.locations if loc.kind == "homing_fiducial"), None
    )
    if fiducial is None or fiducial.z is None:
        raise NozzleOffsetError(
            "the machine profile has no homing fiducial with a height (z): the disc lies "
            "on the datum board, and its height is found there"
        )
    return fiducial


async def sniff_datum(
    machine: MachineService, nozzle: Nozzle, measure: VacuumMeasure | None = None
) -> float:
    """Step the nozzle down onto the datum board's center fiducial, with suction on, until
    the vacuum seals: the board's top, as this nozzle reaches it, in heights shifted by its
    Z offset as it stands (add the offset for the raw height). ``measure``: fill in its
    vacuum levels, open and sealed."""
    fiducial = _datum(machine)
    assert fiducial.z is not None
    try:
        z = await sniff(
            machine,
            nozzle,
            fiducial.position,
            fiducial.z,
            SNIFF_ABOVE_MM,
            SNIFF_BELOW_MM,
            "the datum board",
            angle=0.0,
            measure=measure,
        )
    except SniffError as e:
        raise NozzleOffsetError(str(e)) from e
    if z is None:
        raise NozzleOffsetError(
            f"{floor_message(nozzle, 'the datum board', fiducial.z, SNIFF_BELOW_MM)}: check "
            "the tip seals (a tip that leaks never does), and the board's height in the profile"
        )
    return z


async def find_disc(machine: MachineService, near: Point, diameter: float | None) -> Point:
    """Center the top camera on the disc near a point, and return where it is."""
    camera = machine.top_camera()
    search = SEARCH_FRACTION * min(camera.field_of_view.x, camera.field_of_view.y)
    low, high = (diameter * 0.7, diameter * 1.4) if diameter else DISC_DIAMETER_MM
    at = near
    for _ in range(CENTER_PASSES):
        await machine.move_camera_to(at.x, at.y)
        found = await machine.locate_circle(low, high, search, diameter)
        if found is None:
            raise VisionError(
                f"the top camera cannot see the disc within {search:.0f} mm of "
                f"({at.x:.2f}, {at.y:.2f}): lay it flat on the datum board, center the "
                "camera on it, and check the light"
            )
        at = found
    return at


async def turn_disc(
    machine: MachineService,
    nozzle: Nozzle,
    at: Point,
    turn: float,
    height: float,
    level: float | None = None,
) -> float | None:
    """Pick the disc up at a point, turn it, and set it down there again. ``level``: the
    vacuum reading that means it is held, in place of the nozzle's threshold. Returns the
    reading while it was held."""
    await machine.move_nozzle_over(nozzle, at.x, at.y, 0.0)
    await machine.lower_nozzle(nozzle, height)
    await machine.suction(nozzle, True)
    await asyncio.sleep(PICK_DWELL_S)
    await machine.raise_nozzles()
    held = await machine.holds_part(nozzle, True, VACUUM_TIMEOUT_S, level)
    reading = await machine.settled_vacuum(nozzle, 0.0, HELD_SETTLE_S) if held else None
    if held is False:
        await machine.suction(nozzle, False)
        raise NozzleOffsetError(
            f"{nozzle.label} did not pick up the disc at height {height:g}: check the disc "
            "lies flat, and that the nozzle tip is fitted and clear"
        )
    await machine.move_nozzle_over(nozzle, at.x, at.y, turn)
    await machine.lower_nozzle(nozzle, height)
    await machine.suction(nozzle, False)
    await asyncio.sleep(PLACE_DWELL_S)
    await machine.raise_nozzles()
    return reading


def reading(value: float | None) -> str:
    """A vacuum reading for a message: large raw counts with thousands separators."""
    if value is None:
        return "none"
    return f"{value:,.0f}" if abs(value) >= 10_000 else f"{value:.3g}"


def vacuum_levels(
    machine: MachineService, nozzle: Nozzle, measure: VacuumMeasure, held: float | None
) -> VacuumLevels | None:
    """A nozzle's vacuum levels from its readings open, sealed, and holding the disc;
    None if its sensor gave no readings. Raises if it cannot tell a part held from none."""
    sensor = machine.vacuum_sensor(nozzle)
    if sensor is None or sensor.range is None:
        return None
    if measure.open is None or measure.sealed is None or held is None:
        return None
    o, s, h = measure.open, measure.sealed, held
    nearer = h if abs(h - o) <= abs(s - o) else s
    if sensor.range.fraction(nearer) - sensor.range.fraction(o) < VACUUM_GAP_MIN:
        raise NozzleOffsetError(
            f"{nozzle.label}'s vacuum barely changes between open ({reading(o)}) and holding "
            f"the disc ({reading(h)}): it cannot tell a part held from none. Check the tip "
            "seals, the tubing for leaks, and the pump"
        )
    return VacuumLevels(open=o, sealed=s, held=h, threshold=o + (nearer - o) / 2)


def solve(moves: list[tuple[float, Point]]) -> tuple[float, float, float]:
    """The nozzle's error (x, y) from how far each turn moved the disc, and the spread."""
    rows: list[list[float]] = []
    seen: list[float] = []
    for turn, moved in moves:
        a = math.radians(turn)
        c, s = math.cos(a), math.sin(a)
        rows += [[1 - c, s], [-s, 1 - c]]  # I - R
        seen += [moved.x, moved.y]
    a_matrix, b = np.array(rows), np.array(seen)
    e, *_ = np.linalg.lstsq(a_matrix, b, rcond=None)
    left = a_matrix @ e - b
    spread = math.sqrt(float(left @ left) / len(moves))
    return float(e[0]), float(e[1]), spread


async def calibrate_nozzle(
    machine: MachineService,
    nozzle: Nozzle,
    disc: Point,
    diameter: float | None,
    height: float,
    level: float | None = None,
) -> tuple[NozzleFix, Point, float | None]:
    """Measure one nozzle's offset with the disc found at ``disc``, taking the disc as
    held at vacuum reading ``level`` (default: the nozzle's threshold). Returns where the
    disc was left, and the vacuum reading holding it, too."""
    moves: list[tuple[float, Point]] = []
    readings: list[float] = []
    for turn in TURNS:
        reading = await turn_disc(machine, nozzle, disc, turn, height, level)
        if reading is not None:
            readings.append(reading)
        after = await find_disc(machine, disc, diameter)
        moves.append((turn, Point(x=after.x - disc.x, y=after.y - disc.y)))
        disc = after
    ex, ey, spread = solve(moves)
    if spread > SPREAD_LIMIT_MM:
        raise NozzleOffsetError(
            f"the disc moved unevenly under {nozzle.label} (the turns disagree by "
            f"{spread:.2f} mm): it may be slipping on the nozzle, or not lying flat. Try a "
            "lighter, flatter disc"
        )
    fix = NozzleFix(nozzle, nozzle.offset.x + ex, nozzle.offset.y + ey, spread)
    return fix, disc, sum(readings) / len(readings) if readings else None


def start_nozzle_offset_calibration(
    machine: MachineService,
    nozzle_ids: list[str] | None,
    diameter: float | None,
    thickness: float | None,
) -> Operation:
    """Measure the offsets of some nozzles (all, unless given) with a disc the top camera
    is over, keep them, and use them from now on."""
    nozzles = [machine.nozzle(i) for i in nozzle_ids] if nozzle_ids else machine.profile.nozzles
    if not nozzles:
        raise InvalidTargetError("no nozzles to calibrate")
    machine.top_camera()
    _datum(machine)
    disc_thickness = PAPER_MM if thickness is None else thickness

    async def work() -> str:
        camera = machine.top_camera()
        await machine.set_light(camera, True)
        try:
            return await measure()
        finally:
            await machine.raise_nozzles()
            await machine.set_light(camera, False)

    async def measure() -> str:
        disc = await find_disc(machine, machine.camera_position(), diameter)
        fixes: list[NozzleFix] = []
        heights: dict[str, float] = {}
        levels: dict[str, VacuumLevels] = {}
        for nozzle in nozzles:
            measure = VacuumMeasure()
            heights[nozzle.id] = await sniff_datum(machine, nozzle, measure)
            # Until its levels are known, the disc is held at a reading halfway to sealed.
            level = (
                measure.open + (measure.sealed - measure.open) / 2
                if measure.open is not None and measure.sealed is not None
                else None
            )
            fix, disc, held = await calibrate_nozzle(
                machine, nozzle, disc, diameter, heights[nozzle.id] + disc_thickness, level
            )
            fixes.append(fix)
            found = vacuum_levels(machine, nozzle, measure, held)
            if found is not None:
                levels[nozzle.id] = found

        previous = machine.calibration.get().lines.get("nozzle_offsets")
        values = dict(previous.values) if previous else {}
        parts: list[str] = []
        for fix in fixes:
            n = fix.nozzle
            design = machine.design_nozzle(n.id) or n
            off = math.hypot(fix.x - design.offset.x, fix.y - design.offset.y)
            values[nozzle_key(n.id, "x")] = fix.x
            values[nozzle_key(n.id, "y")] = fix.y
            values[f"off.{n.id}"] = off
            values[f"spread.{n.id}"] = fix.spread
            # Kept raw, so a new Z offset follows from it whatever the old one was.
            raw = heights[n.id] + n.z_offset
            values[datum_key(n.id)] = raw
            parts.append(
                f"{n.label}: ({fix.x:.3f}, {fix.y:.3f}), {off:.2f} mm from the profile's, "
                f"touching the datum board at Z {raw:.2f}"
            )
        spread = max(f.spread for f in fixes)
        message = f"{'; '.join(parts)}. The disc's turns agreed within {spread:.3f} mm."
        machine.calibration.record("nozzle_offsets", message, values)
        if levels:
            record_vacuum(machine, levels)
        machine.apply_calibration()
        return message

    return machine.start_task("nozzle_offsets", work)


def record_vacuum(machine: MachineService, levels: dict[str, VacuumLevels]) -> None:
    """Keep the nozzles' measured vacuum levels (line 9), with those of nozzles not
    measured this time."""
    previous = machine.calibration.get().lines.get("vacuum")
    values = dict(previous.values) if previous else {}
    for nozzle_id, found in levels.items():
        for name in ("open", "sealed", "held", "threshold"):
            values[vacuum_key(nozzle_id, name)] = getattr(found, name)
    parts: list[str] = []
    for nozzle in machine.profile.nozzles:
        v = {n: values.get(vacuum_key(nozzle.id, n)) for n in ("open", "held", "threshold")}
        if all(x is not None for x in v.values()):
            parts.append(
                f"{nozzle.label}: open {reading(v['open'])}, holding a part "
                f"{reading(v['held'])}, threshold {reading(v['threshold'])}"
            )
    unit = next((i.unit for i in machine.profile.inputs if i.role == "vacuum" and i.unit), None)
    message = "; ".join(parts) + (f" ({unit})." if unit else ".")
    machine.calibration.record("vacuum", message, values)
