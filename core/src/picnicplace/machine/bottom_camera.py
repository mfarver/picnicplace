"""Calibrating the bottom camera (spec §7.7, line 5): where it is, its focus height, its
scale, and how far its image is turned.

An empty nozzle tip is brought over the camera at the profile's focus height and found
by vision, looking for any round mark of a plausible size: the profile's scale for the
camera is only a guess, and may be far off (a wider lens). The head then steps a known
distance each way in X and Y, and how the tip moves in the image gives its scale and
turn; with those, the tip is centered, which puts the camera's center where the tip is.
Then the nozzle steps up and down, never more than ``FOCUS_RANGE_MM`` from the profile's
focus height (so it cannot reach the camera), for the sharpest picture of the tip, and
the steps are taken again there for the final scale and turn.

Once the scale is known, with a tip fitted whose size the profile gives, vision looks
for a circle that size, and
the message says how big the tip's end looked, noting a tip that looks unlike the one the
operator said is fitted (the start of checking the fitted tip by camera).
"""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from picnicplace.calibration import nozzle_key
from picnicplace.machine.errors import VisionError
from picnicplace.machine.profile import Camera, Nozzle, NozzleTip
from picnicplace.machine.service import MachineService
from picnicplace.schemas import Operation

FOCUS_RANGE_MM = 2.0
"""The focus search stays within this of the profile's focus height."""
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


@dataclass(frozen=True)
class BottomCameraFix:
    x: float
    y: float
    focus: float
    scale_x: float
    scale_y: float
    rotation: float
    """Degrees the image is turned from what the profile's axes say."""
    width: int
    height: int
    tip_diameter: float
    """How big the tip's end looked, in mm, at the measured scale."""


def bottom_camera(machine: MachineService) -> Camera:
    camera = next((c for c in machine.profile.cameras if c.looking == "up"), None)
    if camera is None:
        raise VisionError("this machine's profile has no bottom camera")
    return camera


def fitted_tip(machine: MachineService, nozzle: Nozzle) -> NozzleTip | None:
    """The tip the operator said is fitted on a nozzle, if any."""
    return machine.profile.nozzle_tip(nozzle.tip)


def tip_size_note(tip: NozzleTip | None, seen: float) -> str:
    """What the bottom camera saw of a nozzle's tip, and whether it looks like the tip the
    operator said is fitted."""
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


async def _find_tip(
    machine: MachineService,
    camera: Camera,
    nozzle: Nozzle,
    search_px: float | None,
    mm_per_px: float | None,
) -> tuple[float, float, int, int, float]:
    """Where the nozzle's tip is in the bottom camera's image, the image's size, and the
    tip's diameter, in pixels. With the image's scale known (``mm_per_px``), the search
    looks for a tip of the fitted tip's size; without it, for any round mark of a
    plausible size near the center."""
    frame = await machine.frame(camera.id)
    h, w = frame.image.shape[:2]
    search = search_px if search_px is not None else min(w, h) * 0.45
    tip = fitted_tip(machine, nozzle)
    expected = tip.outer_diameter if tip is not None and mm_per_px is not None else None
    if mm_per_px is None:
        low_px, high_px = (f * w for f in TIP_SEARCH_FRACTION)
    elif tip is not None and tip.outer_diameter is not None:
        low, high = tip.outer_diameter / TIP_SIZE_SLACK, tip.outer_diameter * TIP_SIZE_SLACK
        if tip.inner_diameter is not None:
            # A wide hole is a dark circle nearly the end's size: look past it.
            low = max(low, (tip.inner_diameter + tip.outer_diameter) / 2)
        low_px, high_px = low / mm_per_px, high / mm_per_px
    else:
        low_px, high_px = (d / mm_per_px for d in TIP_DIAMETER_MM)
    circle = await machine.recorder.vision(
        "find_circle",
        frame.image,
        low_px,
        high_px,
        search,
        expected / mm_per_px if expected is not None and mm_per_px is not None else None,
        camera=camera.id,
        mm_per_px=mm_per_px,
        purpose="nozzle tip",
    )
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
    guess = camera.focus_height
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
    tip_px: float
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

    async def look(at_x: float, at_y: float, z: float, search: float | None, mm: float | None):
        await machine.move_nozzle_over(nozzle, at_x, at_y)
        await machine.lower_nozzle(nozzle, z)
        return await _find_tip(machine, camera, nozzle, search, mm)

    async def step(at_x: float, at_y: float, z: float, mm: float | None) -> _Steps:
        """Step the head each way and watch the tip move in the image."""
        moved: dict[str, Vector] = {}
        sizes: list[float] = []
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
        return _Steps(moved["X"], moved["Y"], sum(sizes) / len(sizes), w, h)

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
        radius = SHARPNESS_RADIUS_MM / steps.mm_per_px
        value: float = await machine.recorder.vision(
            "sharpness", frame.image, fw / 2, fh / 2, radius, camera=camera.id, purpose="focus"
        )
        return value

    async def best(center: float, half: float, step_mm: float) -> tuple[float, float]:
        n = round(half / step_mm)
        heights = [
            min(0.0, center + k * step_mm)
            for k in range(-n, n + 1)
            if abs(center + k * step_mm - guess) <= FOCUS_RANGE_MM + 1e-9
        ]
        scores = [await sharp(z) for z in heights]
        i = max(range(len(scores)), key=scores.__getitem__)
        if 0 < i < len(scores) - 1:
            a, b, c = scores[i - 1], scores[i], scores[i + 1]
            denominator = a - 2 * b + c
            if denominator < 0:
                return heights[i] + step_mm * (a - c) / (2 * denominator), b
        return heights[i], scores[i]

    coarse, _ = await best(guess, FOCUS_RANGE_MM, COARSE_STEP_MM)
    focus, _ = await best(coarse, 2 * COARSE_STEP_MM, FINE_STEP_MM)
    if abs(focus - guess) >= FOCUS_RANGE_MM - FINE_STEP_MM:
        raise VisionError(
            f"the sharpest picture is at the end of the search ({focus:.2f}), "
            f"{FOCUS_RANGE_MM:g} mm from the profile's focus height: set focus_height "
            "nearer the camera's focus in the local profile and calibrate again"
        )

    # Scale and turn at the focus height, now looking for a tip of the fitted tip's size.
    final = await step(x, y, focus, steps.mm_per_px)
    scale_x = 1 / math.hypot(*final.x)
    scale_y = 1 / math.hypot(*final.y)

    # Which way the profile says +X and +Y move the tip in the image (right, down).
    turns: list[float] = []
    for axis, (du, dv) in (("X", final.x), ("Y", final.y)):
        right, down = _image_direction(machine, camera, axis)
        expected = math.atan2(down, right)
        actual = math.atan2(dv, du)
        turns.append(math.degrees(math.remainder(actual - expected, math.tau)))
    rotation = sum(turns) / len(turns)
    tip_diameter = final.tip_px * (scale_x + scale_y) / 2
    return BottomCameraFix(
        x, y, focus, scale_x, scale_y, rotation, final.width, final.height, tip_diameter
    )


