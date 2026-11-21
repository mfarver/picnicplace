"""Calibrating the bottom camera (spec §7.7, line 5): where it is, its focus height, its
scale, and how far its image is turned.

An empty nozzle tip is brought over the camera at the profile's focus height and found
by vision, looking for any round mark of a plausible size: the profile's scale for the
camera is only a guess, and may be far off (a wider lens). The head then steps a known
distance each way in X and Y, and how the tip moves in the image gives its scale and
turn; with those, the tip is centered, which puts the camera's center where the tip is.
Then the nozzle steps up and down, never more than ``FOCUS_RANGE_MM`` from the profile's
focus height (so it cannot reach the camera), for the sharpest picture of the tip, and
the steps are taken again there for the final scale and turn. A camera with a deep field
is as sharp all through: the profile's focus height is kept.

Once the scale is known, with a tip fitted whose size the profile gives, vision looks
for a circle that size, and
the message says how big the tip's end looked, noting a tip that looks unlike the one the
operator said is fitted (the start of checking the fitted tip by camera).
"""

import itertools
import math
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import cv2
import numpy as np

from picnicplace.calibration import nozzle_key, z_offset_key
from picnicplace.machine.errors import VisionError
from picnicplace.machine.profile import Camera, Nozzle, NozzleTip
from picnicplace.machine.service import MachineService
from picnicplace.schemas import Operation
from picnicplace.vision.fiducial import Circle

FOCUS_RANGE_MM = 2.0
"""The focus search stays within this of the profile's focus height: the profile's, never
the last one found, which a search could otherwise move nearer the camera every time."""
FOCUS_NOISE_FRAMES = 3
"""Pictures taken at one height before the focus search, to see how much the sharpness
changes from picture to picture with nothing moving."""
FOCUS_NOISE = 3.0
"""A focus search whose sharpest picture stands out from the middle of the rest by no more
than this many times that change found no focus: a camera with a deep field is as sharp
all through (a real LumenPnP's scored 11.5 to 15.3 over 4 mm, with no peak), and the
profile's height is kept."""
COARSE_STEP_MM = 0.25
FINE_STEP_MM = 0.05
CENTER_PASSES = 5
CENTER_TOLERANCE_MM = 0.02
SCALE_STEP_MM = 1.0
TIP_DIAMETER_MM = (0.5, 8.0)
"""Nozzle tips seen from below, smallest to largest, when the fitted tip's size is not
known."""
TIP_SIZE_SLACK = 1.5
"""A fitted tip of known size is looked for from this much smaller to this much larger."""
TIP_SIZE_MISMATCH = 1.25
"""A tip's end looking this much smaller or larger than the fitted tip's is noted."""
SHARPNESS_RADIUS_MM = 4.0
FOCUS_MARGIN_MM = 0.25
"""Sharpness is scored over the fitted tip's end and this much round it, not the nozzle
behind it: going down, the end comes into focus first, and the nozzle's larger rings,
higher up, only lower down. On a real LumenPnP an 8 mm patch went on getting sharper
1 mm past an N045's focus, while a patch round its end peaked at the focus."""


def peak(heights: list[float], scores: list[float], step: float) -> tuple[float, float]:
    """The sharpest height, from sharpness scored at evenly spaced heights: each score is
    first averaged with its neighbors (a small patch's score jumps from picture to
    picture), and a parabola through the best three places it between them."""
    n = len(scores)
    near = [scores[max(0, i - 1) : i + 2] for i in range(n)]
    smooth = [sum(s) / len(s) for s in near]
    i = max(range(n), key=smooth.__getitem__)
    if 0 < i < n - 1:
        a, b, c = smooth[i - 1], smooth[i], smooth[i + 1]
        denominator = a - 2 * b + c
        if denominator < 0:
            return heights[i] + step * (a - c) / (2 * denominator), b
    return heights[i], smooth[i]


def focus_radius(tip: NozzleTip | None) -> float:
    """How far round the middle of the picture (mm) sharpness is scored, for a tip."""
    if tip is None or not tip.outer_diameter:
        return SHARPNESS_RADIUS_MM
    return tip.outer_diameter / 2 + FOCUS_MARGIN_MM


@dataclass(frozen=True)
class BottomCameraFix:
    x: float
    y: float
    focus: float
    scale_x: float
    scale_y: float
    rotation: float
    """Degrees the image is turned from what the profile's axes say."""
    mirrored: bool
    """The picture shows the machine mirrored, as from below; false if it is flipped to
    show it as from above."""
    width: int
    height: int
    tip_diameter: float | None
    """How big the tip's end looked, in mm, at the measured scale (None: the tip was found
    by its hole, and its end was not seen)."""
    runout: float = 0.0
    """How far the tip's end was from the axis it turns about, in mm: the place found is
    the axis's, not the tip's."""
    flat: bool = False
    """The picture was as sharp all through the focus search: the focus is the profile's."""


