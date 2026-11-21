"""A simulated machine for development and tests. No hardware required.

Besides motion, it simulates the profile's I/O:

- Home sensors read true while their axis is at its home position.
- A safe-Z sensor, if the profile has one, reads true while every nozzle is at safe height.
- Vacuum sensors follow their nozzle's pump and valve. With both on, the reading rises
  toward an open-nozzle level, or a higher level while a part is held. A nozzle "picks" a
  part when suction is on while it is lowered at least ``PICK_DEPTH``, and drops it when
  suction goes off. With a ``pick_source`` (simulated parts, see ``simparts.py``) a
  nozzle only picks where there is a part; without one it picks anywhere. ``on_drop``
  reports where a part was let go. A nozzle lowered onto a surface (``surface_at``) with
  suction on seals, and reads as holding a part, as when sniffing for a height. Each
  nozzle's readings open and holding a part may be set (``vacuum_levels``), as for a tip
  that leaks or a weak pump.
- A tip whose face comes within ``WEAK_RIM_MM`` of a paper disc's edge (``rim_at``, for
  the tip the operator says they fitted) seals on it, but drops it once lifted
  ``SLIP_LIFT_MM``, as a real tip did with its rim near the edge. A nozzle may also be
  set to drop the next parts it picks the same way (``drops_on_lift``), as a worn tip
  or a part caught in its pocket would.
- Each nozzle's tip end may sit off the axis it turns about (``runout``, at 0 degrees,
  turning with it): it touches, picks and is seen there.
- Each nozzle's tip may be longer than the profile's (``tip_errors``), and each kind of
  tip too (``tip_kind_errors``, by tip ID, for the tip the operator says they fitted): it
  reaches that much lower than its axis says, for touching parts and surfaces. The safe-Z sensor
  reads the axis, not the tip.
- Photon feeders on the feeder bus (``feeders/sim.py``), if given.

Faults can be injected for tests: ``inject_z_slip`` makes the real Z position differ from
the commanded one, as if the motor lost steps. Sensors see the real position.
"""

import asyncio
import contextlib
import math
from collections.abc import Callable, Mapping, Sequence

from picnicplace.feeders.sim import SimulatedPhotonBus
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import MotionInterruptedError
from picnicplace.machine.profile import LightColor, MachineProfile, Nozzle, Point, nozzle_height

TICK_S = 0.05
"""Real-time interval between updates during a move or a vacuum change."""

HOME_DURATION_S = 1.0
"""Simulated time a homing cycle takes."""

HOME_TOLERANCE = 0.01
"""Home sensors read true within this distance of the home position."""

PICK_DEPTH = 5.0
"""How far below the top of travel a nozzle must be, with suction on, to pick a part."""

VACUUM_OPEN = 0.9
"""Suction on, nothing on the nozzle: this fraction of the way from the range start to the
pick threshold. Readings are in the profile's units, whatever they are."""

VACUUM_HELD = 0.5
"""Suction on, part held: this fraction of the way from the threshold to the range end."""

SETTLED_FRACTION = 0.0005
"""A vacuum reading this close to its target (as a fraction of the range) snaps to it."""

VACUUM_TAU_S = 0.15
"""Time constant of the simulated vacuum response."""

WEAK_RIM_MM = 0.5
"""A tip with less paper than this round its face seals on a disc but cannot lift it: a
real N24 tip (4 mm across) on a 4.9 mm disc, 0.45 mm of paper round it, dropped it."""

SLIP_LIFT_MM = 2.0
"""...and drops it once lifted this far from where it sealed: off the board, past any
press into the disc."""


def sim_profile(
    profile: MachineProfile, nozzle_errors: Mapping[str, tuple[float, float]]
) -> MachineProfile:
    """The machine as it really is in the simulator: the profile, with each nozzle off by
    its error. A copy, so calibrating the profile's nozzle offsets does not move them."""
    truth = profile.model_copy(deep=True)
    for nozzle in truth.nozzles:
        dx, dy = nozzle_errors.get(nozzle.id, (0.0, 0.0))
        nozzle.offset = Point(x=nozzle.offset.x + dx, y=nozzle.offset.y + dy)
    return truth


FIRST_TOUCH_MM = 0.1
"""How far into touching the bare datum board a small tip's seal is still partial."""


