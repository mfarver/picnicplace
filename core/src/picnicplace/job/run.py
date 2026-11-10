"""Running a job: every part on the board picked, aligned, and placed (milestone M4).

**Plan.** Each enabled part placement on the side facing up becomes an item, with the
feeder slot (or strip or tray on the bed) that holds its part and the nozzles that can
reach both it and the placement. Feeder slots come before strips and trays. An item
that cannot be done (no part number, no part in the library or no height for it, no
feeder holding it, no nozzle reaching it) is *blocked*, and the plan says why. Planning
moves nothing, so it doubles as a dry run of a job against the machine and its loaded
feeders.

**Board.** Before the first part, the top camera finds up to three of the job's
fiducials (the panel's own if it has two or more, else the boards'). A rigid fit (shift
and turn) of where they were found to where the job says they are corrects every
placement. Without fiducials, the job's location is used as it is.

**Feeders.** On a machine with a feeder bus, the run scans the feeders before it finds
the board, in case one was moved or swapped since the job was planned; a placement whose
part is no longer loaded pauses the run. The first pick from each feeder slot in a run
finds that feeder's fiducial first, near where its last fix or its calibrated rail puts
it, and takes the pick location from it (unless picks are not corrected at all): each
feeder sits a little differently in its slot, and the fix is kept with the feeder.

**Batches.** Every nozzle that can takes a part: all of them pick, then all of them
align, then all of them place. Items go in order of part height (lowest first, so a
tall part never stands next to where a nozzle must still go), then by feeder slot, so
consecutive picks come from the same tape.

**Problems.** A pick that comes up empty is tried again, feeding the next part, up to
``PICK_ATTEMPTS`` times. A part bottom vision cannot find goes to the discard bin, and
its placement is tried again later; with no discard location, the run parks the head and
pauses for the operator to take the part off and say so (Part removed). Anything else
pauses the run, with the parts already picked left on their nozzles: fix the cause, then
resume, or skip the placement. An empty strip or tray pauses the run too, with the head
moved to the park position out of the operator's way: refill it, mark it refilled, and
resume. Pausing and stopping take effect after the batch in progress; a step resumes a
paused run for one batch.
"""

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, computed_field

from picnicplace.camera.synthetic import BoardScene
from picnicplace.feeders.service import FeederService, FeedersState, rotate
from picnicplace.feeders.setup import PickCorrection
from picnicplace.job.layout import Issue, MachinePlacement, layout
from picnicplace.job.models import Job
from picnicplace.job.store import JobStore
from picnicplace.library import Library, Part
from picnicplace.machine.errors import (
    FeederEmptyError,
    InvalidTargetError,
    MachineBusyError,
    MachineError,
    VisionError,
)
from picnicplace.machine.profile import MachineProfile, Point, Rect
from picnicplace.machine.service import MachineService
from picnicplace.placement import MispickError, PlacementService
from picnicplace.schemas import Operation, WireModel

log = logging.getLogger(__name__)

PICK_ATTEMPTS = 3
ALIGN_ATTEMPTS = 2
"""Times a part may be discarded for bottom vision before its placement fails."""
MAX_FIDUCIALS = 3
FIDUCIAL_SEARCH_MM = 3.0
"""How far from where the job says a fiducial is, the camera looks for it."""
FIDUCIAL_DIAMETER_MM = 1.0
"""For fiducials whose footprint is not in the library."""
SIM_DECOY_SIZE = 0.7
"""A simulated decoy's size, as a fraction of its fiducial's (see ``sim_decoys``)."""
FIDUCIAL_RESIDUAL_MM = 0.25
"""Fiducials found further than this from the fit mean the board, or one of them, is
not where the job says: the run stops before placing anything."""


class RunError(MachineError):
    code = "run_failed"


ItemStatus = Literal["pending", "placed", "skipped", "failed", "blocked"]


class RunItem(WireModel):
    index: int
    copy_id: str | None
    ref: str
    part_id: str | None
    footprint_id: str
    x: float = Field(description="Machine X from the job's location, before fiducial alignment.")
    y: float
    rotation: float = Field(description="The part's machine angle, degrees.")
    slot_id: str | None = Field(
        description="The feeder slot to pick from, or a strip or tray on the bed (T1, ...)."
    )
    nozzles: list[str] = Field(description="Nozzles that reach both the slot and the place.")
    status: ItemStatus
    note: str = Field(default="", description="Why it is blocked, skipped, or failed.")
    nozzle: str | None = Field(default=None, description="The nozzle that placed it.")
    discards: int = Field(default=0, description="Parts discarded after bottom vision failed.")


