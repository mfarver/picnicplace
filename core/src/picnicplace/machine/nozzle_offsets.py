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
down onto the disc itself until the vacuum says the tip has sealed on it. Paper seals a
tip the moment it touches; bare datum board does not. Its printing and the mask opening
around its dot let a tip leak until it is pressed well into its spring (seen on a real
LumenPnP: 2.5 mm for an N24 tip, more than 5 mm for an N75). The datum board's top, for
the nozzle's Z offset, is the disc's top less its thickness. A seal too weak to be a
touch is refused, and the nozzle never goes more than ``SNIFF_BELOW_MM`` below the
profile's height for the board. While a nozzle is measured, its old Z offset is set
aside, so a wrong one cannot move the search.

The same steps measure each nozzle's vacuum levels (line 9): open, before the sniff;
sealed on the disc; and holding the disc. Its threshold, from then on, is halfway from
open to whichever of the other two is nearer.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from picnicplace.calibration import datum_key, nozzle_key, vacuum_key
from picnicplace.machine.errors import InvalidTargetError, MachineError, VisionError
from picnicplace.machine.profile import Location, Nozzle, NozzleTip, Point, VacuumLevels
from picnicplace.machine.seat_check import keep_references, near_pad
from picnicplace.machine.service import APPROACH_SPEED, MachineService, RoundMark
from picnicplace.machine.sniff import SniffError, VacuumMeasure, floor_message, sniff
from picnicplace.schemas import Operation, TipChangeNozzle

TURNS = (90.0, -90.0, 180.0)
"""The turns each nozzle gives the disc, in degrees."""
DISC_DIAMETER_MM = (4.0, 12.0)
"""Discs the finder takes, smallest to largest, when the request gives no diameter. A
hole punch makes about 6 mm; the datum board's own fiducials, rings included, are
smaller than 4 mm."""
SEARCH_FRACTION = 0.4
"""The disc is looked for within this fraction of the top camera's smaller field of view."""
CENTER_PASSES = 2
"""The camera is centered on the disc this many times before it is measured, so it is
measured in the middle of the image, where the camera's scale matters least."""
PICK_DWELL_S = 0.2
"""How long the nozzle stays down once the vacuum says it holds the disc."""
PICK_SEAL_TIMEOUT_S = 2.0
"""How long the nozzle, down on the disc, waits for the vacuum to say it holds it."""
LIFT_SLOW_MM = 1.0
"""The disc is lifted this far at ``APPROACH_SPEED`` before the nozzle rises at speed."""
PLACE_DWELL_S = 0.2
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


async def sniff_disc(
    machine: MachineService,
    nozzle: Nozzle,
    disc: Point,
    thickness: float,
    measure: VacuumMeasure,
    what: str = "the disc",
) -> float:
    """Step the nozzle down onto the disc lying on the datum board, with suction on, until
    the vacuum seals: the board's top under it, as this nozzle reaches it (the disc's top
    less its thickness), in heights shifted by its Z offset as it stands (add the offset
    for the raw height). Fills in its vacuum levels, open and sealed."""
    fiducial = _datum(machine)
    assert fiducial.z is not None
    expected = fiducial.z + thickness
    try:
        z = await sniff(
            machine,
            nozzle,
            disc,
            expected,
            SNIFF_ABOVE_MM,
            SNIFF_BELOW_MM,
            what,
            angle=0.0,
            measure=measure,
        )
    except SniffError as e:
        raise NozzleOffsetError(str(e)) from e
    if z is None:
        raise NozzleOffsetError(
            f"{floor_message(nozzle, what, expected, SNIFF_BELOW_MM)}: check the tip "
            "seals (a tip that leaks never does), and the datum board's height in the profile"
        )
    return z - thickness


def check_seal(
    nozzle: Nozzle,
    measure: VacuumMeasure,
    held: float | None,
    z: float,
    what: str = "the disc",
) -> None:
    """Refuse a seal on the disc much weaker than holding the disc: a leak pressed shut as
    the tip went on down, not a touch, and its height would be too low."""
    if measure.open is None or measure.sealed is None or held is None:
        return
    holding = abs(held - measure.open)
    if holding <= 0:
        return
    rise = abs(measure.sealed - measure.open) / holding
    if rise < SEAL_MIN_FRACTION:
        raise NozzleOffsetError(
            f"{nozzle.label} sealed only weakly on {what} at Z {z:.2f} (the vacuum rose "
            f"{rise:.0%} as far as holding {what}, less than {SEAL_MIN_FRACTION:.0%}): a "
            "leak pressed shut, not a touch, so the height would be too low. Check the tip "
            f"is pushed fully on, and that {what} lies flat under it"
        )


