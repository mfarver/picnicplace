"""Reading a camera's formats with V4L2 ioctls, against a fake camera."""

import struct

from picnicplace.camera.v4l2 import (
    VIDIOC_ENUM_FMT,
    VIDIOC_ENUM_FRAMEINTERVALS,
    VIDIOC_ENUM_FRAMESIZES,
    VIDIOC_G_FMT,
    read_formats,
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