def bottom_camera(machine: MachineService) -> Camera:
    camera = next((c for c in machine.profile.cameras if c.looking == "up"), None)
    if camera is None:
        raise VisionError("this machine's profile has no bottom camera")
    return camera


FOCUS_REACH_MM = 1.0
"""A nozzle that cannot reach the bottom camera's focus height goes as low as its travel
allows instead, if that is within this of it: on a real LumenPnP the picture of an N24's
end was about as sharp over 1 mm (raw Z -26.6 to -27.6), and an N75 on the right nozzle,
2 mm lower than the left's N24 on the datum board, needed 0.05 mm more than its travel."""
TRAVEL_MARGIN_MM = 0.01
"""Kept clear of a Z axis's limit when a nozzle goes as low as it can."""


def focus_height_for(machine: MachineService, nozzle: Nozzle, camera: Camera) -> float:
    """The height to lower a nozzle to for the camera's focus: the focus height, or as low
    as the nozzle's travel allows, if that is within ``FOCUS_REACH_MM`` of it."""
    focus = camera.focus_height
    if focus is None:
        raise VisionError("calibrate the bottom camera first")
    axis = machine.profile.axis(nozzle.z_axis)
    lowest_raw = (axis.min if nozzle.z_sign > 0 else -axis.max) + TRAVEL_MARGIN_MM
    lowest = lowest_raw - nozzle.z_offset
    if focus >= lowest:
        return focus
    if lowest - focus <= FOCUS_REACH_MM:
        return lowest
    tip = f" with the {nozzle.tip} tip" if nozzle.tip else ""
    raise VisionError(
        f"{nozzle.label}{tip} cannot reach the {camera.label.lower()}'s focus: it would "
        f"go to Z {focus + nozzle.z_offset:.2f}, past the end of its travel "
        f"({lowest_raw:.2f}). Check the tip is pushed fully onto the nozzle, then measure "
        "the nozzle offsets again (they give each nozzle's height)"
    )


def fitted_tip(machine: MachineService, nozzle: Nozzle) -> NozzleTip | None:
    """The tip the operator said is fitted on a nozzle, if any."""
    return machine.profile.nozzle_tip(nozzle.tip)


def tip_size_note(tip: NozzleTip | None, seen: float | None) -> str:
    """What the bottom camera saw of a nozzle's tip, and whether it looks like the tip the
    operator said is fitted. None: the tip was found by its hole, its end not seen."""
    if seen is None:
        return "The tip was found by its hole; its end was not clear enough to measure."
    note = f"The tip's end looked {seen:.2f} mm across"
    if tip is None or tip.outer_diameter is None:
        return note + "."
    ratio = seen / tip.outer_diameter
    if 1 / TIP_SIZE_MISMATCH <= ratio <= TIP_SIZE_MISMATCH:
        return note + f", as the {tip.id} tip should."
    return (
        note + f", but the {tip.id} tip is {tip.outer_diameter:g} mm: check which tip is "
        "fitted, and that it is clean."
    )


TIP_SEARCH_FRACTION = (0.01, 0.25)
"""Before the camera's scale is known, a tip is any round mark this fraction of the
image's width across: the profile's field of view may be far off (a wider lens)."""


HOLE_SEARCH_PX = 8.0
"""Before the camera's scale is known, a tip's hole is any dark round mark from this many
pixels across (an N045's 0.45 mm hole was 12 pixels on a real LumenPnP) to the largest
``TIP_SEARCH_FRACTION`` says."""


