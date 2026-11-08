"""Carrier tape geometry from EIA-481 (embossed and punched paper tape).

Dimensions are the standard's nominal values, in millimeters, in the tape frame: looking
down on the cover tape, sprocket holes along the top edge, feeding left to right, +X
along the feed and +Y up (toward the sprocket holes).
"""

from dataclasses import dataclass

SPROCKET_PITCH = 4.0
"""P0: distance between sprocket holes."""
SPROCKET_DIAMETER = 1.5
"""D0: sprocket hole diameter."""
SPROCKET_EDGE = 1.75
"""E1: from the tape's top edge to the sprocket holes' centers."""
POCKET_TO_SPROCKET = 2.0
"""P2: along the tape, from a sprocket hole's center to the nearest pocket center."""

POCKET_LINE: dict[int, float] = {8: 3.5, 12: 5.5, 16: 7.5, 24: 11.5, 32: 14.2, 44: 20.2, 56: 26.2}
"""F: from the sprocket holes' center line to the pockets' center line, by tape width."""

STANDARD_WIDTHS = tuple(POCKET_LINE)


@dataclass(frozen=True)
class TapeGeometry:
    width: float
    pitch: float
    pocket_line: float
    """F for this width."""

    @property
    def sprocket_y(self) -> float:
        """Sprocket hole centers, measured down from the top edge (negative Y)."""
        return -SPROCKET_EDGE

    @property
    def pocket_y(self) -> float:
        """Pocket centers, measured from the top edge."""
        return -SPROCKET_EDGE - self.pocket_line

    def pocket_x(self, index: int) -> float:
        """Center of pocket ``index``, along the tape, from the first sprocket hole."""
        return POCKET_TO_SPROCKET + index * self.pitch


def geometry(width: float, pitch: float) -> TapeGeometry:
    """Standard geometry for a tape width. Raises ValueError for nonstandard widths."""
    key = round(width)
    if abs(width - key) > 0.01 or key not in POCKET_LINE:
        widths = ", ".join(str(w) for w in STANDARD_WIDTHS)
        raise ValueError(f"{width:g} mm is not a standard tape width ({widths})")
    return TapeGeometry(width=float(key), pitch=pitch, pocket_line=POCKET_LINE[key])
