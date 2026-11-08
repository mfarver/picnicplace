"""Real cameras (USB UVC, such as the LumenPnP's), captured with OpenCV on a thread.

On Linux the device is a path, ideally a stable one under ``/dev/v4l/by-id/`` so the top
and bottom cameras never swap after a reboot. A number selects a camera by index, which is
handy on macOS during development.
"""

import asyncio
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Protocol

import cv2

from picnicplace.camera.source import CameraSource, Image

log = logging.getLogger(__name__)

REOPEN_DELAY_S = 2.0
"""Wait before reopening a camera that failed or was unplugged."""


class Capture(Protocol):
    """The parts of cv2.VideoCapture the source uses (so tests can substitute one)."""

    def isOpened(self) -> bool: ...
    def read(self) -> tuple[bool, Image | None]: ...
    def set(self, prop: int, value: float) -> bool: ...
    def get(self, prop: int) -> float: ...
    def release(self) -> None: ...


def open_capture(device: str) -> Capture:
    target: int | str = int(device) if device.isdigit() else device
    if sys.platform.startswith("linux"):
        return cv2.VideoCapture(target, cv2.CAP_V4L2)  # type: ignore[return-value]
    return cv2.VideoCapture(target)  # type: ignore[return-value]


class Orientation:
    """Flips and a small rotation, so the image matches the machine's axes."""

    def __init__(self, flip_x: bool, flip_y: bool, rotation: float) -> None:
        self.flip_code: int | None = {
            (True, False): 1,
            (False, True): 0,
            (True, True): -1,
        }.get((flip_x, flip_y))
        self.rotation = rotation

    def apply(self, image: Image) -> Image:
        if self.flip_code is not None:
            image = cv2.flip(image, self.flip_code)  # type: ignore[assignment]
        if self.rotation:
            h, w = image.shape[:2]
            matrix = cv2.getRotationMatrix2D((w / 2, h / 2), self.rotation, 1.0)
            image = cv2.warpAffine(  # type: ignore[assignment]
                image, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
            )
        return image


class DeviceCamera(CameraSource):
    kind = "device"

    def __init__(
        self,
        device: str,
        width: int,
        height: int,
        fps: float,
        opener: Callable[[str], Capture] = open_capture,
        *,
        flip_x: bool = False,
        flip_y: bool = False,
        rotation: float = 0.0,
    ) -> None:
        super().__init__()
        self._orient = Orientation(flip_x, flip_y, rotation)
        self._device = device
        self._requested = (width, height)
        self._fps = fps
        self._opener = opener
        self._size = (width, height)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._capture, name=f"camera {self._device}", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        await asyncio.to_thread(thread.join, 5.0)

    def _capture(self) -> None:
        while not self._stop.is_set():
            cap = self._open()
            if cap is None:
                self._stop.wait(REOPEN_DELAY_S)
                continue
            try:
                self._read_frames(cap)
            finally:
                cap.release()
            if not self._stop.is_set():
                self._stop.wait(REOPEN_DELAY_S)

    def _open(self) -> Capture | None:
        try:
            cap = self._opener(self._device)
        except Exception as e:  # OpenCV raises a variety of errors
            self._fail(f"cannot open camera {self._device}: {e}")
            return None
        if not cap.isOpened():
            cap.release()
            self._fail(f"cannot open camera {self._device}")
            return None
        # Ask for compressed frames: USB 2 cannot carry uncompressed 1080p at speed.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._requested[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._requested[1])
        cap.set(cv2.CAP_PROP_FPS, self._fps)
        self._size = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or self._requested[0],
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self._requested[1],
        )
        log.info("camera %s open at %dx%d", self._device, *self._size)
        return cap

    def _read_frames(self, cap: Capture) -> None:
        failures = 0
        while not self._stop.is_set():
            ok, image = cap.read()
            if not ok or image is None:
                failures += 1
                if failures >= 10:
                    self._fail(f"camera {self._device} stopped sending frames")
                    return
                time.sleep(0.05)
                continue
            failures = 0
            self._deliver(self._orient.apply(image), time.time())

    def _deliver(self, image: Image, timestamp: float) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._publish(image, timestamp), loop)

    def _fail(self, message: str) -> None:
        if self.error != message:
            log.warning("%s", message)
        self.error = message
