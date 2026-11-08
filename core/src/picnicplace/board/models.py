"""Boards: what goes where on one PCB design.

**Board frame.** Looking down on the board's top side: origin at the bottom-left corner
of the board outline's bounding box, +X right, +Y up, millimeters. Every placement,
top or bottom, is given in this frame, and its rotation is the footprint's orientation
as the board designer set it (counterclockwise, seen from the top), so a board here
reads like the design file.

When a board is loaded bottom side up, the machine sees it flipped left to right: a
bottom-side placement at (x, y) with rotation r is at (width - x, y) with rotation
180 - r in the flipped board's frame, and its footprint is drawn unmirrored, seen
looking at the part. ``placed_frame`` does that conversion.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.library.models import Size

Side = Literal["top", "bottom"]


class BoardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Placement(BoardModel):
    ref: str = Field(description="Reference designator, such as 'C1'.")
    kind: Literal["part", "fiducial"] = "part"
    part_id: str | None = Field(
        description="Library part ID, usually the manufacturer part number. None if unknown."
    )
    value: str = ""
    footprint_id: str = Field(description="Library footprint ID (the footprint's name).")
    x: float
    y: float
    rotation: float = Field(description="Degrees counterclockwise, seen from the top.")
    side: Side
    enabled: bool = True
    note: str = Field(default="", description="Why the placement is disabled, if it is.")


class Board(BoardModel):
    name: str
    size: Size = Field(description="The outline's bounding box.")
    thickness: float | None = Field(default=None, description="Board thickness, mm.")
    source: str = ""
    placements: list[Placement] = Field(default_factory=list[Placement])

    def sides(self) -> set[Side]:
        return {p.side for p in self.placements if p.enabled and p.kind == "part"}


def normalize_angle(degrees: float) -> float:
    """Into (-180, 180]."""
    a = degrees % 360.0
    if a > 180.0:
        a -= 360.0
    return a + 0.0


def placed_frame(placement: Placement, board_width: float) -> tuple[float, float, float]:
    """The placement's (x, y, rotation) seen from the side it is on: unchanged for top
    placements; mirrored left to right for bottom ones (the board flipped about its
    vertical center line)."""
    if placement.side == "top":
        return placement.x, placement.y, normalize_angle(placement.rotation)
    return board_width - placement.x, placement.y, normalize_angle(180.0 - placement.rotation)
