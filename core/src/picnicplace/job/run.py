"""Running a job: every part on the board picked, aligned, and placed (milestone M4).

**What runs.** A machine runs a job's published revision (spec §5.4), its newest unless
another is asked for, set up as this machine keeps it (where the panel sits, the
board's height), with parts, footprints, and tapes from the revision's own copy of the
library. A job not yet published can be planned from its working copy and the library,
but not run.

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
placement. Without fiducials, the job's location is used as it is. Then a nozzle finds
the board's top by touch on the first fiducial (flat, bare copper): with suction on, it
steps down until the vacuum seals. Every part is placed at that height, so a thicker or
warped board, or one higher on its plate, is placed on as it is.

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
pauses for the operator to take the part off and say so (Part removed). A feeder
problem on one nozzle's pick (nothing picked after the tries, a pocket or strip it cannot
use) lets the other nozzles pick, places every part held, and then pauses for it. Anything
else pauses the run at once, with the parts already picked left on their nozzles: fix the
cause, then resume, or skip the placement. An empty strip or tray pauses the run too, with the head
moved to the park position out of the operator's way: refill it, mark it refilled, and
resume. Pausing and stopping take effect after the batch in progress; a step resumes a
paused run for one batch.

**Speed.** The run counts the time the machine spends working on it (``stats.working_s``),
leaving out the time it sits paused for the operator, so the Studio can give placements per
hour both while working and from the start of the run to its end.

A pause that asks the operator to do something (a problem, or the Pause button) parks the
head out of the way first, as the end of a run does (finished or stopped, for the board to
come off); the eye button on a placement's line brings the camera back
over it. A step's pause leaves the head where it is.

**Progress.** The run state says which step is under way (scanning the feeders, finding
the board, or finding a feeder's fiducial, picking, aligning, or placing one item), and
keeps it when a problem pauses the run, so the Studio can show where it stopped. It is
written to disk after every batch and every change, and read back when the core starts:
a run the core stopped in the middle of comes back paused and marked interrupted. The
machine must be homed again, and the nozzles may still hold parts, so the operator clears
them first; resuming finds the board's fiducials again, then goes on with what is left.
"""

import asyncio
import logging
import math
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import cv2
from pydantic import Field, computed_field

from picnicplace.calibration import current, run_issues
from picnicplace.camera.source import Image
from picnicplace.camera.synthetic import BoardScene
from picnicplace.feeders.service import FeederService, FeedersState, rotate
from picnicplace.feeders.setup import PickCorrection
from picnicplace.job.footprint_check import footprint_mismatch
from picnicplace.job.layout import Issue, MachinePlacement, layout
from picnicplace.job.models import Job
from picnicplace.job.revision import JobFileError, PartsSource
from picnicplace.job.store import JobStore
from picnicplace.library import Library, Part, Tape
from picnicplace.library.models import Footprint
from picnicplace.machine.errors import (
    FeederEmptyError,
    FeederError,
    InvalidTargetError,
    MachineBusyError,
    MachineError,
    UntrainableError,
    VisionError,
)
from picnicplace.machine.profile import MachineProfile, Point, Rect
from picnicplace.machine.service import MachineService
from picnicplace.machine.sniff import floor_message, sniff
from picnicplace.placement import MispickError, PlacementService
from picnicplace.schemas import Operation, WireModel
from picnicplace.storage import write_atomic
from picnicplace.vision.placed import (
    FoundPads,
    PlacedChip,
    Polygon,
    check_picture,
    chip_ends,
    chip_offset,
    image_turn,
    on_pads,
)

log = logging.getLogger(__name__)

PICK_ATTEMPTS = 3
ALIGN_ATTEMPTS = 2
UNTRAINABLE = "skipped: training found no way of seeing its part"
"""How a placement's note begins when its part could not be trained in the run: it and the
part's other placements are skipped, not picked again (each pick would cost a part and see
no more). The operator may un-skip them once the part is trained."""
"""Times a part may be discarded for bottom vision before its placement fails."""
MAX_FIDUCIALS = 3
FIDUCIAL_SEARCH_MM = 3.0
"""How far from where the job says a fiducial is, the camera looks for it."""
BOARD_SNIFF_ABOVE_MM = 1.5
"""Sniffing for the board's top starts this far above the job's or profile's height..."""
BOARD_SNIFF_BELOW_MM = 1.0
"""...and stops this far below it."""
FIDUCIAL_DIAMETER_MM = 1.0
"""For fiducials whose footprint is not in the library."""
SIM_DECOY_SIZE = 0.7
"""A simulated decoy's size, as a fraction of its fiducial's (see ``sim_decoys``)."""
FIDUCIAL_RESIDUAL_MM = 0.25
"""Fiducials found further than this from the fit mean the board, or one of them, is
not where the job says: the run stops before placing anything."""
CHECK_OFF_MM = 0.25
"""Checking placements: a part found further than this from where it was sent (when its
pads cannot be seen; else ``CHECK_ON_PADS``)..."""
CHECK_TURN = 10.0
"""...or turned more than this many degrees from the angle asked for..."""
CHECK_SCORE = 0.5
"""...or not found (the pattern matched worse than this) pauses the run, to be looked at:
a real 0603 that landed 0.43 mm along its length, one end lifted, would have stood up when
soldered."""
CHECK_UNSURE = 0.35
"""A part matched worse than ``CHECK_SCORE`` but at least this well is not judged: on a real
LumenPnP (2026-11-20) gray ceramic capacitors sitting squarely on their pads matched 0.42 to
0.5 (black resistors 0.6 to 0.7), and the run paused after each. Such a placement is marked
not checked, and the run goes on."""
CHECK_ON_PADS = 0.75
"""A part with less than this of either metal end over its pad pauses the run. On a real
0603 (2026-11-19), one 0.3 mm along its length from its pads had 23% of an end on its pad,
and one 0.3 mm across, 72%; those well placed had 97% or more."""
CHECK_PADS_SCORE = 0.6
"""The pads' shape must match the bright copper this well (0 to 1) to be judged by; real
0603 pads matched 0.8 to 0.9."""
CHECK_PADS_MOVED_MM = 0.4
"""Pads found further than this from where the board's fit puts them mean the board has
moved since its fiducials were found: the run pauses, and finds them again on resume. On a
real board knocked by the parts pressed into its tape, the pads were 0.2 to 0.33 mm off;
but on a real LumenPnP (2026-11-20), pads on a board that never moved (its fiducials found
again eight times, each time within 0.01 mm of the last) were up to 0.33 mm off too, the
machine's own error from place to place: lower than this, the pause is a false alarm.
The pads are looked for no more than about 0.5 mm from where they should be."""


TIP_HOLE_MAX = 1.0
"""A nozzle tip may pick a part when its hole is no wider than this share of the part's
narrower side. On a real LumenPnP (2026-11-20) an N08 tip (0.8 mm hole) picked 0603
resistors (0.8 mm wide) 39 times out of 39; on 0603 capacitors, thicker and heavier, it
sealed on the tape beside them and lifted nothing. Which tip suits which part beyond size
is learned from picks (``TipPicks``)."""

TIP_HOLE_MIN = 0.35
"""...and no narrower than this share of it: a much smaller hole holds a large part
weakly. With no tip in between, the largest hole that is not too wide."""


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
    check: "PlacementCheck | None" = Field(
        default=None, description="Where the top camera found the part after placing it."
    )


class PlacementCheck(WireModel):
    """Where the top camera found a part just placed, against where it was sent."""

    dx: float = Field(description="Machine X from where it was sent, mm.")
    dy: float
    turn: float = Field(description="Degrees from the angle asked for.")
    score: float = Field(description="How well the chip's pattern matched, -1 to 1.")
    on_pads: float | None = Field(
        default=None,
        description=(
            "How much of the part's metal ends lies over its pads: of each end, the share "
            "over its pad, and of the two the less, 0 to 1. None if the pads were not seen."
        ),
    )
    pads_dx: float | None = Field(
        default=None,
        description=(
            "Machine X of the pads from where the board's fit puts them, mm: far off, the "
            "board has moved. None if the pads were not seen."
        ),
    )
    pads_dy: float | None = None
    ok: bool
    checked: bool = Field(
        default=True,
        description=(
            "Judged at all: false when the camera could not make the part out well enough "
            "to say (a gray ceramic capacitor on a board, say), so the run went on."
        ),
    )
    picture: bool = Field(
        default=False,
        description="A picture of it is kept (GET /api/v1/run/checks/{index}).",
    )


