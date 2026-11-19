"""Finding a chip where it was placed (vision.placed), on drawn pictures: a dark body
between two bright caps, on a board with bright pads wider than the chip."""

import math

import cv2
import numpy as np
import pytest

from picnicplace.vision.placed import chip_offset, find_placed_chip

MX, MY = 0.0105, 0.0108  # a LumenPnP top camera's two scales differ a little


def _picture(dx: float, dy: float, turn: float, pads_angle: float) -> np.ndarray:
    """A board 4 x 4 mm with an 0603's pads at the middle (at ``pads_angle``), and the
    chip (1.6 x 0.8, 0.3 mm caps) placed ``dx``, ``dy`` mm off (right, up) and turned."""
    w, h = round(4 / MX), round(4 / MY)
    img = np.full((h, w, 3), (90, 70, 20), np.uint8)

    def rect(x: float, y: float, a: float, length: float, width: float, color: tuple[int, ...]):
        r = math.radians(a)
        pts = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            px, py = sx * length / 2, sy * width / 2
            mx, my = (
                x + px * math.cos(r) - py * math.sin(r),
                y + px * math.sin(r) + py * math.cos(r),
            )
            pts.append((round(w / 2 + mx / MX), round(h / 2 - my / MY)))
        cv2.fillPoly(img, [np.array(pts, np.int32)], color)

    a = math.radians(pads_angle)
    for s in (-1, 1):
        rect(
            s * 0.825 * math.cos(a), s * 0.825 * math.sin(a), pads_angle, 0.8, 0.95, (200, 200, 205)
        )
    angle = pads_angle + turn
    b = math.radians(angle)
    rect(dx, dy, angle, 1.6, 0.8, (60, 25, 10))
    for s in (-1, 1):
        rect(
            dx + s * 0.65 * math.cos(b),
            dy + s * 0.65 * math.sin(b),
            angle,
            0.3,
            0.8,
            (235, 235, 240),
        )
    return cv2.GaussianBlur(img, (5, 5), 0)


@pytest.mark.parametrize(
    ("dx", "dy", "turn", "pads"),
    [(0.0, 0.0, 0.0, 0.0), (0.05, -0.08, 2.0, 45.0), (-0.12, 0.41, 9.0, 88.0)],
)
def test_a_placed_chip_is_found_where_it_landed(
    dx: float, dy: float, turn: float, pads: float
) -> None:
    img = _picture(dx, dy, turn, pads)
    h, w = img.shape[:2]
    found = find_placed_chip(img, MX, MY, 1.6, 0.825, pads)
    assert found is not None and found.score > 0.6
    right, down = chip_offset(found, w, h, MX, MY)
    assert right == pytest.approx(dx, abs=0.05)
    assert -down == pytest.approx(dy, abs=0.05)
    assert ((found.angle - pads - turn + 90) % 180) - 90 == pytest.approx(0, abs=1.5)