async def find_disc(machine: MachineService, near: Point, diameter: float | None) -> Point:
    """Center the top camera on the disc near a point, and return where it is."""
    camera = machine.top_camera()
    search = SEARCH_FRACTION * min(camera.field_of_view.x, camera.field_of_view.y)
    low, high = (diameter * 0.7, diameter * 1.4) if diameter else DISC_DIAMETER_MM
    at = near
    for _ in range(CENTER_PASSES):
        await machine.move_camera_to(at.x, at.y)
        found = await machine.locate_circle(low, high, search, diameter, through_lines=True)
        if found is None:
            raise VisionError(
                f"the top camera cannot see the disc within {search:.0f} mm of "
                f"({at.x:.2f}, {at.y:.2f}): lay it flat on the datum board, center the "
                "camera on it, and check the light"
            )
        at = found
    return at


async def disc_size(machine: MachineService) -> float | None:
    """How big the disc the top camera is centered on looks, in mm; None if unseen."""
    marks = await machine.locate_round_marks(*DISC_DIAMETER_MM, through_lines=True)
    here = machine.camera_position()
    near = min(marks, key=lambda m: math.hypot(m.at.x - here.x, m.at.y - here.y), default=None)
    return near.diameter if near else None


def tips_for_disc(machine: MachineService, disc: float) -> list[str]:
    """The profile's tips small enough to seal on a disc, smallest first."""
    fit = [
        t
        for t in machine.profile.nozzle_tips
        if t.outer_diameter is not None and t.outer_diameter <= disc - 2 * DISC_RIM_MM
    ]
    return [t.id for t in sorted(fit, key=lambda t: t.outer_diameter or 0.0)]


def too_big(machine: MachineService, nozzle: Nozzle, disc: float) -> NozzleTip | None:
    """The tip on a nozzle, if it is too big to seal on the disc."""
    tip = machine.profile.nozzle_tip(nozzle.tip)
    if tip is None or tip.outer_diameter is None:
        return None
    return tip if tip.outer_diameter > disc - 2 * DISC_RIM_MM else None


async def fit_disc_tips(machine: MachineService, nozzles: list[Nozzle], disc: float | None) -> bool:
    """Before any nozzle touches the disc: if a tip is too big to seal on it, bring the
    head to the front and ask for small tips, until they fit. Says whether tips changed."""
    if disc is None:
        return False
    changed = False
    while big := [(n, t) for n in nozzles if (t := too_big(machine, n, disc)) is not None]:
        choices = tips_for_disc(machine, disc)
        if not choices:
            nozzle, tip = big[0]
            raise NozzleOffsetError(
                f"{nozzle.label}'s {tip.id} tip is {tip.outer_diameter:g} mm across, too big "
                f"to seal on the {disc:.1f} mm disc, and this machine's profile has no smaller "
                "tip: use a bigger disc"
            )
        which = ", ".join(f"{n.label.lower()}'s {t.id} ({t.outer_diameter:g} mm)" for n, t in big)
        await machine.change_tips(
            [TipChangeNozzle(nozzle_id=n.id, fitted=n.tip, choices=choices) for n in nozzles],
            "Fit small tips for the disc",
            f"A tip needs {DISC_RIM_MM:g} mm of paper all round to seal on the {disc:.1f} mm "
            f"disc, too little for {which}. Fit one of {', '.join(choices)} on each nozzle "
            "(a different one on each is fine: a LumenPnP comes with one of each), then say "
            "which tip is on each.",
            "Each nozzle then finds the disc's height and measures its offset, with the tip "
            "it has now.",
        )
        changed = True
    return changed


