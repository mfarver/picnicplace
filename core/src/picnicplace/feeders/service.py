"""The feeder service: finds feeders, feeds them, and knows where to pick from each.

Fixed feeders (strips of tape and trays held on the bed) have no slot and no bus: each
pick takes the next part in order, and when none are left the operator refills it.

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
from picnicplace.feeders.rails import fit_rail, residual
from picnicplace.feeders.setup import (
    FeederAssignment,
    FeederSetupStore,
    FixedFeeder,
    PickLearning,
    PickMethod,
    PickMisses,
    RailCalibration,
    RailPoint,
    SlotCalibration,
)
from picnicplace.feeders.sim import SimulatedPhotonBus
from picnicplace.feeders.slots import Slot, machine_slots
from picnicplace.library import Library, Part
from picnicplace.library.tape import SPROCKET_DIAMETER, geometry
from picnicplace.machine.errors import (
    FeederEmptyError,
    FeederError,
    InvalidTargetError,
    NotSupportedError,
)
from picnicplace.machine.profile import FeederBank, Point
from picnicplace.machine.service import MachineService
from picnicplace.machine.simulator import SimulatorDriver
from picnicplace.operations import OperationManager
from picnicplace.schemas import Operation, WireModel
from picnicplace.simparts import PartShape, SimulatedParts

log = logging.getLogger(__name__)

DEFAULT_PITCH = 4.0
"""Feed distance when neither the feeder nor its part says: the most common pitch."""

FIDUCIAL_SEARCH_MM = 3.0

RAIL_SEARCH_ACROSS_MM = 7.0
"""Searching for a rail's end fiducial, the camera also looks this far to either side of
where it is expected, across the rail."""
RAIL_SEARCH_ALONG_MM = 5.0
"""And this far along the rail. A fiducial found more than half a slot along the rail
from where it was expected may be the next feeder's, so it is not taken."""
RAIL_SEARCH_RADIUS_MM = 5.0
"""How far from each frame's center a fiducial is looked for: within the top camera's
view."""
RAIL_MIN_SPAN = 1 / 3
"""A rail is fitted to fiducials at least this fraction of its length apart."""
RAIL_RESIDUAL_MM = 1.0
"""A rail fitted to more than two fiducials that misses one by more than this is
suspect: a fiducial was mistaken, or a rail is bent."""
"""Look for a feeder's fiducial this far from where it should be."""

FIDUCIAL_DIAMETERS = (0.5, 2.5)
"""Diameter range searched when the profile does not give the fiducial's size."""

SIM_FIDUCIAL_OFFSET = Point(x=19.15, y=-4.1)
"""Where the simulator draws a feeder's fiducial when the profile gives none: as on
Opulo's Photon feeders, along the feed from the pick location and across from the
sprocket holes' line."""

PHOTON_WINDOW = (-7.7, 4.8, -5.5, 1.75)
"""A Photon feeder's pick window, from Opulo's design: along the feed from the pick
location, and across from the sprocket holes' line (toward the holes positive)."""

PHOTON_ACROSS = (-11.5, 3.5)
"""A Photon feeder's sides, across from the sprocket holes' line."""

SIM_FIDUCIAL_MARKS = 2.0
"""The two small marks beside a Photon feeder's fiducial, this far to either side across
the tape (on an 8 mm feeder)."""

SIM_PICK_RADIUS = 1.5
"""A simulated pick this close to a pocket takes its part."""

SIM_FIXED_ERROR = Point(x=0.15, y=-0.1)
"""How far a simulated strip or tray really is from where its setup says."""

LEARN_FIRST = 4
"""A feeder's first picks are averaged evenly; after these, each counts for LEARN_GAIN."""
LEARN_GAIN = 0.25
LEARN_MAX_MISS_MM = 1.5
"""A pick this far off center is not learned from: likely a part that tipped or slid."""
LEARN_MAX_OFFSET_MM = 3.0
"""The learned offset never moves a pick further than this."""
MISS_WINDOW = 20


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
    learned: PickLearning | None
    """What the bottom camera has learned of this slot's picks, with its feeder now."""
    feeder: FeederState | None
    error: str | None
    pocket: PocketCheck | None


class FixedFeederState(WireModel):
    id: str
    feeder: FixedFeeder
    remaining: int
    next: Point | None
    """Where the next part is, or None when it is empty."""
    angle: float | None
    """The parts' machine angle, if known (a strip needs its part's tape rotation)."""
    pocket: PocketCheck | None
    learned: PickLearning | None


class RailState(WireModel):
    bank: str
    label: str
    first_slot: str
    last_slot: str
    calibration: RailCalibration | None
    """Fiducials found on the rail, and the rail fitted to them, if any."""


