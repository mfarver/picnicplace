"""Find a round fiducial in an image.

The image is thresholded at many levels, both ways (bright on dark and dark on bright),
and at each level every blob that is round and the right size is noted. A real fiducial
shows up at many levels in the same place; noise and partial shapes do not. Sweeping
matters for targets with more than two brightness levels, such as the LumenPnP datum
board: a bright copper dot inside a mid-gray ring of bare board inside darker solder
mask. Any single threshold either finds the dot or merges it with its ring.

Of the blobs found, the strongest ones are kept: those found at many levels and, when
the expected size is given, close to it. A silkscreen letter such as the "D" of a "FID1"
label can be round enough and in the size range, but it is found at few levels and is
smaller than the fiducial (seen on a real board). Of the strong ones, the one nearest the
image center wins, which picks the right one of several alike, such as sprocket holes.
Where the mark's polarity is known, the other kind is not looked for: a feeder's copper
fiducial is bright, and a sprocket hole in loose tape beside it is dark (seen on a real
machine).
Its center is the median of its detections, each from image moments, which gives
subpixel accuracy.
"""

import math
from dataclasses import dataclass
from statistics import median
from typing import Literal

import cv2
import numpy as np
from cv2.typing import MatLike

from picnicplace.camera.source import Image

MIN_CIRCULARITY = 0.8
"""4πA/P², which is 1 for a perfect circle. Pads, ticks, and tape pockets score lower."""

THRESHOLDS = range(16, 248, 8)

LEVELS = 29
"""For faint marks: threshold levels spread across the frame's own range of brightness,
so a dim frame gets finer steps than a bright one, and a mark only a little darker than
its surroundings is still found at several levels."""
RANGE = (1.0, 99.0)
"""The frame's range of brightness: these percentiles of it."""
MIN_SPAN = 32.0
"""The levels span at least this much (of 255), so a frame of nearly one brightness is
not cut finer than its noise."""

MIN_HITS = 3
"""A fiducial must be found at this many threshold levels."""

SIZE_TOLERANCE = 0.2
"""How quickly a blob's strength falls as its size departs from the expected size: a blob
this fraction off (on a log scale) keeps 1/e of its strength. Generous, because the camera
scale may not be calibrated yet."""

STRONG_FRACTION = 0.5
"""Blobs weaker than this fraction of the strongest are not considered."""

Polarity = Literal["bright", "dark"]
"""Which marks to look for: brighter than around them (a copper fiducial on solder mask),
or darker (a hole in paper tape). None looks for both."""


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


def find_circles(
    image: Image,
    min_diameter_px: float,
    max_diameter_px: float,
    open_px: int = 0,
    polarity: Polarity | None = None,
    faint: bool = False,
) -> list[Circle]:
    """Every round blob with a diameter in range, each found at several threshold levels
    (or, if none is, every one found at all). ``open_px``: first wipe out anything
    narrower than this, such as silkscreen lines touching a paper disc, which would
    otherwise merge with it at every level that finds it. ``polarity``: only bright marks,
    or only dark ones. ``faint``: marks that differ little from around them, under a tinted
    light (sprocket holes under a real LumenPnP's blue top light): each pixel's brightest
    channel, at levels spread across the frame's range. Not for everything: gray suits the
    bottom camera's nozzle tips better, and a fiducial's strength is how many of the fixed
    levels find it."""
    gray = brightness(image) if faint else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    h, w = gray.shape
    kernel = (
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_px | 1, open_px | 1))
        if open_px > 1
        else None
    )
    modes = {
        None: (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV),
        "bright": (cv2.THRESH_BINARY,),
        "dark": (cv2.THRESH_BINARY_INV,),
    }[polarity]
    hits: list[Circle] = []
    for level in levels(gray) if faint else THRESHOLDS:
        for mode in modes:
            _, mask = cv2.threshold(gray, level, 255, mode)
            if kernel is not None:
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            hits.extend(
                _round_blobs(
                    mask, w, h, min_diameter_px, max_diameter_px, outer_only=polarity is not None
                )
            )
    if not hits:
        return []

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
    return [
        Circle(
            x=median(c.x for c in g),
            y=median(c.y for c in g),
            diameter=median(c.diameter for c in g),
            circularity=median(c.circularity for c in g),
            hits=len(g),
        )
        for g in stable
    ]


def brightness(image: Image) -> Image:
    """Each pixel's brightest channel. Under a tinted light (a real LumenPnP's top light is
    blue), gray weighs the light's color least, and kept a tenth of the contrast between
    light tape and its sprocket holes; the brightest channel keeps it all."""
    return image.max(axis=2) if image.ndim == 3 else image


def levels(gray: MatLike) -> list[float]:
    """The threshold levels for a frame: ``LEVELS`` across its range of brightness."""
    low, high = (float(v) for v in np.percentile(gray, RANGE))
    if high - low < MIN_SPAN:
        middle = min(max((low + high) / 2, MIN_SPAN / 2), 255 - MIN_SPAN / 2)
        low, high = middle - MIN_SPAN / 2, middle + MIN_SPAN / 2
    step = (high - low) / (LEVELS + 1)
    return [low + step * (k + 1) for k in range(LEVELS)]


