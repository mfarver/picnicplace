"""The feeder service: finds feeders, feeds them, and knows where to pick from each.

A slot's pick location is the profile's nominal location plus the slot's correction from
the feeder setup. Corrections are taught by eye (center the top camera over the pocket)
or found from the feeder's alignment fiducial: once the fiducial's offset from the pick
location has been measured on one feeder, every other slot can be calibrated by finding
its fiducial with the camera.
"""

import logging
import math
from collections.abc import Callable
from typing import Literal

from picnicplace.camera.synthetic import TapeScene
from picnicplace.feeders.photon import PhotonBus
from picnicplace.feeders.pocket import pocket_from_holes
from picnicplace.feeders.setup import FeederAssignment, FeederSetupStore, SlotCalibration
from picnicplace.feeders.sim import SimulatedPhotonBus
from picnicplace.feeders.slots import Slot, machine_slots
from picnicplace.library import Library, Part
from picnicplace.library.tape import SPROCKET_DIAMETER, geometry
from picnicplace.machine.errors import FeederError, InvalidTargetError, NotSupportedError
from picnicplace.machine.profile import FeederBank, Point
from picnicplace.machine.service import MachineService
from picnicplace.machine.simulator import SimulatorDriver
from picnicplace.operations import OperationManager
from picnicplace.schemas import Operation, WireModel

log = logging.getLogger(__name__)

DEFAULT_PITCH = 4.0
"""Feed distance when neither the feeder nor its part says: the most common pitch."""

FIDUCIAL_SEARCH_MM = 3.0
"""Look for a feeder's fiducial this far from where it should be."""

FIDUCIAL_DIAMETERS = (0.5, 2.5)
"""Diameter range searched when the profile does not give the fiducial's size."""

SIM_FIDUCIAL_OFFSET = Point(x=6.0, y=-6.5)
"""Where the simulator draws a feeder's fiducial when the profile gives none: the
simulator's own choice, in the tape frame. Real feeders' offsets are measured."""

SIM_PICK_RADIUS = 1.5
"""A simulated pick this close to a pocket takes its part."""


class FeederState(WireModel):
    uuid: str
    initialized: bool
    assignment: FeederAssignment | None


class PocketCheck(WireModel):
    """Where vision last found the pick pocket, from the tape's sprocket holes."""

    at: Point
    along: float
    """From the slot's pick location along the feed, in mm."""
    across: float
    holes: int


class SlotState(WireModel):
    slot: Slot
    pick: Point
    """Where to pick: nominal plus the correction, if any."""
    calibration: SlotCalibration | None
    feeder: FeederState | None
    error: str | None
    pocket: PocketCheck | None


class FeedersState(WireModel):
    bus: Literal["photon"] | None
    scanned: bool
    """Whether the bus has been scanned since the core started."""
    slots: list[SlotState]
    fiducial_offset: Point | None
    fiducial_offset_source: Literal["measured", "profile"] | None
    assignments: dict[str, FeederAssignment]
    """Every feeder the setup knows, by ID, found or not."""


def rotate(p: Point, degrees: float) -> Point:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return Point(x=p.x * c - p.y * s, y=p.x * s + p.y * c)


def sim_slot_error(number: int) -> Point:
    """How far a simulated slot really is from its nominal place: fixed per slot, within
    half a millimeter, so calibration has something to find."""
    return Point(x=((number * 37) % 11 - 5) * 0.1, y=((number * 53) % 9 - 4) * 0.1)


