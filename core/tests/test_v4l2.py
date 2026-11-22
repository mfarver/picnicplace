"""Reading a camera's formats, and reading and setting its controls, with V4L2 ioctls,
against a fake camera."""

import struct

import pytest

from picnicplace.camera.v4l2 import (
    CID_AUTO_WHITE_BALANCE,
    CID_EXPOSURE_ABSOLUTE,
    CID_EXPOSURE_AUTO,
    CID_GAIN,
    CID_WHITE_BALANCE_TEMPERATURE,
    EXPOSURE_APERTURE_PRIORITY,
    EXPOSURE_MANUAL,
    VIDIOC_ENUM_FMT,
    VIDIOC_ENUM_FRAMEINTERVALS,
    VIDIOC_ENUM_FRAMESIZES,
    VIDIOC_G_CTRL,
    VIDIOC_G_FMT,
    VIDIOC_QUERYCTRL,
    VIDIOC_S_CTRL,
    Control,
    read_controls,
    read_formats,
    write_controls,
)


def code(fourcc: str) -> int:
    return struct.unpack("<I", fourcc.encode())[0]


class FakeCamera:
    """Answers ioctls the way a LumenPnP camera does: two formats, each with sizes and
    frame rates (as fractions of a second), and set to MJPEG at 1080p."""

    def __init__(self, formats: dict[str, dict[tuple[int, int], list[int]]] | None = None):
        self.formats = (
            formats
            if formats is not None
            else {
                "MJPG": {(1920, 1080): [30, 5], (640, 480): [30]},
                "YUYV": {(1920, 1080): [5], (640, 480): [30, 15]},
            }
        )
        self.stepwise = False

    def __call__(self, fd: int, request: int, buf: bytearray) -> None:
        names = list(self.formats)
        if request == VIDIOC_G_FMT:
            if not names:
                raise OSError(22, "Invalid argument")
            struct.pack_into("<III", buf, 8, 1920, 1080, code("MJPG"))
        elif request == VIDIOC_ENUM_FMT:
            index = struct.unpack_from("<I", buf, 0)[0]
            if index >= len(names):
                raise OSError(22, "Invalid argument")
            buf[12:44] = f"{names[index]} format".encode().ljust(32, b"\0")
            struct.pack_into("<I", buf, 44, code(names[index]))
        elif request == VIDIOC_ENUM_FRAMESIZES:
            index, pixel = struct.unpack_from("<II", buf, 0)
            sizes = list(self.formats[struct.pack("<I", pixel).decode()])
            if index >= len(sizes):
                raise OSError(22, "Invalid argument")
            struct.pack_into("<I", buf, 8, 3 if self.stepwise else 1)
            struct.pack_into("<II", buf, 12, *sizes[index])
        elif request == VIDIOC_ENUM_FRAMEINTERVALS:
            index, pixel, width, height = struct.unpack_from("<IIII", buf, 0)
            rates = self.formats[struct.pack("<I", pixel).decode()][(width, height)]
            if index >= len(rates):
                raise OSError(22, "Invalid argument")
            struct.pack_into("<III", buf, 16, 1, 1, rates[index])
        else:
            raise OSError(25, "Inappropriate ioctl for device")


def test_formats_sizes_and_rates() -> None:
    found = read_formats(3, FakeCamera())
    assert found.current == ("MJPG", 1920, 1080)
    assert [f.fourcc for f in found.formats] == ["MJPG", "YUYV"]
    mjpeg, yuyv = found.formats
    assert mjpeg.description == "MJPG format"
    assert [str(s) for s in mjpeg.sizes] == ["1920x1080@30/5", "640x480@30"]
    assert mjpeg.summary() == "MJPG 1920x1080 at 30 fps"
    assert yuyv.summary() == "YUYV 1920x1080 at 5 fps"
    assert yuyv.sizes[1].rates == [30, 15]


def test_a_metadata_node_has_no_formats() -> None:
    found = read_formats(3, FakeCamera({}))
    assert found.formats == []
    assert found.current is None


def test_a_camera_that_gives_a_range_of_sizes() -> None:
    camera = FakeCamera({"GREY": {(640, 480): [30]}})
    camera.stepwise = True
    (grey,) = read_formats(3, camera).formats
    assert grey.stepwise and grey.sizes == []
    assert grey.summary() == "GREY (a range of sizes)"


