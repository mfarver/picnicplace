"""Bottom vision on real camera frames (tests/data/vision), whose README says what each
shows."""

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from picnicplace.camera.source import Image
from picnicplace.placement import SPECK_FRACTION
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
