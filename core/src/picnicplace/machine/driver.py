"""The interface every motion driver implements (spec section 7.2).

Drivers talk to machine firmware. They do not enforce safety policy (presence, limits,
homing); the machine service does that before calling them.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence


class MotionDriver(ABC):
    name: str

    def __init__(self) -> None:
        self.on_change: Callable[[], None] | None = None
        """Called whenever position or connection state changes."""

    @property
    @abstractmethod
    def axes(self) -> Sequence[str]: ...

    @property
    @abstractmethod
    def position(self) -> Mapping[str, float]: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def home(self) -> None:
        """Home all axes. Raises MotionInterruptedError if stopped."""

    @abstractmethod
    async def move_to(self, target: Mapping[str, float], speed: float | None) -> None:
        """Move to an absolute position. Raises MotionInterruptedError if stopped."""

    @abstractmethod
    async def stop(self) -> None:
        """Controlled stop: end the current move and keep position knowledge."""

    @abstractmethod
    async def emergency_stop(self) -> None:
        """Immediate stop that bypasses any command queue."""

    @abstractmethod
    async def reset(self) -> None:
        """Recover after an emergency stop."""

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()
