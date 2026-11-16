"""Checking a tip sits as it did when its height was measured (spec §7.7, line 6).

A tip's height is measured on paper (the disc, or a square), and kept for that tip. A tip
pushed on again may sit a little higher or lower than it did, and every height it reaches
is off by as much. Laying paper down at every tip change is a chore, and the bare datum
board will not do: a real tip leaks on its printing until pressed some 3 mm into its
spring. So the owner glues a small square of plain paper flat on the datum board once, the
paper pad, and sets its place (a location of kind ``paper_pad``, in the local profile).

Right after a tip's height is measured, it touches the pad, and that touch is kept as its
reference. After a tip change, each tip touches the pad again: a touch more than
``SEAT_WARN_MM`` from its reference means the tip sits differently, and the operator is
told to push it fully on, or measure its height again. The check changes nothing itself.
The top camera looks at the pad before every touch: if it does not see paper there, with
room round the tip, the touch is skipped rather than touch something else.

Searches for the paper disc and the paper square leave the pad out (``PAD_CLEAR_MM``).
"""

import math

import cv2
import numpy as np

from picnicplace.calibration import board_key, datum_key
from picnicplace.machine.profile import Camera, Nozzle, Point
from picnicplace.machine.service import MachineService
from picnicplace.machine.sniff import SniffError, sniff

SEAT_WARN_MM = 0.15
"""A touch this far from the tip's reference says it sits differently."""
TOUCH_ABOVE_MM = 2.0
TOUCH_BELOW_MM = 0.5
"""The touch goes no further than this below where the tip touched the paper disc or
square (the board's top under it): the pad's paper lies above that."""
PAD_ROOM_MM = 0.5
"""Paper seen all round the tip's face on the pad."""
PAD_WHITE = 150.0
"""Paper is at least this bright (of 255, its brightest color channel), as for the disc."""
PAD_CLEAR_MM = 9.0
"""Searches for the paper disc and the paper square leave out this much round the pad's
place: a pad about 10 mm across, and a little more."""


def paper_pad(machine: MachineService) -> Point | None:
    """Where the paper pad is, if its place has been set."""
    pad = next((loc for loc in machine.profile.locations if loc.kind == "paper_pad"), None)
    return None if pad is None else pad.position


def _board_height(machine: MachineService) -> float | None:
    fiducial = next(
        (loc for loc in machine.profile.locations if loc.kind == "homing_fiducial"), None
    )
    return None if fiducial is None else fiducial.z


def near_pad(machine: MachineService, at: Point) -> bool:
    """Whether a point is on or near the paper pad, so not the disc or the square."""
    pad = paper_pad(machine)
    return pad is not None and math.hypot(at.x - pad.x, at.y - pad.y) <= PAD_CLEAR_MM


def hide_pad(
    machine: MachineService, camera: Camera, seen_from: Point, image: np.ndarray, mm: float
) -> np.ndarray:
    """A copy of a top camera picture taken from ``seen_from`` with the paper pad (and
    ``PAD_CLEAR_MM`` round it) painted black, so a search for paper does not find it."""
    pad = paper_pad(machine)
    if pad is None:
        return image
    # The picture's axes in the machine's, to find where the pad is in it.
    rx, ry = machine.image_to_machine(camera, 1.0, 0.0)
    dx, dy = machine.image_to_machine(camera, 0.0, 1.0)
    ex, ey = pad.x - seen_from.x, pad.y - seen_from.y
    det = rx * dy - dx * ry
    right, down = (ex * dy - dx * ey) / det, (rx * ey - ex * ry) / det
    h, w = image.shape[:2]
    hidden = image.copy()
    center = (round(w / 2 + right / mm), round(h / 2 + down / mm))
    cv2.circle(hidden, center, round(PAD_CLEAR_MM / mm), (0, 0, 0), -1)
    return hidden


def _room(machine: MachineService, nozzle: Nozzle) -> float:
    tip = machine.profile.nozzle_tip(nozzle.tip) if nozzle.tip else None
    radius = (tip.outer_diameter or 0.0) / 2 if tip is not None else 0.0
    return radius + PAD_ROOM_MM