async def find_tip(
    machine: MachineService,
    camera: Camera,
    nozzle: Nozzle,
    search_px: float | None,
    mm_per_px: float | None,
) -> tuple[float, float, int, int, float | None]:
    """Where the nozzle's tip is in the bottom camera's image, the image's size, and the
    tip's end's diameter, in pixels (None: its hole was found, its end not).

    A fitted tip with a hole is found by its hole, the darkest round mark near the
    middle: the hole is at the tip's end, while the nozzle's lit rings behind it are
    farther from the camera and move less in the picture as the head moves, which gave
    a real LumenPnP's bottom camera a scale half as large again as it is. Its end, as
    the end looks, is then looked for round the hole, for its size. Without a hole seen,
    the end is looked for: with the image's scale known (``mm_per_px``), a circle of the
    fitted tip's size; without it, any round mark of a plausible size near the
    center."""
    frame = await machine.frame(camera.id)
    h, w = frame.image.shape[:2]
    search = search_px if search_px is not None else min(w, h) * 0.45
    tip = fitted_tip(machine, nozzle)
    hole = None
    if tip is not None and tip.inner_diameter is not None:
        if mm_per_px is None:
            low_px, high_px = HOLE_SEARCH_PX, TIP_SEARCH_FRACTION[1] * w
        else:
            low_px = tip.inner_diameter / HOLE_SLACK / mm_per_px
            high_px = tip.inner_diameter * HOLE_SLACK / mm_per_px
        hole = await machine.recorder.vision(
            "find_hole",
            frame.image,
            low_px,
            high_px,
            search,
            camera=camera.id,
            mm_per_px=mm_per_px,
            purpose=f"the {tip.id} tip's hole",
        )
    if hole is not None and (tip is None or mm_per_px is None or tip.outer_diameter is None):
        return hole.x, hole.y, w, h, None
    if hole is not None:
        search = max(HOLE_CENTER * hole.diameter, TIP_EDGE_PX)
    expected = tip.outer_diameter if tip is not None and mm_per_px is not None else None
    if mm_per_px is None:
        low_px, high_px = (f * w for f in TIP_SEARCH_FRACTION)
    elif tip is not None and tip.outer_diameter is not None:
        low, high = tip_window(tip)
        low_px, high_px = low / mm_per_px, high / mm_per_px
    else:
        low_px, high_px = (d / mm_per_px for d in TIP_DIAMETER_MM)
    image = frame.image
    if hole is not None:
        # The end, centered on the hole: measured from the picture's middle, moved there.
        shift = np.array([[1, 0, w / 2 - hole.x], [0, 1, h / 2 - hole.y]], dtype=np.float32)
        image = np.asarray(cv2.warpAffine(frame.image, shift, (w, h)), dtype=np.uint8)
    circle = await machine.recorder.vision(
        "find_circle",
        image,
        low_px,
        high_px,
        search,
        expected / mm_per_px if expected is not None and mm_per_px is not None else None,
        camera=camera.id,
        mm_per_px=mm_per_px,
        purpose="nozzle tip",
    )
    if hole is not None:
        return hole.x, hole.y, w, h, circle.diameter if circle is not None else None
    if circle is None:
        fitted = f"the {tip.id} tip" if tip is not None and mm_per_px is not None else "the tip"
        raise VisionError(
            f"the {camera.label.lower()} cannot see the nozzle tip near the middle of its "
            f"picture: check {fitted} is fitted and empty, the camera's light, and the "
            "camera's position in the profile"
        )
    return circle.x, circle.y, w, h, circle.diameter


async def calibrate_bottom_camera(machine: MachineService, nozzle: Nozzle) -> BottomCameraFix:
    camera = bottom_camera(machine)
    design = machine.design_camera(camera.id)
    guess = design.focus_height if design is not None else camera.focus_height
    if guess is None:
        raise VisionError(
            "the bottom camera has no focus height in the profile: set an approximate one "
            "(focus_height in the local profile) first. The search stays within "
            f"{FOCUS_RANGE_MM:g} mm of it, so the nozzle never reaches the camera"
        )
    try:
        return await _calibrate(machine, camera, nozzle, guess)
    finally:
        await machine.raise_nozzles()


Vector = tuple[float, float]


@dataclass(frozen=True)
class _Steps:
    """How the tip moves in the image when the head moves: pixels per mm of +X and +Y
    (right, down), and the tip's diameter in pixels."""

    x: Vector
    y: Vector
    tip_px: float | None
    width: int
    height: int

    @property
    def mm_per_px(self) -> float:
        return 2 / (math.hypot(*self.x) + math.hypot(*self.y))

    def to_machine(self, du: float, dv: float) -> Vector:
        """The machine displacement that moves the tip ``du``, ``dv`` pixels."""
        (a, c), (b, d) = self.x, self.y
        det = a * d - b * c
        if abs(det) < 1e-9:
            raise VisionError("the nozzle tip did not move in the picture when the head moved")
        return (d * du - b * dv) / det, (a * dv - c * du) / det


