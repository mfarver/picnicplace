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
where there were none, the deepest single dip was 16 (except other tapes' holes)."""
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
    return holes