async def _on_pad(machine: MachineService, pad: Point, room: float) -> bool:
    """Whether the top camera sees paper at the pad, with ``room`` mm all round."""
    camera = machine.top_camera()
    await machine.move_camera_to(pad.x, pad.y)
    frame = await machine.frame(camera.id)
    h, w = frame.image.shape[:2]
    white = np.pad((frame.image.max(axis=2) >= PAD_WHITE).astype(np.uint8), 1)
    depth = cv2.distanceTransform(white, cv2.DIST_L2, 5)
    return float(depth[h // 2 + 1, w // 2 + 1]) * camera.field_of_view.x / w >= room


async def _touch(machine: MachineService, nozzle: Nozzle, spot: Point) -> float | None:
    """Touch the paper pad: the raw height the tip sealed at, or None."""
    board = _board_height(machine)
    if board is None:
        return None
    line = machine.calibration.get().lines.get("nozzle_offsets")
    paper = line.values.get(datum_key(nozzle.id, nozzle.tip)) if line else None
    # Where the tip touched the paper, in heights shifted by its offset as it stands.
    expected = board if paper is None else paper - nozzle.z_offset
    try:
        z = await sniff(
            machine,
            nozzle,
            spot,
            expected,
            TOUCH_ABOVE_MM,
            TOUCH_BELOW_MM,
            "the paper pad",
            angle=0.0,
        )
    except SniffError:
        return None
    return None if z is None else z + nozzle.z_offset


def _label(machine: MachineService, nozzle: Nozzle) -> str:
    return nozzle.label + (f" with the {nozzle.tip} tip" if nozzle.tip else "")


async def keep_references(machine: MachineService, nozzles: list[Nozzle]) -> str:
    """Right after these nozzles' tips had their heights measured on paper: touch the
    paper pad with each, and keep the touch as its reference. Says what could not be
    done, if anything; a failure never undoes the heights."""
    with_tips = [n for n in nozzles if n.tip is not None]
    line = machine.calibration.get().lines.get("nozzle_offsets")
    if not with_tips or line is None or _board_height(machine) is None:
        return ""
    pad = paper_pad(machine)
    if pad is None:
        return (
            " Glue a small square of plain paper flat on the datum board and set it as the "
            "paper pad, to have each tip checked after a tip change."
        )
    found: dict[str, float] = {}
    missed: list[str] = []
    try:
        for nozzle in with_tips:
            if not await _on_pad(machine, pad, _room(machine, nozzle)):
                missed.append(f"{_label(machine, nozzle)} (no paper seen at the pad)")
                continue
            z = await _touch(machine, nozzle, pad)
            if z is None:
                missed.append(f"{_label(machine, nozzle)} (it did not seal on the pad)")
                continue
            found[board_key(nozzle.id, nozzle.tip)] = z
    finally:
        await machine.raise_nozzles()
    latest = machine.calibration.get().lines["nozzle_offsets"]
    machine.calibration.record("nozzle_offsets", latest.message, {**latest.values, **found})
    if not missed:
        return ""
    return f" Not touched on the paper pad, so not checked after a tip change: {'; '.join(missed)}."


async def check_seats(machine: MachineService, nozzle_ids: list[str]) -> str:
    """After a tip change: touch the paper pad with each tip that has a reference, and
    say whether it sits as it did when its height was measured."""
    line = machine.calibration.get().lines.get("nozzle_offsets")
    pad = paper_pad(machine)
    if line is None or pad is None:
        return ""
    nozzles = [n for n in (machine.nozzle(i) for i in nozzle_ids) if n.tip is not None]
    said: list[str] = []
    try:
        for nozzle in nozzles:
            label = _label(machine, nozzle)
            reference = line.values.get(board_key(nozzle.id, nozzle.tip))
            if reference is None:
                continue
            if not await _on_pad(machine, pad, _room(machine, nozzle)):
                said.append(f"{label}: not checked, as the top camera saw no paper at the pad")
                continue
            z = await _touch(machine, nozzle, pad)
            if z is None:
                said.append(f"{label}: not checked, as it did not seal on the paper pad")
                continue
            moved = z - reference
            if abs(moved) > SEAT_WARN_MM:
                # Sealing higher up means the tip reaches further down: its end is lower.
                which = "lower" if moved > 0 else "higher"
                said.append(
                    f"{label}: its end is {abs(moved):.2f} mm {which} than when its height "
                    "was measured. Push it fully on and change tips again, or measure its "
                    "height again on paper (the setup wizard's paper stop)"
                )
            else:
                said.append(f"{label}: sits as when its height was measured ({moved:+.2f} mm)")
    finally:
        await machine.raise_nozzles()
    return "".join(f" {s}." for s in said)
