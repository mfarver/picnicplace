"""A simulated machine for development and tests. No hardware required."""

import asyncio
from collections.abc import Mapping, Sequence

from picnicplace.config import MachineConfig
from picnicplace.machine.driver import MotionDriver
from picnicplace.machine.errors import MotionInterruptedError

TICK_S = 0.05
"""Real-time interval between position updates during a move."""

HOME_DURATION_S = 1.0
"""Simulated time a homing cycle takes."""


class SimulatorDriver(MotionDriver):
    name = "simulator"

    def __init__(self, config: MachineConfig) -> None:
        super().__init__()
        self._axes = config.axes
        self._time_scale = config.sim_time_scale
        self._position = {a.name: 0.0 for a in config.axes}
        self._connected = False
        self._stop = asyncio.Event()

    @property
    def axes(self) -> Sequence[str]:
        return [a.name for a in self._axes]

    @property
    def position(self) -> Mapping[str, float]:
        return dict(self._position)

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True
        self._notify()

    async def disconnect(self) -> None:
        self._stop.set()
        self._connected = False
        self._notify()

    async def home(self) -> None:
        self._stop.clear()
        await self._sleep_interruptible(HOME_DURATION_S / self._time_scale)
        self._position = {a.name: a.home for a in self._axes}
        self._notify()

    async def move_to(self, target: Mapping[str, float], speed: float | None) -> None:
        self._stop.clear()
        start = dict(self._position)
        end = {**start, **target}
        duration = 0.0
        for axis in self._axes:
            axis_speed = axis.max_speed if speed is None else min(speed, axis.max_speed)
            duration = max(duration, abs(end[axis.name] - start[axis.name]) / axis_speed)
        duration /= self._time_scale

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while True:
            frac = 1.0 if duration == 0 else min(1.0, (loop.time() - t0) / duration)
            self._position = {a: start[a] + (end[a] - start[a]) * frac for a in start}
            self._notify()
            if frac >= 1.0:
                return
            await self._sleep_interruptible(TICK_S)

    async def stop(self) -> None:
        self._stop.set()

    async def emergency_stop(self) -> None:
        self._stop.set()

    async def reset(self) -> None:
        self._stop.clear()

    async def _sleep_interruptible(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except TimeoutError:
            return
        raise MotionInterruptedError("motion was stopped")
