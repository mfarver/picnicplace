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
  suction on seals, and reads as holding a part, as when sniffing for a height.
- Each nozzle's tip may be longer than the profile's (``tip_errors``): it reaches that
  much lower than its axis says, for touching parts and surfaces. The safe-Z sensor
  reads the axis, not the tip.
- Photon feeders on the feeder bus (``feeders/sim.py``), if given.

Faults can be injected for tests: ``inject_z_slip`` makes the real Z position differ from
the commanded one, as if the motor lost steps. Sensors see the real position.
"""

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence

from picnicplace.feeders.sim import SimulatedPhotonBus
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import MotionInterruptedError
from picnicplace.machine.profile import MachineProfile, Nozzle, Point, nozzle_height

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
        self.surface_at: Callable[[float, float], float | None] | None = None
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
        self._z_slip: dict[str, float] = {}
        self._inputs_by_id = {i.id: i for i in profile.inputs}
        self._connected = False
        self._stop = asyncio.Event()
        self._io_wake = asyncio.Event()
        self._io_task: asyncio.Task[None] | None = None

    # --- State ---------------------------------------------------------------------------

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
        self._update_parts()
        self._notify()

    def _height(self, nozzle: Nozzle) -> float:
        real_z = self._position[nozzle.z_axis] + self._z_slip.get(nozzle.z_axis, 0.0)
        return nozzle_height(nozzle, real_z)

    def _tip(self, nozzle: Nozzle) -> float:
        """Where the nozzle's tip really is: its height, less how much longer it is."""
        return self._height(nozzle) - self._tip_errors.get(nozzle.id, 0.0)

    def board_at(self, x: float, y: float) -> float | None:
        """The height of what lies under a physical X/Y: the datum board where it is, else
        a board on the staging plates, at the profile's height (the truth here)."""
        if self.surface_at is not None:
            surface = self.surface_at(x, y)
            if surface is not None:
                return surface
        return self._profile.board_height

    def _nozzle_at(self, nozzle: Nozzle) -> tuple[float, float, float]:
        """Where a nozzle physically is, and its angle."""
        at = self.scene_position
        angle = self._position.get(nozzle.rotation_axis, 0.0)
        return at["X"] + nozzle.offset.x, at["Y"] + nozzle.offset.y, angle

    def _suction(self, nozzle: Nozzle) -> bool:
        outputs = (nozzle.pump_output, nozzle.valve_output)
        return all(self._outputs[o] for o in outputs if o is not None) and any(
            o is not None for o in outputs
        )

    def _update_parts(self) -> None:
        """Apply the pick and drop rules, and wake the vacuum loop if anything changed."""
        for n in self._profile.nozzles:
            if not self._suction(n):
                if self._held[n.id]:
                    self._held[n.id] = False
                    if self.on_drop is not None:
                        x, y, angle = self._nozzle_at(n)
                        self.on_drop(n.id, x, y, angle, self._tip(n))
            elif self._height(n) <= -PICK_DEPTH and not self._held[n.id]:
                if self.pick_source is None:
                    self._held[n.id] = True
                else:
                    self._held[n.id] = self.pick_source(n.id, *self._nozzle_at(n), self._tip(n))
            sealed = False
            if self._suction(n) and not self._held[n.id] and self.surface_at is not None:
                x, y, _ = self._nozzle_at(n)
                surface = self.surface_at(x, y)
                sealed = surface is not None and self._tip(n) <= surface
            self._sealed[n.id] = sealed
        self._io_wake.set()

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
        if self._held[nozzle.id] or self._sealed[nozzle.id]:
            return threshold + (end - threshold) * VACUUM_HELD
        return rest + (threshold - rest) * VACUUM_OPEN

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
