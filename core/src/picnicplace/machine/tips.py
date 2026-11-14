"""Nozzle tips (spec §7.7): which tip is fitted on each nozzle.

The profile lists the kinds of tip a machine can be fitted with (``nozzle_tips``). Which
one is on each nozzle changes at the bench, so it is kept apart from the profile, in
``nozzle-tips.json`` in the data directory, and set by the operator when they change a
tip. A nozzle's Z offset and vacuum levels depend on its tip, so they are measured, kept,
and used for each nozzle and tip (see ``calibration.py``): fitting a tip measured before
brings its values back, and fitting one not measured yet asks for the nozzle offsets to
be calibrated again.
"""

import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.storage import write_atomic


class FittedTips(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["picnicplace-nozzle-tips"] = "picnicplace-nozzle-tips"
    version: Literal[1] = 1
    fitted: dict[str, str] = Field(
        default_factory=dict[str, str], description="The tip on each nozzle, by nozzle ID."
    )


class TipStore:
    """Which tip is on each nozzle, kept across restarts. With no path (tests), it is
    kept in memory."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._memory = FittedTips()

    def get(self) -> dict[str, str]:
        if self._path is None:
            return dict(self._memory.fitted)
        if not self._path.is_file():
            return {}
        try:
            return FittedTips.model_validate_json(self._path.read_text()).fitted
        except ValueError:
            return {}

    def put(self, nozzle_id: str, tip_id: str | None) -> None:
        with self._lock:
            fitted = self.get()
            if tip_id is None:
                fitted.pop(nozzle_id, None)
            else:
                fitted[nozzle_id] = tip_id
            record = FittedTips(fitted=fitted)
            if self._path is None:
                self._memory = record
            else:
                write_atomic(self._path, record.model_dump_json(indent=2))
