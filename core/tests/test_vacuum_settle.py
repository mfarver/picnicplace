"""Judging a vacuum reading settled (MachineService.settled_vacuum), and a part held after
a lift (MachineService.holds_after_lift), on readings from a real LumenPnP."""

import asyncio
from types import SimpleNamespace
from typing import Any

from picnicplace.machine.profile import ValueRange
from picnicplace.machine.service import MachineService

# The left nozzle with an N045 tip, suction on in open air (2026-11-18): the sensor updates
# about every 0.1 s, in steps of 65,536 counts, and settles after 0.6 s.
REAL = [(0.0, -65536.0), (0.2, -983040.0), (0.3, -1179648.0), (0.41, -1310720.0),
        (0.515, -1376256.0), (0.62, -1441792.0)]  # fmt: skip


class _Clocked:
    """Just enough of a machine service: a vacuum sensor whose reading follows ``readings``
    (seconds, counts) in time."""

    def __init__(self, readings: list[tuple[float, float]] = REAL) -> None:
        self.start = asyncio.get_running_loop().time()
        self.readings = readings
        self._driver = SimpleNamespace(refresh_input=self._refresh, time_scale=1.0)
        self.sensor = SimpleNamespace(id="vac", range=SimpleNamespace(min=-16777216, max=0))

    async def _refresh(self, _: str) -> None:
        pass

    def vacuum_sensor(self, _: Any) -> Any:
        return self.sensor

    def vacuum_reading(self, _: Any) -> float:
        t = asyncio.get_running_loop().time() - self.start
        return [v for at, v in self.readings if at <= t][-1]


def test_an_open_reading_is_not_taken_while_the_vacuum_still_rises() -> None:
    """Two polls 0.05 s apart agree while the sensor waits for its next update: the
    reading must hold for a while before it counts as settled. Taken at 0.3 s, as it
    was, the open reading was 262,144 counts short, and the first step of a touch read
    as a seal."""

    async def run() -> float | None:
        fake = _Clocked()
        settled = MachineService.settled_vacuum
        return await settled(fake, None, 0.3, 2.0)  # type: ignore[arg-type]

    assert asyncio.run(run()) == -1441792.0


# The left nozzle with an N08, lifting a 0603 resistor on board 2 of a real run (2026-11-23),
# from when the lift ended: the reading kept creeping stronger for most of a second, so
# waiting for it to settle took 0.9 s.
LIFTED = [(0.0, -196608.0), (0.13, -1900544.0), (0.23, -1966080.0), (0.34, -2031616.0),
          (0.66, -2097152.0), (0.88, -2031616.0)]  # fmt: skip
# A part dropped as the nozzle lifted: the reading still says sealed, then weakens.
DROPPED = [(0.0, -1966080.0), (0.1, -1900544.0), (0.2, -1769472.0), (0.3, -1572864.0),
           (0.4, -1245184.0)]  # fmt: skip


class _Lifted(_Clocked):
    """A LumenPnP's left nozzle after a lift: its sensor's range and held level."""

    def __init__(self, readings: list[tuple[float, float]]) -> None:
        super().__init__(readings)
        self.sensor = SimpleNamespace(id="vac", range=ValueRange(min=0, max=-2500000))
        self.recorder = SimpleNamespace(event=lambda *_, **__: None)

    def vacuum_threshold(self, _: Any) -> float:
        return -1600000.0

    def reads_held(self, nozzle: Any, value: float | None, level: float | None = None) -> bool:
        return MachineService.reads_held(self, nozzle, value, level)  # type: ignore[arg-type]

    async def settled_vacuum(self, *args: Any, **kwargs: Any) -> float | None:
        return await MachineService.settled_vacuum(self, *args, **kwargs)  # type: ignore[arg-type]


def lift(readings: list[tuple[float, float]]) -> tuple[bool | None, float | None, float]:
    """Whether the part is held after a lift, the reading, and how long that took."""

    async def run() -> tuple[bool | None, float | None, float]:
        fake = _Lifted(readings)
        nozzle = SimpleNamespace(id="N1")
        held, reading = await MachineService.holds_after_lift(fake, nozzle, 1.0)  # type: ignore[arg-type]
        return held, reading, asyncio.get_running_loop().time() - fake.start

    return asyncio.run(run())


def test_a_part_held_after_a_lift_is_known_before_the_reading_settles() -> None:
    """Readings past the held level, none weaker than the last, for 0.25 s: held, at
    about 0.4 s after the lift rather than 0.9 s."""
    held, reading, took = lift(LIFTED)
    assert held is True and reading is not None and reading <= -1900544.0
    assert 0.35 <= took < 0.5, took


def test_a_part_dropped_on_the_way_up_still_waits_for_the_reading_to_fall() -> None:
    """Past the held level just after the lift, but weakening: not taken for held, and
    once the reading has fallen and settled, the part is known to be gone."""
    held, reading, took = lift(DROPPED)
    assert held is False and reading == -1245184.0
    assert took >= 0.7, took
