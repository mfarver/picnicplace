"""The pocket finder and the tape checks on real top-camera frames of 0603 resistors, and
0603 ceramic capacitors, in 8 mm paper tape on a LumenPnP (tests/data/vision/README.md)."""

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from picnicplace.camera.source import Image
from picnicplace.feeders.pocket import choose_pocket, pocket_choices, pocket_from_holes
from picnicplace.vision.fiducial import find_circles, find_rims
from picnicplace.vision.sprockets import find_hole_rhythm
from picnicplace.vision.tape import holds_part, loose_parts, pocket_darkness, pocket_ends

FRAMES = Path(__file__).parent / "data" / "vision"
MM_PER_PX = 20.176616212637704 / 1920
PICK = (360.0, 540.0)
"""The slot's pick location in the frames: they are cut from 1920 x 1080 frames taken
there, keeping columns 600 to 1499."""
ROTATION = -90.1139
"""The back rail's tape direction, in machine degrees: the tape feeds down the picture."""
BODY = (1.6, 0.8)
OUTLINE = (2.45, 0.95)
"""An 0603's land pattern, wider than the part: what the library's footprint gives. These
resistors lie with their length across the tape, along the picture's width."""
PITCH, LINE, HOLE = 4.0, 3.5, 1.5


def _frame(name: str) -> Image:
    frame = cv2.imread(str(FRAMES / f"lumenpnp-{name}.webp"))
    assert frame is not None
    return np.asarray(frame, dtype=np.uint8)


def _turn(x: float, y: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _pockets(image: Image) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """The pockets in view, nearest the pick location first, and the sprocket holes, in
    pixels: found as the feeder service finds them. The picture's right is +X and its up
    is +Y."""
    circles = find_circles(image, HOLE * 0.7 / MM_PER_PX, HOLE * 1.3 / MM_PER_PX)
    holes = [
        _turn((c.x - PICK[0]) * MM_PER_PX, (PICK[1] - c.y) * MM_PER_PX, -ROTATION) for c in circles
    ]
    fix = pocket_from_holes(holes, LINE, PITCH)
    assert fix is not None
    h = image.shape[0]
    pockets: list[tuple[float, float]] = []
    for k in sorted(range(-3, 4), key=abs):
        dx, dy = _turn(fix.along + k * PITCH, fix.across, ROTATION)
        u, v = PICK[0] + dx / MM_PER_PX, PICK[1] - dy / MM_PER_PX
        if 0 <= v < h:
            pockets.append((u, v))
    return pockets, [(c.x, c.y) for c in circles]


def _box(image: Image, u: float, v: float, w: float, h: float) -> list[tuple[float, float]]:
    """A rectangle w x h mm about (u, v) pixels, square to the picture."""
    a, b = w / 2 / MM_PER_PX, h / 2 / MM_PER_PX
    return [(u - a, v - b), (u + a, v - b), (u + a, v + b), (u - a, v + b)]


def _contents(name: str) -> list[bool | None]:
    """Whether each pocket in view holds a part, from the top of the picture down."""
    image = _frame(name)
    pockets, _ = _pockets(image)
    # The resistors' length lies along the picture's width: their ends are looked for there.
    return [
        holds_part(
            pocket_darkness(image, u, v, BODY, MM_PER_PX, OUTLINE),
            pocket_ends(image, u, v, BODY, MM_PER_PX, OUTLINE, 0.0),
        )
        for u, v in sorted(pockets, key=lambda p: p[1])
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The reel's empty leader: every pocket empty; the bottom one is cut by the feeder.
        ("tape-empty", [False, False, None]),
        # The first parts arrive one pocket back from the pick pocket (upstream, at the
        # top). A pocket whose paper round it is cut by the picture's edge is not judged.
        ("tape-loose-up", [None, False, None]),
        ("tape-loose-flipped", [True, False, None]),
        ("tape-loose-edge", [None, False, None]),
        # Fed on: every pocket full.
        ("tape-full-28", [True, True]),
        ("tape-full-32", [True, True]),
    ],
)
def test_pockets_holding_a_part_are_told_from_empty_ones(
    name: str, expected: list[bool | None]
) -> None:
    assert _contents(name) == expected


