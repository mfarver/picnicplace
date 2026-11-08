"""Find a round fiducial in an image.

The image is thresholded both ways (bright on dark, and dark on bright) with Otsu's method,
and each blob is scored by how round it is and whether its size fits. The roundest
fitting blob nearest the image center wins. Its center comes from image moments, which
gives subpixel accuracy.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

from picnicplace.camera.source import Image

MIN_CIRCULARITY = 0.8
"""4πA/P², which is 1 for a perfect circle. Pads and tape pockets score far lower."""


@dataclass(frozen=True)
class Circle:
    x: float
    """Center, in pixels from the left edge."""
    y: float
    """Center, in pixels from the top edge."""
    diameter: float
    circularity: float


def find_circle(image: Image, min_diameter_px: float, max_diameter_px: float) -> Circle | None:
    """The round blob with a diameter in range that is nearest the image center."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    h, w = gray.shape
    candidates: list[Circle] = []
    for layer in (gray, cv2.bitwise_not(gray)):
        _, mask = cv2.threshold(layer, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        for contour in contours:
            area = cv2.contourArea(contour)
            perimeter = cv2.arcLength(contour, True)
            if area < 12 or perimeter <= 0:
                continue
            diameter = 2 * math.sqrt(area / math.pi)
            if not min_diameter_px <= diameter <= max_diameter_px:
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
            candidates.append(
                Circle(m["m10"] / m["m00"], m["m01"] / m["m00"], diameter, circularity)
            )
    if not candidates:
        return None
    center = np.array([w / 2, h / 2])
    return min(candidates, key=lambda c: float(np.hypot(c.x - center[0], c.y - center[1])))
