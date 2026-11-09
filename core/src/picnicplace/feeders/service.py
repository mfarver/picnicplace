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
from picnicplace.feeders.setup import (
    FeederAssignment,
    FeederSetupStore,
    FixedFeeder,
    PickCorrection,
    PickLearning,
    PickMisses,
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
            found = await self._machine.locate_circle(low, high, FIDUCIAL_SEARCH_MM)
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
                bank = self._banks[slot.bank]
                offset, _ = self.fiducial_offset(bank)
                assert offset is not None
                turned = self._fiducial_vector(slot, offset)
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
                self._store.setup.learned.pop(slot.id, None)
                done.append(slot.label)
            self._store.save()
            message = f"Calibrated {len(done)} slot{'' if len(done) == 1 else 's'}."
            if failed:
                message += f" No fiducial found at: {', '.join(failed)}."
            return message

        return self._machine.start_task("feeder_calibrate", work)

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
        self, source_id: str, base: Point, part_at: Point, miss: float, how: PickCorrection
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
        error = sim_slot_error(slot.number)
        return Point(x=slot.pick.x + error.x, y=slot.pick.y + error.y)

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
        by_address = {s.address: s for s in self._slots if s.address is not None}
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
            fid = rotate(Point(x=offset.x, y=offset.y + line), slot.rotation)
            side = rotate(Point(x=0, y=SIM_FIDUCIAL_MARKS), slot.rotation)
            marks = tuple(
                (pick.x + fid.x + k * side.x, pick.y + fid.y + k * side.y) for k in (-1, 1)
            )
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
