"""A simulated machine for development and tests. No hardware required.

Besides motion, it simulates the profile's I/O:

- Home sensors read true while their axis is at its home position.
- A safe-Z sensor, if the profile has one, reads true while every nozzle is at safe height.
- Vacuum sensors follow their nozzle's pump and valve. With both on, the reading rises
  toward an open-nozzle level, or a higher level while a part is held. A nozzle "picks" a
  part when suction is on while it is lowered at least ``PICK_DEPTH``, and drops it when
  suction goes off. There are no simulated parts or feeders yet, so this happens anywhere.

Faults can be injected for tests: ``inject_z_slip`` makes the real Z position differ from
the commanded one, as if the motor lost steps. Sensors see the real position.
"""

import asyncio
import contextlib
from collections.abc import Mapping, Sequence

from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import MotionInterruptedError
from picnicplace.machine.profile import MachineProfile, Nozzle, nozzle_height

TICK_S = 0.05
"""Real-time interval between updates during a move or a vacuum change."""

HOME_DURATION_S = 1.0
"""Simulated time a homing cycle takes."""

HOME_TOLERANCE = 0.01
"""Home sensors read true within this distance of the home position."""

PICK_DEPTH = 5.0
"""How far below the top of travel a nozzle must be, with suction on, to pick a part."""

VACUUM_OPEN = 12.0
"""Vacuum reading with suction on and nothing on the nozzle."""

VACUUM_HELD = 55.0
"""Vacuum reading with suction on and a part on the nozzle."""

VACUUM_TAU_S = 0.15
"""Time constant of the simulated vacuum response."""


class SimulatorDriver(MotionDriver):
    name = "simulator"

    def __init__(self, profile: MachineProfile, time_scale: float = 1.0) -> None:
        super().__init__()
        self._profile = profile
        self._time_scale = time_scale
        self._position = {a.name: 0.0 for a in profile.axes}
        self._outputs = {o.id: False for o in profile.outputs}
        self._vacuum = {n.vacuum_input: 0.0 for n in profile.nozzles if n.vacuum_input}
        self._held = {n.id: False for n in profile.nozzles}
        self._z_slip: dict[str, float] = {}
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
                values[i.id] = round(self._vacuum[i.id], 2)
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

    def _suction(self, nozzle: Nozzle) -> bool:
        outputs = (nozzle.pump_output, nozzle.valve_output)
        return all(self._outputs[o] for o in outputs if o is not None) and any(
            o is not None for o in outputs
        )

    def _update_parts(self) -> None:
        """Apply the pick and drop rules, and wake the vacuum loop if anything changed."""
        for n in self._profile.nozzles:
            if not self._suction(n):
                self._held[n.id] = False
            elif self._height(n) <= -PICK_DEPTH:
                self._held[n.id] = True
        self._io_wake.set()

    def _vacuum_target(self, nozzle: Nozzle) -> float:
        if not self._suction(nozzle):
            return 0.0
        return VACUUM_HELD if self._held[nozzle.id] else VACUUM_OPEN

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
            if abs(target - new) < 0.05:
                new = target
            self._vacuum[n.vacuum_input] = new
            changed = True
        return changed

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
