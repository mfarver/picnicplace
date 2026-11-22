"""What a Linux (V4L2) camera can send: its formats, frame sizes and frame rates, and the
format it is set to now; and its exposure, gain and white balance, read and set.

It asks the camera directly with ioctls, so nothing needs installing (``v4l2-ctl`` gives
the same answers). Asking is safe while another program, such as ``picnicd``, streams from
the camera: reading formats and controls only reads, and controls may be set from another
handle while the camera streams. The structures are laid out as on 64-bit Linux.
"""

import fcntl
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Ioctl = Callable[[int, int, bytearray], object]
"""Sends one request to an open device, filling in the buffer; raises OSError if refused."""


def _iowr(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | number


VIDIOC_ENUM_FMT = _iowr(2, 64)
VIDIOC_G_FMT = _iowr(4, 208)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, 44)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, 52)
VIDIOC_G_CTRL = _iowr(27, 8)
VIDIOC_S_CTRL = _iowr(28, 8)
VIDIOC_QUERYCTRL = _iowr(36, 68)
CID_AUTO_WHITE_BALANCE = 0x0098090C
CID_GAIN = 0x00980913
CID_WHITE_BALANCE_TEMPERATURE = 0x0098091A
CID_EXPOSURE_AUTO = 0x009A0901
CID_EXPOSURE_ABSOLUTE = 0x009A0902
"""In units of 100 µs."""
EXPOSURE_MANUAL = 1
EXPOSURE_APERTURE_PRIORITY = 3
"""The camera sets its own exposure (UVC cameras' "auto")."""
CTRL_FLAG_DISABLED = 0x0001
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


@dataclass
class Control:
    """One of a camera's controls: its value now and the values it takes."""

    value: int
    minimum: int
    maximum: int
    default: int


@dataclass
class CameraControls:
    """A camera's exposure, gain and white balance, as far as it has them (None if not)."""

    exposure_auto: bool | None = None
    exposure: Control | None = None
    """In units of 100 µs."""
    gain: Control | None = None
    white_balance_auto: bool | None = None
    white_balance: Control | None = None
    """In kelvin."""


def read_control(fd: int, ioctl: Ioctl, cid: int) -> Control | None:
    """A control's value and range, or None if the camera does not have it."""
    query = bytearray(68)
    struct.pack_into("<I", query, 0, cid)
    if not _ask(ioctl, fd, VIDIOC_QUERYCTRL, query):
        return None
    minimum, maximum, _, default, flags = struct.unpack_from("<iiiiI", query, 40)
    if flags & CTRL_FLAG_DISABLED:
        return None
    value = bytearray(8)
    struct.pack_into("<I", value, 0, cid)
    if not _ask(ioctl, fd, VIDIOC_G_CTRL, value):
        return None
    return Control(struct.unpack_from("<i", value, 4)[0], minimum, maximum, default)


def write_control(fd: int, ioctl: Ioctl, cid: int, value: int) -> None:
    """Set a control; raises OSError if the camera refuses."""
    buf = bytearray(8)
    struct.pack_into("<Ii", buf, 0, cid, value)
    ioctl(fd, VIDIOC_S_CTRL, buf)


def read_controls(fd: int, ioctl: Ioctl) -> CameraControls:
    exposure_mode = read_control(fd, ioctl, CID_EXPOSURE_AUTO)
    white_auto = read_control(fd, ioctl, CID_AUTO_WHITE_BALANCE)
    return CameraControls(
        exposure_auto=(
            exposure_mode.value != EXPOSURE_MANUAL if exposure_mode is not None else None
        ),
        exposure=read_control(fd, ioctl, CID_EXPOSURE_ABSOLUTE),
        gain=read_control(fd, ioctl, CID_GAIN),
        white_balance_auto=bool(white_auto.value) if white_auto is not None else None,
        white_balance=read_control(fd, ioctl, CID_WHITE_BALANCE_TEMPERATURE),
    )


def _clamp(control: Control | None, value: int, what: str) -> int:
    if control is None:
        raise OSError(f"the camera has no {what} setting")
    if not control.minimum <= value <= control.maximum:
        raise OSError(f"{what} must be from {control.minimum} to {control.maximum}")
    return value


def write_controls(
    fd: int,
    ioctl: Ioctl,
    exposure: int | Literal["auto"] | None = None,
    gain: int | None = None,
    white_balance: int | Literal["auto"] | None = None,
) -> None:
    """Set exposure (100 µs units, or the camera's own), gain, and white balance (kelvin,
    or the camera's own); None leaves one as it is. Manual exposure and white balance are
    switched on before their values are set: a camera ignores a value otherwise. Raises
    OSError, saying which, if the camera has no such setting or refuses the value."""
    now = read_controls(fd, ioctl)
    if exposure == "auto":
        write_control(fd, ioctl, CID_EXPOSURE_AUTO, EXPOSURE_APERTURE_PRIORITY)
    elif exposure is not None:
        value = _clamp(now.exposure, exposure, "exposure (in 100 µs)")
        write_control(fd, ioctl, CID_EXPOSURE_AUTO, EXPOSURE_MANUAL)
        write_control(fd, ioctl, CID_EXPOSURE_ABSOLUTE, value)
    if gain is not None:
        write_control(fd, ioctl, CID_GAIN, _clamp(now.gain, gain, "gain"))
    if white_balance == "auto":
        write_control(fd, ioctl, CID_AUTO_WHITE_BALANCE, 1)
    elif white_balance is not None:
        value = _clamp(now.white_balance, white_balance, "white balance (in kelvin)")
        write_control(fd, ioctl, CID_AUTO_WHITE_BALANCE, 0)
        write_control(fd, ioctl, CID_WHITE_BALANCE_TEMPERATURE, value)


def device_path(device: str) -> Path:
    """A camera's device file, from a path or an index (``0`` is ``/dev/video0``)."""
    return Path(f"/dev/video{device}") if device.isdigit() else Path(device)


def open_device(device: str) -> int:
    """Open a camera's device file for reading and setting its controls."""
    return os.open(device_path(device), os.O_RDWR | os.O_NONBLOCK)
