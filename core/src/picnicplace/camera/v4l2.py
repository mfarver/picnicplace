"""What a Linux (V4L2) camera can send: its formats, frame sizes and frame rates, and the
format it is set to now.

It asks the camera directly with ioctls, so nothing needs installing (``v4l2-ctl`` gives
the same answers). Asking is safe while another program, such as ``picnicd``, streams from
the camera: these requests only read. The structures are laid out as on 64-bit Linux.
"""

import fcntl
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Ioctl = Callable[[int, int, bytearray], object]
"""Sends one request to an open device, filling in the buffer; raises OSError if refused."""


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


VIDIOC_ENUM_FMT = _iowr(2, 64)
VIDIOC_G_FMT = _iowr(4, 208)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, 44)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, 52)
BUF_TYPE_VIDEO_CAPTURE = 1
DISCRETE = 1
MAX_ENTRIES = 64
"""A guard against a driver that never says it has listed everything."""


@dataclass
class FrameSize:
    width: int
    height: int
    rates: list[float] = field(default_factory=list[float])
    """Frames per second, fastest first. Empty if the camera gives a range instead."""

    def __str__(self) -> str:
        rates = "/".join(f"{r:g}" for r in self.rates) or "?"
        return f"{self.width}x{self.height}@{rates}"


@dataclass
class Format:
    fourcc: str
    """Such as ``MJPG`` or ``YUYV``."""
    description: str
    sizes: list[FrameSize] = field(default_factory=list[FrameSize])
    stepwise: bool = False
    """The camera gives a range of sizes rather than a list."""

    def summary(self) -> str:
        """The largest size and its fastest rate, such as ``MJPG 1920x1080 at 30 fps``."""
        if not self.sizes:
            return f"{self.fourcc} ({'a range of sizes' if self.stepwise else 'no sizes'})"
        big = max(self.sizes, key=lambda s: s.width * s.height)
        rate = f" at {big.rates[0]:g} fps" if big.rates else ""
        return f"{self.fourcc} {big.width}x{big.height}{rate}"


@dataclass
class CameraFormats:
    formats: list[Format]
    current: tuple[str, int, int] | None
    """The format it is set to now: FourCC, width, height."""


def _fourcc(value: int) -> str:
    return struct.pack("<I", value).decode("ascii", "replace").strip()


def _ask(ioctl: Ioctl, fd: int, request: int, buf: bytearray) -> bool:
    try:
        ioctl(fd, request, buf)
    except OSError:
        return False
    return True


def read_formats(fd: int, ioctl: Ioctl) -> CameraFormats:
    """Everything the open device ``fd`` can capture. A device that captures nothing (a
    camera's metadata node) gives no formats."""
    current = None
    buf = bytearray(208)
    struct.pack_into("<I", buf, 0, BUF_TYPE_VIDEO_CAPTURE)
    if _ask(ioctl, fd, VIDIOC_G_FMT, buf):
        width, height, pixel = struct.unpack_from("<III", buf, 8)
        current = (_fourcc(pixel), width, height)
    formats: list[Format] = []
    for index in range(MAX_ENTRIES):
        desc = bytearray(64)
        struct.pack_into("<II", desc, 0, index, BUF_TYPE_VIDEO_CAPTURE)
        if not _ask(ioctl, fd, VIDIOC_ENUM_FMT, desc):
            break
        pixel = struct.unpack_from("<I", desc, 44)[0]
        name = bytes(desc[12:44]).split(b"\0")[0].decode("ascii", "replace")
        fmt = Format(_fourcc(pixel), name)
        for size_index in range(MAX_ENTRIES):
            size = bytearray(44)
            struct.pack_into("<II", size, 0, size_index, pixel)
            if not _ask(ioctl, fd, VIDIOC_ENUM_FRAMESIZES, size):
                break
            if struct.unpack_from("<I", size, 8)[0] != DISCRETE:
                fmt.stepwise = True
                break
            width, height = struct.unpack_from("<II", size, 12)
            fmt.sizes.append(FrameSize(width, height, _rates(fd, ioctl, pixel, width, height)))
        formats.append(fmt)
    return CameraFormats(formats, current)


def _rates(fd: int, ioctl: Ioctl, pixel: int, width: int, height: int) -> list[float]:
    rates: list[float] = []
    for index in range(MAX_ENTRIES):
        interval = bytearray(52)
        struct.pack_into("<IIII", interval, 0, index, pixel, width, height)
        if not _ask(ioctl, fd, VIDIOC_ENUM_FRAMEINTERVALS, interval):
            break
        if struct.unpack_from("<I", interval, 16)[0] != DISCRETE:
            break
        numerator, denominator = struct.unpack_from("<II", interval, 20)
        if numerator:
            rates.append(denominator / numerator)
    return sorted(rates, reverse=True)


def camera_formats(path: Path) -> CameraFormats:
    """Open a camera device read-only and ask what it can capture."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return read_formats(fd, fcntl.ioctl)
    finally:
        os.close(fd)
