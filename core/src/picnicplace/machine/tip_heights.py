"""Measuring a nozzle tip's height on a paper square (spec §7.7, lines 6 and 9).

Calibrating the nozzle offsets finds where each nozzle is and how far down it reaches,
but only with tips small enough to seal on the paper disc. Where a nozzle is does not
change with its tip; how far down it reaches, and its vacuum levels, do. A LumenPnP comes
with one tip of each size, and the bigger ones (N24 and up) are too big for its disc, so
their heights are measured here instead, on a square of plain paper big enough for any
tip, laid on the datum board.

The top camera looks over the datum board for the paper: the point deepest inside a
white area, far enough from its edges for the tip and a millimeter of paper all round.
Then each nozzle sniffs the paper for its height, as on the disc, picks the square up to
read its vacuum holding it, and sets it down again. The height is kept for the nozzle
with that tip, beside the offsets, and the vacuum levels with the others.
"""

from collections.abc import Awaitable, Callable

import cv2
import numpy as np

from picnicplace.calibration import datum_key, nozzle_key
from picnicplace.machine.errors import InvalidTargetError
from picnicplace.machine.nozzle_offsets import (
    DISC_MIN_BRIGHTNESS,
    DISC_PRESS_MM,
    DISC_RIM_MM,
    PAPER_MM,
    NozzleOffsetError,
    check_seal,
    record_vacuum,
    search_area,
    search_spots,
    sniff_disc,
    turn_disc,
    vacuum_levels,
    with_tip,
)
from picnicplace.machine.profile import Nozzle, Point, VacuumLevels
from picnicplace.machine.seat_check import hide_pad, keep_references
from picnicplace.machine.service import MachineService
from picnicplace.machine.sniff import VacuumMeasure
from picnicplace.schemas import Operation

PAPER_WHITE = DISC_MIN_BRIGHTNESS
"""Paper is at least this bright (of 255, its brightest color channel); the datum board,
its copper and its printing are darker or too thin to hold a tip."""
FRAME_EDGE_PX = 1
"""Beyond the frame is not known to be paper: the area must lie inside it."""


def measured_offset(machine: MachineService, nozzle: Nozzle) -> bool:
    """Whether the nozzle's offset has been measured (with any tip)."""
    line = machine.calibration.get().lines.get("nozzle_offsets")
    return line is not None and nozzle_key(nozzle.id, "x") in line.values


def height_unknown(machine: MachineService, nozzle: Nozzle) -> bool:
    """Whether the tip on a nozzle has no height measured yet."""
    line = machine.calibration.get().lines.get("nozzle_offsets")
    return line is None or datum_key(nozzle.id, nozzle.tip) not in line.values


def tips_to_measure(machine: MachineService) -> list[Nozzle]:
    """Nozzles whose offset is measured but whose tip's height is not."""
    return [
        n
        for n in machine.profile.nozzles
        if n.tip is not None and measured_offset(machine, n) and height_unknown(machine, n)
    ]


def paper_needed(machine: MachineService, nozzles: list[Nozzle]) -> float:
    """How far from the paper's edges a tip touches it (mm): the biggest tip's radius and a
    millimeter of paper all round."""
    outer = [machine.profile.nozzle_tip(n.tip) for n in nozzles]
    return max((t.outer_diameter or 0.0) / 2 for t in outer if t is not None) + DISC_RIM_MM


def deepest_white(image: np.ndarray) -> tuple[float, float, float]:
    """The point deepest inside the white areas of a picture (x, y in pixels), and how far
    it is from the nearest edge of white, or the frame's edge (pixels)."""
    white = (image.max(axis=2) >= PAPER_WHITE).astype(np.uint8)
    white = np.pad(white, FRAME_EDGE_PX)
    distance = cv2.distanceTransform(white, cv2.DIST_L2, 5)
    _, depth, _, (x, y) = cv2.minMaxLoc(distance)
    return x - FRAME_EDGE_PX, y - FRAME_EDGE_PX, float(depth)


