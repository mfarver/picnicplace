"""One hub per camera: starts its source when the first viewer arrives, stops it a little
after the last one leaves, and encodes JPEGs once per frame and size, however many
viewers share them."""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import AsyncGenerator

import cv2

from picnicplace.camera.source import CameraSource, Frame

log = logging.getLogger(__name__)

IDLE_STOP_S = 5.0
"""Keep a source running this long after its last viewer, so reloads do not restart it."""

ENCODE_CACHE = 8


class CameraHub:
    def __init__(self, camera_id: str, source: CameraSource) -> None:
        self.camera_id = camera_id
        self.source = source
        self._viewers = 0
        self._running = False
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._encoded: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self.closed = False

    @property
    def viewers(self) -> int:
        return self._viewers

    @contextlib.asynccontextmanager
    async def viewer(self) -> AsyncGenerator[CameraSource]:
        """Hold the source running while inside this block."""
        async with self._lock:
            self._viewers += 1
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
            if not self._running:
                log.info("camera %s: starting", self.camera_id)
                try:
                    await self.source.start()
                except BaseException:
                    self._viewers -= 1
                    raise
                self._running = True
        try:
            yield self.source
        finally:
            async with self._lock:
                self._viewers -= 1
                if self._viewers == 0 and self._running and not self.closed:
                    self._idle_task = asyncio.create_task(self._stop_when_idle())

    async def _stop_when_idle(self) -> None:
        await asyncio.sleep(IDLE_STOP_S)
        async with self._lock:
            if self._viewers == 0 and self._running:
                log.info("camera %s: no viewers; stopping", self.camera_id)
                self._running = False
                self._idle_task = None
                await self.source.stop()
                self.source.forget()

    async def shutdown(self) -> None:
        self.closed = True
        if self._idle_task is not None:
            self._idle_task.cancel()
        if self._running:
            self._running = False
            await self.source.stop()

    async def jpeg(self, frame: Frame, width: int | None, quality: int) -> bytes:
        """The frame as JPEG, scaled down to ``width`` if given. Cached per frame."""
        native_w = frame.image.shape[1]
        target_w = native_w if width is None or width >= native_w else width
        key = (frame.seq, target_w, quality)
        cached = self._encoded.get(key)
        if cached is not None:
            return cached
        data = await asyncio.to_thread(_encode, frame, target_w, quality)
        self._encoded[key] = data
        while len(self._encoded) > ENCODE_CACHE:
            self._encoded.popitem(last=False)
        return data


def _encode(frame: Frame, width: int, quality: int) -> bytes:
    image = frame.image
    if width != image.shape[1]:
        height = max(1, round(image.shape[0] * width / image.shape[1]))
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buffer.tobytes()