async def turn_disc(
    machine: MachineService,
    nozzle: Nozzle,
    at: Point,
    turn: float,
    height: float,
    level: float | None = None,
    what: str = "the disc",
) -> float | None:
    """Pick the disc (or ``what``) up at a point, turn it, and set it down there again.
    ``level``: the vacuum reading that means it is held, in place of the nozzle's
    threshold. Returns the reading while it was held."""
    await machine.move_nozzle_over(nozzle, at.x, at.y, 0.0)
    # Suction on before going down, so it is already pulling when the tip arrives, and
    # the disc lifted only once the vacuum says it is held: a pump started with the tip
    # already down, and lifted after a fixed wait, left a disc that sealed weakly behind.
    await machine.suction(nozzle, True)
    await machine.lower_nozzle(nozzle, height)
    held = await machine.holds_part(nozzle, True, PICK_SEAL_TIMEOUT_S, level)
    if held is False:
        await machine.suction(nozzle, False)
        await machine.raise_nozzles()
        raise NozzleOffsetError(
            f"{nozzle.label} did not seal on {what} at height {height:g}: check it lies flat "
            "(a punched disc often curls at its edge), and that the nozzle tip is fitted and "
            "clear"
        )
    await asyncio.sleep(PICK_DWELL_S)
    # The first millimeter slowly, so the disc is not torn off before the seal holds it.
    await machine.lower_nozzle(nozzle, height + LIFT_SLOW_MM, APPROACH_SPEED)
    await machine.raise_nozzles()
    # Judged once the vacuum has settled: a disc that slipped off on the way up looked
    # held until the reading fell.
    held, reading = await machine.holds_after_lift(nozzle, HELD_SETTLE_S, level)
    if not held:
        reading = None
    if held is False:
        await machine.suction(nozzle, False)
        raise NozzleOffsetError(
            f"{nozzle.label} sealed on {what} at height {height:g} but dropped it as it "
            "lifted: check it lies flat and is loose (not stuck down), and that the nozzle "
            "tip is fitted and clear"
        )
    await machine.move_nozzle_over(nozzle, at.x, at.y, turn)
    await machine.lower_nozzle(nozzle, height)
    await machine.suction(nozzle, False)
    await asyncio.sleep(PLACE_DWELL_S)
    await machine.raise_nozzles()
    return reading


def with_tip(nozzle: Nozzle) -> str:
    """A nozzle's name, with its tip if one is said, for a message."""
    return nozzle.label + (f" with the {nozzle.tip} tip" if nozzle.tip else "")


def reading(value: float | None) -> str:
    """A vacuum reading for a message: large raw counts with thousands separators."""
    if value is None:
        return "none"
    return f"{value:,.0f}" if abs(value) >= 10_000 else f"{value:.3g}"