async def find_paper(machine: MachineService, radius: float) -> Point:
    """Look over the datum board with the top camera for a white area with room for a
    circle of ``radius`` mm: its deepest point."""
    camera = machine.top_camera()
    fov = (camera.field_of_view.x, camera.field_of_view.y)
    fit = min(2 * radius, min(fov) - 1.0)
    best: tuple[float, Point] | None = None
    for spot in search_spots(search_area(machine), fov, fit):
        await machine.move_camera_to(spot.x, spot.y)
        frame = await machine.frame(camera.id)
        h, w = frame.image.shape[:2]
        mm = camera.field_of_view.x / w
        x, y, depth = deepest_white(hide_pad(machine, camera, spot, frame.image, mm))
        dx, dy = machine.image_to_machine(camera, (x - w / 2) * mm, (y - h / 2) * mm)
        at = Point(x=spot.x + dx, y=spot.y + dy)
        if best is None or depth * mm > best[0]:
            best = (depth * mm, at)
        if depth * mm >= radius:
            return at
    room = f" (the most room found was {best[0]:.1f} mm)" if best else ""
    raise NozzleOffsetError(
        f"the top camera found no paper on the datum board with {radius:.1f} mm of room all "
        f"round{room}: lay a square of plain white paper, at least {2 * radius + 4:.0f} mm "
        "across, flat on it, and check the top light"
    )


def tip_heights_work(
    machine: MachineService, nozzle_ids: list[str] | None
) -> Callable[[], Awaitable[str]]:
    """Measuring the heights of the tips on some nozzles (those whose tip has none yet,
    unless given) on a paper square, keeping them and using them from now on: checked
    now, run when awaited."""
    if nozzle_ids:
        nozzles = [machine.nozzle(i) for i in nozzle_ids]
    else:
        nozzles = tips_to_measure(machine)
        if not nozzles:
            raise InvalidTargetError(
                "every nozzle's tip has its height measured already: name a nozzle to "
                "measure it again"
            )
    line = machine.calibration.get().lines.get("nozzle_offsets")
    if line is not None and line.stale is not None:
        raise InvalidTargetError(
            f"the nozzle offsets are out of date ({line.stale}): calibrate them again first"
        )
    for n in nozzles:
        if n.tip is None:
            raise InvalidTargetError(f"say which tip is on {n.label.lower()} first")
        if not measured_offset(machine, n):
            raise InvalidTargetError(
                f"{n.label}'s offset has not been measured yet: calibrate the nozzle offsets "
                "first (the setup wizard's paper disc stop)"
            )

    async def work() -> str:
        spot = await find_paper(machine, paper_needed(machine, nozzles))
        raw: dict[str, float] = {}
        levels: dict[str, VacuumLevels] = {}
        old = {n.id: n.z_offset for n in nozzles}
        try:
            for nozzle in nozzles:
                # Measured afresh: the offset it has is another tip's, or the profile's.
                nozzle.z_offset = 0.0
                measure = VacuumMeasure()
                z = await sniff_disc(machine, nozzle, spot, PAPER_MM, measure, "the paper")
                held = await turn_disc(
                    machine,
                    nozzle,
                    spot,
                    0.0,
                    z + PAPER_MM - DISC_PRESS_MM,
                    measure.open + (measure.sealed - measure.open) / 2
                    if measure.open is not None and measure.sealed is not None
                    else None,
                    "the paper",
                )
                check_seal(nozzle, measure, held, z + PAPER_MM, "the paper")
                raw[nozzle.id] = z
                found = vacuum_levels(machine, nozzle, measure, held, "the paper")
                if found is not None:
                    levels[nozzle.id] = found
        finally:
            for n in nozzles:
                n.z_offset = old[n.id]
            await machine.raise_nozzles()

        # Kept with the offsets they go with, and said in the line's message.
        record = machine.calibration.get().lines["nozzle_offsets"]
        values = dict(record.values)
        parts: list[str] = []
        for nozzle in nozzles:
            values[datum_key(nozzle.id, nozzle.tip)] = raw[nozzle.id]
            parts.append(f"{with_tip(nozzle)} touched at Z {raw[nozzle.id]:.2f}")
        said = "; ".join(parts)
        marker = " Tip heights on paper: "
        before = record.message.split(marker)[0]
        earlier = record.message.split(marker)[1].rstrip(".") if marker in record.message else ""
        heights = "; ".join(p for p in (earlier, said) if p)
        machine.calibration.record("nozzle_offsets", f"{before}{marker}{heights}.", values)
        if levels:
            record_vacuum(machine, levels)
        machine.apply_calibration()
        return f"On the paper: {said}." + await keep_references(machine, nozzles)

    return work


def start_tip_heights(machine: MachineService, nozzle_ids: list[str] | None) -> Operation:
    """Measure tip heights on a paper square, keep them, and use them from now on."""
    return machine.start_task("tip_heights", tip_heights_work(machine, nozzle_ids))
