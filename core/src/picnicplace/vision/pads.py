"""Fit a footprint's pads to the metal a bottom camera sees.

``find_part`` draws one box around everything bright, which is right for a part whose
metal is all pads, centered on its body. It is wrong for a part like a connector whose
leads stand out past one side of its body, or whose body is as bright as its leads: the
box's center is then not the pads' center, and the part is placed off its pads.

This finder knows where the pads are. It is given the footprint's pads as rectangles in
image pixels, about the footprint's origin, and the angle the part is expected at; it
finds where that pattern lies on the metal, and returns where the footprint's origin is
and its angle. First a coarse search: the pattern, drawn at a few angles about the
expected one, is slid over the metal mask, and the best match taken. Then a fine fit:
each pad takes the patches of metal whose middles lie on it, and the middle of their
extent along each of its sides is where its metal is (stains and dull patches inside a
lead move a mean, not the lead's ends). The pattern is then moved and turned to lie on
those as well as it can: least squares, each pad weighted by its metal, and its miss
counting less along its long side (by the square of its shape), where a lead's ends are
less sure than its sides. A lead narrower than its pad still centers in it.

Plastic as bright as tin (an ivory connector body) is left out by its color; see
``part.metal``.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np
from cv2.typing import MatLike

from picnicplace.camera.source import Image
from picnicplace.vision.part import BRIGHT, WARM, metal

COARSE_SCALE = 0.25
"""The coarse search runs on the image shrunk to this."""
COARSE_SPAN = 4.0
"""The coarse search tries angles this many degrees either side of the expected one..."""
COARSE_STEP = 0.5
"""...this far apart."""
REACH = 1.15
"""A pad takes the metal within this many times its size, about where it is thought to be."""
REFINE_PASSES = 3
SOLVE_STEPS = 6
SPECK_SHARE = 0.02
"""Bright patches smaller than this share of the smallest pad are left out before fitting:
each pad is placed by the ends of its metal, which a stray fleck would move."""
MIN_PAD_SHARE = 0.05
"""A pad whose reach holds less metal than this share of its area is reported as missing."""


@dataclass(frozen=True)
class PadRect:
    x: float
    """Center, pixels from the footprint's origin along +u, the part at 0 degrees."""
    y: float
    """...and along +v."""
    width: float
    """Along u, in pixels."""
    height: float


@dataclass(frozen=True)
class PadFit:
    u: float
    """The footprint's origin, pixels from the left edge."""
    v: float
    """...and from the top edge."""
    angle: float
    """The pattern's turn, in image degrees: from +u toward +v."""
    residual: float
    """How far the pads' metal lies from where the fit puts them: root mean square, in
    pixels."""
    found: int
    """How many pads had metal within their reach."""