def find_circle(
    image: Image,
    min_diameter_px: float,
    max_diameter_px: float,
    max_offset_px: float | None = None,
    expected_diameter_px: float | None = None,
    open_px: int = 0,
    polarity: Polarity | None = None,
) -> Circle | None:
    """The round blob with a diameter in range that is nearest the image center, among
    the strongest (see ``strength``), and within ``max_offset_px`` of the center if given.
    The limit keeps a search from locking onto a neighboring fiducial when the one wanted
    is farther off than expected. ``polarity``: as for ``find_circles``."""
    h, w = image.shape[:2]
    circles = find_circles(image, min_diameter_px, max_diameter_px, open_px, polarity)

    def offset(c: Circle) -> float:
        return math.hypot(c.x - w / 2, c.y - h / 2)

    if max_offset_px is not None:
        circles = [c for c in circles if offset(c) <= max_offset_px]
    if not circles:
        return None
    best = max(strength(c, expected_diameter_px) for c in circles)
    strong = [c for c in circles if strength(c, expected_diameter_px) >= STRONG_FRACTION * best]
    return min(strong, key=offset)


def find_hole(
    image: Image,
    min_diameter_px: float,
    max_diameter_px: float,
    max_offset_px: float | None = None,
) -> Circle | None:
    """A dark round hole with a diameter in range, within ``max_offset_px`` of the image
    center if given: of the dark round blobs found, the darkest inside, such as a nozzle
    tip's hole seen from below against its lit end. Darkest, not nearest the center or
    found at the most levels: a ring behind the end, or the end's own gray, can make a
    larger dark circle about as often (seen on a real LumenPnP)."""
    h, w = image.shape[:2]
    circles = find_circles(image, min_diameter_px, max_diameter_px, polarity="dark")
    if max_offset_px is not None:
        circles = [c for c in circles if math.hypot(c.x - w / 2, c.y - h / 2) <= max_offset_px]
    if not circles:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image

    def inside(c: Circle) -> float:
        r = max(1, round(c.diameter * HOLE_CORE))
        x0, y0 = max(0, round(c.x) - r), max(0, round(c.y) - r)
        patch = gray[y0 : round(c.y) + r + 1, x0 : round(c.x) + r + 1]
        return float(patch.mean()) if patch.size else 255.0

    return min(circles, key=inside)


HOLE_CORE = 0.25
"""A hole's darkness is its middle's: a square this fraction of its diameter each way
from its center, clear of its blurred edge."""

RIM_SIZE = 0.15
"""A rim is looked for within this fraction of the expected diameter either way."""
RIM_VOTES = 0.1
"""How much of a rim's edge must be seen, as a fraction of its circumference."""
RIM_STEP = 1.5
"""The rim search's accumulator cell, in pixels: coarser is faster and less fussy about a
rim that is not a perfect circle."""


def find_rims(image: Image, diameter_px: float, min_gap_px: float) -> list[Circle]:
    """Round rims of a known diameter, from their edges alone (a Hough transform), at least
    ``min_gap_px`` apart. For holes that do not stand out as blobs: in black carrier tape
    on a LumenPnP's Photon feeders, a sprocket hole shows the feeder's bright tooth inside
    a dark ring that merges with the tape, and in a strip a holder's edge may cover part
    of a hole; either way some of its rim still shows (seen on a real machine). The frame's
    contrast is evened out first, so a dim frame of black tape still has edges. Finds
    plenty of other round things too: the caller keeps those where holes should be."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    gray = cv2.GaussianBlur(gray, (0, 0), 2)
    radius = diameter_px / 2
    votes = max(10, round(RIM_VOTES * 2 * math.pi * radius / RIM_STEP))
    found = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=RIM_STEP,
        minDist=min_gap_px,
        param1=60,
        param2=votes,
        minRadius=int(radius * (1 - RIM_SIZE)),
        maxRadius=math.ceil(radius * (1 + RIM_SIZE)),
    )
    if found is None:
        return []
    return [Circle(float(x), float(y), 2 * float(r), circularity=1.0) for x, y, r in found[0]]


def strength(circle: Circle, expected_diameter_px: float | None = None) -> float:
    """How sure the finder is that a blob is the fiducial: the threshold levels it was
    found at, less the further its size is from the expected one."""
    if not expected_diameter_px:
        return float(circle.hits)
    off = math.log(circle.diameter / expected_diameter_px) / SIZE_TOLERANCE
    return circle.hits * math.exp(-(off**2))


def _round_blobs(
    mask: MatLike, w: int, h: int, min_d: float, max_d: float, outer_only: bool = False
) -> list[Circle]:
    """Round blobs in a mask. Every edge counts, a hole's as well as a blob's, unless
    ``outer_only``: then a dark hole in a bright area is not taken for a bright mark."""
    found: list[Circle] = []
    if outer_only:
        # Two levels: the outer edges of blobs (no parent), and the edges of holes in them.
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        if contours:  # with none, OpenCV gives no hierarchy either
            contours = [c for c, link in zip(contours, hierarchy[0], strict=True) if link[3] < 0]
    else:
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
