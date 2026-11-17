"""Bottom vision on real camera frames (tests/data/vision), whose README says what each
shows."""

import asyncio
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from picnicplace.camera.source import Image
from picnicplace.config import Config
from picnicplace.machine.bottom_camera import (
    SHARPNESS_RADIUS_MM,
    focus_radius,
    measure_tip,
    peak,
)
from picnicplace.machine.datum_board import DATUM_MARKS, board_turn
from picnicplace.machine.profile import Point
from picnicplace.placement import SPECK_FRACTION
from picnicplace.recorder import VISION_FUNCTIONS
from picnicplace.vision.fiducial import find_circles, find_hole
from picnicplace.vision.focus import sharpness
from picnicplace.vision.pads import PadRect, fit_pads
from picnicplace.vision.part import BRIGHT, find_part

FRAMES = Path(__file__).parent / "data" / "vision"


def _frame(name: str) -> Image:
    frame = cv2.imread(str(FRAMES / name))
    assert frame is not None
    return np.asarray(frame, dtype=np.uint8)


def test_a_wound_inductors_wire_ends_are_not_pads() -> None:
    # The VLS6045EX's terminals are 1.7 x 4.8 mm, their outer edges 6.0 mm apart; its land
    # pattern's pads are 1.9 x 5.1 mm. The bright copper wire ends below the terminals
    # would stretch the box to 5.4 mm and move its center 0.33 mm. The machine's bottom
    # camera was calibrated at 0.013065 x 0.013035 mm per pixel.
    image = _frame("vls6045ex-bottom.webp")
    h, w = image.shape[:2]
    mm = 0.01305  # millimeters per pixel: the machine's calibrated bottom camera
    search = (5.1 + 3.0) / mm
    speck = SPECK_FRACTION * 1.9 * 5.1 / mm**2
    box = find_part(image, search, BRIGHT, speck)
    assert box is not None
    across, along = sorted((box.width * mm, box.height * mm))
    assert across == pytest.approx(4.8, abs=0.2)
    assert along == pytest.approx(6.0, abs=0.2)
    assert box.angle % 90 == pytest.approx(0, abs=1)
    # The terminals' middle, measured on the frame.
    assert (box.u - w / 2) * mm == pytest.approx(0.06, abs=0.05)
    assert (box.v - h / 2) * mm == pytest.approx(-0.27, abs=0.05)


def test_the_inductors_pads_fit_its_terminals() -> None:
    # The same frame, fitted with the land pattern (two 1.9 x 5.1 mm pads, 4.2 mm apart).
    image = _frame("vls6045ex-bottom.webp")
    mm = 0.01305
    pads = [PadRect(s * 2.1 / mm, 0.0, 1.9 / mm, 5.1 / mm) for s in (-1, 1)]
    fit = fit_pads(image, pads, 0.0, (5.1 + 3.0) / mm)
    assert fit is not None
    assert fit.found == 2
    # The terminals' middle, measured on the frame; they lie square.
    assert (fit.u - 964.7) * mm == pytest.approx(0, abs=0.03)
    assert (fit.v - 519.5) * mm == pytest.approx(0, abs=0.03)
    assert fit.angle == pytest.approx(0, abs=0.5)


def test_a_connectors_pads_are_found_off_its_body() -> None:
    # The B5B-PH-SM4-TB's land pattern, about the middle of its outline, with the leads
    # leaving the body toward +v (down the frame): five 1.0 x 5.5 mm pads on a 2.0 mm
    # pitch, 2.25 mm toward the leads, and two 1.6 x 3.0 mm tabs 6.4 mm either side. The
    # body is as bright as the leads; one box around the bright pixels is the whole part.
    image = _frame("b5b-ph-sm4-tb-bottom.webp")
    mm = 8.0 / 651  # the five leads span 651 pixels
    pads = [PadRect(x / mm, 2.25 / mm, 1.0 / mm, 5.5 / mm) for x in (-4, -2, 0, 2, 4)]
    pads += [PadRect(s * 6.4 / mm, 0.0, 1.6 / mm, 3.0 / mm) for s in (-1, 1)]
    fit = fit_pads(image, pads, 0.0, 9.0 / mm)
    assert fit is not None
    assert fit.found == 7
    assert fit.residual * mm < 0.1
    # Measured on the frame: the leads lean 1.6 degrees, and the tabs' metal is centered
    # at (929.8, 531.7). The tabs are seen edge-on, so where they lie along the leads is
    # less sure than across.
    assert fit.angle == pytest.approx(1.6, abs=0.3)
    assert (fit.u - 929.8) * mm == pytest.approx(0, abs=0.05)
    assert (fit.v - 531.7) * mm == pytest.approx(0, abs=0.25)
    # The middle lead crosses row 810 at u = 922.3.
    middle = fit.u - math.sin(math.radians(fit.angle)) * (810 - fit.v)
    assert (middle - 922.3) * mm == pytest.approx(0, abs=0.05)


