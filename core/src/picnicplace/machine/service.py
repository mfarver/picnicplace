"""The machine service: the one owner of the machine, and where safety rules live.

Clients never reach a driver directly. Every motion request passes through here, where
presence, E-stop, homing, busy state, soft limits, and safe Z are checked (spec section
5.2). The control lease is checked by the API before a request reaches this service.

Axes come in three groups, derived from the profile:

- **Z axes** move nozzles up and down (each nozzle's ``z_axis``).
- **Travel axes** are the other linear axes (X and Y): they move the head over the bed.
- **Rotary axes** turn nozzles.

Travel-axis motion is only allowed with every nozzle at safe Z. Moves that include travel
run in up to three steps: raise Z to travel height (if needed), move the travel and rotary
axes, then lower Z to its target (if one was given).
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping

from picnicplace.camera.synthetic import SceneState
from picnicplace.events import EventBus
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import (
    ConnectFailedError,
    EmergencyStopActiveError,
    InvalidTargetError,
    MachineBusyError,
    MachineError,
    NoClientPresentError,
    NotConnectedError,
    NotHomedError,
    SafeZFaultError,
    UnknownOutputError,
    ZNotSafeError,
)
from picnicplace.machine.profile import MachineProfile, nozzle_height
from picnicplace.operations import OperationManager
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import MachineState, MachineStateEvent, Operation

log = logging.getLogger(__name__)

_Work = Callable[[], Awaitable[None]]

EPSILON = 1e-6
"""Axis positions closer than this count as equal."""

TRAVEL_HEIGHT = 0.0
"""Z axis position used for travel: every nozzle at the top of its travel."""


class MachineService:
    def __init__(
        self,
        profile: MachineProfile,
        driver: MotionDriver,
        bus: EventBus,
        presence: PresenceTracker,
        operations: OperationManager,
        *,
        speed_scale: float = 1.0,
    ) -> None:
        self._speed_scale = speed_scale
        self._profile = profile
        self._axes = {a.name: a for a in profile.axes}
        self._inputs = {i.id: i for i in profile.inputs}
        self._z_axes = {n.z_axis for n in profile.nozzles}
        self._travel_axes = {
            a.name for a in profile.axes if a.kind == "linear" and a.name not in self._z_axes
        }
        self._driver = driver
        self._bus = bus
        self._presence = presence
        self._operations = operations
        self._homed = False
        self._estop = False
        self._busy = False
        self._connection_error: str | None = None
        self._connect_lock = asyncio.Lock()

        driver.on_change = self._on_driver_change
        presence.on_change.append(self._publish_state)
        presence.on_empty.append(self.stop_manual_motion)

    # --- State ---------------------------------------------------------------------------

    @property
    def profile(self) -> MachineProfile:
        return self._profile

    @property
    def driver(self) -> MotionDriver:
        return self._driver

    @property
    def z_safe_computed(self) -> bool:
        """Every nozzle at or above safe Z, according to the Z positions."""
        position = self._driver.position
        return all(
            nozzle_height(n, position[n.z_axis]) >= self._profile.safe_z
            for n in self._profile.nozzles
        )

    @property
    def z_safe(self) -> bool:
        """Whether every nozzle is at safe travel height.

        A safe-Z input, when the profile has one, is authoritative (spec section 5.2).
        Otherwise this is computed from the Z positions.
        """
        if self._profile.safe_z_input is not None:
            return bool(self._driver.inputs.get(self._profile.safe_z_input, False))
        return self.z_safe_computed

    def scene(self) -> SceneState:
        """What the synthetic cameras draw: position, outputs, and which nozzles hold a
        part (vacuum past the pick threshold). Positions are rounded so tiny changes do
        not cause new frames."""
        inputs = self._driver.inputs
        held: dict[str, bool] = {}
        for nozzle in self._profile.nozzles:
            sensor = self._inputs.get(nozzle.vacuum_input or "")
            value = inputs.get(sensor.id) if sensor else None
            held[nozzle.id] = (
                sensor is not None
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and sensor.past_threshold(float(value))
            )
        return SceneState(
            position={a: round(v, 2) for a, v in self._driver.position.items()},
            outputs=dict(self._driver.outputs),
            held=held,
        )

    @property
    def state(self) -> MachineState:
        return MachineState(
            driver=self._driver.name,
            firmware=self._driver.firmware,
            connected=self._driver.connected,
            connection_error=self._connection_error,
            homed=self._homed,
            estop=self._estop,
            busy=self._busy,
            motion_enabled=self._driver.connected and self._presence.present and not self._estop,
            axes=list(self._driver.axes),
            position=dict(self._driver.position),
            inputs=dict(self._driver.inputs),
            outputs=dict(self._driver.outputs),
            z_safe=self.z_safe,
            speed_scale=self._speed_scale,
            warnings=list(self._driver.warnings),
        )

    async def start(self) -> None:
        """Connect at startup. A machine that is off or unplugged is not fatal: the core
        runs, reports the error, and clients can try again with ``connect``."""
        with contextlib.suppress(MachineError):
            await self.connect()

    async def connect(self) -> None:
        async with self._connect_lock:  # startup and a client may both ask at once
            await self._connect_once()

    async def _connect_once(self) -> None:
        if self._driver.connected:
            return
        self._homed = False
        try:
            await self._driver.connect()
        except Exception as e:
            self._connection_error = _describe_error(e)
            log.error("cannot connect to the machine: %s", self._connection_error)
            self._publish_state()
            raise ConnectFailedError(self._connection_error) from e
        self._connection_error = None
        log.info("connected to %s driver", self._driver.name)
        self._publish_state()

    async def shutdown(self) -> None:
        await self._driver.disconnect()

    # --- Commands ------------------------------------------------------------------------

    def start_home(self) -> Operation:
        self._check_can_move()
        return self._start_operation("home", self._home)

    def start_move(self, target: Mapping[str, float], speed: float | None) -> Operation:
        """Move to an absolute position, at safe Z if X or Y change."""
        self._check_can_move()
        self._check_homed()
        self._check_target(target)
        steps = self._plan_move(target)
        return self._start_operation("move", lambda: self._run_steps(steps, speed))

    def start_jog(self, axis: str, distance: float, speed: float | None) -> Operation:
        """Move one axis by a relative distance, clamped to its limits.

        Unlike a move, a jog never raises Z on its own: jogging X or Y with a nozzle below
        safe Z is refused, so the operator decides when Z moves.
        """
        self._check_can_move()
        self._check_homed()
        limits = self._axes.get(axis)
        if limits is None:
            raise InvalidTargetError(f"unknown axis {axis!r}")
        if axis in self._travel_axes:
            self._check_travel_allowed()
        current = self._driver.position[axis]
        target = current + distance
        if not limits.continuous:
            # Clamp to the limits, but never move against the jog's direction: after
            # homing outside the limits (LumenPnP Y homes at 0, below its 44.45 minimum),
            # a jog away from the range does nothing, and one toward it is allowed.
            if distance < 0:
                target = max(target, min(limits.min, current))
            else:
                target = min(target, max(limits.max, current))
        return self._start_operation("jog", lambda: self._run_steps([{axis: target}], speed))

    async def set_output(self, output_id: str, on: bool) -> None:
        """Switch an output. Turning one off is allowed whenever the machine is connected."""
        if output_id not in self._driver.outputs:
            raise UnknownOutputError(f"unknown output {output_id!r}")
        if on:
            self._check_can_actuate()
        elif not self._driver.connected:
            raise NotConnectedError("the machine is not connected")
        await self._driver.set_output(output_id, on)

    async def emergency_stop(self) -> None:
        """Always allowed, from any client, in any state."""
        log.warning("emergency stop")
        self._estop = True
        self._homed = False
        await self._driver.emergency_stop()
        self._publish_state()

    async def reset(self) -> None:
        if not self._estop:
            return
        await self._driver.reset()
        self._estop = False
        self._publish_state()

    def _on_driver_change(self) -> None:
        # A machine that drops off (unplugged, powered off) must be homed again.
        if not self._driver.connected:
            self._homed = False
        # Firmware that halts by itself (a kill() for an endstop or thermal fault) is
        # treated as a latched emergency stop, so Reset can recover it.
        if self._driver.halted and not self._estop:
            log.warning("the controller halted; latching the emergency stop")
            self._estop = True
            self._homed = False
        self._publish_state()

    async def stop_manual_motion(self) -> None:
        """Stop the current manual move, keeping position (the controlling client left)."""
        if self._busy:
            log.warning("controlling client left during motion; stopping")
            await self._driver.stop()

    # --- Checks --------------------------------------------------------------------------

    def _check_can_actuate(self) -> None:
        if not self._driver.connected:
            raise NotConnectedError("the machine is not connected")
        if self._estop:
            raise EmergencyStopActiveError("emergency stop is active; reset first")
        if not self._presence.present:
            raise NoClientPresentError("motion requires a connected client")

    def _check_can_move(self) -> None:
        self._check_can_actuate()
        if self._busy:
            raise MachineBusyError("the machine is busy")

    def _check_homed(self) -> None:
        if not self._homed:
            raise NotHomedError("home the machine before moving")

    def _check_target(self, target: Mapping[str, float]) -> None:
        for name, value in target.items():
            axis = self._axes.get(name)
            if axis is None:
                raise InvalidTargetError(f"unknown axis {name!r}")
            if not axis.continuous and not axis.min <= value <= axis.max:
                raise InvalidTargetError(
                    f"{name}={value:g} is outside the limits {axis.min:g}..{axis.max:g}"
                )

    def _check_travel_allowed(self) -> None:
        """Refuse X/Y motion unless every nozzle is at safe Z.

        With a safe-Z sensor, a sensor reading "not safe" while the Z positions say safe
        means Z has lost steps: that is a fault, and the machine must be homed again.
        """
        computed = self.z_safe_computed
        if self._profile.safe_z_input is not None and not self.z_safe:
            if computed:
                self._homed = False
                self._publish_state()
                raise SafeZFaultError(
                    "the safe-Z sensor reads not safe although Z is at a safe height; "
                    "Z may have lost steps. Home the machine again."
                )
            raise ZNotSafeError("raise Z to a safe height before moving X or Y")
        if not computed:
            raise ZNotSafeError("raise Z to a safe height before moving X or Y")

    # --- Motion --------------------------------------------------------------------------

    def _plan_move(self, target: Mapping[str, float]) -> list[dict[str, float]]:
        position = self._driver.position
        travels = any(
            abs(value - position[axis]) > EPSILON
            for axis, value in target.items()
            if axis in self._travel_axes
        )
        if not travels:
            return [dict(target)]
        steps: list[dict[str, float]] = []
        if not self.z_safe:
            steps.append({axis: TRAVEL_HEIGHT for axis in sorted(self._z_axes)})
        steps.append({a: v for a, v in target.items() if a not in self._z_axes})
        lower = {a: v for a, v in target.items() if a in self._z_axes}
        if lower:
            steps.append(lower)
        return steps

    async def _run_steps(self, steps: list[dict[str, float]], speed: float | None) -> None:
        for step in steps:
            position = self._driver.position
            travels = any(
                abs(value - position[axis]) > EPSILON
                for axis, value in step.items()
                if axis in self._travel_axes
            )
            if travels:
                if self._profile.safe_z_input is not None:
                    await self._driver.refresh_input(self._profile.safe_z_input)
                self._check_travel_allowed()
            await self._driver.move_to(step, self._limit_speed(step, speed))

    def _limit_speed(self, step: Mapping[str, float], speed: float | None) -> float | None:
        """Apply the speed scale: no faster than that fraction of the slowest moving axis."""
        if self._speed_scale >= 1:
            return speed
        cap = min(self._axes[a].max_speed for a in step) * self._speed_scale
        return cap if speed is None else min(speed, cap)

    async def _home(self) -> None:
        await self._driver.home()
        if self._profile.safe_z_input is not None and not self.z_safe:
            raise SafeZFaultError(
                "after homing, the safe-Z sensor reads not safe; check the sensor and the "
                "Z mechanism"
            )
        self._homed = True

    def _start_operation(self, kind: str, work: _Work) -> Operation:
        self._busy = True
        self._publish_state()

        async def run() -> None:
            try:
                await work()
            finally:
                self._busy = False
                self._publish_state()

        return self._operations.start(kind, run)

    def _publish_state(self) -> None:
        self._bus.publish(MachineStateEvent(machine=self.state))


def _describe_error(e: Exception) -> str:
    if isinstance(e, FileNotFoundError):
        return f"no such serial port: {e.filename}"
    if isinstance(e, PermissionError):
        return f"no permission to open {e.filename}; add your user to the dialout group"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e) or type(e).__name__