class PartNeed(WireModel):
    """One part number the job uses, and the feeders loaded with it."""

    part_id: str | None
    count: int
    slot_ids: list[str]
    problem: str | None = None


class FiducialCheck(WireModel):
    ref: str
    copy_id: str | None
    expected: Point
    found: Point


class BoardFix(WireModel):
    """How the board really sits, from its fiducials: machine positions from the job
    are turned by ``angle`` about ``about`` and then shifted by (dx, dy)."""

    dx: float
    dy: float
    angle: float
    about: Point
    residual: float = Field(description="Largest distance of a fiducial from the fit, mm.")
    fiducials: list[FiducialCheck]

    def apply(self, x: float, y: float) -> Point:
        p = rotate(Point(x=x - self.about.x, y=y - self.about.y), self.angle)
        return Point(x=p.x + self.about.x + self.dx, y=p.y + self.about.y + self.dy)


class JobPlan(WireModel):
    job_id: str
    name: str
    items: list[RunItem]
    parts: list[PartNeed]
    fiducials: int = Field(description="Fiducials the run will look for.")
    issues: list[Issue]

    @computed_field
    @property
    def ok(self) -> bool:
        """Nothing stops the run, and every placement can be done."""
        return not any(i.severity == "error" for i in self.issues)

    @computed_field
    @property
    def runnable(self) -> bool:
        """The run can start, placing what it can and skipping what is blocked."""
        return not any(i.severity == "error" and i.ref is None for i in self.issues)


RunStatus = Literal["running", "paused", "finished", "stopped"]


class RunState(WireModel):
    job_id: str
    name: str
    status: RunStatus
    plan: JobPlan
    fix: BoardFix | None
    message: str | None = None
    error: str | None = Field(default=None, description="Why the run paused, if it did.")
    pause_requested: bool = False
    stop_requested: bool = False
    pick_correction: PickCorrection = "top"
    """How picks are kept centered (see the config's machine.pick_correction)."""
    located: list[str] = Field(
        default_factory=list[str],
        description="Feeder slots whose feeder's fiducial has been found in this run.",
    )
    recording: str | None = Field(
        default=None, description="The flight recording this run is kept in, if any."
    )
    held: dict[str, int] = Field(
        default_factory=dict[str, int],
        description="Nozzle ID to the item whose part it holds.",
    )
    started_at: datetime
    finished_at: datetime | None = None

    @computed_field
    @property
    def placed(self) -> int:
        return sum(1 for i in self.plan.items if i.status == "placed")

    @computed_field
    @property
    def remaining(self) -> int:
        return sum(1 for i in self.plan.items if i.status == "pending")


# --- Planning ----------------------------------------------------------------------------


def machine_angle(profile: MachineProfile, view: float) -> float:
    """An angle as the operator sees the bed, in machine degrees."""
    a = math.radians(view)
    x, y = profile.x_sign * math.cos(a), profile.y_sign * math.sin(a)
    return round(math.degrees(math.atan2(y, x)), 6) + 0.0


def _inside(rect: Rect, x: float, y: float) -> bool:
    return rect.x_min <= x <= rect.x_max and rect.y_min <= y <= rect.y_max


def _too_wide(part: Part, profile: MachineProfile) -> str | None:
    """Why no feeder on the machine can take the part's tape, if none can."""
    width = part.tape.width if part.tape else None
    widths = {w for b in profile.feeder_banks for w in b.tape_widths}
    if width is None or not profile.feeder_banks or not widths:
        return None
    if any(not b.tape_widths for b in profile.feeder_banks) or width in widths:
        return None
    takes = " and ".join(f"{w:g}" for w in sorted(widths))
    return (
        f"{part.id} comes in {width:g} mm tape; this machine's feeders take {takes} mm "
        "(hold it in a strip on the bed instead)"
    )


def _slot_order(slot_id: str | None) -> int:
    """Feeder slots by number, then strips and trays (T1, T2, ...)."""
    if slot_id and slot_id.isdigit():
        return int(slot_id)
    if slot_id and slot_id[:1] == "T" and slot_id[1:].isdigit():
        return 1_000_000 + int(slot_id[1:])
    return 2_000_000


def _source(slot_id: str) -> str:
    return f"slot {slot_id}" if slot_id.isdigit() else slot_id


