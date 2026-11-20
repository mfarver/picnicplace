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
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from picnicplace.calibration import (
    CRASH_STALES,
    CalibrationStore,
    apply_measurements,
    datum_key,
)
from picnicplace.camera.source import Frame
from picnicplace.camera.synthetic import BoardScene, SceneState, TapeScene
from picnicplace.events import EventBus
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import (
    CalibrationRequiredError,
    ConnectFailedError,
    EmergencyStopActiveError,
    HomingError,
    InvalidTargetError,
    MachineBusyError,
    MachineError,
    MachinePausedError,
    NoClientPresentError,
    NotConnectedError,
    NotHomedError,
    NotSupportedError,
    SafeZFaultError,
    StoppedError,
    UnknownOutputError,
    VisionError,
    ZNotSafeError,
)
from picnicplace.machine.profile import (
    WHITE,
    Camera,
    Input,
    LightColor,
    Location,
    MachineProfile,
    Nozzle,
    Point,
    nozzle_height,
)
from picnicplace.machine.tips import TipStore
from picnicplace.operations import OperationManager
from picnicplace.presence import PresenceTracker
from picnicplace.recorder import FlightRecorder
from picnicplace.schemas import (
    MachineState,
    MachineStateEvent,
    Operation,
    OperatorAnswer,
    OperatorRequest,
    TipChangeNozzle,
)
from picnicplace.simparts import HeldPart, PlacedPart
from picnicplace.vision.fiducial import Polarity
from picnicplace.wizard import LIMITED_SPEED, Limit

log = logging.getLogger(__name__)

_Work = Callable[[], Awaitable[str | None]]

Grab = Callable[[str, float, float], Awaitable[Frame | None]]
"""Get a frame from a camera captured at or after a time: (camera ID, after, wait_s)."""

HOME_Z_TOLERANCE_MM = 0.5
"""How far Z may read from the profile's home_z right after homing."""

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

APPROACH_MM = 2.0
"""A nozzle going down slows for this last stretch, so it meets a part, a pocket, or the
board gently, whatever the machine's speed."""
APPROACH_SPEED = 10.0
"""mm/s, for a nozzle's last ``APPROACH_MM`` down, and for lifting a part off gently."""

VACUUM_POLL_S = 0.05
VACUUM_STEADY = 0.002
"""A vacuum reading that changes less than this (as a fraction of its sensor's range)..."""
VACUUM_STEADY_S = 0.3
"""...for this long has settled. Not from one poll to the next: a LumenPnP's sensor
updates about every 0.1 s in steps of 65,536 counts, so two polls can agree while the
vacuum is still rising. With an N045's small hole it rose for 0.6 s in open air, and an
open reading taken at 0.3 s made the first step of a touch read as a seal."""


def _open_px(min_diameter_px: float) -> int:
    """How wide a line is wiped out before looking for marks at least this big: a tenth
    of their diameter, which a 4 mm disc keeps and silkscreen lines do not."""
    return max(3, round(min_diameter_px / 10))


@dataclass(frozen=True)
class RoundMark:
    """A round mark the top camera saw."""

    at: Point
    diameter: float
    """In mm."""
    brightness: float
    """Its middle's mean brightness, 0 to 255."""