class RunStats(WireModel):
    """What went wrong how often in a run, to tell how well it went."""

    picks: int = Field(default=0, description="Picks tried.")
    empty_picks: int = Field(default=0, description="Picks that came up without a part.")
    alignments: int = Field(default=0, description="Parts the bottom camera looked for.")
    failed_alignments: int = Field(
        default=0, description="Parts the bottom camera could not find (discarded)."
    )
    placed: int = Field(default=0, description="Parts placed.")
    checked: int = Field(default=0, description="Placements checked with the top camera.")
    unsure: int = Field(
        default=0,
        description="Checked placements the camera could not make out well enough to judge.",
    )
    bad_placements: int = Field(
        default=0, description="Checked placements off their target, turned, or not found."
    )
    working_s: float = Field(
        default=0.0,
        description=(
            "Seconds the machine spent working on the run, not counting the time it sat "
            "paused for the operator (or the core was down)."
        ),
    )


class PartNeed(WireModel):
    """One part number the job uses, and the feeders loaded with it: a line of the load
    list, with what it takes to find the reel and a feeder for it."""

    part_id: str | None
    count: int
    slot_ids: list[str]
    problem: str | None = None
    refs: list[str] = Field(
        default_factory=list[str], description="Its references on the board, in order."
    )
    value: str = Field(default="", description="Its value: the board's, else the library's.")
    description: str = ""
    manufacturer: str = ""
    footprint_id: str | None = None
    tape: Tape | None = Field(default=None, description="The tape it comes in, if known.")


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


class BoardCheck(WireModel):
    """The last check of a job's board: its fiducials found and fitted, nothing placed."""

    job_id: str
    checked_at: datetime
    fix: BoardFix | None = Field(default=None, description="The fit, if the fiducials were found.")
    ok: bool = Field(description="Every fiducial was found and they fit the job's layout.")
    message: str


class JobPlan(WireModel):
    job_id: str
    revision: int | None = Field(
        default=None,
        description=(
            "The published revision planned, whose copy of the library it uses. Empty for "
            "a job not yet published, planned from its working copy (which cannot run)."
        ),
    )
    name: str
    items: list[RunItem]
    parts: list[PartNeed]
    fiducials: int = Field(description="Fiducials the run will look for.")
    issues: list[Issue]
    nozzles: list[str] = Field(
        default_factory=list[str], description="The machine's nozzles, in the profile's order."
    )
    taken: list[int] = Field(
        default_factory=list[int],
        description="Items in the order the run took them onto a nozzle (empty until it runs).",
    )

    @computed_field
    @property
    def order(self) -> list[int]:
        """Every item, in the order the run deals with them: those it has taken onto a
        nozzle, as it took them; then the rest to place, batch by batch as the run will
        take them if every part goes on as planned; then those it will not place."""
        return processing_order(self.items, self.nozzles, self.taken)

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


def fill_batch(pending: list[RunItem], nozzles: list[str], batch: dict[str, int]) -> dict[str, int]:
    """Give each free nozzle the first of ``pending`` it can take, in order; a nozzle that
    some items can use alone goes to those, if they come up. ``batch`` (nozzle ID to item
    index) is filled in place and returned."""
    only = {n: sum(1 for i in pending if i.nozzles == [n]) for n in nozzles}
    for item in pending:
        if len(batch) >= len(nozzles):
            break
        free = [n for n in item.nozzles if n not in batch]
        if not free:
            continue
        batch[min(free, key=lambda n: only.get(n, 0))] = item.index
    return batch


def processing_order(items: list[RunItem], nozzles: list[str], taken: list[int]) -> list[int]:
    """Item indices in the order a run deals with them (see ``JobPlan.order``)."""
    order: list[int] = []
    seen: set[int] = set()
    for index in taken:
        if 0 <= index < len(items) and index not in seen:
            order.append(index)
            seen.add(index)
    # Dealt with but not in ``taken`` (a run kept before it was): next, in plan order.
    for i in items:
        if i.status in ("placed", "failed") and i.index not in seen:
            order.append(i.index)
            seen.add(i.index)
    nozzles = nozzles or sorted({n for i in items for n in i.nozzles})
    rest = [i for i in items if i.status == "pending" and i.index not in seen]
    while rest:
        batch = fill_batch(rest, nozzles, {})
        if not batch:
            break
        order += batch.values()
        seen.update(batch.values())
        rest = [i for i in rest if i.index not in seen]
    order += [i.index for i in items if i.index not in seen]
    return order


RunStatus = Literal["running", "paused", "finished", "stopped"]

StepAction = Literal[
    "scanning",
    "finding_fiducials",
    "measuring_board",
    "finding_feeder",
    "picking",
    "aligning",
    "placing",
    "checking",
]


class RunStep(WireModel):
    """What the run is doing now, or was doing when it paused on a problem."""

    stage: Literal["scan", "board", "item"] = Field(
        description="Scanning the feeders, finding the board, or working on a placement."
    )
    action: StepAction
    index: int | None = Field(default=None, description="The placement, for stage 'item'.")
    nozzle: str | None = None


class RunState(WireModel):
    job_id: str
    name: str
    status: RunStatus
    plan: JobPlan
    fix: BoardFix | None
    board_z: float | None = Field(
        default=None,
        description=(
            "Nozzle height at the board's top, found by touch on a fiducial at the start of "
            "the run. Empty if not measured: the job's or the profile's is used."
        ),
    )
    message: str | None = None
    error: str | None = Field(default=None, description="Why the run paused, if it did.")
    pause_requested: bool = False
    stop_requested: bool = False
    pick_correction: PickCorrection = "top"
    check_placements: bool = Field(
        default=False,
        description="Look at each part with the top camera once placed, pausing on a bad one.",
    )
    stats: RunStats = Field(default_factory=RunStats)
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
    current: RunStep | None = Field(
        default=None,
        description=(
            "The step under way. Kept when the run pauses on a problem, to show where it "
            "stopped; empty between batches and when the run ends."
        ),
    )
    interrupted: bool = Field(
        default=False,
        description=(
            "The core stopped in the middle of this run (a restart, a crash, a power cut) "
            "and read it back from disk when it started again. Cleared on resume."
        ),
    )
    started_at: datetime
    finished_at: datetime | None = None
    working_since: datetime | None = Field(
        default=None,
        description=(
            "While the machine is working on the run: since when, beyond the time counted "
            "in stats.working_s."
        ),
    )

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


def part_width(footprint: Footprint | None) -> float | None:
    """A part's narrower side seen from above: its body's, or else its pads' extent."""
    if footprint is None:
        return None
    if footprint.body is not None:
        return min(footprint.body.x, footprint.body.y)
    if not footprint.pads:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for pad in footprint.pads:
        across = round(pad.rotation / 90) % 2 == 1
        w, h = (pad.height, pad.width) if across else (pad.width, pad.height)
        xs += [pad.x - w / 2, pad.x + w / 2]
        ys += [pad.y - h / 2, pad.y + h / 2]
    return min(max(xs) - min(xs), max(ys) - min(ys))


def tip_nozzles(
    nozzles: list[str],
    part: Part | None,
    library: PartsSource | None,
    profile: MachineProfile,
    barred: Callable[[str, str], bool] | None = None,
) -> list[str]:
    """Of ``nozzles``, the ones to pick ``part`` with, by the tips fitted: those whose hole
    suits its size (``TIP_HOLE_MAX``, ``TIP_HOLE_MIN``), less any tip ``barred`` for it
    (part ID, tip ID: one that never picked it where another tip has) unless that leaves
    none. All of them when a tip or the part's size is not known, or when no tip suits it."""
    footprint = library.find_footprint(part.footprint_id) if library and part else None
    width = part_width(footprint)
    if width is None:
        return nozzles
    holes: dict[str, float] = {}
    tips: dict[str, str] = {}
    for nozzle_id in nozzles:
        nozzle = next((n for n in profile.nozzles if n.id == nozzle_id), None)
        tip = profile.nozzle_tip(nozzle.tip) if nozzle is not None else None
        if tip is None or tip.inner_diameter is None:
            return nozzles
        holes[nozzle_id], tips[nozzle_id] = tip.inner_diameter, tip.id
    small = {n: h for n, h in holes.items() if h <= TIP_HOLE_MAX * width + 1e-9}
    if not small:
        return nozzles
    fit = {n: h for n, h in small.items() if h >= TIP_HOLE_MIN * width}
    if not fit:
        best = max(small.values())
        fit = {n: h for n, h in small.items() if h == best}
    if barred is not None and part is not None:
        kept = {n: h for n, h in fit.items() if not barred(part.id, tips[n])}
        fit = kept or fit
    return [n for n in nozzles if n in fit]


