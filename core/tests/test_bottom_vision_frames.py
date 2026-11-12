"""Bottom vision on real camera frames (tests/data/vision)."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from picnicplace.placement import SPECK_FRACTION
from picnicplace.vision.part import BRIGHT, find_part

FRAMES = Path(__file__).parent / "data" / "vision"


def test_a_wound_inductors_wire_ends_are_not_pads() -> None:
    # The VLS6045EX's terminals are 1.7 x 4.8 mm, their outer edges 6.0 mm apart; its land
    # pattern's pads are 1.9 x 5.1 mm. The bright copper wire ends below the terminals
    # would stretch the box to 5.4 mm and move its center 0.33 mm. The machine's bottom
    # camera was calibrated at 0.013065 x 0.013035 mm per pixel.
    frame = cv2.imread(str(FRAMES / "vls6045ex-bottom.webp"))
    assert frame is not None
    image = np.asarray(frame, dtype=np.uint8)
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
