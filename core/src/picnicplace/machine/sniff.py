"""Finding a height by touch (spec §7.7, line 8): sniffing.

A nozzle with suction on steps down onto a surface until the vacuum says its tip has
sealed on it. On a flat surface (the datum board, a board's fiducial) that marks the
surface's top; on a part in its pocket, the part's top, and the part is then held, so
sniffing is also a pick.

Heights here, as everywhere, are shifted by the nozzle's Z offset, so a sniff with one
nozzle holds for the others. The nozzle never goes below ``expected - below``, nor below
the bottom of its axis's travel.

The vacuum is read open at safe height first. A nozzle whose levels have been measured
(line 9) seals when the reading moves from that by as much as from its measured open
level to its threshold, so a pump that has warmed up or a filter that has clogged a
little since does not throw it off. One not measured yet seals at its sensor's
threshold, except while its levels are being measured (``VacuumMeasure``): then any
clear jump from the open reading counts.
"""

import asyncio
from dataclasses import dataclass

from picnicplace.machine.errors import MachineError
from picnicplace.machine.profile import Nozzle, Point
from picnicplace.machine.service import MachineService

SNIFF_STEP_MM = 0.1
SNIFF_WAIT_S = 0.3
"""How long each step waits for the vacuum to seal (in a simulator running faster than
real time, less, down to ``MIN_WAIT_S``)."""
MIN_WAIT_S = 0.1
CLEAR_WAIT_S = 1.0
"""How long the vacuum is given to settle open before the first step."""
MEASURE_JUMP = 0.1
"""While measuring a nozzle's levels, a reading this far (as a fraction of its sensor's
range) past the open reading means the tip sealed."""


class SniffError(MachineError):
    """Sniffing found nothing to seal on."""


@dataclass
class VacuumMeasure:
    """Asks a sniff to measure the nozzle's vacuum levels as it goes: what it read open,
    and sealed (None until it seals)."""

    open: float | None = None
    sealed: float | None = None


async def sniff(
    machine: MachineService,
    nozzle: Nozzle,
    at: Point,
    expected: float,
    above: float,
    below: float,
    what: str,
    hold: bool = False,
    angle: float | None = None,
    dwell_s: float = 0.0,
    measure: VacuumMeasure | None = None,
) -> float | None:
    """Step ``nozzle`` down over ``at`` from ``expected + above``, with suction on, until
    the vacuum seals: the height it sealed at, or None if it reached ``expected - below``
    without sealing. ``what`` names the surface, for errors. With ``hold``, suction stays
    on after a seal, as for a part to keep; the nozzle is raised either way. ``angle``:
    the nozzle's turn, if it matters; ``dwell_s``: how long to wait after a seal before
    lifting, for a part to settle on the tip. ``measure``: fill in the vacuum levels."""
    sensor = machine.vacuum_sensor(nozzle)
    if sensor is None or sensor.range is None:
        raise SniffError(f"{nozzle.label} has no vacuum sensor, so it cannot sniff for {what}")
    span = sensor.range.max - sensor.range.min
    axis = machine.profile.axis(nozzle.z_axis)
    lowest = (axis.min if nozzle.z_sign == 1 else -axis.max) - nozzle.z_offset
    floor = max(expected - below, lowest)
    start = expected + above
    wait = max(MIN_WAIT_S, SNIFF_WAIT_S / machine.driver.time_scale)
    await machine.move_nozzle_over(nozzle, at.x, at.y, angle)
    await machine.suction(nozzle, True)
    found: float | None = None
    try:
        # Suction on at safe height: an open tip reads open before it nears anything.
        open_reading = await machine.settled_vacuum(nozzle, wait, CLEAR_WAIT_S)
        if open_reading is None:
            raise SniffError(f"{nozzle.label}'s vacuum sensor gives no reading")
        level: float | None
        if measure is not None:
            measure.open = open_reading
            level = open_reading + MEASURE_JUMP * span
        else:
            if machine.reads_held(nozzle, open_reading):
                raise SniffError(
                    f"{nozzle.label} reads as holding a part before touching {what}: take "
                    "off whatever is on it, and check its tip is clear"
                    + (
                        ""
                        if nozzle.vacuum is not None
                        else ". A tip that leaks can read like this too: calibrating the "
                        "nozzle offsets (line 6) measures this nozzle's own vacuum levels"
                    )
                )
            levels = nozzle.vacuum
            level = None if levels is None else open_reading + levels.threshold - levels.open
        for step in range(round((start - floor) / SNIFF_STEP_MM) + 1):
            z = max(floor, round(start - step * SNIFF_STEP_MM, 3))
            await machine.lower_nozzle(nozzle, z)
            if await machine.holds_part(nozzle, True, wait, level):
                if step == 0:
                    # Sealed where it started: the surface may be higher still.
                    raise SniffError(
                        f"{nozzle.label} sealed on {what} as soon as it came down to Z "
                        f"{z:g}, {above:g} mm above where it was expected: it is higher than "
                        "that. Check the height it was expected at"
                    )
                found = z
                if measure is not None:
                    measure.sealed = await machine.settled_vacuum(nozzle, 0.0, CLEAR_WAIT_S)
                if hold and dwell_s > 0:
                    await asyncio.sleep(dwell_s)
                break
    finally:
        if found is None or not hold:
            await machine.suction(nozzle, False)
        await machine.raise_nozzles()
    return found


def floor_message(nozzle: Nozzle, what: str, expected: float, below: float) -> str:
    """Says a sniff found nothing, for an error."""
    return (
        f"{nozzle.label} did not seal on {what} down to {below:g} mm below where it was "
        f"expected (Z {expected:g})"
    )
