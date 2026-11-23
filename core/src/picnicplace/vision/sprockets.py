"""Find sprocket holes by their rhythm: dark spots a sprocket pitch apart along the line
where the holes should be.

A hole half covered by a strip holder's rail is neither a round mark nor much of a rim
(seen on a real LumenPnP: two strips, black tape, the rail over half of every hole). But
the half that shows still darkens the hole line, every 4 mm. So the frame is sampled along
the hole line, in narrow bands a little either side of it; the band whose brightness has
the strongest component at the sprocket pitch is the one through the holes; and each dark
dip in it is a hole, if it is clearly darker than the tape halfway to the next one.

It says where the holes are along the tape, not across it: the band through half a hole
is off its middle.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

from picnicplace.camera.source import Image

MIN_DEPTH = 20.0
"""A dip is a hole if it is this much darker (of 255, with the frame's contrast evened
out) than the tape halfway to the next hole. On real frames holes were 30 to 190 darker;
where there were none, the deepest single dip was 16 (except other tapes' holes), but a
holder's rail gave two of 22 and 25: see ``CLEAR``."""
CLEAR = 2.5
"""...and this many times as deep as the line's brightness usually varies away from the
holes (its median absolute deviation): a holder's machined metal rail, where there were no
holes, gave dips of 22 and 25 that were 2.1 times its variation; holes were 3.1 to 45."""
EVEN = 0.6
"""The holes found must be about as dark as one another: the faintest at least this
fraction as deep as the deepest."""
MIN_HOLES = 2


@dataclass(frozen=True)
class RhythmHole:
    along: float
    """Pixels along the line from its start."""
    depth: float
    """How much darker than the tape between holes."""


def find_hole_rhythm(
    image: Image,
    start: tuple[float, float],
    angle: float,
    pitch_px: float,
    hole_px: float,
    search_px: float,
    band_px: float,
) -> list[RhythmHole]:
    """Sprocket holes along a line through ``start`` (pixels) at ``angle`` (degrees in the
    image, from its x axis toward its y axis), a pitch apart, looked for in bands
    ``band_px`` wide up to ``search_px`` either side of the line. Empty unless at least
    two holes are found, about as dark as one another."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    gray = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray).astype(np.float32)
    h, w = gray.shape
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)
    half_len = math.hypot(w, h) / 2
    half_band = max(1, round(band_px / 2))
    reach = round(search_px) + half_band
    t = np.arange(-half_len, half_len + 1, dtype=np.float32)
    s = np.arange(-reach, reach + 1, dtype=np.float32)
    tt, ss = np.meshgrid(t, s)
    map_x = (start[0] + tt * ux - ss * uy).astype(np.float32)
    map_y = (start[1] + tt * uy + ss * ux).astype(np.float32)
    strip = cv2.remap(gray, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    inside = (map_x >= 0) & (map_x <= w - 1) & (map_y >= 0) & (map_y <= h - 1)

    best: tuple[float, np.ndarray, np.ndarray, complex] | None = None
    for row in range(half_band, len(s) - half_band):
        rows = slice(row - half_band, row + half_band + 1)
        seen = inside[rows].all(axis=0)
        if seen.sum() < 2 * pitch_px:
            continue
        along, profile = t[seen], strip[rows][:, seen].mean(axis=0)
        wave = complex(((profile - profile.mean()) * np.exp(-2j * np.pi * along / pitch_px)).sum())
        strength = 2 * abs(wave) / len(profile)
        if best is None or strength > best[0]:
            best = (strength, along, profile, wave)
    if best is None:
        return []
    _, along, profile, wave = best
    # profile ~ A cos(2 pi t / P - phi), and the wave's angle is -phi: darkest half a pitch
    # from the brightest.
    darkest = (-math.atan2(wave.imag, wave.real) / (2 * math.pi) * pitch_px + pitch_px / 2) % (
        pitch_px
    )
    r = hole_px / 4
    holes: list[RhythmHole] = []
    first = math.floor((float(along.min()) - darkest) / pitch_px)
    last = math.ceil((float(along.max()) - darkest) / pitch_px)
    for k in range(first, last + 1):
        x = darkest + k * pitch_px
        middle = profile[np.abs(along - x) <= r]
        between = profile[
            (np.abs(along - (x - pitch_px / 2)) <= r) | (np.abs(along - (x + pitch_px / 2)) <= r)
        ]
        if len(middle) < r or len(between) < r:
            continue  # not wholly in view
        depth = float(np.median(between) - np.median(middle))
        if depth >= MIN_DEPTH:
            # Its middle: the center of its darkness within a hole's width, darker than
            # the tape between holes.
            near = np.abs(along - x) <= hole_px / 2
            weight = np.clip(float(np.median(between)) - profile[near], 0, None)
            if weight.sum() > 0:
                x = float((along[near] * weight).sum() / weight.sum())
            holes.append(RhythmHole(float(x), depth))
    if len(holes) < MIN_HOLES or min(x.depth for x in holes) < EVEN * max(x.depth for x in holes):
        return []
    away = np.ones_like(along, dtype=bool)
    for hole in holes:
        away &= np.abs(along - hole.along) > hole_px * 2 / 3
    rest = profile[away]
    if len(rest):
        spread = float(np.median(np.abs(rest - np.median(rest))))
        if min(x.depth for x in holes) < CLEAR * spread:
            return []
    return holes


MIN_MATCH = 0.6
"""A hole is a place on the hole line that looks this much like a hole: the normalized
correlation of the frame with a hole-sized disk in a ring of tape. On a real LumenPnP's
strips, black tape over black and red holders, holes matched 0.68 to 0.77."""
RING = 1.6
"""The template: a hole's disk in a square this many times its radius from its middle,
the rest tape (a hole's edge is 1 mm from the tape's, so a little more would reach past
it)."""


IN_LINE = 0.1
"""Holes lie in a line: each found must be within this fraction of a hole's diameter of
the line through the likeliest one, across the tape. On a real machine whole holes were
within 0.06; holes cut through by a strip's end, 0.13 and 0.15 off it."""
SIDES = 0.5
"""A hole has tape before and after it along the line: each side must differ from the
hole by at least this fraction of the other's difference. A hole cut through by a strip's
end, tape on one side only, matched 0.64 on a real machine, as well as some whole holes."""


@dataclass(frozen=True)
class MatchedHole:
    x: float
    y: float
    """Its middle, in pixels."""
    score: float
    """How much it looks like a hole: 1 is exactly."""


def match_holes(
    image: Image,
    start: tuple[float, float],
    angle: float,
    hole_px: float,
    search_px: float,
    min_gap_px: float,
) -> list[MatchedHole]:
    """Sprocket holes near a line through ``start`` (pixels) at ``angle`` (degrees in the
    image, from its x axis toward its y axis): places up to ``search_px`` either side of
    it that look like a hole ``hole_px`` across, at least ``min_gap_px`` apart.

    For holes too faint to be round marks, such as black tape over a black holder: the
    frame is matched against a dark disk in a ring of tape, which needs no particular
    brightness, only the shape. Only near the line, so that nothing round elsewhere (a
    part's markings, a holder's textured lip) is taken. Dark holes or bright ones (a light
    holder under black tape), whichever more of the holes are."""
    value = image.max(axis=2) if image.ndim == 3 else image
    value = cv2.GaussianBlur(value, (5, 5), 0).astype(np.float32)
    h, w = value.shape
    r = hole_px / 2
    size = int(2 * r * RING) | 1
    template = np.ones((size, size), np.float32)
    cv2.circle(template, (size // 2, size // 2), round(r), 0.0, -1)

    # The frame straightened along the line, wide enough for the template either side.
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)
    half_len = math.hypot(w, h) / 2
    reach = round(search_px) + size // 2
    t = np.arange(-half_len, half_len + 1, dtype=np.float32)
    s = np.arange(-reach, reach + 1, dtype=np.float32)
    tt, ss = np.meshgrid(t, s)
    map_x = (start[0] + tt * ux - ss * uy).astype(np.float32)
    map_y = (start[1] + tt * uy + ss * ux).astype(np.float32)
    strip = cv2.remap(value, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    inside = ((map_x >= 0) & (map_x <= w - 1) & (map_y >= 0) & (map_y <= h - 1)).astype(np.uint8)
    if strip.shape[0] < size or strip.shape[1] < size:
        return []
    match = cv2.matchTemplate(strip, template, cv2.TM_CCOEFF_NORMED)
    # Only where the whole template lies in the frame.
    whole = cv2.erode(inside, np.ones((size, size), np.uint8), anchor=(0, 0))
    whole = whole[: match.shape[0], : match.shape[1]].astype(bool)
    match = np.where(whole & np.isfinite(match), match, 0.0)

    def mean(i: float, j: float, half_i: float, half_j: float) -> float:
        """The strip's mean in a box about column i, row j."""
        i0, i1 = max(0, round(i - half_i)), round(i + half_i) + 1
        j0, j1 = max(0, round(j - half_j)), round(j + half_j) + 1
        box = strip[j0:j1, i0:i1]
        return float(box.mean()) if box.size else math.nan

    def whole_hole(i: int, j: int, sign: float) -> bool:
        """Tape before and after the hole at column i, row j along the line."""
        hole = mean(i, j, r / 2, r / 2)
        sides = [sign * (mean(i + k * r * 1.3, j, r / 4, r / 2) - hole) for k in (-1, 1)]
        return not any(math.isnan(d) for d in sides) and min(sides) >= SIDES * max(sides) > 0

    def peaks(scores: np.ndarray, sign: float) -> list[MatchedHole]:
        scores = scores.copy()
        found: list[MatchedHole] = []
        line: float | None = None  # across, of the likeliest hole
        while True:
            _, best, _, (i, j) = cv2.minMaxLoc(scores)
            if best < MIN_MATCH:
                return found
            cv2.circle(scores, (i, j), max(1, round(min_gap_px)), 0.0, -1)
            if not whole_hole(i + size // 2, j + size // 2, sign):
                continue
            along, across = float(t[i + size // 2]), float(s[j + size // 2])
            if line is None:
                line = across
            elif abs(across - line) > IN_LINE * hole_px:
                continue
            found.append(
                MatchedHole(
                    x=start[0] + along * ux - across * uy,
                    y=start[1] + along * uy + across * ux,
                    score=round(float(best), 3),
                )
            )

    # The template's disk is dark: dark holes match it, bright ones its negative.
    dark, bright = peaks(match, 1.0), peaks(-match, -1.0)
    return max((dark, bright), key=lambda found: (len(found), sum(m.score for m in found)))
