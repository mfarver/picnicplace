"""The flight recorder: what the machine saw and did, for a user to send to developers.

A recording is a folder in the data directory's ``recordings/``:

- ``manifest.json``: what it is (format, version, PicnicPlace version, when, why), and how
  many frames and events it holds. It is kept up to date while recording (every few
  seconds), so a copy taken then says what it holds; ``ended_at`` stays empty until the
  recording stops. A recording the core never stopped (it crashed or was killed) is
  closed when the core next starts, with the outcome ``interrupted``;
- ``snapshot/``: the setup at the start: the config (without its token), the machine
  profile, the feeder setup, the parts and footprints in use, and the job, if any;
- ``events.jsonl``: one JSON object a line, ``t`` seconds after the start: everything the
  core told its clients (operations, machine state), its log, and what each task did
  (picks, alignments, placements, run progress);
- ``frames/``: every camera frame that vision looked at, losslessly, as PNG.

Each frame's ``vision`` event names the vision function, its arguments other than the
image, and what it answered, so :func:`replay_vision` can run today's vision code on
yesterday's real frames and say what changed. Recording never stops the machine: a
frame or an event that cannot be written is logged and dropped.
"""

import asyncio
import dataclasses
import json
import logging
import math
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import cv2
import numpy as np
from pydantic import BaseModel, Field

from picnicplace import __version__
from picnicplace.events import EventBus, Subscription, SubscriptionOverflowError
from picnicplace.schemas import WireModel
from picnicplace.vision.fiducial import find_circle, find_circles
from picnicplace.vision.focus import sharpness
from picnicplace.vision.pads import fit_pads
from picnicplace.vision.part import find_part

log = logging.getLogger(__name__)

FORMAT = "picnicplace-recording"
VERSION = 1

VISION_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "find_circle": find_circle,
    "find_circles": find_circles,
    "find_part": find_part,
    "fit_pads": fit_pads,
    "sharpness": sharpness,
}
"""The vision functions a recording can hold calls of, by name, for replaying them."""

MANIFEST_EVERY_S = 2.0
"""While recording, the manifest is rewritten at most this often."""

_ID = re.compile(r"^\d{8}-\d{6}(-[a-z0-9-]+)?$")


class RecordingInfo(WireModel):
    id: str
    label: str
    reason: Literal["manual", "run"]
    job_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    outcome: str | None = Field(default=None, description="How it ended, such as a run's.")
    frames: int = 0
    events: int = 0
    bytes: int = Field(default=0, description="Size on disk.")
    recording: bool = Field(default=False, description="True while it is still recording.")


class _Recording:
    def __init__(self, folder: Path, info: RecordingInfo) -> None:
        self.folder = folder
        self.info = info
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.events = (folder / "events.jsonl").open("a", encoding="utf-8")
        self.task: asyncio.Task[None] | None = None
        self.manifest_at = 0.0
        """When the manifest was last written (monotonic)."""

    def t(self) -> float:
        return round(time.monotonic() - self.start, 3)


class _LogToRecording(logging.Handler):
    def __init__(self, recorder: "FlightRecorder") -> None:
        super().__init__(logging.INFO)
        self._recorder = recorder

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return  # the recorder's own trouble goes to the console only
        self._recorder.event(
            "log", level=record.levelname, logger=record.name, message=record.getMessage()
        )