def vacuum_levels(
    machine: MachineService,
    nozzle: Nozzle,
    measure: VacuumMeasure,
    held: float | None,
    what: str = "the disc",
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
            f"{what} ({reading(h)}): it cannot tell a part held from none. Check the tip "
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


DISC_PRESS_MM = 1.0
"""The disc is picked and set down this far below where its top was touched, pressing the
tip firmly onto it so it cannot slip as it is let go. A LumenPnP nozzle gives about 3 mm;
lighter presses (0.2 and 0.4 mm) let a real disc's turns disagree by 0.4 mm."""
SEAL_MIN_FRACTION = 0.6
"""A seal on the disc must raise the vacuum from the open reading at least this much as
far as holding the disc does. On a real LumenPnP, paper sealed an N24 tip about 90% as
far; bare datum board pressed shut gave 36% (N24) and 20% (N75)."""
DISC_MIN_BRIGHTNESS = 150.0
"""A round mark this bright (of 255) in its middle may be white paper; copper and bare
board are darker."""
DOT_CLEARANCE_MM = 5.0
"""Round marks this near the datum board's dot are the board's own (the dot and its
ring), not the disc, which is laid clear of it."""
DISC_EDGE_MM = 5.0
"""The disc is looked for this far past the datum board's keep-out, near its edge."""
DISC_SEARCH_MARGIN_MM = 15.0
"""Without a datum board keep-out in the profile, the disc is looked for this far around
the homing fiducial."""
DISC_FIT_MM = 7.0
"""The search's frames overlap by this much, so a disc up to this size (a hole punch's,
6.4 mm, or the LumenPnP's, about 5) lies wholly inside at least one of them: the finder
leaves out round marks cut off by a frame's edge. Overlapping by a fixed fraction of a
LumenPnP's top camera's short side (3.5 mm) cut a 4.9 mm disc off in every frame."""
SEARCH_MIN_STEP_MM = 1.0
DISC_RIM_MM = 1.0
"""A tip seals on the disc only with this much paper all round its face: an N24 tip (4 mm
across) on a 4.9 mm disc sealed weakly and dropped it, its rim half over the disc's edge."""


def search_area(machine: MachineService) -> tuple[float, float, float, float]:
    """Where to look for the disc: the datum board's keep-out, or around its dot."""
    keep = next((k for k in machine.profile.keep_outs if k.id == "datum"), None)
    if keep is not None:
        a, m = keep.area, DISC_EDGE_MM
        return a.x_min - m, a.y_min - m, a.x_max + m, a.y_max + m
    dot = _datum(machine).position
    m = DISC_SEARCH_MARGIN_MM
    return dot.x - m, dot.y - m, dot.x + m, dot.y + m


def search_spots(
    area: tuple[float, float, float, float], fov: tuple[float, float], fit: float
) -> list[Point]:
    """Where to look, to cover an area: a grid whose frames (``fov``: width, height) overlap
    by ``fit``, so that a disc that size anywhere in the area lies wholly inside at least
    one frame."""
    x0, y0, x1, y1 = area

    def centers(lo: float, hi: float, view: float) -> list[float]:
        # Every point is at most half a step from a frame's center, so a disc there is
        # at most (view - fit) / 2 off it, and fits.
        step = max(SEARCH_MIN_STEP_MM, view - fit)
        n = max(1, math.ceil((hi - lo) / step))
        return [lo + (i + 0.5) * (hi - lo) / n for i in range(n)]

    return [Point(x=x, y=y) for x in centers(x0, x1, fov[0]) for y in centers(y0, y1, fov[1])]


async def search_disc(machine: MachineService, diameter: float | None) -> tuple[Point, float]:
    """Look over the datum board with the top camera for the disc, nearest its dot first,
    and center on it: where it is, and how big."""
    camera = machine.top_camera()
    dot = _datum(machine).position
    low, high = (diameter * 0.7, diameter * 1.4) if diameter else DISC_DIAMETER_MM
    fov = (camera.field_of_view.x, camera.field_of_view.y)
    spots = search_spots(search_area(machine), fov, min(high, DISC_FIT_MM))
    spots.sort(key=lambda p: math.hypot(p.x - dot.x, p.y - dot.y))
    marks: list[RoundMark] = []
    for spot in spots:
        await machine.move_camera_to(spot.x, spot.y)
        marks.extend(
            m
            for m in await machine.locate_round_marks(low, high, through_lines=True)
            if math.hypot(m.at.x - dot.x, m.at.y - dot.y) > DOT_CLEARANCE_MM
            and not near_pad(machine, m.at)
        )
    # The board has round marks of its own (fiducials, a copper disc, mounting holes):
    # the paper disc is the brightest.
    paper = [m for m in marks if m.brightness >= DISC_MIN_BRIGHTNESS]
    if paper:
        disc = max(paper, key=lambda m: m.brightness)
        size = diameter or disc.diameter
        return await find_disc(machine, disc.at, size), size
    raise VisionError(
        "the top camera found no disc on the datum board: lay a small paper disc (from a "
        "hole punch) flat on it, clear of the center dot, and check the top light"
    )


def nozzle_offsets_work(
    machine: MachineService,
    nozzle_ids: list[str] | None,
    diameter: float | None,
    thickness: float | None,
    search: bool = False,
) -> Callable[[], Awaitable[str]]:
    """Measuring the offsets of some nozzles (all, unless given) with a disc, keeping them
    and using them from now on: checked now, run when awaited. The disc is the one the top
    camera is over, or with ``search``, found on the datum board."""
    nozzles = [machine.nozzle(i) for i in nozzle_ids] if nozzle_ids else machine.profile.nozzles
    if not nozzles:
        raise InvalidTargetError("no nozzles to calibrate")
    machine.top_camera()
    _datum(machine)
    disc_thickness = PAPER_MM if thickness is None else thickness
    tips = {n.id: n.tip for n in nozzles}

    async def work() -> str:
        # Each nozzle is measured afresh: an old Z offset, perhaps wrong, would move where
        # it looks for the disc. Kept if the measuring fails.
        old = {n.id: n.z_offset for n in nozzles}
        for n in nozzles:
            n.z_offset = 0.0
        done = False
        try:
            message = await measure()
            done = True
            return message
        finally:
            if not done:
                for n in nozzles:
                    n.z_offset = old[n.id]
                # Tips changed: each nozzle's offset is its new tip's, if it has one.
                if any(n.tip != tips[n.id] for n in nozzles):
                    machine.apply_calibration()
            await machine.raise_nozzles()

    async def measure() -> str:
        if search:
            disc, size = await search_disc(machine, diameter)
        else:
            disc = await find_disc(machine, machine.camera_position(), diameter)
            size = diameter or await disc_size(machine)
        if await fit_disc_tips(machine, nozzles, size):
            # Fitting them applied each tip's own offset: measure afresh, as above.
            for n in nozzles:
                n.z_offset = 0.0
        fixes: list[NozzleFix] = []
        heights: dict[str, float] = {}
        levels: dict[str, VacuumLevels] = {}
        for nozzle in nozzles:
            measure = VacuumMeasure()
            heights[nozzle.id] = await sniff_disc(machine, nozzle, disc, disc_thickness, measure)
            # Touching it can shift the disc (paper clings to the tip as it lifts): look again.
            disc = await find_disc(machine, disc, diameter)
            # Until its levels are known, the disc is held at a reading halfway to sealed.
            level = (
                measure.open + (measure.sealed - measure.open) / 2
                if measure.open is not None and measure.sealed is not None
                else None
            )
            fix, disc, held = await calibrate_nozzle(
                machine,
                nozzle,
                disc,
                diameter,
                heights[nozzle.id] + disc_thickness - DISC_PRESS_MM,
                level,
            )
            check_seal(nozzle, measure, held, heights[nozzle.id] + disc_thickness)
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
            values[datum_key(n.id, n.tip)] = raw
            parts.append(
                f"{with_tip(n)}: ({fix.x:.3f}, {fix.y:.3f}), {off:.2f} mm from the profile's, "
                f"touching the datum board at Z {raw:.2f}"
            )
        spread = max(f.spread for f in fixes)
        message = f"{'; '.join(parts)}. The disc's turns agreed within {spread:.3f} mm."
        machine.calibration.record("nozzle_offsets", message, values)
        if levels:
            record_vacuum(machine, levels)
        machine.apply_calibration()
        return message + await keep_references(machine, nozzles)

    return work


def start_nozzle_offset_calibration(
    machine: MachineService,
    nozzle_ids: list[str] | None,
    diameter: float | None,
    thickness: float | None,
) -> Operation:
    """Measure the offsets of some nozzles (all, unless given) with a disc the top camera
    is over, keep them, and use them from now on."""
    work = nozzle_offsets_work(machine, nozzle_ids, diameter, thickness)
    return machine.start_task("nozzle_offsets", work)


def record_vacuum(machine: MachineService, levels: dict[str, VacuumLevels]) -> None:
    """Keep the nozzles' measured vacuum levels (line 9) with the tips they have fitted,
    with those of other nozzles and tips measured before."""
    previous = machine.calibration.get().lines.get("vacuum")
    values = dict(previous.values) if previous else {}
    for nozzle_id, found in levels.items():
        tip = machine.nozzle(nozzle_id).tip
        for name in ("open", "sealed", "held", "threshold"):
            values[vacuum_key(nozzle_id, name, tip)] = getattr(found, name)
    parts: list[str] = []
    for nozzle in machine.profile.nozzles:
        v = {
            n: values.get(vacuum_key(nozzle.id, n, nozzle.tip))
            for n in ("open", "held", "threshold")
        }
        if all(x is not None for x in v.values()):
            parts.append(
                f"{with_tip(nozzle)}: open {reading(v['open'])}, holding a part "
                f"{reading(v['held'])}, threshold {reading(v['threshold'])}"
            )
    unit = next((i.unit for i in machine.profile.inputs if i.role == "vacuum" and i.unit), None)
    message = "; ".join(parts) + (f" ({unit})." if unit else ".")
    machine.calibration.record("vacuum", message, values)
