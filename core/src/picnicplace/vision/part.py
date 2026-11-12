"""Find a part held on a nozzle, in a bottom camera image.

From below, a part's leads or pad ends are bright metal on a darker body, nozzle, and
background. The finder takes the bright pixels near the image center (the nozzle is
over the camera's center), and fits the smallest rotated rectangle around them: its
center, and the direction of one of its sides. A rectangle's sides repeat every 90
degrees, so the caller picks the angle nearest the one it expects.

Not everything bright is a pad: a wound inductor's wire ends, a glint on a nozzle, or a
fleck of solder can shine too, and one outside the pads stretches the rectangle and moves
its center. Given the smallest pad's area, the finder leaves out bright specks much
smaller than any pad.

Nor is every bright thing metal: a white or ivory plastic body can be nearly as bright
as tin. Plastics like that are yellowish, and tin and solder are neutral or slightly
blue, so the metal mask can leave out bright pixels warmer than a limit.

The brightness threshold suits the simulator's images. Real parts and lighting will
need tuning, from captured frames.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np
from cv2.typing import MatLike

from picnicplace.camera.source import Image

BRIGHT = 170
"""Gray level above which a pixel counts as metal."""

WARM = -2.0
"""Pixels yellower than this (Lab b* minus a*, averaged over ``CHROMA_BLUR`` pixels) are
plastic, not metal, when asked. On a LumenPnP's bottom camera tin reads about -6 to -12,
and ivory nylon -1 to +13."""
CHROMA_BLUR = 9
"""Color is averaged over this many pixels before it is judged: cameras and compressed
frames keep color at a coarser grain than brightness."""
EDGE_OPEN = 5
"""Where a plastic body meets the dark background, its averaged color turns neutral in a
thin line along its outline; an opening this many pixels across takes such lines out."""

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


def metal(
    image: Image,
    search_px: float,
    bright: int = BRIGHT,
    min_blob_px: float = 0,
    warm: float | None = None,
) -> MatLike:
    """The bright metal within ``search_px`` of the center, as a mask (1 for metal),
    leaving out bright specks smaller than ``min_blob_px`` pixels and, given ``warm``,
    bright pixels yellower than it (a white or ivory plastic body)."""
    smooth = cv2.GaussianBlur(image, (3, 3), 0)
    gray = cv2.cvtColor(smooth, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    mask = (gray >= bright).astype(np.uint8)
    if warm is not None:
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
        yellow = cv2.GaussianBlur(lab[:, :, 2] - lab[:, :, 1], (CHROMA_BLUR, CHROMA_BLUR), 0)
        mask[yellow > warm] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((EDGE_OPEN, EDGE_OPEN), np.uint8))
    yy, xx = np.ogrid[:h, :w]
    mask[(xx - w / 2) ** 2 + (yy - h / 2) ** 2 > search_px**2] = 0
    if min_blob_px > 0:
        _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        small = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] < min_blob_px)
        mask[np.isin(labels, small[small > 0])] = 0
    return mask


def find_part(
    image: Image, search_px: float, bright: int = BRIGHT, min_blob_px: float = 0
) -> PartBox | None:
    """The rotated box around the bright metal within ``search_px`` of the center, leaving
    out bright specks smaller than ``min_blob_px`` pixels."""
    mask = metal(image, search_px, bright, min_blob_px)
    if cv2.countNonZero(mask) < MIN_PIXELS:
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