def plan_job(
    job_id: str,
    job: Job,
    profile: MachineProfile,
    feeders: FeedersState,
    library: Library | None,
) -> JobPlan:
    result = layout(job, profile)
    issues = [i for i in result.issues if not (i.ref and i.severity == "warning")]
    reach = profile.nozzle_reach

    # Feeders by part, in slot order, then strips and trays, with where each picks.
    loaded: dict[str, list[tuple[str, list[Point]]]] = {}
    for s in feeders.slots:
        a = s.feeder.assignment if s.feeder else None
        if a is None or a.part_id is None or not a.enabled:
            continue
        loaded.setdefault(a.part_id, []).append((s.slot.id, [s.pick]))
    on_bed: set[str] = set()
    for f in feeders.fixed:
        if f.feeder.part_id is None or not f.feeder.enabled:
            continue
        on_bed.add(f.feeder.part_id)
        problem = None
        if f.angle is None:
            problem = "its part has no tape data, so the parts' angle is not known"
        elif f.feeder.pick_height is None and profile.board_height is None:
            problem = "it has no pick height"
        if problem:
            issues.append(Issue(severity="warning", message=f"{f.id} is not used: {problem}"))
            continue
        loaded.setdefault(f.feeder.part_id, []).append((f.id, f.feeder.corners()))
    if any(c.looking == "up" for c in profile.cameras) and not any(
        loc.kind == "discard" for loc in profile.locations
    ):
        issues.append(
            Issue(
                severity="warning",
                message="there is no discard location: a part the bottom camera cannot find "
                "pauses the run for you to take it off the nozzle (add one under "
                "[[machine.locations]] in the config)",
            )
        )
    if not feeders.scanned and profile.feeder_bus is not None:
        issues.append(
            Issue(severity="warning", message="the feeders have not been scanned since start")
        )

    parts: dict[str, Part | None] = {}

    def part(part_id: str) -> Part | None:
        if part_id not in parts:
            parts[part_id] = library.find_part(part_id) if library else None
        return parts[part_id]

    items: list[RunItem] = []
    fiducials: list[MachinePlacement] = []
    for p in result.placements:
        if p.kind == "fiducial":
            if p.enabled:
                fiducials.append(p)
            continue
        status: ItemStatus = "pending" if p.enabled else "skipped"
        note = p.note if not p.enabled else ""
        slot_id: str | None = None
        nozzles: list[str] = []
        if p.enabled:
            what = part(p.part_id) if p.part_id else None
            if p.part_id is None:
                note = "no part number"
            elif what is None:
                note = f"{p.part_id} is not in the parts library"
            elif what.height is None:
                note = f"{p.part_id} has no height in the parts library"
            elif p.part_id not in on_bed and (wide := _too_wide(what, profile)) is not None:
                note = wide
            elif p.part_id not in loaded:
                note = f"no feeder holds {p.part_id}"
            else:
                for sid, picks in loaded[p.part_id]:
                    ok = [
                        n
                        for n, r in reach.items()
                        if _inside(r, p.x, p.y) and all(_inside(r, q.x, q.y) for q in picks)
                    ]
                    if ok:
                        slot_id, nozzles = sid, ok
                        break
                if slot_id is None:
                    note = "no nozzle reaches both the place and a feeder holding its part"
            if note:
                status = "blocked"
        items.append(
            RunItem(
                index=0,
                copy_id=p.copy_id,
                ref=p.ref,
                part_id=p.part_id,
                footprint_id=p.footprint_id,
                x=p.x,
                y=p.y,
                rotation=machine_angle(profile, p.rotation),
                slot_id=slot_id,
                nozzles=nozzles,
                status=status,
                note=note,
            )
        )

    def height(item: RunItem) -> float:
        what = part(item.part_id) if item.part_id else None
        return what.height if what and what.height is not None else math.inf

    items.sort(
        key=lambda i: (height(i), _slot_order(i.slot_id), i.part_id or "", i.copy_id or "", i.ref)
    )
    for n, item in enumerate(items):
        item.index = n

    # One issue per problem, naming the placements it affects.
    blocked: dict[str, list[RunItem]] = {}
    for item in items:
        if item.status == "blocked":
            blocked.setdefault(item.note, []).append(item)
    for note, affected in blocked.items():
        refs = [f"{i.ref}{f' (board {i.copy_id})' if i.copy_id else ''}" for i in affected]
        shown = ", ".join(refs[:6]) + (f", and {len(refs) - 6} more" if len(refs) > 6 else "")
        issues.append(Issue(severity="error", message=f"{note}: {shown}", ref=affected[0].ref))
    one_nozzle = [i for i in items if i.status == "pending" and len(i.nozzles) < len(reach)]
    if one_nozzle:
        issues.append(
            Issue(
                severity="warning",
                message=(
                    f"{len(one_nozzle)} placement{'s' if len(one_nozzle) != 1 else ''} can "
                    "be reached by only some of the nozzles, which slows the run"
                ),
            )
        )
    # Strips and trays that will run out.
    for f in feeders.fixed:
        takes = sum(1 for i in items if i.status == "pending" and i.slot_id == f.id)
        if takes > f.remaining:
            issues.append(
                Issue(
                    severity="warning",
                    message=(
                        f"{f.id} has {f.remaining} part{'s' if f.remaining != 1 else ''} "
                        f"left and this job takes {takes}: the run will pause for you to "
                        "refill it"
                    ),
                )
            )
    chosen = choose_fiducials(fiducials)
    if not chosen and result.placements:
        issues.append(
            Issue(
                severity="warning",
                message="no fiducials: the board is assumed to be exactly where the job says",
            )
        )

    needs: dict[str | None, PartNeed] = {}
    for item in items:
        if item.status == "skipped":
            continue
        need = needs.get(item.part_id)
        if need is None:
            slots = [sid for sid, _ in loaded.get(item.part_id or "", [])]
            need = PartNeed(part_id=item.part_id, count=0, slot_ids=slots)
            needs[item.part_id] = need
        need.count += 1
        if item.status == "blocked" and need.problem is None:
            need.problem = item.note
    return JobPlan(
        job_id=job_id,
        name=job.name,
        items=items,
        parts=sorted(needs.values(), key=lambda n: (n.problem is None, n.part_id or "")),
        fiducials=len(chosen),
        issues=issues,
    )


