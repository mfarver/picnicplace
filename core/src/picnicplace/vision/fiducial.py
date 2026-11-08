"""Find a round fiducial in an image.

The image is thresholded at many levels, both ways (bright on dark and dark on bright),
and at each level every blob that is round and the right size is noted. A real fiducial
shows up at many levels in the same place; noise and partial shapes do not. Sweeping
matters for targets with more than two brightness levels, such as the LumenPnP datum
board: a bright copper dot inside a mid-gray ring of bare board inside darker solder
mask. Any single threshold either finds the dot or merges it with its ring.

The fiducial nearest the image center wins. Its center is the median of its detections,
each from image moments, which gives subpixel accuracy.
"""

import math
from dataclasses import dataclass
from statistics import median

import cv2
from cv2.typing import MatLike

from picnicplace.camera.source import Image

MIN_CIRCULARITY = 0.8
"""4πA/P², which is 1 for a perfect circle. Pads, ticks, and tape pockets score lower."""

THRESHOLDS = range(16, 248, 8)

MIN_HITS = 3
"""A fiducial must be found at this many threshold levels."""


@dataclass(frozen=True)
class Circle:
    x: float
    """Center, in pixels from the left edge."""
    y: float
    """Center, in pixels from the top edge."""
    diameter: float
    circularity: float
    hits: int = 1
    """How many threshold levels found it."""


def find_circle(
    image: Image,
    min_diameter_px: float,
    max_diameter_px: float,
    max_offset_px: float | None = None,
) -> Circle | None:
    """The round blob with a diameter in range that is nearest the image center, and
    within ``max_offset_px`` of it if given. The limit keeps a search from locking onto
    a neighboring fiducial when the one wanted is farther off than expected."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    h, w = gray.shape
    hits: list[Circle] = []
    for level in THRESHOLDS:
        for mode in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
            _, mask = cv2.threshold(gray, level, 255, mode)
            hits.extend(_round_blobs(mask, w, h, min_diameter_px, max_diameter_px))
    if not hits:
        return None

    # Group detections of the same blob: centers closer than a quarter of the smallest
    # expected diameter.
    radius = max(2.0, min_diameter_px / 4)
    groups: list[list[Circle]] = []
    for hit in hits:
        for group in groups:
            if math.hypot(hit.x - group[0].x, hit.y - group[0].y) <= radius:
                group.append(hit)
                break
        else:
            groups.append([hit])
    stable = [g for g in groups if len(g) >= MIN_HITS] or groups

    def offset(g: list[Circle]) -> float:
        return math.hypot(g[0].x - w / 2, g[0].y - h / 2)

    if max_offset_px is not None:
        stable = [g for g in stable if offset(g) <= max_offset_px]
        if not stable:
            return None
    best = min(stable, key=offset)
    return Circle(
        x=median(c.x for c in best),
        y=median(c.y for c in best),
        diameter=median(c.diameter for c in best),
        circularity=median(c.circularity for c in best),
        hits=len(best),
    )


def _round_blobs(mask: MatLike, w: int, h: int, min_d: float, max_d: float) -> list[Circle]:
    found: list[Circle] = []
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 12:
            continue
        diameter = 2 * math.sqrt(area / math.pi)
        if not min_d <= diameter <= max_d:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        circularity = 4 * math.pi * area / perimeter**2
        if circularity < MIN_CIRCULARITY:
            continue
        # Blobs cut off by the image edge are not trustworthy.
        x0, y0, bw, bh = cv2.boundingRect(contour)
        if x0 <= 1 or y0 <= 1 or x0 + bw >= w - 1 or y0 + bh >= h - 1:
            continue
        m = cv2.moments(contour)
        if m["m00"] == 0:
            continue
        found.append(Circle(m["m10"] / m["m00"], m["m01"] / m["m00"], diameter, circularity))
    return found
