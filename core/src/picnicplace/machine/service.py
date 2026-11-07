"""The machine service: the one owner of the machine, and where safety rules live.

Clients never reach a driver directly. Every motion request passes through here, where
presence, E-stop, homing, busy state, and soft limits are checked (spec section 5.2).
"""

import logging
from collections.abc import Awaitable, Callable, Mapping

from picnicplace.config import MachineConfig
from picnicplace.events import EventBus
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import (
    EmergencyStopActiveError,
    InvalidTargetError,
    MachineBusyError,
    NoClientPresentError,
    NotConnectedError,
    NotHomedError,
)
from picnicplace.operations import OperationManager
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import MachineState, MachineStateEvent, Operation

log = logging.getLogger(__name__)

_Work = Callable[[], Awaitable[None]]


class MachineService:
    def __init__(
        self,
        config: MachineConfig,
        driver: MotionDriver,
        bus: EventBus,
        presence: PresenceTracker,
        operations: OperationManager,
    ) -> None:
        self._axes = {a.name: a for a in config.axes}
        self._driver = driver
        self._bus = bus
        self._presence = presence
        self._operations = operations
        self._homed = False
        self._estop = False
        self._busy = False

        driver.on_change = self._publish_state
        presence.on_change.append(self._publish_state)
        presence.on_empty.append(self._on_no_clients)

    @property
    def state(self) -> MachineState:
        return MachineState(
            driver=self._driver.name,
            connected=self._driver.connected,
            homed=self._homed,
            estop=self._estop,
            busy=self._busy,
            motion_enabled=self._driver.connected and self._presence.present and not self._estop,
            axes=list(self._driver.axes),
            position=dict(self._driver.position),
        )

    async def start(self) -> None:
        await self._driver.connect()
        log.info("connected to %s driver", self._driver.name)

    async def shutdown(self) -> None:
        await self._driver.disconnect()

    def start_home(self) -> Operation:
        self._check_can_move()
        return self._start_operation("home", self._home)

    def start_move(self, target: Mapping[str, float], speed: float | None) -> Operation:
        self._check_can_move()
        if not self._homed:
            raise NotHomedError("home the machine before moving")
        self._check_target(target)
        return self._start_operation("move", lambda: self._move(target, speed))

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

    def _check_can_move(self) -> None:
        if not self._driver.connected:
            raise NotConnectedError("the machine is not connected")
        if self._estop:
            raise EmergencyStopActiveError("emergency stop is active; reset first")
        if not self._presence.present:
            raise NoClientPresentError("motion requires a connected client")
        if self._busy:
            raise MachineBusyError("the machine is busy")

    def _check_target(self, target: Mapping[str, float]) -> None:
        for name, value in target.items():
            axis = self._axes.get(name)
            if axis is None:
                raise InvalidTargetError(f"unknown axis {name!r}")
            if not axis.min <= value <= axis.max:
                raise InvalidTargetError(
                    f"{name}={value:g} is outside the limits {axis.min:g}..{axis.max:g}"
                )

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

    async def _home(self) -> None:
        await self._driver.home()
        self._homed = True

    async def _move(self, target: Mapping[str, float], speed: float | None) -> None:
        await self._driver.move_to(target, speed)

    async def _on_no_clients(self) -> None:
        if self._busy:
            log.warning("last client disconnected during motion; stopping")
            await self._driver.stop()

    def _publish_state(self) -> None:
        self._bus.publish(MachineStateEvent(machine=self.state))