def choose_fiducials(fiducials: list[MachinePlacement]) -> list[MachinePlacement]:
    """Up to three, spread as widely as possible: the panel's own if it has two or more,
    otherwise the boards'."""
    panel = [f for f in fiducials if f.copy_id is None]
    pool = panel if len(panel) >= 2 else fiducials
    if len(pool) <= 2:
        return pool
    pairs = [(a, b) for i, a in enumerate(pool) for b in pool[i + 1 :]]
    a, b = max(pairs, key=lambda ab: math.hypot(ab[0].x - ab[1].x, ab[0].y - ab[1].y))

    def area(c: MachinePlacement) -> float:
        return abs((b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x))

    rest = [c for c in pool if c is not a and c is not b]
    third = max(rest, key=area)
    return [a, b, third][:MAX_FIDUCIALS] if area(third) > 1.0 else [a, b]


def fit_board(about: Point, pairs: list[tuple[Point, Point]]) -> tuple[float, float, float]:
    """The rigid motion (dx, dy, degrees about ``about``) taking expected points to
    found ones, by least squares. One pair gives a shift only."""
    if len(pairs) == 1:
        (e, f), angle = pairs[0], 0.0
    else:
        ex = sum(e.x for e, _ in pairs) / len(pairs)
        ey = sum(e.y for e, _ in pairs) / len(pairs)
        fx = sum(f.x for _, f in pairs) / len(pairs)
        fy = sum(f.y for _, f in pairs) / len(pairs)
        cross = sum((e.x - ex) * (f.y - fy) - (e.y - ey) * (f.x - fx) for e, f in pairs)
        dot = sum((e.x - ex) * (f.x - fx) + (e.y - ey) * (f.y - fy) for e, f in pairs)
        angle = math.degrees(math.atan2(cross, dot))
        e, f = Point(x=ex, y=ey), Point(x=fx, y=fy)
    turned = rotate(Point(x=e.x - about.x, y=e.y - about.y), angle)
    return f.x - (turned.x + about.x), f.y - (turned.y + about.y), angle


# --- Running -----------------------------------------------------------------------------


