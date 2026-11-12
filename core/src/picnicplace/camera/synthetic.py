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
from picnicplace.simparts import HeldPart, PartShape, PlacedPart

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
PAPER = (235, 238, 240)

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
    tapes: tuple["TapeScene", ...] | None = None
    """Simulated feeders: their tape and parts. None draws generic tape in every slot."""
    held_parts: tuple[tuple[str, HeldPart], ...] = ()
    """Nozzle ID and the part it holds, where the simulation knows the part."""
    placed: tuple[PlacedPart, ...] = ()
    """Parts put down on the bed."""
    boards: tuple["BoardScene", ...] = ()
    """Boards loaded for a job. Without any, the demo board is drawn."""


@dataclass(frozen=True)
class BoardScene:
    """A job's board (or panel) on the bed, where it really is, which may differ a
    little from where the job says: fiducial alignment finds the difference."""

    outline: tuple[tuple[float, float], ...]
    fiducials: tuple[tuple[float, float, float], ...] = ()
    """(x, y, diameter): copper dots in a solder mask opening twice as wide."""
    pads: tuple[tuple[float, float, float, float, float], ...] = ()
    """(x, y, width, height, angle) in machine coordinates."""
    marks: tuple[tuple[float, float, float], ...] = ()
    """(x, y, diameter): round silkscreen marks, such as the "O" or "D" of a label."""


@dataclass(frozen=True)
class TapeScene:
    """One simulated feeder, as the top camera sees it. Positions are true (physical)
    ones, which may differ from the profile's nominal slots."""

    pick: tuple[float, float]
    rotation: float
    """Tape direction in machine degrees."""
    width: float = 8.0
    pitch: float = 4.0
    pocket_line: float = 3.5
    """From the sprocket holes' line to the pockets' line."""
    advance: float = 0.0
    """Tape moved since the start, in mm: the pockets shift by this, modulo the pitch."""
    pick_full: bool = False
    """Whether the pocket at the pick location still holds a part."""
    part_rotation: float = 0.0
    """The part's rotation in the tape, EIA-481 view."""
    body: tuple[float, float] | None = None
    pads: tuple[tuple[float, float, float, float, float], ...] = ()
    """Pads as (x, y, width, height, rotation) in the footprint's frame."""
    part_height: float | None = None
    """How tall its parts are, from the library."""
    top: float | None = None
    """Where the top of the part at the pick location really is, as a nozzle height."""
    fiducial: tuple[float, float] | None = None
    fiducial_diameter: float = 1.0
    fiducial_marks: tuple[tuple[float, float], ...] = ()
    """Smaller marks beside the fiducial (a Photon feeder's width code)."""
    feeder_ahead: float = 8.0
    """How far the feeder reaches past the pick location."""
    feeder_center: float = 0.0
    """Where the middle of the feeder is across the tape, from the pocket line."""
    window: tuple[float, float, float, float] | None = None
    """The opening the tape shows through (along from, along to, across from, across
    to), from the pick location and the pocket line. Elsewhere the feeder covers the
    tape. None: the tape shows its whole length."""
    feeder_width: float = 15.0
    feeder_length: float = 100.0


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

    def polygon(
        self, img: Image, points: tuple[tuple[float, float], ...], color: tuple[int, int, int]
    ) -> None:
        pts = np.array([self.px(x, y) for x, y in points], dtype=np.int32)
        cv2.fillPoly(img, [pts], color, cv2.LINE_AA, SHIFT)


def _job_board(c: _Canvas, img: Image, b: BoardScene) -> None:
    xs, ys = [x for x, _ in b.outline], [y for _, y in b.outline]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    if not c.visible(cx, cy, max(max(xs) - min(xs), max(ys) - min(ys)) / 2):
        return
    c.polygon(img, b.outline, PCB)
    for x, y, w, h, angle in b.pads:
        if c.visible(x, y, max(w, h)):
            c.rect(img, x, y, w, h, angle, COPPER)
    for x, y, d in b.fiducials:
        if c.visible(x, y, 2 * d):
            c.circle(img, x, y, d, FIDUCIAL_MASK)
            c.circle(img, x, y, d / 2, COPPER)
    for x, y, d in b.marks:
        if c.visible(x, y, d):
            c.circle(img, x, y, d / 2, SILK)


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
        if state.boards:
            for board in state.boards:
                _job_board(canvas, img, board)
        else:
            self._board(canvas, img)
        self._locations(canvas, img)
        for part in state.placed:  # on top of the datum board, as a calibration disc is
            if canvas.visible(part.x, part.y, 10):
                _shape(canvas, img, part.x, part.y, part.angle, part.shape)
        if state.tapes is None:
            self._feeders(canvas, img)
        else:
            for tape in state.tapes:
                _tape(canvas, img, tape)
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
            kx = plate.center.x + plate.knockout_offset.x
            ky = plate.center.y + plate.knockout_offset.y
            if plate.knockout_diameter and c.visible(kx, ky, plate.knockout_diameter):
                c.circle(img, kx, ky, plate.knockout_diameter / 2, (6, 6, 8))

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
                bottom = next((k for k in self.profile.cameras if k.looking == "up"), None)
                _datum_board(c, img, p, bottom.position.y if bottom else None)
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


