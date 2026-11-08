"""Picking, aligning, and placing one part at a time (spec section 7.3, milestone M3).

Each step is an operation, so a first run on a real machine can go one step at a time:

- **Pick**: feed the slot's tape, find the pocket from the sprocket holes, put the nozzle
  over it, lower it to the top of the part, turn suction on, lift, and check the vacuum
  says a part is held.
- **Align**: carry the part over the bottom camera with its underside in focus, find it,
  and record where it sits on the nozzle and at what angle.
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
from picnicplace.library import Library, Part
from picnicplace.machine.errors import InvalidTargetError, MachineError, VisionError
from picnicplace.machine.profile import Camera, Nozzle, Point
from picnicplace.machine.service import MachineService
from picnicplace.schemas import Operation, WireModel
from picnicplace.vision.part import find_part, nearest_angle, wrap

log = logging.getLogger(__name__)

PICK_DWELL_S = 0.2
"""Wait this long with the nozzle on the part and suction on, before lifting."""
PLACE_DWELL_S = 0.1
"""Wait this long after letting go, before lifting."""
VACUUM_TIMEOUT_S = 1.0
SEARCH_MARGIN_MM = 3.0
"""Bottom vision looks this far around the part's expected size."""
SIZE_TOLERANCE = 0.35
"""A part seen more than this fraction larger or smaller than its pads is reported."""


class PlacementError(MachineError):
    code = "placement_failed"


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


class PlacementState(WireModel):
    loads: list[NozzleLoad]
    """What each nozzle holds, as far as PicnicPlace knows."""


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
    ) -> None:
        self._machine = machine
        self._feeders = feeders
        self._library = library
        self._loads: dict[str, NozzleLoad] = {}

    def state(self) -> PlacementState:
        return PlacementState(loads=[self._loads[k] for k in sorted(self._loads)])

    # --- Operations ----------------------------------------------------------------------

    def start_pick(
        self, slot_id: str, nozzle_id: str, feed: bool = True, find_pocket: bool = True
    ) -> Operation:
        slot = self._feeders.slot(slot_id)
        nozzle = self._machine.nozzle(nozzle_id)
        self._check_empty(nozzle)
        if feed:
            self._feeders.check_can_feed(slot)
        return self._machine.start_task(
            "pick", lambda: self._pick(slot_id, nozzle, feed, find_pocket)
        )

    def start_align(self, nozzle_id: str) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        self._load(nozzle)
        self._bottom_camera()
        return self._machine.start_task("align", lambda: self._align(nozzle))

    def start_place(self, nozzle_id: str, x: float, y: float, rotation: float) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        load = self._load(nozzle)
        if load.alignment is None:
            raise PlacementError(f"align the part on {nozzle.id} before placing it")
        self._place_height(load)
        return self._machine.start_task(
            "place", lambda: self._place(nozzle, Point(x=x, y=y), rotation)
        )

    def start_pick_and_place(
        self,
        slot_id: str,
        nozzle_id: str,
        x: float,
        y: float,
        rotation: float,
        feed: bool = True,
        find_pocket: bool = True,
    ) -> Operation:
        slot = self._feeders.slot(slot_id)
        nozzle = self._machine.nozzle(nozzle_id)
        self._check_empty(nozzle)
        self._bottom_camera()
        part = self._feeders.part(slot)
        self._part_height(part, slot.label)
        if feed:
            self._feeders.check_can_feed(slot)

        async def work() -> str:
            steps = [await self._pick(slot_id, nozzle, feed, find_pocket)]
            steps.append(await self._align(nozzle))
            steps.append(await self._place(nozzle, Point(x=x, y=y), rotation))
            return " ".join(steps)

        return self._machine.start_task("pick_and_place", work)

    def start_discard(self, nozzle_id: str) -> Operation:
        nozzle = self._machine.nozzle(nozzle_id)
        bin_ = next((loc for loc in self._machine.profile.locations if loc.kind == "discard"), None)
        if bin_ is None:
            raise PlacementError(
                "no discard location: add one under [[machine.locations]] in the config"
            )

        async def work() -> str:
            await self._machine.move_nozzle_over(nozzle, bin_.position.x, bin_.position.y)
            await self._machine.suction(nozzle, False)
            await asyncio.sleep(PLACE_DWELL_S)
            self._loads.pop(nozzle.id, None)
            return f"{nozzle.id}: dropped its part in the {bin_.label.lower()}."

        return self._machine.start_task("discard", work)

    # --- Steps ---------------------------------------------------------------------------

    async def _pick(self, slot_id: str, nozzle: Nozzle, feed: bool, find_pocket: bool) -> str:
        slot = self._feeders.slot(slot_id)
        bank = self._feeders.bank(slot)
        if bank.pick_height is None:
            raise PlacementError(f"the machine profile gives no pick height for {slot.label}")
        part = self._feeders.part(slot)
        if feed:
            await self._feeders.feed(slot)
        at = self._feeders.pick(slot)
        found = ""
        if find_pocket:
            check = await self._feeders.locate_pocket(slot)
            at = check.at
            found = f" (pocket {check.along:+.2f} mm along, {check.across:+.2f} mm across)"
        await self._machine.move_nozzle_over(nozzle, at.x, at.y)
        await self._machine.lower_nozzle(nozzle, bank.pick_height)
        await self._machine.suction(nozzle, True)
        await asyncio.sleep(PICK_DWELL_S)
        await self._machine.raise_nozzles()
        held = await self._machine.holds_part(nozzle, True, VACUUM_TIMEOUT_S)
        if held is False:
            await self._machine.suction(nozzle, False)
            reading = self._machine.vacuum_reading(nozzle)
            raise PlacementError(
                f"{nozzle.id} picked nothing from {slot.label}: the vacuum reads "
                f"{reading:g}, not a part. Check the pocket and the pick height."
                if reading is not None
                else f"{nozzle.id} picked nothing from {slot.label}"
            )
        tape_angle = slot.rotation + (part.tape.rotation if part and part.tape else 0.0)
        self._loads[nozzle.id] = NozzleLoad(
            nozzle=nozzle.id,
            part_id=part.id if part else None,
            slot_id=slot.id,
            angle=wrap(tape_angle - self._machine.nozzle_angle(nozzle)),
            alignment=None,
        )
        what = part.id if part else "a part"
        return f"{nozzle.id} picked {what} from {slot.label}{found}."

    async def _align(self, nozzle: Nozzle) -> str:
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
        box = await asyncio.to_thread(find_part, frame.image, search)
        await self._machine.raise_nozzles()
        if box is None:
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
        )

    async def _place(self, nozzle: Nozzle, target: Point, rotation: float) -> str:
        load = self._load(nozzle)
        alignment = load.alignment
        if alignment is None:
            raise PlacementError(f"align the part on {nozzle.id} before placing it")
        height = self._place_height(load)
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

    def _place_height(self, load: NozzleLoad) -> float:
        board = self._machine.profile.board_height
        if board is None:
            raise PlacementError("the machine profile gives no board height")
        library = self._library()
        part = library.find_part(load.part_id) if library and load.part_id else None
        return board + self._part_height(part, f"slot {load.slot_id}")
