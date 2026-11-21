"""Does a part's footprint match the board's footprint where it goes?

A placement's rotation and position are the board footprint's; the run picks, turns, and
places the part by its own footprint in the library. Two footprints by different names
can have the same pads (a library's name for an 0603 and KiCad's), and then all is well.
If their pads differ, the part may not fit the board's pads; if they are the same pads
turned, or with the origin elsewhere, the part lands turned or off its pads.
"""

import math

from picnicplace.library.models import Footprint, Pad

PAD_FIT_MM = 0.15
"""Pads this close (center to center) are taken as the same pad."""


def footprint_mismatch(board: Footprint | None, part: Footprint | None) -> str | None:
    """Why the part's footprint ``part`` does not fit the board's ``board``: None if it
    does (the same footprint, or the same pads in the same places)."""
    if board is not None and part is not None and board.id == part.id:
        return None
    if board is None or part is None or not board.pads or not part.pads:
        return "one of them has no pads in the library to compare"
    if len(board.pads) != len(part.pads):
        return f"it has {len(part.pads)} pads, the board's {len(board.pads)}"
    for turn in (0, 90, 180, 270):
        if _same(board.pads, part.pads, turn, centered=False):
            if turn == 0:
                return None
            return (
                f"its pads are the board's turned {360 - turn}° counterclockwise: the part "
                f"would be placed turned {360 - turn}° from the board's footprint"
            )
    bx, by = _center(board.pads)
    px, py = _center(part.pads)
    for turn in (0, 90, 180, 270):
        if _same(board.pads, part.pads, turn, centered=True):
            turned = f" and turned {360 - turn}° counterclockwise" if turn else ""
            return (
                f"its origin is {math.hypot(bx - px, by - py):.2f} mm from the board's"
                f"{turned}: the part would be placed off its pads"
            )
    return "its pads are not where the board's are"


def _center(pads: list[Pad]) -> tuple[float, float]:
    return sum(p.x for p in pads) / len(pads), sum(p.y for p in pads) / len(pads)


def _same(board: list[Pad], part: list[Pad], turn: int, centered: bool) -> bool:
    """Whether the part's pads, turned ``turn`` degrees counterclockwise (back onto the
    board's, so the part's are the board's turned ``360 - turn``), lie one to one
    on the board's: turned about the origin, or with ``centered``, about the pads' center
    and with that center put on the board pads' center. Pin 1 must land on pin 1 when
    both footprints mark it."""
    a = math.radians(turn)
    c, s = round(math.cos(a)), round(math.sin(a))
    px, py = _center(part) if centered else (0.0, 0.0)
    bx, by = _center(board) if centered else (0.0, 0.0)
    # Pin 1 on pin 1, when both mark it: a two-pad diode's pads fit either way round.
    pin1 = any(b.pin1 for b in board) and any(p.pin1 for p in part)
    free = list(board)
    for p in part:
        x, y = p.x - px, p.y - py
        x, y = x * c - y * s + bx, x * s + y * c + by
        match = next((b for b in free if math.hypot(b.x - x, b.y - y) <= PAD_FIT_MM), None)
        if match is None or (pin1 and match.pin1 != p.pin1):
            return False
        free.remove(match)
    return True
