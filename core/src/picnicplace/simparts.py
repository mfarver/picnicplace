"""Simulated parts: which part each nozzle holds, and where placed parts landed.

The simulator's nozzles pick a part only where there is one: a ``source`` (the feeder
service, with its simulated tapes) says what lies under the nozzle and takes it out of
its pocket. The part is held where it really was relative to the nozzle, at the angle
it really had, so bottom vision has a true offset and rotation to find. A part dropped
low enough over an empty pocket goes back into it (``restore``); one dropped low
enough anywhere else lands on the board where it was, so a test can check where it went.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartShape:
    body: tuple[float, float] | None = None
    pads: tuple[tuple[float, float, float, float, float], ...] = ()
    """Pads as (x, y, width, height, rotation) in the footprint's frame."""


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


Source = Callable[[float, float], tuple[PartShape, float, float, float] | None]
"""Take the part under a point, if any: its shape, true center, and angle."""

Restore = Callable[[float, float], bool]
"""Put a part back in the empty pocket under a point, if there is one; say if it did."""

PLACED_BELOW = -10.0
"""A part dropped with the nozzle lower than this lands on the board; higher, it is lost."""


def _turn(x: float, y: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


@dataclass
class SimulatedParts:
    source: Source
    restore: Restore | None = None
    held: dict[str, HeldPart] = field(default_factory=dict[str, HeldPart])
    placed: list[PlacedPart] = field(default_factory=list[PlacedPart])

    def pick(self, nozzle: str, x: float, y: float, nozzle_angle: float) -> bool:
        """The nozzle at (x, y) turned on suction: take the part there, if any."""
        taken = self.source(x, y)
        if taken is None:
            return False
        shape, px, py, part_angle = taken
        dx, dy = _turn(px - x, py - y, -nozzle_angle)
        self.held[nozzle] = HeldPart(shape, dx, dy, part_angle - nozzle_angle)
        return True

    def drop(self, nozzle: str, x: float, y: float, nozzle_angle: float, height: float) -> None:
        """The nozzle released its part at (x, y) with the nozzle at ``height``."""
        part = self.held.pop(nozzle, None)
        if part is None or height > PLACED_BELOW:
            return
        ox, oy = _turn(part.dx, part.dy, nozzle_angle)
        if self.restore is not None and self.restore(x + ox, y + oy):
            return
        self.placed.append(PlacedPart(part.shape, x + ox, y + oy, nozzle_angle + part.angle))

    def scene(self) -> tuple[tuple[tuple[str, HeldPart], ...], tuple[PlacedPart, ...]]:
        return tuple(sorted(self.held.items())), tuple(self.placed)
