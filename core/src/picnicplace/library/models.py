"""The parts library: footprints, parts, and how each part sits in its tape (spec 7.6).

**Footprint frame.** A footprint is drawn looking down on the part's own top side, as
the board designer's footprint is at 0 degrees: origin at the placement point (the
centroid in a placement file), +X right, +Y up, angles counterclockwise. Pads are given
in that frame. A placement's rotation turns this frame on the board.

**Tape frame (EIA-481).** Looking down on the cover tape, sprocket holes along the top,
the tape feeding left to right. A part's ``tape.rotation`` is its footprint frame's
angle in that view, counterclockwise. A feeder knows the tape frame's angle on the
machine, so the angle of a part waiting in its pocket is the feeder's angle plus the
part's rotation in tape, with no per-feeder adjustment.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Basis = Literal["datasheet", "standard", "observed", "measured", "estimated", "unknown"]
"""Where a value came from:

- ``datasheet``: the manufacturer's document states it for this part
- ``standard``: the usual value for the size and packaging, not confirmed for this part
- ``observed``: seen on the real part or reel
- ``measured``: measured, for example by the machine
- ``estimated``: a guess, such as a body size taken from a footprint's outline
- ``unknown``: not known; check before relying on it
"""

ID = Field(min_length=1, max_length=200)


class LibraryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Size(LibraryModel):
    x: float = Field(gt=0, description="Along the footprint's X axis, mm.")
    y: float = Field(gt=0, description="Along the footprint's Y axis, mm.")


class Pad(LibraryModel):
    name: str = Field(description="Pad number or name, such as '1' or 'EP'.")
    x: float = Field(description="Pad center in the footprint frame, mm.")
    y: float
    width: float = Field(gt=0, description="Size along X before rotation, mm.")
    height: float = Field(gt=0, description="Size along Y before rotation, mm.")
    rotation: float = Field(default=0.0, description="Degrees counterclockwise.")
    roundness: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description=(
            "Corner rounding as a percentage of the shorter side: 0 is a rectangle, "
            "100 a circle or oval."
        ),
    )


class Footprint(LibraryModel):
    """The shape of a part as seen from above: its pads and body."""

    id: str = ID
    description: str = ""
    body: Size | None = Field(
        default=None, description="Body outline, centered on the origin, if known."
    )
    body_basis: Basis = "unknown"
    pads: list[Pad] = Field(default_factory=list[Pad])
    source: str = Field(default="", description="Where this footprint came from.")


TapeKind = Literal["paper", "embossed", "unknown"]
Quadrant = Literal["Q1", "Q2", "Q3", "Q4"]


class Tape(LibraryModel):
    """How a part is packaged in carrier tape (EIA-481)."""

    kind: TapeKind = "unknown"
    width: float | None = Field(
        default=None, gt=0, description="Tape width, mm: 8, 12, 16, 24, ... None if not known."
    )
    pitch: float | None = Field(
        default=None, gt=0, description="Pocket pitch (P1), mm: 2, 4, 8, ... None if not known."
    )
    rotation: float = Field(
        default=0.0,
        description=(
            "The footprint frame's angle in the tape frame, degrees counterclockwise "
            "(sprocket holes along the top, feeding left to right)."
        ),
    )
    pin1_quadrant: Quadrant | None = Field(
        default=None,
        description=(
            "Pin 1's quadrant in the tape frame, as in tape-and-reel datasheets: "
            "Q1 top left, Q2 top right, Q3 bottom left, Q4 bottom right."
        ),
    )
    basis: Basis = "unknown"
    source: str = ""


class Part(LibraryModel):
    """A part number: what it is, its footprint, its height, and its tape."""

    id: str = Field(
        min_length=1,
        max_length=200,
        description="Usually the manufacturer part number, as in the board's BOM.",
    )
    name: str = Field(default="", description="Short name or value, such as '100nF'.")
    manufacturer: str = ""
    description: str = ""
    footprint_id: str = ID
    height: float | None = Field(
        default=None, ge=0, description="Height above the board, mm. None if not known."
    )
    height_basis: Basis = "unknown"
    height_source: str = ""
    tape: Tape | None = None
    speed: float = Field(
        default=1.0, gt=0, le=1, description="Fraction of full speed when carrying this part."
    )
