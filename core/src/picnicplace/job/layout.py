"""Where a job's placements are on the machine, and whether the machine can do them.

The checks follow the machine profile: every placement must be inside the area every
nozzle can reach (``placement_area``), every fiducial where the top camera can see it,
and the panel's outline must stay clear of the keep-outs (the bottom camera, the datum
board, and similar). ``find_location`` searches for a spot that passes.
"""

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, computed_field

from picnicplace.board.models import Placement, normalize_angle
from picnicplace.board.panel import BoardCopy
from picnicplace.job.models import Job, MachineLocation
from picnicplace.machine.profile import MachineProfile, Point, Rect
from picnicplace.schemas import WireModel

Matrix = tuple[float, float, float, float]
"""(a, b, c, d): x' = a x + b y, y' = c x + d y."""


@dataclass(frozen=True)
class Affine:
    m: Matrix = (1.0, 0.0, 0.0, 1.0)
    tx: float = 0.0
    ty: float = 0.0

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d = self.m
        return a * x + b * y + self.tx, c * x + d * y + self.ty

    def then(self, outer: "Affine") -> "Affine":
        """This transform followed by ``outer``."""
        a, b, c, d = self.m
        p, q, r, s = outer.m
        tx, ty = outer(self.tx, self.ty)
        return Affine((p * a + q * c, p * b + q * d, r * a + s * c, r * b + s * d), tx, ty)


def rotation(degrees: float, tx: float = 0.0, ty: float = 0.0) -> Affine:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return Affine((c, -s, s, c), tx, ty)


MIRROR_X = (-1.0, 0.0, 0.0, 1.0)
FIDUCIAL_MARGIN_MM = 1.0


class MachinePlacement(WireModel):
    copy_id: str | None = Field(description="Board copy, or None for a panel fiducial.")
    ref: str
    kind: Literal["part", "fiducial"]
    part_id: str | None
    footprint_id: str
    x: float = Field(description="Machine X, mm.")
    y: float = Field(description="Machine Y, mm.")
    rotation: float = Field(
        description="The part's angle as the operator sees it, degrees counterclockwise."
    )
    enabled: bool
    note: str = ""


class Issue(WireModel):
    severity: Literal["error", "warning"]
    message: str
    ref: str | None = None
    copy_id: str | None = None


class JobLayout(WireModel):
    placements: list[MachinePlacement]
    outline: list[Point] = Field(description="The panel's corners, machine coordinates.")
    issues: list[Issue]

    @computed_field
    @property
    def ok(self) -> bool:
        """No errors: the machine can do every enabled placement."""
        return not any(i.severity == "error" for i in self.issues)


def _loaded(job: Job) -> Affine:
    """Panel frame to loaded frame (flipped left to right when bottom side up)."""
    if job.side_up == "top":
        return Affine()
    return Affine(MIRROR_X, job.panel.size.x, 0.0)


def _to_machine(job: Job, profile: MachineProfile, location: MachineLocation) -> Affine:
    """Panel frame to machine coordinates."""
    view = _loaded(job).then(rotation(location.rotation))
    axes = Affine((float(profile.x_sign), 0.0, 0.0, float(profile.y_sign)), location.x, location.y)
    return view.then(axes)


def _footprint(p: Placement) -> Affine:
    """Footprint frame to board frame (the part's own view, unmirrored)."""
    if p.side == "top":
        return rotation(p.rotation)
    return rotation(180.0 - p.rotation).then(Affine(MIRROR_X))


def _view_angle(chain: Affine) -> float:
    a, _, c, _ = chain.m
    return normalize_angle(round(math.degrees(math.atan2(c, a)), 6))


def outline(job: Job, profile: MachineProfile, location: MachineLocation) -> list[Point]:
    to_machine = _to_machine(job, profile, location)
    w, h = job.panel.size.x, job.panel.size.y
    return [Point(x=x, y=y) for x, y in (to_machine(*c) for c in ((0, 0), (w, 0), (w, h), (0, h)))]


def machine_placements(
    job: Job, profile: MachineProfile, location: MachineLocation
) -> list[MachinePlacement]:
    """Every placement and fiducial on the side facing up, in machine coordinates."""
    to_machine = _to_machine(job, profile, location)
    to_view = _loaded(job).then(rotation(location.rotation))
    result: list[MachinePlacement] = []
    for copy in job.panel.copies:
        to_panel = rotation(copy.rotation, copy.x, copy.y)
        for p in job.board.placements:
            if p.side != job.side_up:
                continue
            x, y = to_panel.then(to_machine)(p.x, p.y)
            angle = _view_angle(_footprint(p).then(to_panel).then(to_view))
            enabled = p.enabled and copy.enabled
            note = p.note or ("" if copy.enabled else f"board copy {copy.id} is off")
            result.append(_placed(p, copy, x, y, angle, enabled, note))
    for p in job.panel.fiducials:
        if p.side != job.side_up:
            continue
        x, y = to_machine(p.x, p.y)
        result.append(_placed(p, None, x, y, 0.0, p.enabled, p.note))
    return result


def _placed(
    p: Placement,
    copy: BoardCopy | None,
    x: float,
    y: float,
    angle: float,
    enabled: bool,
    note: str,
) -> MachinePlacement:
    return MachinePlacement(
        copy_id=copy.id if copy else None,
        ref=p.ref,
        kind=p.kind,
        part_id=p.part_id,
        footprint_id=p.footprint_id,
        x=round(x, 4) + 0.0,
        y=round(y, 4) + 0.0,
        rotation=angle,
        enabled=enabled,
        note=note,
    )


