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
from collections.abc import Callable

from picnicplace.feeders.service import FeederService, rotate
from picnicplace.feeders.setup import PickCorrection, PickMethod
from picnicplace.library import Library, Part
from picnicplace.machine.errors import InvalidTargetError, MachineError, VisionError
from picnicplace.machine.profile import Camera, Location, Nozzle, Point
from picnicplace.machine.service import MachineService
from picnicplace.schemas import Operation, WireModel
from picnicplace.vision.part import nearest_angle, wrap

log = logging.getLogger(__name__)

PICK_DWELL_S = 0.2
"""Wait this long with the nozzle on the part and suction on, before lifting."""
PLACE_DWELL_S = 0.1
"""Wait this long after letting go, before lifting."""
VACUUM_TIMEOUT_S = 1.0
SEARCH_MARGIN_MM = 3.0
"""Bottom vision looks this far around the part's expected size."""
AUTO_MISS_MM = 0.3
"""Auto correction: a part picked with the bottom camera's correction that sits further
than this off the nozzle's center sends the next picks from its feeder to the top camera."""
AUTO_LOOKS = 3
"""How many picks then look with the top camera first, before trying the bottom camera's
correction again (the top camera's picks keep teaching it meanwhile)."""
SIZE_TOLERANCE = 0.35
"""A part seen more than this fraction larger or smaller than its pads is reported."""


class PlacementError(MachineError):
    code = "placement_failed"


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


