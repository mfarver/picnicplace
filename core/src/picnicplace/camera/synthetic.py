"""Synthetic cameras for the simulator: views rendered from the machine state.

The top camera looks down at whatever is under the head: the bed, a demo board with
fiducials and pads in the work area, the homing fiducial, the discard bin, and part tape
in every feeder slot. The bottom camera looks up and sees a nozzle when one is over it,
with the part it holds. Lights dim the view when off.

The scene is deterministic, so a vision test can know the true position of every feature.
Frames are only rendered when what the camera would see changes.
"""

import asyncio
import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from picnicplace.camera.source import CameraSource, Frame, Image
from picnicplace.machine.profile import Camera, MachineProfile, Point

# Colors, BGR.
BED = (30, 30, 32)
"""The LumenPnP's staging plates: black PCBs."""
PCB = (40, 92, 30)
COPPER = (70, 150, 200)
SILK = (225, 230, 230)
FIDUCIAL_MASK = (30, 70, 25)
TAPE = (150, 185, 205)
POCKET = (95, 120, 135)
PART_BODY = (35, 35, 38)
PART_END = (200, 200, 205)
NOZZLE = (140, 140, 145)
NOZZLE_BORE = (20, 20, 20)
HEAD_UNDERSIDE = (25, 25, 28)

SHIFT = 4
"""Fixed-point bits for subpixel drawing."""
ONE = 1 << SHIFT

DIM = 0.3
"""Brightness with the camera's light off."""

PART_SIZE = (1.6, 0.8)
"""A 0603 chip: what the nozzles hold."""


@dataclass(frozen=True)
class SceneState:
    """What the cameras need to know about the machine."""

    position: dict[str, float]
    outputs: dict[str, bool]
    held: dict[str, bool]
    """Nozzle ID to whether it holds a part."""


class _Canvas:
    """Maps machine millimeters to pixels for one frame."""

    def __init__(
        self,
        size: tuple[int, int],
        center: Point,
        fov_w: float,
        mirror_x: bool,
        x_sign: int = 1,
        y_sign: int = 1,
    ):
        self.w, self.h = size
        self.x_sign, self.y_sign = x_sign, y_sign
        self.ppm = self.w / fov_w
        self.cx, self.cy = center.x, center.y
        self.mirror = mirror_x
        self.half_w = self.w / self.ppm / 2
        self.half_h = self.h / self.ppm / 2

    def px(self, x: float, y: float) -> tuple[int, int]:
        # Images are shown like the machine view, front at the bottom (see
        # MachineProfile.x_points and y_points).
        dx = (x - self.cx) * self.x_sign
        dy = (y - self.cy) * self.y_sign
        if self.mirror:
            dx = -dx
        u = self.w / 2 + dx * self.ppm
        v = self.h / 2 - dy * self.ppm
        return round(u * ONE), round(v * ONE)

    def visible(self, x: float, y: float, margin: float) -> bool:
        return abs(x - self.cx) <= self.half_w + margin and abs(y - self.cy) <= self.half_h + margin

    def circle(self, img: Image, x: float, y: float, r: float, color: tuple[int, int, int]):
        cv2.circle(img, self.px(x, y), round(r * self.ppm * ONE), color, -1, cv2.LINE_AA, SHIFT)

    def ring(self, img: Image, x: float, y: float, r: float, width: float, color: tuple[int, ...]):
        thickness = max(1, round(width * self.ppm))
        cv2.circle(
            img, self.px(x, y), round(r * self.ppm * ONE), color, thickness, cv2.LINE_AA, SHIFT
        )

    def rect(
        self,
        img: Image,
        x: float,
        y: float,
        w: float,
        h: float,
        angle_deg: float,
        color: tuple[int, int, int],
    ) -> None:
        """A filled rectangle centered at (x, y), rotated counterclockwise."""
        a = math.radians(angle_deg)
        ca, sa = math.cos(a), math.sin(a)
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        pts = np.array(
            [self.px(x + dx * ca - dy * sa, y + dx * sa + dy * ca) for dx, dy in corners],
            dtype=np.int32,
        )
        cv2.fillPoly(img, [pts], color, cv2.LINE_AA, SHIFT)


