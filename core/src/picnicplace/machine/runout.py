"""Nozzle runout: how far each tip's end is from the axis it turns about (spec §7.7,
line 7).

A tip that is a little bent, or not pushed on straight, has its end off the axis the
nozzle turns about, so its end draws a circle as the nozzle turns. Each nozzle is turned
over the bottom camera, at its focus, through eight angles, and the tip's end is found at
each. A least-squares fit of a circle turning with the nozzle gives the axis (the
circle's center) and where the end is from it at 0 degrees.

Bottom vision already sees parts from the axis, so placing needs nothing more. Picks use
it: they put the tip's end, not the axis, over the part. Runout changes with the tip, so
it is kept for the tip fitted, as the heights are.
"""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from picnicplace.calibration import RUNOUT_WARN_MM, runout_key
from picnicplace.machine.bottom_camera import bottom_camera, find_tip, focus_height_for
from picnicplace.machine.errors import InvalidTargetError, VisionError
from picnicplace.machine.nozzle_offsets import with_tip
from picnicplace.machine.profile import Nozzle, Point
from picnicplace.machine.service import MachineService
from picnicplace.schemas import Operation

TURNS = tuple(float(a) for a in range(0, 360, 45))
"""The angles the tip's end is found at."""
SEARCH_MM = 1.5
"""The tip's end is looked for this far past its own size from the picture's middle."""
FIT_LIMIT_MM = 0.03
"""Ends further than this (root mean square) from the fitted circle mean the tip moved,
or was not seen well."""


@dataclass(frozen=True)
class Runout:
    nozzle: Nozzle
    end: Point
    """Where the tip's end is from the axis, with the nozzle at 0 degrees."""
    axis: Point
    """Where the axis was seen, from where the bottom camera's place says it should be."""
    spread: float
    """How far the ends are from the fitted circle (root mean square, mm)."""

    @property
    def radius(self) -> float:
        return math.hypot(self.end.x, self.end.y)


def fit(seen: list[tuple[float, float, float]]) -> tuple[Point, Point, float]:
    """The axis (cx, cy) and the end at 0 degrees (rx, ry) that best explain each end
    seen at (angle, x, y): x = cx + rx cos a - ry sin a, y = cy + rx sin a + ry cos a.
    Returns the axis, the end, and the root mean square left over."""
    rows: list[list[float]] = []
    rhs: list[float] = []
    for angle, x, y in seen:
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        rows += [[1.0, 0.0, c, -s], [0.0, 1.0, s, c]]
        rhs += [x, y]
    a, b = np.array(rows), np.array(rhs)
    solution = np.linalg.lstsq(a, b, rcond=None)[0]
    left = b - a @ solution
    rms = float(math.sqrt(float(np.mean(left.reshape(-1, 2) ** 2)) * 2))
    cx, cy, rx, ry = (float(v) for v in solution)
    return Point(x=cx, y=cy), Point(x=rx, y=ry), rms


async def measure_runout(machine: MachineService, nozzle: Nozzle) -> Runout:
    """Turn a nozzle over the bottom camera and fit the circle its tip's end makes."""
    camera = bottom_camera(machine)
    height = focus_height_for(machine, nozzle, camera)
    tip = machine.profile.nozzle_tip(nozzle.tip)
    size = tip.outer_diameter if tip is not None and tip.outer_diameter is not None else 2.0
    seen: list[tuple[float, float, float]] = []
    try:
        for i, turn in enumerate(TURNS):
            await machine.move_nozzle_over(nozzle, camera.position.x, camera.position.y, turn)
            if i == 0:
                await machine.lower_nozzle(nozzle, height)
            frame = await machine.frame(camera.id)
            w = frame.image.shape[1]
            mm = camera.field_of_view.x / w
            u, v, w, h, _ = await find_tip(machine, camera, nozzle, (size / 2 + SEARCH_MM) / mm, mm)
            dx, dy = machine.image_to_machine(camera, (u - w / 2) * mm, (v - h / 2) * mm)
            seen.append((turn, dx, dy))
    finally:
        await machine.raise_nozzles()
    axis, end, spread = fit(seen)
    if spread > FIT_LIMIT_MM:
        raise VisionError(
            f"{with_tip(nozzle)}: its tip's end did not go round a circle as it turned (it "
            f"strayed {spread:.3f} mm from one): check the tip is pushed fully on and clean, "
            "and the bottom camera's light"
        )
    return Runout(nozzle, end, axis, spread)


def _nozzles(machine: MachineService, nozzle_ids: list[str] | None) -> list[Nozzle]:
    """The nozzles named, or those whose fitted tip has no runout measured."""
    if nozzle_ids:
        return [machine.nozzle(i) for i in nozzle_ids]
    line = machine.calibration.get().lines.get("nozzle_runout")
    have = line.values if line else {}
    return [n for n in machine.profile.nozzles if runout_key(n.id, "x", n.tip) not in have]


