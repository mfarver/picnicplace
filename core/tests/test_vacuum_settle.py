"""Judging a vacuum reading settled (MachineService.settled_vacuum), on readings from a
real LumenPnP."""

import asyncio
from types import SimpleNamespace
from typing import Any

from picnicplace.machine.service import MachineService

# The left nozzle with an N045 tip, suction on in open air (2026-11-18): the sensor updates
# about every 0.1 s, in steps of 65,536 counts, and settles after 0.6 s.
REAL = [(0.0, -65536.0), (0.2, -983040.0), (0.3, -1179648.0), (0.41, -1310720.0),
        (0.515, -1376256.0), (0.62, -1441792.0)]  # fmt: skip


class _Clocked:
    """Just enough of a machine service: a vacuum sensor whose reading follows ``REAL``
    in time."""

    def __init__(self) -> None:
        self.start = asyncio.get_running_loop().time()
        self._driver = SimpleNamespace(refresh_input=self._refresh, time_scale=1.0)
        self.sensor = SimpleNamespace(id="vac", range=SimpleNamespace(min=-16777216, max=0))

    async def _refresh(self, _: str) -> None:
        pass

    def vacuum_sensor(self, _: Any) -> Any:
        return self.sensor

    def vacuum_reading(self, _: Any) -> float:
        t = asyncio.get_running_loop().time() - self.start
        return [v for at, v in REAL if at <= t][-1]


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
