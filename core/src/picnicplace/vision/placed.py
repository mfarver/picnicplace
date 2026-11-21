"""Find a chip where it was just placed, from above: where it landed and how it is turned.

Seen from the top camera, a two-terminal chip (a resistor, a capacitor, an LED) is a dark
body between two bright metal end caps. The finder correlates that pattern (bright cap,
dark body, bright cap, filling the chip's outline) with the picture, normalized so the
light does not matter, at positions about the picture's middle and turns about the one
expected, and keeps the best. The picture is first resampled to square pixels, since a
camera's two scales may differ.

On a real LumenPnP (2026-11-18, 0603 resistors on double-sided tape over an Opulo FTP), it
found a part 0.43 mm off its target and turned 9 degrees, as it lay, and the others within
0.15 mm. Across a part's width it can be some 0.1 mm off: the board's pad under each cap is
wider than the part, so the pattern can slide that way a little. It is a check that a part
landed where it should (not off its pads, standing up, or missing), not a measure of
placement accuracy to a few hundredths.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from picnicplace.camera.source import Image

GRID_MM = 0.01
"""The picture is resampled to square pixels this size before matching."""
CAP_FRACTION = 0.2
"""Each end cap is taken as this fraction of the chip's length (an 0603's are 0.3 mm)."""
TURN_STEP = 0.5
"""Turns are tried this many degrees apart."""


@dataclass(frozen=True)
class PlacedChip:
    u: float
    """The chip's center, pixels from the picture's left edge."""
    v: float
    """...and from its top edge."""
    angle: float
    """The chip's long axis, image degrees from +u toward -v (counterclockwise as shown)."""
    score: float
    """How well the pattern matched, -1 to 1 (normalized correlation)."""


def find_placed_chip(
    image: Image,
    mm_per_px_x: float,
    mm_per_px_y: float,
    length: float,
    width: float,
    angle: float,
    search_mm: float = 0.6,
    turn: float = 15.0,
) -> PlacedChip | None:
    """The chip ``length`` x ``width`` mm nearest the picture's middle, within
    ``search_mm`` of it and ``turn`` degrees of ``angle`` (image degrees, as in
    ``PlacedChip``); None if the picture is too small to look in."""
    h, w = image.shape[:2]
    reach = length / 2 + search_mm + 0.3
    rx, ry = reach / mm_per_px_x, reach / mm_per_px_y
    x0, y0 = max(0, int(w / 2 - rx)), max(0, int(h / 2 - ry))
    x1, y1 = min(w, int(w / 2 + rx)), min(h, int(h / 2 + ry))
    crop = image[y0:y1, x0:x1]
    sx, sy = mm_per_px_x / GRID_MM, mm_per_px_y / GRID_MM
    size = (max(1, round(crop.shape[1] * sx)), max(1, round(crop.shape[0] * sy)))
    crop = cv2.resize(crop, size, interpolation=cv2.INTER_AREA)
    gray = np.asarray(
        cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop, dtype=np.float32
    )
    height_px, width_px = gray.shape
    tl, tw = round(length / GRID_MM), round(width / GRID_MM)
    cap = max(1, round(CAP_FRACTION * length / GRID_MM))
    s = round(search_mm / GRID_MM)
    if width_px < tl + 2 * s or height_px < tw + 2 * s:
        return None
    pattern = np.zeros((tw, tl), np.float32)
    pattern[:, :cap] = 1
    pattern[:, -cap:] = 1
    # The middle of the picture, in the resampled crop.
    mx, my = (w / 2 - x0) * sx, (h / 2 - y0) * sy
    best: tuple[float, float, float, float] | None = None
    for k in range(-round(turn / TURN_STEP), round(turn / TURN_STEP) + 1):
        a = angle + k * TURN_STEP
        # Turn the picture so a chip at ``a`` lies along +u.
        m = cv2.getRotationMatrix2D((mx, my), -a, 1.0)
        turned = cv2.warpAffine(gray, m, (width_px, height_px), borderMode=cv2.BORDER_REPLICATE)
        res = cv2.matchTemplate(turned, pattern, cv2.TM_CCOEFF_NORMED)
        cx, cy = round(mx - tl / 2), round(my - tw / 2)
        lo_y, lo_x = max(0, cy - s), max(0, cx - s)
        window = res[lo_y : cy + s + 1, lo_x : cx + s + 1]
        if window.size == 0:
            continue
        iy, ix = np.unravel_index(int(np.argmax(window)), window.shape)
        score = float(window[iy, ix])
        if best is None or score > best[0]:
            px, py = lo_x + ix + tl / 2, lo_y + iy + tw / 2
            back = cv2.invertAffineTransform(m)
            u = back[0, 0] * px + back[0, 1] * py + back[0, 2]
            v = back[1, 0] * px + back[1, 1] * py + back[1, 2]
            best = (score, float(u), float(v), a)
    if best is None:
        return None
    score, u, v, a = best
    return PlacedChip(u=x0 + u / sx, v=y0 + v / sy, angle=a, score=score)


