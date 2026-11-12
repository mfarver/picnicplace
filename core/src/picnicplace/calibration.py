"""The calibration checklist (spec §7.7, §8.2): every calibration the machine needs, in
order, each saying whether it is done, its value and when it was measured, or that it
is out of date and why.

What the machine measures (homing, the homing fiducial, the cameras, the nozzle offsets
and the nozzles' vacuum levels) is kept in ``calibration.json`` in the data directory,
with when it was measured. A crash (Z losing steps) marks what it may have knocked out
of true as out of date, until it is measured again. Lines the core cannot measure yet
say where their value comes from today (the profile, the config, or the bring-up
checklist).

Before a run, the lines the job needs are checked (``run_issues``): a job that aligns
parts on the bottom camera does not start until the bottom camera has a focus height,
and the plan says which line to do.
"""

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from picnicplace.machine.profile import MachineProfile, Point, VacuumLevels
from picnicplace.schemas import WireModel
from picnicplace.storage import write_atomic

if TYPE_CHECKING:
    from picnicplace.feeders.service import FeederService, FeedersState
    from picnicplace.machine.service import MachineService

LineId = Literal[
    "machine_frame",
    "top_camera_orientation",
    "homing_fiducial",
    "top_camera_scale",
    "bottom_camera",
    "nozzle_offsets",
    "nozzle_runout",
    "heights",
    "vacuum",
    "feeder_rails",
    "strips_and_trays",
    "boards",
]

LineStatus = Literal["done", "needed", "stale", "assumed", "every_run", "not_needed"]
"""``done``: measured, and still good. ``needed``: must be done, and is not. ``stale``:
measured, but out of date (the reason says why). ``assumed``: the value comes from the
profile or the config, and the machine cannot measure it yet. ``every_run``: done at
the start of each run. ``not_needed``: this machine or setup has nothing to calibrate."""

Action = Literal[
    "home", "visual_home", "camera_scale", "bottom_camera", "nozzle_offsets", "calibrate_feeders"
]
"""What starts a line's calibration, for the Studio's button."""

CRASH_STALES: tuple[LineId, ...] = (
    "homing_fiducial",
    "top_camera_scale",
    "nozzle_offsets",
    "vacuum",
)
"""What a crash may knock out of true: the datum board, the camera on the head, and the
nozzles and their tips."""
HOMING_OFF_WARN_MM = 1.0
"""Visual homing finding the machine farther off than this is worth a look."""
SCALE_WARN = 0.1
"""A measured camera scale this far (as a fraction) from the profile's is worth a look:
the measured one is used, but so far off suggests the wrong lens or camera height."""
ROTATION_WARN_DEG = 1.0
"""A bottom camera image turned more than this is worth straightening."""
NOZZLE_OFFSET_WARN_MM = 1.0
"""A nozzle measured farther than this from the profile's offset is worth a look: the
measured offset is used, but so far off suggests a bent nozzle or a loose head."""