class FeederService:
    def __init__(
        self,
        machine: MachineService,
        operations: OperationManager,
        store: FeederSetupStore,
        library: Callable[[], Library | None],
        simulated: SimulatedPhotonBus | None = None,
    ) -> None:
        self._machine = machine
        self._operations = operations
        self._store = store
        self._library = library
        self._profile = machine.profile
        self._slots = machine_slots(self._profile)
        self._by_id = {s.id: s for s in self._slots}
        self._banks: dict[str, FeederBank] = {b.id: b for b in self._profile.feeder_banks}
        self._bus = (
            PhotonBus(machine.driver.rs485) if self._profile.feeder_bus == "photon" else None
        )
        self._found: dict[str, str] = {}
        """Slot ID to the ID of the feeder found there."""
        self._initialized: set[str] = set()
        self._errors: dict[str, str] = {}
        self._scanned = False
        self._pockets: dict[str, PocketCheck] = {}

        self._sim = simulated
        self._sim_full: dict[int, bool] = {}
        self._tapes: tuple[TapeScene, ...] | None = None
        if simulated is not None:
            simulated.on_change = self._invalidate
            machine.scene_tapes = self._scene_tapes
            driver = machine.driver
            if isinstance(driver, SimulatorDriver):
                driver.on_pick = self._sim_picked

    # --- State ---------------------------------------------------------------------------

    @property
    def setup(self) -> FeederSetupStore:
        return self._store

    def fiducial_offset(self, bank: FeederBank) -> tuple[Point | None, str | None]:
        measured = self._store.setup.fiducial_offset
        if measured is not None:
            return measured, "measured"
        if bank.fiducial_offset is not None:
            return bank.fiducial_offset, "profile"
        return None, None

    def pick(self, slot: Slot) -> Point:
        cal = self._store.setup.slots.get(slot.id)
        if cal is None:
            return slot.pick
        return Point(x=slot.pick.x + cal.correction.x, y=slot.pick.y + cal.correction.y)

    def state(self) -> FeedersState:
        setup = self._store.setup
        slots: list[SlotState] = []
        for slot in self._slots:
            uuid = self._found.get(slot.id)
            feeder = (
                FeederState(
                    uuid=uuid,
                    initialized=slot.id in self._initialized,
                    assignment=setup.feeders.get(uuid),
                )
                if uuid is not None
                else None
            )
            slots.append(
                SlotState(
                    slot=slot,
                    pick=self.pick(slot),
                    calibration=setup.slots.get(slot.id),
                    feeder=feeder,
                    error=self._errors.get(slot.id),
                    pocket=self._pockets.get(slot.id),
                )
            )
        offset, source = (
            self.fiducial_offset(self._profile.feeder_banks[0])
            if self._profile.feeder_banks
            else (None, None)
        )
        return FeedersState(
            bus=self._profile.feeder_bus,
            scanned=self._scanned,
            slots=slots,
            fiducial_offset=offset,
            fiducial_offset_source=source,  # type: ignore[arg-type]
            assignments=dict(setup.feeders),
        )

    def slot(self, slot_id: str) -> Slot:
        slot = self._by_id.get(slot_id)
        if slot is None:
            raise InvalidTargetError(f"no feeder slot {slot_id!r}")
        return slot

    def _need_bus(self) -> PhotonBus:
        if self._bus is None:
            raise NotSupportedError("this machine has no feeder bus")
        return self._bus

    def _address(self, slot: Slot) -> int:
        if slot.address is None:
            raise NotSupportedError(f"{slot.label} has no bus address")
        return slot.address

    # --- Bus -----------------------------------------------------------------------------

    def start_scan(self) -> Operation:
        """Ask every slot for its feeder, and initialize each feeder found."""
        bus = self._need_bus()
        self._machine.check_can_actuate()

        async def work() -> str:
            found: dict[str, str] = {}
            initialized: set[str] = set()
            errors: dict[str, str] = {}
            for slot in self._slots:
                if slot.address is None:
                    continue
                try:
                    uuid = await bus.feeder_id(slot.address)
                    if uuid is None:
                        continue
                    found[slot.id] = uuid
                    await bus.initialize(slot.address, uuid)
                    initialized.add(slot.id)
                except FeederError as e:
                    errors[slot.id] = str(e)
            self._found, self._initialized, self._errors = found, initialized, errors
            self._scanned = True
            self._invalidate()
            new = [u for u in found.values() if u not in self._store.setup.feeders]
            parts = [f"Found {len(found)} feeder{'' if len(found) == 1 else 's'}"]
            if new:
                parts.append(f"{len(new)} new")
            if errors:
                parts.append(f"{len(errors)} with errors")
            return ", ".join(parts) + "."

        return self._operations.start("feeder_scan", work)

    def feed_distance(self, slot: Slot) -> float:
        uuid = self._found.get(slot.id)
        assignment = self._store.setup.feeders.get(uuid) if uuid else None
        if assignment is not None and assignment.part_pitch:
            return assignment.part_pitch
        part = self._part(assignment)
        if part is not None and part.tape is not None and part.tape.pitch:
            return part.tape.pitch
        return DEFAULT_PITCH

    def start_feed(self, slot_id: str, distance: float | None) -> Operation:
        slot = self.slot(slot_id)
        bus = self._need_bus()
        address = self._address(slot)
        self._machine.check_can_actuate()
        if slot.id not in self._initialized:
            raise FeederError(f"{slot.label}: no initialized feeder; scan the feeders first")
        mm = self.feed_distance(slot) if distance is None else distance

        async def work() -> str:
            try:
                await bus.feed(address, mm)
            except FeederError as e:
                self._errors[slot.id] = str(e)
                raise
            self._errors.pop(slot.id, None)
            if mm > 0:
                self._sim_full[address] = True
            self._invalidate()
            return f"{slot.label}: fed {mm:g} mm."

        return self._operations.start("feed", work)

    async def identify(self, slot_id: str) -> bool:
        slot = self.slot(slot_id)
        bus = self._need_bus()
        uuid = self._found.get(slot.id)
        if uuid is None:
            raise FeederError(f"{slot.label}: no feeder found there; scan the feeders first")
        self._machine.check_can_actuate()
        return await bus.identify(uuid)

    # --- Setup ---------------------------------------------------------------------------

    def assign(self, uuid: str, assignment: FeederAssignment) -> None:
        self._store.setup.feeders[uuid] = assignment
        self._store.save()
        self._invalidate()

    def forget(self, uuid: str) -> None:
        self._store.setup.feeders.pop(uuid, None)
        self._store.save()
        self._invalidate()

    def teach_pick(self, slot_id: str, at: Point) -> SlotCalibration:
        """The pick location is here (the camera was centered over the pocket by eye)."""
        slot = self.slot(slot_id)
        cal = SlotCalibration(
            correction=Point(x=round(at.x - slot.pick.x, 3), y=round(at.y - slot.pick.y, 3)),
            basis="taught",
        )
        self._store.setup.slots[slot.id] = cal
        self._store.save()
        return cal

    def forget_pick(self, slot_id: str) -> None:
        self._store.setup.slots.pop(self.slot(slot_id).id, None)
        self._store.save()

    # --- Fiducials -----------------------------------------------------------------------

    def _fiducial_size(self, bank: FeederBank) -> tuple[float, float]:
        if bank.fiducial_diameter is not None:
            return bank.fiducial_diameter * 0.6, bank.fiducial_diameter * 1.5
        return FIDUCIAL_DIAMETERS

    def start_measure_fiducial(self, slot_id: str) -> Operation:
        """With the top camera over a feeder's fiducial, find it, and record its offset
        from the slot's pick location. Teach that slot's pick location first."""
        slot = self.slot(slot_id)
        bank = self._banks[slot.bank]

        async def work() -> str:
            low, high = self._fiducial_size(bank)
            found = await self._machine.locate_circle(low, high, FIDUCIAL_SEARCH_MM)
            if found is None:
                raise FeederError(
                    "no fiducial found near the camera's center; center it within "
                    f"{FIDUCIAL_SEARCH_MM:g} mm"
                )
            pick = self.pick(slot)
            offset = rotate(Point(x=found.x - pick.x, y=found.y - pick.y), -slot.rotation)
            offset = Point(x=round(offset.x, 3), y=round(offset.y, 3))
            self._store.setup.fiducial_offset = offset
            self._store.save()
            taught = slot.id in self._store.setup.slots
            note = "" if taught else " The slot's pick location was not taught: check it."
            return (
                f"Feeder fiducial is {offset.x:+.2f} mm along the feed and {offset.y:+.2f} mm "
                f"across it from the pick location.{note}"
            )

        return self._machine.start_task("feeder_fiducial", work)

    def start_calibrate(self, slot_ids: list[str] | None) -> Operation:
        """Find each slot's feeder fiducial and correct the slot's pick location. With no
        slots given, every slot with a feeder found on the bus."""
        targets = (
            [self.slot(s) for s in slot_ids]
            if slot_ids
            else [s for s in self._slots if s.id in self._found]
        )
        if not targets:
            raise FeederError("no slots to calibrate; scan the feeders first")
        for slot in targets:
            if self.fiducial_offset(self._banks[slot.bank])[0] is None:
                raise FeederError(
                    "the feeder fiducial's offset is not known yet: teach one slot's pick "
                    "location, then measure its fiducial"
                )

        async def work() -> str:
            done: list[str] = []
            failed: list[str] = []
            for slot in targets:
                bank = self._banks[slot.bank]
                offset, _ = self.fiducial_offset(bank)
                assert offset is not None
                turned = rotate(offset, slot.rotation)
                guess = self.pick(slot)
                await self._machine.move_camera_to(guess.x + turned.x, guess.y + turned.y)
                low, high = self._fiducial_size(bank)
                found = await self._machine.locate_circle(low, high, FIDUCIAL_SEARCH_MM)
                if found is None:
                    failed.append(slot.label)
                    self._errors[slot.id] = "fiducial not found"
                    continue
                self._errors.pop(slot.id, None)
                pick = Point(x=found.x - turned.x, y=found.y - turned.y)
                self._store.setup.slots[slot.id] = SlotCalibration(
                    correction=Point(
                        x=round(pick.x - slot.pick.x, 3), y=round(pick.y - slot.pick.y, 3)
                    ),
                    basis="fiducial",
                )
                done.append(slot.label)
            self._store.save()
            message = f"Calibrated {len(done)} slot{'' if len(done) == 1 else 's'}."
            if failed:
                message += f" No fiducial found at: {', '.join(failed)}."
            return message

        return self._machine.start_task("feeder_calibrate", work)

    # --- Pockets -------------------------------------------------------------------------

    def _pocket_line(self, slot: Slot) -> tuple[float, float]:
        """The tape's pocket line (F) and pitch for the part in a slot."""
        uuid = self._found.get(slot.id)
        part = self._part(self._store.setup.feeders.get(uuid) if uuid else None)
        width = part.tape.width if part is not None and part.tape and part.tape.width else 8.0
        return _pocket_line_for(width), self.feed_distance(slot)

    async def locate_pocket(self, slot: Slot) -> PocketCheck:
        """Look at a slot's pick location and find the pick pocket from the sprocket
        holes. For use inside a machine task."""
        pick = self.pick(slot)
        await self._machine.move_camera_to(pick.x, pick.y)
        low, high = SPROCKET_DIAMETER * 0.7, SPROCKET_DIAMETER * 1.3
        seen = await self._machine.locate_circles(low, high)
        holes = [
            (q.x, q.y)
            for q in (rotate(Point(x=p.x - pick.x, y=p.y - pick.y), -slot.rotation) for p in seen)
        ]
        line, pitch = self._pocket_line(slot)
        fix = pocket_from_holes(holes, line, pitch)
        if fix is None:
            self._pockets.pop(slot.id, None)
            raise FeederError(f"{slot.label}: no sprocket holes found near the pick location")
        shift = rotate(Point(x=fix.along, y=fix.across), slot.rotation)
        check = PocketCheck(
            at=Point(x=round(pick.x + shift.x, 3), y=round(pick.y + shift.y, 3)),
            along=round(fix.along, 3),
            across=round(fix.across, 3),
            holes=fix.holes,
        )
        self._pockets[slot.id] = check
        return check

    def start_locate_pocket(self, slot_id: str) -> Operation:
        slot = self.slot(slot_id)

        async def work() -> str:
            check = await self.locate_pocket(slot)
            await self._machine.move_camera_to(check.at.x, check.at.y)
            return (
                f"{slot.label}: the pocket is {check.along:+.2f} mm along the feed and "
                f"{check.across:+.2f} mm across from the pick location "
                f"(from {check.holes} sprocket holes)."
            )

        return self._machine.start_task("feeder_pocket", work)

    # --- Simulation ----------------------------------------------------------------------

    def _part(self, assignment: FeederAssignment | None) -> Part | None:
        library = self._library()
        if assignment is None or assignment.part_id is None or library is None:
            return None
        return library.part(assignment.part_id)

    def _invalidate(self) -> None:
        self._tapes = None

    def _sim_true_pick(self, slot: Slot) -> Point:
        error = sim_slot_error(slot.number)
        return Point(x=slot.pick.x + error.x, y=slot.pick.y + error.y)

    def _sim_picked(self, _nozzle: str, x: float, y: float) -> None:
        if self._sim is None:
            return
        for address in self._sim.feeders:
            slot = next((s for s in self._slots if s.address == address), None)
            if slot is None:
                continue
            p = self._sim_true_pick(slot)
            if math.hypot(p.x - x, p.y - y) <= SIM_PICK_RADIUS:
                self._sim_full[address] = False
                self._invalidate()

    def _scene_tapes(self) -> tuple[TapeScene, ...]:
        if self._tapes is None:
            self._tapes = self._build_tapes()
        return self._tapes

    def _build_tapes(self) -> tuple[TapeScene, ...]:
        if self._sim is None:
            return ()
        library = self._library()
        tapes: list[TapeScene] = []
        by_address = {s.address: s for s in self._slots if s.address is not None}
        for address, feeder in sorted(self._sim.feeders.items()):
            slot = by_address.get(address)
            if slot is None:
                continue
            bank = self._banks[slot.bank]
            pick = self._sim_true_pick(slot)
            fid = rotate(bank.fiducial_offset or SIM_FIDUCIAL_OFFSET, slot.rotation)
            assignment = self._store.setup.feeders.get(feeder.uuid.hex().upper())
            part = self._part(assignment)
            footprint = (
                library.footprint(part.footprint_id) if part is not None and library else None
            )
            tape = part.tape if part is not None else None
            width = tape.width if tape is not None and tape.width else 8.0
            pitch = tape.pitch if tape is not None and tape.pitch else DEFAULT_PITCH
            tapes.append(
                TapeScene(
                    pick=(pick.x, pick.y),
                    rotation=slot.rotation,
                    width=width,
                    pitch=pitch,
                    pocket_line=_pocket_line_for(width),
                    advance=feeder.fed_mm,
                    pick_full=self._sim_full.get(address, True),
                    part_rotation=tape.rotation if tape is not None else 0.0,
                    body=(footprint.body.x, footprint.body.y)
                    if footprint is not None and footprint.body is not None
                    else None,
                    pads=tuple((p.x, p.y, p.width, p.height, p.rotation) for p in footprint.pads)
                    if footprint is not None
                    else (),
                    fiducial=(pick.x + fid.x, pick.y + fid.y),
                    fiducial_diameter=bank.fiducial_diameter or 1.0,
                    feeder_width=bank.width,
                    feeder_length=bank.length,
                )
            )
        return tuple(tapes)


def _pocket_line_for(width: float) -> float:
    try:
        return geometry(width, DEFAULT_PITCH).pocket_line
    except ValueError:
        return geometry(8.0, DEFAULT_PITCH).pocket_line
