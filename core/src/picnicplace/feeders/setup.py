"""The feeder setup: what this machine's owner has told the core about its feeders.

Kept in the data directory as ``feeders.json``:

- which part each feeder holds, by the feeder's own ID (a Photon feeder keeps its part
  when it moves to another slot, as its reel goes with it);
- each slot's pick correction: how far the true pick location is from the profile's
  nominal one, taught by eye or found from the feeder's fiducial;
- the feeder fiducial's offset from the pick location, once measured;
- fixed feeders: strips of tape and trays held on the bed, with how many parts have
  been taken from each.
"""

import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.machine.profile import Point
from picnicplace.storage import write_atomic


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class FeederAssignment(_Model):
    part_id: str | None = Field(default=None, description="Library part in this feeder.")
    part_pitch: float | None = Field(
        default=None,
        gt=0,
        description="Distance from one part to the next. Empty: the part's tape pitch.",
    )
    enabled: bool = True
    note: str = ""


class SlotCalibration(_Model):
    correction: Point = Field(description="True pick location minus the nominal one.")
    basis: Literal["taught", "fiducial"] = Field(
        description="Taught by eye over the pocket, or found from the feeder's fiducial."
    )


class FixedFeeder(_Model):
    """A strip of tape or a tray of parts held in place on the bed. Parts are taken in
    order: along the first row, then the next row, and so on."""

    label: str = Field(default="", description="What the owner calls it, such as '24 mm holder'.")
    kind: Literal["strip", "tray"] = Field(
        description=(
            "A strip of tape (cover film peeled back), or a tray of parts. A strip's "
            "parts take their angle from the tape; a tray's from part_angle."
        )
    )
    part_id: str | None = Field(default=None, description="Library part it holds.")
    first: Point = Field(description="Center of the first part, in machine coordinates.")
    step: Point = Field(
        description=(
            "From one part to the next along a row, in mm. On a strip, from one pocket to "
            "the next one to pick: against the direction the tape would feed."
        )
    )
    columns: int = Field(ge=1, le=1000, description="Parts in each row.")
    rows: int = Field(default=1, ge=1, le=1000)
    row_step: Point = Field(
        default_factory=lambda: Point(x=0, y=0),
        description="From the first part of one row to the first part of the next.",
    )
    part_angle: float = Field(
        default=0,
        description=(
            "Trays: the machine angle of the part's 0 degree direction (its footprint's "
            "+X), the same for every part."
        ),
    )
    pick_height: float | None = Field(
        default=None,
        description=(
            "Nozzle height at the top of a part. Empty: the board height plus the part's "
            "height, which is right only for parts lying on the bed itself."
        ),
    )
    picked: int = Field(default=0, ge=0, description="Parts taken (or tried) so far.")
    enabled: bool = True

    @property
    def capacity(self) -> int:
        return self.columns * self.rows

    @property
    def remaining(self) -> int:
        return max(0, self.capacity - self.picked)

    def position(self, index: int) -> Point:
        """Where part ``index`` (from 0) lies."""
        column, row = index % self.columns, index // self.columns
        return Point(
            x=self.first.x + column * self.step.x + row * self.row_step.x,
            y=self.first.y + column * self.step.y + row * self.row_step.y,
        )

    def corners(self) -> list[Point]:
        """The first and last parts of the first and last rows."""
        last_row = (self.rows - 1) * self.columns
        indexes = {0, self.columns - 1, last_row, last_row + self.columns - 1}
        return [self.position(i) for i in sorted(indexes)]


class FeederSetup(_Model):
    format: Literal["picnicplace-feeders"] = "picnicplace-feeders"
    version: Literal[1] = 1
    feeders: dict[str, FeederAssignment] = Field(
        default_factory=dict[str, FeederAssignment], description="By feeder ID."
    )
    slots: dict[str, SlotCalibration] = Field(
        default_factory=dict[str, SlotCalibration], description="By slot ID."
    )
    fiducial_offset: Point | None = Field(
        default=None,
        description=(
            "Measured offset of a feeder's fiducial from its pick location, in the tape's "
            "frame. Replaces the profile's value."
        ),
    )
    fixed: dict[str, FixedFeeder] = Field(
        default_factory=dict[str, FixedFeeder],
        description="Strips and trays on the bed, by ID: T1, T2, and so on.",
    )


class FeederSetupStore:
    """Load and save the setup."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.setup = self._load()

    def _load(self) -> FeederSetup:
        if self._path is None or not self._path.is_file():
            return FeederSetup()
        return FeederSetup.model_validate_json(self._path.read_text())

    def save(self) -> None:
        if self._path is None:
            return
        with self._lock:
            write_atomic(self._path, self.setup.model_dump_json(indent=2))
