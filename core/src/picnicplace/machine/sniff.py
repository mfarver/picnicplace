"""Finding a height by touch (spec §7.7, line 8): sniffing.

A nozzle with suction on steps down onto a surface until the vacuum says its tip has
sealed on it. On a flat surface (the datum board, a board's fiducial) that marks the
surface's top; on a part in its pocket, the part's top, and the part is then held, so
sniffing is also a pick.

Heights here, as everywhere, are shifted by the nozzle's Z offset, so a sniff with one
nozzle holds for the others. The nozzle never goes below ``expected - below``, nor below
the bottom of its axis's travel.
"""

import asyncio

from picnicplace.machine.errors import MachineError
from picnicplace.machine.profile import Nozzle, Point
from picnicplace.machine.service import MachineService

SNIFF_STEP_MM = 0.1
SNIFF_WAIT_S = 0.3
"""How long each step waits for the vacuum to seal (in a simulator running faster than
real time, less, down to ``MIN_WAIT_S``)."""
MIN_WAIT_S = 0.1
CLEAR_WAIT_S = 1.0
"""How long the vacuum is given to read open before the first step."""


class SniffError(MachineError):
    """Sniffing found nothing to seal on."""


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
) -> float | None:
    """Step ``nozzle`` down over ``at`` from ``expected + above``, with suction on, until
    the vacuum seals: the height it sealed at, or None if it reached ``expected - below``
    without sealing. ``what`` names the surface, for errors. With ``hold``, suction stays
    on after a seal, as for a part to keep; the nozzle is raised either way. ``angle``:
    the nozzle's turn, if it matters; ``dwell_s``: how long to wait after a seal before
    lifting, for a part to settle on the tip."""
    if nozzle.vacuum_input is None:
        raise SniffError(f"{nozzle.label} has no vacuum sensor, so it cannot sniff for {what}")
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
        if await machine.holds_part(nozzle, False, CLEAR_WAIT_S):
            raise SniffError(
                f"{nozzle.label} reads sealed before touching {what}: take off whatever is "
                "on it, and check its tip is clear"
            )
        for step in range(round((start - floor) / SNIFF_STEP_MM) + 1):
            z = max(floor, round(start - step * SNIFF_STEP_MM, 3))
            await machine.lower_nozzle(nozzle, z)
            if await machine.holds_part(nozzle, True, wait):
                if step == 0:
                    # Sealed where it started: the surface may be higher still.
                    raise SniffError(
                        f"{nozzle.label} sealed on {what} as soon as it came down to Z "
                        f"{z:g}, {above:g} mm above where it was expected: it is higher than "
                        "that. Check the height it was expected at"
                    )
                found = z
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
