"""Training: how the bottom camera sees a part best, found by trying, and kept per part.

A part is picked and held over the bottom camera. The nozzle turns it to four angles, and
at each the camera takes frames under every light the machine's ring light can give
(white at several brightnesses and, on a light that takes a color, red, green and blue).
Each frame is then measured every way bottom vision can: by fitting the footprint's pads
to the metal, or by the part's outline, with the metal taken as whatever is brighter than
each of several fractions of how bright the part looks.

A way of seeing the part is judged by what the turns make known. The part turns with the
nozzle, so its angle on the nozzle and where it sits on it must come out the same in
every frame; a way that sees it at all is scored by how much they wander (``ANGLE_STEADY``,
``OFFSET_STEADY``), and by how well the pads fit, or the outline matches the footprint.
Only a way that found the part in every frame, the same way round each time, is chosen;
a part whose pads are not symmetric (a SOT-23's) is chosen a way that fits its pads if
any does, since an outline cannot say which way round it is.

What was chosen is kept for the part on this machine (its camera and light are its own),
in the data directory, and alignment uses it from then on. The part is then dropped in
the discard.
"""

import itertools
import math
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from pydantic import Field

from picnicplace.camera.source import Image
from picnicplace.library import Footprint
from picnicplace.machine.profile import WHITE, LightColor
from picnicplace.schemas import WireModel
from picnicplace.storage import write_atomic

Method = Literal["auto", "pads", "outline"]
"""How bottom vision finds a part: 'auto', its outline and then its pads, or the outline
alone when no metal shows; 'pads', only by fitting its pads; 'outline', only by its
outline."""

TURNS = (0.0, 90.0, 180.0, 270.0)
"""The nozzle turns the part to each of these, from where it was picked."""
FRAMES_PER_LIGHT = 2
"""Frames taken under each light at each turn: the same answer twice is steady."""
WHITE_LEVELS = (255, 180, 120, 80, 50)
"""Brightnesses of white light tried (of 255)."""
COLOR_LEVELS = (255, 120, 60)
"""Brightnesses of red, green and blue light tried, on a light that takes a color."""
THRESHOLDS: tuple[float | None, ...] = (None, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9)
"""Metal is what is brighter than this fraction of how bright the part looks
(``bright_level``); None: the fixed level untrained parts are seen with."""
ANGLE_STEADY = 0.5
"""Degrees the part's angle on the nozzle may wander across frames for one point."""
OFFSET_STEADY = 0.05
"""Millimeters its place on the nozzle may wander for one point."""
FIT_UNIT = 0.05
"""Millimeters of pad fit residual, or of outline size error, for one point."""
KEPT_CANDIDATES = 12
"""How many of the best ways of seeing a part are kept with it, to show."""
ZOOM = 3
"""The pictures shown for approval are this many times the camera's pixels."""


def lights_to_try(colored: bool) -> list[LightColor]:
    """The lights tried, white first: on a light that only switches on and off, white."""
    if not colored:
        return [WHITE]
    out = [LightColor(red=255, green=255, blue=255, brightness=b) for b in WHITE_LEVELS]
    for r, g, b in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
        out += [LightColor(red=r, green=g, blue=b, brightness=v) for v in COLOR_LEVELS]
    return out


def describe_light(light: LightColor | None) -> str:
    if light is None:
        return "its usual light"
    names = {
        (255, 255, 255): "white",
        (255, 0, 0): "red",
        (0, 255, 0): "green",
        (0, 0, 255): "blue",
    }
    name = names.get((light.red, light.green, light.blue))
    if name is None:
        name = f"color ({light.red}, {light.green}, {light.blue})"
    if light.brightness >= 255:
        return f"{name} light at full brightness"
    return f"{name} light at {round(light.brightness / 2.55)}%"


def describe_threshold(threshold: float | None) -> str:
    if threshold is None:
        return "the usual brightness for metal"
    return f"metal brighter than {round(threshold * 100)}% of the part"


class PartVision(WireModel):
    """How the bottom camera is to see a part."""

    light: LightColor | None = Field(
        default=None, description="The ring light's color and brightness; none: the usual."
    )
    threshold: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        description=(
            "Metal is what is brighter than this fraction of how bright the part looks; "
            "none: a fixed level."
        ),
    )
    method: Method = "auto"