class _Frame:
    def __init__(self, image: Image) -> None:
        self.image = image


class _Recorder:
    async def vision(self, name: str, image: Image, *args: object, **_: object) -> object:
        return VISION_FUNCTIONS[name](image, *args)


class _Machine:
    """Enough of the machine service to name a tip from one frame, with the bottom
    camera's scale as the real machine's calibration had it."""

    def __init__(self, image: Image, mm: float) -> None:
        self.profile = Config.model_validate(
            {"machine": {"profile": "lumenpnp-v4.1"}}
        ).machine.machine_profile
        camera = next(c for c in self.profile.cameras if c.looking == "up")
        h, w = image.shape[:2]
        camera.field_of_view = type(camera.field_of_view)(x=w * mm, y=h * mm)
        self.recorder = _Recorder()
        self._image = image

    async def move_nozzle_over(self, *_: object) -> None:
        pass

    async def lower_nozzle(self, *_: object) -> None:
        pass

    async def frame(self, _: str) -> _Frame:
        return _Frame(self._image)


@pytest.mark.parametrize("name", ["lumenpnp-n24-z-4.63.png", "lumenpnp-n24-z-6.49.png"])
def test_an_n24_with_a_lit_ring_round_it_is_named_an_n24(name: str) -> None:
    # The ring round the N24's end is about 8.4 mm across, within an N75's tolerance of its
    # 9 mm end; the edge of its 2.4 mm hole is within an N14's of its 2.7 mm end. Neither
    # has its own hole inside: only the N24 does.
    machine = _Machine(_frame(name), 0.06502)
    nozzle = machine.profile.nozzles[0]
    tip, seen, alike = asyncio.run(measure_tip(machine, nozzle))  # type: ignore[arg-type]
    assert (tip.id if tip else None, alike) == ("N24", [])
    assert seen == pytest.approx(4.0, abs=0.15)


def test_an_n24_on_the_left_nozzle_is_named_an_n24() -> None:
    machine = _Machine(_frame("lumenpnp-n24-left.png"), 0.06218)
    tip, _, alike = asyncio.run(measure_tip(machine, machine.profile.nozzles[0]))  # type: ignore[arg-type]
    assert (tip.id if tip else None, alike) == ("N24", [])


def test_an_n08_in_a_lit_ring_is_not_named_an_n24() -> None:
    # The ring round the N08's end looks like an N24's 4 mm end, and the N08's own end
    # inside it like the N24's hole, but it is lit: a hole is dark all through. The N08's
    # end barely stands out from the ring, so the camera cannot name it, and says so
    # rather than name it wrong.
    machine = _Machine(_frame("lumenpnp-n08-right.png"), 0.06218)
    tip, _, alike = asyncio.run(measure_tip(machine, machine.profile.nozzles[1]))  # type: ignore[arg-type]
    assert (tip.id if tip else None, alike) == (None, [])


def test_the_datum_boards_own_marks_are_where_they_are_said_to_be() -> None:
    # Stitched from a real LumenPnP's top camera (see the README). The board faces the
    # bottom camera, in front of it (toward -Y), so it is turned half round.
    image = _frame("lumenpnp-datum-top.webp")
    mm, left, top = 0.05, 168.0, 214.0
    dot = Point(x=218.107, y=197.304)
    s = board_turn(dot, 151.5)
    assert s == -1
    seen = [
        (left + c.x * mm, top - c.y * mm, c.diameter * mm, int(image[round(c.y), round(c.x)].max()))
        for c in find_circles(image, 3.5 / mm, 7.5 / mm)
    ]
    for mark in DATUM_MARKS:
        x, y = dot.x - s * mark.dx, dot.y - s * mark.dy
        near = [m for m in seen if math.hypot(m[0] - x, m[1] - y) < 0.3]
        assert len(near) == 1, (mark, seen)
        assert near[0][2] == pytest.approx(mark.diameter, abs=0.2)
        # As bright as paper: brightness cannot tell them from a paper disc.
        assert near[0][3] >= 200, (mark, near)