async def _calibrate(
    machine: MachineService, camera: Camera, nozzle: Nozzle, guess: float
) -> BottomCameraFix:
    x, y = camera.position.x, camera.position.y

    async def look(
        at_x: float, at_y: float, z: float, search: float | None, mm: float | None, turn: float = 0
    ):
        await machine.move_nozzle_over(nozzle, at_x, at_y, turn)
        await machine.lower_nozzle(nozzle, z)
        return await find_tip(machine, camera, nozzle, search, mm)

    async def step(at_x: float, at_y: float, z: float, mm: float | None) -> _Steps:
        """Step the head each way and watch the tip move in the image."""
        moved: dict[str, Vector] = {}
        sizes: list[float | None] = []
        w = h = 0
        for axis in ("X", "Y"):
            points: list[Vector] = []
            for sign in (-1, 1):
                d = sign * SCALE_STEP_MM
                sx, sy = (at_x + d, at_y) if axis == "X" else (at_x, at_y + d)
                u, v, w, h, size = await look(sx, sy, z, None, mm)
                points.append((u, v))
                sizes.append(size)
            moved[axis] = (
                (points[1][0] - points[0][0]) / (2 * SCALE_STEP_MM),
                (points[1][1] - points[0][1]) / (2 * SCALE_STEP_MM),
            )
        seen = [s for s in sizes if s is not None]
        return _Steps(moved["X"], moved["Y"], sum(seen) / len(seen) if seen else None, w, h)

    # Find the tip at the profile's place, searching wide (the profile's scale may be far
    # off), then learn how it moves in the image, which gives the scale and the turn
    # whatever the profile says, and center it with that.
    u, v, w, h, _ = await look(x, y, guess, None, None)
    steps = await step(x, y, guess, None)
    for _ in range(CENTER_PASSES):
        dx, dy = steps.to_machine(u - w / 2, v - h / 2)
        if math.hypot(dx, dy) <= CENTER_TOLERANCE_MM:
            break
        x, y = x - dx, y - dy
        u, v, w, h, _ = await look(x, y, guess, SHARPNESS_RADIUS_MM / steps.mm_per_px, None)
    else:
        raise VisionError(f"could not center the nozzle tip in {CENTER_PASSES} passes")

    # The sharpest height, coarse and then fine, with a parabola through the best three.
    async def sharp(z: float) -> float:
        await machine.lower_nozzle(nozzle, z)
        frame = await machine.frame(camera.id)
        fh, fw = frame.image.shape[:2]
        radius = focus_radius(fitted_tip(machine, nozzle)) / steps.mm_per_px
        value: float = await machine.recorder.vision(
            "sharpness", frame.image, fw / 2, fh / 2, radius, camera=camera.id, purpose="focus"
        )
        return value

    async def best(center: float, half: float, step_mm: float) -> tuple[float, float, bool]:
        """The sharpest height near ``center``, its sharpness, and whether every height
        was about as sharp, within the pictures' own noise (``FOCUS_NOISE``)."""
        n = round(half / step_mm)
        heights = [
            min(0.0, center + k * step_mm)
            for k in range(-n, n + 1)
            if abs(center + k * step_mm - guess) <= FOCUS_RANGE_MM + 1e-9
        ]
        scores = [await sharp(z) for z in heights]
        flat = max(scores) - statistics.median(scores) <= FOCUS_NOISE * noise
        at, score = peak(heights, scores, step_mm)
        return at, score, flat

    still = [await sharp(guess) for _ in range(FOCUS_NOISE_FRAMES)]
    noise = max(still) - min(still)
    coarse, _, flat = await best(guess, FOCUS_RANGE_MM, COARSE_STEP_MM)
    if flat:
        focus = guess
    else:
        focus, _, _ = await best(coarse, 2 * COARSE_STEP_MM, FINE_STEP_MM)
    if not flat and abs(focus - guess) >= FOCUS_RANGE_MM - FINE_STEP_MM:
        raise VisionError(
            f"the sharpest picture is at the end of the search ({focus:.2f}), "
            f"{FOCUS_RANGE_MM:g} mm from the profile's focus height: set focus_height "
            "nearer the camera's focus in the local profile and calibrate again"
        )

    # Scale and turn at the focus height, now looking for a tip of the fitted tip's size.
    final = await step(x, y, focus, steps.mm_per_px)
    scale_x = 1 / math.hypot(*final.x)
    scale_y = 1 / math.hypot(*final.y)

    # Mirrored or not: a mirror reverses the turn from +X to +Y in the picture. A flipped
    # picture (a config's flip_x or flip_y) is not mirrored; taken for one, its X would
    # seem turned 180 degrees and its Y not at all, averaging to a turn of 90.
    (ax_, ay_), (bx_, by_) = final.x, final.y
    seen = ax_ * by_ - ay_ * bx_
    (ex, ey), (fx, fy) = (_image_direction(machine, camera, a, False) for a in ("X", "Y"))
    mirrored = (seen > 0) != (ex * fy - ey * fx > 0)

    # Which way the profile says +X and +Y move the tip in the image (right, down).
    turns: list[float] = []
    for axis, (du, dv) in (("X", final.x), ("Y", final.y)):
        right, down = _image_direction(machine, camera, axis, mirrored)
        expected = math.atan2(down, right)
        actual = math.atan2(dv, du)
        turns.append(math.degrees(math.remainder(actual - expected, math.tau)))
    rotation = sum(turns) / len(turns)
    tip_diameter = final.tip_px * (scale_x + scale_y) / 2 if final.tip_px is not None else None

    # The tip's end is centered with the nozzle at 0 degrees. A tip a little off the axis
    # it turns about (runout) is as far off the other way at 180: the axis is halfway, and
    # where the part is seen from is the axis, which does not move as the nozzle turns.
    ends: list[Vector] = []
    for turn in (0, 180):
        search = SHARPNESS_RADIUS_MM / final.mm_per_px
        u, v, w, h, _ = await look(x, y, focus, search, final.mm_per_px, turn)
        ends.append(final.to_machine(u - w / 2, v - h / 2))
    await machine.move_nozzle_over(nozzle, x, y, 0)
    (ax, ay), (bx, by) = ends
    dx, dy = bx - ax, by - ay
    return BottomCameraFix(
        x - (ax + bx) / 2,
        y - (ay + by) / 2,
        focus,
        scale_x,
        scale_y,
        rotation,
        mirrored,
        final.width,
        final.height,
        tip_diameter,
        math.hypot(dx, dy) / 2,
        flat,
    )