def _inside(rect: Rect, x: float, y: float) -> bool:
    return rect.x_min <= x <= rect.x_max and rect.y_min <= y <= rect.y_max


def _is_tray(feeders: FeedersState, slot_id: str) -> bool:
    return any(f.id == slot_id and f.feeder.kind == "tray" for f in feeders.fixed)


def _too_wide(part: Part, tape: Tape | None, profile: MachineProfile) -> str | None:
    """Why no feeder on the machine can take the part's tape, if none can."""
    width = tape.width if tape else None
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


def _ref_order(ref: str) -> tuple[str, int, str]:
    """C2 before C10."""
    match = re.fullmatch(r"([^\d]*)(\d+)(.*)", ref)
    return (ref, 0, "") if match is None else (match[1], int(match[2]), match[3])


def _source(slot_id: str) -> str:
    return f"slot {slot_id}" if slot_id.isdigit() else slot_id


def short_reels(items: list[RunItem], feeders: FeedersState) -> list[Issue]:
    """A warning for each feeder whose reel, as counted, has fewer parts left than the
    placements planned from it."""
    needed: dict[str, int] = {}
    for item in items:
        if item.status == "pending" and item.slot_id is not None:
            needed[item.slot_id] = needed.get(item.slot_id, 0) + 1
    issues: list[Issue] = []
    for s in feeders.slots:
        a = s.feeder.assignment if s.feeder else None
        n = needed.get(s.slot.id, 0)
        if a is None or a.reel_left is None or n <= a.reel_left:
            continue
        issues.append(
            Issue(
                severity="warning",
                message=(
                    f"{s.slot.label}'s reel of {a.part_id} has {a.reel_left} parts left by its "
                    f"count, and the job places {n} from it: load a fuller reel, or splice on "
                    "more and set its count (Feeders workspace)"
                ),
            )
        )
    return issues


def blocked_issues(items: list[RunItem]) -> list[Issue]:
    """One issue per reason placements are blocked, naming the placements it affects."""
    blocked: dict[str, list[RunItem]] = {}
    for item in items:
        if item.status == "blocked":
            blocked.setdefault(item.note, []).append(item)
    issues: list[Issue] = []
    for note, affected in blocked.items():
        refs = [f"{i.ref}{f' (board {i.copy_id})' if i.copy_id else ''}" for i in affected]
        shown = ", ".join(refs[:6]) + (f", and {len(refs) - 6} more" if len(refs) > 6 else "")
        issues.append(Issue(severity="error", message=f"{note}: {shown}", ref=affected[0].ref))
    return issues