class FakeControls:
    """Answers control ioctls the way a LumenPnP's top camera does (read on a real one,
    2026-11-21): manual exposure at 7.8 ms (78 in 100 µs units, 1 to 5000), gain 0 (0 to
    100), white balance not automatic and at 4000 K (2800 to 6500). Like a UVC camera it
    refuses an exposure time while it sets its own exposure, and a white balance while
    that is automatic."""

    def __init__(self, white_balance: bool = True) -> None:
        self.controls: dict[int, list[int]] = {
            CID_EXPOSURE_AUTO: [EXPOSURE_MANUAL, 0, 3, 1],
            CID_EXPOSURE_ABSOLUTE: [78, 1, 5000, 78],
            CID_GAIN: [0, 0, 100, 0],
        }
        if white_balance:
            self.controls[CID_AUTO_WHITE_BALANCE] = [0, 0, 1, 0]
            self.controls[CID_WHITE_BALANCE_TEMPERATURE] = [4000, 2800, 6500, 4000]
        self.set: list[tuple[int, int]] = []

    def value(self, cid: int) -> int:
        return self.controls[cid][0]

    def __call__(self, fd: int, request: int, buf: bytearray) -> None:
        cid = struct.unpack_from("<I", buf, 0)[0]
        if cid not in self.controls:
            raise OSError(22, "Invalid argument")
        value, minimum, maximum, default = self.controls[cid]
        if request == VIDIOC_QUERYCTRL:
            struct.pack_into("<iiiiI", buf, 40, minimum, maximum, 1, default, 0)
        elif request == VIDIOC_G_CTRL:
            struct.pack_into("<i", buf, 4, value)
        elif request == VIDIOC_S_CTRL:
            new = struct.unpack_from("<i", buf, 4)[0]
            if not minimum <= new <= maximum:
                raise OSError(34, "Numerical result out of range")
            if cid == CID_EXPOSURE_ABSOLUTE and self.value(CID_EXPOSURE_AUTO) != EXPOSURE_MANUAL:
                raise OSError(13, "Permission denied")
            if cid == CID_WHITE_BALANCE_TEMPERATURE and self.value(CID_AUTO_WHITE_BALANCE):
                raise OSError(13, "Permission denied")
            self.controls[cid][0] = new
            self.set.append((cid, new))
        else:
            raise OSError(25, "Inappropriate ioctl for device")


def test_controls_are_read_as_the_camera_has_them() -> None:
    controls = read_controls(0, FakeControls())
    assert controls.exposure_auto is False
    assert controls.exposure == Control(78, 1, 5000, 78)
    assert controls.gain == Control(0, 0, 100, 0)
    assert controls.white_balance_auto is False
    assert controls.white_balance == Control(4000, 2800, 6500, 4000)
    plain = read_controls(0, FakeControls(white_balance=False))
    assert plain.white_balance is None and plain.white_balance_auto is None


def test_manual_exposure_is_switched_on_before_its_time_is_set() -> None:
    camera = FakeControls()
    camera.controls[CID_EXPOSURE_AUTO][0] = EXPOSURE_APERTURE_PRIORITY
    camera.controls[CID_AUTO_WHITE_BALANCE][0] = 1
    write_controls(0, camera, exposure=300, gain=20, white_balance=5600)
    assert camera.value(CID_EXPOSURE_AUTO) == EXPOSURE_MANUAL
    assert camera.value(CID_EXPOSURE_ABSOLUTE) == 300
    assert camera.value(CID_GAIN) == 20
    assert camera.value(CID_AUTO_WHITE_BALANCE) == 0
    assert camera.value(CID_WHITE_BALANCE_TEMPERATURE) == 5600
    write_controls(0, camera, exposure="auto", white_balance="auto")
    assert camera.value(CID_EXPOSURE_AUTO) == EXPOSURE_APERTURE_PRIORITY
    assert camera.value(CID_AUTO_WHITE_BALANCE) == 1


def test_a_setting_the_camera_cannot_take_says_why() -> None:
    camera = FakeControls(white_balance=False)
    with pytest.raises(OSError, match=r"exposure \(in 100 µs\) must be from 1 to 5000"):
        write_controls(0, camera, exposure=6000)
    with pytest.raises(OSError, match="the camera has no white balance"):
        write_controls(0, camera, white_balance=5000)
    assert camera.set == []
