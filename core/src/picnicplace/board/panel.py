"""Panels: copies of one board, and the panel's own fiducials.

A panel file (such as KiKit's output) repeats the board's footprints, with the same
references, once per copy. Matching each copy to the board recovers where it sits:
its offset and rotation in the panel frame (top view, origin at the panel outline's
bottom-left corner, +Y up). Fiducials that belong to no copy are the panel's own.
"""

import math

from pydantic import Field

from picnicplace.board.models import Board, BoardModel, Placement, normalize_angle
from picnicplace.library.models import Size

MATCH_MM = 0.01
MATCH_DEG = 0.01


class BoardCopy(BoardModel):
    id: str
    x: float = Field(description="Where the board's origin is, in the panel frame.")
    y: float
    rotation: float = Field(default=0.0, description="Degrees counterclockwise, from the top.")
    enabled: bool = True


class Panel(BoardModel):
    size: Size
    copies: list[BoardCopy]
    fiducials: list[Placement] = Field(
        default_factory=list[Placement], description="The panel's own, in the panel frame."
    )

    @classmethod
    def single(cls, board: Board) -> "Panel":
        """A "panel" of one board, for boards run on their own."""
        return cls(size=board.size, copies=[BoardCopy(id="1", x=0.0, y=0.0)])


def to_panel(copy: BoardCopy, x: float, y: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(copy.rotation)), math.sin(math.radians(copy.rotation))
    return copy.x + c * x - s * y, copy.y + s * x + c * y


def find_copies(board: Board, panel: Board) -> tuple[Panel, list[str]]:
    """Recover the board copies in a panel from matching references.
    Returns the panel and warnings about anything that did not match."""
    warnings: list[str] = []
    by_ref: dict[str, list[Placement]] = {}
    for p in panel.placements:
        by_ref.setdefault(p.ref, []).append(p)
    anchor = next(
        (p for p in board.placements if p.kind == "part" and p.ref in by_ref),
        next((p for p in board.placements if p.ref in by_ref), None),
    )
    if anchor is None:
        raise ValueError("the panel has none of the board's references")

    copies: list[BoardCopy] = []
    for twin in by_ref[anchor.ref]:
        rotation = normalize_angle(twin.rotation - anchor.rotation)
        c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        copies.append(
            BoardCopy(
                id="",
                x=round(twin.x - (c * anchor.x - s * anchor.y), 4) + 0.0,
                y=round(twin.y - (s * anchor.x + c * anchor.y), 4) + 0.0,
                rotation=rotation,
            )
        )

    # Number the copies by their centers, from the top left, row by row.
    def center(c: BoardCopy) -> tuple[float, float]:
        x, y = to_panel(c, board.size.x / 2, board.size.y / 2)
        return -round(y, 1), round(x, 1)

    copies.sort(key=center)
    for n, copy in enumerate(copies, start=1):
        copy.id = str(n)

    used: set[int] = set()
    for copy in copies:
        missing: list[str] = []
        for p in board.placements:
            x, y = to_panel(copy, p.x, p.y)
            angle = normalize_angle(p.rotation + copy.rotation)
            match = next(
                (
                    q
                    for q in by_ref.get(p.ref, [])
                    if id(q) not in used
                    and math.hypot(q.x - x, q.y - y) < MATCH_MM
                    and abs(normalize_angle(q.rotation - angle)) < MATCH_DEG
                    and q.side == p.side
                ),
                None,
            )
            if match is None:
                missing.append(p.ref)
            else:
                used.add(id(match))
        if missing:
            warnings.append(
                f"board copy {copy.id}: {len(missing)} placement(s) not found in the panel: "
                + ", ".join(missing[:8])
                + ("..." if len(missing) > 8 else "")
            )
    fiducials = [p for p in panel.placements if p.kind == "fiducial" and id(p) not in used]
    extra = [
        p.ref for p in panel.placements if p.kind == "part" and p.enabled and id(p) not in used
    ]
    if extra:
        warnings.append("panel placements that belong to no board copy: " + ", ".join(extra[:8]))
    return Panel(size=panel.size, copies=copies, fiducials=fiducials), warnings
