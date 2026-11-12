"""Simulated parts: which part each nozzle holds, and where placed parts landed.

The simulator's nozzles pick a part only where there is one: a ``source`` (the feeder
service, with its simulated tapes) says what lies under the nozzle and takes it out of
its pocket. The part is held where it really was relative to the nozzle, at the angle
it really had, so bottom vision has a true offset and rotation to find. A part dropped
low enough lands on the board where it was, so a test can check where it went.

Heights matter where they are known. A nozzle picks a part only if its tip comes within
``REACH_MM`` of the part's top (``top_at``), and pressing more than ``PRESS_MM`` into a
part, or a held part into the board (``board_at``), is a crash, kept in ``crashes``. A
part let go above the board falls the rest of the way, and how far is kept with it.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartShape:
    body: tuple[float, float] | None = None
    pads: tuple[tuple[float, float, float, float, float], ...] = ()
    """Pads as (x, y, width, height, rotation) in the footprint's frame."""
    disc: float | None = None
    """A round disc of this diameter, such as a punched paper disc for calibrating the
    nozzle offsets. A disc can be picked up again where it lies."""
    height: float | None = None
    """How tall the part is, where known: where a held part's bottom is."""


@dataclass(frozen=True)
class HeldPart:
    shape: PartShape
    dx: float
    """The part's center from the nozzle's axis, with the nozzle at 0 degrees."""
    dy: float
    angle: float
    """The part's angle minus the nozzle's."""


@dataclass(frozen=True)
class PlacedPart:
    shape: PartShape
    x: float
    y: float
    angle: float
    fell: float = 0.0
    """How far it fell when let go, in mm: 0 if set down on the board."""


Source = Callable[[float, float], tuple[PartShape, float, float, float] | None]
"""Take the part under a point, if any: its shape, true center, and angle."""

SIM_DISC_DIAMETER_MM = 6.0
"""The simulator's calibration disc: a paper punch's, about a quarter inch."""
SIM_DISC_THICKNESS_MM = 0.1
"""...and paper's thickness."""

REACH_MM = 0.25
"""A nozzle's tip this far above a part's top still picks it up: the vacuum pulls it in."""
PRESS_MM = 0.5
"""Pressing a part (or a held part into the board) harder than this is a crash."""

PLACED_BELOW = -10.0
"""A part dropped with the nozzle lower than this lands on the board; higher, it is lost."""


def _turn(x: float, y: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


Height = Callable[[float, float], float | None]
"""A height under a point, if known."""


@dataclass
class SimulatedParts:
    source: Source
    top_at: Height | None = None
    """The top of the part waiting under a point (a feeder's pick location), if known."""
    board_at: Height | None = None
    """The top of whatever a placed part rests on under a point, if known."""
    held: dict[str, HeldPart] = field(default_factory=dict[str, HeldPart])
    placed: list[PlacedPart] = field(default_factory=list[PlacedPart])
    crashes: list[str] = field(default_factory=list[str])

    def pick(self, nozzle: str, x: float, y: float, nozzle_angle: float, tip: float) -> bool:
        """The nozzle at (x, y), its tip at ``tip``, turned on suction: take the part
        there, if any, and if the tip reaches it."""
        top = self._top(x, y)
        if top is not None:
            if tip > top + REACH_MM:
                return False
            if tip < top - PRESS_MM:
                self.crashes.append(
                    f"{nozzle} pressed {top - tip:.2f} mm into a part at ({x:.1f}, {y:.1f})"
                )
        taken = self._take_disc(x, y) or self.source(x, y)
        if taken is None:
            return False
        shape, px, py, part_angle = taken
        dx, dy = _turn(px - x, py - y, -nozzle_angle)
        self.held[nozzle] = HeldPart(shape, dx, dy, part_angle - nozzle_angle)
        return True

    def _top(self, x: float, y: float) -> float | None:
        """The top of the part under a point: a disc's on what it lies on, or a feeder's."""
        for part in self.placed:
            d = part.shape.disc
            if d is not None and math.hypot(part.x - x, part.y - y) <= d / 2:
                under = self.board_at(x, y) if self.board_at is not None else None
                h = part.shape.height
                return under + h if under is not None and h is not None else None
        return self.top_at(x, y) if self.top_at is not None else None

    def _take_disc(self, x: float, y: float) -> tuple[PartShape, float, float, float] | None:
        """A disc lying under the point, taken up."""
        for i, part in enumerate(self.placed):
            d = part.shape.disc
            if d is not None and math.hypot(part.x - x, part.y - y) <= d / 2:
                del self.placed[i]
                return part.shape, part.x, part.y, part.angle
        return None

    def drop(self, nozzle: str, x: float, y: float, nozzle_angle: float, tip: float) -> None:
        """The nozzle released its part at (x, y) with its tip at ``tip``."""
        part = self.held.pop(nozzle, None)
        if part is None or tip > PLACED_BELOW:
            return
        ox, oy = _turn(part.dx, part.dy, nozzle_angle)
        px, py = x + ox, y + oy
        fell = 0.0
        board = self.board_at(px, py) if self.board_at is not None else None
        if board is not None and part.shape.height is not None:
            bottom = tip - part.shape.height
            if bottom < board - PRESS_MM:
                self.crashes.append(
                    f"{nozzle} pressed its part {board - bottom:.2f} mm into the board at "
                    f"({px:.1f}, {py:.1f})"
                )
            fell = max(0.0, bottom - board)
        self.placed.append(PlacedPart(part.shape, px, py, nozzle_angle + part.angle, fell))

    def scene(self) -> tuple[tuple[tuple[str, HeldPart], ...], tuple[PlacedPart, ...]]:
        return tuple(sorted(self.held.items())), tuple(self.placed)