class FeedersState(WireModel):
    bus: Literal["photon"] | None
    scanned: bool
    """Whether the bus has been scanned since the core started."""
    slots: list[SlotState]
    fiducial_offset: Point | None
    fiducial_offset_source: Literal["measured", "profile"] | None
    assignments: dict[str, FeederAssignment]
    """Every feeder the setup knows, by ID, found or not."""
    fixed: list[FixedFeederState]
    """Strips and trays on the bed."""
    rails: list[RailState]
    """Each feeder rail, and where it really is, if calibrated."""


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
        self._nominal = machine_slots(self._profile)
        """The slots where the profile puts them."""
        self._banks: dict[str, FeederBank] = {b.id: b for b in self._profile.feeder_banks}
        self._slots: list[Slot] = []
        """The slots where their rails really are (see ``_apply_rails``)."""
        self._by_id: dict[str, Slot] = {}
        self._apply_rails()
        self.sim_rail_errors: dict[str, tuple[float, float, float]] = {}
        """Simulated rails: how far each bank really is from the profile, by bank ID: a
        shift (x, y) in mm and a turn in degrees about its first slot."""
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
        self._sim_fixed_taken: dict[str, set[int]] = {}
        """Simulated strips and trays: the parts really gone from each."""
        self._tapes: tuple[TapeScene, ...] | None = None
        self._tape_at: dict[int, TapeScene] = {}
        self.sim_parts: SimulatedParts | None = None
        if simulated is not None:
            simulated.on_change = self._invalidate
            machine.scene_tapes = self._scene_tapes
            driver = machine.driver
            if isinstance(driver, SimulatorDriver):
                parts = SimulatedParts(self._take_from_tape)
                self.sim_parts = parts
                driver.pick_source = lambda nozzle, x, y, angle: parts.pick(nozzle, x, y, angle)
                driver.on_drop = parts.drop
                machine.scene_parts = parts.scene

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

    def calibration(self, slot: Slot) -> SlotCalibration | None:
        """The slot's pick correction, if it applies to the feeder in it now: one found
        from another feeder's fiducial does not."""
        cal = self._store.setup.slots.get(slot.id)
        if cal is None or cal.feeder is None or cal.feeder == self._found.get(slot.id):
            return cal
        return None

    def pick(self, slot: Slot) -> Point:
        cal = self.calibration(slot)
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
                    calibration=self.calibration(slot),
                    learned=self.learned(slot.id),
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
            fixed=[self.fixed_state(i) for i in sorted(setup.fixed, key=_fixed_order)],
            rails=[self._rail_state(bank) for bank in self._profile.feeder_banks],
        )

    def fixed_state(self, fixed_id: str) -> FixedFeederState:
        f = self.fixed(fixed_id)
        return FixedFeederState(
            id=fixed_id,
            feeder=f,
            remaining=f.remaining,
            next=f.position(f.picked) if f.remaining else None,
            angle=self.fixed_angle(f),
            pocket=self._pockets.get(fixed_id),
            learned=self.learned(fixed_id),
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
        self._need_bus()
        self._machine.check_can_actuate()
        return self._operations.start("feeder_scan", self.scan)

    @property
    def has_bus(self) -> bool:
        return self._bus is not None

    async def scan(self) -> str:
        """Ask every slot for its feeder, and initialize each feeder found."""
        bus = self._need_bus()
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

    def feed_distance(self, slot: Slot) -> float:
        uuid = self._found.get(slot.id)
        assignment = self._store.setup.feeders.get(uuid) if uuid else None
        if assignment is not None and assignment.part_pitch:
            return assignment.part_pitch
        part = self._part(assignment)
        if part is not None and part.tape is not None and part.tape.pitch:
            return part.tape.pitch
        return DEFAULT_PITCH

    def check_can_feed(self, slot: Slot) -> None:
        self._need_bus()
        self._address(slot)
        if slot.id not in self._initialized:
            raise FeederError(f"{slot.label}: no initialized feeder; scan the feeders first")

    async def feed(self, slot: Slot, distance: float | None = None) -> float:
        """Feed a slot's tape and wait for it. For use inside an operation. Returns the
        distance fed."""
        self.check_can_feed(slot)
        bus = self._need_bus()
        address = self._address(slot)
        mm = self.feed_distance(slot) if distance is None else distance
        try:
            await bus.feed(address, mm)
        except FeederError as e:
            self._errors[slot.id] = str(e)
            raise
        self._errors.pop(slot.id, None)
        if mm > 0:
            self._sim_full[address] = True
        self._invalidate()
        return mm

    def start_feed(self, slot_id: str, distance: float | None) -> Operation:
        slot = self.slot(slot_id)
        self._machine.check_can_actuate()
        self.check_can_feed(slot)

        async def work() -> str:
            mm = await self.feed(slot, distance)
            return f"{slot.label}: fed {mm:g} mm."

        return self._operations.start("feed", work)

    def part(self, slot: Slot) -> Part | None:
        """The library part in the feeder found in a slot, if it has one."""
        uuid = self._found.get(slot.id)
        return self._part(self._store.setup.feeders.get(uuid) if uuid else None)

    def bank(self, slot: Slot) -> FeederBank:
        return self._banks[slot.bank]

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
        self._store.setup.learned.pop(slot.id, None)
        self._store.save()
        return cal

    def forget_pick(self, slot_id: str) -> None:
        self._store.setup.slots.pop(self.slot(slot_id).id, None)
        self._store.setup.learned.pop(slot_id, None)
        self._store.save()

    # --- Fiducials -----------------------------------------------------------------------

    def _fiducial_vector(self, slot: Slot, offset: Point) -> Point:
        """From a slot's pick location to its feeder's fiducial, in machine coordinates.
        The offset is measured across from the sprocket holes, so the tape's pocket line
        is added to make it relative to the pocket."""
        line, _ = self._pocket_line(slot)
        return rotate(Point(x=offset.x, y=offset.y + line), slot.rotation)

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
            found = await self._machine.locate_circle(
                low, high, FIDUCIAL_SEARCH_MM, bank.fiducial_diameter
            )
            if found is None:
                raise FeederError(
                    "no fiducial found near the camera's center; center it within "
                    f"{FIDUCIAL_SEARCH_MM:g} mm"
                )
            pick = self.pick(slot)
            seen = rotate(Point(x=found.x - pick.x, y=found.y - pick.y), -slot.rotation)
            line, _ = self._pocket_line(slot)
            offset = Point(x=round(seen.x, 3), y=round(seen.y - line, 3))
            self._store.setup.fiducial_offset = offset
            self._store.save()
            taught = slot.id in self._store.setup.slots
            note = "" if taught else " The slot's pick location was not taught: check it."
            return (
                f"Feeder fiducial is {offset.x:+.2f} mm along the feed from the pick location "
                f"and {offset.y:+.2f} mm across from the sprocket holes.{note}"
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
                try:
                    await self.locate_feeder(slot)
                except FeederError:
                    failed.append(slot.label)
                    continue
                done.append(slot.label)
            message = f"Calibrated {len(done)} slot{'' if len(done) == 1 else 's'}."
            if failed:
                message += f" No fiducial found at: {', '.join(failed)}."
            return message

        return self._machine.start_task("feeder_calibrate", work)

    async def locate_feeder(self, slot: Slot) -> float:
        """Find the fiducial of the feeder in a slot, near where its last fix (or else its
        rail) puts it, and set the slot's pick location from it: the fiducial, less the
        feeder's design offset. The fix is kept with that feeder. Returns how far the pick
        location moved, in mm. For use inside a machine task."""
        bank = self._banks[slot.bank]
        offset, _ = self.fiducial_offset(bank)
        if offset is None:
            raise FeederError(
                "the feeder fiducial's offset is not known yet: teach one slot's pick "
                "location, then measure its fiducial"
            )
        turned = self._fiducial_vector(slot, offset)
        before = self.pick(slot)
        guess = Point(x=before.x + turned.x, y=before.y + turned.y)
        await self._machine.move_camera_to(guess.x, guess.y)
        low, high = self._fiducial_size(bank)
        found = await self._machine.locate_circle(
            low, high, FIDUCIAL_SEARCH_MM, bank.fiducial_diameter
        )
        if found is None:
            found = await self._search_fiducial(bank, guess)
        if found is None:
            self._errors[slot.id] = "feeder fiducial not found"
            raise FeederError(
                f"{slot.label}: its feeder's fiducial was not found near "
                f"({guess.x:.1f}, {guess.y:.1f}). Check that the feeder is seated, then "
                "calibrate its slot (or teach its pick location) and resume."
            )
        self._errors.pop(slot.id, None)
        pick = Point(x=found.x - turned.x, y=found.y - turned.y)
        feeder = self._found.get(slot.id)
        setup = self._store.setup
        old = setup.slots.get(slot.id)
        if old is None or old.basis != "fiducial" or old.feeder != feeder:
            # What the bottom camera learned was from another starting point.
            setup.learned.pop(slot.id, None)
        setup.slots[slot.id] = SlotCalibration(
            correction=Point(x=round(pick.x - slot.pick.x, 3), y=round(pick.y - slot.pick.y, 3)),
            basis="fiducial",
            feeder=feeder,
        )
        self._store.save()
        return math.hypot(pick.x - before.x, pick.y - before.y)

    # --- Rails -------------------------------------------------------------------------

    def _apply_rails(self) -> None:
        """Place the slots where their rails really are: the profile's places, moved by
        each rail's calibration."""
        rails = self._store.setup.rails
        slots: list[Slot] = []
        for nominal in self._nominal:
            rail = rails.get(nominal.bank)
            if rail is None or not rail.fitted:
                slots.append(nominal)
                continue
            slots.append(
                nominal.model_copy(
                    update={
                        "pick": rail.apply(nominal.pick),
                        "rotation": round(nominal.rotation + rail.angle, 4),
                    }
                )
            )
        self._slots = slots
        self._by_id = {s.id: s for s in slots}

    def _rail_state(self, bank: FeederBank) -> RailState:
        mine = [s for s in self._nominal if s.bank == bank.id]
        return RailState(
            bank=bank.id,
            label=bank.label,
            first_slot=mine[0].id,
            last_slot=mine[-1].id,
            calibration=self._store.setup.rails.get(bank.id),
        )

    def _bank(self, bank_id: str) -> FeederBank:
        bank = self._banks.get(bank_id)
        if bank is None:
            raise InvalidTargetError(f"no feeder rail {bank_id!r}")
        return bank

    def _expected_fiducial(self, slot: Slot) -> Point:
        """Where the profile's rail puts a slot's feeder fiducial."""
        nominal = self._nominal[slot.number - 1]
        offset, _ = self.fiducial_offset(self._banks[nominal.bank])
        assert offset is not None
        turned = self._fiducial_vector(nominal, offset)
        return Point(x=nominal.pick.x + turned.x, y=nominal.pick.y + turned.y)

    def _need_fiducial_offset(self, bank: FeederBank) -> None:
        if self.fiducial_offset(bank)[0] is None:
            raise FeederError(
                "the feeder fiducial's place on a feeder is not known: the machine profile "
                "does not give it. Teach one slot's pick location, then measure its fiducial"
            )

    def _min_span(self, bank: FeederBank) -> int:
        return max(2, round((bank.count - 1) * RAIL_MIN_SPAN))

    def start_calibrate_rails(self, bank_ids: list[str] | None = None) -> Operation:
        """Find where each rail really is: the fiducials of the two feeders farthest apart
        on it, and the profile's rail fitted to them. Every slot on the rail moves with
        it, and the rail's slot corrections and what was learned of its picks are
        cleared, since they were measured from the old places."""
        banks = [self._bank(b) for b in bank_ids] if bank_ids else list(self._profile.feeder_banks)
        if not banks:
            raise FeederError("this machine has no feeder rails")
        if not self._scanned and self._bus is not None:
            raise FeederError("scan the feeders first, so the core knows where they are")
        plans: list[tuple[FeederBank, list[Slot]]] = []
        for bank in banks:
            self._need_fiducial_offset(bank)
            mine = [s for s in self._nominal if s.bank == bank.id]
            fitted = [s for s in mine if s.id in self._found]
            if len(fitted) < 2 or fitted[-1].number - fitted[0].number < self._min_span(bank):
                raise FeederError(
                    f"the {bank.label.lower()} needs feeders in two slots far apart to find "
                    f"where it is: put feeders in {mine[0].label.lower()} and "
                    f"{mine[-1].label.lower()} (the ends), scan, and try again"
                )
            plans.append((bank, [fitted[0], fitted[-1]]))

        async def work() -> str:
            notes: list[str] = []
            missing: list[str] = []
            for bank, ends in plans:
                points: dict[str, RailPoint] = {}
                for slot in ends:
                    expected = self._expected_fiducial(slot)
                    rail = self._store.setup.rails.get(bank.id)
                    guess = rail.apply(expected) if rail is not None else expected
                    found = await self._search_fiducial(bank, guess)
                    if found is None:
                        missing.append(slot.label.lower())
                        self._errors[slot.id] = "feeder fiducial not found near its rail place"
                        continue
                    self._errors.pop(slot.id, None)
                    points[slot.id] = RailPoint(expected=expected, found=found, basis="found")
                notes.append(self._fit(bank, points))
            self._store.save()
            message = " ".join(notes)
            if missing:
                message += (
                    f" No feeder fiducial was found at {', '.join(missing)}: jog the camera "
                    "onto it and use Fiducial is here."
                )
            return message

        return self._machine.start_task("rail_calibrate", work)

    def start_rail_fiducial_here(self, slot_id: str) -> Operation:
        """The operator has centered the top camera on a slot's feeder fiducial (when the
        search did not find it): find it there and fit its rail again with it."""
        slot = self.slot(slot_id)
        bank = self._banks[slot.bank]
        self._need_fiducial_offset(bank)

        async def work() -> str:
            low, high = self._fiducial_size(bank)
            found = await self._machine.locate_circle(
                low, high, FIDUCIAL_SEARCH_MM, bank.fiducial_diameter
            )
            if found is None:
                raise FeederError(
                    "no feeder fiducial near the camera's center: center the camera on the "
                    f"small round dot on the feeder's nose, within {FIDUCIAL_SEARCH_MM:g} mm"
                )
            self._errors.pop(slot.id, None)
            rail = self._store.setup.rails.get(bank.id)
            points = dict(rail.points) if rail else {}
            points[slot.id] = RailPoint(
                expected=self._expected_fiducial(slot), found=found, basis="taught"
            )
            message = self._fit(bank, points)
            self._store.save()
            return message

        return self._machine.start_task("rail_fiducial", work)

    def forget_rail(self, bank_id: str) -> None:
        """Go back to the profile's place for a rail."""
        bank = self._bank(bank_id)
        if self._store.setup.rails.pop(bank.id, None) is not None:
            self._clear_bank(bank)
            self._store.save()
            self._apply_rails()
            self._invalidate()

    def _fit(self, bank: FeederBank, points: dict[str, RailPoint]) -> str:
        """Fit a rail to the points found on it, keep it, and move its slots. Returns
        what happened, for the operator."""
        numbers = sorted(int(s) for s in points)
        if len(numbers) < 2 or numbers[-1] - numbers[0] < self._min_span(bank):
            self._store.setup.rails[bank.id] = RailCalibration(points=points)
            return (
                f"{bank.label}: {len(points)} feeder fiducial{'' if len(points) == 1 else 's'} "
                "found, not enough far apart to place the rail yet."
            )
        rail = fit_rail(points)
        worst = residual(rail)
        if worst > RAIL_RESIDUAL_MM:
            raise FeederError(
                f"the {bank.label.lower()}'s feeder fiducials do not lie on one straight rail "
                f"(one is {worst:.1f} mm off): check that each feeder is seated, and that the "
                "camera was on the right fiducial"
            )
        self._store.setup.rails[bank.id] = rail
        self._clear_bank(bank)
        self._apply_rails()
        self._invalidate()
        moved = math.hypot(rail.shift.x, rail.shift.y)
        stretch = (rail.scale - 1) * 100
        return (
            f"{bank.label} placed: {moved:.2f} mm from the profile's place, turned "
            f"{rail.angle:+.2f} degrees, slots {abs(stretch):.2f}% "
            f"{'farther apart' if stretch >= 0 else 'closer'}. Its slots' corrections were "
            "cleared: calibrate them again."
        )

    def _clear_bank(self, bank: FeederBank) -> None:
        for slot in self._nominal:
            if slot.bank == bank.id:
                self._store.setup.slots.pop(slot.id, None)
                self._store.setup.learned.pop(slot.id, None)
                self._pockets.pop(slot.id, None)

    async def _search_fiducial(self, bank: FeederBank, guess: Point) -> Point | None:
        """Look for a feeder fiducial around where it is expected, frame by frame, nearest
        first. A fiducial more than half a slot along the rail from where it was expected
        is not taken: it may be the next feeder's."""
        length = math.hypot(bank.pitch.x, bank.pitch.y)
        ux, uy = (bank.pitch.x / length, bank.pitch.y / length) if length else (1.0, 0.0)
        steps = [
            (along, across)
            for along in (0.0, -RAIL_SEARCH_ALONG_MM, RAIL_SEARCH_ALONG_MM)
            for across in (0.0, -RAIL_SEARCH_ACROSS_MM, RAIL_SEARCH_ACROSS_MM)
        ]
        steps.sort(key=lambda s: math.hypot(*s))
        low, high = self._fiducial_size(bank)
        for along, across in steps:
            x = guess.x + ux * along - uy * across
            y = guess.y + uy * along + ux * across
            await self._machine.move_camera_to(x, y)
            found = await self._machine.locate_circle(
                low, high, RAIL_SEARCH_RADIUS_MM, bank.fiducial_diameter
            )
            if found is None:
                continue
            off_along = (found.x - guess.x) * ux + (found.y - guess.y) * uy
            if abs(off_along) <= length * 0.45:
                return found
        return None

    # --- Learning from the bottom camera ------------------------------------------------

    def learned(self, source_id: str) -> PickLearning | None:
        """What the bottom camera has learned of a slot's or a strip's or tray's picks. For a
        slot, only while the same feeder is in it."""
        learning = self._store.setup.learned.get(source_id)
        if learning is None or self.is_fixed(source_id):
            return learning
        return learning if learning.feeder == self._found.get(source_id) else None

    def learned_offset(self, source_id: str) -> Point:
        learning = self.learned(source_id)
        return learning.offset if learning is not None else Point(x=0, y=0)

    def learn_pick(
        self, source_id: str, base: Point, part_at: Point, miss: float, how: PickMethod
    ) -> PickLearning | None:
        """The bottom camera saw a part picked from ``source_id`` that lay at ``part_at``
        when it was picked, ``miss`` mm from the nozzle's axis; the setup put it at
        ``base``. Returns what is learned now, or None when the pick was too far off center
        to learn from."""
        if miss > LEARN_MAX_MISS_MM:
            return None
        setup = self._store.setup
        learning = self.learned(source_id)
        if learning is None:
            feeder = None if self.is_fixed(source_id) else self._found.get(source_id)
            learning = PickLearning(feeder=feeder, offset=Point(x=0, y=0), samples=0)
        seen = Point(x=part_at.x - base.x, y=part_at.y - base.y)
        n = learning.samples
        gain = 1 / (n + 1) if n < LEARN_FIRST else LEARN_GAIN
        x = learning.offset.x + gain * (seen.x - learning.offset.x)
        y = learning.offset.y + gain * (seen.y - learning.offset.y)
        size = math.hypot(x, y)
        if size > LEARN_MAX_OFFSET_MM:
            x, y = x * LEARN_MAX_OFFSET_MM / size, y * LEARN_MAX_OFFSET_MM / size
        stats = learning.misses.get(how, PickMisses())
        count = stats.count + 1
        mean = stats.mean + (miss - stats.mean) / min(count, MISS_WINDOW)
        misses = dict(learning.misses)
        misses[how] = PickMisses(count=count, mean=round(mean, 4), last=round(miss, 4))
        learning = learning.model_copy(
            update={
                "offset": Point(x=round(x, 4), y=round(y, 4)),
                "samples": n + 1,
                "misses": misses,
            }
        )
        setup.learned[source_id] = learning
        self._store.save()
        return learning

    def forget_learned(self, source_id: str) -> None:
        if not self.is_fixed(source_id):
            self.slot(source_id)
        self._store.setup.learned.pop(source_id, None)
        self._store.save()

    # --- Fixed feeders: strips and trays on the bed -----------------------------------

    def is_fixed(self, source_id: str) -> bool:
        return source_id in self._store.setup.fixed

    def fixed(self, fixed_id: str) -> FixedFeeder:
        f = self._store.setup.fixed.get(fixed_id)
        if f is None:
            raise InvalidTargetError(f"no strip or tray {fixed_id!r}")
        return f

    def fixed_label(self, fixed_id: str) -> str:
        f = self.fixed(fixed_id)
        return f"{fixed_id} ({f.label})" if f.label else fixed_id

    def add_fixed(self, feeder: FixedFeeder) -> str:
        taken = self._store.setup.fixed
        n = 1
        while f"T{n}" in taken:
            n += 1
        fixed_id = f"T{n}"
        self.put_fixed(fixed_id, feeder)
        return fixed_id

    def put_fixed(self, fixed_id: str, feeder: FixedFeeder) -> None:
        if fixed_id not in self._store.setup.fixed and not _is_fixed_id(fixed_id):
            raise InvalidTargetError(f"a strip or tray's ID is T and a number, not {fixed_id!r}")
        old = self._store.setup.fixed.get(fixed_id)
        if old is None or _geometry(old) != _geometry(feeder):
            self._store.setup.learned.pop(fixed_id, None)
        self._store.setup.fixed[fixed_id] = feeder
        self._store.save()
        self._sim_fixed_taken[fixed_id] = set(range(feeder.picked))
        self._pockets.pop(fixed_id, None)
        self._invalidate()

    def remove_fixed(self, fixed_id: str) -> None:
        self.fixed(fixed_id)
        del self._store.setup.fixed[fixed_id]
        self._store.setup.learned.pop(fixed_id, None)
        self._store.save()
        self._invalidate()

    def refill(self, fixed_id: str, picked: int = 0) -> FixedFeeder:
        """The operator refilled a strip or tray. ``picked``: parts already gone from the
        start of the new one (0 for a full one)."""
        f = self.fixed(fixed_id)
        if picked > f.capacity:
            raise InvalidTargetError(f"{fixed_id} holds only {f.capacity} parts")
        f = f.model_copy(update={"picked": picked})
        self._store.setup.fixed[fixed_id] = f
        self._store.save()
        self._sim_fixed_taken[fixed_id] = set(range(picked))
        self._pockets.pop(fixed_id, None)
        self._invalidate()
        return f

    def teach_fixed(
        self, fixed_id: str, which: Literal["first", "row_end", "last"], at: Point
    ) -> FixedFeeder:
        """Teach where a part is, by the camera centered over it: the first part, the
        last part of the first row (giving the step along a row), or the last part of
        the last row (giving the step from row to row)."""
        f = self.fixed(fixed_id)
        update: dict[str, Point]
        if which == "first":
            update = {"first": Point(x=round(at.x, 3), y=round(at.y, 3))}
        elif which == "row_end":
            if f.columns < 2:
                raise InvalidTargetError(f"{fixed_id} has one part per row: nothing to step")
            n = f.columns - 1
            update = {
                "step": Point(
                    x=round((at.x - f.first.x) / n, 4), y=round((at.y - f.first.y) / n, 4)
                )
            }
        else:
            if f.rows < 2:
                raise InvalidTargetError(f"{fixed_id} has one row: teach the end of the row")
            n, k = f.rows - 1, f.columns - 1
            update = {
                "row_step": Point(
                    x=round((at.x - f.first.x - k * f.step.x) / n, 4),
                    y=round((at.y - f.first.y - k * f.step.y) / n, 4),
                )
            }
        f = f.model_copy(update=update)
        self._store.setup.fixed[fixed_id] = f
        self._store.setup.learned.pop(fixed_id, None)
        self._store.save()
        self._invalidate()
        return f

    def fixed_part(self, f: FixedFeeder) -> Part | None:
        return self._part(FeederAssignment(part_id=f.part_id))

    def fixed_angle(self, f: FixedFeeder) -> float | None:
        """The machine angle of the parts' 0 degree direction."""
        if f.kind == "tray":
            return f.part_angle
        part = self.fixed_part(f)
        if part is None or part.tape is None:
            return None
        return _strip_direction(f) + part.tape.rotation

    def fixed_pick_height(self, f: FixedFeeder, label: str) -> float:
        if f.pick_height is not None:
            return f.pick_height
        board = self._profile.board_height
        part = self.fixed_part(f)
        if board is None or part is None or part.height is None:
            raise FeederError(
                f"{label} has no pick height: set one (the nozzle height at the top of a part)"
            )
        return board + part.height

    def next_fixed(self, fixed_id: str) -> tuple[int, Point]:
        """The next part to take from a strip or tray, and where it lies."""
        f = self.fixed(fixed_id)
        if not f.enabled:
            raise FeederError(f"{self.fixed_label(fixed_id)} is turned off")
        if f.remaining == 0:
            raise FeederEmptyError(
                f"{self.fixed_label(fixed_id)} is empty after {f.capacity} parts: refill it "
                "and mark it refilled, then carry on"
            )
        return f.picked, f.position(f.picked)

    def took_fixed(self, fixed_id: str) -> None:
        """A pick was tried at the next part: whether or not it came up, move past it."""
        f = self.fixed(fixed_id)
        self._store.setup.fixed[fixed_id] = f.model_copy(update={"picked": f.picked + 1})
        self._store.save()
        self._pockets.pop(fixed_id, None)
        self._invalidate()

    async def locate_strip_pocket(self, fixed_id: str) -> PocketCheck:
        """Find a strip's next pocket from its sprocket holes. For use inside a machine
        task."""
        f = self.fixed(fixed_id)
        _, at = self.next_fixed(fixed_id)
        part = self.fixed_part(f)
        width = part.tape.width if part and part.tape and part.tape.width else 8.0
        pitch = math.hypot(f.step.x, f.step.y) or DEFAULT_PITCH
        line = _pocket_line_for(width)
        # Look at the holes: on wide tape they are out of view from the pocket.
        check = await self._find_pocket(
            self.fixed_label(fixed_id), at, _strip_direction(f), line, pitch, look_across=line
        )
        self._pockets[fixed_id] = check
        return check

    def start_locate_strip_pocket(self, fixed_id: str) -> Operation:
        f = self.fixed(fixed_id)
        if f.kind != "strip":
            raise InvalidTargetError(f"{fixed_id} is a tray: trays have no sprocket holes")
        self.next_fixed(fixed_id)
        label = self.fixed_label(fixed_id)

        async def work() -> str:
            check = await self.locate_strip_pocket(fixed_id)
            await self._machine.move_camera_to(check.at.x, check.at.y)
            return (
                f"{label}: the next pocket is {check.along:+.2f} mm along the tape and "
                f"{check.across:+.2f} mm across from where it should be "
                f"(from {check.holes} sprocket holes)."
            )

        return self._machine.start_task("feeder_pocket", work)

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
        line, pitch = self._pocket_line(slot)
        try:
            check = await self._find_pocket(slot.label, self.pick(slot), slot.rotation, line, pitch)
        except FeederError:
            self._pockets.pop(slot.id, None)
            raise
        self._pockets[slot.id] = check
        return check

    async def _find_pocket(
        self,
        label: str,
        pick: Point,
        rotation: float,
        line: float,
        pitch: float,
        look_across: float = 0.0,
    ) -> PocketCheck:
        """``look_across``: center the camera this far across the tape from the pick
        location, toward the holes."""
        look = rotate(Point(x=0, y=look_across), rotation)
        await self._machine.move_camera_to(pick.x + look.x, pick.y + look.y)
        low, high = SPROCKET_DIAMETER * 0.7, SPROCKET_DIAMETER * 1.3
        seen = await self._machine.locate_circles(low, high)
        holes = [
            (q.x, q.y)
            for q in (rotate(Point(x=p.x - pick.x, y=p.y - pick.y), -rotation) for p in seen)
        ]
        fix = pocket_from_holes(holes, line, pitch)
        if fix is None:
            raise FeederError(f"{label}: no sprocket holes found near the pick location")
        shift = rotate(Point(x=fix.along, y=fix.across), rotation)
        return PocketCheck(
            at=Point(x=round(pick.x + shift.x, 3), y=round(pick.y + shift.y, 3)),
            along=round(fix.along, 3),
            across=round(fix.across, 3),
            holes=fix.holes,
        )

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
        return library.find_part(assignment.part_id)

    def _invalidate(self) -> None:
        self._tapes = None

    def _sim_true_pick(self, slot: Slot) -> Point:
        nominal = self._nominal[slot.number - 1]
        dx, dy, angle = self.sim_rail_errors.get(nominal.bank, (0.0, 0.0, 0.0))
        first = self._banks[nominal.bank].first_pick
        turned = rotate(Point(x=nominal.pick.x - first.x, y=nominal.pick.y - first.y), angle)
        error = sim_slot_error(slot.number)
        return Point(x=first.x + turned.x + dx + error.x, y=first.y + turned.y + dy + error.y)

    def _sim_true_rotation(self, slot: Slot) -> float:
        nominal = self._nominal[slot.number - 1]
        return nominal.rotation + self.sim_rail_errors.get(nominal.bank, (0.0, 0.0, 0.0))[2]

    def _take_from_tape(self, x: float, y: float) -> tuple[PartShape, float, float, float] | None:
        """The part in the pocket under (x, y), taken out of its tape: its shape, true
        center, and angle. None if no full pocket is there."""
        self._scene_tapes()
        for address, tape in self._tape_at.items():
            a = math.radians(tape.rotation)
            phase = tape.advance % tape.pitch
            along = phase if phase <= tape.pitch / 2 else phase - tape.pitch
            px = tape.pick[0] + math.cos(a) * along
            py = tape.pick[1] + math.sin(a) * along
            if math.hypot(px - x, py - y) > SIM_PICK_RADIUS:
                continue
            if not tape.pick_full:
                return None
            self._sim_full[address] = False
            self._invalidate()
            shape = PartShape(body=tape.body, pads=tape.pads)
            return shape, px, py, tape.rotation + tape.part_rotation
        return self._take_from_fixed(x, y)

    def _take_from_fixed(self, x: float, y: float) -> tuple[PartShape, float, float, float] | None:
        library = self._library()
        for fixed_id, f in self._store.setup.fixed.items():
            taken = self._sim_fixed_taken.setdefault(fixed_id, set(range(f.picked)))
            for n in range(f.capacity):
                at = f.position(n)
                px, py = at.x + SIM_FIXED_ERROR.x, at.y + SIM_FIXED_ERROR.y
                if math.hypot(px - x, py - y) > SIM_PICK_RADIUS:
                    continue
                if n in taken:
                    return None
                taken.add(n)
                self._invalidate()
                part = self.fixed_part(f)
                footprint = library.find_footprint(part.footprint_id) if part and library else None
                shape = PartShape(
                    body=(footprint.body.x, footprint.body.y)
                    if footprint is not None and footprint.body is not None
                    else None,
                    pads=tuple((q.x, q.y, q.width, q.height, q.rotation) for q in footprint.pads)
                    if footprint is not None
                    else (),
                )
                return shape, px, py, self.fixed_angle(f) or 0.0
        return None

    def _scene_tapes(self) -> tuple[TapeScene, ...]:
        if self._tapes is None:
            self._tapes = self._build_tapes()
        return self._tapes

    def _build_tapes(self) -> tuple[TapeScene, ...]:
        if self._sim is None:
            return ()
        library = self._library()
        tapes: list[TapeScene] = []
        self._tape_at = {}
        by_address = {s.address: s for s in self._nominal if s.address is not None}
        for address, feeder in sorted(self._sim.feeders.items()):
            slot = by_address.get(address)
            if slot is None:
                continue
            bank = self._banks[slot.bank]
            pick = self._sim_true_pick(slot)
            assignment = self._store.setup.feeders.get(feeder.uuid.hex().upper())
            part = self._part(assignment)
            footprint = (
                library.find_footprint(part.footprint_id) if part is not None and library else None
            )
            tape = part.tape if part is not None else None
            width = tape.width if tape is not None and tape.width else 8.0
            pitch = tape.pitch if tape is not None and tape.pitch else DEFAULT_PITCH
            offset = bank.fiducial_offset or SIM_FIDUCIAL_OFFSET
            line = _pocket_line_for(width)
            turn = self._sim_true_rotation(slot)
            fid = rotate(Point(x=offset.x, y=offset.y + line), turn)
            side = rotate(Point(x=0, y=SIM_FIDUCIAL_MARKS), turn)
            marks = tuple(
                (pick.x + fid.x + k * side.x, pick.y + fid.y + k * side.y) for k in (-1, 1)
            )
            tapes.append(
                TapeScene(
                    pick=(pick.x, pick.y),
                    rotation=turn,
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
                    fiducial_marks=marks,
                    feeder_ahead=bank.pick_to_front,
                    feeder_center=(PHOTON_ACROSS[0] + PHOTON_ACROSS[1]) / 2 + line,
                    window=(
                        PHOTON_WINDOW[0],
                        PHOTON_WINDOW[1],
                        PHOTON_WINDOW[2] + line,
                        PHOTON_WINDOW[3] + line,
                    ),
                    feeder_width=bank.width,
                    feeder_length=bank.length,
                )
            )
            self._tape_at[address] = tapes[-1]
        tapes.extend(self._build_strips())
        return tuple(tapes)

    def _build_strips(self) -> list[TapeScene]:
        """Strips on the bed, drawn from the next pocket on."""
        library = self._library()
        out: list[TapeScene] = []
        for fixed_id, f in self._store.setup.fixed.items():
            part = self.fixed_part(f)
            if f.kind != "strip" or f.remaining == 0 or part is None or part.tape is None:
                continue
            footprint = library.find_footprint(part.footprint_id) if library else None
            taken = self._sim_fixed_taken.setdefault(fixed_id, set(range(f.picked)))
            width = part.tape.width or 8.0
            pitch = math.hypot(f.step.x, f.step.y) or DEFAULT_PITCH
            line = _pocket_line_for(width)
            at = f.position(f.picked)
            length = (f.remaining * pitch + 8) / 0.8
            out.append(
                TapeScene(
                    pick=(at.x + SIM_FIXED_ERROR.x, at.y + SIM_FIXED_ERROR.y),
                    rotation=_strip_direction(f),
                    width=width,
                    pitch=pitch,
                    pocket_line=line,
                    pick_full=f.picked not in taken,
                    part_rotation=part.tape.rotation,
                    body=(footprint.body.x, footprint.body.y)
                    if footprint is not None and footprint.body is not None
                    else None,
                    pads=tuple((q.x, q.y, q.width, q.height, q.rotation) for q in footprint.pads)
                    if footprint is not None
                    else (),
                    feeder_ahead=8.0,
                    feeder_center=line + 1.75 - width / 2,
                    feeder_width=width + 6,
                    feeder_length=length,
                )
            )
        return out


def _strip_direction(f: FixedFeeder) -> float:
    """A strip's tape direction (the EIA-481 view's +X) in machine degrees: the tape
    would feed opposite to the step from one pocket to the next one to pick."""
    return math.degrees(math.atan2(-f.step.y, -f.step.x))


def _geometry(f: FixedFeeder) -> tuple[object, ...]:
    """What says where a strip's or tray's parts lie."""
    return (f.kind, f.first, f.step, f.columns, f.rows, f.row_step)


def _is_fixed_id(fixed_id: str) -> bool:
    return fixed_id[:1] == "T" and fixed_id[1:].isdigit() and int(fixed_id[1:]) > 0


def _fixed_order(fixed_id: str) -> int:
    return int(fixed_id[1:]) if _is_fixed_id(fixed_id) else 1_000_000


def _pocket_line_for(width: float) -> float:
    try:
        return geometry(width, DEFAULT_PITCH).pocket_line
    except ValueError:
        return geometry(8.0, DEFAULT_PITCH).pocket_line