RESCUE_WAYS: tuple[PartVision, ...] = (
    PartVision(
        light=LightColor(red=255, green=255, blue=255, brightness=180),
        threshold=0.6,
        method="outline",
    ),
    PartVision(
        light=LightColor(red=0, green=255, blue=0, brightness=120), threshold=0.6, method="outline"
    ),
    PartVision(light=LightColor(red=0, green=0, blue=255, brightness=255), method="pads"),
    PartVision(
        light=LightColor(red=255, green=255, blue=255, brightness=120),
        threshold=0.75,
        method="auto",
    ),
    PartVision(
        light=LightColor(red=255, green=255, blue=255, brightness=255),
        threshold=0.8,
        method="pads",
    ),
)
"""Ways of seeing a part tried in turn, before it is discarded, when the usual (or trained)
one sees nothing: the part is already over the camera, so each costs a frame or two. They
are what training chose for the parts a first real board's run could not see (2026-11-20,
a LumenPnP): an LM63615 (WSON) by its outline under green light, a CD74HC4051 (TSSOP) and an
MMBTA06Q (SOT-23) by their pads under blue, and white at other brightnesses. On a light
that cannot take a color, those with a color are left out."""


class Candidate(WireModel):
    """One way of seeing a part, as training judged it."""

    vision: PartVision
    seen: int = Field(description="Frames it found the part in.")
    frames: int
    light_index: int | None = Field(
        default=None, description="Which light's thumbnail shows it (light-NN.jpg)."
    )
    angle_spread: float | None = Field(
        default=None, description="How far the part's angle on the nozzle wandered (degrees)."
    )
    offset_spread: float | None = Field(
        default=None, description="How far its place on the nozzle wandered (mm)."
    )
    fit: float | None = Field(
        default=None,
        description="Pad fit residual, or outline size error, the worst frame's (mm).",
    )
    flipped: bool = Field(
        default=False, description="Its pads showed the part turned 180 degrees from the tape."
    )
    score: float | None = Field(
        default=None, description="Lower is better; none: it did not find the part every time."
    )


class TrainedPart(WireModel):
    part_id: str
    vision: PartVision
    trained_at: datetime
    nozzle: str
    tip: str | None = None
    message: str
    candidates: list[Candidate] = Field(
        default_factory=list[Candidate], description="The best ways tried, best first."
    )
    tried: int = Field(default=0, description="How many ways of seeing it were tried.")
    recording: str | None = Field(default=None, description="The recording of the training.")
    approved: bool = Field(
        default=False,
        description=(
            "The operator saw the footprint line up with the part, the right way round; "
            "alignment uses a trained part's settings only once approved."
        ),
    )


class _Record(WireModel):
    format: Literal["picnicplace-trained-parts"] = "picnicplace-trained-parts"
    version: int = 1
    parts: dict[str, TrainedPart] = Field(default_factory=dict[str, TrainedPart])


class TrainedPartStore:
    """Trained parts, kept across restarts in the data directory; with no path (tests), in
    memory. Thumbnails of what the camera saw under each light go in ``frames_dir``."""

    def __init__(self, path: Path | None, frames_dir: Path | None = None) -> None:
        self._path = path
        self._frames = frames_dir
        self._lock = threading.Lock()
        self._memory = _Record()

    def _get(self) -> _Record:
        if self._path is None:
            return self._memory
        if not self._path.is_file():
            return _Record()
        try:
            return _Record.model_validate_json(self._path.read_text())
        except ValueError:
            return _Record()

    def _put(self, record: _Record) -> None:
        if self._path is None:
            self._memory = record
        else:
            write_atomic(self._path, record.model_dump_json(indent=2))

    def all(self) -> list[TrainedPart]:
        return sorted(self._get().parts.values(), key=lambda p: p.part_id)

    def get(self, part_id: str) -> TrainedPart | None:
        return self._get().parts.get(part_id)

    def vision(self, part_id: str | None) -> PartVision | None:
        """A part's approved settings, if any."""
        trained = self.get(part_id) if part_id else None
        return trained.vision if trained and trained.approved else None

    def approve(self, part_id: str) -> TrainedPart | None:
        with self._lock:
            record = self._get()
            trained = record.parts.get(part_id)
            if trained is None:
                return None
            trained = trained.model_copy(update={"approved": True})
            record.parts[part_id] = trained
            self._put(record)
            return trained

    def put(self, trained: TrainedPart) -> None:
        with self._lock:
            record = self._get()
            record.parts[trained.part_id] = trained
            self._put(record)

    def forget(self, part_id: str) -> bool:
        with self._lock:
            record = self._get()
            if record.parts.pop(part_id, None) is None:
                return False
            self._put(record)
        folder = self.frames_folder(part_id)
        if folder is not None and folder.is_dir():
            for f in folder.iterdir():
                f.unlink(missing_ok=True)
        return True

    def frames_folder(self, part_id: str) -> Path | None:
        if self._frames is None:
            return None
        return self._frames / re.sub(r"[^A-Za-z0-9._-]+", "_", part_id)

    def image_path(self, part_id: str, name: str) -> Path | None:
        """A picture kept with a trained part: 'overlay', 'beside', or 'light-NN'."""
        folder = self.frames_folder(part_id)
        if folder is None or not re.fullmatch(r"overlay|beside|light-\d\d", name):
            return None
        return folder / f"{name}.jpg"

    def save_image(self, part_id: str, name: str, image: Image) -> None:
        path = self.image_path(part_id, name)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            path.write_bytes(data.tobytes())


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def spread_degrees(angles: list[float]) -> float:
    """How far angles stray from their (circular) mean, at most."""
    x = sum(math.cos(math.radians(a)) for a in angles)
    y = sum(math.sin(math.radians(a)) for a in angles)
    mean = math.degrees(math.atan2(y, x))
    return max(abs((a - mean + 180.0) % 360.0 - 180.0) for a in angles)