LIMITED_CACHE_S = 1.0
"""How long whether the machine is limited (to a quarter of full speed) is taken as
known, so every move does not work it out again."""


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
        recorder: FlightRecorder | None = None,
    ) -> None:
        self._speed_scale = speed_scale
        self.limits: Callable[[str], Limit] | None = None
        """Set by the setup wizard: whether a task of a kind may start now, and what to
        warn of if it may."""
        self.camera_lights: dict[str, LightColor] = {}
        """From the config: the color and brightness each camera's light is set to for
        vision, by camera ID (none given: whatever it is, or white if it is off)."""
        self.sim_tips: dict[str, str] = {}
        """Simulator only: the tips really fitted, by nozzle, whatever the operator said."""
        self.limited: Callable[[], bool] | None = None
        """Set by the setup wizard: moves are capped at a quarter of full speed."""
        self._limited_cache = (-math.inf, False)
        self.recorder = recorder or FlightRecorder(None)
        """Keeps what the machine saw and did, while recording."""
        self.calibration = CalibrationStore(None)
        """What the machine has measured of itself, for the calibration checklist."""
        self.tips = TipStore(None)
        """Which tip is fitted on each nozzle."""
        self.on_tip_changed: list[Callable[[str, str | None], None]] = []
        """Called with a nozzle's ID and its new tip whenever the tip said changes."""
        self.after_tip_change: Callable[[list[str], list[str]], Awaitable[str]] | None = None
        """Run at the end of a tip change by hand, with the nozzles whose tip was said, and
        those given a new tip; what it returns is added to the operation's message."""
        self._design = profile.model_copy(deep=True)
        """The profile as loaded, before measurements took the place of its values."""
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
        self._visually_homed = False
        self._estop = False
        self._busy = False
        self._stop_requested = False
        self._paused = False
        self._request: OperatorRequest | None = None
        self._answer: OperatorAnswer | None = None
        self._wake = asyncio.Event()
        self._connection_error: str | None = None
        self._connect_lock = asyncio.Lock()
        self._grab: Grab | None = None
        self.scene_tapes: Callable[[], tuple[TapeScene, ...]] | None = None
        """Simulated feeder tapes for the synthetic cameras, if any."""
        self.scene_parts: (
            Callable[[], tuple[tuple[tuple[str, HeldPart], ...], tuple[PlacedPart, ...]]] | None
        ) = None
        """Simulated parts held by nozzles and placed on boards, if any."""
        self.scene_boards: Callable[[], tuple[BoardScene, ...]] | None = None
        """Simulated boards loaded for a job, if any."""
        self.disc_seen: Point | None = None
        """Where the top camera last found the nozzle-offset disc, so the next search for
        it starts there (a failed measurement is usually tried again with the disc where
        it was)."""

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
        """What the synthetic cameras draw: position, outputs, which nozzles hold a part
        (vacuum past the pick threshold), and the tips fitted. Positions are rounded so tiny
        changes do not cause new frames."""
        held = {n.id: self.reads_held(n, self.vacuum_reading(n)) for n in self._profile.nozzles}
        held_parts, placed = self.scene_parts() if self.scene_parts is not None else ((), ())
        return SceneState(
            position={a: round(v, 3) for a, v in self._driver.scene_position.items()},
            outputs=dict(self._driver.outputs),
            held=held,
            tapes=self.scene_tapes() if self.scene_tapes is not None else None,
            held_parts=held_parts,
            placed=placed,
            boards=self.scene_boards() if self.scene_boards is not None else (),
            tips=tuple(self.sim_tips.items())
            if self.sim_tips
            else tuple((n.id, n.tip) for n in self._profile.nozzles if n.tip is not None),
            runout=tuple(
                (n, round(x, 4), round(y, 4)) for n, (x, y) in self._driver.scene_runout.items()
            ),
            lights=tuple(
                (k, (c.red, c.green, c.blue, c.brightness))
                for k, c in sorted(self._driver.light_colors.items())
            ),
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
            paused=self._paused,
            busy=self._busy,
            motion_enabled=self._driver.connected and self._presence.present and not self._estop,
            axes=list(self._driver.axes),
            position=dict(self._driver.position),
            inputs=dict(self._driver.inputs),
            outputs=dict(self._driver.outputs),
            lights=dict(self._driver.light_colors),
            z_safe=self.z_safe,
            speed_scale=self.speed_scale,
            warnings=list(self._driver.warnings),
            request=self._request,
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

    async def set_light(self, output_id: str, color: LightColor) -> None:
        """Set a light's color and brightness; a brightness of 0 switches it off, which is
        allowed whenever the machine is connected."""
        output = next((o for o in self._profile.outputs if o.id == output_id), None)
        if output is None or output_id not in self._driver.outputs:
            raise UnknownOutputError(f"unknown output {output_id!r}")
        if output.role != "light":
            raise InvalidTargetError(f"{output.label} is not a light: switch it on or off")
        if not output.colored and color.brightness > 0 and color != WHITE:
            raise NotSupportedError(
                f"the {output.label.lower()} can only be switched on and off, at full white"
            )
        if color.brightness > 0:
            self._check_can_actuate()
        elif not self._driver.connected:
            raise NotConnectedError("the machine is not connected")
        await self._driver.set_light(output_id, color)

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

    def pause(self) -> None:
        """Hold the machine before its next move: the move under way finishes (Marlin
        cannot cut one short without halting until it is power-cycled), then the running
        operation waits until resume or stop. Nothing new starts while paused. Always
        allowed, from any client: the stop for an emergency is unplugging the controller or
        pressing its reset button, or an E-stop switch that cuts the motors' power."""
        if not self._paused:
            log.warning("paused")
            self._paused = True
            self._publish_state()

    def resume(self) -> None:
        """Carry on from where pause held the machine."""
        if self._paused:
            log.info("resumed")
            self._paused = False
            self._wake.set()
            self._publish_state()

    @property
    def paused(self) -> bool:
        return self._paused

    def request_stop(self) -> bool:
        """End the running operation before its next move (raising the nozzles to travel
        height is still allowed, so the work can tidy up), and resume the machine if it is
        paused. The firmware keeps running and the machine stays homed. Says whether
        anything was running."""
        if self._paused:
            self.resume()
        if not self._busy:
            return False
        log.warning("stop requested")
        self._stop_requested = True
        self._wake.set()
        return True

    async def _hold(self, step: Mapping[str, float] | None = None) -> None:
        """Before a move: wait while paused, and end the operation if a stop was asked
        for, or the machine dropped off or halted while it waited."""
        while self._paused and not self._stop_requested:
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 0.5)
            if not self._driver.connected or self._estop:
                raise NotConnectedError("the machine dropped off while it was paused")
        if not self._stop_requested:
            return
        if step and all(a in self._z_axes and v == TRAVEL_HEIGHT for a, v in step.items()):
            return
        raise StoppedError("stopped by the operator")

    async def stop_manual_motion(self) -> None:
        """Stop the current manual move, keeping position (the controlling client left)."""
        if self._busy:
            log.warning("controlling client left during motion; stopping")
            await self._driver.stop()

    # --- For other services (feeders, calibration) -----------------------------------------

    def check_can_actuate(self) -> None:
        """Raise unless outputs and feeders may be used now."""
        self._check_can_actuate()

    def check_not_paused(self) -> None:
        """Raise if the machine is paused: nothing new starts until it is resumed."""
        if self._paused:
            raise MachinePausedError("the machine is paused; resume it first")

    async def hold(self) -> None:
        """Before an action that is not a move, such as a feeder advance: wait while
        paused, and end the operation if a stop was asked for."""
        await self._hold()

    # --- Asking the operator ----------------------------------------------------------------

    @property
    def request(self) -> OperatorRequest | None:
        """What the running operation is waiting for the operator to do, if anything."""
        return self._request

    async def ask(self, request: OperatorRequest) -> OperatorAnswer:
        """Within an operation: wait for the operator to do what ``request`` says and
        answer it (``answer``). Stop ends the operation instead."""
        self._request = request
        self._answer = None
        log.info("asking the operator: %s", request.title)
        self._publish_state()
        try:
            while self._answer is None:
                if self._stop_requested:
                    raise StoppedError("stopped by the operator")
                if not self._driver.connected or self._estop:
                    raise NotConnectedError("the machine dropped off while it waited")
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 0.5)
            return self._answer
        finally:
            self._request = None
            self._answer = None
            self._publish_state()

    def answer(self, answer: OperatorAnswer) -> None:
        """The operator's answer to what the machine asked: the operation carries on."""
        request = self._request
        if request is None or request.id != answer.request_id:
            raise InvalidTargetError("the machine is not asking that (now): nothing to answer")
        if request.kind == "tip_change":
            for wanted in request.nozzles:
                if wanted.nozzle_id not in answer.tips:
                    raise InvalidTargetError(
                        f"say which tip is on {self.nozzle(wanted.nozzle_id).label.lower()} now"
                    )
            for nozzle_id, tip_id in answer.tips.items():
                self.nozzle(nozzle_id)
                if tip_id is not None and self._profile.nozzle_tip(tip_id) is None:
                    raise InvalidTargetError(f"this machine's profile has no nozzle tip {tip_id!r}")
        self._answer = answer
        self._wake.set()

    def tip_change_spot(self) -> dict[str, float]:
        """Where the head goes for a tip change: front and center, within reach."""
        x, y = self._axes["X"], self._axes["Y"]
        return {"X": (x.min + x.max) / 2, "Y": y.min if self._profile.y_sign > 0 else y.max}

    async def change_tips(
        self,
        nozzles: list[TipChangeNozzle],
        title: str,
        message: str,
        after: str,
    ) -> dict[str, str | None]:
        """Within an operation: raise the nozzles, bring the head to the front, and ask the
        operator to change the tips on ``nozzles`` and say which is on each now. Returns
        the tips said; they are fitted from then on."""
        for wanted in nozzles:
            nozzle = self.nozzle(wanted.nozzle_id)
            outputs = [o for o in (nozzle.pump_output, nozzle.valve_output) if o is not None]
            if any(self._driver.outputs.get(o) for o in outputs):
                raise InvalidTargetError(
                    f"{nozzle.label} has suction on, so it may be holding a part: put it down "
                    "or discard it before changing its tip"
                )
        await self.raise_nozzles()
        await self.move_axes(self.tip_change_spot())
        answer = await self.ask(
            OperatorRequest(
                id=uuid.uuid4().hex,
                kind="tip_change",
                title=title,
                message=message,
                after=after,
                nozzles=nozzles,
            )
        )
        for nozzle_id, tip_id in answer.tips.items():
            self.set_tip(nozzle_id, tip_id, in_task=True)
            if self.sim_tips:
                # The simulated operator did what they said.
                if tip_id is None:
                    self.sim_tips.pop(nozzle_id, None)
                else:
                    self.sim_tips[nozzle_id] = tip_id
        return answer.tips

    def start_tip_change(self, nozzle_ids: list[str] | None) -> Operation:
        """Bring the head to the front for changing tips by hand, and wait for the operator
        to say which tip is on each nozzle (all, unless given)."""
        nozzles = [self.nozzle(i) for i in nozzle_ids] if nozzle_ids else self._profile.nozzles
        self._check_can_move()
        self._check_homed()

        async def work() -> str:
            before = {n.id: n.tip for n in nozzles}
            tips = await self.change_tips(
                [TipChangeNozzle(nozzle_id=n.id, fitted=n.tip, choices=[]) for n in nozzles],
                "Change nozzle tips",
                "The head is at the front with the nozzles up. Change the tips you want to, "
                "then say which tip is on each nozzle.",
                "The machine uses each tip's own heights and vacuum levels, if it has been "
                "calibrated with it.",
            )
            said = [f"{self.nozzle(i).label}: {t or 'none'}" for i, t in tips.items()]
            line = self.calibration.get().lines.get("nozzle_offsets")
            new = [
                t
                for i, t in tips.items()
                if t is not None and (line is None or datum_key(i, t) not in line.values)
            ]
            then = (
                f" Not measured yet: {', '.join(dict.fromkeys(new))}. Lay a square of plain "
                "paper on the datum board and measure their heights (the paper stop's Start)."
                if new and line is not None
                else ""
            )
            changed = [i for i, t in tips.items() if t is not None and t != before.get(i)]
            fitted = [i for i, t in tips.items() if t is not None]
            after = await self.after_tip_change(fitted, changed) if self.after_tip_change else ""
            return "Tips: " + "; ".join(said) + "." + then + after

        return self._start_operation("tip_change", work)

    @property
    def homed(self) -> bool:
        return self._homed

    @property
    def connected(self) -> bool:
        return self._driver.connected

    @property
    def firmware(self) -> str | None:
        return self._driver.firmware

    @property
    def speed_scale(self) -> float:
        """The fraction of full speed moves are capped at: the config's, and no more than
        a quarter until the setup wizard is finished."""
        if self.limited is None:
            return self._speed_scale
        now = time.monotonic()
        at, limited = self._limited_cache
        if now - at > LIMITED_CACHE_S:
            limited = self.limited()
            self._limited_cache = (now, limited)
        return min(self._speed_scale, LIMITED_SPEED) if limited else self._speed_scale

    async def home_now(self) -> str | None:
        """Home, within a task."""
        return await self._home()

    @property
    def visually_homed(self) -> bool:
        """Homed, and visual homing has corrected X and Y since."""
        return self._homed and self._visually_homed

    async def visual_home_now(self) -> str:
        """Visual homing, within a task."""
        self._check_homed()
        return await self._visual_home()

    async def measure_camera_scale_now(self) -> str:
        """Measure the top camera's scale, within a task."""
        self._check_homed()
        return await self._measure_camera_scale()

    def start_sequence(self, kind: str, work: _Work) -> Operation:
        """Run steps that begin by homing (so the machine need not be homed yet) as one
        operation, with the machine busy."""
        self._check_can_move()
        return self._start_operation(kind, work)

    def start_task(self, kind: str, work: _Work) -> Operation:
        """Run work that moves the machine as an operation, with the machine busy. The
        setup wizard's stage may refuse it (a pick before the machine is calibrated), or
        let it go ahead with a warning put before its message."""
        self._check_can_move()
        self._check_homed()
        allowed = self.limits(kind) if self.limits is not None else Limit()
        if allowed.refused:
            raise CalibrationRequiredError(allowed.refused)
        if allowed.warning:
            warning, inner = allowed.warning, work

            async def warned() -> str | None:
                message = await inner()
                return f"Warning: {warning}. " + (message or "")

            work = warned
        return self._start_operation(kind, work)

    async def frame(
        self, camera_id: str, settle: bool = True, light: LightColor | None = None
    ) -> Frame:
        """A frame taken now (after the camera's settle time, if asked), with the camera's
        light set to ``light`` if given (a part trained to be seen so), else to its usual
        color."""
        camera = next((c for c in self._profile.cameras if c.id == camera_id), None)
        if camera is None:
            raise VisionError(f"no camera {camera_id!r}")
        if self._grab is None:
            raise VisionError("the cameras are not available")
        if settle:
            await self._light_for(camera, light)
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

    async def move_axes(self, target: Mapping[str, float], speed: float | None = None) -> None:
        """Move to an absolute position, at safe Z if X or Y change, no faster than
        ``speed`` (mm/s) if given. For use in a task."""
        self._check_target(target)
        await self._run_steps(self._plan_move(target), speed)

    async def move_nozzle_over(
        self, nozzle: Nozzle, x: float, y: float, angle: float | None = None
    ) -> None:
        """Put a nozzle over a point (at safe Z), turned to ``angle`` if given (see
        ``nozzle_turn``)."""
        target = {"X": x - nozzle.offset.x, "Y": y - nozzle.offset.y}
        if angle is not None:
            target[nozzle.rotation_axis] = self.nozzle_turn(nozzle, angle)
        await self.move_axes(target)

    def nozzle_turn(self, nozzle: Nozzle, angle: float) -> float:
        """Where to turn a nozzle's rotation axis for ``angle``. A continuous axis (one
        with a rotating coupling, such as the LumenPnP's) turns to it as given. An axis
        with limits, on a head whose hoses or wires would twist, turns to the same angle
        a whole number of turns away that lies within them, the nearest to where it is,
        so it never winds up past them however many parts it turns."""
        axis = self._axes[nozzle.rotation_axis]
        if axis.continuous:
            return angle
        lowest = math.ceil((axis.min - angle) / 360.0 - 1e-9)
        highest = math.floor((axis.max - angle) / 360.0 + 1e-9)
        if lowest > highest:
            return angle  # No turn of it fits: the limits refuse it, saying why.
        now = self.nozzle_angle(nozzle)
        return min(
            (angle + 360.0 * k for k in range(lowest, highest + 1)), key=lambda a: abs(a - now)
        )

    async def lower_nozzle(self, nozzle: Nozzle, height: float, speed: float | None = None) -> None:
        """Move a nozzle down (or up) to a height: 0 at the top, negative down. The
        nozzle's measured Z offset is added, so a height means the same for every nozzle
        and tip. Going down, it slows to ``APPROACH_SPEED`` for the last ``APPROACH_MM``
        (or all the way, if nearer); ``speed`` (mm/s) limits the move further."""
        raw = min(0.0, height + nozzle.z_offset)
        now = nozzle_height(nozzle, self._driver.position[nozzle.z_axis])
        if raw < now - EPSILON:
            if now - raw > APPROACH_MM:
                await self.move_axes({nozzle.z_axis: nozzle.z_sign * (raw + APPROACH_MM)}, speed)
            speed = APPROACH_SPEED if speed is None else min(speed, APPROACH_SPEED)
        await self.move_axes({nozzle.z_axis: nozzle.z_sign * raw}, speed)

    async def park(self) -> str:
        """Raise the nozzles and move the head out of the operator's way: to the park
        location, or else to the back of its travel. For use inside an operation. Says
        where it went."""
        profile = self._profile
        park = next((loc for loc in profile.locations if loc.kind == "park"), None)
        if park is not None:
            target = {"X": park.position.x, "Y": park.position.y}
            where = f"the {park.label.lower()}"
        else:
            y = next((a for a in profile.axes if a.name == "Y"), None)
            if y is None:
                return "nowhere: the machine has no Y axis"
            target = {"Y": y.max if profile.y_sign > 0 else y.min}
            where = "the back (no park location is set)"
        await self.raise_nozzles()
        await self.move_axes(target)
        return where

    def start_park(self) -> Operation:
        """Park the head (``park``) as an operation."""

        async def work() -> str:
            return f"The head moved to {await self.park()}."

        return self.start_task("park", work)

    async def raise_nozzles(self) -> None:
        await self.move_axes({axis: TRAVEL_HEIGHT for axis in sorted(self._z_axes)})

    async def suction(self, nozzle: Nozzle, on: bool) -> None:
        """Pump and valve on together, or both off (which lets go of a part)."""
        outputs = [o for o in (nozzle.pump_output, nozzle.valve_output) if o is not None]
        for output in outputs if on else reversed(outputs):
            await self._driver.set_output(output, on)

    def vacuum_sensor(self, nozzle: Nozzle) -> Input | None:
        """The nozzle's vacuum sensor, if it has one with a range to read it against."""
        sensor = self._inputs.get(nozzle.vacuum_input or "")
        return sensor if sensor is not None and sensor.range is not None else None

    def vacuum_threshold(self, nozzle: Nozzle) -> float | None:
        """The reading at or past which the nozzle holds a part: its measured threshold
        (calibration line 9), else its sensor's."""
        if nozzle.vacuum is not None:
            return nozzle.vacuum.threshold
        sensor = self._inputs.get(nozzle.vacuum_input or "")
        return sensor.threshold if sensor is not None else None

    def reads_held(self, nozzle: Nozzle, value: float | None, level: float | None = None) -> bool:
        """Whether a reading is at or past ``level`` (default: the nozzle's threshold),
        going toward its sensor's range max: a part held, or the tip sealed."""
        sensor = self.vacuum_sensor(nozzle)
        level = self.vacuum_threshold(nozzle) if level is None else level
        if sensor is None or sensor.range is None or level is None or value is None:
            return False
        return sensor.range.fraction(value) >= sensor.range.fraction(level)

    async def holds_part(
        self, nozzle: Nozzle, expect: bool, timeout_s: float, level: float | None = None
    ) -> bool | None:
        """Wait up to ``timeout_s`` for the vacuum to say whether a part is held (until
        it says ``expect``). ``level``: the reading that means held, in place of the
        nozzle's threshold. None if the nozzle has no vacuum sensor."""
        sensor = self._inputs.get(nozzle.vacuum_input or "")
        if sensor is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            await self._driver.refresh_input(sensor.id)
            value = self.vacuum_reading(nozzle)
            held = self.reads_held(nozzle, value, level)
            if held == expect or loop.time() >= deadline:
                self.recorder.event(
                    "vacuum", nozzle=nozzle.id, expect=expect, held=held, reading=value
                )
                return held
            await asyncio.sleep(0.03)

    async def holds_after_lift(
        self, nozzle: Nozzle, most_s: float, level: float | None = None
    ) -> tuple[bool | None, float | None]:
        """Whether a nozzle that has just lifted a part still holds it, and the vacuum
        reading that says so, judged once the reading has settled (within ``most_s``): just
        after the lift it still reads the seal, so a part that fell off on the way up looks
        held until the reading falls. ``level``: as for ``holds_part``. None, None if the
        nozzle has no vacuum sensor."""
        reading = await self.settled_vacuum(nozzle, 0.0, most_s)
        if reading is None:
            return None, None
        held = self.reads_held(nozzle, reading, level)
        self.recorder.event("vacuum", nozzle=nozzle.id, expect=True, held=held, reading=reading)
        return held, reading

    async def settled_vacuum(self, nozzle: Nozzle, least_s: float, most_s: float) -> float | None:
        """The nozzle's vacuum reading once it has stopped changing: after at least
        ``least_s`` (for the pump to start), and at most ``most_s``. None if the nozzle
        has no vacuum sensor."""
        sensor = self.vacuum_sensor(nozzle)
        if sensor is None or sensor.range is None:
            return None
        span = abs(sensor.range.max - sensor.range.min)
        loop = asyncio.get_running_loop()
        start = loop.time()
        steady_s = max(2 * VACUUM_POLL_S, VACUUM_STEADY_S / self._driver.time_scale)
        await asyncio.sleep(least_s)
        since: float | None = None  # when the reading last changed
        last: float | None = None
        while True:
            await self._driver.refresh_input(sensor.id)
            value = self.vacuum_reading(nozzle)
            now = loop.time()
            if value is None or last is None or abs(value - last) > VACUUM_STEADY * span:
                since, last = now, value
            elif since is not None and now - since >= steady_s:
                return value
            if now - start >= most_s:
                return value
            await asyncio.sleep(VACUUM_POLL_S)

    def vacuum_reading(self, nozzle: Nozzle) -> float | None:
        value = self._driver.inputs.get(nozzle.vacuum_input or "")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    async def light_for(self, camera: Camera, want: LightColor | None = None) -> None:
        """Set a camera's light for vision (``_light_for``)."""
        await self._light_for(camera, want)

    async def _light_for(self, camera: Camera, want: LightColor | None = None) -> None:
        """Vision needs the camera's light: set it to ``want`` (a part's own light), else
        to the config's color for the camera, else white; and leave it on (switching it
        for every look would flicker through a run). The caller then waits the camera's
        settle time, which lets the picture brighten."""
        light = camera.light_output
        if light is None:
            return
        want = want or self.camera_lights.get(camera.id)
        now = self._driver.light_colors.get(light)
        if want is not None and want != WHITE:
            if not self._driver.outputs.get(light) or now != want:
                await self.set_light(light, want)
        elif not self._driver.outputs.get(light) or (now is not None and now != WHITE):
            # Back to plain white after a part's own light.
            await self._driver.set_output(light, True)

    def image_to_machine(
        self,
        camera: Camera,
        right: float,
        down: float,
        turned: bool = True,
        mirrored: bool | None = None,
    ) -> tuple[float, float]:
        """A displacement seen in a camera's image (mm right and mm down, as shown) as a
        machine X/Y displacement. Up-looking cameras see the machine mirrored left to
        right, as from below, unless their picture is flipped (``mirrored``, by default
        as calibration found it), and their picture's measured turn is undone (unless
        ``turned`` is false: how the profile alone says the picture should be)."""
        if turned and camera.looking == "up" and camera.image_rotation:
            t = math.radians(-camera.image_rotation)
            right, down = (
                right * math.cos(t) - down * math.sin(t),
                right * math.sin(t) + down * math.cos(t),
            )
        x, y = self._profile.image_to_machine(right, down)
        if mirrored is None:
            mirrored = camera.image_mirrored
        return (-x, y) if camera.looking == "up" and mirrored else (x, y)

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
        return [m.at for m in await self.locate_round_marks(min_diameter, max_diameter)]

    async def locate_round_marks(
        self, min_diameter: float, max_diameter: float, through_lines: bool = False
    ) -> list[RoundMark]:
        """Every round mark the top camera sees: where, how big, and how bright.
        ``through_lines``: find marks crossed by thin lines, such as a paper disc lying on
        a board's silkscreen."""
        camera = self.top_camera()
        if self._grab is None:
            raise VisionError("the cameras are not available")
        await self._light_for(camera)
        await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera.id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        circles = await self.recorder.vision(
            "find_circles",
            frame.image,
            min_diameter / mm_per_px,
            max_diameter / mm_per_px,
            _open_px(min_diameter / mm_per_px) if through_lines else 0,
            camera=camera.id,
            position=dict(self._driver.position),
            mm_per_px=mm_per_px,
        )
        here = self.camera_position()
        # The strongest channel, not gray: under a tinted light, white paper is bright in
        # every channel the light has, but its gray can be no brighter than copper's.
        value = frame.image.max(axis=2) if frame.image.ndim == 3 else frame.image
        out: list[RoundMark] = []
        for c in circles:
            dx, dy = self._profile.image_to_machine(
                (c.x - w / 2) * mm_per_px, (c.y - h / 2) * mm_per_px
            )
            # How bright its middle is (0-255): white paper is far brighter than copper.
            r = max(1, round(c.diameter * 0.3))
            u, v = round(c.x), round(c.y)
            middle = value[max(0, v - r) : v + r + 1, max(0, u - r) : u + r + 1]
            brightness = float(middle.mean()) if middle.size else 0.0
            out.append(
                RoundMark(Point(x=here.x + dx, y=here.y + dy), c.diameter * mm_per_px, brightness)
            )
        return out

    async def locate_circle(
        self,
        min_diameter: float,
        max_diameter: float,
        search_mm: float,
        expected_diameter: float | None = None,
        through_lines: bool = False,
        polarity: Polarity | None = None,
    ) -> Point | None:
        """Find a round mark near the top camera's center: its machine position, or None.
        With its expected diameter, marks of other sizes (such as silkscreen letters) are
        less likely to be taken for it. ``through_lines``: as for ``locate_round_marks``.
        ``polarity``: only bright marks, or only dark ones."""
        camera = self.top_camera()
        if self._grab is None:
            raise VisionError("the cameras are not available")
        await self._light_for(camera)
        await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera.id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        circle = await self.recorder.vision(
            "find_circle",
            frame.image,
            min_diameter / mm_per_px,
            max_diameter / mm_per_px,
            search_mm / mm_per_px,
            expected_diameter / mm_per_px if expected_diameter else None,
            _open_px(min_diameter / mm_per_px) if through_lines else 0,
            polarity,
            camera=camera.id,
            position=dict(self._driver.position),
            mm_per_px=mm_per_px,
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
        if self._paused:
            raise MachinePausedError("the machine is paused; resume it first")
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
                self.calibration.mark_stale(
                    CRASH_STALES,
                    f"Z lost steps at {time.strftime('%Y-%m-%d %H:%M')}: a crash may have moved it",
                )
                self._publish_state()
                then = (
                    "Home the machine again."
                    if self._profile.homes_z_first
                    else "Raise the nozzles by hand until it reads safe, then home again."
                )
                raise SafeZFaultError(
                    "the safe-Z sensor reads not safe although Z is at a safe height; "
                    f"Z may have lost steps. {then}"
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
            await self._hold(step)
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
            self.recorder.event("move", to=step, speed=speed)
            await self._driver.move_to(step, self._limit_speed(step, speed))

    def _limit_speed(self, step: Mapping[str, float], speed: float | None) -> float | None:
        """Apply the speed scale: no faster than that fraction of the slowest moving axis."""
        scale = self.speed_scale
        if scale >= 1:
            return speed
        cap = min(self._axes[a].max_speed for a in step) * scale
        return cap if speed is None else min(speed, cap)

    async def _home(self) -> str | None:
        await self._hold()
        await self._check_home_from_safe_z()
        self._visually_homed = False
        await self._driver.home()
        self._check_home_z()
        if self._profile.safe_z_input is not None and not self.z_safe:
            raise SafeZFaultError(
                "after homing, the safe-Z sensor reads not safe; check the sensor and the "
                "Z mechanism"
            )
        z = self._driver.position.get("Z", self._profile.home_z)
        self.calibration.record(
            "machine_frame", f"Homed, with Z at {z:g} as the profile expects.", {"z": z}
        )
        message = None
        if self._profile.visual_homing:
            message = await self._visual_home()
        self._homed = True
        return message

    async def _check_home_from_safe_z(self) -> None:
        """Refuse homing while a safe-Z sensor says a nozzle is low, unless the machine
        raises Z before X and Y move: homing would drag the nozzle across the machine
        (spec section 5.2). Without a sensor, the operator is asked instead."""
        sensor = self._profile.safe_z_input
        if sensor is None or self._profile.homes_z_first:
            return
        await self._driver.refresh_input(sensor)
        if not self.z_safe:
            raise ZNotSafeError(
                "the safe-Z sensor reads not safe: a nozzle is low, and homing would drag it "
                "across the machine. Nothing moved. Raise the nozzles by hand until it reads "
                "safe, then home"
            )

    def _check_home_z(self) -> None:
        """Refuse a homing that leaves Z in another frame than the profile's: every height
        the core moves to would be off by the difference, and the lift to safe Z could
        drive a nozzle down instead."""
        z = self._driver.position.get("Z")
        expected = self._profile.home_z
        if z is None or abs(z - expected) <= HOME_Z_TOLERANCE_MM:
            return
        raise HomingError(
            f"after homing, the controller reports Z = {z:g}, but this machine's profile "
            f"expects Z = {expected:g} with the nozzles level, so every height would be "
            f"wrong and moving could drive a nozzle into the feeders or the bed. Nothing "
            f"else was moved. Add G92 Z{expected:g} after G28 in the profile's home "
            "commands, or fix the profile's home_z."
        )

    def use_calibration(self, store: CalibrationStore) -> None:
        """Keep measurements in this store, and use the ones it has."""
        self.calibration = store
        self.apply_calibration()

    def use_tips(self, store: TipStore) -> None:
        """Keep which tip is on each nozzle in this store, and fit the ones it says (a
        tip the profile no longer has is taken as none said)."""
        self.tips = store
        fitted = store.get()
        for nozzle in self._profile.nozzles:
            tip = fitted.get(nozzle.id)
            nozzle.tip = tip if self._profile.nozzle_tip(tip) is not None else None
            self._driver.tip_fitted(nozzle.id, nozzle.tip)
        self.apply_calibration()

    def set_tip(self, nozzle_id: str, tip_id: str | None, in_task: bool = False) -> Nozzle:
        """Say which tip is now fitted on a nozzle (None: not said). Its Z offset and
        vacuum levels become those measured with that tip, if it has been. ``in_task``:
        the machine's own task found it (by camera), so the machine being busy is fine."""
        nozzle = self.nozzle(nozzle_id)
        if tip_id is not None and self._profile.nozzle_tip(tip_id) is None:
            raise InvalidTargetError(
                f"this machine's profile has no nozzle tip {tip_id!r}: add it to the local "
                "profile's nozzle_tips first"
            )
        if self._busy and not in_task:
            raise MachineBusyError("the machine is busy: change tips once it has stopped")
        outputs = [o for o in (nozzle.pump_output, nozzle.valve_output) if o is not None]
        if any(self._driver.outputs.get(o) for o in outputs):
            raise InvalidTargetError(
                f"{nozzle.label} has suction on, so it may be holding a part: put the part "
                "down or discard it, and turn suction off, before changing its tip"
            )
        changed = tip_id != nozzle.tip
        self.tips.put(nozzle.id, tip_id)
        self._driver.tip_fitted(nozzle.id, tip_id)
        nozzle.tip = tip_id
        if changed:
            for listener in self.on_tip_changed:
                listener(nozzle.id, tip_id)
        self.apply_calibration()
        self.recorder.event("nozzle_tip", nozzle=nozzle.id, tip=tip_id)
        return nozzle

    def apply_calibration(self) -> None:
        """Use what has been measured in place of the profile's values."""
        apply_measurements(self._profile, self.calibration.get())

    def design_camera(self, camera_id: str) -> Camera | None:
        """A camera as the profile gives it, before measurements took its place."""
        return next((c for c in self._design.cameras if c.id == camera_id), None)

    def design_nozzle(self, nozzle_id: str) -> Nozzle | None:
        """A nozzle as the profile gives it, before measurements took its place."""
        return next((n for n in self._design.nozzles if n.id == nozzle_id), None)

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
        await self._light_for(camera)
        await asyncio.sleep(camera.settle_s)
        frame = await self._grab(camera.id, time.time(), FRAME_WAIT_S)
        if frame is None:
            raise VisionError(f"the {camera.label.lower()} sent no frame")
        h, w = frame.image.shape[:2]
        mm_per_px = camera.field_of_view.x / w
        expected = fiducial.diameter
        low, high = (expected * 0.6, expected * 1.5) if expected else (0.5, 4.0)
        circle = await self.recorder.vision(
            "find_circle",
            frame.image,
            low / mm_per_px,
            high / mm_per_px,
            VISUAL_SEARCH_RADIUS_MM / mm_per_px,
            expected / mm_per_px if expected else None,
            camera=camera.id,
            position=dict(self._driver.position),
            mm_per_px=mm_per_px,
            purpose="homing fiducial",
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
        _, _, width, height = await self._locate_fiducial(fiducial, camera)
        design = next(c for c in self._design.cameras if c.id == camera.id)
        assumed = design.field_of_view.x / width
        message = (
            f"{camera.label}: {scales['X']:.5f} mm/pixel in X and {scales['Y']:.5f} in Y "
            f"(the profile's field of view implies {assumed:.5f})."
        )
        self.calibration.record(
            "top_camera_scale",
            message,
            {
                "x": scales["X"],
                "y": scales["Y"],
                "profile": assumed,
                "width": width,
                "height": height,
            },
        )
        self.apply_calibration()
        return message

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
        self._visually_homed = True
        off = math.hypot(off_x, off_y)
        self.calibration.record(
            "homing_fiducial",
            f"Found the datum board's dot at ({target['X'] + camera.position.x:g}, "
            f"{target['Y'] + camera.position.y:g}); homing was {off:.3f} mm off.",
            {"x": off_x, "y": off_y, "off": off},
        )
        return (
            f"Visual homing found the machine {off:.3f} mm off after "
            f"homing (X {off_x:+.3f}, Y {off_y:+.3f}) and corrected the coordinates."
        )

    def _start_operation(self, kind: str, work: _Work) -> Operation:
        self._busy = True
        self._stop_requested = False
        self._publish_state()

        async def run() -> str | None:
            try:
                return await work()
            finally:
                self._busy = False
                self._stop_requested = False
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