def test_focus_is_found_round_the_tips_end_not_the_nozzle_behind_it() -> None:
    # A real N045 over a LumenPnP's bottom camera at 13 heights (see the README). Going
    # down, the tip's end comes into focus first; the nozzle's larger rings, higher up,
    # come into focus lower down, so a patch 8 mm across goes on getting sharper past the
    # end's focus, and a search scored on it ran off its end. Scored round the end, the
    # peak is in the band where the end is sharp.
    frames = sorted(
        (float(p.stem.removeprefix("z")), _frame(f"n045-focus/{p.name}"))
        for p in (FRAMES / "n045-focus").glob("z*.webp")
    )
    frames.reverse()  # from the top down
    heights = [z for z, _ in frames]
    assert len(heights) == 13
    mm = 0.038
    tip = Config.model_validate({"machine": {"profile": "lumenpnp-v4.1"}}).machine.machine_profile
    n045 = tip.nozzle_tip("N045")

    def scores(radius_mm: float) -> list[float]:
        # Round the tip, as calibrating centers it before the focus search.
        out: list[float] = []
        for _, image in frames:
            h, w = image.shape[:2]
            out.append(sharpness(image, w / 2 + 18, h / 2 + 15, radius_mm / mm))
        return out

    at, _ = peak(heights, scores(focus_radius(n045)), 0.25)
    assert -27.2 <= at <= -25.8, at
    wide, _ = peak(heights, scores(SHARPNESS_RADIUS_MM), 0.25)
    assert wide == heights[-1]


def test_a_tips_hole_moves_as_its_end_does_not_as_the_rings_behind_it() -> None:
    """A real N24 near a LumenPnP's bottom camera's focus, the head stepped 1 mm each way in
    X. A lit flange about 10 mm across behind the tip, farther from the camera, moved 18.7
    pixels per mm and was found first, which made the scale 0.0535 mm per pixel; the tip's
    hole, at its end, moves 26.6, and its size says so: 2.4 mm at that scale."""
    left = find_hole(_frame("lumenpnp-n24-hole-x-1.webp"), 8, 480, 486)
    right = find_hole(_frame("lumenpnp-n24-hole-x+1.webp"), 8, 480, 486)
    assert left is not None and right is not None
    per_mm = math.hypot(right.x - left.x, right.y - left.y) / 2
    assert 25.0 < per_mm < 28.5
    mm = 1 / per_mm
    assert left.diameter * mm == pytest.approx(2.4, abs=0.2)


def test_a_tips_hole_is_found_where_its_end_is_not() -> None:
    """The same N24 turned 180° at the focus, 0.0362 mm per pixel: its end was not found as
    a circle of the N24's size, and the calibration failed; its hole is plain."""
    mm = 0.0362
    hole = find_hole(_frame("lumenpnp-n24-hole-turned.webp"), 2.4 / 1.3 / mm, 2.4 * 1.3 / mm, 110)
    assert hole is not None
    assert hole.diameter * mm == pytest.approx(2.4, abs=0.2)
    assert math.hypot(hole.x - 960, hole.y - 540) < 30


@pytest.mark.parametrize(
    ("name", "nozzle", "expected"),
    [("lumenpnp-n24-focus-left.webp", 0, "N24"), ("lumenpnp-n75-focus-right.webp", 1, "N75")],
)
def test_tips_are_named_at_the_bottom_cameras_real_focus(
    name: str, nozzle: int, expected: str
) -> None:
    """In focus, at the scale the bottom camera measured by the tip's hole (0.03469 mm per
    pixel). The N24's end looks 4.9 mm across, past its 4.0 mm and the tolerance, and is
    named by its hole, 2.4 mm and dark; the N75's 7.5 mm hole is a cup, lit inside, with
    the nozzle's own dark bore at the bottom, and it is named by its 9 mm end."""
    machine = _Machine(_frame(name), 0.03469)
    tip, _, alike = asyncio.run(measure_tip(machine, machine.profile.nozzles[nozzle]))  # type: ignore[arg-type]
    assert (tip.id if tip else None, alike) == (expected, [])