def _chip(canvas: _Canvas, img: Image, x: float, y: float, angle: float) -> None:
    """A 0603 resistor seen from above or below: dark body, metal ends."""
    w, h = PART_SIZE
    canvas.rect(img, x, y, w, h, angle, PART_END)
    canvas.rect(img, x, y, w * 0.62, h, angle, PART_BODY)


@dataclass(frozen=True)
class DemoBoard:
    """The board the simulator places in the work area: 80 x 60 mm, fiducials at three
    corners, and rows of 0603 pads."""

    origin: Point
    width: float = 80.0
    height: float = 60.0

    @property
    def fiducials(self) -> list[Point]:
        o = self.origin
        return [
            Point(x=o.x + 4, y=o.y + 4),
            Point(x=o.x + self.width - 4, y=o.y + 4),
            Point(x=o.x + 4, y=o.y + self.height - 4),
        ]

    def pads(self) -> list[tuple[float, float]]:
        o = self.origin
        return [
            (o.x + 12 + col * 6.0, o.y + 12 + row * 5.0) for row in range(8) for col in range(10)
        ]


def demo_board(profile: MachineProfile) -> DemoBoard | None:
    area = profile.work_area
    if area is None:
        return None
    cx, cy = (area.x_min + area.x_max) / 2, (area.y_min + area.y_max) / 2
    return DemoBoard(origin=Point(x=cx - 40, y=cy - 30))