def _image_direction(
    machine: MachineService, camera: Camera, axis: str, mirrored: bool
) -> tuple[float, float]:
    """Which way (right, down) a nozzle tip moving +1 along an axis moves in the image,
    the picture not turned, and mirrored or not."""
    # image_to_machine is linear; invert it for the two unit image directions.
    rx, ry = machine.image_to_machine(camera, 1.0, 0.0, turned=False, mirrored=mirrored)
    dx, dy = machine.image_to_machine(camera, 0.0, 1.0, turned=False, mirrored=mirrored)
    det = rx * dy - dx * ry
    mx, my = (1.0, 0.0) if axis == "X" else (0.0, 1.0)
    right = (mx * dy - dx * my) / det
    down = (rx * my - mx * ry) / det
    return right, down


def bottom_camera_work(
    machine: MachineService, nozzle_id: str | None
) -> Callable[[], Awaitable[str]]:
    """Calibrating the bottom camera with a nozzle (the first, unless given), keeping what
    was found and using it from now on: checked now, run when awaited."""
    nozzle = machine.nozzle(nozzle_id) if nozzle_id else machine.profile.nozzles[0]
    camera = bottom_camera(machine)

    async def work() -> str:
        fix = await calibrate_bottom_camera(machine, nozzle)
        design = machine.design_camera(camera.id) or camera
        moved = math.hypot(fix.x - design.position.x, fix.y - design.position.y)
        message = (
            f"{camera.label}: at ({fix.x:.3f}, {fix.y:.3f}), {moved:.2f} mm from the "
            f"profile's place; in focus with the nozzle at Z {fix.focus:.2f}"
            + (
                " (the profile's focus height: the picture was as sharp all through the "
                f"search, {FOCUS_RANGE_MM:g} mm either side)"
                if fix.flat
                else ""
            )
            + f"; {(fix.scale_x + fix.scale_y) / 2:.5f} mm/pixel; image turned "
            f"{fix.rotation:+.2f}°"
            + ("" if fix.mirrored else ", flipped to show the machine as from above")
            + f". Measured with {nozzle.label}, on the axis it turns "
            f"about (its tip's end was {fix.runout:.2f} mm off it). "
            + tip_size_note(fitted_tip(machine, nozzle), fix.tip_diameter)
        )
        machine.calibration.record(
            "bottom_camera",
            message,
            {
                "x": fix.x,
                "y": fix.y,
                "focus": fix.focus,
                "scale_x": fix.scale_x,
                "scale_y": fix.scale_y,
                "rotation": fix.rotation,
                "mirrored": fix.mirrored,
                "width": fix.width,
                "height": fix.height,
                # Where the nozzle was taken to be: the place moves with it if its offset
                # is measured later.
                nozzle_key(nozzle.id, "x"): nozzle.offset.x,
                nozzle_key(nozzle.id, "y"): nozzle.offset.y,
                # And its Z offset: the focus height moves if that is measured later.
                z_offset_key(nozzle.id): nozzle.z_offset,
            },
        )
        machine.apply_calibration()
        return message

    return work


def start_bottom_camera_calibration(machine: MachineService, nozzle_id: str | None) -> Operation:
    """Calibrate the bottom camera with a nozzle (the first, unless given), keep what was
    found, and use it from now on."""
    return machine.start_task("bottom_camera", bottom_camera_work(machine, nozzle_id))


