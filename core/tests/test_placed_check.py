"""Finding a chip where it was placed, and the board's pads under it (vision.placed): on
drawn pictures (a dark body between two bright caps, on a board with bright pads wider than
the chip), and on real ones."""

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from picnicplace.vision.placed import (
    Polygon,
    chip_ends,
    chip_offset,
    find_board_pads,
    find_placed_chip,
    on_pads,
)

MX, MY = 0.0105, 0.0108  # a LumenPnP top camera's two scales differ a little
DATA = Path(__file__).parent / "data" / "vision"


def pads_0603(angle: float) -> list[Polygon]:
    """An 0603's pads (0.8 x 0.95 mm, 1.65 mm apart) about the picture's middle, along
    ``angle`` (image degrees), in mm right and down."""
    a = math.radians(angle)
    ux, uy = math.cos(a), -math.sin(a)
    vx, vy = math.sin(a), math.cos(a)
    return [
        [
            ((c + p * 0.4) * ux + q * 0.475 * vx, (c + p * 0.4) * uy + q * 0.475 * vy)
            for p, q in ((-1, -1), (1, -1), (1, 1), (-1, 1))
        ]
        for c in (-0.825, 0.825)
    ]


def _picture(
    dx: float, dy: float, turn: float, pads_angle: float, pads_at: tuple[float, float] = (0, 0)
) -> np.ndarray:
    """A board 4 x 4 mm with an 0603's pads at the middle (at ``pads_angle``; or
    ``pads_at`` mm off it, right and up), and the chip (1.6 x 0.8, 0.3 mm caps) placed
    ``dx``, ``dy`` mm off the middle (right, up) and turned."""
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
            pads_at[0] + s * 0.825 * math.cos(a),
            pads_at[1] + s * 0.825 * math.sin(a),
            pads_angle,
            0.8,
            0.95,
            (200, 200, 205),
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


@pytest.mark.parametrize(
    ("pads_at", "angle"), [((0.0, 0.0), 0.0), ((0.2, -0.1), 45.0), ((-0.3, 0.25), 88.0)]
)
def test_the_pads_are_found_where_they_are(pads_at: tuple[float, float], angle: float) -> None:
    # The chip sits centered on its pads, wherever they are.
    img = _picture(pads_at[0], pads_at[1], 0.0, angle, pads_at)
    found = find_board_pads(img, MX, MY, pads_0603(angle))
    assert found is not None and found.score > 0.8
    assert (found.dx, -found.dy) == pytest.approx(pads_at, abs=0.03)


def test_a_chip_off_its_pads_does_not_drag_them_far() -> None:
    """Its bright ends lie over the pads, but barely pull their fit its way."""
    a = math.radians(30.0)
    for off in (
        (-math.sin(a) * 0.35, math.cos(a) * 0.35),
        (math.cos(a) * 0.35, math.sin(a) * 0.35),
    ):
        found = find_board_pads(_picture(*off, 0.0, 30.0), MX, MY, pads_0603(30.0))
        assert found is not None
        assert math.hypot(found.dx, found.dy) < 0.03


def _share(img: np.ndarray, angle: float, length: float = 1.6, width: float = 0.8) -> float:
    h, w = img.shape[:2]
    chip = find_placed_chip(img, MX, MY, length, 0.825, angle)
    pads = find_board_pads(img, MX, MY, pads_0603(angle))
    assert chip is not None and pads is not None
    moved = [[(x + pads.dx, y + pads.dy) for x, y in p] for p in pads_0603(angle)]
    return on_pads(chip_ends(chip, w, h, MX, MY, length, width), moved)


@pytest.mark.parametrize(
    ("dx", "dy", "low", "high"),
    [(0.0, 0.0, 0.95, 1.0), (0.0, 0.25, 0.6, 0.85), (0.3, 0.0, 0.0, 0.5)],
)
def test_how_much_of_a_chip_is_on_its_pads(dx: float, dy: float, low: float, high: float) -> None:
    """Centered, all of it; 0.25 mm across, most of it; 0.3 mm along, little of one end."""
    assert low <= _share(_picture(dx, dy, 0.0, 0.0), 0.0) <= high


# Real 0603s placed on tape over a test board's pads (see data/vision/README.md).
REAL_MX, REAL_MY = 0.010596, 0.010750


@pytest.mark.parametrize(
    ("name", "angle", "pads_down", "low", "high"),
    [
        ("good", -2.499, 0.0, 0.95, 1.0),
        ("good-vertical", -92.499, 0.0, 0.95, 1.0),
        ("board-moved", -0.8633, 0.32, 0.6, 0.75),
        ("off-end", -90.8633, 0.19, 0.0, 0.4),
    ],
)
def test_real_placements_on_their_pads(
    name: str, angle: float, pads_down: float, low: float, high: float
) -> None:
    frame = cv2.imread(str(DATA / f"lumenpnp-placed-0603-{name}.webp"))
    assert frame is not None
    img = np.asarray(frame, dtype=np.uint8)
    h, w = img.shape[:2]
    pads = find_board_pads(img, REAL_MX, REAL_MY, pads_0603(angle))
    chip = find_placed_chip(img, REAL_MX, REAL_MY, 1.6, 0.825, angle)
    assert pads is not None and pads.score > 0.75
    assert chip is not None and chip.score > 0.6
    assert pads.dy == pytest.approx(pads_down, abs=0.05)
    assert abs(pads.dx) < 0.1
    moved = [[(x + pads.dx, y + pads.dy) for x, y in p] for p in pads_0603(angle)]
    share = on_pads(chip_ends(chip, w, h, REAL_MX, REAL_MY, 1.6, 0.825), moved)
    assert low <= share <= high
