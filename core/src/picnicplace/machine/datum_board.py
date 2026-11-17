"""The LumenPnP datum board's own marks, which a search for paper on it must leave out, and
the bare spots where a small tip can touch the board itself.

The board (the homing fiducial's ``lumenpnp-datum`` style) is mounted with its notched
edge, along its ruler, toward the bottom camera: turned to face it, never mirrored. Its
marks are given in the board's own frame, with that edge toward -Y.
"""

from dataclasses import dataclass

from picnicplace.machine.profile import MachineProfile, Point


@dataclass(frozen=True)
class DatumMark:
    dx: float
    dy: float
    diameter: float
    what: str


DATUM_MARKS = (
    DatumMark(-22.5, -7.5, 5.0, "a tinned pad"),
    DatumMark(22.5, -7.5, 5.0, "a silkscreen disc"),
)
"""Round marks about a paper disc's size, as bright as paper under a top camera's light
(on a real LumenPnP the tinned pad looked whiter than a paper disc)."""
BARE_SPOTS = ((-7.5, -7.5), (7.5, -7.5), (-7.5, 2.5), (7.5, 2.5))
"""Bare solder mask, in the middle of a square of the board's copper grid, 2.5 mm from its
lines and clear of its fiducials: where a small tip touches the board itself."""
BARE_SEAL_TIP_MM = 1.5
"""Tips with a face up to this size seal on the board's bare mask as soon as they touch
it. On a real LumenPnP (2026-11-16) an N045 sealed at the same height four times out of
four, and an N08 within 0.1 mm of touching; an N24 and an N75 leaked until pressed some
3 mm into their springs, and paper is needed for them."""
MARK_CLEARANCE_MM = 4.5
"""Round marks this near one of the board's own are taken for it, not for paper: a paper
disc this near would lie over it."""


def board_turn(dot: Point, camera_y: float | None) -> int:
    """1 if the board faces +Y (the bottom camera behind it), else -1."""
    return 1 if camera_y is None or camera_y > dot.y else -1


def datum_marks(profile: MachineProfile) -> list[Point]:
    """Where the datum board's own round marks are, on this machine."""
    bottom = next((c for c in profile.cameras if c.looking == "up"), None)
    marks: list[Point] = []
    for loc in profile.locations:
        if loc.kind != "homing_fiducial" or loc.style != "lumenpnp-datum":
            continue
        s = board_turn(loc.position, bottom.position.y if bottom else None)
        marks += [
            Point(x=loc.position.x - s * m.dx, y=loc.position.y - s * m.dy) for m in DATUM_MARKS
        ]
    return marks


def bare_spots(profile: MachineProfile, avoid: Point, clearance: float) -> list[Point]:
    """The bare spots on the datum board at least ``clearance`` mm from ``avoid`` (paper
    lying on it), nearest the board's dot first; none if it is not a LumenPnP datum
    board."""
    bottom = next((c for c in profile.cameras if c.looking == "up"), None)
    spots: list[Point] = []
    for loc in profile.locations:
        if loc.kind != "homing_fiducial" or loc.style != "lumenpnp-datum":
            continue
        s = board_turn(loc.position, bottom.position.y if bottom else None)
        for dx, dy in BARE_SPOTS:
            spot = Point(x=loc.position.x - s * dx, y=loc.position.y - s * dy)
            if (spot.x - avoid.x) ** 2 + (spot.y - avoid.y) ** 2 >= clearance**2:
                spots.append(spot)
    return spots


def on_datum_mark(profile: MachineProfile, at: Point) -> bool:
    """Whether a point is on one of the datum board's own round marks."""
    return any(
        (at.x - m.x) ** 2 + (at.y - m.y) ** 2 <= MARK_CLEARANCE_MM**2 for m in datum_marks(profile)
    )