class SimulatorDriver(MotionDriver):
    name = "simulator"
    firmware = "PicnicPlace simulator"

    def __init__(
        self,
        profile: MachineProfile,
        time_scale: float = 1.0,
        home_error: tuple[float, float] = (0.0, 0.0),
        photon: SimulatedPhotonBus | None = None,
        tip_errors: Mapping[str, float] | None = None,
        vacuum_levels: Mapping[str, tuple[float, float]] | None = None,
        tip_kind_errors: Mapping[str, float] | None = None,
        runout: Mapping[str, tuple[float, float]] | None = None,
    ) -> None:
        super().__init__()
        self.photon = photon
        self.pick_source: Callable[[str, float, float, float, float], bool] | None = None
        """Asked, with the nozzle ID, its physical X/Y, its angle, and its tip's height,
        whether a part is there to pick (and to take it). None: there always is."""
        self.on_drop: Callable[[str, float, float, float, float], None] | None = None
        """Called with the nozzle ID, its physical X/Y, angle, and tip's height when it
        lets go of a part."""
        self._tip_errors = dict(tip_errors or {})
        """How much longer each nozzle's tip really is than the profile's, by ID: it
        reaches this much lower."""
        self._tip_kind_errors = dict(tip_kind_errors or {})
        """How much longer each kind of tip really is, by tip ID."""
        self._fitted: dict[str, str | None] = {}
        self._runout = dict(runout or {})
        """Where each nozzle's tip end is from its turning axis at 0 degrees, by ID."""
        """The kind of tip on each nozzle, as the operator said when they fitted it."""
        self._vacuum_levels = dict(vacuum_levels or {})
        """Each nozzle's readings open and holding a part (or sealed), by ID, as fractions
        of its sensor's range; the rest read as ``VACUUM_OPEN`` and ``VACUUM_HELD`` say."""
        self.rim_at: Callable[[float, float], float | None] | None = None
        """How far a physical X/Y is from the edge of the paper disc under it, if any."""
        self._slips: dict[str, float] = {}
        self.drops_on_lift: dict[str, int] = {}
        """How many of its next picks each nozzle drops once lifted, by ID."""
        """Nozzles holding a disc they cannot lift, by ID: the tip's height as it sealed."""
        self.surface_at: Callable[[float, float], float | None] | None = None
        self.datum_seal_press = 0.0
        self.bare_seal_tip_mm = 0.0
        """Tips with a face up to this size (mm) seal on the bare datum board at a touch,
        as small tips did on a real LumenPnP, however far bigger ones must be pressed."""
        self.bare_first_seal = 1.0
        """How far a small tip's seal on the bare datum board rises, as a fraction of the
        way from open to holding a part, in the first ``FIRST_TOUCH_MM`` of touching it: a
        real N08 rose 46% there, and fully 0.1 mm further in."""
        self.paper_lift: dict[str, float] = {}
        """How far (mm) a paper disc jumps up to each kind of tip, by tip ID, with suction
        on: a real N045 lifted a disc about a millimeter before it touched."""
        """How far a tip must be pressed into a bare surface before it seals."""
        """The height of a surface under a physical X/Y, if any, for a nozzle to seal on."""
        self._home_error = home_error
        self._offset = {"X": 0.0, "Y": 0.0}
        """Physical minus reported position: what homing gets wrong."""
        self._profile = profile
        self._time_scale = time_scale
        self._position = {a.name: 0.0 for a in profile.axes}
        self._outputs = {o.id: False for o in profile.outputs}
        self._vacuum = {n.vacuum_input: 0.0 for n in profile.nozzles if n.vacuum_input}
        self._held = {n.id: False for n in profile.nozzles}
        self._sealed = {n.id: False for n in profile.nozzles}
        self._first_touch = {n.id: False for n in profile.nozzles}
        self._z_slip: dict[str, float] = {}
        self._inputs_by_id = {i.id: i for i in profile.inputs}
        self._connected = False
        self._stop = asyncio.Event()
        self._io_wake = asyncio.Event()
        self._io_task: asyncio.Task[None] | None = None

    # --- State ---------------------------------------------------------------------------

    @property
    def time_scale(self) -> float:
        return self._time_scale

    @property
    def axes(self) -> Sequence[str]:
        return [a.name for a in self._profile.axes]

    @property
    def position(self) -> Mapping[str, float]:
        return dict(self._position)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def inputs(self) -> Mapping[str, bool | float]:
        values: dict[str, bool | float] = {}
        for i in self._profile.inputs:
            if i.id in self._vacuum:
                # Raw-count sensors read in whole numbers; small-range ones keep 2 places.
                span = abs(i.range.max - i.range.min) if i.range else 1.0
                values[i.id] = round(self._vacuum[i.id], 0 if span >= 1000 else 2)
            elif i.role == "home" and i.axis is not None:
                home = self._profile.axis(i.axis).home
                values[i.id] = abs(self._position[i.axis] - home) <= HOME_TOLERANCE
            elif i.role == "safe_z":
                values[i.id] = all(
                    self._height(n) >= self._profile.safe_z for n in self._profile.nozzles
                )
            else:
                values[i.id] = 0.0 if i.kind == "analog" else False
        return values

    @property
    def outputs(self) -> Mapping[str, bool]:
        return dict(self._outputs)

    @property
    def parts_held(self) -> Mapping[str, bool]:
        """Simulator only: which nozzles hold a part."""
        return dict(self._held)

    # --- Connection ----------------------------------------------------------------------

    async def connect(self) -> None:
        self._connected = True
        self._io_task = asyncio.create_task(self._io_loop())
        self._notify()

    async def disconnect(self) -> None:
        self._stop.set()
        if self._io_task is not None:
            self._io_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._io_task
            self._io_task = None
        self._connected = False
        self._notify()

    # --- Motion --------------------------------------------------------------------------

    async def home(self) -> None:
        """Home Z first, so X and Y home with the nozzles up, then everything else."""
        self._stop.clear()
        z_axes = {n.z_axis for n in self._profile.nozzles}
        for group in (
            [a for a in self._profile.axes if a.name in z_axes],
            [a for a in self._profile.axes if a.name not in z_axes],
        ):
            await self._sleep_interruptible(HOME_DURATION_S / 2 / self._time_scale)
            for axis in group:
                self._position[axis.name] = axis.home
                self._z_slip.pop(axis.name, None)
            if group and group[0].name not in z_axes:
                # Endstops are only so repeatable: the head really stops a little off.
                self._offset = {"X": self._home_error[0], "Y": self._home_error[1]}
            self._update_parts()
            self._notify()

    async def move_to(self, target: Mapping[str, float], speed: float | None) -> None:
        self._stop.clear()
        start = dict(self._position)
        end = {**start, **target}
        duration = 0.0
        for axis in self._profile.axes:
            axis_speed = axis.max_speed if speed is None else min(speed, axis.max_speed)
            duration = max(duration, abs(end[axis.name] - start[axis.name]) / axis_speed)
        duration /= self._time_scale

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while True:
            frac = 1.0 if duration == 0 else min(1.0, (loop.time() - t0) / duration)
            self._position = {a: start[a] + (end[a] - start[a]) * frac for a in start}
            self._update_parts()
            self._notify()
            if frac >= 1.0:
                return
            await self._sleep_interruptible(TICK_S)

    async def stop(self) -> None:
        self._stop.set()

    async def emergency_stop(self) -> None:
        self._stop.set()
        for output in self._outputs:
            self._outputs[output] = False
        self._update_parts()

    async def reset(self) -> None:
        self._stop.clear()

    @property
    def scene_position(self) -> Mapping[str, float]:
        return {a: v + self._offset.get(a, 0.0) for a, v in self._position.items()}

    async def set_position(self, position: Mapping[str, float]) -> None:
        for axis, value in position.items():
            # The head does not move: its physical position stays, the reported one changes.
            self._offset[axis] = self._offset.get(axis, 0.0) + self._position[axis] - value
            self._position[axis] = value
        self._notify()

    async def rs485(self, packet: bytes) -> bytes | None:
        if self.photon is None:
            return await super().rs485(packet)
        await asyncio.sleep(0.005 / self._time_scale)
        return self.photon.handle(packet)

    def inject_z_slip(self, axis: str, distance: float) -> None:
        """Simulator only: make the real position of a Z axis differ from the commanded one."""
        self._z_slip[axis] = self._z_slip.get(axis, 0.0) + distance
        self._update_parts()
        self._notify()

    # --- I/O -----------------------------------------------------------------------------

    async def set_output(self, output_id: str, on: bool) -> None:
        if output_id not in self._outputs:
            raise KeyError(output_id)
        self._outputs[output_id] = on
        self._light_colors.pop(output_id, None)
        self._update_parts()
        self._notify()

    async def set_light(self, output_id: str, color: LightColor) -> None:
        await self.set_output(output_id, color.brightness > 0)
        if color.brightness > 0:
            self._light_colors[output_id] = color

    def _height(self, nozzle: Nozzle) -> float:
        real_z = self._position[nozzle.z_axis] + self._z_slip.get(nozzle.z_axis, 0.0)
        return nozzle_height(nozzle, real_z)

    def _tip(self, nozzle: Nozzle) -> float:
        """Where the nozzle's tip really is: its height, less how much longer it is."""
        kind = self._fitted.get(nozzle.id)
        longer = self._tip_errors.get(nozzle.id, 0.0)
        longer += self._tip_kind_errors.get(kind, 0.0) if kind is not None else 0.0
        return self._height(nozzle) - longer

    def tip_fitted(self, nozzle_id: str, tip_id: str | None) -> None:
        self._fitted[nozzle_id] = tip_id

    def board_at(self, x: float, y: float) -> float | None:
        """The height of what lies under a physical X/Y: the datum board where it is, else
        a board on the staging plates, at the profile's height (the truth here)."""
        if self.surface_at is not None:
            surface = self.surface_at(x, y)
            if surface is not None:
                return surface
        return self._profile.board_height

    def _nozzle_at(self, nozzle: Nozzle) -> tuple[float, float, float]:
        """Where a nozzle's tip end physically is, and its angle."""
        at = self.scene_position
        angle = self._position.get(nozzle.rotation_axis, 0.0)
        rx, ry = self._turned_runout(nozzle)
        return at["X"] + nozzle.offset.x + rx, at["Y"] + nozzle.offset.y + ry, angle

    def _turned_runout(self, nozzle: Nozzle) -> tuple[float, float]:
        x, y = self._runout.get(nozzle.id, (0.0, 0.0))
        a = math.radians(self._position.get(nozzle.rotation_axis, 0.0))
        return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)

    @property
    def scene_runout(self) -> Mapping[str, tuple[float, float]]:
        return {n.id: self._turned_runout(n) for n in self._profile.nozzles if n.id in self._runout}

    def _suction(self, nozzle: Nozzle) -> bool:
        outputs = (nozzle.pump_output, nozzle.valve_output)
        return all(self._outputs[o] for o in outputs if o is not None) and any(
            o is not None for o in outputs
        )

    def _update_parts(self) -> None:
        """Apply the pick and drop rules, and wake the vacuum loop if anything changed."""
        for n in self._profile.nozzles:
            sealed_at = self._slips.get(n.id)
            slipped = sealed_at is not None and self._tip(n) > sealed_at + SLIP_LIFT_MM
            if not self._suction(n) or slipped:
                self._slips.pop(n.id, None)
                if self._held[n.id]:
                    self._held[n.id] = False
                    if self.on_drop is not None:
                        x, y, angle = self._nozzle_at(n)
                        # A slipping disc lets go where it slipped, however far the nozzle
                        # went on up before this update.
                        tip = self._tip(n)
                        if slipped and sealed_at is not None:
                            tip = sealed_at + SLIP_LIFT_MM
                        self.on_drop(n.id, x, y, angle, tip)
            elif self._height(n) <= -PICK_DEPTH and not self._held[n.id]:
                if self.pick_source is None:
                    self._held[n.id] = True
                else:
                    weak = self._weak_rim(n) or self.drops_on_lift.get(n.id, 0) > 0
                    self._held[n.id] = self.pick_source(
                        n.id, *self._nozzle_at(n), self._tip(n) - self._lift(n)
                    )
                    if self._held[n.id] and weak:
                        self._slips[n.id] = self._tip(n)
                        if self.drops_on_lift.get(n.id, 0) > 0:
                            self.drops_on_lift[n.id] -= 1
            sealed = first = False
            if self._suction(n) and not self._held[n.id] and self.surface_at is not None:
                x, y, _ = self._nozzle_at(n)
                surface = self.surface_at(x, y)
                if surface is not None:
                    reach = surface - self._seal_press(n)
                    sealed = self._tip(n) <= reach
                    first = sealed and self._tip(n) > reach - FIRST_TOUCH_MM
            self._sealed[n.id] = sealed
            self._first_touch[n.id] = first
        self._io_wake.set()

    def _lift(self, nozzle: Nozzle) -> float:
        """How far a paper disc under the nozzle jumps up to its tip (0 with none under)."""
        kind = self._fitted.get(nozzle.id)
        if kind is None or self.rim_at is None:
            return 0.0
        x, y, _ = self._nozzle_at(nozzle)
        return self.paper_lift.get(kind, 0.0) if self.rim_at(x, y) is not None else 0.0

    def _seal_press(self, nozzle: Nozzle) -> float:
        """How far the nozzle's tip must be pressed into the bare datum board to seal."""
        tip = self._profile.nozzle_tip(self._fitted.get(nozzle.id))
        face = tip.outer_diameter if tip is not None else None
        if face is not None and face <= self.bare_seal_tip_mm:
            return 0.0
        return self.datum_seal_press

    def _weak_rim(self, nozzle: Nozzle) -> bool:
        """Whether the nozzle's tip has too little paper round its face to lift the disc
        under it (False with no disc under it, or no tip said)."""
        if self.rim_at is None:
            return False
        tip = self._profile.nozzle_tip(self._fitted.get(nozzle.id))
        if tip is None or tip.outer_diameter is None:
            return False
        x, y, _ = self._nozzle_at(nozzle)
        rim = self.rim_at(x, y)
        return rim is not None and rim - tip.outer_diameter / 2 < WEAK_RIM_MM

    def _vacuum_target(self, nozzle: Nozzle) -> float:
        sensor = self._inputs_by_id.get(nozzle.vacuum_input or "")
        if sensor is None or sensor.range is None:
            return 0.0
        start, end = sensor.range.min, sensor.range.max
        threshold = sensor.threshold if sensor.threshold is not None else (start + end) / 2
        # The resting reading: zero when the range spans it, else the range start.
        rest = 0.0 if min(start, end) <= 0 <= max(start, end) else start
        if not self._suction(nozzle):
            return rest
        held = self._held[nozzle.id] or self._sealed[nozzle.id]
        if nozzle.id in self._vacuum_levels:
            lo, hi = self._vacuum_levels[nozzle.id]
            opened, holding = start + (end - start) * lo, start + (end - start) * hi
        else:
            opened = rest + (threshold - rest) * VACUUM_OPEN
            holding = threshold + (end - threshold) * VACUUM_HELD
        if not held:
            return opened
        if self._first_touch[nozzle.id] and not self._held[nozzle.id]:
            return opened + (holding - opened) * self.bare_first_seal
        return holding

    def _step_vacuum(self, dt: float) -> bool:
        """Advance the vacuum readings by ``dt`` simulated seconds. True if any changed."""
        changed = False
        alpha = min(1.0, dt / VACUUM_TAU_S)
        for n in self._profile.nozzles:
            if n.vacuum_input is None:
                continue
            current = self._vacuum[n.vacuum_input]
            target = self._vacuum_target(n)
            if current == target:
                continue
            new = current + (target - current) * alpha
            if abs(target - new) < SETTLED_FRACTION * self._span(n):
                new = target
            self._vacuum[n.vacuum_input] = new
            changed = True
        return changed

    def _span(self, nozzle: Nozzle) -> float:
        sensor = self._inputs_by_id.get(nozzle.vacuum_input or "")
        return abs(sensor.range.max - sensor.range.min) if sensor and sensor.range else 1.0

    async def _io_loop(self) -> None:
        while True:
            await self._io_wake.wait()
            self._io_wake.clear()
            while self._step_vacuum(TICK_S * self._time_scale):
                self._notify()
                await asyncio.sleep(TICK_S)

    async def _sleep_interruptible(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except TimeoutError:
            return
        raise MotionInterruptedError("motion was stopped")
