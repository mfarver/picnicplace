"""Find a part held on a nozzle, in a bottom camera image.

From below, a part's leads or pad ends are bright metal on a darker body, nozzle, and
background. The finder takes the bright pixels near the image center (the nozzle is
over the camera's center), and fits the smallest rotated rectangle around them: its
center, and the direction of one of its sides. A rectangle's sides repeat every 90
degrees, so the caller picks the angle nearest the one it expects.

The brightness threshold suits the simulator's images. Real parts and lighting will
need tuning, from captured frames.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

from picnicplace.camera.source import Image

BRIGHT = 170
"""Gray level above which a pixel counts as metal."""

MIN_PIXELS = 40


@dataclass(frozen=True)
class PartBox:
    u: float
    """Center, pixels from the left edge."""
    v: float
    """Center, pixels from the top edge."""
    width: float
    """Along the side at ``angle``, in pixels."""
    height: float
    angle: float
    """Direction of the box's first side, in image degrees: from +u toward +v."""


def find_part(image: Image, search_px: float, bright: int = BRIGHT) -> PartBox | None:
    """The rotated box around the bright metal within ``search_px`` of the center."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    h, w = gray.shape
    mask = (gray >= bright).astype(np.uint8)
    yy, xx = np.ogrid[:h, :w]
    mask[(xx - w / 2) ** 2 + (yy - h / 2) ** 2 > search_px**2] = 0
    if int(mask.sum()) < MIN_PIXELS:
        return None
    points = cv2.findNonZero(mask)
    (u, v), (bw, bh), angle = cv2.minAreaRect(points)
    # minAreaRect measures in pixel cells; its box runs through pixel centers, so the
    # metal reaches half a pixel further on every side.
    return PartBox(u=u, v=v, width=bw + 1, height=bh + 1, angle=angle)


def nearest_angle(measured: float, expected: float, period: float = 90.0) -> float:
    """``measured`` plus a whole number of periods, as close as possible to ``expected``."""
    return measured + period * round((expected - measured) / period)


def wrap(degrees: float) -> float:
    """An angle in (-180, 180]."""
    a = math.fmod(degrees, 360.0)
    if a <= -180:
        a += 360
    elif a > 180:
        a -= 360
    return a