TIP_NAME_TOLERANCE = 0.15
"""A tip's end found within this fraction of its size fits it."""
TIP_EDGE_PX = 2.0
"""A tip's end can look up to this many pixels larger or smaller than it is (blur, and its
edge falling between pixels). On a coarse camera that is more than the tolerance: an N045
seen at 0.11 mm per pixel looked 1.31 mm across, and fitted an N08. A tip within the
tolerance plus this of the same circle could be it too, and the camera cannot tell."""
SAME_CIRCLE_PX = 2.0
"""Two tips' searches that found circles this close in size may have found the same edge
(each search sizes it a little differently)."""


HOLE_MIN_PX = 6.0
"""A tip's hole narrower than this many pixels is too small to look for: the tip is named
by its end alone."""
HOLE_SLACK = 1.3
"""A tip's hole is looked for from this much smaller than its inner diameter (a real N24's
2.4 mm hole looked 2.5 to 2.8 mm across)."""
HOLE_DARK = 0.25
"""A tip's hole is dark all through: inside it, at most this fraction as bright as the
end round it. On a real LumenPnP an N24's hole was 0.07 as bright; an N08's end, inside a
lit ring 4.2 mm across that looked like an N24's end, was 0.48."""
HOLE_DARK_MIN_PX = 10.0
"""A hole narrower than this many pixels is blurred to its middle, so how dark it is
says nothing: seen at all, it counts (an N08's 0.8 mm hole on a camera at 0.11 mm per
pixel). At a real LumenPnP's 0.062 mm per pixel, an N08's is 13 pixels."""
HOLE_LIT_MM = 5.0
"""A hole this wide or wider may be a cup, lit inside (a real N75's 7.5 mm cup was, with the
nozzle's own dark bore at the bottom). Such a tip fits only if its end and its hole are
both its size, and not if a tip with a dark hole fits too: a lit ring behind a smaller
tip, about 8.2 mm across with an inner edge 5.8 to 6.7 mm, can otherwise pass for one."""
HOLE_SEARCH_MM = 1.0
"""When no tip's end is found, a tip's hole is looked for this far from the picture's
middle: the nozzle is over the calibrated camera, so its hole is near it."""
HOLE_CENTER = 0.25
"""A tip's hole is centered on its end within this fraction of the hole's size (or
``TIP_EDGE_PX``, if more)."""


def hole_window(tip: NozzleTip, end: float, mm: float) -> tuple[float, float] | None:
    """The sizes (mm) to look for a tip's hole in, inside its end seen ``end`` mm across:
    from a little below its inner diameter to halfway to the end seen, and clearly
    smaller than it. None: the tip has no hole said, or it is too small to see."""
    inner, outer = tip.inner_diameter, tip.outer_diameter
    if inner is None or outer is None or inner / mm < HOLE_MIN_PX:
        return None
    # Halfway from the hole to the end as seen: the end's own edge, sized a little
    # differently by another search, is not taken for the hole.
    return inner / HOLE_SLACK, min((inner + end) / 2, end - 2 * TIP_EDGE_PX * mm)


async def _has_hole(
    machine: MachineService,
    camera: Camera,
    image: np.ndarray,
    tip: NozzleTip,
    end: Circle,
    mm: float,
) -> "Circle | _LitHole | bool":
    """The tip's hole, if seen inside the end found for it (True: the tip has none said,
    or it is too small to see; False: not seen; ``_LitHole``: a wide hole, not dark). A
    tip's end can be mistaken for a ring behind it, or a smaller tip's end for its hole's
    edge: the hole tells them apart."""
    window = hole_window(tip, end.diameter * mm, mm)
    if window is None:
        return True
    low, high = window
    if high <= low:
        return False
    holes: list[Circle] = await machine.recorder.vision(
        "find_circles",
        image,
        low / mm,
        high / mm,
        camera=camera.id,
        mm_per_px=mm,
        purpose=f"the {tip.id} tip's hole",
    )
    inner = tip.inner_diameter or 0.0
    near = max(HOLE_CENTER * inner / mm, TIP_EDGE_PX)
    centered = [h for h in holes if math.hypot(h.x - end.x, h.y - end.y) <= near]
    dark = next(
        (h for h in centered if inner / mm < HOLE_DARK_MIN_PX or _dark_inside(image, h, end)),
        None,
    )
    if dark is not None:
        return dark
    if inner >= HOLE_LIT_MM:
        return next((_LitHole(h) for h in centered if _lit_through(image, h)), False)
    return False


@dataclass(frozen=True)
class _LitHole:
    """A wide hole seen, but lit inside: a cup tip's, or a lit ring's inner edge."""

    circle: Circle


CUP_LIT = 0.4
"""A cup tip's hole is lit all through: from 30% of the way out to its edge, no band is
less than this fraction as bright as the brightest. A real N75's dimmest was 0.49; a lit
ring behind a smaller tip has a dark gap inside it, 0.30 to 0.33 as bright."""
CUP_BANDS = 10


