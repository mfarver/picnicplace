"""Fitting a feeder rail to the feeder fiducials found on it.

A rail's slots are where the machine profile says, give or take how the machine was
assembled: the rail may sit a few millimeters off, turned a little, and its slots a
little farther apart or closer. Feeder fiducials found on the rail (two far apart are
enough) fit the profile's rail to the real one with a shift, a turn, and a stretch, which
places every slot between them.
"""

import cmath
import math

from picnicplace.feeders.setup import RailCalibration, RailPoint
from picnicplace.machine.profile import Point


def fit_rail(points: dict[str, RailPoint]) -> RailCalibration:
    """The rail that best carries each point's expected place onto where it was found
    (least squares). With fewer than two points, the rail is not fitted."""
    rail = RailCalibration(points=dict(points))
    if len(points) < 2:
        return rail
    zs = [complex(p.expected.x, p.expected.y) for p in points.values()]
    ws = [complex(p.found.x, p.found.y) for p in points.values()]
    zc = sum(zs) / len(zs)
    wc = sum(ws) / len(ws)
    spread = sum(abs(z - zc) ** 2 for z in zs)
    if spread < 1e-9:
        return rail
    a = sum((w - wc) * (z - zc).conjugate() for z, w in zip(zs, ws, strict=True)) / spread
    return rail.model_copy(
        update={
            "about": Point(x=round(zc.real, 4), y=round(zc.imag, 4)),
            "shift": Point(x=round((wc - zc).real, 4), y=round((wc - zc).imag, 4)),
            "angle": round(math.degrees(cmath.phase(a)), 4),
            "scale": round(abs(a), 6),
            "fitted": True,
        }
    )


def residual(rail: RailCalibration) -> float:
    """How far the farthest point is from where the fitted rail puts it, in mm."""
    if not rail.fitted:
        return 0.0
    worst = 0.0
    for p in rail.points.values():
        at = rail.apply(p.expected)
        worst = max(worst, math.hypot(at.x - p.found.x, at.y - p.found.y))
    return worst