class FlightRecorder:
    def __init__(
        self,
        root: Path | None,
        bus: EventBus | None = None,
        keep: int = 10,
        max_bytes: int = 5_000_000_000,
    ) -> None:
        self._root = root
        self._bus = bus
        self._keep = keep
        self._max_bytes = max_bytes
        self._current: _Recording | None = None
        self._handler = _LogToRecording(self)
        self.snapshot: Callable[[str | None], dict[str, Any]] | None = None
        """Builds the setup to keep at the start, by file name (for a job ID, if any)."""
        self._frame_count = 0
        self._close_interrupted()

    # --- Recording -------------------------------------------------------------------

    @property
    def active(self) -> RecordingInfo | None:
        return self._info(self._current) if self._current else None

    @property
    def available(self) -> bool:
        return self._root is not None

    def start(
        self, label: str, reason: Literal["manual", "run"] = "manual", job_id: str | None = None
    ) -> RecordingInfo:
        if self._root is None:
            raise RecorderError("recordings need a data directory")
        if self._current is not None:
            raise RecorderError(f"already recording ({self._current.info.label})")
        now = datetime.now(UTC)
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
        rid = now.strftime("%Y%m%d-%H%M%S") + (f"-{slug}" if slug else "")
        folder = self._root / rid
        n = 2
        while folder.exists():
            folder = self._root / f"{rid}-{n}"
            n += 1
        (folder / "frames").mkdir(parents=True)
        (folder / "snapshot").mkdir()
        info = RecordingInfo(
            id=folder.name,
            label=label or "recording",
            reason=reason,
            job_id=job_id,
            started_at=now,
            recording=True,
        )
        if self.snapshot is not None:
            try:
                for name, data in self.snapshot(job_id).items():
                    text = json.dumps(data, indent=2, default=str)
                    (folder / "snapshot" / name).write_text(text, encoding="utf-8")
            except Exception:
                log.exception("could not save the setup with the recording")
        self._current = _Recording(folder, info)
        self._frame_count = 0
        self._write_manifest(self._current)
        logging.getLogger("picnicplace").addHandler(self._handler)
        if self._bus is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                # Subscribe now, not when the task first runs, so no event is missed.
                sub = self._bus.subscribe()
                self._current.task = loop.create_task(self._follow_bus(self._bus, sub))
        log.info("recording %s", folder.name)
        self.event("recording.start", label=info.label, reason=reason, job_id=job_id)
        return self._info(self._current)

    def stop(self, outcome: str | None = None) -> RecordingInfo | None:
        rec = self._current
        if rec is None:
            return None
        self.event("recording.stop", outcome=outcome)
        self._current = None
        logging.getLogger("picnicplace").removeHandler(self._handler)
        if rec.task is not None:
            rec.task.cancel()
        with rec.lock:
            rec.events.close()
        rec.info = rec.info.model_copy(
            update={"ended_at": datetime.now(UTC), "outcome": outcome, "recording": False}
        )
        self._write_manifest(rec)
        log.info("recording %s stopped", rec.folder.name)
        self._prune()
        return self.get(rec.info.id)

    def event(self, kind: str, **data: Any) -> None:
        """Note something in the active recording, if any."""
        rec = self._current
        if rec is None:
            return
        line = json.dumps({"t": rec.t(), "kind": kind, **_jsonable(data)}, default=str)
        try:
            with rec.lock:
                if not rec.events.closed:
                    rec.events.write(line + "\n")
                    rec.events.flush()
                    rec.info.events += 1
                    if time.monotonic() - rec.manifest_at >= MANIFEST_EVERY_S:
                        self._write_manifest(rec)
        except OSError as e:
            log.warning("could not write to the recording: %s", e)

    async def vision(
        self,
        name: str,
        image: np.ndarray,
        *args: Any,
        camera: str,
        position: dict[str, float] | None = None,
        **context: Any,
    ) -> Any:
        """Run a vision function on a frame, keeping the frame, the call, and its answer
        if recording."""
        fn = VISION_FUNCTIONS[name]
        result = await asyncio.to_thread(fn, image, *args)
        rec = self._current
        if rec is not None:
            self._frame_count += 1
            path = f"frames/{self._frame_count:05d}-{camera}-{name}.png"
            try:
                await asyncio.to_thread(_write_png, rec.folder / path, image)
                rec.info.frames += 1
            except (OSError, cv2.error) as e:
                log.warning("could not save a frame with the recording: %s", e)
                path = None  # type: ignore[assignment]
            self.event(
                "vision",
                fn=name,
                camera=camera,
                frame=path,
                args=list(args),
                position=position,
                result=result,
                **context,
            )
        return result

    async def _follow_bus(self, bus: EventBus, sub: Subscription) -> None:
        try:
            while True:
                try:
                    event = await sub.get()
                except SubscriptionOverflowError:
                    self.event("recording.gap", reason="fell behind the core's events")
                    bus.unsubscribe(sub)
                    sub = bus.subscribe()
                    continue
                data = event.model_dump(mode="json")
                kind = data.pop("type", "event")
                self.event(f"core.{kind}", **data)
        finally:
            bus.unsubscribe(sub)

    # --- Kept recordings -------------------------------------------------------------

    def list(self) -> list[RecordingInfo]:
        if self._root is None or not self._root.is_dir():
            return []
        found = [self.get(p.name) for p in self._root.iterdir() if (p / "manifest.json").is_file()]
        return sorted(found, key=lambda r: r.started_at, reverse=True)

    def get(self, recording_id: str) -> RecordingInfo:
        folder = self._folder(recording_id)
        if self._current is not None and self._current.folder == folder:
            return self._info(self._current)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        info = RecordingInfo.model_validate(manifest["recording"])
        return info.model_copy(update={"bytes": _size(folder)})

    def delete(self, recording_id: str) -> None:
        folder = self._folder(recording_id)
        if self._current is not None and self._current.folder == folder:
            raise RecorderError("stop the recording before deleting it")
        shutil.rmtree(folder)
        folder.with_suffix(".zip").unlink(missing_ok=True)

    def zip(self, recording_id: str) -> Path:
        """The recording as one zip file, made once and kept beside it."""
        folder = self._folder(recording_id)
        if self._current is not None and self._current.folder == folder:
            raise RecorderError("stop the recording before downloading it")
        out = folder.with_suffix(".zip")
        if out.is_file():
            return out
        with tempfile.NamedTemporaryFile(dir=folder.parent, suffix=".part", delete=False) as tmp:
            part = Path(tmp.name)
        try:
            with zipfile.ZipFile(part, "w") as z:
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        stored = path.suffix == ".png"  # already compressed
                        z.write(
                            path,
                            f"{folder.name}/{path.relative_to(folder)}",
                            zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
                        )
            part.replace(out)
        finally:
            part.unlink(missing_ok=True)
        return out

    def _folder(self, recording_id: str) -> Path:
        if self._root is None or not _ID.match(recording_id):
            raise RecorderError(f"no recording {recording_id!r}")
        folder = self._root / recording_id
        if not (folder / "manifest.json").is_file():
            raise RecorderError(f"no recording {recording_id!r}")
        return folder

    def _info(self, rec: _Recording) -> RecordingInfo:
        return rec.info.model_copy(update={"bytes": _size(rec.folder)})

    def _write_manifest(self, rec: _Recording) -> None:
        rec.manifest_at = time.monotonic()
        _save_manifest(rec.folder, rec.info)

    def _close_interrupted(self) -> None:
        """Close recordings the core never stopped: count what they hold, and say how
        they ended."""
        if self._root is None or not self._root.is_dir():
            return
        for folder in self._root.iterdir():
            path = folder / "manifest.json"
            if not path.is_file():
                continue
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                info = RecordingInfo.model_validate(manifest["recording"])
            except (OSError, ValueError, KeyError) as e:
                log.warning("could not read recording %s: %s", folder.name, e)
                continue
            if info.ended_at is not None:
                continue
            events, last_t = _count_events(folder / "events.jsonl")
            frames = sum(1 for _ in (folder / "frames").glob("*.png"))
            ended = info.started_at + timedelta(seconds=last_t)
            info = info.model_copy(
                update={
                    "ended_at": ended,
                    "outcome": "interrupted",
                    "frames": frames,
                    "events": events,
                    "recording": False,
                }
            )
            try:
                _save_manifest(folder, info)
            except OSError as e:
                log.warning("could not close recording %s: %s", folder.name, e)
                continue
            log.info("closed recording %s, which was interrupted", folder.name)

    def _prune(self) -> None:
        """Keep the newest recordings, at most ``keep`` of them and ``max_bytes`` in all."""
        total = 0
        for i, info in enumerate(self.list()):
            if info.recording:
                continue
            total += info.bytes
            if i >= self._keep or total > self._max_bytes:
                log.info("deleting old recording %s", info.id)
                self.delete(info.id)