def _inside(rect: Rect, x: float, y: float, margin: float = 1e-6) -> bool:
    return (
        rect.x_min - margin <= x <= rect.x_max + margin
        and rect.y_min - margin <= y <= rect.y_max + margin
    )


def overlaps(polygon: list[Point], rect: Rect) -> bool:
    """Whether a convex polygon and a rectangle overlap (separating axis test)."""
    corners = [(rect.x_min, rect.y_min), (rect.x_max, rect.y_max)]
    pts = [(p.x, p.y) for p in polygon]
    axes = [(1.0, 0.0), (0.0, 1.0)]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
        axes.append((y1 - y2, x2 - x1))
    box = [(corners[0][0], corners[0][1]), (corners[1][0], corners[0][1])]
    box += [(corners[1][0], corners[1][1]), (corners[0][0], corners[1][1])]
    for ax, ay in axes:
        a = [ax * x + ay * y for x, y in pts]
        b = [ax * x + ay * y for x, y in box]
        if max(a) <= min(b) + 1e-9 or max(b) <= min(a) + 1e-9:
            return False
    return True


def camera_travel(profile: MachineProfile) -> Rect | None:
    x = next((a for a in profile.axes if a.name == "X"), None)
    y = next((a for a in profile.axes if a.name == "Y"), None)
    if x is None or y is None:
        return None
    return Rect(x_min=x.min, y_min=y.min, x_max=x.max, y_max=y.max)


def layout(job: Job, profile: MachineProfile) -> JobLayout:
    if job.location is None:
        issue = Issue(severity="error", message="the job has no location on the machine yet")
        return JobLayout(placements=[], outline=[], issues=[issue])
    placements = machine_placements(job, profile, job.location)
    corners = outline(job, profile, job.location)
    issues: list[Issue] = []
    area = profile.placement_area
    travel = camera_travel(profile)
    for p in placements:
        if not p.enabled:
            continue
        if p.kind == "part" and area is not None and not _inside(area, p.x, p.y):
            short = [n for n, r in profile.nozzle_reach.items() if not _inside(r, p.x, p.y)]
            where = f"nozzle {', '.join(short)} cannot reach it" if short else "off the plates"
            issues.append(
                Issue(
                    severity="error",
                    message=f"{p.ref} is outside the placement area ({where})",
                    ref=p.ref,
                    copy_id=p.copy_id,
                )
            )
        elif p.kind == "fiducial" and travel is not None and not _inside(travel, p.x, p.y):
            issues.append(
                Issue(
                    severity="error",
                    message=f"fiducial {p.ref} is where the top camera cannot see it",
                    ref=p.ref,
                    copy_id=p.copy_id,
                )
            )
    for keep_out in profile.keep_outs:
        if overlaps(corners, keep_out.area):
            issues.append(
                Issue(severity="error", message=f"the board covers the {keep_out.label.lower()}")
            )
    if area is not None and not all(_inside(area, c.x, c.y) for c in corners):
        issues.append(
            Issue(
                severity="warning",
                message="the board extends beyond the area every nozzle can reach",
            )
        )
    return JobLayout(placements=placements, outline=corners, issues=issues)


def find_location(
    job: Job, profile: MachineProfile, rotations: tuple[float, ...] = (0, 90, 180, 270)
) -> MachineLocation | None:
    """A location where the whole panel is inside the placement area, its fiducials are
    where the top camera can see them, and it is clear of the keep-outs. Searched on a
    1 mm grid, nearest the back of the bed first."""
    area = profile.placement_area
    if area is None:
        return None
    travel = camera_travel(profile)
    step = 1.0
    for turn in rotations:
        origin = MachineLocation(x=0.0, y=0.0, rotation=turn)
        corners = outline(job, profile, origin)
        xs, ys = [c.x for c in corners], [c.y for c in corners]
        x_lo, x_hi = area.x_min - min(xs), area.x_max - max(xs)
        y_lo, y_hi = area.y_min - min(ys), area.y_max - max(ys)
        fiducials = [
            p
            for p in machine_placements(job, profile, origin)
            if p.kind == "fiducial" and p.enabled
        ]
        if travel is not None and fiducials:
            # Keep fiducials a little inside the camera's travel, not on its very edge.
            m = FIDUCIAL_MARGIN_MM
            fx, fy = [p.x for p in fiducials], [p.y for p in fiducials]
            x_lo = max(x_lo, travel.x_min + m - min(fx))
            x_hi = min(x_hi, travel.x_max - m - max(fx))
            y_lo = max(y_lo, travel.y_min + m - min(fy))
            y_hi = min(y_hi, travel.y_max - m - max(fy))
        if x_lo > x_hi or y_lo > y_hi:
            continue
        nx, ny = int((x_hi - x_lo) / step), int((y_hi - y_lo) / step)
        # Back of the bed first (largest Y if +Y points back), then from the middle out.
        y_order = range(ny + 1) if profile.y_sign < 0 else range(ny, -1, -1)
        x_order = sorted(range(nx + 1), key=lambda i: abs(i - nx / 2))
        for j in y_order:
            for i in x_order:
                dx, dy = x_lo + i * step, y_lo + j * step
                moved = [Point(x=c.x + dx, y=c.y + dy) for c in corners]
                if not any(overlaps(moved, k.area) for k in profile.keep_outs):
                    return MachineLocation(x=round(dx, 3), y=round(dy, 3), rotation=turn)
    return None
