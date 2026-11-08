"""The bring-up record: how far the owner has got checking a new machine, and what they
found.

The Studio's bring-up checklist keeps its progress here (``bring-up.json`` in the data
directory), so it survives restarts and any client sees it. Findings that belong in the
machine profile or config (an axis direction, a measured keep-out) are recorded as
values; the Studio turns them into TOML for the owner to copy, so the core never edits
those files itself.
"""

import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.storage import write_atomic


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class StepRecord(_Model):
    status: Literal["todo", "done", "problem", "skipped"] = "todo"
    note: str = Field(default="", max_length=4000)
    values: dict[str, float | str | bool] = Field(
        default_factory=dict[str, float | str | bool],
        description="What the step found, such as which way +X moved the head.",
    )
    updated: datetime | None = None


class RecordedPoint(_Model):
    """A place on the machine, recorded by centering the top camera on it."""

    label: str = Field(min_length=1, max_length=200)
    x: float
    y: float


class BringUpRecord(_Model):
    format: Literal["picnicplace-bring-up"] = "picnicplace-bring-up"
    version: Literal[1] = 1
    steps: dict[str, StepRecord] = Field(default_factory=dict[str, StepRecord])
    points: list[RecordedPoint] = Field(default_factory=list[RecordedPoint], max_length=500)


class BringUpStore:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()

    def get(self) -> BringUpRecord:
        if self._path is None or not self._path.is_file():
            return BringUpRecord()
        return BringUpRecord.model_validate_json(self._path.read_text())

    def put(self, record: BringUpRecord) -> None:
        if self._path is None:
            return
        with self._lock:
            write_atomic(self._path, record.model_dump_json(indent=2))