FEEDER_BODY = (52, 52, 56)
FIDUCIAL_BOARD = (28, 28, 30)
"""The small black board on a Photon feeder's nose."""
SPROCKET_PITCH = 4.0
SPROCKET_EDGE = 1.75


def _tape(c: _Canvas, img: Image, t: TapeScene) -> None:
    """A feeder with its tape, in the EIA-481 frame turned to the feeder's rotation: x
    along the feed, y across it toward the sprocket holes."""
    px, py = t.pick
    a = math.radians(t.rotation)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux

    def at(s: float, d: float) -> tuple[float, float]:
        return px + ux * s + nx * d, py + uy * s + ny * d

    if not c.visible(px, py, t.feeder_length + 20):
        return
    # The feeder body, then the tape on it.
    behind = t.feeder_length - t.feeder_ahead
    bx, by = at(t.feeder_ahead - t.feeder_length / 2, t.feeder_center)
    c.rect(img, bx, by, t.feeder_length, t.feeder_width, t.rotation, FEEDER_BODY)
    holes_line = t.pocket_line
    edge = holes_line + SPROCKET_EDGE
    length = t.feeder_length * 0.8
    tx, ty = at(6 - length / 2, edge - t.width / 2)
    c.rect(img, tx, ty, length, t.width, t.rotation, TAPE)

    hole_phase = t.advance % SPROCKET_PITCH
    for k in range(int(length // SPROCKET_PITCH) + 1):
        # EIA-481: a pocket is 2 mm along the tape from a sprocket hole.
        s = 6 - k * SPROCKET_PITCH + hole_phase
        hx, hy = at(s, holes_line)
        if c.visible(hx, hy, 2):
            c.circle(img, hx, hy, 0.75, BED)

    turn = math.radians(t.part_rotation)
    angle = t.rotation + t.part_rotation
    ex, ey = _extent(t)
    ca, sa = abs(math.cos(turn)), abs(math.sin(turn))
    pocket = (max(1.0, ex * ca + ey * sa + 0.4), max(0.8, ex * sa + ey * ca + 0.4))
    # The pocket nearest the pick location is the one to pick next; those past it along
    # the feed were picked already.
    phase = t.advance % t.pitch
    pick_s = phase if phase <= t.pitch / 2 else phase - t.pitch
    for k in range(-1, int(length // t.pitch) + 1):
        s = pick_s - k * t.pitch
        if s > 5:
            continue
        qx, qy = at(s, 0)
        if not c.visible(qx, qy, max(pocket) + 1):
            continue
        c.rect(img, qx, qy, pocket[0], pocket[1], t.rotation, POCKET)
        if k < 0 or (k == 0 and not t.pick_full):
            continue
        _part(c, img, qx, qy, angle, t)

    if t.window is not None:
        # The top of the feeder covers the tape except at the pick window.
        s0, s1, d0, d1 = t.window
        lo, hi = t.feeder_center - t.feeder_width / 2, t.feeder_center + t.feeder_width / 2
        for (a0, a1), (e0, e1) in (
            ((-behind, s0), (lo, hi)),
            ((s1, t.feeder_ahead), (lo, hi)),
            ((s0, s1), (lo, d0)),
            ((s0, s1), (d1, hi)),
        ):
            if a1 > a0 and e1 > e0:
                cx, cy = at((a0 + a1) / 2, (e0 + e1) / 2)
                c.rect(img, cx, cy, a1 - a0, e1 - e0, t.rotation, FEEDER_BODY)

    if t.fiducial is not None:
        # A 4 x 12 mm board, long across the feeder: the fiducial in a mask opening twice
        # its size, and the smaller marks beside it.
        fx, fy = t.fiducial
        c.rect(img, fx, fy, 4.0, 12.0, t.rotation, FIDUCIAL_BOARD)
        c.circle(img, fx, fy, t.fiducial_diameter, DATUM_FR4)
        c.circle(img, fx, fy, t.fiducial_diameter / 2, DATUM_COPPER)
        for mx, my in t.fiducial_marks:
            c.circle(img, mx, my, 0.75, DATUM_FR4)
            c.circle(img, mx, my, 0.25, DATUM_COPPER)


def _extent(t: TapeScene) -> tuple[float, float]:
    """The part's size in its own frame."""
    if t.body is not None:
        return t.body
    if not t.pads:
        return PART_SIZE
    xs = [abs(x) + w / 2 for x, _, w, _, _ in t.pads]
    ys = [abs(y) + h / 2 for _, y, _, h, _ in t.pads]
    return 2 * max(xs), 2 * max(ys)


def _part(c: _Canvas, img: Image, x: float, y: float, angle: float, t: TapeScene) -> None:
    _shape(c, img, x, y, angle, PartShape(body=t.body, pads=t.pads))


def _shape(c: _Canvas, img: Image, x: float, y: float, angle: float, s: PartShape) -> None:
    """A part seen from above or below: its body, and metal where its pads are. Without
    a footprint, a 0603 chip."""
    if s.disc is not None:
        c.circle(img, x, y, s.disc / 2, PAPER)
        return
    if s.body is None and not s.pads:
        _chip(c, img, x, y, angle)
        return
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    if s.body is not None:
        c.rect(img, x, y, s.body[0], s.body[1], angle, PART_BODY)
    for px, py, w, h, r in s.pads:
        cx, cy = x + px * ca - py * sa, y + px * sa + py * ca
        c.rect(img, cx, cy, w * 0.8, h * 0.8, angle + r, PART_END)


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


def _datum_board(c: _Canvas, img: Image, o: Point, camera_y: float | None) -> None:
    # The offsets below are in the board's own frame, with its notched edge (ruler side)
    # toward -Y. The board is mounted with that edge facing the bottom camera, so it is
    # turned (never mirrored) to face the camera.
    s = 1 if camera_y is None or camera_y > o.y else -1

    def at(dx: float, dy: float) -> tuple[float, float]:
        return o.x - s * dx, o.y - s * dy

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
            empty = not state.held.get(nozzle.id) and nozzle.id not in dict(state.held_parts)
            if empty and self.camera.focus_height is not None:
                # An empty tip is sharp only at the camera's focus height.
                height = state.position.get(nozzle.z_axis, 0.0) * nozzle.z_sign
                _defocus(canvas, img, x, y, abs(height - self.camera.focus_height))
            angle = state.position.get(nozzle.rotation_axis, 0.0)
            known = dict(state.held_parts).get(nozzle.id)
            if known is not None:
                # Where the part really sits on the nozzle, turned with it.
                a = math.radians(angle)
                ox = known.dx * math.cos(a) - known.dy * math.sin(a)
                oy = known.dx * math.sin(a) + known.dy * math.cos(a)
                _shape(canvas, img, x + ox, y + oy, angle + known.angle, known.shape)
            elif state.held.get(nozzle.id):
                # Parts never sit exactly centered: a small, fixed pick error.
                _chip(canvas, img, x + 0.12, y - 0.07, angle + 3.0)
        return _finish(img, self._grain, self.camera, state)


DEFOCUS_BLUR = 0.3
"""Blur (sigma, mm) per mm a nozzle tip is from the bottom camera's focus."""


def _defocus(canvas: _Canvas, img: Image, x: float, y: float, off: float) -> None:
    """Blur the area around a nozzle tip as far out of focus as it is."""
    sigma = off * DEFOCUS_BLUR * canvas.ppm
    if sigma < 0.3:
        return
    u, v = (c / ONE for c in canvas.px(x, y))
    r = round(4.5 * canvas.ppm + 3 * sigma)
    h, w = img.shape[:2]
    x0, x1 = max(0, round(u) - r), min(w, round(u) + r)
    y0, y1 = max(0, round(v) - r), min(h, round(v) + r)
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = cv2.GaussianBlur(img[y0:y1, x0:x1], (0, 0), sigma)


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