class JobRunner:
    def __init__(
        self,
        machine: MachineService,
        feeders: FeederService,
        placement: PlacementService,
        library: Callable[[], Library | None],
        jobs: JobStore,
        sim_board_error: tuple[float, float, float] | None = None,
    ) -> None:
        self._machine = machine
        self._feeders = feeders
        self._placement = placement
        self._library = library
        self._jobs = jobs
        self._run: RunState | None = None
        self._job: Job | None = None
        self._active = False
        self._step = False
        self._picked: set[str] = set()
        """Slots, strips, and trays picked from in this run, since the feeders were
        scanned."""
        self._recording: str | None = None
        """The flight recording this runner started for the run, if any."""
        self.record_runs = False
        """Record every run (from the config)."""
        self._sim_error = sim_board_error if feeders.sim_parts is not None else None
        self.sim_decoys = False
        """In the simulator, print a round silkscreen mark smaller than each fiducial
        where the job expects the fiducial, nearer the camera's center than the fiducial
        itself (which is off by the board's error): a fiducial finder that takes the
        nearest round blob is fooled by it, as one was by a label's "D" on a real board."""
        self._sim_board: BoardScene | None = None
        self._sim_fix: BoardFix | None = None
        if self._sim_error is not None:
            machine.scene_boards = self._scene_boards

    def state(self) -> RunState | None:
        run = self._run
        if run is not None and not self._active:
            self._forget_removed(run)
        return run

    def _forget_removed(self, run: RunState) -> None:
        """Forget the parts taken off their nozzles (discarded, or by hand) while paused:
        their placements are picked again."""
        for nozzle_id in list(run.held):
            if self._placement.holds(nozzle_id) is None:
                del run.held[nozzle_id]

    def _get_job(self, job_id: str) -> Job:
        try:
            job = self._jobs.get(job_id)
        except ValueError as e:
            raise InvalidTargetError(str(e)) from e
        if job is None:
            raise InvalidTargetError(f"no job {job_id!r}")
        return job

    def plan(self, job_id: str) -> JobPlan:
        job = self._get_job(job_id)
        return plan_job(job_id, job, self._machine.profile, self._feeders.state(), self._library())

    # --- Control -----------------------------------------------------------------------

    def start(
        self,
        job_id: str,
        skip_blocked: bool,
        pick_correction: PickCorrection | None = None,
        record: bool = False,
    ) -> Operation:
        if self._active:
            raise MachineBusyError("a job is running: pause or stop it first")
        job = self._get_job(job_id)
        plan = plan_job(job_id, job, self._machine.profile, self._feeders.state(), self._library())
        stopping = [i for i in plan.issues if i.severity == "error" and i.ref is None]
        if stopping:
            raise RunError(f"the job cannot run: {stopping[0].message}")
        blocked = [i for i in plan.items if i.status == "blocked"]
        if blocked and not skip_blocked:
            raise RunError(
                f"{len(blocked)} placement{'s are' if len(blocked) != 1 else ' is'} blocked "
                f"({blocked[0].ref}: {blocked[0].note}); fix that, or start with the "
                "blocked ones skipped"
            )
        if not any(i.status == "pending" for i in plan.items):
            raise RunError("the job has nothing to place")
        held = [load.nozzle for load in self._placement.state().loads]
        if held:
            raise RunError(
                f"{held[0]} holds a part: place or discard it first, or take it off by hand "
                "and click Part removed"
            )
        if any(c.looking == "up" for c in self._machine.profile.cameras):
            self._placement.bottom_camera()  # raises if it has no focus height
        self._job = job
        self._run = RunState(
            job_id=job_id,
            name=job.name,
            status="running",
            plan=plan,
            fix=None,
            pick_correction=pick_correction or self._placement.pick_correction,
            started_at=datetime.now(UTC),
        )
        self._step = False
        self._picked = set()
        if self._sim_error is not None:
            self._load_sim_board(job)
        recorder = self._machine.recorder
        active = recorder.active
        if self._recording is not None and active is not None and active.id == self._recording:
            recorder.stop("the run was left paused and a new one started")
        self._recording = None
        if (record or self.record_runs) and recorder.available and recorder.active is None:
            info = recorder.start(f"run {job.name}", "run", job_id)
            self._run.recording = info.id
            self._recording = info.id
        recorder.event("run.start", run=self._run)
        return self._start_task()

    def resume(self, step: bool = False) -> Operation:
        """Carry on with a paused run; with ``step``, for one batch only."""
        run = self._need_run()
        if run.status != "paused":
            raise RunError(f"the run is {run.status}, not paused")
        run.status = "running"
        run.error = None
        run.pause_requested = False
        self._step = step
        return self._start_task()

    def pause(self) -> RunState:
        run = self._need_run()
        if run.status == "running":
            run.pause_requested = True
            run.message = "pausing after the parts on the nozzles are placed"
        return run

    def stop(self) -> RunState:
        run = self._need_run()
        if run.status == "running":
            run.stop_requested = True
            run.message = "stopping after the parts on the nozzles are placed"
        elif run.status == "paused":
            self._finish(run, "stopped", "stopped")
        return run

    def skip(self, index: int, skip: bool) -> RunState:
        run = self._need_run()
        if not 0 <= index < len(run.plan.items):
            raise InvalidTargetError(f"no placement {index} in the run")
        item = run.plan.items[index]
        self._forget_removed(run)
        if index in run.held.values():
            raise RunError(
                f"{item.ref}'s part is on a nozzle: place or discard it first, or take it "
                "off by hand and click Part removed"
            )
        operator = "skipped by the operator"
        if skip and item.status in ("pending", "failed"):
            item.status, item.note = "skipped", operator
        elif not skip and (
            item.status == "failed" or (item.status == "skipped" and item.note == operator)
        ):
            item.status, item.note, item.discards = "pending", "", 0
        return run

    def _need_run(self) -> RunState:
        if self._run is None:
            raise RunError("no job has been started")
        return self._run

    def _start_task(self) -> Operation:
        run = self._need_run()
        try:
            op = self._machine.start_task("job_run", self._work)
        except MachineError:
            run.status = "paused"
            raise
        self._active = True
        return op

    def _finish(self, run: RunState, status: RunStatus, message: str) -> None:
        run.status = status
        run.message = message
        run.pause_requested = run.stop_requested = False
        run.finished_at = datetime.now(UTC)
        recorder = self._machine.recorder
        recorder.event("run.end", run=run)
        active = recorder.active
        if self._recording is not None and active is not None and active.id == self._recording:
            recorder.stop(message)
        self._recording = None

    # --- The run itself ----------------------------------------------------------------

    async def _work(self) -> str:
        run = self._need_run()
        try:
            if run.fix is None:
                if self._feeders.has_bus:
                    await self._rescan(run)
                run.message = "finding the board's fiducials"
                run.fix = await self._find_board(run)
            while True:
                if run.stop_requested:
                    self._finish(run, "stopped", f"stopped: {self._summary(run)}")
                    return run.message or ""
                if run.pause_requested:
                    run.status, run.pause_requested = "paused", False
                    run.message = f"paused: {self._summary(run)}"
                    return run.message
                batch = self._next_batch(run)
                if not batch:
                    self._finish(run, "finished", f"finished: {self._summary(run)}")
                    return run.message or ""
                await self._do_batch(run, batch)
                if self._step:
                    run.pause_requested = True
        except MachineError as e:
            run.status, run.error = "paused", str(e)
            run.pause_requested = False
            self._machine.recorder.event("run.paused", error=str(e), code=e.code)
            if isinstance(e, FeederEmptyError):
                run.error += await self._park()
                if run.stop_requested:
                    self._finish(run, "stopped", f"stopped: {self._summary(run)}")
                raise FeederEmptyError(run.error) from e
            if run.stop_requested:
                self._finish(run, "stopped", f"stopped: {self._summary(run)}")
            raise
        except asyncio.CancelledError:
            run.status, run.error = "paused", "cancelled"
            raise
        finally:
            self._active = False

    def _summary(self, run: RunState) -> str:
        counts: dict[str, int] = {}
        for item in run.plan.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        order = ("placed", "pending", "failed", "blocked", "skipped")
        names = {"pending": "to go"}
        return ", ".join(f"{counts[k]} {names.get(k, k)}" for k in order if counts.get(k))

    async def _rescan(self, run: RunState) -> None:
        """Scan the feeders, and take each placement still to do from wherever its part is
        now. Pauses the run if some part is no longer loaded."""
        job = self._job
        assert job is not None
        run.message = "scanning the feeders"
        await self._feeders.scan()
        run.located = []
        self._picked = set()
        plan = plan_job(
            run.job_id, job, self._machine.profile, self._feeders.state(), self._library()
        )
        now = {(i.copy_id, i.ref): i for i in plan.items}
        lost: list[str] = []
        for item in run.plan.items:
            if item.status != "pending" or item.index in run.held.values():
                continue
            new = now.get((item.copy_id, item.ref))
            if new is None or new.status != "pending":
                lost.append(f"{item.ref} ({new.note if new else 'not in the job'})")
                continue
            item.slot_id, item.nozzles = new.slot_id, new.nozzles
        if lost:
            raise RunError(
                f"the feeders changed since the run was planned: {', '.join(lost[:5])}"
                f"{' and more' if len(lost) > 5 else ''}. Load the parts and resume, or "
                "skip those placements."
            )

    async def _locate_feeder(self, run: RunState, slot_id: str) -> None:
        """Before the first pick from a feeder slot in this run, find its feeder's
        fiducial."""
        feeders = self._feeders
        if run.pick_correction == "off" or slot_id in run.located or feeders.is_fixed(slot_id):
            return
        slot = feeders.slot(slot_id)
        if feeders.fiducial_offset(feeders.bank(slot))[0] is None:
            return  # feeders without a known fiducial: picks go where the setup says
        run.message = f"finding the feeder fiducial at {slot.label.lower()}"
        moved = await feeders.locate_feeder(slot)
        run.located.append(slot_id)
        self._machine.recorder.event("run.feeder", slot=slot_id, moved=round(moved, 4))

    async def _find_board(self, run: RunState) -> BoardFix:
        job = self._job
        assert job is not None and job.location is not None
        result = layout(job, self._machine.profile)
        chosen = choose_fiducials(
            [p for p in result.placements if p.kind == "fiducial" and p.enabled]
        )
        about = Point(x=job.location.x, y=job.location.y)
        if not chosen:
            return BoardFix(dx=0, dy=0, angle=0, about=about, residual=0, fiducials=[])
        library = self._library()
        checks: list[FiducialCheck] = []
        for f in chosen:
            d = FIDUCIAL_DIAMETER_MM
            footprint = library.find_footprint(f.footprint_id) if library else None
            if footprint and footprint.pads:
                d = min(footprint.pads[0].width, footprint.pads[0].height)
            await self._machine.move_camera_to(f.x, f.y)
            found = await self._machine.locate_circle(d * 0.6, d * 1.5, FIDUCIAL_SEARCH_MM, d)
            where = f"{f.ref}{f' on board {f.copy_id}' if f.copy_id else ''}"
            if found is None:
                raise VisionError(
                    f"fiducial {where} was not found within {FIDUCIAL_SEARCH_MM:g} mm of "
                    f"({f.x:.2f}, {f.y:.2f}); check the board's location in the job"
                )
            checks.append(
                FiducialCheck(
                    ref=f.ref, copy_id=f.copy_id, expected=Point(x=f.x, y=f.y), found=found
                )
            )
        dx, dy, angle = fit_board(about, [(c.expected, c.found) for c in checks])
        fix = BoardFix(dx=dx, dy=dy, angle=angle, about=about, residual=0, fiducials=checks)
        residual = max(
            math.hypot(
                fix.apply(c.expected.x, c.expected.y).x - c.found.x,
                fix.apply(c.expected.x, c.expected.y).y - c.found.y,
            )
            for c in checks
        )
        fix.residual = round(residual, 4)
        fix.dx, fix.dy, fix.angle = round(dx, 4), round(dy, 4), round(angle, 4)
        if residual > FIDUCIAL_RESIDUAL_MM:
            raise RunError(
                f"the fiducials are up to {residual:.2f} mm from where the job's layout puts "
                "them relative to each other: check that the right board is loaded, the "
                "side facing up, and the fiducials in the design"
            )
        run.message = (
            f"board found {math.hypot(dx, dy):.2f} mm from its location in the job, "
            f"turned {angle:+.2f} degrees ({len(checks)} fiducials)"
        )
        log.info("%s", run.message)
        return fix

    def _next_batch(self, run: RunState) -> dict[str, int]:
        """Nozzle ID to item index, for every nozzle that can take a part now."""
        nozzles = [n.id for n in self._machine.profile.nozzles]
        batch: dict[str, int] = {}
        # Parts still held from before a pause come first.
        for nozzle_id, index in list(run.held.items()):
            if self._placement.holds(nozzle_id) is None:
                # Discarded, or taken off by hand, while paused: pick it again.
                del run.held[nozzle_id]
            else:
                batch[nozzle_id] = index
        busy = set(run.held.values())
        pending = [i for i in run.plan.items if i.status == "pending" and i.index not in busy]
        # Nozzles that some pending items can use alone go to those, if they come up.
        only = {n: sum(1 for i in pending if i.nozzles == [n]) for n in nozzles}
        for item in pending:
            free = [n for n in item.nozzles if n not in batch]
            if not free:
                continue
            batch[min(free, key=lambda n: only.get(n, 0))] = item.index
            if len(batch) == len(nozzles):
                break
        return batch

    async def _do_batch(self, run: RunState, batch: dict[str, int]) -> None:
        job = self._job
        assert job is not None and run.fix is not None
        items = run.plan.items
        for nozzle_id, index in batch.items():
            if nozzle_id in run.held:
                continue
            item = items[index]
            nozzle = self._machine.nozzle(nozzle_id)
            assert item.slot_id is not None
            await self._locate_feeder(run, item.slot_id)
            run.message = f"{nozzle_id} picking {item.ref} from {_source(item.slot_id)}"
            for attempt in range(1, PICK_ATTEMPTS + 1):
                try:
                    await self._placement.pick(
                        item.slot_id,
                        nozzle,
                        True,
                        run.pick_correction,
                        first=item.slot_id not in self._picked,
                    )
                    self._picked.add(item.slot_id)
                    break
                except MispickError as e:
                    if attempt == PICK_ATTEMPTS:
                        where = _source(item.slot_id)
                        item.note = f"picked nothing {PICK_ATTEMPTS} times from {where}"
                        raise RunError(
                            f"{item.ref}: {e} ({PICK_ATTEMPTS} tries). Check the feeder, "
                            "then resume to try again, or skip the placement."
                        ) from e
            run.held[nozzle_id] = index
            item.note = ""

        for nozzle_id in list(batch):
            index = batch[nozzle_id]
            item = items[index]
            load = self._placement.holds(nozzle_id)
            if load is not None and load.alignment is not None:
                continue
            nozzle = self._machine.nozzle(nozzle_id)
            run.message = f"{nozzle_id} aligning {item.ref}"
            try:
                await self._placement.align(nozzle)
            except VisionError as e:
                if not self._placement.can_discard():
                    item.discards += 1
                    item.note = f"bottom vision failed ({e}): take the part off {nozzle_id}"
                    parked = await self._park()
                    raise RunError(
                        f"{item.ref}: {e}, and there is no discard location to drop it in."
                        f"{parked} Take whatever is on {nozzle_id} off it (it may be empty), "
                        "click Part removed, and resume to pick it again, or skip the "
                        "placement. Add a discard location under [[machine.locations]] in the "
                        "config, and the run will do this itself."
                    ) from e
                await self._placement.discard(nozzle)
                del run.held[nozzle_id]
                del batch[nozzle_id]
                item.discards += 1
                if item.discards >= ALIGN_ATTEMPTS:
                    item.status = "failed"
                    item.note = f"bottom vision failed {item.discards} times: {e}"
                else:
                    item.note = f"bottom vision failed ({e}): part discarded, to try again"

        for nozzle_id, index in batch.items():
            item = items[index]
            nozzle = self._machine.nozzle(nozzle_id)
            target = run.fix.apply(item.x, item.y)
            run.message = f"{nozzle_id} placing {item.ref}"
            await self._placement.place(nozzle, target, item.rotation + run.fix.angle, job.board_z)
            del run.held[nozzle_id]
            item.status, item.note, item.nozzle = "placed", "", nozzle_id
            self._machine.recorder.event("run.placed", ref=item.ref, index=index, at=target)

    async def _park(self) -> str:
        """Move the head out of the way of an operator refilling a strip or tray: to the
        park location, or else to the back of its travel. Says where it went, for the
        run's message; a move that fails is reported, not raised, so the pause stands."""
        profile = self._machine.profile
        park = next((loc for loc in profile.locations if loc.kind == "park"), None)
        if park is not None:
            target = {"X": park.position.x, "Y": park.position.y}
            where = f"the {park.label.lower()}"
        else:
            y = next((a for a in profile.axes if a.name == "Y"), None)
            if y is None:
                return ""
            target = {"Y": y.max if profile.y_sign > 0 else y.min}
            where = "the back (no park location is set)"
        try:
            await self._machine.raise_nozzles()
            await self._machine.move_axes(target)
        except MachineError as e:
            log.warning("could not park the head: %s", e)
            return f" The head could not move out of the way ({e}): mind it."
        return f" The head moved to {where}."

    # --- Simulation --------------------------------------------------------------------

    def _load_sim_board(self, job: Job) -> None:
        """Put the job's board on the simulated bed, a little off from where the job
        says, so fiducial alignment has something to find."""
        assert self._sim_error is not None and job.location is not None
        dx, dy, angle = self._sim_error
        about = Point(x=job.location.x, y=job.location.y)
        true = BoardFix(dx=dx, dy=dy, angle=angle, about=about, residual=0, fiducials=[])
        self._sim_fix = true
        result = layout(job, self._machine.profile)
        library = self._library()

        def at(x: float, y: float) -> tuple[float, float]:
            p = true.apply(x, y)
            return p.x, p.y

        pads: list[tuple[float, float, float, float, float]] = []
        fiducials: list[tuple[float, float, float]] = []
        marks: list[tuple[float, float, float]] = []
        for p in result.placements:
            footprint = library.find_footprint(p.footprint_id) if library else None
            if p.kind == "fiducial":
                d = FIDUCIAL_DIAMETER_MM
                if footprint and footprint.pads:
                    d = min(footprint.pads[0].width, footprint.pads[0].height)
                fiducials.append((*at(p.x, p.y), d))
                if self.sim_decoys:
                    marks.append((p.x, p.y, d * SIM_DECOY_SIZE))
                continue
            if footprint is None:
                continue
            a = machine_angle(self._machine.profile, p.rotation) + angle
            for pad in footprint.pads:
                off = rotate(Point(x=pad.x, y=pad.y), a)
                x, y = at(p.x, p.y)
                pads.append((x + off.x, y + off.y, pad.width, pad.height, a + pad.rotation))
        outline = tuple(at(c.x, c.y) for c in result.outline)
        self._sim_board = BoardScene(
            outline=outline, fiducials=tuple(fiducials), pads=tuple(pads), marks=tuple(marks)
        )

    def sim_target(self, item: RunItem) -> tuple[Point, float] | None:
        """Where the simulated board really wants an item's part, and at what angle."""
        if self._sim_fix is None:
            return None
        return self._sim_fix.apply(item.x, item.y), item.rotation + self._sim_fix.angle

    def _scene_boards(self) -> tuple[BoardScene, ...]:
        return (self._sim_board,) if self._sim_board is not None else ()
