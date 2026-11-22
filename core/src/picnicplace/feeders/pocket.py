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


PITCH_TOLERANCE_MM = 0.5
"""Holes on the hole line that are not a whole sprocket pitch apart, within this, are not
all holes: a rim search also finds round things that happen to lie on the line."""


def _on_pitch(line: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The most holes on the line that are whole sprocket pitches apart."""
    best: list[tuple[float, float]] = []
    for x0, _ in line:
        agree = [
            (x, y) for x, y in line if abs(_wrap(x - x0, SPROCKET_PITCH)) <= PITCH_TOLERANCE_MM
        ]
        if len(agree) > len(best):
            best = agree
    return best


def pocket_from_holes(
    holes: list[tuple[float, float]], pocket_line: float, pitch: float
) -> PocketFix | None:
    """The pocket nearest the expected pick location, from sprocket holes seen nearby.

    ``holes`` are hole centers in the tape frame, relative to the expected pick location
    (x along the feed, y across it toward the holes). With the tape exactly in place a
    pocket is at (0, 0) and the holes at (2 + 4k, F).
    """
    line = _on_pitch([(x, y) for x, y in holes if abs(y - pocket_line) <= ACROSS_TOLERANCE_MM])
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


AMBIGUOUS_MM = 0.5
"""A pocket found within this of half a pitch from the pick location is about as near as
the next one the other way: on a real LumenPnP, feeders stopped 1.9 and 2.0 mm off."""


def pocket_choices(along: float, pitch: float) -> list[float]:
    """The pockets that may be the pick pocket, nearest first, from the nearest one's
    place along the feed: it, and the next one the other way if that is about as near."""
    if abs(along) > pitch / 2 - AMBIGUOUS_MM:
        return [along, along - math.copysign(pitch, along)]
    return [along]


def choose_pocket(holds: list[bool | None]) -> int:
    """Which of ``pocket_choices`` to take, from whether each holds a part: the nearest,
    unless only the other is seen holding one."""
    return 1 if len(holds) == 2 and holds[0] is not True and holds[1] is True else 0
