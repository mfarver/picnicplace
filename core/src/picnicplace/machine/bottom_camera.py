"""Calibrating the bottom camera (spec §7.7, line 5): where it is, its focus height, its
scale, and how far its image is turned.

An empty nozzle tip is brought over the camera at the profile's focus height and
centered in the image by vision, which puts the camera's center where the tip is. Then
the nozzle steps up and down, never more than ``FOCUS_RANGE_MM`` from the profile's
focus height (so it cannot reach the camera), for the sharpest picture of the tip.
Last, the head steps by a known distance each way in X and Y: how far the tip moves in
the image gives the scale, and which way it moves gives the image's turn.
"""

import math
from dataclasses import dataclass

from picnicplace.calibration import nozzle_key
from picnicplace.machine.errors import VisionError
from picnicplace.machine.profile import Camera, Nozzle
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
"""Nozzle tips seen from below, smallest to largest."""
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


def bottom_camera(machine: MachineService) -> Camera:
    camera = next((c for c in machine.profile.cameras if c.looking == "up"), None)
    if camera is None:
        raise VisionError("this machine's profile has no bottom camera")
    return camera


async def _find_tip(
    machine: MachineService, camera: Camera, search_mm: float
) -> tuple[float, float, int, int]:
    frame = await machine.frame(camera.id)
    h, w = frame.image.shape[:2]
    mm = camera.field_of_view.x / w
    low, high = TIP_DIAMETER_MM
    circle = await machine.recorder.vision(
        "find_circle",
        frame.image,
        low / mm,
        high / mm,
        search_mm / mm,
        camera=camera.id,
        mm_per_px=mm,
        purpose="nozzle tip",
    )
    if circle is None:
        raise VisionError(
            f"the {camera.label.lower()} cannot see the nozzle tip within {search_mm:g} mm "
            "of its center: check the tip is fitted and empty, the camera's light, and the "
            "camera's position in the profile"
        )
    return circle.x, circle.y, w, h


async def calibrate_bottom_camera(machine: MachineService, nozzle: Nozzle) -> BottomCameraFix:
    camera = bottom_camera(machine)
    guess = camera.focus_height
    if guess is None:
        raise VisionError(
            "the bottom camera has no focus height in the profile: set an approximate one "
            "(focus_height in the local profile) first. The search stays within "
            f"{FOCUS_RANGE_MM:g} mm of it, so the nozzle never reaches the camera"
        )
    await machine.set_light(camera, True)
    try:
        return await _calibrate(machine, camera, nozzle, guess)
    finally:
        await machine.raise_nozzles()
        await machine.set_light(camera, False)


async def _calibrate(
    machine: MachineService, camera: Camera, nozzle: Nozzle, guess: float
) -> BottomCameraFix:
    # Center the tip in the image: the camera's center is then where the tip is.
    x, y = camera.position.x, camera.position.y
    search = min(camera.field_of_view.x, camera.field_of_view.y) / 2
    for _ in range(CENTER_PASSES):
        await machine.move_nozzle_over(nozzle, x, y)
        await machine.lower_nozzle(nozzle, guess)
        u, v, w, h = await _find_tip(machine, camera, search)
        mm = camera.field_of_view.x / w
        dx, dy = machine.image_to_machine(camera, (u - w / 2) * mm, (v - h / 2) * mm)
        if math.hypot(dx, dy) <= CENTER_TOLERANCE_MM:
            break
        x, y = x - dx, y - dy
        search = SHARPNESS_RADIUS_MM
    else:
        raise VisionError(f"could not center the nozzle tip in {CENTER_PASSES} passes")

    # The sharpest height, coarse and then fine, with a parabola through the best three.
    async def sharp(z: float) -> float:
        await machine.lower_nozzle(nozzle, z)
        frame = await machine.frame(camera.id)
        h, w = frame.image.shape[:2]
        radius = SHARPNESS_RADIUS_MM * w / camera.field_of_view.x
        value: float = await machine.recorder.vision(
            "sharpness", frame.image, w / 2, h / 2, radius, camera=camera.id, purpose="focus"
        )
        return value

    async def best(center: float, half: float, step: float) -> tuple[float, float]:
        n = round(half / step)
        heights = [
            min(0.0, center + k * step)
            for k in range(-n, n + 1)
            if abs(center + k * step - guess) <= FOCUS_RANGE_MM + 1e-9
        ]
        scores = [await sharp(z) for z in heights]
        i = max(range(len(scores)), key=scores.__getitem__)
        if 0 < i < len(scores) - 1:
            a, b, c = scores[i - 1], scores[i], scores[i + 1]
            denominator = a - 2 * b + c
            if denominator < 0:
                return heights[i] + step * (a - c) / (2 * denominator), b
        return heights[i], scores[i]

    coarse, _ = await best(guess, FOCUS_RANGE_MM, COARSE_STEP_MM)
    focus, _ = await best(coarse, 2 * COARSE_STEP_MM, FINE_STEP_MM)
    if abs(focus - guess) >= FOCUS_RANGE_MM - FINE_STEP_MM:
        raise VisionError(
            f"the sharpest picture is at the end of the search ({focus:.2f}), "
            f"{FOCUS_RANGE_MM:g} mm from the profile's focus height: set focus_height "
            "nearer the camera's focus in the local profile and calibrate again"
        )

    # Scale and turn: step the head each way and watch the tip move in the image.
    seen: dict[str, tuple[float, float]] = {}
    w = h = 0
    for axis in ("X", "Y"):
        points: list[tuple[float, float]] = []
        for sign in (-1, 1):
            step = sign * SCALE_STEP_MM
            sx, sy = (x + step, y) if axis == "X" else (x, y + step)
            await machine.move_nozzle_over(nozzle, sx, sy)
            await machine.lower_nozzle(nozzle, focus)
            u, v, w, h = await _find_tip(machine, camera, SHARPNESS_RADIUS_MM)
            points.append((u, v))
        seen[axis] = (points[1][0] - points[0][0], points[1][1] - points[0][1])
    scale_x = 2 * SCALE_STEP_MM / math.hypot(*seen["X"])
    scale_y = 2 * SCALE_STEP_MM / math.hypot(*seen["Y"])

    # Which way the profile says +X and +Y move the tip in the image (right, down).
    turns: list[float] = []
    for axis, (du, dv) in seen.items():
        right, down = _image_direction(machine, camera, axis)
        expected = math.atan2(down, right)
        actual = math.atan2(dv, du)
        turns.append(math.degrees(math.remainder(actual - expected, math.tau)))
    rotation = sum(turns) / len(turns)
    return BottomCameraFix(x, y, focus, scale_x, scale_y, rotation, w, h)


def _image_direction(machine: MachineService, camera: Camera, axis: str) -> tuple[float, float]:
    """Which way (right, down) a nozzle tip moving +1 along an axis moves in the image."""
    # image_to_machine is linear; invert it for the two unit image directions.
    rx, ry = machine.image_to_machine(camera, 1.0, 0.0)
    dx, dy = machine.image_to_machine(camera, 0.0, 1.0)
    det = rx * dy - dx * ry
    mx, my = (1.0, 0.0) if axis == "X" else (0.0, 1.0)
    right = (mx * dy - dx * my) / det
    down = (rx * my - mx * ry) / det
    return right, down


def start_bottom_camera_calibration(machine: MachineService, nozzle_id: str | None) -> Operation:
    """Calibrate the bottom camera with a nozzle (the first, unless given), keep what was
    found, and use it from now on."""
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
            f"{fix.rotation:+.2f}°. Measured with {nozzle.label}."
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

    return machine.start_task("bottom_camera", work)
