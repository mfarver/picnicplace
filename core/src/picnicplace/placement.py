"""Picking, aligning, and placing one part at a time (spec section 7.3, milestone M3).

Each step is an operation, so a first run on a real machine can go one step at a time:

- **Pick**: feed the slot's tape, put the nozzle over the pocket, lower it to the top of
  the part, turn suction on, lift, and check the vacuum says a part is held. From a strip
  or tray on the bed (a fixed feeder, ID T1, T2, ...), take the next part in order
  instead of feeding. Where the pocket is comes from the pick correction chosen: 'top'
  finds it from the sprocket holes with the top camera; 'bottom' moves the pick by what
  the bottom camera saw of the feeder's earlier picks; 'off' trusts the feeder setup.
  'auto' picks with the bottom camera's correction, and looks with the top camera first
  on a feeder's first pick in a run, while nothing is learned of it, after an empty pick
  from it (the vacuum said no part was held) or a part the bottom camera could not
  find, and for ``AUTO_LOOKS`` picks after a part from it sat more than
  ``AUTO_MISS_MM`` off the nozzle's center (a feeder stepping unevenly, which learning
  cannot follow). Trays have no sprocket holes, so they always use the bottom camera.
  The first pick from each feeder, strip, or tray takes its part by touch: the nozzle
  steps down onto it with suction on until the vacuum seals, which marks the part's top.
  That height is kept with the feeder and part, and later picks go straight to it; an
  empty pick forgets it, so the next pick finds it again.
- **Align**: carry the part over the bottom camera with its underside in focus, find it,
  and record where it sits on the nozzle and at what angle. How far it sits off center
  says where the part really lay when it was picked: the feeder learns from that.
- **Place**: turn the nozzle so the part has the angle asked for, put the part's center
  (not the nozzle's) over the target, lower it to the board, let go, lift, and check
  the part left the nozzle.

Angles: a part's angle is the machine angle of its footprint's 0 degree direction. In a
tape it is the slot's tape direction plus the part's rotation in the tape (EIA-481). A
held part keeps its angle relative to the nozzle, so turning the nozzle turns the part.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from picnicplace.camera.source import Image
from picnicplace.feeders.service import FeederService, PocketCheck, rotate
from picnicplace.feeders.setup import PickCorrection, PickMethod
from picnicplace.library import Footprint, Library, Part, Size
from picnicplace.machine.errors import (
    FeederError,
    InvalidTargetError,
    MachineError,
    VisionError,
)
from picnicplace.machine.profile import Camera, LightColor, Location, Nozzle, Point
from picnicplace.machine.service import MachineService
from picnicplace.machine.sniff import SniffError, floor_message, sniff
from picnicplace.recorder import VISION_FUNCTIONS
from picnicplace.schemas import Operation, WireModel
from picnicplace.training import (
    ANGLE_STEADY,
    FIT_UNIT,
    FRAMES_PER_LIGHT,
    KEPT_CANDIDATES,
    OFFSET_STEADY,
    THRESHOLDS,
    TURNS,
    Candidate,
    Method,
    PartVision,
    TrainedPart,
    TrainedPartStore,
    approval_images,
    describe_light,
    describe_threshold,
    lights_to_try,
    now,
    spread_degrees,
    spread_points,
)
from picnicplace.vision.pads import PadFit, PadRect
from picnicplace.vision.part import BRIGHT, PartBox, bright_level, nearest_angle, wrap

log = logging.getLogger(__name__)

PICK_DWELL_S = 0.2
"""Wait this long with the nozzle on the part and suction on, before lifting."""
PLACE_DWELL_S = 0.1
"""Wait this long after letting go, before lifting."""
VACUUM_TIMEOUT_S = 1.0
PART_SNIFF_ABOVE_MM = 1.5
"""Sniffing for a part's top starts this far above the setup's height for it..."""
PART_SNIFF_BELOW_MM = 0.5
"""...and stops this far below it, gently: further would press into the part."""
SEARCH_MARGIN_MM = 3.0
"""Bottom vision looks this far around the part's expected size."""
AUTO_MISS_MM = 0.3
"""Auto correction: a part picked with the bottom camera's correction that sits further
than this off the nozzle's center sends the next picks from its feeder to the top camera."""
AUTO_LOOKS = 3
"""How many picks then look with the top camera first, before trying the bottom camera's
correction again (the top camera's picks keep teaching it meanwhile)."""
SPECK_FRACTION = 0.25
"""Bottom vision leaves out bright specks smaller than this fraction of the part's
smallest pad, such as a wound inductor's wire ends."""
FIT_RESIDUAL_MM = 0.15
"""Pads fitted with more than this left over (root mean square) are reported."""
SIZE_TOLERANCE = 0.35
"""A part seen more than this fraction larger or smaller than its pads is reported."""
ELONGATED = 1.2
"""An outline longer than this times its width has two ways round, not four: its longer
side is told from its shorter."""
HALF_TURN_BETTER = 0.5
"""With as many pads found either way round, the pads fitted 180 degrees from the tape's
angle are taken only if they lie this much closer to their metal (a fraction of the
other way's residual)."""


class PlacementError(MachineError):
    code = "placement_failed"


_Vision = Callable[..., Awaitable[Any]]
"""Runs a vision function by name on a frame: through the recorder, or directly."""


@dataclass(frozen=True)
class _View:
    """What measuring a held part in a bottom camera frame needs (``_view``)."""

    camera: Camera
    footprint: Footprint | None
    pads: tuple[Point, Point] | None
    """The center and size of the footprint's pads, in its frame."""
    size: Point
    mm_per_px: float
    search: float
    """How far from the picture's middle the part is looked for, in pixels."""
    speck: float
    """Bright specks smaller than this (square pixels) are not metal."""
    mirror: float
    turn: float
    to_image: Callable[[float, float], tuple[float, float]]


@dataclass(frozen=True)
class Look:
    """A held part as measured in one frame."""

    center: Point
    """The part's origin, in machine coordinates."""
    seen: Point
    """Its pads' middle from the nozzle's axis, in machine directions: what the feeder
    learns from."""
    angle: float
    """The part's machine angle."""
    note: str
    method: Literal["pads", "outline"]
    flipped: bool
    fit: float | None
    """Pad fit residual, or outline size error (mm)."""
    complete: bool
    """Every pad found, or the outline matched the footprint."""


@dataclass(frozen=True)
class _Shot:
    """A frame taken in training: the middle of the picture, under one of the lights."""

    light: int
    image: Image
    nozzle_at: Point
    nozzle_angle: float
    expected: float


