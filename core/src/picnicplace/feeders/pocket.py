"""Find the pick pocket from the tape's sprocket holes.

Feeders do not step exactly, and their errors add up, so the part is rarely exactly
where the slot's pick location says. The tape itself says where it is: EIA-481 puts
every pocket a fixed distance from the sprocket holes, 2 mm along the tape from a
hole's center and a fixed distance across (F, by tape width). Sprocket holes are round,
of known size, and high in contrast, which makes them easy to find with the camera.

So: find the holes near the pick location, work out where the pockets must be, and
take the pocket nearest the expected pick location. This corrects drift along the tape
and the tape sitting off to one side. Errors of more than half a sprocket pitch (2 mm)
cannot be told apart from the next pocket, so they are not corrected.
"""

import math
from dataclasses import dataclass
from statistics import median

from picnicplace.library.tape import POCKET_TO_SPROCKET, SPROCKET_PITCH

ACROSS_TOLERANCE_MM = 1.0
"""Holes this far from where the hole line should be are something else."""


@dataclass(frozen=True)
class PocketFix:
    along: float
    """How far the pocket is from the expected pick location, along the feed."""
    across: float
    """And across the tape, toward the sprocket holes."""
    holes: int
    """How many sprocket holes it was found from."""


def _wrap(value: float, period: float) -> float:
    """``value`` moved by whole periods into [-period/2, period/2)."""
    return value - period * math.floor(value / period + 0.5)


def pocket_from_holes(
    holes: list[tuple[float, float]], pocket_line: float, pitch: float
) -> PocketFix | None:
    """The pocket nearest the expected pick location, from sprocket holes seen nearby.

    ``holes`` are hole centers in the tape frame, relative to the expected pick location
    (x along the feed, y across it toward the holes). With the tape exactly in place a
    pocket is at (0, 0) and the holes at (2 + 4k, F).
    """
    line = [(x, y) for x, y in holes if abs(y - pocket_line) <= ACROSS_TOLERANCE_MM]
    if not line:
        return None
    across = median(y for _, y in line) - pocket_line
    # Where the hole grid is, relative to where it should be, from each hole; pockets
    # repeat every pitch, or every sprocket pitch if that is shorter (2 mm tape).
    period = min(pitch, SPROCKET_PITCH)
    shifts = [_wrap(x - POCKET_TO_SPROCKET, SPROCKET_PITCH) for x, _ in line]
    # Average on a circle, so shifts near +2 and -2 do not cancel out.
    angle = math.atan2(
        sum(math.sin(2 * math.pi * s / SPROCKET_PITCH) for s in shifts),
        sum(math.cos(2 * math.pi * s / SPROCKET_PITCH) for s in shifts),
    )
    along = _wrap(angle / (2 * math.pi) * SPROCKET_PITCH, period)
    return PocketFix(along=along, across=across, holes=len(line))
