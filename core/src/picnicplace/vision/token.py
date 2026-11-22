"""Find the calibration token's opening: the round of bare copper in the middle of its top.

The token (docs/hardware/calibration-token.md) is a small white disc of solder mask with a
round opening in the middle where the copper is bare. Its outline is routed, and a tab's
stub can be left on it, so the outline's center moves a little as the token turns; the
opening is set by the mask, round and centered on the copper, and gives a steadier center.

The opening is told by color: ENIG copper is gold, reflecting little blue, and white mask
reflects it all. So the blue channel is dark in the opening and bright round it, whatever
the brightness. A paper punch, white all through, has no opening, and is not a token.
"""

import math

import cv2
import numpy as np

from picnicplace.camera.source import Image
from picnicplace.vision.fiducial import MIN_CIRCULARITY, Circle

MIN_CONTRAST = 40.0
"""The opening's blue must be at least this much (of 255) darker than the mask round it."""
SIZE_RANGE = (0.75, 1.3)
"""The opening may look this much smaller or larger than expected: the camera's scale may
be off a little, and the mask's edge blurs."""
MAX_OFF = 0.3
"""The opening's center must lie within this fraction of the disc's diameter of the disc's
center."""


def find_opening(
    image: Image, x: float, y: float, disc_px: float, opening_px: float
) -> Circle | None:
    """The token's opening, in the disc seen at (``x``, ``y``) pixels, ``disc_px`` across:
    a round darker in blue than the mask round it, about ``opening_px`` across. None if
    there is none, as on a paper disc."""
    if image.ndim != 3 or disc_px <= 0 or opening_px <= 0:
        return None
    h, w = image.shape[:2]
    half = math.ceil(disc_px * 0.6)
    x0, y0 = max(0, round(x) - half), max(0, round(y) - half)
    x1, y1 = min(w, round(x) + half + 1), min(h, round(y) + half + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    blue = cv2.GaussianBlur(image[y0:y1, x0:x1, 0], (5, 5), 0).astype(np.float32)
    yy, xx = np.ogrid[y0:y1, x0:x1]
    dist = np.hypot(xx - x, yy - y)
    inner = blue[dist <= opening_px * 0.3]
    ring = blue[(dist >= opening_px * 0.65) & (dist <= disc_px * 0.4)]
    if inner.size == 0 or ring.size == 0:
        return None
    dark, bright = float(np.median(inner)), float(np.median(ring))
    if bright - dark < MIN_CONTRAST:
        return None
    mask = ((blue < (dark + bright) / 2) & (dist <= disc_px * 0.42)).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    best: Circle | None = None
    for contour in contours:
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)
        if area < 12 or perimeter <= 0:
            continue
        diameter = 2 * math.sqrt(area / math.pi)
        if not SIZE_RANGE[0] * opening_px <= diameter <= SIZE_RANGE[1] * opening_px:
            continue
        circularity = 4 * math.pi * area / perimeter**2
        if circularity < MIN_CIRCULARITY:
            continue
        m = cv2.moments(contour)
        if m["m00"] == 0:
            continue
        cx, cy = x0 + m["m10"] / m["m00"], y0 + m["m01"] / m["m00"]
        off = math.hypot(cx - x, cy - y)
        if off > MAX_OFF * disc_px:
            continue
        if best is None or off < math.hypot(best.x - x, best.y - y):
            best = Circle(cx, cy, diameter, circularity)
    return best
