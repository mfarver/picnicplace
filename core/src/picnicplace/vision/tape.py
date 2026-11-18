"""Look at paper carrier tape: whether a pocket holds a part, and parts lying loose on it.

Under the top camera, paper tape is the brightest thing in view. An empty pocket shows
its paper floor, darker than the tape's top but even; a part in it shows its body, much
darker (a resistor's is black), between its metal ends. On a LumenPnP's top camera
(2026-11-18, 0603 resistors in 8 mm paper tape) a pocket with a part had 26 to 80% of
its middle darker than a third of the paper's brightness; an empty pocket had at most 6%.

A part that jumps out of its pocket as the cover tape is peeled lies on the tape beside
the pockets, right side up or upside down (its white back is a little darker than the
paper). Anything darker than the paper inside the tape, away from the pockets and the
sprocket holes and as big as a small part, is taken for one.

Embossed tape shows the feeder through clear pockets, or is black all through: these
checks are only for paper tape.
"""

import math

import cv2
import numpy as np
from cv2.typing import MatLike

from picnicplace.camera.source import Image

PART_DARK = 0.35
"""A pixel darker than this fraction of the paper's brightness is a part's body."""
PART_FRACTION = 0.15
"""A pocket holds a part if at least this much of its middle is that dark."""
CORE = 0.6
"""The middle looked at: a square this fraction of the part's smaller side, clear of its
metal ends and of the pocket's walls."""
RING_GAP_MM = 0.5
"""The paper round the pocket is looked at this far past the part's larger half-side..."""
RING_WIDTH_MM = 0.4
"""...in a ring this wide."""
RING_EVEN = 0.75
"""The ring is paper if its darker pixels (20th percentile) are at least this fraction of
its median: not cut by the image's edge or the feeder."""
RING_MIN = 40.0
"""...and its median is at least this bright (of 255)."""
LOOSE_DARK = 0.88
"""Inside the tape, a pixel darker than this fraction of the paper is not paper."""
LOOSE_MIN_AREA_MM2 = 0.4
"""A loose part covers at least this much: an 0402 is 0.5 mm²."""
LOOSE_MIN_SIDE_MM = 0.4
"""...and is at least this wide: thin marks (a crease, a printed line) are not parts."""
EDGE_MM = 0.3
"""Loose parts are looked for this far inside the tape's edges."""


def _gray(image: Image) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return cv2.GaussianBlur(gray, (5, 5), 0)


def pocket_darkness(
    image: Image,
    x: float,
    y: float,
    body: tuple[float, float],
    mm_per_px: float,
    outline: tuple[float, float] | None = None,
) -> float | None:
    """How much of the middle of the pocket at (``x``, ``y``) pixels is as dark as a
    part's body, 0 to 1; the part's body is ``body`` mm (length, width), and its outline,
    leads and all, ``outline`` (default: the body): the pocket is a little larger. None if
    the paper round the pocket is not all in view, so the pocket cannot be judged."""
    gray = _gray(image)
    h, w = gray.shape
    big, small = max(outline or body) / 2, min(body) / 2
    r1 = (big + RING_GAP_MM) / mm_per_px
    r2 = r1 + RING_WIDTH_MM / mm_per_px
    if x - r2 < 0 or y - r2 < 0 or x + r2 >= w or y + r2 >= h:
        return None
    x0, y0 = math.floor(x - r2), math.floor(y - r2)
    patch = gray[y0 : math.ceil(y + r2) + 1, x0 : math.ceil(x + r2) + 1].astype(np.float32)
    yy, xx = np.mgrid[0 : patch.shape[0], 0 : patch.shape[1]]
    dist = np.hypot(xx + x0 - x, yy + y0 - y)
    ring = patch[(dist >= r1) & (dist <= r2)]
    paper = float(np.median(ring))
    if paper < RING_MIN or float(np.percentile(ring, 20)) < RING_EVEN * paper:
        return None
    half = max(1, round(CORE * small / mm_per_px))
    core = patch[(np.abs(xx + x0 - x) <= half) & (np.abs(yy + y0 - y) <= half)]
    return float((core < PART_DARK * paper).mean())


def holds_part(darkness: float | None) -> bool | None:
    """Whether a pocket holds a part, from ``pocket_darkness``; None if it could not be
    judged."""
    return None if darkness is None else darkness >= PART_FRACTION


def loose_parts(
    image: Image, keep_out: list[list[tuple[float, float]]], mm_per_px: float
) -> list[tuple[float, float, float]]:
    """Parts lying loose on the paper tape in view: each one's center (x, y pixels) and
    area (mm²). ``keep_out``: polygons (corners in pixels) where darker is expected, the
    pockets and the sprocket holes."""
    gray = _gray(image)
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bright)
    if n < 2:
        return []
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    tape: MatLike = np.where(labels == biggest, 255, 0).astype(np.uint8)
    paper = float(np.median(gray[tape > 0]))
    # The tape with whatever lies on it or is cut in it: its outline, filled.
    inside = np.zeros_like(tape)
    outline, _ = cv2.findContours(tape, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cv2.drawContours(inside, outline, -1, 255, -1)
    edge = max(1, round(EDGE_MM / mm_per_px)) * 2 + 1
    inside = cv2.erode(inside, np.ones((edge, edge), np.uint8))
    odd = ((gray < LOOSE_DARK * paper) & (inside > 0)).astype(np.uint8) * 255
    for corners in keep_out:
        cv2.fillPoly(odd, [np.array([(round(x), round(y)) for x, y in corners], np.int32)], 0)
    speck = max(3, round(0.1 / mm_per_px)) | 1
    odd = cv2.morphologyEx(odd, cv2.MORPH_OPEN, np.ones((speck, speck), np.uint8))
    found: list[tuple[float, float, float]] = []
    blobs, _ = cv2.findContours(odd, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    for blob in blobs:
        area = cv2.contourArea(blob) * mm_per_px**2
        (cx, cy), (bw, bh), _ = cv2.minAreaRect(blob)
        if area < LOOSE_MIN_AREA_MM2 or min(bw, bh) * mm_per_px < LOOSE_MIN_SIDE_MM:
            continue
        found.append((float(cx), float(cy), round(area, 2)))
    return found
