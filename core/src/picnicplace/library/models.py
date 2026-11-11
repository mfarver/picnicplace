"""The parts library: footprints, parts, and how each part sits in its tape (spec 7.6).

**Footprint frame.** A footprint is drawn looking down on the part's own top side, as
the board designer's footprint is at 0 degrees: origin at the placement point (the
centroid in a placement file), +X right, +Y up, angles counterclockwise. Pads are given
in that frame. A placement's rotation turns this frame on the board.

**Tape frame (EIA-481).** Looking down on the cover tape, sprocket holes along the top,
the tape feeding left to right. A tape's ``rotation`` is the footprint frame's angle in
that view, counterclockwise. A feeder knows the tape frame's angle on the machine, so
the angle of a part waiting in its pocket is the feeder's angle plus the rotation in
tape, with no per-feeder adjustment.

**Tapes are records of their own.** Many parts come in the same tape the same way (every
0603 resistor and capacitor on 8 mm paper at 4 mm pitch, say), so a part links to a
tape by ID rather than carrying its own copy, and fixing one tape fixes every part on
it. A tape is not tied to a footprint: the same footprint can come packaged differently
from different makers, and different footprints can share a tape.

**Pin 1.** A footprint can mark one pad as pin 1 (``Pad.pin1``). The mark is only for
drawing (the tape preview, outlines over the camera); vision never looks at it.
"""

import math
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
    pin1: bool = Field(
        default=False,
        description="The pad drawn as pin 1. Drawing only: vision ignores it.",
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
    """How parts are packaged in carrier tape (EIA-481). Parts link to it by ID."""

    id: str = ID
    name: str = Field(default="", description="A short name, such as '8 mm paper, 4 mm'.")
    kind: TapeKind = "unknown"
    width: float | None = Field(
        default=None, gt=0, description="Tape width, mm: 8, 12, 16, 24, ... None if not known."
    )
    pitch: float | None = Field(
        default=None, gt=0, description="Pocket pitch (P1), mm: 2, 4, 8, ... None if not known."
    )
    rotation: float | None = Field(
        default=None,
        description=(
            "The footprint frame's angle in the tape frame, degrees counterclockwise "
            "(sprocket holes along the top, feeding left to right). None if not known."
        ),
    )
    basis: Basis = "unknown"
    source: str = ""
    notes: str = Field(default="", description="Free text. Feeders use only the values above.")


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
    tape_id: str | None = Field(default=None, description="The tape it comes in, if known.")
    speed: float = Field(
        default=1.0, gt=0, le=1, description="Fraction of full speed when carrying this part."
    )


def with_pin1(pads: list[Pad]) -> list[Pad]:
    """Pads with pin 1 marked, as a suggestion when importing: the pad named '1', or else
    'A1' (a BGA). Pads that already mark one are left as they are."""
    if any(p.pin1 for p in pads):
        return pads
    for name in ("1", "A1"):
        for n, pad in enumerate(pads):
            if pad.name.strip().upper() == name:
                return [*pads[:n], pad.model_copy(update={"pin1": True}), *pads[n + 1 :]]
    return pads


def standard_tape(
    kind: TapeKind, width: float | None, pitch: float | None, rotation: float | None
) -> Tape:
    """A tape with an ID and name made from its values, so the same values from different
    sources (an import, an older library) become the same tape."""

    def num(v: float | None) -> str:
        return "na" if v is None else f"{v:g}"

    kind_word = "" if kind == "unknown" else f" {kind}"
    turn = "" if rotation is None else f", {rotation:g}°"
    width_text = "? mm" if width is None else f"{width:g} mm"
    pitch_text = "" if pitch is None else f", {pitch:g} mm pitch"
    return Tape(
        id=f"{num(width)}mm-{kind}-p{num(pitch)}-r{num(rotation)}",
        name=f"{width_text}{kind_word}{pitch_text}{turn}",
        kind=kind,
        width=width,
        pitch=pitch,
        rotation=rotation,
    )


def pin1_quadrant(footprint: Footprint, rotation: float) -> Quadrant | None:
    """Which quadrant of its pocket a footprint's pin-1 pad lands in, in the tape frame,
    turned by ``rotation`` (Q1 top left, Q2 top right, Q3 bottom left, Q4 bottom right, as
    tape-and-reel drawings say). None without exactly one marked pad off both center
    lines."""
    marked = [p for p in footprint.pads if p.pin1]
    if len(marked) != 1:
        return None
    a = math.radians(rotation)
    x = marked[0].x * math.cos(a) - marked[0].y * math.sin(a)
    y = marked[0].x * math.sin(a) + marked[0].y * math.cos(a)
    if abs(x) < 1e-6 or abs(y) < 1e-6:
        return None
    if y > 0:
        return "Q1" if x < 0 else "Q2"
    return "Q3" if x < 0 else "Q4"


QUADRANT_ROTATION: dict[Quadrant, float] = {"Q1": 0.0, "Q2": -90.0, "Q3": 90.0, "Q4": 180.0}
"""For a footprint with pin 1 at the top left at 0 degrees: the rotation in tape that
puts pin 1 in each quadrant, as a tape-and-reel datasheet gives it."""