TITLES: dict[LineId, str] = {
    "machine_frame": "Machine frame",
    "top_camera_orientation": "Top camera orientation",
    "homing_fiducial": "Homing fiducial",
    "top_camera_scale": "Top camera scale",
    "bottom_camera": "Bottom camera",
    "nozzle_offsets": "Nozzle offsets",
    "nozzle_runout": "Nozzle runout",
    "heights": "Heights",
    "vacuum": "Vacuum levels",
    "feeder_rails": "Feeder rails",
    "strips_and_trays": "Strips and trays",
    "boards": "Each board",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Measurement(_Model):
    measured_at: datetime
    message: str = Field(description="What was found, in words.")
    values: dict[str, float] = Field(default_factory=dict[str, float])
    stale: str | None = Field(
        default=None, description="Why it is out of date, if it is: measure it again."
    )


class CalibrationRecord(_Model):
    format: Literal["picnicplace-calibration"] = "picnicplace-calibration"
    version: Literal[1] = 1
    lines: dict[LineId, Measurement] = Field(default_factory=dict[LineId, Measurement])


class CalibrationStore:
    """What the machine has measured, kept across restarts. With no path (tests), it is
    kept in memory."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._memory = CalibrationRecord()

    def get(self) -> CalibrationRecord:
        if self._path is None:
            return self._memory.model_copy(deep=True)
        if not self._path.is_file():
            return CalibrationRecord()
        try:
            return CalibrationRecord.model_validate_json(self._path.read_text())
        except ValueError:
            return CalibrationRecord()

    def _put(self, record: CalibrationRecord) -> None:
        if self._path is None:
            self._memory = record
        else:
            write_atomic(self._path, record.model_dump_json(indent=2))

    def record(
        self, line: LineId, message: str, values: dict[str, float] | None = None
    ) -> Measurement:
        """Keep a measurement, replacing the last (and whatever made it out of date)."""
        with self._lock:
            record = self.get()
            measurement = Measurement(
                measured_at=datetime.now(UTC).replace(microsecond=0),
                message=message,
                values=values or {},
            )
            record.lines[line] = measurement
            self._put(record)
            return measurement

    def mark_stale(self, lines: tuple[LineId, ...], reason: str) -> None:
        """Mark measurements out of date, saying why."""
        with self._lock:
            record = self.get()
            for line in lines:
                if line in record.lines:
                    record.lines[line].stale = reason
            self._put(record)


class CalibrationLine(WireModel):
    id: LineId
    number: int = Field(
        description="Its place in the order (spec §7.7): each needs the ones before."
    )
    title: str
    status: LineStatus
    detail: str = Field(description="Its value, or what to do, in words.")
    measured_at: datetime | None = None
    stale: str | None = Field(default=None, description="Why it is out of date, if it is.")
    warning: str | None = Field(
        default=None, description="Something about the value worth checking."
    )
    action: Action | None = Field(default=None, description="What starts it, if the core can.")


class CalibrationChecklist(WireModel):
    lines: list[CalibrationLine]

    def line(self, line_id: LineId) -> CalibrationLine:
        return next(line for line in self.lines if line.id == line_id)


Z_OFFSET_LIMIT_MM = 2.0
"""A nozzle touching the datum board farther than this from the profile's height for it
is not trusted: its Z offset is left as it was."""


def datum_key(nozzle_id: str) -> str:
    """The key of where a nozzle touched the datum board (its raw height) in the nozzle
    offsets' values."""
    return f"datum.{nozzle_id}"


def nozzle_key(nozzle_id: str, axis: Literal["x", "y"]) -> str:
    """The key of a nozzle's offset in a measurement's values."""
    return f"nozzle.{nozzle_id}.{axis}"


VacuumName = Literal["open", "sealed", "held", "threshold"]


def vacuum_key(nozzle_id: str, name: VacuumName) -> str:
    """The key of one of a nozzle's vacuum levels in the vacuum line's values."""
    return f"{name}.{nozzle_id}"


def apply_measurements(profile: MachineProfile, record: CalibrationRecord) -> None:
    """Use what the machine measured in place of the profile's values: the top camera's
    scale, the nozzle offsets and vacuum levels, and the bottom camera's place, focus
    height, and scale.
    Out-of-date values are still the best known, and are used until measured again."""
    offsets = record.lines.get("nozzle_offsets")
    datum = next(
        (loc.z for loc in profile.locations if loc.kind == "homing_fiducial" and loc.z is not None),
        None,
    )
    for nozzle in profile.nozzles:
        v = offsets.values if offsets else {}
        kx, ky = nozzle_key(nozzle.id, "x"), nozzle_key(nozzle.id, "y")
        if kx in v and ky in v:
            nozzle.offset = Point(x=v[kx], y=v[ky])
        # Where the nozzle touched the datum board, against where the profile says the
        # board is: its tip's length, and the frame's height, in one.
        touched = v.get(datum_key(nozzle.id))
        if touched is not None and datum is not None and abs(touched - datum) <= Z_OFFSET_LIMIT_MM:
            nozzle.z_offset = touched - datum
        vacuum = record.lines.get("vacuum")
        levels = (
            {
                name: vacuum.values.get(vacuum_key(nozzle.id, name))
                for name in ("open", "sealed", "held", "threshold")
            }
            if vacuum
            else {}
        )
        if levels and all(x is not None for x in levels.values()):
            nozzle.vacuum = VacuumLevels.model_validate(levels)
    for camera in profile.cameras:
        if camera.looking == "down" and camera.mount == "head":
            m = record.lines.get("top_camera_scale")
        elif camera.looking == "up":
            m = record.lines.get("bottom_camera")
        else:
            continue
        if m is None:
            continue
        v = m.values
        if {"x", "y", "width", "height"} <= v.keys() and camera.looking == "down":
            camera.field_of_view = Point(x=v["x"] * v["width"], y=v["y"] * v["height"])
        if {"x", "y", "focus", "scale_x", "scale_y", "width", "height"} <= v.keys():
            # The place was found with a nozzle's tip: where the nozzle is now known to
            # be, measured since, moves it with the nozzle.
            dx = dy = 0.0
            for nozzle in profile.nozzles:
                kx, ky = nozzle_key(nozzle.id, "x"), nozzle_key(nozzle.id, "y")
                if kx in v and ky in v:
                    dx, dy = nozzle.offset.x - v[kx], nozzle.offset.y - v[ky]
            camera.position = Point(x=v["x"] + dx, y=v["y"] + dy)
            camera.focus_height = v["focus"]
            camera.field_of_view = Point(x=v["scale_x"] * v["width"], y=v["scale_y"] * v["height"])


def _measured(
    line_id: LineId, number: int, record: CalibrationRecord, action: Action | None
) -> CalibrationLine | None:
    m = record.lines.get(line_id)
    if m is None:
        return None
    return CalibrationLine(
        id=line_id,
        number=number,
        title=TITLES[line_id],
        status="stale" if m.stale else "done",
        detail=m.message,
        measured_at=m.measured_at,
        stale=m.stale,
        action=action,
    )


def build_checklist(
    profile: MachineProfile,
    homed: bool,
    record: CalibrationRecord,
    feeders: "FeedersState | None",
) -> CalibrationChecklist:
    """Every calibration this machine needs, in order, with where each stands."""
    lines: list[CalibrationLine] = []

    def add(
        line_id: LineId,
        status: LineStatus,
        detail: str,
        action: Action | None = None,
        **extra: str | None,
    ) -> None:
        lines.append(
            CalibrationLine(
                id=line_id,
                number=len(lines) + 1,
                title=TITLES[line_id],
                status=status,
                detail=detail,
                action=action,
                warning=extra.get("warning"),
            )
        )

    def add_measured(line: CalibrationLine, warning: str | None = None) -> None:
        line.number = len(lines) + 1
        line.warning = warning
        lines.append(line)

    # 1. Homing, and Z reading as the profile expects with the nozzles level.
    frame = record.lines.get("machine_frame")
    if homed:
        add(
            "machine_frame",
            "done",
            f"Homed, with Z at {profile.home_z:g} as the profile expects. Which way each "
            "axis moves and which output is each pump and valve are checked by hand, in "
            "the bring-up checklist.",
        )
        lines[-1].measured_at = frame.measured_at if frame else None
    else:
        add("machine_frame", "needed", "Home the machine: everything else needs it.", "home")

    # 2. Top camera flips and rotation.
    add(
        "top_camera_orientation",
        "assumed",
        "From the config's camera settings, checked by hand: the machine cannot measure it yet.",
    )

    # 3. The homing fiducial on the datum board.
    has_fiducial = any(loc.kind == "homing_fiducial" for loc in profile.locations)
    if not has_fiducial:
        add("homing_fiducial", "not_needed", "This machine's profile has no homing fiducial.")
    elif (line := _measured("homing_fiducial", 0, record, "visual_home")) is not None:
        off = record.lines["homing_fiducial"].values.get("off", 0.0)
        warning = (
            f"Homing left the machine {off:.2f} mm off: check the homing switches"
            if off > HOMING_OFF_WARN_MM
            else None
        )
        add_measured(line, warning)
    else:
        add(
            "homing_fiducial",
            "needed",
            "Visual homing has not found the datum board's center dot yet.",
            "visual_home",
        )

    # 4. Top camera millimeters per pixel.
    if not has_fiducial:
        add(
            "top_camera_scale",
            "assumed",
            "From the profile's field of view: measuring it needs a homing fiducial.",
        )
    elif (line := _measured("top_camera_scale", 0, record, "camera_scale")) is not None:
        values = record.lines["top_camera_scale"].values
        measured = (values.get("x", 0.0) + values.get("y", 0.0)) / 2
        assumed = values.get("profile", 0.0)
        warning = None
        if assumed and abs(measured - assumed) / assumed > SCALE_WARN:
            warning = (
                f"{abs(measured - assumed) / assumed:.0%} from what the profile's field of "
                "view implies (the measured scale is used): check the lens and the "
                "camera's height"
            )
        add_measured(line, warning)
    else:
        add(
            "top_camera_scale",
            "assumed",
            "From the profile's field of view. Measure it to check.",
            "camera_scale",
        )

    # 5. Bottom camera position and focus.
    up = [c for c in profile.cameras if c.looking == "up"]
    if not up:
        add("bottom_camera", "not_needed", "This machine has no bottom camera.")
    elif (line := _measured("bottom_camera", 0, record, "bottom_camera")) is not None:
        turned = record.lines["bottom_camera"].values.get("rotation", 0.0)
        warning = (
            f"Its image is turned {turned:+.1f}°: turn the camera, or set its rotation in "
            "the config"
            if abs(turned) > ROTATION_WARN_DEG
            else None
        )
        add_measured(line, warning)
    elif any(c.focus_height is None for c in up):
        add(
            "bottom_camera",
            "needed",
            "The profile gives no focus height for the bottom camera, so parts cannot be "
            "aligned: set its focus_height in the local profile.",
        )
    else:
        camera = up[0]
        add(
            "bottom_camera",
            "assumed",
            f"At ({camera.position.x:g}, {camera.position.y:g}), in focus with the nozzle at "
            f"Z {camera.focus_height:g}: from the profile. Calibrate it with an empty nozzle "
            "tip fitted.",
            "bottom_camera",
        )

    # 6. Nozzle offsets from the top camera.
    if (line := _measured("nozzle_offsets", 0, record, "nozzle_offsets")) is not None:
        values = record.lines["nozzle_offsets"].values
        off = max((v for k, v in values.items() if k.startswith("off.")), default=0.0)
        warning = (
            f"A nozzle is {off:.2f} mm from the profile's offset (the measured offset is "
            "used): check it is straight and the head is tight"
            if off > NOZZLE_OFFSET_WARN_MM
            else None
        )
        add_measured(line, warning)
    else:
        add(
            "nozzle_offsets",
            "assumed",
            "From the profile. Lay a small paper disc (from a hole punch) on the datum board, "
            "center the top camera on it, and calibrate: each nozzle picks it up, turns it, "
            "and sets it down.",
            "nozzle_offsets",
        )

    # 7-8. Not measured yet, or not alone.
    add("nozzle_runout", "assumed", "Not measured: taken as none.")
    fixed = feeders.fixed if feeders else []
    no_height = [f.id for f in fixed if f.feeder.pick_height is None]
    if no_height:
        add(
            "heights",
            "needed",
            f"{', '.join(no_height)} {'has' if len(no_height) == 1 else 'have'} no pick "
            "height: jog the nozzle down to a part and set it (Feeders workspace).",
        )
    else:
        touched = "nozzle_offsets" in record.lines and any(
            datum_key(n.id) in record.lines["nozzle_offsets"].values for n in profile.nozzles
        )
        nozzles = (
            "Each nozzle's heights are shifted by where it touched the datum board (line 6): "
            + ", ".join(f"{n.label} {n.z_offset:+.2f} mm" for n in profile.nozzles)
            + ". "
            if touched
            else "Every nozzle is taken as reaching the profile's heights (line 6 measures "
            "each one on the datum board). "
        )
        add(
            "heights",
            "assumed",
            nozzles + "Board and feeder heights from the profile, and strips' and trays' as set "
            "by hand; parts' heights from the library.",
        )

    # 9. Vacuum levels, measured with the nozzle offsets.
    if (line := _measured("vacuum", 0, record, "nozzle_offsets")) is not None:
        add_measured(line)
    else:
        add(
            "vacuum",
            "assumed",
            "One threshold for every nozzle, from the profile. Calibrating the nozzle offsets "
            "(line 6) measures each nozzle's own: open, sealed, and holding a part.",
            "nozzle_offsets",
        )

    # 10. Feeder rails.
    if feeders is None or not feeders.rails:
        add("feeder_rails", "not_needed", "This machine has no feeder rails.")
    else:
        used = {s.slot.bank for s in feeders.slots if s.feeder is not None}
        rails = [r for r in feeders.rails if r.bank in used]
        if not feeders.scanned and feeders.bus is not None:
            add("feeder_rails", "needed", "Scan the feeders first.", "calibrate_feeders")
        elif not rails:
            add("feeder_rails", "not_needed", "No feeders are on the rails.")
        else:
            unfitted = [r.label for r in rails if not (r.calibration and r.calibration.fitted)]
            if unfitted:
                add(
                    "feeder_rails",
                    "needed",
                    f"Not fitted yet: {', '.join(unfitted)}. Each feeder's fiducial is still "
                    "found on its first pick in a run.",
                    "calibrate_feeders",
                )
            else:
                add(
                    "feeder_rails",
                    "done",
                    f"{len(rails)} rail{'s' if len(rails) != 1 else ''} fitted to their feeders' "
                    "fiducials.",
                    "calibrate_feeders",
                )

    # 11. Strips and trays.
    if not fixed:
        add("strips_and_trays", "not_needed", "No strips or trays are on the bed.")
    else:
        add(
            "strips_and_trays",
            "done",
            f"{len(fixed)} taught: {', '.join(f.id for f in fixed)}.",
        )

    # 12. Each board.
    add("boards", "every_run", "Its fiducials are found at the start of every run.")
    return CalibrationChecklist(lines=lines)


def current(machine: "MachineService", feeders: "FeederService | None") -> CalibrationChecklist:
    """The checklist as the machine stands now."""
    return build_checklist(
        machine.profile,
        machine.state.homed,
        machine.calibration.get(),
        feeders.state() if feeders else None,
    )


def run_issues(
    checklist: CalibrationChecklist, aligns: bool
) -> list[tuple[Literal["error", "warning"], str]]:
    """What the checklist says about a run: the lines the job needs that are not done
    (errors), and measurements out of date (warnings). ``aligns``: the job has parts to
    align on the bottom camera."""
    issues: list[tuple[Literal["error", "warning"], str]] = []
    for line in checklist.lines:
        where = f"calibration line {line.number}, {line.title.lower()}"
        if line.id == "bottom_camera" and aligns and line.status == "needed":
            issues.append(("error", f"{where}, is not done: {line.detail}"))
        elif line.status == "stale":
            issues.append(("warning", f"{where}, is out of date ({line.stale}): do it again"))
    return issues