class TopRenderer:
    def __init__(self, profile: MachineProfile, camera: Camera, size: tuple[int, int]):
        self.profile = profile
        self.camera = camera
        self.size = size
        self.board = demo_board(profile)
        rng = np.random.default_rng(7)
        w, h = size
        self._grain = rng.integers(-6, 7, size=(h, w, 1), dtype=np.int16)

    def render(self, state: SceneState) -> Image:
        w, h = self.size
        head = Point(x=state.position.get("X", 0.0), y=state.position.get("Y", 0.0))
        center = Point(x=head.x + self.camera.position.x, y=head.y + self.camera.position.y)
        canvas = _Canvas(
            self.size,
            center,
            self.camera.field_of_view.x,
            False,
            self.profile.x_sign,
            self.profile.y_sign,
        )
        img = np.empty((h, w, 3), dtype=np.uint8)
        img[:] = BED
        self._plates(canvas, img)
        self._board(canvas, img)
        self._locations(canvas, img)
        self._feeders(canvas, img)
        return _finish(img, self._grain, self.camera, state)

    def _plates(self, c: _Canvas, img: Image) -> None:
        for plate in self.profile.staging_plates:
            for hole in plate.holes:
                if c.visible(hole.x, hole.y, 5):
                    c.circle(img, hole.x, hole.y, plate.hole_diameter / 2, (10, 10, 10))
            for corner in plate.corner_holes:
                x, y = plate.center.x + corner.x, plate.center.y + corner.y
                if c.visible(x, y, 6):
                    c.circle(img, x, y, plate.corner_hole_diameter / 2, (10, 10, 10))
            if plate.knockout_diameter and c.visible(
                plate.center.x, plate.center.y, plate.knockout_diameter
            ):
                c.circle(
                    img, plate.center.x, plate.center.y, plate.knockout_diameter / 2, (6, 6, 8)
                )

    def _board(self, c: _Canvas, img: Image) -> None:
        b = self.board
        if b is None:
            return
        if not (
            c.visible(b.origin.x + b.width / 2, b.origin.y + b.height / 2, max(b.width, b.height))
        ):
            return
        c.rect(img, b.origin.x + b.width / 2, b.origin.y + b.height / 2, b.width, b.height, 0, PCB)
        for f in b.fiducials:
            if c.visible(f.x, f.y, 3):
                c.circle(img, f.x, f.y, 1.5, FIDUCIAL_MASK)
                c.circle(img, f.x, f.y, 0.5, COPPER)
        for x, y in b.pads():
            if c.visible(x, y, 2):
                c.rect(img, x - 0.8, y, 0.8, 0.9, 0, COPPER)
                c.rect(img, x + 0.8, y, 0.8, 0.9, 0, COPPER)
                c.rect(img, x, y + 1.2, 2.6, 0.12, 0, SILK)

    def _locations(self, c: _Canvas, img: Image) -> None:
        for loc in self.profile.locations:
            p = loc.position
            if not c.visible(p.x, p.y, 50):
                continue
            if loc.kind == "homing_fiducial" and loc.style == "lumenpnp-datum":
                _datum_board(c, img, p)
            elif loc.kind == "homing_fiducial":
                r = (loc.diameter or 2.0) / 2
                c.circle(img, p.x, p.y, r * 3, (20, 20, 20))
                c.circle(img, p.x, p.y, r, (235, 235, 235))
            elif loc.kind == "fiducial":
                # A 20 x 20 mm green board (cal-fid) with a copper dot in a mask opening.
                c.rect(img, p.x, p.y, 20, 20, 0, (40, 110, 45))
                r = (loc.diameter or 1.0) / 2
                c.circle(img, p.x, p.y, r * 2, (95, 140, 150))
                c.circle(img, p.x, p.y, r, DATUM_COPPER)
            elif loc.kind == "discard":
                c.rect(img, p.x, p.y, 14, 14, 0, (30, 30, 30))
                c.rect(img, p.x, p.y, 12, 12, 0, (15, 15, 15))
            elif loc.kind == "tip_holder":
                for dx in (-10, 0, 10):
                    c.ring(img, p.x + dx, p.y, 2.0, 0.6, (90, 90, 90))
        for cam in self.profile.cameras:
            if cam.mount == "fixed" and c.visible(cam.position.x, cam.position.y, 8):
                c.circle(img, cam.position.x, cam.position.y, 6.0, (20, 20, 22))
                c.ring(img, cam.position.x, cam.position.y, 4.5, 0.6, (60, 60, 70))

    def _feeders(self, c: _Canvas, img: Image) -> None:
        """8 mm paper tape in each slot, pockets every 4 mm, the first at the pick point
        (and empty: it was just picked)."""
        for bank in self.profile.feeder_banks:
            a = math.radians(bank.rotation)
            ux, uy = math.cos(a), math.sin(a)  # feed direction
            for i in range(bank.count):
                px = bank.first_pick.x + bank.pitch.x * i
                py = bank.first_pick.y + bank.pitch.y * i
                if not c.visible(px, py, bank.length):
                    continue
                # Tape runs back from the pick point, against the feed direction. EIA-481
                # 8 mm tape: sprocket holes 1.75 mm from one edge, pockets 3.5 mm from the
                # holes, so the pocket line is 1.25 mm off the tape's centerline.
                nx, ny = -uy, ux  # across the tape, toward the sprocket holes
                length = bank.length * 0.8
                tx = px - ux * (length / 2 - 2) + nx * 1.25
                ty = py - uy * (length / 2 - 2) + ny * 1.25
                c.rect(img, tx, ty, length, 8.0, bank.rotation, TAPE)
                for k in range(int(length // 4)):
                    qx, qy = px - ux * 4 * k, py - uy * 4 * k
                    if not c.visible(qx, qy, 3):
                        continue
                    c.rect(img, qx, qy, 2.0, 1.4, bank.rotation + 90, POCKET)
                    if k > 0:  # the pocket at the pick point was just emptied
                        _chip(c, img, qx, qy, bank.rotation + 90)
                    c.circle(img, qx + nx * 3.5, qy + ny * 3.5, 0.75, BED)


# --- The LumenPnP datum board ----------------------------------------------------------
# Geometry from the board's KiCad design (PCB-0004 rev 00), relative to its center
# fiducial, in machine millimeters (+Y toward the back). Bare copper shows through the
# solder mask wherever the design opens the mask over the ground pour.

# LumenPnP boards are black with white silkscreen. The copper finish is not known; gold
# (ENIG) is assumed.
DATUM_MASK = (34, 33, 32)  # black mask over copper
DATUM_COPPER = (120, 190, 225)  # exposed copper
DATUM_FR4 = (95, 140, 150)  # bare board, where neither mask nor copper covers it
DATUM_SILK = (225, 228, 230)

DATUM_OUTLINE = [
    (-40, -10),
    (-20, 10),
    (20, 10),
    (40, -10),
    (40, -20),
    (35, -25),
    (25, -25),
    (15, -15),
    (-15, -15),
    (-25, -25),
    (-35, -25),
    (-40, -20),
]
DATUM_FIDUCIALS = [(-10, 5), (10, 5), (-10, -10), (10, -10), (-35, -20), (35, -20)]
"""Other 1 mm fiducials on the board, in 3 mm openings (FID2 to FID7)."""
DATUM_HOLES = [(-15, 0), (-30, -15), (30, -15), (15, 0)]


def _datum_board(c: _Canvas, img: Image, o: Point) -> None:
    # The offsets below have +X to the operator's right and +Y toward the back, with the
    # board's notched edge (ruler side) toward the front, facing the bottom camera.
    # Turned into machine coordinates by which way the machine's axes point (on a
    # LumenPnP, +Y is the front).
    def at(dx: float, dy: float) -> tuple[float, float]:
        return o.x + c.x_sign * dx, o.y + c.y_sign * dy

    pts = np.array([c.px(*at(dx, dy)) for dx, dy in DATUM_OUTLINE], dtype=np.int32)
    cv2.fillPoly(img, [pts], DATUM_MASK, cv2.LINE_AA, SHIFT)

    def line(a: tuple[float, float], b: tuple[float, float], w: float = 0.15) -> None:
        cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        length = math.dist(a, b) + w
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        c.rect(img, *at(cx, cy), length, w, angle, DATUM_COPPER)

    # A 5 mm grid of exposed copper lines, 20 x 15 mm, from 5 mm behind the center
    # fiducial to 10 mm in front of it, broken where it meets the 8 mm ring.
    for x in (-10, -5, 5, 10):
        line((x, 5), (x, -10))
    for y in (5, -5, -10):
        line((-10, y), (10, y))
    line((-10, 0), (-4, 0))
    line((4, 0), (10, 0))
    line((0, 5), (0, 4))
    line((0, -4), (0, -10))
    c.ring(img, *at(0, 0), 4.0, 0.15, DATUM_COPPER)

    # The reticle: a ring with four ticks pointing in at the fiducial.
    c.ring(img, *at(0, 0), 2.35, 0.3, DATUM_COPPER)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        line((dx * 1.6, dy * 1.6), (dx * 2.85, dy * 2.85), 0.3)

    # Fiducials: a 1 mm copper dot isolated in an opening of bare board.
    c.circle(img, *at(0, 0), 1.0, DATUM_FR4)
    c.circle(img, *at(0, 0), 0.5, DATUM_COPPER)
    for dx, dy in DATUM_FIDUCIALS:
        c.circle(img, *at(dx, dy), 1.5, DATUM_FR4)
        c.circle(img, *at(dx, dy), 0.5, DATUM_COPPER)

    # A 5 mm exposed copper disc, mounting holes, and a 30 mm ruler along the front edge.
    c.circle(img, *at(-22.5, -7.5), 2.5, DATUM_COPPER)
    c.ring(img, *at(22.5, -7.5), 1.25, 0.15, DATUM_SILK)  # the one front silkscreen mark
    for dx, dy in DATUM_HOLES:
        c.circle(img, *at(dx, dy), 3.2, DATUM_COPPER)
        c.circle(img, *at(dx, dy), 1.6, (15, 15, 15))
    for i in range(31):
        length = 3.0 if i % 10 == 0 else 2.0 if i % 5 == 0 else 1.0
        line((-15 + i, -15), (-15 + i, -15 + length), 0.15)


class BottomRenderer:
    def __init__(self, profile: MachineProfile, camera: Camera, size: tuple[int, int]):
        self.profile = profile
        self.camera = camera
        self.size = size
        rng = np.random.default_rng(11)
        w, h = size
        self._grain = rng.integers(-5, 6, size=(h, w, 1), dtype=np.int16)

    def render(self, state: SceneState) -> Image:
        w, h = self.size
        canvas = _Canvas(
            self.size,
            self.camera.position,
            self.camera.field_of_view.x,
            True,
            self.profile.x_sign,
            self.profile.y_sign,
        )
        img = np.empty((h, w, 3), dtype=np.uint8)
        img[:] = (12, 12, 14)
        head_x, head_y = state.position.get("X", 0.0), state.position.get("Y", 0.0)
        # The head's underside, if it is over the camera.
        if canvas.visible(head_x, head_y, 60):
            canvas.rect(img, head_x, head_y - 40, 90, 70, 0, HEAD_UNDERSIDE)
        for nozzle in self.profile.nozzles:
            x, y = head_x + nozzle.offset.x, head_y + nozzle.offset.y
            if not canvas.visible(x, y, 5):
                continue
            canvas.circle(img, x, y, 2.6, NOZZLE)
            canvas.circle(img, x, y, 0.35, NOZZLE_BORE)
            if state.held.get(nozzle.id):
                angle = state.position.get(nozzle.rotation_axis, 0.0)
                # Parts never sit exactly centered: a small, fixed pick error.
                _chip(canvas, img, x + 0.12, y - 0.07, angle + 3.0)
        return _finish(img, self._grain, self.camera, state)


def _finish(img: Image, grain: np.ndarray, camera: Camera, state: SceneState) -> Image:
    """Sensor grain, and dimming when the camera's light is off."""
    out = img.astype(np.int16) + grain
    if camera.light_output is not None and not state.outputs.get(camera.light_output, False):
        out = (out * DIM).astype(np.int16)
    return np.clip(out, 0, 255).astype(np.uint8)


def render_size(camera: Camera, max_width: int = 1280) -> tuple[int, int]:
    """Frame size: the camera's aspect ratio, at most ``max_width`` wide."""
    aspect = camera.field_of_view.y / camera.field_of_view.x
    width = max_width
    return width, max(2, round(width * aspect / 2) * 2)


class SyntheticCamera(CameraSource):
    kind = "synthetic"

    def __init__(
        self,
        profile: MachineProfile,
        camera: Camera,
        scene: Callable[[], SceneState],
        fps: float = 15.0,
    ) -> None:
        super().__init__()
        self._size = render_size(camera)
        renderer_cls = TopRenderer if camera.looking == "down" else BottomRenderer
        self._renderer = renderer_cls(profile, camera, self._size)
        self._scene = scene
        self._interval = 1.0 / fps
        self._task: asyncio.Task[None] | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def fresh_frame(self, after: float, wait_s: float) -> Frame | None:
        """Render the present right away rather than waiting for the next tick."""
        image = await asyncio.to_thread(self._renderer.render, self._scene())
        await self._publish(image)
        return self.latest

    async def _run(self) -> None:
        last: SceneState | None = None
        while True:
            state = self._scene()
            if state != last:
                image = await asyncio.to_thread(self._renderer.render, state)
                await self._publish(image)
                last = state
            await asyncio.sleep(self._interval)