def _image_direction(machine: MachineService, camera: Camera, axis: str) -> tuple[float, float]:
    """Which way (right, down) a nozzle tip moving +1 along an axis moves in the image."""
    # image_to_machine is linear; invert it for the two unit image directions.
    rx, ry = machine.image_to_machine(camera, 1.0, 0.0, turned=False)
    dx, dy = machine.image_to_machine(camera, 0.0, 1.0, turned=False)
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
            f"profile's place; in focus with the nozzle at Z {fix.focus:.2f}; "
            f"{(fix.scale_x + fix.scale_y) / 2:.5f} mm/pixel; image turned "
            f"{fix.rotation:+.2f}°. Measured with {nozzle.label}. "
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
                "width": fix.width,
                "height": fix.height,
                # Where the nozzle was taken to be: the place moves with it if its offset
                # is measured later.
                nozzle_key(nozzle.id, "x"): nozzle.offset.x,
                nozzle_key(nozzle.id, "y"): nozzle.offset.y,
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


def tip_window(tip: NozzleTip) -> tuple[float, float]:
    """The sizes (mm) to look for a tip's end in: around its outer diameter, and above
    halfway to its hole, so the hole (a circle of its own, sharing its center) is not
    taken for it."""
    outer = tip.outer_diameter or 0.0
    low, high = outer / TIP_SIZE_SLACK, outer * TIP_SIZE_SLACK
    if tip.inner_diameter is not None:
        low = max(low, (tip.inner_diameter + outer) / 2)
    return low, high


def _outer(tip: NozzleTip) -> float:
    return tip.outer_diameter or 0.0


async def measure_tip(
    machine: MachineService, nozzle: Nozzle
) -> tuple[NozzleTip | None, float, list[str]]:
    """Which of the profile's tips a nozzle has, by how its end looks from the calibrated
    bottom camera. Each tip is looked for in its own size window; it fits if a circle is
    found there within ``TIP_NAME_TOLERANCE`` of its size. A smaller tip's window can also
    fit a larger tip's end merged with its hole, so the largest that fits wins, unless
    another tip, allowing ``TIP_EDGE_PX`` more, could be the same circle: then the camera
    cannot tell them apart.
    Returns the tip (None: none fits, or it cannot tell), the size seen, in mm, and the
    tips it cannot tell apart (empty unless so)."""
    camera = bottom_camera(machine)
    if camera.focus_height is None:
        raise VisionError("calibrate the bottom camera first")
    await machine.move_nozzle_over(nozzle, camera.position.x, camera.position.y)
    await machine.lower_nozzle(nozzle, camera.focus_height)
    frame = await machine.frame(camera.id)
    mm = camera.field_of_view.x / frame.image.shape[1]
    fits: list[tuple[NozzleTip, float]] = []
    near: list[tuple[NozzleTip, float]] = []  # within the tolerance plus TIP_EDGE_PX
    nearest: float | None = None
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
        if abs(math.log(seen / tip.outer_diameter)) <= math.log(1 + TIP_NAME_TOLERANCE):
            fits.append((tip, seen))
        slack = TIP_NAME_TOLERANCE * tip.outer_diameter + TIP_EDGE_PX * mm
        if abs(seen - tip.outer_diameter) <= slack:
            near.append((tip, seen))
    if fits:
        best, seen = max(fits, key=lambda f: f[0].outer_diameter or 0.0)
        alike = [t for t, d in near if t is not best and abs(d - seen) <= SAME_CIRCLE_PX * mm]
        if alike:
            return None, seen, [t.id for t in sorted([*alike, best], key=_outer)]
        return best, seen, []
    if nearest is None:
        raise VisionError(
            f"the {camera.label.lower()} cannot see {nozzle.label.lower()}'s tip: check a "
            "tip is fitted and empty"
        )
    return None, nearest, []


async def identify_tips(machine: MachineService) -> str:
    """Bring each nozzle over the bottom camera, name its tip by the size of its end, and
    say it is fitted (within a task). A tip the camera cannot name is left as it was."""
    notes: list[str] = []
    try:
        for nozzle in machine.profile.nozzles:
            tip, seen, alike = await measure_tip(machine, nozzle)
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
            notes.append(f"{nozzle.label}: {tip.id} (its end looked {seen:.2f} mm across)")
    finally:
        await machine.raise_nozzles()
    return "; ".join(notes) + "."