class PlacementService:
    def __init__(
        self,
        machine: MachineService,
        feeders: FeederService,
        library: Callable[[], Library | None],
        pick_correction: PickCorrection = "top",
    ) -> None:
        self._machine = machine
        self._feeders = feeders
        self._library = library
        self.pick_correction: PickCorrection = pick_correction
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
        at, found = base, ""
        if how == "top":
            check = await self._feeders.locate_pocket(slot)
            at = check.at
            found = f" (pocket {check.along:+.2f} mm along, {check.across:+.2f} mm across)"
        elif how == "bottom":
            at, found = self._learned(slot.id, base)
        tape_angle = slot.rotation + (part.tape.rotation if part and part.tape else 0.0)
        await self._take_counted(slot.id, how, nozzle, at, bank.pick_height, slot.label)
        self._loads[nozzle.id] = self._picked(nozzle, part, slot.id, tape_angle, at, base, how)
        what = part.id if part else "a part"
        return f"{nozzle.id} picked {what} from {slot.label}{found}{why}."

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
        if how == "top":
            check = await feeders.locate_strip_pocket(fixed_id)
            at = check.at
            found = f" (pocket {check.along:+.2f} mm along, {check.across:+.2f} mm across)"
        elif how == "bottom":
            at, found = self._learned(fixed_id, base)
        feeders.took_fixed(fixed_id)  # tried or taken, that pocket is done with
        await self._take_counted(fixed_id, how, nozzle, at, height, label)
        self._loads[nozzle.id] = self._picked(nozzle, part, fixed_id, angle, at, base, how)
        what = part.id if part else "a part"
        return f"{nozzle.id} picked {what} number {index + 1} from {label}{found}{why}."

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
    ) -> None:
        """Take the part, and keep count for auto correction: after an empty pick, look
        with the top camera next time; a part taken after a look uses one up."""
        try:
            await self._take(nozzle, at, height, label)
        except MispickError:
            self._look[source_id] = (1, "after an empty pick")
            raise
        if how == "top" and source_id in self._look:
            looks, why = self._look[source_id]
            if looks <= 1:
                del self._look[source_id]
            else:
                self._look[source_id] = (looks - 1, why)

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

    async def _take(self, nozzle: Nozzle, at: Point, height: float, label: str) -> None:
        """Lower onto a part, take it with suction, lift, and check it came up."""
        await self._machine.move_nozzle_over(nozzle, at.x, at.y)
        await self._machine.lower_nozzle(nozzle, height)
        await self._machine.suction(nozzle, True)
        await asyncio.sleep(PICK_DWELL_S)
        await self._machine.raise_nozzles()
        held = await self._machine.holds_part(nozzle, True, VACUUM_TIMEOUT_S)
        if held is False:
            await self._machine.suction(nozzle, False)
            reading = self._machine.vacuum_reading(nozzle)
            raise MispickError(
                f"{nozzle.id} picked nothing from {label}: the vacuum reads "
                f"{reading:g}, not a part. Check the pocket and the pick height."
                if reading is not None
                else f"{nozzle.id} picked nothing from {label}"
            )

    async def align(self, nozzle: Nozzle) -> str:
        load = self._load(nozzle)
        camera = self._bottom_camera()
        assert camera.focus_height is not None
        library = self._library()
        part = library.find_part(load.part_id) if library and load.part_id else None
        height = part.height if part is not None and part.height is not None else 0.0
        pads = _footprint_center(part, library)

        await self._machine.move_nozzle_over(nozzle, camera.position.x, camera.position.y)
        await self._machine.lower_nozzle(nozzle, camera.focus_height + height)
        await self._machine.set_light(camera, True)
        try:
            frame = await self._machine.frame(camera.id)
        finally:
            await self._machine.set_light(camera, False)
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        size = pads[1] if pads else Point(x=1.6, y=0.8)
        search = (math.hypot(size.x, size.y) / 2 + SEARCH_MARGIN_MM) / mm_per_px
        box = await self._machine.recorder.vision(
            "find_part",
            frame.image,
            search,
            camera=camera.id,
            position=dict(self._machine.driver.position),
            mm_per_px=mm_per_px,
            nozzle=nozzle.id,
            part=load.part_id,
            source=load.slot_id,
        )
        await self._machine.raise_nozzles()
        if box is None:
            # Likely a bad pick (nothing there, or a part on its side), even when the
            # vacuum could not tell: auto correction looks first next time.
            self._look[load.slot_id] = (1, "after the bottom camera saw no part")
            raise VisionError(f"the {camera.label.lower()} saw no part on {nozzle.id}")

        nozzle_at = self._machine.nozzle_position(nozzle)
        nozzle_angle = self._machine.nozzle_angle(nozzle)
        dx, dy = self._machine.image_to_machine(
            camera, (box.u - w / 2) * mm_per_px, (box.v - h / 2) * mm_per_px
        )
        # The box's first side, from image directions to machine ones.
        a = math.radians(box.angle)
        sx, sy = self._machine.image_to_machine(camera, math.cos(a), math.sin(a))
        side = math.degrees(math.atan2(sy, sx))
        expected = nozzle_angle + load.angle
        angle = nearest_angle(side, expected)
        # The box is around the pads; the part's origin may not be their center.
        center = Point(x=nozzle_at.x + dx, y=nozzle_at.y + dy)
        if pads is not None:
            shift = rotate(pads[0], angle)
            center = Point(x=center.x - shift.x, y=center.y - shift.y)
        offset = rotate(Point(x=center.x - nozzle_at.x, y=center.y - nozzle_at.y), -nozzle_angle)
        alignment = Alignment(
            dx=round(offset.x, 4), dy=round(offset.y, 4), angle=wrap(angle - nozzle_angle)
        )
        self._loads[nozzle.id] = load.model_copy(update={"alignment": alignment})
        self._machine.recorder.event("align", nozzle=nozzle.id, alignment=alignment)
        learned = self._learn(load, Point(x=dx, y=dy), nozzle_angle)

        seen = sorted((box.width * mm_per_px, box.height * mm_per_px))
        want = sorted((size.x, size.y))
        note = ""
        if pads is not None and any(
            abs(s - e) > SIZE_TOLERANCE * e for s, e in zip(seen, want, strict=True)
        ):
            note = (
                f" Its metal measures {seen[0]:.2f} x {seen[1]:.2f} mm; its pads are "
                f"{want[0]:.2f} x {want[1]:.2f}: check the part."
            )
        return (
            f"{nozzle.id} holds its part {math.hypot(offset.x, offset.y):.2f} mm off center "
            f"and turned {wrap(angle - expected):+.1f} degrees from the tape's angle.{note}"
            f"{learned}"
        )

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
        # The nozzle may have turned since the pick; the part turned with it.
        off = rotate(seen, (load.pick_angle or 0.0) - nozzle_angle)
        part_at = Point(x=load.picked_at.x + off.x, y=load.picked_at.y + off.y)
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
