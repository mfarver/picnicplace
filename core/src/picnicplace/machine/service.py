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
import math
import time
from collections.abc import Awaitable, Callable, Mapping

from picnicplace.camera.source import Frame
from picnicplace.camera.synthetic import SceneState, TapeScene
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
    VisionError,
    ZNotSafeError,
)
from picnicplace.machine.profile import (
    Camera,
    Location,
    MachineProfile,
    Nozzle,
    Point,
    nozzle_height,
)
from picnicplace.operations import OperationManager
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import MachineState, MachineStateEvent, Operation
from picnicplace.simparts import HeldPart, PlacedPart
from picnicplace.vision.fiducial import find_circle, find_circles

log = logging.getLogger(__name__)

_Work = Callable[[], Awaitable[str | None]]

Grab = Callable[[str, float, float], Awaitable[Frame | None]]
"""Get a frame from a camera captured at or after a time: (camera ID, after, wait_s)."""

VISUAL_TOLERANCE_MM = 0.02
"""Visual homing is done when the fiducial is this close to the camera center."""

VISUAL_PASSES = 4

VISUAL_SEARCH_RADIUS_MM = 4.0
"""Only accept a fiducial this close to where it should be. Endstop homing is far more
repeatable than this, and on the LumenPnP datum board the nearest other fiducial is
more than 11 mm away, so a wrong fiducial is never mistaken for the homing one."""

FRAME_WAIT_S = 5.0