@pytest.mark.parametrize(
    ("name", "loose"),
    [
        ("tape-empty", 0),
        ("tape-full-28", 0),
        ("tape-full-32", 0),
        # Right side up, beside the pocket it jumped out of.
        ("tape-loose-up", 1),
        # Upside down: its white back is only a little darker than the paper.
        ("tape-loose-flipped", 1),
        # Upside down, by the picture's top edge, with a thin crease in the tape below.
        ("tape-loose-edge", 1),
    ],
)
def test_parts_lying_loose_on_the_tape_are_seen(name: str, loose: int) -> None:
    image = _frame(name)
    pockets, holes = _pockets(image)
    keep_out = [_box(image, u, v, OUTLINE[0] + 0.8, OUTLINE[1] + 0.8) for u, v in pockets]
    side = HOLE + 0.5
    keep_out += [_box(image, x, y, side, side) for x, y in holes]
    assert len(loose_parts(image, keep_out, MM_PER_PX)) == loose


def test_of_two_pockets_half_a_pitch_off_the_full_one_is_taken() -> None:
    """This feeder stopped with its pockets 2.00 mm either side of the pick location:
    the nearer one by a hair is empty, and the other holds the reel's first part."""
    image = _frame("tape-half-pitch")
    circles = find_circles(image, HOLE * 0.7 / MM_PER_PX, HOLE * 1.3 / MM_PER_PX)
    holes = [
        _turn((c.x - PICK[0]) * MM_PER_PX, (PICK[1] - c.y) * MM_PER_PX, -ROTATION) for c in circles
    ]
    fix = pocket_from_holes(holes, LINE, PITCH)
    assert fix is not None and abs(fix.along) == pytest.approx(2.0, abs=0.1)
    choices = pocket_choices(fix.along, PITCH)
    holds = []
    for along in choices:
        dx, dy = _turn(along, fix.across, ROTATION)
        u, v = PICK[0] + dx / MM_PER_PX, PICK[1] - dy / MM_PER_PX
        holds.append(holds_part(pocket_darkness(image, u, v, BODY, MM_PER_PX, OUTLINE)))
    assert holds == [False, True]
    assert choose_pocket(holds) == 1


CAP_MM_PER_PX = 0.01059562780069793
"""The top camera's scale as calibrated on 2026-11-20, when the capacitor frames were taken."""


@pytest.mark.parametrize(
    ("name", "pocket"),
    [
        # Centered in its pocket.
        ("tape-gray-cap", (401.0, 300.2)),
        # Lying toward one end of its pocket, which was found 0.2 mm off the part's middle.
        ("tape-gray-cap-off-center", (406.9, 299.6)),
    ],
)
def test_a_gray_capacitor_is_told_from_an_empty_pocket(
    name: str, pocket: tuple[float, float]
) -> None:
    """A 10 uF 0603 ceramic capacitor in paper tape, on a LumenPnP's front rail (2026-11-20):
    its gray body is no darker than an empty pocket's floor, so its middle alone took it for
    an empty pocket, and the run stopped. Its bright metal ends tell it is there. The empty
    pockets of the frames above stay empty with the same test."""
    image = _frame(name)
    u, v = pocket
    darkness = pocket_darkness(image, u, v, BODY, CAP_MM_PER_PX, OUTLINE)
    assert darkness is not None and darkness < 0.1  # the middle alone: "empty"
    ends = pocket_ends(image, u, v, BODY, CAP_MM_PER_PX, OUTLINE, 0.0)
    assert ends is not None and ends > 0.95
    assert holds_part(darkness, ends) is True
    # Looked for across the part instead, its ends would fall on the pocket's dark sides.
    across = pocket_ends(image, u, v, BODY, CAP_MM_PER_PX, OUTLINE, 90.0)
    assert across is not None and across < 0.85


BLACK_MM_PER_PX = 20.343605377340026 / 1920
BLACK_PICK = (760.0, 540.0)
"""The pick location in the black tape frames: columns 200 to 1399 of 1920 x 1080 frames
taken there."""
BLACK_LINE = 5.5
"""12 mm tape: the hole line is 5.5 mm from the pockets, to the picture's left."""