def chip_offset(found: PlacedChip, w: int, h: int, mm_x: float, mm_y: float) -> tuple[float, float]:
    """Where a found chip is from the picture's middle: mm right and mm down."""
    return (found.u - w / 2) * mm_x, (found.v - h / 2) * mm_y


Polygon = Sequence[tuple[float, float]]
"""Corners in mm from the picture's middle: right and down."""


@dataclass(frozen=True)
class FoundPads:
    dx: float
    """Where the pads are from where they were expected: mm right..."""
    dy: float
    """...and mm down."""
    score: float
    """How well the pads' shape matched the bright copper, 0 to 1."""


def find_board_pads(
    image: Image,
    mm_per_px_x: float,
    mm_per_px_y: float,
    pads: Sequence[Polygon],
    search_mm: float = 0.5,
) -> FoundPads | None:
    """The footprint's ``pads`` (as expected about the picture's middle) where they really
    are, within ``search_mm``; None if the picture is too small to look in.

    Bare copper is the brightest thing on a board, so the picture is split into bright and
    dark (Otsu's threshold, so the light does not matter) and the pads' shape slid over the
    bright part. A part's metal ends are bright too, and lie over the pads, so a part off
    its pads pulls the fit its way, but barely: by 0.015 mm at most for an 0603 drawn 0.35
    mm off them."""
    corners = np.array([p for pad in pads for p in pad], np.float64)
    if corners.size == 0:
        return None
    reach = float(np.abs(corners).max()) + search_mm + 0.3
    h, w = image.shape[:2]
    rx, ry = reach / mm_per_px_x, reach / mm_per_px_y
    x0, y0 = max(0, int(w / 2 - rx)), max(0, int(h / 2 - ry))
    x1, y1 = min(w, int(w / 2 + rx)), min(h, int(h / 2 + ry))
    crop = image[y0:y1, x0:x1]
    sx, sy = mm_per_px_x / GRID_MM, mm_per_px_y / GRID_MM
    size = (max(1, round(crop.shape[1] * sx)), max(1, round(crop.shape[0] * sy)))
    crop = cv2.resize(crop, size, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    _, bright = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = bright.astype(np.float32)
    # The pads drawn as a template, a margin of dark about them.
    margin = 0.2
    lo = corners.min(axis=0) - margin
    hi = corners.max(axis=0) + margin
    tw, th = round((hi[0] - lo[0]) / GRID_MM), round((hi[1] - lo[1]) / GRID_MM)
    template = np.zeros((th, tw), np.float32)
    for pad in pads:
        pts = np.array([((x - lo[0]) / GRID_MM, (y - lo[1]) / GRID_MM) for x, y in pad])
        cv2.fillPoly(template, [np.round(pts).astype(np.int32)], (1.0,))
    s = round(search_mm / GRID_MM)
    # Where the template's corner falls if the pads are where they were expected.
    mx, my = (w / 2 - x0) * sx, (h / 2 - y0) * sy
    cx, cy = round(mx + lo[0] / GRID_MM), round(my + lo[1] / GRID_MM)
    if cx - s < 0 or cy - s < 0:
        return None
    if cx + s + tw > bright.shape[1] or cy + s + th > bright.shape[0]:
        return None
    window = bright[cy - s : cy + s + th, cx - s : cx + s + tw]
    res = cv2.matchTemplate(window, template, cv2.TM_CCORR_NORMED)
    iy, ix = np.unravel_index(int(np.argmax(res)), res.shape)
    return FoundPads(
        dx=(int(ix) - s) * GRID_MM, dy=(int(iy) - s) * GRID_MM, score=float(res[iy, ix])
    )


def chip_ends(
    found: PlacedChip, w: int, h: int, mm_x: float, mm_y: float, length: float, width: float
) -> list[Polygon]:
    """A found chip's two metal ends, as polygons in mm from the picture's middle."""
    cu, cv_ = chip_offset(found, w, h, mm_x, mm_y)
    a = math.radians(found.angle)
    # Along the chip, and across it, in mm right and down (image degrees: v grows down).
    ux, uy = math.cos(a), -math.sin(a)
    vx, vy = math.sin(a), math.cos(a)
    cap = CAP_FRACTION * length
    ends: list[Polygon] = []
    for side in (-1, 1):
        mid = side * (length - cap) / 2
        ends.append(
            [
                (
                    cu + (mid + p * cap / 2) * ux + q * width / 2 * vx,
                    cv_ + (mid + p * cap / 2) * uy + q * width / 2 * vy,
                )
                for p, q in ((-1, -1), (1, -1), (1, 1), (-1, 1))
            ]
        )
    return ends


def on_pads(ends: Sequence[Polygon], pads: Sequence[Polygon]) -> float:
    """How much of a chip's ends lie on pads: of each end, the most of it over any one pad,
    and of those the least, 0 to 1. A chip centered on 0603 pads gives 1."""
    least = 1.0
    for end in ends:
        e = np.array(end, np.float32)
        area = cv2.contourArea(e)
        if area <= 0:
            return 0.0
        best = 0.0
        for pad in pads:
            shared, _ = cv2.intersectConvexConvex(e, np.array(pad, np.float32))
            best = max(best, shared / area)
        least = min(least, best)
    return least


def image_turn(dx: float, dy: float) -> float:
    """An image direction (right, down) as image degrees (counterclockwise as shown)."""
    return math.degrees(math.atan2(-dy, dx))


TARGET_COLOR = (60, 200, 255)
FOUND_COLOR = (255, 0, 255)
PADS_COLOR = (80, 220, 80)
PICTURE_PX = 360
"""Pictures of checked placements are about this many pixels across."""


def check_picture(
    image: Image,
    mm_per_px_x: float,
    mm_per_px_y: float,
    length: float,
    width: float,
    angle: float,
    found: PlacedChip | None,
    pads: Sequence[Polygon] | None = None,
    margin_mm: float = 0.7,
) -> Image:
    """A placement as checked, for the operator: the middle of the frame, square pixels
    and enlarged, with the chip's outline where it was sent (orange, at ``angle``, image
    degrees), and where it was found (magenta), the board's ``pads`` where they were found
    (green, mm from the picture's middle), and a cross at the target."""
    h, w = image.shape[:2]
    half = length / 2 + margin_mm
    rx, ry = half / mm_per_px_x, half / mm_per_px_y
    x0, y0 = max(0, int(w / 2 - rx)), max(0, int(h / 2 - ry))
    x1, y1 = min(w, int(w / 2 + rx)), min(h, int(h / 2 + ry))
    crop = image[y0:y1, x0:x1]
    scale = PICTURE_PX / (2 * half)  # pixels per mm in the picture
    size = (
        max(1, round((x1 - x0) * mm_per_px_x * scale)),
        max(1, round((y1 - y0) * mm_per_px_y * scale)),
    )
    pic = np.ascontiguousarray(
        cv2.resize(crop, size, interpolation=cv2.INTER_CUBIC), dtype=np.uint8
    )

    def to_pic(u: float, v: float) -> tuple[int, int]:
        return round((u - x0) * mm_per_px_x * scale), round((v - y0) * mm_per_px_y * scale)

    def outline(cu: float, cv_: float, a: float, color: tuple[int, int, int]) -> None:
        r = math.radians(a)
        pts: list[tuple[int, int]] = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            du, dv = sx * length / 2, sy * width / 2
            # Image degrees run counterclockwise as shown: v grows downward.
            mu = du * math.cos(r) + dv * math.sin(r)
            mv = -du * math.sin(r) + dv * math.cos(r)
            pts.append(to_pic(cu + mu / mm_per_px_x, cv_ + mv / mm_per_px_y))
        cv2.polylines(pic, [np.array(pts, np.int32)], True, color, 2, cv2.LINE_AA)

    for pad in pads or ():
        pts = [to_pic(w / 2 + x / mm_per_px_x, h / 2 + y / mm_per_px_y) for x, y in pad]
        cv2.polylines(pic, [np.array(pts, np.int32)], True, PADS_COLOR, 2, cv2.LINE_AA)
    outline(w / 2, h / 2, angle, TARGET_COLOR)
    if found is not None:
        outline(found.u, found.v, found.angle, FOUND_COLOR)
    c = to_pic(w / 2, h / 2)
    cv2.drawMarker(pic, c, (0, 0, 255), cv2.MARKER_CROSS, 20, 1)
    return pic
