"""Camera sources produce frames; the hub decides when they run."""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]
"""A BGR image, height x width x 3."""


@dataclass(frozen=True)
class Frame:
    image: Image
    timestamp: float
    """When the frame was captured, in seconds since the Unix epoch."""
    seq: int
    """Increases by one with every frame from a source."""


class CameraSource(ABC):
    """Produces frames while started. Subclasses call ``_publish`` for each new frame."""

    kind: str = "unknown"

    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._seq = 0
        self._changed = asyncio.Condition()
        self.error: str | None = None
        """Why the source is not producing frames, if it failed."""

    @property
    def latest(self) -> Frame | None:
        return self._frame

    @property
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Native frame size, (width, height)."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def next_frame(self, after_seq: int, wait_s: float) -> Frame | None:
        """The first frame newer than ``after_seq``, or None if none arrives in time."""
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(
                        lambda: self._frame is not None and self._frame.seq > after_seq
                    ),
                    wait_s,
                )
            except TimeoutError:
                return None
            return self._frame

    async def _publish(self, image: Image, timestamp: float | None = None) -> None:
        self._seq += 1
        self._frame = Frame(image, timestamp if timestamp is not None else time.time(), self._seq)
        self.error = None
        async with self._changed:
            self._changed.notify_all()