@pytest.mark.parametrize(
    ("name", "pocket"),
    [
        # The pocket's middle (mm down the picture from the pick location, give or take
        # whole sprocket pitches): from the hole in the middle of an empty pocket, or
        # judged from the part and the pocket's walls.
        ("black-tape-empty", 1.08),
        ("black-tape-run", 1.75),
        ("black-tape-8mm-pitch", 0.21),
        ("black-tape-qfn", -0.34),
        ("black-strip", 0.12),
    ],
)
def test_holes_in_black_tape_are_found_by_their_rims(name: str, pocket: float) -> None:
    """Black 12 mm tape on a LumenPnP's Photon feeders, and in a strip on its bed: the
    holes do not stand out as round marks (the feeder's tooth shows bright in each, in a
    dark ring that merges with the tape), but their rims do, and the pocket found from
    them is within 0.25 mm of where it is."""
    image = _frame(name)
    mm, (u, v) = BLACK_MM_PER_PX, BLACK_PICK

    def tape(x: float, y: float) -> tuple[float, float]:
        """Along the tape (down the picture) and across it (toward the holes, left)."""
        return (y - v) * mm, (u - x) * mm

    rims = find_rims(image, HOLE / mm, 0.6 * PITCH / mm)
    fix = pocket_from_holes([tape(c.x, c.y) for c in rims], BLACK_LINE, 8.0)
    assert fix is not None and fix.holes >= 2
    assert abs(fix.across) < 0.25
    off = (fix.along - pocket + PITCH / 2) % PITCH - PITCH / 2
    assert abs(off) < 0.25, (fix, off)


@pytest.mark.parametrize("name", ["black-tape-empty", "black-tape-run", "black-strip"])
def test_holes_in_black_tape_are_not_seen_as_round_marks(name: str) -> None:
    """Why the rims are looked for: as round marks, fewer than two holes are found."""
    image = _frame(name)
    mm, (u, _) = BLACK_MM_PER_PX, BLACK_PICK
    marks = find_circles(image, HOLE * 0.7 / mm, HOLE * 1.3 / mm)
    assert len([c for c in marks if abs((u - c.x) * mm - BLACK_LINE) < 1.0]) < 2


STRIP_LINE = (600.0, 540.0)
"""The hole line runs down the middle of the strip frames (column 600), the camera
centered on it there."""


@pytest.mark.parametrize(
    ("name", "first"),
    [
        # Where the holes are, mm down the picture from the frame's middle (judged: the
        # holes' halves that show).
        ("strip-half-holes", -2.04),
        ("strip-half-holes-led", -2.0),
    ],
)
def test_half_covered_holes_are_found_by_their_rhythm(name: str, first: float) -> None:
    """Black tape in strips whose holder's rail covers half of every hole: the holes are
    neither round marks nor much of a rim, but darken the hole line every 4 mm."""
    image = _frame(name)
    mm = BLACK_MM_PER_PX
    holes = find_hole_rhythm(image, STRIP_LINE, 90.0, PITCH / mm, HOLE / mm, 0.6 / mm, 0.3 / mm)
    assert len(holes) >= 2
    for hole in holes:
        off = (hole.along * mm - first + PITCH / 2) % PITCH - PITCH / 2
        assert abs(off) < 0.2, (holes, off)
    line = [c for c in find_circles(image, 1.05 / mm, 1.95 / mm) if abs(c.x - STRIP_LINE[0]) < 95]
    rims = [c for c in find_rims(image, HOLE / mm, 0.6 * PITCH / mm) if abs(c.x - 600) < 95]
    assert len(line) < 2 and len(rims) < 2


def test_no_rhythm_where_there_are_no_holes() -> None:
    """The other side of a strip: its holder, the part, and the bed beside it."""
    image = _frame("strip-holder-side")
    mm = BLACK_MM_PER_PX
    assert (
        find_hole_rhythm(image, STRIP_LINE, 90.0, PITCH / mm, HOLE / mm, 0.6 / mm, 0.3 / mm) == []
    )