def _turn(x: float, y: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def _template(pads: list[PadRect], angle: float, scale: float) -> tuple[MatLike, float, float]:
    """The pads drawn filled at an angle and scale; and where the origin is in it."""
    corners: list[tuple[float, float]] = []
    for p in pads:
        for sx in (-0.5, 0.5):
            for sy in (-0.5, 0.5):
                corners.append(_turn(p.x + sx * p.width, p.y + sy * p.height, angle))
    xs = [c[0] * scale for c in corners]
    ys = [c[1] * scale for c in corners]
    x0, y0 = math.floor(min(xs)) - 2, math.floor(min(ys)) - 2
    w, h = math.ceil(max(xs)) - x0 + 3, math.ceil(max(ys)) - y0 + 3
    tpl = np.zeros((h, w), np.float32)
    for p in pads:
        box = (
            (_turn(p.x, p.y, angle)[0] * scale - x0, _turn(p.x, p.y, angle)[1] * scale - y0),
            (p.width * scale, p.height * scale),
            angle,
        )
        cv2.fillPoly(tpl, [np.round(cv2.boxPoints(box)).astype(np.int32)], 1.0)
    return tpl, -x0, -y0


def _coarse(mask: MatLike, pads: list[PadRect], expected: float) -> tuple[float, float, float]:
    """The origin and angle where the pattern best covers the metal, roughly."""
    small = cv2.resize(
        mask.astype(np.float32),
        None,
        fx=COARSE_SCALE,
        fy=COARSE_SCALE,
        interpolation=cv2.INTER_AREA,
    )
    best = (-1.0, 0.0, 0.0, expected)
    steps = round(COARSE_SPAN / COARSE_STEP)
    for i in range(-steps, steps + 1):
        angle = expected + i * COARSE_STEP
        tpl, ox, oy = _template(pads, angle, COARSE_SCALE)
        if tpl.shape[0] > small.shape[0] or tpl.shape[1] > small.shape[1]:
            continue
        score = cv2.matchTemplate(small, tpl, cv2.TM_CCORR_NORMED)
        _, peak, _, (px, py) = cv2.minMaxLoc(score)
        if peak > best[0]:
            best = (peak, (px + ox) / COARSE_SCALE, (py + oy) / COARSE_SCALE, angle)
    _, u, v, angle = best
    return u, v, angle


def _centers(
    mask: MatLike, pads: list[PadRect], u: float, v: float, angle: float
) -> list[tuple[PadRect, float, float, float]]:
    """Each pad with metal within its reach: the metal's center and how much there is.
    Only patches of metal whose middle lies on the pad count, so a neighbor's metal, or a
    glint on the body, that strays into its reach does not pull it."""
    found: list[tuple[PadRect, float, float, float]] = []
    h, w = mask.shape[:2]
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    for p in pads:
        dx, dy = _turn(p.x, p.y, angle)
        cu, cv = u + dx, v + dy
        box = ((cu, cv), (p.width * REACH, p.height * REACH), angle)
        reach = np.zeros((h, w), np.uint8)
        cv2.fillPoly(reach, [np.round(cv2.boxPoints(box)).astype(np.int32)], 1)
        n, labels, _, mids = cv2.connectedComponentsWithStats(cv2.bitwise_and(mask, reach))
        keep: list[int] = []
        for i in range(1, n):
            # The patch's middle, in the pad's own frame.
            ox, oy = mids[i][0] - cu, mids[i][1] - cv
            if abs(ox * c + oy * s) <= p.width / 2 and abs(-ox * s + oy * c) <= p.height / 2:
                keep.append(i)
        if not keep:
            continue
        ys, xs = np.nonzero(np.isin(labels, keep))
        if len(xs) < MIN_PAD_SHARE * p.width * p.height:
            continue
        # The middle of the metal's extent along each of the pad's sides: stains and
        # dull patches inside a lead do not move it, as they move a mean.
        ox, oy = xs - cu, ys - cv
        along, across = ox * c + oy * s, -ox * s + oy * c
        ma = (float(along.min()) + float(along.max())) / 2
        mc = (float(across.min()) + float(across.max())) / 2
        found.append((p, cu + ma * c - mc * s, cv + ma * s + mc * c, float(len(xs))))
    return found


def _rigid(
    found: list[tuple[PadRect, float, float, float]], u: float, v: float, angle: float
) -> tuple[float, float, float, float]:
    """The origin and turn that lay the pads' centers on their metal's, starting from
    (``u``, ``v``, ``angle``): weighted least squares (Gauss-Newton), each pad's miss
    weighted by its metal and, along each of its sides, by one over that side squared.
    Also what is left over: root mean square, in pixels, on the same weights."""
    total = sum(m for _, _, _, m in found)
    residual = 0.0
    for _ in range(SOLVE_STEPS):
        a = math.radians(angle)
        c, s = math.cos(a), math.sin(a)
        normal = np.zeros((3, 3))
        rhs = np.zeros(3)
        miss = 0.0
        for p, cu, cv, m in found:
            px, py = p.x * c - p.y * s, p.x * s + p.y * c
            r = np.array([u + px - cu, v + py - cv])
            j = np.array([[1.0, 0.0, -py], [0.0, 1.0, px]])
            axes = np.array([[c, -s], [s, c]])
            weight = (m / total) * axes @ np.diag([1 / p.width**2, 1 / p.height**2]) @ axes.T
            normal += j.T @ weight @ j
            rhs += j.T @ weight @ r
            miss += float(r @ weight @ r) * min(p.width, p.height) ** 2
        step = np.linalg.solve(normal, -rhs)
        u, v, angle = u + float(step[0]), v + float(step[1]), angle + math.degrees(float(step[2]))
        residual = math.sqrt(miss)
    return u, v, angle, residual


def fit_pads(
    image: Image,
    pads: list[PadRect],
    expected_angle: float,
    search_px: float,
    bright: int = BRIGHT,
    warm: float | None = WARM,
) -> PadFit | None:
    """Where the footprint whose pads are ``pads`` lies on the metal within ``search_px``
    of the image center, the part turned about ``expected_angle``; None without at least
    two pads' metal to fit to."""
    speck = SPECK_SHARE * min(p.width * p.height for p in pads)
    mask = metal(image, search_px, bright, speck, warm)
    if cv2.countNonZero(mask) == 0:
        return None
    u, v, angle = _coarse(mask, pads, expected_angle)
    found: list[tuple[PadRect, float, float, float]] = []
    residual = 0.0
    for _ in range(REFINE_PASSES):
        found = _centers(mask, pads, u, v, angle)
        if len(found) < 2:
            return None
        u, v, angle, residual = _rigid(found, u, v, angle)
    return PadFit(u=u, v=v, angle=angle, residual=residual, found=len(found))
