"""The feeder setup: what this machine's owner has told the core about its feeders.

Kept in the data directory as ``feeders.json``:

- which part each feeder holds, by the feeder's own ID (a Photon feeder keeps its part
  when it moves to another slot, as its reel goes with it);
- each slot's pick correction: how far the true pick location is from the profile's
  nominal one, taught by eye or found from the feeder's fiducial;
- the feeder fiducial's offset from the pick location, once measured.
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