def _axis_key(nozzle_id: str, axis: Literal["x", "y"], tip_id: str | None) -> str:
    """Where the axis was seen, from where the bottom camera expects it, in the values."""
    return "axis" + runout_key(nozzle_id, axis, tip_id).removeprefix("runout")


def _sentence(
    machine: MachineService, nozzle_id: str, tip_id: str | None, values: dict[str, float]
) -> str:
    label = machine.nozzle(nozzle_id).label + (f" with the {tip_id} tip" if tip_id else "")
    radius = math.hypot(
        values[runout_key(nozzle_id, "x", tip_id)], values[runout_key(nozzle_id, "y", tip_id)]
    )
    axis = math.hypot(
        values.get(_axis_key(nozzle_id, "x", tip_id), 0.0),
        values.get(_axis_key(nozzle_id, "y", tip_id), 0.0),
    )
    check = " (check it is straight)" if radius > RUNOUT_WARN_MM else ""
    return (
        f"{label}: its end is {radius:.3f} mm off its axis{check}, and the axis "
        f"{axis:.3f} mm from where the bottom camera expects it."
    )


def describe(machine: MachineService, values: dict[str, float]) -> str:
    """What the line says: every tip measured, on every nozzle."""
    said: list[str] = []
    for key in values:
        if not (key.startswith("runout.") and key.endswith(".x")):
            continue
        nozzle_id, _, tip_id = key.removeprefix("runout.").removesuffix(".x").partition(".")
        said.append(_sentence(machine, nozzle_id, tip_id or None, values))
    return " ".join(said)


def nozzle_runout_work(
    machine: MachineService, nozzle_ids: list[str] | None
) -> Callable[[], Awaitable[str]]:
    """Measuring the runout of the tips on some nozzles (those whose tip has none yet,
    unless given), keeping it and using it from now on: checked now, run when awaited.
    The nozzles are chosen when it runs, so tips changed before then are measured."""
    bottom = machine.calibration.get().lines.get("bottom_camera")
    if bottom is None:
        raise InvalidTargetError(
            "calibrate the bottom camera first: runout is measured over it (the setup "
            "wizard's first stop)"
        )
    for i in nozzle_ids or []:
        machine.nozzle(i)

    async def work() -> str:
        nozzles = _nozzles(machine, nozzle_ids)
        if not nozzles:
            return "Every tip fitted has its runout measured already."
        return _keep(machine, [await measure_runout(machine, n) for n in nozzles])

    return work


def _keys(nozzle_id: str, tip_id: str | None) -> tuple[str, ...]:
    return tuple(
        key(nozzle_id, axis, tip_id) for key in (runout_key, _axis_key) for axis in ("x", "y")
    )


def _keep(machine: MachineService, found: list[Runout]) -> str:
    """Keep the runout found, use it from now on, and say what it is."""
    line = machine.calibration.get().lines.get("nozzle_runout")
    values = dict(line.values) if line else {}
    for r in found:
        n = r.nozzle
        values.update(zip(_keys(n.id, n.tip), (r.end.x, r.end.y, r.axis.x, r.axis.y), strict=True))
    machine.calibration.record("nozzle_runout", describe(machine, values), values)
    machine.apply_calibration()
    return " ".join(_sentence(machine, r.nozzle.id, r.nozzle.tip, values) for r in found)


def forget_runout(machine: MachineService, nozzle_id: str, tip_id: str | None) -> None:
    """A tip just fitted sits as it was pushed on this time, not as before: forget the
    runout kept for it on that nozzle, so it is measured again."""
    line = machine.calibration.get().lines.get("nozzle_runout")
    keys = _keys(nozzle_id, tip_id)
    if line is None or not any(k in line.values for k in keys):
        return
    values = {k: v for k, v in line.values.items() if k not in keys}
    machine.calibration.record("nozzle_runout", describe(machine, values), values)


async def runout_after_tip_change(machine: MachineService, nozzle_ids: list[str]) -> str:
    """At the end of a tip change by hand: measure the runout of the tips just fitted, if
    the bottom camera is calibrated. Says what was found, or why it was not measured; a
    failure leaves the tips changed and the runout to measure (line 7)."""
    nozzles = [n for n in (machine.nozzle(i) for i in nozzle_ids) if n.tip is not None]
    if not nozzles or "bottom_camera" not in machine.calibration.get().lines:
        return ""
    try:
        found = [await measure_runout(machine, n) for n in nozzles]
    except VisionError as e:
        return (
            f" Runout not measured: {e}. Measure it again from the setup wizard's paper "
            "stop, or with Measure runout."
        )
    return " " + _keep(machine, found)


def start_nozzle_runout(machine: MachineService, nozzle_ids: list[str] | None) -> Operation:
    """Measure nozzle runout over the bottom camera, keep it, and use it from now on."""
    return machine.start_task("nozzle_runout", nozzle_runout_work(machine, nozzle_ids))