def _lit_through(image: np.ndarray, hole: Circle) -> bool:
    """Whether a wide hole is lit all through, as a cup tip's is."""
    gray = image.max(axis=2) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    r = np.hypot(xx - hole.x, yy - hole.y) / (hole.diameter / 2)
    edges = np.linspace(0.3, 1.0, CUP_BANDS + 1)
    bands = [gray[(r >= a) & (r < b)] for a, b in itertools.pairwise(edges)]
    means = [float(b.mean()) for b in bands if b.size]
    return bool(means) and min(means) >= CUP_LIT * max(means)


def _dark_inside(image: np.ndarray, hole: Circle, end: Circle) -> bool:
    """Whether the inside of a hole is dark against the end round it."""
    gray = image.max(axis=2) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    r = np.hypot(xx - hole.x, yy - hole.y)
    inside = gray[r <= hole.diameter * 0.4]
    ring = gray[(r >= hole.diameter * 0.6) & (r <= end.diameter * 0.45)]
    if inside.size == 0 or ring.size == 0:
        return False
    return float(inside.mean()) <= HOLE_DARK * float(ring.mean())


def tip_window(tip: NozzleTip) -> tuple[float, float]:
    """The sizes (mm) to look for a tip's end in: around its outer diameter, and above
    halfway to its hole, so the hole (a circle of its own, sharing its center) is not
    taken for it."""
    outer = tip.outer_diameter or 0.0
    low, high = outer / TIP_SIZE_SLACK, outer * TIP_SIZE_SLACK
    if tip.inner_diameter is not None:
        low = max(low, (tip.inner_diameter + outer) / 2)
    return low, high


def _alike(seen: float, size: float) -> bool:
    """Whether a size seen is a tip's size, within ``TIP_NAME_TOLERANCE``."""
    return abs(math.log(seen / size)) <= math.log(1 + TIP_NAME_TOLERANCE)


def _outer(tip: NozzleTip) -> float:
    return tip.outer_diameter or 0.0


async def _named_by_hole(
    machine: MachineService, camera: Camera, image: np.ndarray, mm: float
) -> tuple[list[NozzleTip], float | None]:
    """The tips a nozzle could have by its hole alone, and the hole's size seen (mm; None
    if no hole was found): for a tip whose end does not stand out from the cone behind it.
    A real LumenPnP's N045 and N08, seen at 0.0371 mm per pixel, had ends lit no brighter
    than their cones, and their ends were not found; their holes, 0.39 to 0.44 and 0.79 to
    0.85 mm, were plain and dark. A tip fits if its hole is within ``HOLE_SLACK`` of the
    size seen, wide enough to judge how dark it is, and dark against the end round it."""
    holed = [
        t
        for t in machine.profile.nozzle_tips
        if t.inner_diameter and t.outer_diameter and t.inner_diameter / mm >= HOLE_DARK_MIN_PX
    ]
    if not holed:
        return [], None
    low = min(t.inner_diameter or 0.0 for t in holed) / HOLE_SLACK
    high = max(t.inner_diameter or 0.0 for t in holed) * HOLE_SLACK
    hole: Circle | None = await machine.recorder.vision(
        "find_hole",
        image,
        low / mm,
        high / mm,
        HOLE_SEARCH_MM / mm,
        camera=camera.id,
        mm_per_px=mm,
        purpose="a tip's hole, its end not found",
    )
    if hole is None:
        return [], None
    seen = hole.diameter * mm
    named = [
        t
        for t in holed
        if abs(math.log(seen / (t.inner_diameter or seen))) <= math.log(HOLE_SLACK)
        and _dark_inside(image, hole, replace(hole, diameter=(t.outer_diameter or 0.0) / mm))
    ]
    return named, seen