CAMERA_SCALE_STEP_MM = 1.0
"""How far the camera scale check moves each way from the fiducial."""

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
        self._grab: Grab | None = None
        self.scene_tapes: Callable[[], tuple[TapeScene, ...]] | None = None
        """Simulated feeder tapes for the synthetic cameras, if any."""
        self.scene_parts: (
            Callable[[], tuple[tuple[tuple[str, HeldPart], ...], tuple[PlacedPart, ...]]] | None
        ) = None
        """Simulated parts held by nozzles and placed on boards, if any."""

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
        held_parts, placed = self.scene_parts() if self.scene_parts is not None else ((), ())
        return SceneState(
            position={a: round(v, 3) for a, v in self._driver.scene_position.items()},
            outputs=dict(self._driver.outputs),
            held=held,
            tapes=self.scene_tapes() if self.scene_tapes is not None else None,
            held_parts=held_parts,
            placed=placed,
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

    def attach_cameras(self, grab: Grab) -> None:
        """Give the service a way to get camera frames, for visual homing."""
        self._grab = grab

    def start_visual_home(self) -> Operation:
        """Run visual homing again on a homed machine (to check repeatability)."""
        self._check_can_move()
        self._check_homed()
        return self._start_operation("visual_home", self._visual_home)

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

    # --- For other services (feeders, calibration) -----------------------------------------

    def check_can_actuate(self) -> None:
        """Raise unless outputs and feeders may be used now."""
        self._check_can_actuate()

    def start_task(self, kind: str, work: _Work) -> Operation:
        """Run work that moves the machine as an operation, with the machine busy."""
        self._check_can_move()
        self._check_homed()
        return self._start_operation(kind, work)

    async def frame(self, camera_id: str, settle: bool = True) -> Frame:
        """A frame taken now (after the camera's settle time, if asked)."""
        camera = next((c for c in self._profile.cameras if c.id == camera_id), None)
        if camera is None:
            raise VisionError(f"no camera {camera_id!r}")
        if self._grab is None:
            raise VisionError("the cameras are not available")
        if settle:
            await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera_id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        return frame

    # --- Nozzles, for tasks such as picking and placing ----------------------------------

    def nozzle(self, nozzle_id: str) -> Nozzle:
        n = next((n for n in self._profile.nozzles if n.id == nozzle_id), None)
        if n is None:
            raise InvalidTargetError(f"no nozzle {nozzle_id!r}")
        return n

    def nozzle_angle(self, nozzle: Nozzle) -> float:
        return self._driver.position.get(nozzle.rotation_axis, 0.0)

    def nozzle_position(self, nozzle: Nozzle) -> Point:
        here = self._driver.position
        return Point(x=here["X"] + nozzle.offset.x, y=here["Y"] + nozzle.offset.y)

    async def move_axes(self, target: Mapping[str, float]) -> None:
        """Move to an absolute position, at safe Z if X or Y change. For use in a task."""
        self._check_target(target)
        await self._run_steps(self._plan_move(target), None)

    async def move_nozzle_over(
        self, nozzle: Nozzle, x: float, y: float, angle: float | None = None
    ) -> None:
        """Put a nozzle over a point (at safe Z), turned to ``angle`` if given."""
        target = {"X": x - nozzle.offset.x, "Y": y - nozzle.offset.y}
        if angle is not None:
            target[nozzle.rotation_axis] = angle
        await self.move_axes(target)

    async def lower_nozzle(self, nozzle: Nozzle, height: float) -> None:
        """Move a nozzle down (or up) to a height: 0 at the top, negative down."""
        await self.move_axes({nozzle.z_axis: nozzle.z_sign * min(0.0, height)})

    async def raise_nozzles(self) -> None:
        await self.move_axes({axis: TRAVEL_HEIGHT for axis in sorted(self._z_axes)})

    async def suction(self, nozzle: Nozzle, on: bool) -> None:
        """Pump and valve on together, or both off (which lets go of a part)."""
        outputs = [o for o in (nozzle.pump_output, nozzle.valve_output) if o is not None]
        for output in outputs if on else reversed(outputs):
            await self._driver.set_output(output, on)

    async def holds_part(self, nozzle: Nozzle, expect: bool, timeout_s: float) -> bool | None:
        """Wait up to ``timeout_s`` for the vacuum to say whether a part is held (until
        it says ``expect``). None if the nozzle has no vacuum sensor."""
        sensor = self._inputs.get(nozzle.vacuum_input or "")
        if sensor is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            await self._driver.refresh_input(sensor.id)
            value = self._driver.inputs.get(sensor.id)
            held = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and sensor.past_threshold(float(value))
            )
            if held == expect or loop.time() >= deadline:
                return held
            await asyncio.sleep(0.03)

    def vacuum_reading(self, nozzle: Nozzle) -> float | None:
        value = self._driver.inputs.get(nozzle.vacuum_input or "")
        return float(value) if isinstance(value, (int, float)) else None

    async def set_light(self, camera: Camera, on: bool) -> None:
        if camera.light_output is not None:
            await self._driver.set_output(camera.light_output, on)

    def image_to_machine(self, camera: Camera, right: float, down: float) -> tuple[float, float]:
        """A displacement seen in a camera's image (mm right and mm down, as shown) as a
        machine X/Y displacement. Up-looking cameras see the machine mirrored left to
        right, as from below."""
        x, y = self._profile.image_to_machine(right, down)
        return (-x, y) if camera.looking == "up" else (x, y)

    def top_camera(self) -> Camera:
        camera = next(
            (c for c in self._profile.cameras if c.mount == "head" and c.looking == "down"),
            None,
        )
        if camera is None:
            raise VisionError("the machine profile has no down-looking head camera")
        return camera

    def camera_position(self) -> Point:
        """Where the top camera is looking, in machine coordinates."""
        camera = self.top_camera()
        here = self._driver.position
        return Point(x=here["X"] + camera.position.x, y=here["Y"] + camera.position.y)

    async def move_camera_to(self, x: float, y: float) -> None:
        """Center the top camera over a point, at safe Z. For use inside a task."""
        camera = self.top_camera()
        target = {"X": x - camera.position.x, "Y": y - camera.position.y}
        self._check_target(target)
        await self._run_steps(self._plan_move(target), None)

    async def locate_circles(self, min_diameter: float, max_diameter: float) -> list[Point]:
        """Every round mark the top camera sees: their machine positions."""
        camera = self.top_camera()
        if self._grab is None:
            raise VisionError("the cameras are not available")
        await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera.id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        circles = await asyncio.get_running_loop().run_in_executor(
            None, find_circles, frame.image, min_diameter / mm_per_px, max_diameter / mm_per_px
        )
        here = self.camera_position()
        out: list[Point] = []
        for c in circles:
            dx, dy = self._profile.image_to_machine(
                (c.x - w / 2) * mm_per_px, (c.y - h / 2) * mm_per_px
            )
            out.append(Point(x=here.x + dx, y=here.y + dy))
        return out

    async def locate_circle(
        self, min_diameter: float, max_diameter: float, search_mm: float
    ) -> Point | None:
        """Find a round mark near the top camera's center: its machine position, or None."""
        camera = self.top_camera()
        if self._grab is None:
            raise VisionError("the cameras are not available")
        await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera.id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        circle = await asyncio.get_running_loop().run_in_executor(
            None,
            find_circle,
            frame.image,
            min_diameter / mm_per_px,
            max_diameter / mm_per_px,
            search_mm / mm_per_px,
        )
        if circle is None:
            return None
        dx, dy = self._profile.image_to_machine(
            (circle.x - w / 2) * mm_per_px, (circle.y - h / 2) * mm_per_px
        )
        here = self.camera_position()
        return Point(x=here.x + dx, y=here.y + dy)

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

    async def _home(self) -> str | None:
        await self._driver.home()
        if self._profile.safe_z_input is not None and not self.z_safe:
            raise SafeZFaultError(
                "after homing, the safe-Z sensor reads not safe; check the sensor and the "
                "Z mechanism"
            )
        message = None
        if self._profile.visual_homing:
            message = await self._visual_home()
        self._homed = True
        return message

    def start_measure_camera_scale(self) -> Operation:
        """Measure the top camera's millimeters per pixel using the homing fiducial."""
        self._check_can_move()
        self._check_homed()
        return self._start_operation("camera_scale", self._measure_camera_scale)

    def _homing_target(self) -> tuple[Location, Camera, dict[str, float]]:
        fiducial = next(
            (loc for loc in self._profile.locations if loc.kind == "homing_fiducial"), None
        )
        camera = next(
            (c for c in self._profile.cameras if c.mount == "head" and c.looking == "down"),
            None,
        )
        if fiducial is None or camera is None:
            raise VisionError(
                "this needs a homing fiducial and a down-looking head camera in the machine profile"
            )
        if self._grab is None:
            raise VisionError("the cameras are not available")
        target = {
            "X": fiducial.position.x - camera.position.x,
            "Y": fiducial.position.y - camera.position.y,
        }
        return fiducial, camera, target

    async def _locate_fiducial(
        self, fiducial: Location, camera: Camera
    ) -> tuple[float, float, int, int]:
        """Find the fiducial in a fresh frame: its center in pixels, and the frame size."""
        assert self._grab is not None
        await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera.id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        expected = fiducial.diameter
        low, high = (expected * 0.6, expected * 1.5) if expected else (0.5, 4.0)
        circle = await asyncio.get_running_loop().run_in_executor(
            None,
            find_circle,
            frame.image,
            low / mm_per_px,
            high / mm_per_px,
            VISUAL_SEARCH_RADIUS_MM / mm_per_px,
        )
        if circle is None:
            raise VisionError(
                f"the homing fiducial was not found within {VISUAL_SEARCH_RADIUS_MM:g} mm of "
                f"({fiducial.position.x:g}, {fiducial.position.y:g}); check the light, the "
                "fiducial location in the machine profile, and that homing is close"
            )
        return circle.x, circle.y, w, h

    async def _measure_camera_scale(self) -> str:
        fiducial, camera, target = self._homing_target()
        step = CAMERA_SCALE_STEP_MM
        await self._run_steps(self._plan_move(target), None)
        scales: dict[str, float] = {}
        for axis, index in (("X", 0), ("Y", 1)):
            seen: list[float] = []
            for sign in (-1, 1):
                await self._run_steps([{**target, axis: target[axis] + sign * step}], None)
                found = await self._locate_fiducial(fiducial, camera)
                seen.append(found[index])
            # The fiducial moves the opposite way to the head, by 2 * step in total.
            pixels = abs(seen[1] - seen[0])
            if pixels < 1:
                raise VisionError(f"the fiducial did not move in the image when moving {axis}")
            scales[axis] = 2 * step / pixels
        await self._run_steps([target], None)
        width = (await self._locate_fiducial(fiducial, camera))[2]
        assumed = camera.field_of_view.x / width
        return (
            f"{camera.label}: {scales['X']:.5f} mm/pixel in X and {scales['Y']:.5f} in Y "
            f"(the profile's field of view implies {assumed:.5f})."
        )

    async def _visual_home(self) -> str:
        """Center the top camera on the homing fiducial, then make the coordinates agree
        with the fiducial's known position. Returns what it corrected."""
        fiducial, camera, target = self._homing_target()
        await self._run_steps(self._plan_move(target), None)
        for _ in range(VISUAL_PASSES):
            u, v, w, h = await self._locate_fiducial(fiducial, camera)
            mm_per_px = camera.field_of_view.x / w  # until the camera is calibrated
            dx, dy = self._profile.image_to_machine(
                (u - w / 2) * mm_per_px, (v - h / 2) * mm_per_px
            )
            if math.hypot(dx, dy) <= VISUAL_TOLERANCE_MM:
                break
            here = self._driver.position
            await self._run_steps([{"X": here["X"] + dx, "Y": here["Y"] + dy}], None)
        else:
            raise VisionError(f"could not center on the homing fiducial in {VISUAL_PASSES} passes")

        # The head is centered on the fiducial, whose true position is the target. How
        # far the reported position is from that is how far off homing left the machine.
        here = self._driver.position
        off_x, off_y = target["X"] - here["X"], target["Y"] - here["Y"]
        await self._driver.set_position(target)
        log.info("visual homing: machine was off by X %+.3f mm, Y %+.3f mm", off_x, off_y)
        return (
            f"Visual homing found the machine {math.hypot(off_x, off_y):.3f} mm off after "
            f"homing (X {off_x:+.3f}, Y {off_y:+.3f}) and corrected the coordinates."
        )

    def _start_operation(self, kind: str, work: _Work) -> Operation:
        self._busy = True
        self._publish_state()

        async def run() -> str | None:
            try:
                return await work()
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