class MispickError(PlacementError):
    """The nozzle came up from a pocket without a part."""


class Alignment(WireModel):
    dx: float
    """The part's center from the nozzle's axis, in mm, with the nozzle at 0 degrees."""
    dy: float
    angle: float
    """The part's angle minus the nozzle's."""


class NozzleLoad(WireModel):
    nozzle: str
    part_id: str | None
    slot_id: str
    angle: float
    """The part's angle minus the nozzle's, as expected from the tape."""
    alignment: Alignment | None
    picked_at: Point | None = None
    """Where the nozzle's axis went down for the part."""
    base: Point | None = None
    """Where the feeder setup put the part, before any correction."""
    pick_angle: float | None = None
    """The nozzle's angle when it picked."""
    correction: PickMethod | None = None
    """How the pick was corrected."""


class PlacementState(WireModel):
    loads: list[NozzleLoad]
    """What each nozzle holds, as far as PicnicPlace knows."""
    pick_correction: PickCorrection
    """How picks are corrected unless a pick or run says otherwise, from the config."""


def _smallest_pad(part: Part | None, library: Library | None) -> float | None:
    """The area of a part's smallest pad, in square millimeters."""
    if part is None or library is None:
        return None
    footprint = library.find_footprint(part.footprint_id)
    if footprint is None or not footprint.pads:
        return None
    return min(p.width * p.height for p in footprint.pads)


def _footprint(part: Part | None, library: Library | None) -> Footprint | None:
    if part is None or library is None:
        return None
    return library.find_footprint(part.footprint_id)


def _image_pads(
    footprint: Footprint, to_image: Callable[[float, float], tuple[float, float]], px: float
) -> list[PadRect]:
    """A footprint's pads as the bottom camera sees them with the part at 0 degrees: in
    image pixels about the footprint's origin. ``to_image`` takes a machine X/Y
    displacement to image right/down; the camera may mirror it, but does not turn it."""
    pads: list[PadRect] = []
    for p in footprint.pads:
        u, v = to_image(p.x, p.y)
        width, height = p.width, p.height
        quarter = round(p.rotation / 90)
        if abs(p.rotation - quarter * 90) < 1e-6:
            if quarter % 2:
                width, height = height, width
        else:
            # Neither along nor across: the box around it.
            a = math.radians(p.rotation)
            c, s = abs(math.cos(a)), abs(math.sin(a))
            width, height = width * c + height * s, width * s + height * c
        pads.append(PadRect(u / px, v / px, width / px, height / px))
    return pads


def symmetric_half_turn(footprint: Footprint) -> bool:
    """Whether a footprint's pads look the same turned 180 degrees (a resistor's do; a
    SOT-23's or a diode's marked pin 1 do not)."""
    tol = 0.02
    pads = [(p.x, p.y, p.width, p.height) for p in footprint.pads]
    return all(
        any(
            abs(x + q[0]) < tol
            and abs(y + q[1]) < tol
            and abs(w - q[2]) < tol
            and abs(h - q[3]) < tol
            for q in pads
        )
        for x, y, w, h in pads
    )


def _elongated(size: Point) -> bool:
    """Whether an outline is long enough for its longer side to be told from its shorter."""
    return max(size.x, size.y) > ELONGATED * min(size.x, size.y)


def outline_matched(
    measured: tuple[float, float], body: Size | None, pads: Point | None
) -> tuple[Point | None, bool]:
    """Which outline the part's measures ``measured`` (mm, either way round) match: its
    footprint's body (and True), or its pads' extent (and False); None if neither."""
    seen = sorted(measured)
    for size, about_body in (
        (Point(x=body.x, y=body.y) if body is not None else None, True),
        (pads, False),
    ):
        if size is None:
            continue
        want = sorted((size.x, size.y))
        if all(abs(m - e) <= SIZE_TOLERANCE * e for m, e in zip(seen, want, strict=True)):
            return size, about_body
    return None, False


def _footprint_center(part: Part | None, library: Library | None) -> tuple[Point, Point] | None:
    """The center and size of a part's pads, in its footprint's frame."""
    if part is None or library is None:
        return None
    footprint = library.find_footprint(part.footprint_id)
    if footprint is None or not footprint.pads:
        return None
    xs = [p.x + s * p.width / 2 for p in footprint.pads for s in (-1, 1)]
    ys = [p.y + s * p.height / 2 for p in footprint.pads for s in (-1, 1)]
    return (
        Point(x=(min(xs) + max(xs)) / 2, y=(min(ys) + max(ys)) / 2),
        Point(x=max(xs) - min(xs), y=max(ys) - min(ys)),
    )


