"""The interface every motion driver implements (spec section 7.2).

Drivers talk to machine firmware. They do not enforce safety policy (presence, limits,
homing); the machine service does that before calling them.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence

from picnicplace.machine.errors import NotSupportedError


class MotionDriver(ABC):
    name: str

    def __init__(self) -> None:
        self.on_change: Callable[[], None] | None = None
        """Called whenever position, connection, or I/O state changes."""

    firmware: str | None = None
    """Firmware name and version, once connected, if the driver knows it."""

    @property
    def time_scale(self) -> float:
        """How much faster than real time the machine runs: above 1 only in a simulator.
        Waits for the machine to settle (the vacuum, say) are divided by it."""
        return 1.0

    @property
    def halted(self) -> bool:
        """The firmware stopped itself (or after an emergency stop) and needs a reset."""
        return False

    async def set_position(self, position: Mapping[str, float]) -> None:
        """Redefine where the axes are, without moving (G92). Used by visual homing."""
        raise NotImplementedError(f"the {self.name} driver cannot set positions")

    @property
    def scene_position(self) -> Mapping[str, float]:
        """Where the machine physically is, for synthetic cameras. Real drivers report
        their position; the simulator adds its simulated homing error."""
        return self.position

    async def rs485(self, packet: bytes) -> bytes | None:
        """Send a packet on the feeder bus and return the reply, or None if none came."""
        raise NotSupportedError(f"the {self.name} driver has no feeder bus")

    async def refresh_input(self, input_id: str) -> None:
        """Read an input now rather than waiting for the next poll (for safety checks)."""
        return None

    @property
    def warnings(self) -> Sequence[str]:
        """Problems the operator should know about, such as a firmware mismatch."""
        return ()

    @property
    @abstractmethod
    def axes(self) -> Sequence[str]: ...

    @property
    @abstractmethod
    def position(self) -> Mapping[str, float]: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def inputs(self) -> Mapping[str, bool | float]:
        """Latest reading of every profile input: bool for digital, float for analog."""

    @property
    @abstractmethod
    def outputs(self) -> Mapping[str, bool]:
        """Current state of every profile output."""

    @abstractmethod
    async def set_output(self, output_id: str, on: bool) -> None: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def home(self) -> None:
        """Home all axes, Z first so X and Y travel with the nozzles up.

        Raises MotionInterruptedError if stopped.
        """

    @abstractmethod
    async def move_to(self, target: Mapping[str, float], speed: float | None) -> None:
        """Move to an absolute position. Raises MotionInterruptedError if stopped."""

    @abstractmethod
    async def stop(self) -> None:
        """Controlled stop: end the current move and keep position knowledge."""

    @abstractmethod
    async def emergency_stop(self) -> None:
        """Immediate stop that bypasses any command queue. Turns all outputs off."""

    @abstractmethod
    async def reset(self) -> None:
        """Recover after an emergency stop."""

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()
