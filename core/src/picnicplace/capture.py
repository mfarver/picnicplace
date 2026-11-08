"""Camera captures: full-resolution frames saved with what the machine was doing.

Captures are for tuning vision on real images. Each one is a lossless PNG and a JSON
file beside it, under ``captures/<date>/`` in the data directory, so a session's
images can be copied off the machine and used as test cases.
"""

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import cv2

from picnicplace.camera.source import Frame
from picnicplace.schemas import WireModel

CAPTURE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}/[A-Za-z0-9._-]+$")


class CaptureInfo(WireModel):
    id: str
    """``<date>/<name>``: the files are ``<id>.png`` and ``<id>.json``."""
    camera: str
    label: str
    taken: datetime
    width: int
    height: int
    position: dict[str, float]
    """Axis positions when the frame was taken."""
    outputs: dict[str, bool]
    """Which lights, pumps, and valves were on."""
    homed: bool


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:60]


class CaptureStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def save(
        self,
        frame: Frame,
        camera: str,
        label: str,
        position: dict[str, float],
        outputs: dict[str, bool],
        homed: bool,
    ) -> CaptureInfo:
        taken = datetime.now(UTC)
        h, w = frame.image.shape[:2]
        stem = "-".join(
            part for part in (taken.strftime("%H%M%S-%f")[:-3], camera, _slug(label)) if part
        )
        info = CaptureInfo(
            id=f"{taken:%Y-%m-%d}/{stem}",
            camera=camera,
            label=label,
            taken=taken,
            width=w,
            height=h,
            position=position,
            outputs=outputs,
            homed=homed,
        )
        with self._lock:
            png, meta = self.paths(info.id)
            png.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(png), frame.image):
                raise OSError(f"could not write {png}")
            meta.write_text(info.model_dump_json(indent=2))
        return info

    def paths(self, capture_id: str) -> tuple[Path, Path]:
        if not CAPTURE_ID.match(capture_id):
            raise ValueError(f"invalid capture ID {capture_id!r}")
        base = self.directory / capture_id
        return base.with_suffix(".png"), base.with_suffix(".json")

    def list(self, limit: int = 50) -> list[CaptureInfo]:
        if not self.directory.is_dir():
            return []
        found: list[CaptureInfo] = []
        for meta in sorted(self.directory.glob("*/*.json"), reverse=True)[:limit]:
            try:
                found.append(CaptureInfo.model_validate(json.loads(meta.read_text())))
            except (ValueError, OSError):
                continue
        return found