def spread_points(points: list[tuple[float, float]]) -> float:
    """How far points stray from their mean, at most."""
    mx = sum(p[0] for p in points) / len(points)
    my = sum(p[1] for p in points) / len(points)
    return max(math.hypot(p[0] - mx, p[1] - my) for p in points)


PIN1 = (60, 60, 230)
PAD = (90, 220, 90)
BODY = (230, 200, 90)


def draw_footprint(
    image: Image, footprint: Footprint, to_px: Callable[[float, float], tuple[float, float]]
) -> None:
    """A footprint drawn on a picture: its body dashed, its pads outlined, pin 1's filled
    red, and a "+" at its origin. ``to_px`` takes a point in the footprint's frame (mm)
    to the picture's pixels."""

    def pts(corners: list[tuple[float, float]]) -> np.ndarray:
        return np.array([[round(c) for c in to_px(x, y)] for x, y in corners], np.int32)

    if footprint.body is not None:
        bx, by = footprint.body.x / 2, footprint.body.y / 2
        corners = [(-bx, -by), (bx, -by), (bx, by), (-bx, by), (-bx, -by)]
        for (x0, y0), (x1, y1) in itertools.pairwise(corners):
            steps = 12
            for k in range(0, steps, 2):
                a, b = k / steps, (k + 1) / steps
                p = pts([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a)])[0]
                q = pts([(x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)])[0]
                cv2.line(image, (int(p[0]), int(p[1])), (int(q[0]), int(q[1])), BODY, 1)
    pin1 = next((p for p in footprint.pads if p.name in ("1", "A1")), None)
    for pad in [p for p in footprint.pads if p is not pin1] + ([pin1] if pin1 else []):
        a = math.radians(pad.rotation)
        ca, sa = math.cos(a), math.sin(a)
        half = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        corners = [
            (
                pad.x + (u * pad.width / 2) * ca - (v * pad.height / 2) * sa,
                pad.y + (u * pad.width / 2) * sa + (v * pad.height / 2) * ca,
            )
            for u, v in half
        ]
        poly = pts(corners)
        if pad is pin1:
            fill = image.copy()
            cv2.fillPoly(fill, [poly], PIN1)
            cv2.addWeighted(fill, 0.45, image, 0.55, 0, dst=image)
            cv2.polylines(image, [poly], True, PIN1, 2)
        else:
            cv2.polylines(image, [poly], True, PAD, 1)
    o = pts([(0.0, 0.0)])[0]
    cv2.drawMarker(image, (int(o[0]), int(o[1])), BODY, cv2.MARKER_CROSS, 10, 1)


def approval_images(
    image: Image,
    center_px: tuple[float, float],
    half_px: float,
    footprint: Footprint,
    to_px: Callable[[float, float], tuple[float, float]],
) -> tuple[Image, Image]:
    """The two pictures an operator approves a trained part by, from a frame of it: the
    part with its footprint drawn over it where it was measured, and the part with the
    footprint beside it, at the same scale and turn, for its markings. ``to_px`` takes the
    footprint's frame to the frame's pixels; ``center_px``: the part's middle."""
    h, w = image.shape[:2]
    r = round(half_px)
    x0 = max(0, round(center_px[0]) - r)
    y0 = max(0, round(center_px[1]) - r)
    x1, y1 = min(w, x0 + 2 * r), min(h, y0 + 2 * r)
    crop = np.ascontiguousarray(image[y0:y1, x0:x1])
    big = np.asarray(
        cv2.resize(crop, None, fx=ZOOM, fy=ZOOM, interpolation=cv2.INTER_CUBIC), dtype=np.uint8
    )

    def zoomed(x: float, y: float) -> tuple[float, float]:
        u, v = to_px(x, y)
        return (u - x0) * ZOOM, (v - y0) * ZOOM

    overlay = big.copy()
    draw_footprint(overlay, footprint, zoomed)
    drawing = np.zeros_like(big)
    drawing[:] = (24, 24, 28)
    draw_footprint(drawing, footprint, zoomed)
    gap = np.full((big.shape[0], 8, 3), 255, np.uint8)
    beside = np.hstack([big, gap, drawing])
    return overlay, beside