class RecorderError(Exception):
    pass


def _save_manifest(folder: Path, info: RecordingInfo) -> None:
    """Write a recording's manifest whole: to a new file, then renamed over the old one,
    so whoever reads or copies it never sees half of it."""
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "picnicplace": __version__,
        "recording": info.model_dump(mode="json", exclude={"bytes", "recording"}),
    }
    part = folder / "manifest.json.part"
    part.write_text(json.dumps(manifest, indent=2), "utf-8")
    part.replace(folder / "manifest.json")


def _read_events(path: Path) -> Generator[dict[str, Any]]:
    """A recording's timeline, skipping any line cut short when the core stopped."""
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield cast(dict[str, Any], event)
    except OSError:
        return


def _count_events(path: Path) -> tuple[int, float]:
    """How many whole events a recording's timeline holds, and the time of the last."""
    count, last = 0, 0.0
    for event in _read_events(path):
        count += 1
        last = max(last, float(event.get("t", 0.0)))
    return count, last


def _write_png(path: Path, image: np.ndarray) -> None:
    ok, data = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise OSError(f"could not encode {path.name}")
    path.write_bytes(data.tobytes())


def _size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def _jsonable(value: Any) -> Any:
    """Plain JSON for dataclasses, Pydantic models, tuples, and numpy numbers."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        items: dict[Any, Any] = value  # type: ignore[assignment]
        return {str(k): _jsonable(v) for k, v in items.items()}
    if isinstance(value, (list, tuple)):
        seq: list[Any] | tuple[Any, ...] = value  # type: ignore[assignment]
        return [_jsonable(v) for v in seq]
    if isinstance(value, np.generic):
        return float(value) if isinstance(value, np.floating) else int(value)  # type: ignore[arg-type]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# --- Replaying vision ------------------------------------------------------------------


class VisionCheck(WireModel):
    frame: str
    fn: str
    recorded: Any
    now: Any
    same: bool
    difference: float | None = Field(
        default=None, description="Largest difference in any number of the answer."
    )


def replay_vision(source: Path, tolerance: float = 0.01) -> list[VisionCheck]:
    """Run each vision call in a recording (its folder, or its zip) again with today's
    code, and compare the answers."""
    with _opened(source) as folder:
        checks: list[VisionCheck] = []
        for event in _read_events(folder / "events.jsonl"):
            if event.get("kind") != "vision" or not event.get("frame"):
                continue
            fn = VISION_FUNCTIONS.get(event["fn"])
            if fn is None:
                continue
            image = cv2.imread(str(folder / event["frame"]), cv2.IMREAD_UNCHANGED)
            if image is None:
                continue  # not saved whole: the core stopped while writing it
            now = _jsonable(fn(image, *event["args"]))
            diff = _difference(event["result"], now)
            checks.append(
                VisionCheck(
                    frame=event["frame"],
                    fn=event["fn"],
                    recorded=event["result"],
                    now=now,
                    same=diff is not None and diff <= tolerance,
                    difference=diff,
                )
            )
        return checks


@contextmanager
def _opened(source: Path) -> Generator[Path]:
    if source.is_dir():
        yield source
        return
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(source) as z:
        root = Path(tmp).resolve()
        for name in z.namelist():
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise RecorderError(f"the zip holds a path outside it: {name}")
        z.extractall(root)
        folders = [p for p in root.iterdir() if (p / "manifest.json").is_file()]
        if len(folders) != 1:
            raise RecorderError("the zip does not hold one recording")
        manifest = json.loads((folders[0] / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise RecorderError("not a PicnicPlace recording")
        yield folders[0]


def _difference(a: Any, b: Any) -> float | None:
    """The largest difference between the numbers of two answers of the same shape, or
    None when their shapes differ (one found something and the other did not)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return 0.0 if a == b else None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b))
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return None
        parts = [_difference(a[k], b[k]) for k in a]  # type: ignore[index]
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):  # type: ignore[arg-type]
            return None
        parts = [_difference(x, y) for x, y in zip(a, b, strict=True)]  # type: ignore[arg-type]
    else:
        return 0.0 if a == b else None
    if any(p is None for p in parts):
        return None
    return max((p for p in parts if p is not None), default=0.0)