async def measure_tip(
    machine: MachineService, nozzle: Nozzle
) -> tuple[NozzleTip | None, float, list[str], bool]:
    """Which of the profile's tips a nozzle has, by how its end looks from the calibrated
    bottom camera. Each tip is looked for in its own size window; it fits if a circle is
    found there within ``TIP_NAME_TOLERANCE`` of its size, with the tip's hole seen inside
    it (``_has_hole``): a lit ring behind a small tip's end can look like a larger tip's
    end, and a larger tip's hole like a smaller tip's end. A smaller tip's window can also
    fit a larger tip's end merged with its hole, so the largest that fits wins, unless
    another tip, allowing ``TIP_EDGE_PX`` more, could be the same circle: then the camera
    cannot tell them apart. If no tip's end fits, the tip is named by its hole
    (``_named_by_hole``), if only one tip's hole fits it.
    Returns the tip (None: none fits, or it cannot tell), the size seen, in mm, the tips it
    cannot tell apart (empty unless so), and whether it was named by its hole: then the
    size is its hole's (also when the circle fitted round it was not its end's size)."""
    camera = bottom_camera(machine)
    height = focus_height_for(machine, nozzle, camera)
    await machine.move_nozzle_over(nozzle, camera.position.x, camera.position.y)
    await machine.lower_nozzle(nozzle, height)
    frame = await machine.frame(camera.id)
    mm = camera.field_of_view.x / frame.image.shape[1]
    fits: list[tuple[NozzleTip, float]] = []
    lit: list[tuple[NozzleTip, float]] = []  # fit by a wide hole lit inside
    near: list[tuple[NozzleTip, float]] = []  # within the tolerance plus TIP_EDGE_PX
    nearest: float | None = None
    holes: dict[str, float] = {}  # each fitted tip's hole, seen (mm)
    for tip in machine.profile.nozzle_tips:
        if not tip.outer_diameter:
            continue
        low, high = tip_window(tip)
        circle = await machine.recorder.vision(
            "find_circle",
            frame.image,
            low / mm,
            high / mm,
            SHARPNESS_RADIUS_MM / mm,
            tip.outer_diameter / mm,
            camera=camera.id,
            mm_per_px=mm,
            purpose=f"is it the {tip.id} tip",
        )
        if circle is None:
            continue
        seen = float(circle.diameter * mm)
        nearest = seen if nearest is None else nearest
        hole = await _has_hole(machine, camera, frame.image, tip, circle, mm)
        inner = tip.inner_diameter
        if isinstance(hole, _LitHole):
            if inner is not None and (
                _alike(seen, tip.outer_diameter) and _alike(hole.circle.diameter * mm, inner)
            ):
                lit.append((tip, seen))
        elif hole is not False and (
            _alike(seen, tip.outer_diameter)
            or (
                isinstance(hole, Circle) and inner is not None and _alike(hole.diameter * mm, inner)
            )
        ):
            fits.append((tip, seen))
            if isinstance(hole, Circle):
                holes[tip.id] = hole.diameter * mm
        slack = TIP_NAME_TOLERANCE * tip.outer_diameter + TIP_EDGE_PX * mm
        if abs(seen - tip.outer_diameter) <= slack:
            near.append((tip, seen))
    fits = fits or lit
    if fits:
        best, seen = max(fits, key=lambda f: f[0].outer_diameter or 0.0)
        alike = [t for t, d in near if t is not best and abs(d - seen) <= SAME_CIRCLE_PX * mm]
        if alike:
            return None, seen, [t.id for t in sorted([*alike, best], key=_outer)], False
        if best.id in holes and not _alike(seen, best.outer_diameter or seen):
            # Named by its hole: the circle found round it was not its end (a real N08's
            # cone, 2.23 mm across, was found in its window).
            return best, holes[best.id], [], True
        return best, seen, [], False
    named, hole_seen = await _named_by_hole(machine, camera, frame.image, mm)
    if len(named) == 1 and hole_seen is not None:
        return named[0], hole_seen, [], True
    if nearest is None:
        raise VisionError(
            f"the {camera.label.lower()} cannot see {nozzle.label.lower()}'s tip: check a "
            "tip is fitted and empty"
        )
    return None, nearest, [], False


async def identify_tips(machine: MachineService) -> str:
    """Bring each nozzle over the bottom camera, name its tip by the size of its end, and
    say it is fitted (within a task). A tip the camera cannot name is left as it was."""
    notes: list[str] = []
    try:
        for nozzle in machine.profile.nozzles:
            tip, seen, alike, by_hole = await measure_tip(machine, nozzle)
            if alike:
                notes.append(
                    f"{nozzle.label}: its end looked {seen:.2f} mm across, which could be "
                    f"{' or '.join(alike)}: the camera's pixels are too coarse to tell them "
                    "apart; say which it is"
                )
                continue
            if tip is None:
                notes.append(
                    f"{nozzle.label}: its end looked {seen:.2f} mm across, like none of the "
                    "profile's tips; say which it is"
                )
                continue
            if nozzle.tip != tip.id:
                machine.set_tip(nozzle.id, tip.id, in_task=True)
            notes.append(
                f"{nozzle.label}: {tip.id} (by its hole, {seen:.2f} mm across: its end did "
                "not stand out from the cone behind it)"
                if by_hole
                else f"{nozzle.label}: {tip.id} (its end looked {seen:.2f} mm across)"
            )
    finally:
        await machine.raise_nozzles()
    return "; ".join(notes) + "."