def plan_job(
    job_id: str,
    job: Job,
    profile: MachineProfile,
    feeders: FeedersState,
    library: PartsSource | None,
    revision: int | None = None,
    sniff_heights: bool = False,
    barred: Callable[[str, str], bool] | None = None,
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
        elif f.feeder.pick_height is None and f.feeder.kind == "strip" and not sniff_heights:
            problem = (
                "it has no pick height (set one in the Feeders workspace, or turn on "
                "sniff_heights in the config to find it by touch)"
            )
        elif f.feeder.pick_height is None and f.feeder.kind == "strip":
            issues.append(
                Issue(
                    severity="warning",
                    message=f"{f.id} has no pick height: its first pick searches down from "
                    "safe Z for its parts' top, which is slow; setting the height is quicker",
                )
            )
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
                "pauses the run for you to take it off the nozzle (set one in the Machine "
                "workspace's Calibration panel: center the camera over the discard tray and "
                "click Set discard here)",
            )
        )
    if feeders.restored:
        issues.append(
            Issue(
                severity="warning",
                message="the feeders have not been scanned since the core started: the slots "
                "are as the last scan found them. The run scans them again first; scan now to "
                "see any you moved since",
            )
        )
    elif not feeders.scanned and profile.feeder_bus is not None:
        issues.append(
            Issue(severity="warning", message="the feeders have not been scanned since start")
        )

    parts: dict[str, Part | None] = {}

    def part(part_id: str) -> Part | None:
        if part_id not in parts:
            parts[part_id] = library.find_part(part_id) if library else None
        return parts[part_id]

    items: list[RunItem] = []
    by_tip: set[str] = set()
    """Placements left to fewer nozzles by the tips fitted (``tip_nozzles``)."""
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
            elif (
                p.part_id not in on_bed
                and (wide := _too_wide(what, library.tape_of(what) if library else None, profile))
                is not None
            ):
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
                        slot_id, nozzles = sid, tip_nozzles(ok, what, library, profile, barred)
                        if len(nozzles) < len(ok):
                            by_tip.add(p.ref + (p.copy_id or ""))
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

    issues += blocked_issues(items)
    issues += short_reels(items, feeders)

    def key(i: RunItem) -> str:
        return i.ref + (i.copy_id or "")

    one_nozzle = [
        i
        for i in items
        if i.status == "pending" and len(i.nozzles) < len(reach) and key(i) not in by_tip
    ]
    tipped: dict[str, int] = {}
    for i in items:
        if i.status == "pending" and key(i) in by_tip:
            names: list[str] = []
            for nozzle_id in i.nozzles:
                nozzle = next(n for n in profile.nozzles if n.id == nozzle_id)
                names.append(f"{nozzle.label.lower()} ({nozzle.tip})")
            shown = " or ".join(names)
            tipped[shown] = tipped.get(shown, 0) + 1
    if tipped:
        each = "; ".join(f"{n} only on the {where}" for where, n in tipped.items())
        issues.append(
            Issue(
                severity="warning",
                message=(
                    f"by the tips fitted, some parts are picked by one nozzle: {each}. A tip "
                    "suits a part when its hole lies well inside the part's top"
                ),
            )
        )
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
    # Parts on tape whose rotation in tape is not known are picked as if it were 0.
    turned: list[str] = []
    for item in items:
        what = part(item.part_id) if item.status == "pending" and item.part_id else None
        tape = library.tape_of(what) if library and what else None
        unknown = tape is None or tape.rotation is None
        on_tape = item.slot_id is not None and not _is_tray(feeders, item.slot_id)
        if what is not None and what.id not in turned and unknown and on_tape:
            turned.append(what.id)
    if turned:
        issues.append(
            Issue(
                severity="warning",
                message=(
                    f"the rotation in tape is not known for {', '.join(turned[:6])}"
                    f"{' and more' if len(turned) > 6 else ''}: they are placed as if it "
                    "were 0 degrees. Give each part its tape in the Parts workspace."
                ),
            )
        )
    # Parts whose tape is still the guess made from their footprint (``tape_rules``).
    guessed: list[str] = []
    for item in items:
        what = part(item.part_id) if item.status == "pending" and item.part_id else None
        tape = library.tape_of(what) if library and what else None
        is_guess = tape is not None and tape.id.endswith("-guessed")
        if what is not None and is_guess and what.id not in guessed:
            guessed.append(what.id)
    if guessed:
        issues.append(
            Issue(
                severity="warning",
                message=(
                    f"the tape of {', '.join(guessed[:6])}{' and more' if len(guessed) > 6 else ''}"
                    " is guessed from its footprint: check its width, pitch and which way round "
                    "the parts lie against the reel, and give the part that tape in the Parts "
                    "workspace"
                ),
            )
        )
    # Parts whose footprint is not the board's where they go.
    mismatched: dict[tuple[str, str, str], list[RunItem]] = {}
    for item in items:
        what = part(item.part_id) if item.status != "skipped" and item.part_id else None
        if library is None or what is None or what.footprint_id == item.footprint_id:
            continue
        why = footprint_mismatch(
            library.find_footprint(item.footprint_id), library.find_footprint(what.footprint_id)
        )
        if why is not None:
            mismatched.setdefault((what.id, item.footprint_id, why), []).append(item)
    for (part_id, board_footprint, why), affected in mismatched.items():
        what = part(part_id)
        refs = sorted({i.ref for i in affected}, key=_ref_order)
        shown = ", ".join(refs[:6]) + (f", and {len(refs) - 6} more" if len(refs) > 6 else "")
        issues.append(
            Issue(
                severity="warning",
                message=(
                    f"{part_id}'s footprint {what.footprint_id if what else '?'} is not the "
                    f"board's {board_footprint} at {shown}: {why}. Check the part number, or "
                    "the part's footprint in the Parts workspace"
                ),
                ref=affected[0].ref,
            )
        )
    # Feeders whose tape width does not match their part's tape.
    used = {i.part_id for i in items if i.status == "pending" and i.part_id}
    for slot in feeders.slots:
        a = slot.feeder.assignment if slot.feeder else None
        if slot.warning and a is not None and a.part_id in used:
            issues.append(Issue(severity="warning", message=slot.warning))
    for f in feeders.fixed:
        if f.warning and f.feeder.part_id in used:
            issues.append(Issue(severity="warning", message=f.warning))
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

    values = {p.ref: p.value for p in job.board.placements if p.value}
    needs: dict[str | None, PartNeed] = {}
    for item in items:
        if item.status == "skipped":
            continue
        need = needs.get(item.part_id)
        if need is None:
            slots = [sid for sid, _ in loaded.get(item.part_id or "", [])]
            what = part(item.part_id) if item.part_id else None
            need = PartNeed(
                part_id=item.part_id,
                count=0,
                slot_ids=slots,
                value=values.get(item.ref) or (what.name if what else ""),
                description=what.description if what else "",
                manufacturer=what.manufacturer if what else "",
                footprint_id=item.footprint_id,
                tape=library.tape_of(what) if library and what else None,
            )
            needs[item.part_id] = need
        need.count += 1
        if item.ref not in need.refs:
            need.refs.append(item.ref)
        if item.status == "blocked" and need.problem is None:
            need.problem = item.note
    for need in needs.values():
        need.refs.sort(key=_ref_order)
    return JobPlan(
        job_id=job_id,
        revision=revision,
        name=job.name,
        items=items,
        parts=sorted(needs.values(), key=lambda n: (n.problem is None, n.part_id or "")),
        fiducials=len(chosen),
        issues=issues,
        nozzles=[n.id for n in profile.nozzles],
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


class _HandedOver(Exception):
    """A placement given to other nozzles: the tip on this one has never picked its part."""


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
        self._parts: PartsSource | None = None
        """The run's parts, footprints, and tapes: its revision's copy of them."""
        self._active = False
        self._step = False
        self._refit = False
        """A check found the board moved: find its fiducials again before going on."""
        self._picked: set[str] = set()
        self._missed: set[str] = set()
        """Slots whose placement was handed to another nozzle: its part is still in the
        pocket, so the next pick that looks first does not feed."""
        """Slots, strips, and trays picked from in this run, since the feeders were
        scanned."""
        self._board_check: BoardCheck | None = None
        self._recording: str | None = None
        self._record_wanted = False
        """The run being resumed was recorded before the core restarted: record it again."""
        """The flight recording this runner started for the run, if any."""
        self.record_runs = False
        """Record every run (from the config)."""
        self.sniff_board = False
        """Find the board's top by touch on a fiducial at the start of each run (from the
        config)."""
        self.check_placements = False
        """Look at each part with the top camera once placed (from the config)."""
        self.checks_dir: Path | None = None
        """Where pictures of the current run's checked placements are kept."""
        self.run_path: Path | None = None
        """Where the run is kept, to come back after the core restarts (``restore``)."""
        self._sim_error = sim_board_error if feeders.sim_parts is not None else None
        self.sim_decoys = False
        """In the simulator, print a round silkscreen mark smaller than each fiducial
        where the job expects the fiducial, nearer the camera's center than the fiducial
        itself (which is off by the board's error): a fiducial finder that takes the
        nearest round blob is fooled by it, as one was by a label's "D" on a real board."""
        self.sim_board_bump: tuple[int, float, float] | None = None
        """In the simulator, once this many parts are placed in a run, the board slides by
        (X, Y) mm with the parts on it, as one knocked by the parts pressed onto it did."""
        self._sim_bumped = False
        self._sim_board: BoardScene | None = None
        self._sim_fix: BoardFix | None = None
        if self._sim_error is not None:
            machine.scene_boards = self._scene_boards

    def state(self) -> RunState | None:
        run = self._run
        if run is not None and not self._active:
            self._forget_removed(run)
        return run

    def restore(self) -> None:
        """Read back the run kept in ``run_path``, if any. One the core stopped in the
        middle of comes back paused and interrupted: the machine must be homed again, its
        nozzles may still hold parts, and resuming finds the board again first."""
        path = self.run_path
        if path is None or not path.is_file():
            return
        try:
            run = RunState.model_validate_json(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("could not read the last run from %s: %s", path, e)
            return
        if run.status in ("running", "paused"):
            items = run.plan.items
            notes: list[str] = []
            held = [
                f"{items[i].ref} on {n}" for n, i in sorted(run.held.items()) if 0 <= i < len(items)
            ]
            if held:
                notes.append(f"the nozzles held parts ({', '.join(held)})")
            step = run.current
            if (
                step is not None
                and step.action == "placing"
                and step.index is not None
                and 0 <= step.index < len(items)
                and items[step.index].status == "pending"
            ):
                notes.append(
                    f"it stopped while placing {items[step.index].ref}: if that part is on "
                    "the board, skip its placement"
                )
            run.status = "paused"
            run.interrupted = True
            # The time since it was last kept on disk is not known: not counted as working.
            run.working_since = None
            run.fix = None
            run.board_z = None
            run.located = []
            run.held = {}
            run.current = None
            self._record_wanted = run.recording is not None
            run.recording = None
            run.pause_requested = run.stop_requested = False
            run.message = f"interrupted: {self._summary(run)}"
            run.error = (
                "the core stopped in the middle of this run"
                + (f"; {'; '.join(notes)}" if notes else "")
                + ". Take any parts off the nozzles by hand, home the machine, then resume: "
                "the run finds the board's fiducials again and places what is left."
            )
        self._run = run

    def _count_working(self, run: RunState) -> None:
        """Add the time worked since ``working_since`` to the run's stats: up to now, and
        on from now while the run's task goes on."""
        if run.working_since is None:
            return
        now = datetime.now(UTC)
        run.stats.working_s += max(0.0, (now - run.working_since).total_seconds())
        run.working_since = now if self._active else None

    def _save(self) -> None:
        """Keep the run on disk, to come back after a restart."""
        if self._run is not None:
            self._count_working(self._run)
        if self.run_path is None or self._run is None:
            return
        try:
            write_atomic(self.run_path, self._run.model_dump_json())
        except OSError as e:
            log.warning("could not keep the run on disk: %s", e)

    def _forget_removed(self, run: RunState) -> None:
        """Forget the parts taken off their nozzles (discarded, or by hand) while paused:
        their placements are picked again."""
        for nozzle_id in list(run.held):
            if self._placement.holds(nozzle_id) is None:
                del run.held[nozzle_id]

    def _resolve(
        self, job_id: str, revision: int | None = None
    ) -> tuple[Job, PartsSource | None, int | None]:
        """What runs for a job (``JobStore.resolve``), with the revision's copy of the
        library; a job not yet published comes with the library, to plan and check but
        not to run."""
        try:
            job, published = self._jobs.resolve(job_id, revision)
        except (LookupError, ValueError, JobFileError) as e:
            raise InvalidTargetError(str(e)) from e
        if published is None:
            return job, self._library(), None
        return job, published.design.library, published.revision.number

    def _plan(self, job_id: str, revision: int | None) -> tuple[Job, PartsSource | None, JobPlan]:
        job, parts, number = self._resolve(job_id, revision)
        plan = plan_job(
            job_id,
            job,
            self._machine.profile,
            self._feeders.state(),
            parts,
            number,
            sniff_heights=self.sniff_board,
            barred=self._placement.tips.barred,
        )
        aligns = any(i.status == "pending" for i in plan.items)
        for severity, message in run_issues(current(self._machine, self._feeders), aligns):
            plan.issues.append(Issue(severity=severity, message=message))
        if number is None:
            plan.issues.insert(
                0,
                Issue(
                    severity="error",
                    message="the job is not published yet: publish it in Prepare to run it",
                ),
            )
        return job, parts, plan

    def plan(self, job_id: str, revision: int | None = None) -> JobPlan:
        return self._plan(job_id, revision)[2]

    # --- Control -----------------------------------------------------------------------

    def start(
        self,
        job_id: str,
        skip_blocked: bool,
        pick_correction: PickCorrection | None = None,
        record: bool = False,
        step: bool = False,
        revision: int | None = None,
        check_placements: bool | None = None,
    ) -> Operation:
        """Start a run. With ``step``, it pauses once the board is found, before the first
        part, and each step then places one batch. ``check_placements``: look at each part
        once placed (default: the config's)."""
        if self._active:
            raise MachineBusyError("a job is running: pause or stop it first")
        job, parts, plan = self._plan(job_id, revision)
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
        self._parts = parts
        self._run = RunState(
            job_id=job_id,
            name=job.name,
            status="running",
            plan=plan,
            fix=None,
            pick_correction=pick_correction or self._placement.pick_correction,
            check_placements=self.check_placements
            if check_placements is None
            else check_placements,
            started_at=datetime.now(UTC),
        )
        self._run.pause_requested = step
        self._step = step
        self._refit = False
        self._sim_bumped = False
        if self.checks_dir is not None and self.checks_dir.is_dir():
            for old in self.checks_dir.glob("*.jpg"):
                old.unlink(missing_ok=True)
        self._picked = set()
        self._missed = set()
        if self._sim_error is not None:
            self._load_sim_board(job, parts)
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
        self._save()
        return self._start_task()

    def check_board(self, job_id: str, revision: int | None = None) -> Operation:
        """Find a job's board by its fiducials and show how it sits, placing nothing.
        Works whatever the job's placements are, even with none to place."""
        if self._active:
            raise MachineBusyError("a job is running: pause or stop it first")
        if self._run is not None and self._run.status == "paused":
            raise RunError("a run is paused: resume or stop it before checking a board")
        job, parts, _ = self._resolve(job_id, revision)
        if job.location is None:
            raise RunError("the job has no location on the machine: place it first")
        if not self._chosen_fiducials(job):
            raise RunError("the job has no fiducials turned on to check")
        if self._sim_error is not None:
            self._load_sim_board(job, parts)

        async def work() -> str:
            now = datetime.now(UTC)
            try:
                fix = await self._locate_board(job, parts)
            except MachineError as e:
                self._board_check = BoardCheck(
                    job_id=job_id, checked_at=now, ok=False, message=str(e)
                )
                raise
            ok = fix.residual <= FIDUCIAL_RESIDUAL_MM
            message = self._describe_fix(fix) if ok else self._misfit(fix.residual)
            self._board_check = BoardCheck(
                job_id=job_id, checked_at=now, fix=fix, ok=ok, message=message
            )
            if not ok:
                raise RunError(message)
            return message

        return self._machine.start_task("board_check", work)

    def board_check(self) -> BoardCheck | None:
        """The last board check, if any."""
        return self._board_check

    def resume(self, step: bool = False) -> Operation:
        """Carry on with a paused run; with ``step``, for one batch only."""
        run = self._need_run()
        if run.status != "paused":
            raise RunError(f"the run is {run.status}, not paused")
        if self._job is None:
            # Read back after a restart: the job comes from its revision again.
            try:
                job, parts, _ = self._resolve(run.job_id, run.plan.revision)
            except InvalidTargetError as e:
                raise RunError(f"the run's job cannot be read: {e}") from e
            self._job, self._parts = job, parts
            if self._sim_error is not None:
                self._load_sim_board(job, parts)
        self._unblock(run)
        # The tips fitted may have changed while paused (or a run read back after a
        # restart was planned before the tips counted): each pending part goes to the
        # nozzles whose tips suit it.
        for item in run.plan.items:
            if item.status == "pending" and item.part_id:
                part = self._parts.find_part(item.part_id) if self._parts else None
                item.nozzles = tip_nozzles(
                    item.nozzles,
                    part,
                    self._parts,
                    self._machine.profile,
                    self._placement.tips.barred,
                )
        error = run.error
        run.status = "running"
        run.error = None
        run.pause_requested = False
        self._step = step
        try:
            op = self._start_task()
        except MachineError:
            run.error = error
            raise
        run.interrupted = False
        self._record_again(run)
        self._save()
        return op

    def _unblock(self, run: RunState) -> int:
        """Plan the run's blocked placements again, as the machine is now: a feeder, strip,
        or tray loaded while paused (or a part added to the revision's library) lets them be
        placed. How many were unblocked."""
        blocked = [i for i in run.plan.items if i.status == "blocked"]
        if not blocked:
            return 0
        try:
            fresh = self._plan(run.job_id, run.plan.revision)[2]
        except (InvalidTargetError, MachineError) as e:
            log.warning("could not plan the blocked placements again: %s", e)
            return 0
        before = blocked_issues(run.plan.items)
        now = {(i.ref, i.copy_id): i for i in fresh.items}
        unblocked: list[str] = []
        for item in blocked:
            again = now.get((item.ref, item.copy_id))
            if again is None:
                continue
            if again.status == "pending":
                item.slot_id, item.nozzles = again.slot_id, again.nozzles
                item.status, item.note = "pending", ""
                unblocked.append(item.ref)
            elif again.status == "blocked":
                item.note = again.note
        if unblocked:
            # Where each part is now loaded; a part is a problem while any of it is blocked.
            fresh_parts = {n.part_id: n for n in fresh.parts}
            for need in run.plan.parts:
                if need.part_id in fresh_parts:
                    need.slot_ids = fresh_parts[need.part_id].slot_ids
                need.problem = next(
                    (
                        i.note
                        for i in run.plan.items
                        if i.part_id == need.part_id and i.status == "blocked"
                    ),
                    None,
                )
            run.plan.parts.sort(key=lambda n: (n.problem is None, n.part_id or ""))
            run.plan.issues = [i for i in run.plan.issues if i not in before] + blocked_issues(
                run.plan.items
            )
            self._machine.recorder.event("run.unblocked", refs=unblocked)
            log.info("resuming: %d blocked placements can now be placed", len(unblocked))
        return len(unblocked)

    def _record_again(self, run: RunState) -> None:
        """On resuming a run that is not being recorded (read back after a restart, or its
        recording stopped while paused), start one if the config records every run or the
        run was recorded before: a run's later part is kept as well as its start."""
        recorder = self._machine.recorder
        active = recorder.active
        if active is not None and active.id == self._recording:
            return
        if not (self.record_runs or self._record_wanted):
            return
        if not recorder.available or active is not None:
            return
        name = self._job.name if self._job is not None else run.name
        info = recorder.start(f"run {name} (resumed)", "run", run.job_id)
        run.recording = self._recording = info.id
        self._record_wanted = False
        recorder.event("run.resume", run=run)

    def pause(self) -> RunState:
        run = self._need_run()
        if run.status == "running":
            run.pause_requested = True
            run.message = "pausing after the parts on the nozzles are placed"
            self._save()
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
            if run.current is not None and run.current.index == index:
                run.current = None
        elif not skip and (
            item.status == "failed"
            or (
                item.status == "skipped"
                and (item.note == operator or item.note.startswith(UNTRAINABLE))
            )
        ):
            item.status, item.note, item.discards = "pending", "", 0
        self._save()
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
        run.working_since = datetime.now(UTC)
        return op

    def _finish(self, run: RunState, status: RunStatus, message: str) -> None:
        run.status = status
        run.message = message
        run.current = None
        run.pause_requested = run.stop_requested = False
        run.finished_at = datetime.now(UTC)
        recorder = self._machine.recorder
        recorder.event("run.end", run=run)
        active = recorder.active
        if self._recording is not None and active is not None and active.id == self._recording:
            recorder.stop(message)
        self._recording = None
        self._save()

    # --- The run itself ----------------------------------------------------------------

    async def _work(self) -> str:
        run = self._need_run()
        try:
            found: str | None = None
            if run.fix is None:
                if self._feeders.has_bus:
                    await self._rescan(run)
                run.message = "finding the board's fiducials"
                run.current = RunStep(stage="board", action="finding_fiducials")
                run.fix = await self._find_board(run)
                found = run.message
                run.current = RunStep(stage="board", action="measuring_board")
                run.board_z = await self._measure_board(run, run.fix)
                if run.board_z is not None:
                    found = f"{found}; {run.message}"
                run.current = None
            elif self._refit:
                # The board moved since its fiducials were found. Its top is where it was,
                # and a nozzle may hold a part, so its height is not touched again.
                run.message = "finding the board's fiducials again: it moved"
                run.current = RunStep(stage="board", action="finding_fiducials")
                run.fix = await self._find_board(run)
                self._refit = False
                run.current = None
            while True:
                if run.stop_requested:
                    await self._park()
                    self._finish(run, "stopped", f"stopped: {self._summary(run)}")
                    return run.message or ""
                if run.pause_requested:
                    run.status, run.pause_requested = "paused", False
                    run.current = None
                    if found is not None:
                        # Started in step mode: the board is found, nothing placed yet.
                        run.message = (
                            f"paused before the first part: {found}. Step places the first "
                            "parts, one on each nozzle; Resume places the rest."
                        )
                    else:
                        run.message = f"paused: {self._summary(run)}"
                        if not self._step:
                            # The Pause button: out of the operator's way. A step's pause
                            # leaves the head over the parts just placed.
                            run.message += await self._park()
                    self._save()
                    return run.message
                found = None
                batch = self._next_batch(run)
                if not batch:
                    # Done: the head out of the way, for the board to come off.
                    await self._park()
                    self._finish(run, "finished", f"finished: {self._summary(run)}")
                    return run.message or ""
                await self._do_batch(run, batch)
                run.current = None
                if self._step:
                    run.pause_requested = True
                self._save()
        except MachineError as e:
            run.status, run.error = "paused", str(e)
            run.pause_requested = False
            self._machine.recorder.event("run.paused", error=str(e), code=e.code)
            if isinstance(e, (FeederEmptyError, RunError)):
                # A pause for the operator to act: out of their way. Other errors (the
                # machine itself failing) leave the head where it is.
                run.error += await self._park()
            if run.stop_requested:
                self._finish(run, "stopped", f"stopped: {self._summary(run)}")
            else:
                self._save()
            if isinstance(e, FeederEmptyError):
                raise FeederEmptyError(run.error) from e
            if isinstance(e, RunError):
                raise RunError(run.error) from e
            raise
        except asyncio.CancelledError:
            run.status, run.error = "paused", "cancelled"
            raise
        except Exception as e:
            # A fault in the core itself: paused, not left marked running with nothing
            # running it, so the operator can resume, skip, or stop.
            log.exception("the run failed")
            run.status, run.pause_requested = "paused", False
            step = run.current
            ref = (
                run.plan.items[step.index].ref
                if step is not None and step.index is not None
                else None
            )
            run.error = (
                f"something went wrong in the core{f' at {ref}' if ref else ''} "
                f"({type(e).__name__}: {e}). The run is paused: resume to try again, or skip "
                "the placement. Please send a flight recording."
            )
            self._machine.recorder.event("run.paused", error=run.error, code="internal")
            if run.stop_requested:
                self._finish(run, "stopped", f"stopped: {self._summary(run)}")
            else:
                self._save()
            raise
        finally:
            self._active = False
            self._save()  # the time worked, counted up to here

    def _summary(self, run: RunState) -> str:
        counts: dict[str, int] = {}
        for item in run.plan.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        order = ("placed", "pending", "failed", "blocked", "skipped")
        names = {"pending": "to go"}
        text = ", ".join(f"{counts[k]} {names.get(k, k)}" for k in order if counts.get(k))
        st = run.stats
        trouble: list[str] = []
        if st.empty_picks:
            trouble.append(f"{st.empty_picks} of {st.picks} picks came up empty")
        if st.failed_alignments:
            trouble.append(
                f"the bottom camera could not find {st.failed_alignments} of {st.alignments} parts"
            )
        if st.bad_placements:
            trouble.append(f"{st.bad_placements} of {st.checked} checked placements were off")
        if st.unsure:
            trouble.append(f"{st.unsure} placements could not be made out to check")
        return text + (f" ({'; '.join(trouble)})" if trouble else "")

    async def _rescan(self, run: RunState) -> None:
        """Scan the feeders, and take each placement still to do from wherever its part is
        now. Pauses the run if some part is no longer loaded."""
        job = self._job
        assert job is not None
        run.message = "scanning the feeders"
        run.current = RunStep(stage="scan", action="scanning")
        await self._feeders.scan()
        run.located = []
        self._picked = set()
        plan = plan_job(
            run.job_id,
            job,
            self._machine.profile,
            self._feeders.state(),
            self._parts,
            sniff_heights=self.sniff_board,
            barred=self._placement.tips.barred,
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

    async def _locate_feeder(self, run: RunState, slot_id: str, index: int) -> None:
        """Before the first pick from a feeder slot in this run, find its feeder's
        fiducial."""
        feeders = self._feeders
        if run.pick_correction == "off" or slot_id in run.located or feeders.is_fixed(slot_id):
            return
        slot = feeders.slot(slot_id)
        if feeders.fiducial_offset(feeders.bank(slot))[0] is None:
            return  # feeders without a known fiducial: picks go where the setup says
        run.message = f"finding the feeder fiducial at {slot.label.lower()}"
        run.current = RunStep(stage="item", action="finding_feeder", index=index)
        moved = await feeders.locate_feeder(slot)
        run.located.append(slot_id)
        self._machine.recorder.event("run.feeder", slot=slot_id, moved=round(moved, 4))

    async def _find_board(self, run: RunState) -> BoardFix:
        job = self._job
        assert job is not None
        fix = await self._locate_board(job, self._parts)
        if fix.residual > FIDUCIAL_RESIDUAL_MM:
            raise RunError(self._misfit(fix.residual))
        run.message = (
            self._describe_fix(fix)
            if fix.fiducials
            else "the job has no fiducials turned on: the board is taken to be where the job says"
        )
        log.info("%s", run.message)
        return fix

    async def _measure_board(self, run: RunState, fix: BoardFix) -> float | None:
        """Sniff the board's top on its first fiducial (flat, bare copper) with a nozzle
        that reaches it. None if sniffing is off, or there is no fiducial or no height to
        start from."""
        job = self._job
        assert job is not None
        profile = self._machine.profile
        expected = job.board_z if job.board_z is not None else profile.board_height
        if not self.sniff_board or not fix.fiducials or expected is None:
            return None
        at = fix.fiducials[0].found
        reach = profile.nozzle_reach
        nozzle = next(
            (
                n
                for n in profile.nozzles
                if n.vacuum_input is not None and n.id in reach and _inside(reach[n.id], at.x, at.y)
            ),
            None,
        )
        if nozzle is None:
            return None
        ref = fix.fiducials[0].ref
        run.message = f"{nozzle.id} finding the board's top on fiducial {ref} by touch"
        source = "the job's" if job.board_z is not None else "the profile's"
        z = await sniff(
            self._machine,
            nozzle,
            at,
            expected,
            BOARD_SNIFF_ABOVE_MM,
            BOARD_SNIFF_BELOW_MM,
            f"fiducial {ref}",
        )
        if z is None:
            raise RunError(
                f"{floor_message(nozzle, f'fiducial {ref}', expected, BOARD_SNIFF_BELOW_MM)}: "
                "check the board is in place and flat, and the nozzle's tip seals. To place "
                f"at {source} board height instead, turn off sniff_heights in the config"
            )
        run.message = f"board's top at Z {z:.2f}, {z - expected:+.2f} mm from {source} height"
        self._machine.recorder.event("run.board_z", z=z, expected=expected, nozzle=nozzle.id)
        log.info("%s", run.message)
        return z

    def _chosen_fiducials(self, job: Job) -> list[MachinePlacement]:
        result = layout(job, self._machine.profile)
        return choose_fiducials(
            [p for p in result.placements if p.kind == "fiducial" and p.enabled]
        )

    @staticmethod
    def _misfit(residual: float) -> str:
        return (
            f"the fiducials are up to {residual:.2f} mm from where the job's layout puts "
            "them relative to each other: check that the right board is loaded, the "
            "side facing up, and the fiducials in the design"
        )

    @staticmethod
    def _describe_fix(fix: BoardFix) -> str:
        return (
            f"board found {math.hypot(fix.dx, fix.dy):.2f} mm from its location in the job, "
            f"turned {fix.angle:+.2f} degrees ({len(fix.fiducials)} fiducials, fit to "
            f"{fix.residual:.2f} mm)"
        )

    async def _locate_board(self, job: Job, library: PartsSource | None) -> BoardFix:
        """Find the job's chosen fiducials and fit the board to them."""
        assert job.location is not None
        chosen = self._chosen_fiducials(job)
        about = Point(x=job.location.x, y=job.location.y)
        if not chosen:
            return BoardFix(dx=0, dy=0, angle=0, about=about, residual=0, fiducials=[])
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
        fill_batch(pending, nozzles, batch)
        # Kept in the order taken, for the Studio's list (``JobPlan.order``).
        for index in batch.values():
            if index not in run.plan.taken:
                run.plan.taken.append(index)
        return batch

    async def _do_batch(self, run: RunState, batch: dict[str, int]) -> None:
        job = self._job
        assert job is not None and run.fix is not None
        items = run.plan.items
        # A feeder problem on one nozzle's pick (nothing picked, a pocket or a strip it
        # cannot use): the other nozzles still pick, and every part held is placed before
        # the run pauses for it, so none waits on a nozzle while the feeder is seen to.
        failed: MachineError | None = None
        failed_step: RunStep | None = None
        for nozzle_id, index in list(batch.items()):
            if nozzle_id in run.held:
                continue
            item = items[index]
            nozzle = self._machine.nozzle(nozzle_id)
            assert item.slot_id is not None
            try:
                await self._pick_item(run, item, index, nozzle_id)
            except _HandedOver:
                # Picked again by another nozzle in a later batch; nothing for the operator.
                del batch[nozzle_id]
                continue
            except (RunError, FeederError) as e:
                failed, failed_step = e, run.current
                del batch[nozzle_id]
                continue
            run.held[nozzle_id] = index
            item.note = ""
        if failed is not None and not run.held:
            raise failed

        for nozzle_id in list(batch):
            index = batch[nozzle_id]
            item = items[index]
            load = self._placement.holds(nozzle_id)
            if load is not None and load.alignment is not None:
                continue
            nozzle = self._machine.nozzle(nozzle_id)
            run.message = f"{nozzle_id} aligning {item.ref}"
            run.current = RunStep(stage="item", action="aligning", index=index, nozzle=nozzle_id)
            run.stats.alignments += 1
            try:
                # Turned to its place angle first, as it will be placed.
                await self._placement.align(nozzle, item.rotation + run.fix.angle)
            except VisionError as e:
                run.stats.failed_alignments += 1
                if isinstance(e, UntrainableError):
                    self._skip_untrainable(run, item, index, e)
                if not self._placement.can_discard():
                    item.discards += 1
                    item.note = f"bottom vision failed ({e}): take the part off {nozzle_id}"
                    raise RunError(
                        f"{item.ref}: {e}, and there is no discard location to drop it in. "
                        f"Take whatever is on {nozzle_id} off it (it may be empty), "
                        "click Part removed, and resume to pick it again, or skip the "
                        "placement. Set a discard location (Calibration panel, Machine "
                        "workspace), and the run will do this itself."
                    ) from e
                await self._placement.discard(nozzle)
                del run.held[nozzle_id]
                del batch[nozzle_id]
                item.discards += 1
                if isinstance(e, UntrainableError):
                    item.status = "skipped"
                    item.note = f"{UNTRAINABLE} ({e}): train it (Placement panel), then un-skip"
                elif item.discards >= ALIGN_ATTEMPTS:
                    item.status = "failed"
                    item.note = f"bottom vision failed {item.discards} times: {e}"
                else:
                    item.note = f"bottom vision failed ({e}): part discarded, to try again"

        for nozzle_id, index in batch.items():
            item = items[index]
            nozzle = self._machine.nozzle(nozzle_id)
            target = run.fix.apply(item.x, item.y)
            run.message = f"{nozzle_id} placing {item.ref}"
            run.current = RunStep(stage="item", action="placing", index=index, nozzle=nozzle_id)
            board = run.board_z if run.board_z is not None else job.board_z
            await self._placement.place(nozzle, target, item.rotation + run.fix.angle, board)
            del run.held[nozzle_id]
            item.status, item.note, item.nozzle = "placed", "", nozzle_id
            run.stats.placed += 1
            self._machine.recorder.event("run.placed", ref=item.ref, index=index, at=target)
            if run.check_placements:
                run.message = f"checking {item.ref}"
                run.current = RunStep(
                    stage="item", action="checking", index=index, nozzle=nozzle_id
                )
                await self._check_placed(run, item, target, item.rotation + run.fix.angle)
            self._bump_sim_board(run)
        if failed is not None:
            # The parts held are placed: now the pick that failed, where it stopped.
            run.current = failed_step
            raise failed

    def _skip_untrainable(
        self, run: RunState, item: RunItem, index: int, error: UntrainableError
    ) -> None:
        """Skip the other placements of a part training could not see, not held: picking
        each would cost a part."""
        for i, other in enumerate(run.plan.items):
            if (
                i != index
                and other.part_id == item.part_id
                and other.status == "pending"
                and i not in run.held.values()
            ):
                other.status = "skipped"
                other.note = (
                    f"{UNTRAINABLE} (at {item.ref}): train it (Placement panel), then un-skip"
                )
        self._machine.recorder.event("run.untrainable", part=item.part_id, ref=item.ref)
        log.warning("%s: %s; its placements are skipped", item.part_id, error)

    async def _pick_item(self, run: RunState, item: RunItem, index: int, nozzle_id: str) -> None:
        """Pick ``item``'s part with ``nozzle_id``, trying again (feeding the next part)
        after an empty pick, up to ``PICK_ATTEMPTS`` times."""
        assert item.slot_id is not None
        nozzle = self._machine.nozzle(nozzle_id)
        await self._locate_feeder(run, item.slot_id, index)
        run.message = f"{nozzle_id} picking {item.ref} from {_source(item.slot_id)}"
        run.current = RunStep(stage="item", action="picking", index=index, nozzle=nozzle_id)
        tips = self._placement.tips
        tip = self._tip_of(nozzle_id)
        for attempt in range(1, PICK_ATTEMPTS + 1):
            run.stats.picks += 1
            # After a miss, a pick that looks first does not feed: the camera sees whether
            # the part is still in its pocket (tried again) or gone (then it feeds), so a
            # part the nozzle failed to take is not fed past and lost.
            looks = run.pick_correction in ("top", "auto")
            missed = attempt > 1 or item.slot_id in self._missed
            try:
                await self._placement.pick(
                    item.slot_id,
                    nozzle,
                    not (missed and looks),
                    run.pick_correction,
                    first=item.slot_id not in self._picked,
                )
                self._picked.add(item.slot_id)
                self._missed.discard(item.slot_id)
                if tip and item.part_id:
                    tips.record(item.part_id, tip, True)
                return
            except MispickError as e:
                run.stats.empty_picks += 1
                if tip and item.part_id:
                    tips.record(item.part_id, tip, False)
                    others = [
                        n
                        for n in item.nozzles
                        if n != nozzle_id
                        and (t := self._tip_of(n)) is not None
                        and not tips.never_picked(item.part_id, t)
                    ]
                    if tips.never_picked(item.part_id, tip) and others:
                        # The tip, not the feeder: this tip has never picked the part. Its
                        # part is still in the pocket, for the next nozzle to look at first.
                        item.nozzles = others
                        self._missed.add(item.slot_id)
                        self._machine.recorder.event(
                            "run.tip_handover", ref=item.ref, tip=tip, nozzle=nozzle_id, to=others
                        )
                        log.info(
                            "%s: %s on %s has never picked %s; handed to %s",
                            item.ref,
                            tip,
                            nozzle_id,
                            item.part_id,
                            ", ".join(others),
                        )
                        raise _HandedOver from e
                if attempt == PICK_ATTEMPTS:
                    where = _source(item.slot_id)
                    item.note = f"picked nothing {PICK_ATTEMPTS} times from {where}"
                    raise RunError(
                        f"{item.ref}: {e} ({PICK_ATTEMPTS} tries). Check the feeder, "
                        "then resume to try again, or skip the placement."
                    ) from e

    def _tip_of(self, nozzle_id: str) -> str | None:
        """The tip fitted on a nozzle, if known."""
        nozzle = next((n for n in self._machine.profile.nozzles if n.id == nozzle_id), None)
        return nozzle.tip if nozzle is not None else None

    async def _check_placed(
        self, run: RunState, item: RunItem, target: Point, rotation: float
    ) -> None:
        """Look at a part just placed with the top camera, keep where it was found with the
        item, and pause the run if it is off its pads, turned, or not there
        (``CHECK_ON_PADS``, ``CHECK_TURN``, ``CHECK_SCORE``; ``CHECK_OFF_MM`` from where it
        was sent when the pads cannot be seen), or if its pads are not where the board's
        fit puts them (``CHECK_PADS_MOVED_MM``): the board has moved, and the run finds it
        again on resume. Only two-terminal chips with a body are looked for
        (``vision.placed``); others are left unchecked."""
        footprint = self._parts.find_footprint(item.footprint_id) if self._parts else None
        if footprint is None or len(footprint.pads) != 2 or footprint.body is None:
            return
        machine = self._machine
        camera = machine.top_camera()
        await machine.move_camera_to(target.x, target.y)
        frame = await machine.frame(camera.id)
        h, w = frame.image.shape[:2]
        mm_x, mm_y = camera.field_of_view.x / w, camera.field_of_view.y / h
        body = footprint.body
        length, width = max(body.x, body.y), min(body.x, body.y)
        # The chip's long axis in the machine, then as the picture shows it.
        along = rotation + (90.0 if body.y > body.x else 0.0)
        ax, ay = machine.image_to_machine(camera, 1.0, 0.0)
        bx, by = machine.image_to_machine(camera, 0.0, 1.0)
        det = ax * by - bx * ay
        a = math.radians(along)
        mx, my = math.cos(a), math.sin(a)
        right, down = (by * mx - bx * my) / det, (-ay * mx + ax * my) / det
        expected = image_turn(right, down)
        found: PlacedChip | None = await machine.recorder.vision(
            "find_placed_chip",
            frame.image,
            mm_x,
            mm_y,
            length,
            width,
            expected,
            camera=camera.id,
            position=dict(machine.driver.position),
            ref=item.ref,
        )
        run.stats.checked += 1
        good = found is not None and found.score >= CHECK_SCORE

        def to_image(x: float, y: float) -> tuple[float, float]:
            """Machine mm as mm right and down in the picture."""
            return (by * x - bx * y) / det, (-ay * x + ax * y) / det

        # The footprint's pads where the board's fit puts them, about the picture's middle.
        expected_pads: list[Polygon] = []
        for pad in footprint.pads:
            corners: list[tuple[float, float]] = []
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                corner = rotate(Point(x=sx * pad.width / 2, y=sy * pad.height / 2), pad.rotation)
                p = rotate(Point(x=pad.x + corner.x, y=pad.y + corner.y), rotation)
                corners.append(to_image(p.x, p.y))
            expected_pads.append(corners)
        pads: FoundPads | None = await machine.recorder.vision(
            "find_board_pads",
            frame.image,
            mm_x,
            mm_y,
            expected_pads,
            camera=camera.id,
            position=dict(machine.driver.position),
            ref=item.ref,
        )
        seen = pads is not None and pads.score >= CHECK_PADS_SCORE
        found_pads = (
            [[(x + pads.dx, y + pads.dy) for x, y in pad] for pad in expected_pads]
            if pads is not None and seen
            else None
        )
        picture = self._save_check(
            item.index,
            check_picture(
                frame.image,
                mm_x,
                mm_y,
                length,
                width,
                expected,
                found if good else None,
                found_pads,
            ),
        )
        if found is not None and not good and found.score >= CHECK_UNSURE:
            # Not made out well enough to judge: say so, and go on.
            run.stats.unsure += 1
            item.check = PlacementCheck(
                dx=0.0,
                dy=0.0,
                turn=0.0,
                score=round(found.score, 2),
                ok=True,
                checked=False,
                picture=picture,
            )
            machine.recorder.event("run.checked", ref=item.ref, check=item.check)
            return
        if found is None or not good:
            run.stats.bad_placements += 1
            item.check = PlacementCheck(
                dx=0.0,
                dy=0.0,
                turn=0.0,
                score=found.score if found else -1.0,
                ok=False,
                picture=picture,
            )
            raise RunError(
                f"{item.ref}: the top camera did not find it where it was placed: look at "
                "it (it may have moved, stood up, or come away with the nozzle), fix or "
                "remove it, and resume"
            )
        r, d = chip_offset(found, w, h, mm_x, mm_y)
        dx, dy = machine.image_to_machine(camera, r, d)
        f = math.radians(found.angle)
        fx, fy = machine.image_to_machine(camera, math.cos(f), -math.sin(f))
        turn = (math.degrees(math.atan2(fy, fx)) - along + 90.0) % 180.0 - 90.0
        turn = round(turn, 1) + 0.0  # never "-0.0"
        off = math.hypot(dx, dy)
        share: float | None = None
        moved: tuple[float, float] | None = None
        if pads is not None and found_pads is not None:
            share = on_pads(chip_ends(found, w, h, mm_x, mm_y, length, width), found_pads)
            moved = machine.image_to_machine(camera, pads.dx, pads.dy)
            ok = share >= CHECK_ON_PADS and abs(turn) <= CHECK_TURN
        else:
            ok = off <= CHECK_OFF_MM and abs(turn) <= CHECK_TURN
        item.check = PlacementCheck(
            dx=round(dx, 3),
            dy=round(dy, 3),
            turn=turn,
            score=round(found.score, 2),
            on_pads=round(share, 2) if share is not None else None,
            pads_dx=round(moved[0], 3) if moved is not None else None,
            pads_dy=round(moved[1], 3) if moved is not None else None,
            ok=ok,
            picture=picture,
        )
        machine.recorder.event("run.checked", ref=item.ref, check=item.check)
        if not ok:
            run.stats.bad_placements += 1
        wrong: list[str] = []
        if share is not None and share < CHECK_ON_PADS:
            wrong.append(f"with only {share:.0%} of an end on its pad")
        if abs(turn) > CHECK_TURN:
            wrong.append(f"turned {turn:+.1f} degrees")
        if moved is not None and math.hypot(*moved) > CHECK_PADS_MOVED_MM:
            # The next parts would land off their pads too: find the board again first.
            self._refit = True
            part = (
                f"{item.ref} itself is on its pads"
                if ok
                else f"{item.ref} landed {' and '.join(wrong)}: fix or remove it"
            )
            raise RunError(
                f"the board seems to have moved: {item.ref}'s pads are "
                f"{math.hypot(*moved):.2f} mm from where its fiducials put them (X "
                f"{moved[0]:+.2f}, Y {moved[1]:+.2f}). {part}. Make sure the board is held "
                "firmly, then resume: the run finds the fiducials again before going on"
            )
        if not ok and share is not None:
            raise RunError(
                f"{item.ref} landed {' and '.join(wrong)}: look at it (it may stand up when "
                "soldered), fix or remove it, and resume"
            )
        if not ok:
            raise RunError(
                f"{item.ref} landed {off:.2f} mm from where it was sent (X {dx:+.2f}, Y "
                f"{dy:+.2f}), turned {turn:+.1f} degrees: look at it (off its pads, it may "
                "stand up when soldered), fix or remove it, and resume"
            )

    def _save_check(self, index: int, picture: Image) -> bool:
        """Keep a checked placement's picture, for the operator; False if there is
        nowhere to keep it, or it could not be saved."""
        if self.checks_dir is None:
            return False
        try:
            self.checks_dir.mkdir(parents=True, exist_ok=True)
            ok, data = cv2.imencode(".jpg", picture, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                return False
            (self.checks_dir / f"{index}.jpg").write_bytes(data.tobytes())
        except OSError as e:
            log.warning("could not keep the picture of a checked placement: %s", e)
            return False
        return True

    def check_picture_path(self, index: int) -> Path | None:
        """Where the picture of the current run's checked placement ``index`` is, if
        kept."""
        run = self._run
        if self.checks_dir is None or run is None:
            return None
        items = run.plan.items
        if not 0 <= index < len(items) or items[index].check is None:
            return None
        path = self.checks_dir / f"{index}.jpg"
        return path if path.is_file() else None

    async def _park(self) -> str:
        """Move the head out of the way of an operator who must do something (refill a
        strip, fix a placement): to the park location, or else to the back of its travel.
        Says where it went, for the run's message; a move that fails is reported, not
        raised, so the pause stands."""
        try:
            where = await self._machine.park()
        except MachineError as e:
            log.warning("could not park the head: %s", e)
            return f" The head could not move out of the way ({e}): mind it."
        return f" The head moved to {where}."

    # --- Simulation --------------------------------------------------------------------

    def _load_sim_board(self, job: Job, library: PartsSource | None) -> None:
        """Put the job's board on the simulated bed, a little off from where the job
        says, so fiducial alignment has something to find."""
        assert self._sim_error is not None and job.location is not None
        dx, dy, angle = self._sim_error
        about = Point(x=job.location.x, y=job.location.y)
        true = BoardFix(dx=dx, dy=dy, angle=angle, about=about, residual=0, fiducials=[])
        self._sim_fix = true
        result = layout(job, self._machine.profile)

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

    def _bump_sim_board(self, run: RunState) -> None:
        """Slide the simulated board by ``sim_board_bump``, once its count of parts is
        placed: the parts on it go with it."""
        bump = self.sim_board_bump
        sim = self._feeders.sim_parts
        if bump is None or self._sim_bumped or self._sim_error is None or self._job is None:
            return
        count, bx, by = bump
        if run.stats.placed < count or sim is None:
            return
        self._sim_bumped = True
        dx, dy, angle = self._sim_error
        self._sim_error = (dx + bx, dy + by, angle)
        self._load_sim_board(self._job, self._parts)
        sim.placed[:] = [
            p if p.shape.disc is not None else replace(p, x=p.x + bx, y=p.y + by)
            for p in sim.placed
        ]

    def sim_target(self, item: RunItem) -> tuple[Point, float] | None:
        """Where the simulated board really wants an item's part, and at what angle."""
        if self._sim_fix is None:
            return None
        return self._sim_fix.apply(item.x, item.y), item.rotation + self._sim_fix.angle

    def _scene_boards(self) -> tuple[BoardScene, ...]:
        return (self._sim_board,) if self._sim_board is not None else ()