def _middle(image: Image, half: float) -> Image:
    """The middle of a frame, ``half`` pixels each way: what bottom vision looks at."""
    h, w = image.shape[:2]
    r = math.ceil(half)
    y0, x0 = max(0, h // 2 - r), max(0, w // 2 - r)
    return np.ascontiguousarray(image[y0 : h - y0, x0 : w - x0])


def _candidate(
    vision: PartVision, shots: list[_Shot], looks: list[Look | None], light_index: int
) -> Candidate:
    """A way of seeing a part, judged on the frames it was tried on: it must find the
    part in every one, the same way round, and is scored by how steady the part's angle
    and place on the nozzle came out, and how well it fit."""
    good = [(s, k) for s, k in zip(shots, looks, strict=True) if k is not None and k.complete]
    n = len(shots)
    flipped = any(k.flipped for _, k in good)
    base = Candidate(
        vision=vision, seen=len(good), frames=n, light_index=light_index, flipped=flipped
    )
    if not good or len(good) < n or len({k.flipped for _, k in good}) > 1:
        return base
    angles = [wrap(k.angle - s.nozzle_angle) for s, k in good]
    offsets = [
        rotate(Point(x=k.center.x - s.nozzle_at.x, y=k.center.y - s.nozzle_at.y), -s.nozzle_angle)
        for s, k in good
    ]
    angle_spread = spread_degrees(angles)
    offset_spread = spread_points([(o.x, o.y) for o in offsets])
    fit = max(k.fit or 0.0 for _, k in good)
    score = angle_spread / ANGLE_STEADY + offset_spread / OFFSET_STEADY + fit / FIT_UNIT
    return base.model_copy(
        update={
            "angle_spread": round(angle_spread, 3),
            "offset_spread": round(offset_spread, 4),
            "fit": round(fit, 4),
            "score": round(score, 3),
        }
    )


class PlacementService:
    def __init__(
        self,
        machine: MachineService,
        feeders: FeederService,
        library: Callable[[], Library | None],
        pick_correction: PickCorrection = "top",
        trained: TrainedPartStore | None = None,
    ) -> None:
        self._machine = machine
        self.trained = trained or TrainedPartStore(None)
        """How the bottom camera sees each trained part best, on this machine."""
        self._feeders = feeders
        self._library = library
        self.pick_correction: PickCorrection = pick_correction
        self.sniff_heights = False
        """Find each feeder's part top by touch on its first pick (from the config)."""
        self._loads: dict[str, NozzleLoad] = {}
        self._look: dict[str, tuple[int, str]] = {}
        """Auto correction: by slot or strip ID, how many more picks to look at with the
        top camera first, and why."""

    def state(self) -> PlacementState:
        return PlacementState(
            loads=[self._loads[k] for k in sorted(self._loads)],
            pick_correction=self.pick_correction,
        )

    # --- Operations ----------------------------------------------------------------------

    def _check_source(self, source_id: str, feed: bool) -> None:
        """Raise now, not mid-task, if parts cannot come from this slot or strip/tray."""
        if self._feeders.is_fixed(source_id):
            self._feeders.next_fixed(source_id)
            return
        slot = self._feeders.slot(source_id)
        if feed:
            self._feeders.check_can_feed(slot)

    def _source_part(self, source_id: str) -> tuple[Part | None, str]:
        if self._feeders.is_fixed(source_id):
            f = self._feeders.fixed(source_id)
            return self._feeders.fixed_part(f), self._feeders.fixed_label(source_id)
        slot = self._feeders.slot(source_id)
        return self._feeders.part(slot), slot.label

    def start_pick(
        self,
        slot_id: str,
        nozzle_id: str,
        feed: bool = True,
        correction: PickCorrection | None = None,
    ) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        self._check_empty(nozzle)
        self._check_source(slot_id, feed)
        how = correction or self.pick_correction
        return self._machine.start_task("pick", lambda: self.pick(slot_id, nozzle, feed, how))

    def start_align(self, nozzle_id: str) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        self._load(nozzle)
        self._bottom_camera()
        return self._machine.start_task("align", lambda: self.align(nozzle))

    def start_place(self, nozzle_id: str, x: float, y: float, rotation: float) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        load = self._load(nozzle)
        if load.alignment is None:
            raise PlacementError(f"align the part on {nozzle.id} before placing it")
        self._place_height(load)
        return self._machine.start_task(
            "place", lambda: self.place(nozzle, Point(x=x, y=y), rotation)
        )

    def start_pick_and_place(
        self,
        slot_id: str,
        nozzle_id: str,
        x: float,
        y: float,
        rotation: float,
        feed: bool = True,
        correction: PickCorrection | None = None,
    ) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        self._check_empty(nozzle)
        self._bottom_camera()
        part, label = self._source_part(slot_id)
        self._part_height(part, label)
        self._check_source(slot_id, feed)

        async def work() -> str:
            how = correction or self.pick_correction
            steps = [await self.pick(slot_id, nozzle, feed, how)]
            steps.append(await self.align(nozzle))
            steps.append(await self.place(nozzle, Point(x=x, y=y), rotation))
            return " ".join(steps)

        return self._machine.start_task("pick_and_place", work)

    def start_train(
        self, slot_id: str, nozzle_id: str, correction: PickCorrection | None = None
    ) -> Operation:
        """Pick a part, find how the bottom camera sees it best, keep that for the part,
        and drop the part in the discard."""
        nozzle = self._machine.nozzle(nozzle_id)
        self._check_empty(nozzle)
        self._bottom_camera()
        self._discard_location()
        part, label = self._source_part(slot_id)
        self._part_height(part, label)
        self._check_source(slot_id, True)
        how = correction or self.pick_correction
        return self._machine.start_task("train", lambda: self.train(slot_id, nozzle, how))

    def start_discard(self, nozzle_id: str) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        self._discard_location()
        return self._machine.start_task("discard", lambda: self.discard(nozzle))

    def start_release(self, nozzle_id: str) -> Operation:
        """The operator took the part off the nozzle by hand (or found it empty)."""
        nozzle = self._machine.nozzle(nozzle_id)
        self._load(nozzle)
        return self._machine.start_task("release", lambda: self.release(nozzle))

    async def release(self, nozzle: Nozzle) -> str:
        """Turn the nozzle's suction off and forget its part: taken off by hand."""
        await self._machine.suction(nozzle, False)
        self._loads.pop(nozzle.id, None)
        return f"{nozzle.id}: suction off, and its part taken as removed by hand."

    def can_discard(self) -> bool:
        return any(loc.kind == "discard" for loc in self._machine.profile.locations)

    def _discard_location(self) -> Location:
        bin_ = next((loc for loc in self._machine.profile.locations if loc.kind == "discard"), None)
        if bin_ is None:
            raise PlacementError(
                "no discard location: set one in the Calibration panel (Machine workspace)"
            )
        return bin_

    async def discard(self, nozzle: Nozzle) -> str:
        """Drop whatever ``nozzle`` holds in the discard bin."""
        bin_ = self._discard_location()
        await self._machine.move_nozzle_over(nozzle, bin_.position.x, bin_.position.y)
        await self._machine.suction(nozzle, False)
        await asyncio.sleep(PLACE_DWELL_S)
        self._loads.pop(nozzle.id, None)
        return f"{nozzle.id}: dropped its part in the {bin_.label.lower()}."

    # --- Steps ---------------------------------------------------------------------------

    async def pick(
        self,
        slot_id: str,
        nozzle: Nozzle,
        feed: bool,
        correction: PickCorrection,
        first: bool = False,
    ) -> str:
        """Pick from a feeder slot, or from a strip or tray on the bed by its ID.
        ``first``: the first pick from it in a run (auto correction looks first)."""
        if self._feeders.is_fixed(slot_id):
            return await self._pick_fixed(slot_id, nozzle, correction, first)
        slot = self._feeders.slot(slot_id)
        bank = self._feeders.bank(slot)
        if bank.pick_height is None:
            raise PlacementError(f"the machine profile gives no pick height for {slot.label}")
        part = self._feeders.part(slot)
        if feed:
            await self._feeders.feed(slot)
        base = self._feeders.pick(slot)
        how, why = self._method(slot.id, correction, first)
        at, found, fed = base, "", ""
        if how == "top":
            check = await self._feeders.locate_pocket(slot)
            self._check_loose(slot.label, check)
            if check.part is False:
                # An empty pocket: feed once and look again. A second one is the feeder's
                # trouble (an empty reel or leader, or a feeder not advancing), not a pick's.
                await self._feeders.feed(slot)
                check = await self._feeders.locate_pocket(slot)
                self._check_loose(slot.label, check)
                if check.part is False:
                    raise FeederError(
                        f"{slot.label}: the top camera saw the pick pocket empty, fed once, and "
                        "saw the next one empty too: the reel may be out of parts or still on "
                        "its empty leader, or the feeder may not be advancing. Check it, feed "
                        "it until a part is in the pick pocket, then try again"
                    )
                fed = " (fed once past an empty pocket)"
            at = check.at
            found = f" (pocket {check.along:+.2f} mm along, {check.across:+.2f} mm across)"
        elif how == "bottom":
            at, found = self._learned(slot.id, base)
        tape = self._feeders.tape_of(part)
        tape_angle = slot.rotation + (tape.rotation or 0.0 if tape else 0.0)
        top = await self._take_counted(slot.id, how, nozzle, at, bank.pick_height, slot.label)
        self._loads[nozzle.id] = self._picked(nozzle, part, slot.id, tape_angle, at, base, how)
        what = part.id if part else "a part"
        return f"{nozzle.id} picked {what} from {slot.label}{fed}{found}{top}{why}."

    async def _pick_fixed(
        self, fixed_id: str, nozzle: Nozzle, correction: PickCorrection, first: bool
    ) -> str:
        feeders = self._feeders
        f = feeders.fixed(fixed_id)
        label = feeders.fixed_label(fixed_id)
        part = feeders.fixed_part(f)
        height = feeders.fixed_pick_height(f, label)
        angle = feeders.fixed_angle(f)
        if angle is None:
            raise PlacementError(
                f"{label}: {f.part_id or 'its part'} has no tape rotation in the parts library"
            )
        index, base = feeders.next_fixed(fixed_id)
        how: PickMethod
        why = ""
        if correction == "off":
            how = "off"
        elif f.kind == "tray":
            how = "bottom"  # no sprocket holes: the bottom camera's learning is all there is
        else:
            how, why = self._method(fixed_id, correction, first)
        at, found = base, ""
        check = None
        if how == "top":
            check = await feeders.locate_strip_pocket(fixed_id)
            at = check.at
            found = f" (pocket {check.along:+.2f} mm along, {check.across:+.2f} mm across)"
        elif how == "bottom":
            at, found = self._learned(fixed_id, base)
        feeders.took_fixed(fixed_id)  # tried or taken, that pocket is done with
        if check is not None:
            self._check_tape(fixed_id, label, check)
        top = await self._take_counted(fixed_id, how, nozzle, at, height, label)
        self._loads[nozzle.id] = self._picked(nozzle, part, fixed_id, angle, at, base, how)
        what = part.id if part else "a part"
        return f"{nozzle.id} picked {what} number {index + 1} from {label}{found}{top}{why}."

    def _check_tape(self, source_id: str, label: str, check: PocketCheck) -> None:
        """Before going down on a strip on the bed: stop for a part lying loose on the tape,
        and take a pocket the top camera sees empty for an empty pick, without touching it
        (a run tries the strip's next pocket)."""
        self._check_loose(label, check)
        if check.part is False:
            self._look[source_id] = (1, "after an empty pocket")
            raise MispickError(f"{label}: the top camera sees the pick pocket empty")

    def _check_loose(self, label: str, check: PocketCheck) -> None:
        """Before going down: stop for a part lying loose on the tape."""
        if check.loose:
            where = ", ".join(f"({p.x:.1f}, {p.y:.1f})" for p in check.loose)
            what = "a part" if len(check.loose) == 1 else f"{len(check.loose)} parts"
            raise FeederError(
                f"{label}: the top camera sees {what} lying loose on the tape near the pick "
                f"pocket, out of the pockets, at {where}: a loose part could be picked upside "
                "down or knocked about. Take it off the tape, or feed the tape on until it is "
                "carried past, then try again"
            )

    def _method(
        self, source_id: str, correction: PickCorrection, first: bool
    ) -> tuple[PickMethod, str]:
        """How to correct this pick, and for auto correction, a note saying why."""
        if correction != "auto":
            return correction, ""
        if first:
            return "top", " (auto: top camera, the first pick from it in this run)"
        if self._feeders.learned(source_id) is None:
            return "top", " (auto: top camera, nothing learned of it yet)"
        looks, why = self._look.get(source_id, (0, ""))
        if looks > 0:
            return "top", f" (auto: top camera, {why})"
        return "bottom", ""

    async def _take_counted(
        self, source_id: str, how: PickMethod, nozzle: Nozzle, at: Point, height: float, label: str
    ) -> str:
        """Take the part, and keep count for auto correction: after an empty pick, look
        with the top camera next time; a part taken after a look uses one up. ``height``:
        the setup's or profile's height for the part's top, used until a sniff finds it.
        Returns a note for the pick's message when it sniffed."""
        top = self._feeders.part_top(source_id) if self.sniff_heights else None
        note = ""
        try:
            if self.sniff_heights and top is None:
                found = await self._sniff_take(nozzle, at, height, label)
                self._feeders.set_part_top(source_id, found, nozzle)
                note = (
                    f" (found the part's top at Z {found:.2f} by touch, "
                    f"{found - height:+.2f} mm from the setup's height)"
                )
            else:
                await self._take(nozzle, at, height if top is None else top, label)
        except MispickError:
            self._look[source_id] = (1, "after an empty pick")
            # Sniff again next time: the parts may sit higher or lower than thought.
            self._feeders.forget_part_top(source_id)
            raise
        if how == "top" and source_id in self._look:
            looks, why = self._look[source_id]
            if looks <= 1:
                del self._look[source_id]
            else:
                self._look[source_id] = (looks - 1, why)
        return note

    def _learned(self, source_id: str, base: Point) -> tuple[Point, str]:
        """Where to pick with the bottom camera's correction, and a note saying so."""
        offset = self._feeders.learned_offset(source_id)
        if offset.x == 0 and offset.y == 0:
            return base, ""
        at = Point(x=base.x + offset.x, y=base.y + offset.y)
        return at, f" (moved {offset.x:+.2f}, {offset.y:+.2f} mm by what the bottom camera saw)"

    def _picked(
        self,
        nozzle: Nozzle,
        part: Part | None,
        source_id: str,
        part_angle: float,
        at: Point,
        base: Point,
        correction: PickMethod,
    ) -> NozzleLoad:
        nozzle_angle = self._machine.nozzle_angle(nozzle)
        load = NozzleLoad(
            nozzle=nozzle.id,
            part_id=part.id if part else None,
            slot_id=source_id,
            angle=wrap(part_angle - nozzle_angle),
            alignment=None,
            picked_at=at,
            base=base,
            pick_angle=nozzle_angle,
            correction=correction,
        )
        self._machine.recorder.event("pick", load=load)
        return load

    def _axis_over(self, nozzle: Nozzle, at: Point) -> Point:
        """Where the nozzle's axis goes to put its tip's end over a point, as it is turned
        now: the end is off the axis by the tip's measured runout, turning with it."""
        end = rotate(nozzle.runout, self._machine.nozzle_angle(nozzle))
        return Point(x=at.x - end.x, y=at.y - end.y)

    async def _take(self, nozzle: Nozzle, at: Point, height: float, label: str) -> None:
        """Lower onto a part, take it with suction, lift, and check it came up."""
        axis = self._axis_over(nozzle, at)
        await self._machine.move_nozzle_over(nozzle, axis.x, axis.y)
        await self._machine.lower_nozzle(nozzle, height)
        await self._machine.suction(nozzle, True)
        await asyncio.sleep(PICK_DWELL_S)
        await self._machine.raise_nozzles()
        held, reading = await self._machine.holds_after_lift(nozzle, VACUUM_TIMEOUT_S)
        if held is False:
            await self._machine.suction(nozzle, False)
            raise MispickError(
                f"{nozzle.id} picked nothing from {label}: the vacuum reads "
                f"{reading:g}, not a part. Check the pocket and the pick height."
                if reading is not None
                else f"{nozzle.id} picked nothing from {label}"
            )

    async def _sniff_take(self, nozzle: Nozzle, at: Point, expected: float, label: str) -> float:
        """Take the part by touch: step down onto it with suction on until the vacuum
        seals, lift, and check it came up. Returns the height it sealed at, its top."""
        try:
            found = await sniff(
                self._machine,
                nozzle,
                self._axis_over(nozzle, at),
                expected,
                PART_SNIFF_ABOVE_MM,
                PART_SNIFF_BELOW_MM,
                f"the part at {label}",
                hold=True,
                dwell_s=PICK_DWELL_S,
            )
        except SniffError as e:
            raise PlacementError(str(e)) from e
        if found is None:
            raise MispickError(
                f"{floor_message(nozzle, f'a part at {label}', expected, PART_SNIFF_BELOW_MM)}: "
                "check there is a part in the pocket, and the pick height"
            )
        held, _ = await self._machine.holds_after_lift(nozzle, VACUUM_TIMEOUT_S)
        if held is False:
            await self._machine.suction(nozzle, False)
            raise MispickError(
                f"{nozzle.id} sealed at Z {found:.2f} over {label} but lifted nothing: check "
                "there is a part in the pocket"
            )
        return found

    def _view(
        self, camera: Camera, part: Part | None, library: Library | None, width: int
    ) -> "_View":
        """What measuring a held part in the bottom camera's frames needs: the part's
        footprint, sizes in pixels, and how the picture is flipped and turned."""
        mm_per_px = camera.field_of_view.x / width
        pads = _footprint_center(part, library)
        size = pads[1] if pads else Point(x=1.6, y=0.8)
        pad_area = _smallest_pad(part, library)
        # Machine X/Y to image right/down as the picture would be without its measured
        # turn: the inverse of image_to_machine, not turned, which flips and mirrors. The
        # pads are drawn so, and the turn is added to the angle they are looked for at.
        ux, uy = self._machine.image_to_machine(camera, 1.0, 0.0, turned=False)
        vx, vy = self._machine.image_to_machine(camera, 0.0, 1.0, turned=False)
        det = ux * vy - uy * vx

        def to_image(x: float, y: float) -> tuple[float, float]:
            return (vy * x - vx * y) / det, (-uy * x + ux * y) / det

        return _View(
            camera=camera,
            footprint=_footprint(part, library),
            pads=pads,
            size=size,
            mm_per_px=mm_per_px,
            search=(math.hypot(size.x, size.y) / 2 + SEARCH_MARGIN_MM) / mm_per_px,
            speck=SPECK_FRACTION * pad_area / mm_per_px**2 if pad_area else 0.0,
            # A mirror turns the other way: a part turned by a in the machine is turned by
            # -a in a mirrored image; the camera's turn adds to it.
            mirror=-1.0 if det < 0 else 1.0,
            turn=camera.image_rotation,
            to_image=to_image,
        )

    async def _measure(
        self,
        view: "_View",
        image: Image,
        nozzle_at: Point,
        expected: float,
        vision: "_Vision",
        threshold: int = BRIGHT,
        method: Method = "auto",
    ) -> "Look | None":
        """Where a held part sits and how it is turned, in one frame (``image``, centered
        on the nozzle at ``nozzle_at``, the part expected at ``expected`` degrees), with
        metal taken as brighter than ``threshold``; None if not found."""
        h, w = image.shape[:2]
        camera, footprint, mm_per_px = view.camera, view.footprint, view.mm_per_px
        # The part's outline first: whatever is bright, its body and its metal.
        box: PartBox | None = await vision("find_part", image, view.search, threshold, view.speck)
        fitted: PadFit | None = None
        flipped = False
        can_fit = footprint is not None and len(footprint.pads) >= 2
        if method != "outline" and footprint is not None and can_fit:
            image_pads = _image_pads(footprint, view.to_image, mm_per_px)
            # Then the pads, looked for from the outline's angle nearest the tape's, which
            # may be some degrees off it.
            guess = view.mirror * expected + view.turn
            start = nearest_angle(box.angle, guess) if box is not None else guess
            if box is not None or method == "pads":
                fitted = await vision("fit_pads", image, image_pads, start, view.search, threshold)
            if fitted is not None and not symmetric_half_turn(footprint):
                # The outline is the same turned 180 degrees; pads that are not (a
                # SOT-23's, a diode's) tell which way round the part is.
                other = await vision(
                    "fit_pads", image, image_pads, start + 180, view.search, threshold
                )
                if other is not None and (
                    other.found > fitted.found
                    or (
                        other.found == fitted.found
                        and other.residual < HALF_TURN_BETTER * fitted.residual
                    )
                ):
                    fitted, flipped = other, True
        if method == "pads" and fitted is None:
            return None
        if fitted is None and box is None:
            return None

        note = ""
        if fitted is not None:
            assert footprint is not None
            # The fit finds the footprint's origin itself, and its turn.
            dx, dy = self._machine.image_to_machine(
                camera, (fitted.u - w / 2) * mm_per_px, (fitted.v - h / 2) * mm_per_px
            )
            angle = view.mirror * (fitted.angle - view.turn)
            center = Point(x=nozzle_at.x + dx, y=nozzle_at.y + dy)
            # What the feeder learns from is where the pads' middle was seen.
            middle = rotate(view.pads[0], angle) if view.pads else Point(x=0, y=0)
            seen = Point(x=dx + middle.x, y=dy + middle.y)
            missing = len(footprint.pads) - fitted.found
            residual = fitted.residual * mm_per_px
            if missing:
                note = f" The camera saw {fitted.found} of its {len(footprint.pads)} pads."
            elif residual > FIT_RESIDUAL_MM:
                note = (
                    f" Its metal lies up to {residual:.2f} mm from its pads: check the part "
                    "and its footprint."
                )
            if flipped:
                note += (
                    " Its pads show it turned about 180 degrees from the tape's angle: check "
                    "its orientation in the tape."
                )
            return Look(center, seen, angle, note, "pads", flipped, residual, not missing)
        assert box is not None
        dx, dy = self._machine.image_to_machine(
            camera, (box.u - w / 2) * mm_per_px, (box.v - h / 2) * mm_per_px
        )
        seen = Point(x=dx, y=dy)
        measured = (box.width * mm_per_px, box.height * mm_per_px)
        # What the outline is round: the part's body (a white chip's, lit), or its
        # pads' metal (a black body unseen against the nozzle).
        body = footprint.body if footprint is not None else None
        size = view.size
        outline, about_body = outline_matched(measured, body, size if view.pads else None)
        # The outline's longer side, from image directions to machine ones, against the
        # footprint's: a long part has two ways round, a square one four.
        long_side = box.angle if box.width >= box.height else box.angle + 90
        r = math.radians(long_side)
        sx, sy = self._machine.image_to_machine(camera, math.cos(r), math.sin(r))
        along = 0.0 if outline is None or outline.x >= outline.y else 90.0
        period = 180.0 if outline is not None and _elongated(outline) else 90.0
        angle = nearest_angle(math.degrees(math.atan2(sy, sx)) - along, expected, period)
        center = Point(x=nozzle_at.x + dx, y=nozzle_at.y + dy)
        if view.pads is not None and not about_body:
            # Round the pads: the part's origin may not be their center.
            shift = rotate(view.pads[0], angle)
            center = Point(x=center.x - shift.x, y=center.y - shift.y)
        error: float | None = None
        if outline is not None:
            error = max(
                abs(m - e)
                for m, e in zip(sorted(measured), sorted((outline.x, outline.y)), strict=True)
            )
        if outline is None and view.pads is not None:
            found, want = sorted(measured), sorted((size.x, size.y))
            note = (
                f" Its outline measures {found[0]:.2f} x {found[1]:.2f} mm; its pads "
                f"are {want[0]:.2f} x {want[1]:.2f}: check the part."
            )
        elif footprint is not None and can_fit:
            # A white chip's ends are as bright as its body (a real 0603 resistor's,
            # under any light): its body's outline is the measure, not a fallback.
            what = "its body's" if about_body else "its pads'"
            note = f" Found by {what} outline"
            note += (
                "."
                if symmetric_half_turn(footprint)
                else ", so which way round it is was taken from the tape."
            )
        return Look(center, seen, angle, note, "outline", False, error, outline is not None)

    async def align(self, nozzle: Nozzle) -> str:
        load = self._load(nozzle)
        camera = self._bottom_camera()
        assert camera.focus_height is not None
        library = self._library()
        part = library.find_part(load.part_id) if library and load.part_id else None
        height = part.height if part is not None and part.height is not None else 0.0
        trained = self.trained.vision(load.part_id)

        await self._machine.move_nozzle_over(nozzle, camera.position.x, camera.position.y)
        await self._machine.lower_nozzle(nozzle, camera.focus_height + height)
        frame = await self._machine.frame(camera.id, light=trained.light if trained else None)
        view = self._view(camera, part, library, frame.image.shape[1])
        nozzle_at = self._machine.nozzle_position(nozzle)
        nozzle_angle = self._machine.nozzle_angle(nozzle)
        expected = nozzle_angle + load.angle
        context: dict[str, Any] = {
            "camera": camera.id,
            "position": dict(self._machine.driver.position),
            "mm_per_px": view.mm_per_px,
            "nozzle": nozzle.id,
            "part": load.part_id,
            "source": load.slot_id,
        }

        async def vision(name: str, image: Image, *args: Any) -> Any:
            return await self._machine.recorder.vision(name, image, *args, **context)

        threshold = BRIGHT
        if trained is not None and trained.threshold is not None:
            threshold = bright_level(frame.image, view.search, trained.threshold)
        method: Method = trained.method if trained is not None else "auto"
        look = await self._measure(
            view, frame.image, nozzle_at, expected, vision, threshold, method
        )
        await self._machine.raise_nozzles()
        if look is None:
            # Likely a bad pick (nothing there, or a part on its side), even when the
            # vacuum could not tell: auto correction looks first next time.
            self._look[load.slot_id] = (1, "after the bottom camera saw no part")
            how = " the way it was trained to (train the part again if this repeats)"
            raise VisionError(
                f"the {camera.label.lower()} saw no part on {nozzle.id}"
                + (how if trained is not None else "")
            )
        center, angle = look.center, look.angle
        offset = rotate(Point(x=center.x - nozzle_at.x, y=center.y - nozzle_at.y), -nozzle_angle)
        alignment = Alignment(
            dx=round(offset.x, 4), dy=round(offset.y, 4), angle=wrap(angle - nozzle_angle)
        )
        self._loads[nozzle.id] = load.model_copy(update={"alignment": alignment})
        self._machine.recorder.event("align", nozzle=nozzle.id, alignment=alignment)
        learned = self._learn(load, look.seen, nozzle_angle)
        return (
            f"{nozzle.id} holds its part {math.hypot(offset.x, offset.y):.2f} mm off center "
            f"and turned {wrap(angle - expected):+.1f} degrees from the tape's angle."
            f"{look.note}{' Seen as trained.' if trained is not None else ''}{learned}"
        )

    async def train(self, slot_id: str, nozzle: Nozzle, how: PickCorrection) -> str:
        """Pick a part from ``slot_id``, find how the bottom camera sees it best
        (``picnicplace.training``), take a picture of it so for the operator to approve,
        keep what was found (to be approved), and drop the part in the discard."""
        recorder = self._machine.recorder
        part_for, _ = self._source_part(slot_id)
        assert part_for is not None
        started = None
        if recorder.available and recorder.active is None:
            started = recorder.start(f"train {part_for.id}", "train")
        try:
            return await self._train(slot_id, nozzle, how, started.id if started else None)
        finally:
            if started is not None:
                recorder.stop("training ended")

    async def _train(
        self, slot_id: str, nozzle: Nozzle, how: PickCorrection, recording: str | None
    ) -> str:
        await self.pick(slot_id, nozzle, True, how)
        load = self._load(nozzle)
        camera = self._bottom_camera()
        assert camera.focus_height is not None
        library = self._library()
        part = library.find_part(load.part_id) if library and load.part_id else None
        assert part is not None and part.height is not None
        footprint = _footprint(part, library)
        output = next(
            (o for o in self._machine.profile.outputs if o.id == camera.light_output), None
        )
        lights: list[LightColor | None] = (
            list(lights_to_try(output.colored)) if output is not None else [None]
        )
        await self._machine.move_nozzle_over(nozzle, camera.position.x, camera.position.y)
        await self._machine.lower_nozzle(nozzle, camera.focus_height + part.height)
        base = self._machine.nozzle_angle(nozzle)
        shots: list[_Shot] = []
        view: _View | None = None
        try:
            for turn in TURNS:
                if turn:
                    axis = nozzle.rotation_axis
                    await self._machine.move_axes(
                        {axis: self._machine.nozzle_turn(nozzle, base + turn)}
                    )
                for i, light in enumerate(lights):
                    for k in range(FRAMES_PER_LIGHT):
                        frame = await self._machine.frame(camera.id, settle=k == 0, light=light)
                        if view is None:
                            view = self._view(camera, part, library, frame.image.shape[1])
                        crop = _middle(frame.image, view.search + 4)
                        angle = self._machine.nozzle_angle(nozzle)
                        shots.append(
                            _Shot(
                                i,
                                crop,
                                self._machine.nozzle_position(nozzle),
                                angle,
                                angle + load.angle,
                            )
                        )
                        if k == 0:
                            await self._machine.recorder.frame(
                                crop, camera.id, "train", light=light, turn=turn
                            )
                            if turn == 0:
                                self.trained.save_image(part.id, f"light-{i:02d}", crop)
            assert view is not None
            candidates = await self._judge(view, shots, lights, footprint)
            usable = [c for c in candidates if c.score is not None]
            if not usable:
                best_seen = max((c.seen for c in candidates), default=0)
                raise PlacementError(
                    f"training found no way of seeing {part.id} in every frame (the best "
                    f"found it in {best_seen} of {candidates[0].frames if candidates else 0}): "
                    "check the part is held flat and its footprint is right"
                )
            pool = usable
            if footprint is not None and not symmetric_half_turn(footprint):
                # An outline cannot say which way round such a part is: its pads can.
                pool = [c for c in usable if c.vision.method == "pads"] or usable
            best = min(pool, key=lambda c: c.score or 0.0)
            # The picture to approve: back where it was picked, under the light chosen.
            await self._machine.move_axes(
                {nozzle.rotation_axis: self._machine.nozzle_turn(nozzle, base)}
            )
            frame = await self._machine.frame(camera.id, light=best.vision.light)
            look = await self._final_look(view, frame.image, nozzle, load.angle, best.vision)
            if look is not None and footprint is not None:
                self._approval_images(part.id, view, frame.image, nozzle, look, footprint)
        finally:
            await self._machine.light_for(camera)
            await self._machine.raise_nozzles()
        discarded = await self.discard(nozzle)
        vision = best.vision
        how_seen = "by fitting its pads" if vision.method == "pads" else "by its outline"
        flip = (
            " Its pads show it turned about 180 degrees from the tape's angle: check its "
            "orientation in the tape."
            if best.flipped
            else ""
        )
        message = (
            f"Trained {part.id}: {describe_light(vision.light)}, {how_seen}, with "
            f"{describe_threshold(vision.threshold)}. Over {len(TURNS)} turns and "
            f"{best.frames} frames its angle on the nozzle held within "
            f"{best.angle_spread or 0.0:.2f} degrees and its place within "
            f"{best.offset_spread or 0.0:.3f} mm; {len(usable)} of {len(candidates)} ways of "
            f"seeing it found it every time.{flip} Approve it in the Studio, where its "
            f"footprint is drawn over it, before it is used. {discarded}"
        )
        self.trained.put(
            TrainedPart(
                part_id=part.id,
                vision=vision,
                trained_at=now(),
                nozzle=nozzle.id,
                tip=nozzle.tip,
                message=message,
                candidates=sorted(
                    candidates, key=lambda c: (c.score is None, c.score or 0.0, -c.seen)
                )[:KEPT_CANDIDATES],
                tried=len(candidates),
                recording=recording,
            )
        )
        return message

    async def _judge(
        self,
        view: "_View",
        shots: list["_Shot"],
        lights: list[LightColor | None],
        footprint: Footprint | None,
    ) -> list[Candidate]:
        """Every way of seeing the part tried on the frames, judged (``training``)."""

        async def direct(name: str, image: Image, *args: Any) -> Any:
            return await asyncio.to_thread(VISION_FUNCTIONS[name], image, *args)

        methods: list[Method] = ["outline"]
        if footprint is not None and len(footprint.pads) >= 2:
            methods.insert(0, "pads")
        out: list[Candidate] = []
        for i, light in enumerate(lights):
            mine = [s for s in shots if s.light == i]
            for threshold in THRESHOLDS:
                levels = [
                    BRIGHT if threshold is None else bright_level(s.image, view.search, threshold)
                    for s in mine
                ]
                for method in methods:
                    looks = [
                        await self._measure(
                            view, s.image, s.nozzle_at, s.expected, direct, level, method
                        )
                        for s, level in zip(mine, levels, strict=True)
                    ]
                    out.append(
                        _candidate(
                            PartVision(light=light, threshold=threshold, method=method),
                            mine,
                            looks,
                            i,
                        )
                    )
        return out

    async def _final_look(
        self, view: "_View", image: Image, nozzle: Nozzle, angle: float, vision: PartVision
    ) -> "Look | None":
        async def direct(name: str, img: Image, *args: Any) -> Any:
            return await asyncio.to_thread(VISION_FUNCTIONS[name], img, *args)

        threshold = BRIGHT
        if vision.threshold is not None:
            threshold = bright_level(image, view.search, vision.threshold)
        expected = self._machine.nozzle_angle(nozzle) + angle
        at = self._machine.nozzle_position(nozzle)
        return await self._measure(view, image, at, expected, direct, threshold, vision.method)

    def _approval_images(
        self,
        part_id: str,
        view: "_View",
        image: Image,
        nozzle: Nozzle,
        look: "Look",
        footprint: Footprint,
    ) -> None:
        """Save the part with its footprint drawn over it, and beside it."""
        h, w = image.shape[:2]
        camera = view.camera
        ax, ay = self._machine.image_to_machine(camera, 1.0, 0.0)
        bx, by = self._machine.image_to_machine(camera, 0.0, 1.0)
        det = ax * by - bx * ay
        at = self._machine.nozzle_position(nozzle)

        def to_px(x: float, y: float) -> tuple[float, float]:
            # The footprint's point on the part, in the machine, then in the picture.
            p = rotate(Point(x=x, y=y), look.angle)
            mx, my = look.center.x + p.x - at.x, look.center.y + p.y - at.y
            right = (by * mx - bx * my) / det
            down = (-ay * mx + ax * my) / det
            return w / 2 + right / view.mm_per_px, h / 2 + down / view.mm_per_px

        overlay, beside = approval_images(
            image, to_px(0.0, 0.0), view.search * 0.6, footprint, to_px
        )
        self.trained.save_image(part_id, "overlay", overlay)
        self.trained.save_image(part_id, "beside", beside)

    def _learn(self, load: NozzleLoad, seen: Point, nozzle_angle: float) -> str:
        """Tell the part's feeder where the part really lay when it was picked. ``seen``:
        the part's center from the nozzle's axis, in machine directions, as the bottom
        camera saw it with the nozzle at ``nozzle_angle``."""
        if load.picked_at is None or load.base is None or load.correction is None:
            return ""
        source = load.slot_id
        try:
            name = self._source_name(source)
        except InvalidTargetError:
            return ""  # the strip or tray was removed since the pick
        # The nozzle may have turned since the pick; the part turned with it. ``seen`` is
        # from the axis, which was off the tip's end (over the pick place) by its runout.
        pick_angle = load.pick_angle or 0.0
        off = rotate(seen, pick_angle - nozzle_angle)
        end = rotate(self._machine.nozzle(load.nozzle).runout, pick_angle)
        part_at = Point(x=load.picked_at.x - end.x + off.x, y=load.picked_at.y - end.y + off.y)
        miss = math.hypot(off.x, off.y)
        learning = self._feeders.learn_pick(source, load.base, part_at, miss, load.correction)
        self._machine.recorder.event(
            "learn", source=source, base=load.base, part_at=part_at, miss=miss, learned=learning
        )
        if load.correction == "bottom" and miss > AUTO_MISS_MM:
            self._look[source] = (AUTO_LOOKS, f"a part from it sat {miss:.2f} mm off center")
        if learning is None:
            return f" Too far off center to learn {source}'s pick place from."
        o = learning.offset
        return f" {name}'s parts lie {o.x:+.2f}, {o.y:+.2f} mm from where its setup says."

    def _source_name(self, source_id: str) -> str:
        if self._feeders.is_fixed(source_id):
            return source_id
        return self._feeders.slot(source_id).label

    async def place(
        self, nozzle: Nozzle, target: Point, rotation: float, board_height: float | None = None
    ) -> str:
        """Place the aligned part on ``nozzle`` with its center at ``target``, turned to
        ``rotation``, on a board whose top is at ``board_height`` (the profile's if None)."""
        load = self._load(nozzle)
        alignment = load.alignment
        if alignment is None:
            raise PlacementError(f"align the part on {nozzle.id} before placing it")
        height = self._place_height(load, board_height)
        self._machine.recorder.event(
            "place", nozzle=nozzle.id, target=target, rotation=rotation, height=height
        )
        # Turn the nozzle so the part has the angle asked for, by the shortest way.
        current = self._machine.nozzle_angle(nozzle)
        turn = current + wrap(rotation - alignment.angle - current)
        offset = rotate(Point(x=alignment.dx, y=alignment.dy), turn)
        over = Point(x=target.x - offset.x, y=target.y - offset.y)
        await self._machine.move_nozzle_over(nozzle, over.x, over.y, turn)
        await self._machine.lower_nozzle(nozzle, height)
        await self._machine.suction(nozzle, False)
        await asyncio.sleep(PLACE_DWELL_S)
        await self._machine.raise_nozzles()
        self._loads.pop(nozzle.id, None)
        # Part-off check: with suction on again in the air, the nozzle must be open.
        await self._machine.suction(nozzle, True)
        still = await self._machine.holds_part(nozzle, False, VACUUM_TIMEOUT_S)
        await self._machine.suction(nozzle, False)
        if still:
            self._loads[nozzle.id] = load
            raise PlacementError(
                f"the part is still on {nozzle.id} after placing it; check the place height"
            )
        return (
            f"{nozzle.id} placed its part at ({target.x:.3f}, {target.y:.3f}), "
            f"{rotation:g} degrees."
        )

    # --- Checks --------------------------------------------------------------------------

    def holds(self, nozzle_id: str) -> NozzleLoad | None:
        return self._loads.get(nozzle_id)

    def check_part(self, part: Part | None, where: str) -> float:
        """The part's height; raise if it cannot be placed (no part, or no height)."""
        return self._part_height(part, where)

    def bottom_camera(self) -> Camera:
        return self._bottom_camera()

    def _check_empty(self, nozzle: Nozzle) -> None:
        if nozzle.id in self._loads:
            raise PlacementError(f"{nozzle.id} already holds a part: place or discard it first")

    def _load(self, nozzle: Nozzle) -> NozzleLoad:
        load = self._loads.get(nozzle.id)
        if load is None:
            raise PlacementError(f"{nozzle.id} holds no part")
        return load

    def _bottom_camera(self) -> Camera:
        camera = next((c for c in self._machine.profile.cameras if c.looking == "up"), None)
        if camera is None:
            raise InvalidTargetError("the machine profile has no up-looking camera")
        if camera.focus_height is None:
            raise PlacementError(
                f"the machine profile gives no focus height for the {camera.label.lower()}"
            )
        return camera

    def _part_height(self, part: Part | None, where: str) -> float:
        if part is None:
            raise PlacementError(f"the feeder in {where} has no part assigned")
        if part.height is None:
            raise PlacementError(f"{part.id} has no height in the parts library")
        return part.height

    def _place_height(self, load: NozzleLoad, board: float | None = None) -> float:
        if board is None:
            board = self._machine.profile.board_height
        if board is None:
            raise PlacementError("the machine profile gives no board height")
        library = self._library()
        part = library.find_part(load.part_id) if library and load.part_id else None
        return board + self._part_height(part, f"slot {load.slot_id}")
