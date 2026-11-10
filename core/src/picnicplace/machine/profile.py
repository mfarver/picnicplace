"""Machine profiles: the declarative description of a machine (spec section 7.1).

A profile says what a machine is made of and where things are: axes and limits, the head
with its nozzles and cameras, fixed cameras, fiducials, feeder slots, and the inputs and
outputs. Drivers, the machine service, and clients (through the machine description
endpoint) all work from the same profile.

Profiles are TOML files. Built-in ones ship in ``picnicplace/profiles``; a config can also
point at a profile file by path. A profile can ``extends`` another (a built-in ID, or a
path relative to its own file) and give only what differs: a later model of a machine,
say, with its standard discard tray. Tables merge key by key, and lists of tables whose
items have an ``id`` (locations, keep-outs, cameras, ...) merge item by item; anything
else is replaced.

Profiles hold what a machine is. Built-in ones describe a model's design; the *local
profile* in the core's data directory (see ``machine/local.py``) is laid over it and
holds what is true of one particular machine, such as where its owner put the discard
tray, or keep-outs measured during bring-up.

Coordinates are the machine's own, in millimeters, as its firmware reports them. Machines
differ in where the origin is and which way the axes point; ``x_points`` and ``y_points``
say which way +X and +Y point for an operator facing the machine. Clients draw the
machine and its camera images that way (front at the bottom of the screen), so the
picture, the map, and the jog arrows all agree with what the operator sees. The head position (the
X and Y axis positions) is the center of the top camera; nozzles are offsets from it.
"""

import copy
import math
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from picnicplace.machine.marlin.settings import MarlinSettings


class ProfileModel(BaseModel):
    # Profiles are strict when loaded, and their models also appear on the wire in the
    # machine description, where fields with defaults are always sent.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class Point(ProfileModel):
    x: float
    y: float


class Rect(ProfileModel):
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("rectangle minimums must be less than maximums")
        return self


class Axis(ProfileModel):
    name: str = Field(min_length=1, max_length=8)
    kind: Literal["linear", "rotary"] = "linear"
    min: float = Field(description="Soft limit. Ignored for continuous axes.")
    max: float = Field(description="Soft limit. Ignored for continuous axes.")
    home: float = Field(
        default=0.0,
        description=(
            "Position after homing. May lie outside the soft limits: the firmware homes "
            "against a switch that moves can then not return to."
        ),
    )
    max_speed: float = Field(gt=0, description="Units per second (mm/s or deg/s).")
    continuous: bool = Field(
        default=False,
        description="Rotary axes only: turns without limit, so min and max are not enforced.",
    )

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.min >= self.max:
            raise ValueError(f"axis {self.name}: min must be less than max")
        if self.continuous and self.kind != "rotary":
            raise ValueError(f"axis {self.name}: only rotary axes can be continuous")
        return self


class Nozzle(ProfileModel):
    id: str
    label: str
    offset: Point = Field(description="Position relative to the head (top camera center).")
    z_axis: str
    z_sign: Literal[1, -1] = Field(
        default=1,
        description=(
            "Direction of this nozzle on its Z axis. Two nozzles sharing one Z motor move in "
            "opposite directions: one has 1, the other -1."
        ),
    )
    rotation_axis: str
    vacuum_input: str | None = None
    pump_output: str | None = None
    valve_output: str | None = None


class Camera(ProfileModel):
    id: str
    label: str
    mount: Literal["head", "fixed"]
    position: Point = Field(
        description="Head cameras: offset from the head. Fixed cameras: machine coordinates."
    )
    looking: Literal["down", "up"]
    field_of_view: Point = Field(description="Width (x) and height (y) of the view in mm.")
    light_output: str | None = None
    settle_s: float = Field(
        default=0.25,
        ge=0,
        description="Wait this long after a move before using a frame (vibration, latency).",
    )
    focus_height: float | None = Field(
        default=None,
        description=(
            "Up-looking cameras: the nozzle height (see safe_z) at which the bottom of a "
            "held part is in focus. A part is shown with the nozzle this high plus the "
            "part's height."
        ),
    )


class Location(ProfileModel):
    id: str
    label: str
    kind: Literal["homing_fiducial", "fiducial", "tip_holder", "discard", "park"]
    position: Point
    z: float | None = Field(
        default=None, description="Height of the surface, where it matters (fiducials)."
    )
    diameter: float | None = Field(
        default=None, gt=0, description="Fiducials: the round mark's diameter in mm."
    )
    style: Literal["dot", "lumenpnp-datum"] = Field(
        default="dot",
        description=(
            "How the simulator draws a fiducial: a plain dot, or the LumenPnP datum board "
            "(a 1 mm copper dot in a reticle, with rings, a 5 mm grid, and corner fiducials)."
        ),
    )


class FeederBank(ProfileModel):
    """A row of evenly spaced feeder slots, such as one LumenPnP feeder rail."""

    id: str
    label: str
    first_pick: Point = Field(description="Pick location of the first slot.")
    pitch: Point = Field(description="Step from one slot's pick location to the next.")
    count: int = Field(ge=1)
    width: float = Field(gt=0, description="Feeder width across the tape, in mm.")
    length: float = Field(gt=0, description="Feeder length along the tape, in mm.")
    rotation: float = Field(
        default=0, description="Tape direction in degrees; 90 means parts feed toward +Y."
    )
    first_address: int | None = Field(
        default=None,
        ge=1,
        le=254,
        description=(
            "Photon feeders: the bus address of the first slot. The others count up from it."
        ),
    )

    pick_height: float | None = Field(
        default=None,
        description="Nozzle height at the top of a part in the tape, at the pick location.",
    )
    pick_to_front: float = Field(
        default=8.0,
        ge=0,
        description="How far a feeder reaches past its pick location, along the feed.",
    )
    fiducial_offset: Point | None = Field(
        default=None,
        description=(
            "Where each feeder's alignment fiducial is: x along the feed from the pick "
            "location, y across the tape from the sprocket holes' center line (positive "
            "toward the holes' side, so the pockets are at minus the tape's pocket line). "
            "Measured from the holes, the offset is the same for every tape width."
        ),
    )
    fiducial_diameter: float | None = Field(default=None, gt=0)
    tape_widths: list[float] = Field(
        default_factory=list[float],
        description="The tape widths, in mm, that feeders in this bank take. Empty if any.",
    )

    def footprint(self) -> "Rect":
        """The area the bank's feeders cover, as a rectangle around all of them."""
        a = math.radians(self.rotation)
        ux, uy = math.cos(a), math.sin(a)
        nx, ny = -uy, ux
        xs: list[float] = []
        ys: list[float] = []
        for i in range(self.count):
            p = self.pick(i)
            for s in (self.pick_to_front - self.length, self.pick_to_front):
                for d in (-self.width / 2, self.width / 2):
                    xs.append(p.x + ux * s + nx * d)
                    ys.append(p.y + uy * s + ny * d)
        return Rect(x_min=min(xs), y_min=min(ys), x_max=max(xs), y_max=max(ys))

    def pick(self, index: int) -> Point:
        """Nominal pick location of a slot, counting from 0."""
        return Point(
            x=self.first_pick.x + self.pitch.x * index, y=self.first_pick.y + self.pitch.y * index
        )


class ValueRange(ProfileModel):
    """A display range. ``max`` may be below ``min`` for readings that grow more negative,
    such as raw vacuum sensor counts."""

    min: float
    max: float

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.min == self.max:
            raise ValueError("range min and max must differ")
        return self

    def fraction(self, value: float) -> float:
        """Where a value falls from min (0) to max (1), unclamped."""
        return (value - self.min) / (self.max - self.min)


class StagingPlate(ProfileModel):
    """A plate that boards and trays mount on, with a grid of tapped holes."""

    id: str
    label: str
    center: Point
    size: Point
    hole_pitch: float = Field(gt=0, description="Spacing of the hole grid, in mm.")
    hole_diameter: float = Field(gt=0)
    hole_columns: int = Field(ge=1, description="Columns of the grid, centered on the plate.")
    hole_rows: int = Field(ge=1)
    checkerboard: bool = Field(
        default=True, description="Holes only where column + row (from the center) is even."
    )
    corner_holes: list[Point] = Field(
        default_factory=list[Point],
        description=(
            "Large mounting holes, relative to the center; grid holes near them are left out."
        ),
    )
    corner_hole_diameter: float = 5.3
    knockout_diameter: float | None = Field(
        default=None,
        description=("A round opening for a bottom camera; grid holes inside are left out."),
    )
    knockout_offset: Point = Field(
        default_factory=lambda: Point(x=0, y=0),
        description="Where the knockout is, relative to the plate's center.",
    )

    @computed_field
    @property
    def holes(self) -> list[Point]:
        """Grid hole centers, in machine coordinates."""
        out: list[Point] = []
        half_c, half_r = (self.hole_columns - 1) // 2, (self.hole_rows - 1) // 2
        for i in range(-half_c, half_c + 1):
            for j in range(-half_r, half_r + 1):
                if self.checkerboard and (i + j) % 2:
                    continue
                dx, dy = i * self.hole_pitch, j * self.hole_pitch
                ko = self.knockout_offset
                if (
                    self.knockout_diameter
                    and math.hypot(dx - ko.x, dy - ko.y) < self.knockout_diameter / 2 + 3
                ):
                    continue
                if any(math.hypot(dx - c.x, dy - c.y) < 10 for c in self.corner_holes):
                    continue
                out.append(Point(x=self.center.x + dx, y=self.center.y + dy))
        return out


class KeepOut(ProfileModel):
    """An area where boards cannot go, such as the bottom camera or a fiducial mount."""

    id: str
    label: str
    area: Rect


class Input(ProfileModel):
    id: str
    label: str
    kind: Literal["digital", "analog"]
    role: Literal["home", "safe_z", "vacuum", "other"]
    axis: str | None = Field(default=None, description="Home sensors: the axis they home.")
    unit: str | None = None
    range: ValueRange | None = Field(default=None, description="Analog inputs: the display range.")
    threshold: float | None = Field(
        default=None,
        description=(
            "Vacuum inputs: readings at or past this value, going toward range.max, mean a "
            "part is held."
        ),
    )

    @model_validator(mode="after")
    def _threshold_needs_range(self) -> Self:
        if self.threshold is not None and self.range is None:
            raise ValueError(f"input {self.id}: a threshold needs a range")
        return self

    def past_threshold(self, value: float) -> bool:
        """For vacuum inputs: whether a reading means a part is held."""
        if self.threshold is None or self.range is None:
            return False
        return self.range.fraction(value) >= self.range.fraction(self.threshold)


class Output(ProfileModel):
    id: str
    label: str
    role: Literal["pump", "valve", "light", "other"]


class MachineProfile(ProfileModel):
    id: str
    name: str
    extends: str | None = Field(
        default=None, description="The profile this one builds on, if any (already merged in)."
    )
    provisional: bool = Field(
        default=False,
        description="Dimensions are estimates that have not been measured on a real machine.",
    )
    bed: Rect = Field(description="Outline of the machine bed, for drawing.")
    work_area: Rect | None = Field(default=None, description="Where boards go.")
    axes: list[Axis] = Field(min_length=1)
    safe_z: float = Field(
        description="A nozzle at or above this height (on its own Z scale) is safe for travel."
    )
    x_points: Literal["right", "left"] = Field(
        default="right",
        description="Which way +X points for an operator facing the machine.",
    )
    y_points: Literal["back", "front"] = Field(
        default="back",
        description="Which way +Y points for an operator facing the machine.",
    )
    home_z: float = Field(
        default=0.0,
        description=(
            "Where Z reads right after homing, with the nozzles level. The core checks it "
            "and does not call the machine homed if Z reads anything else: a firmware that "
            "names level differently needs a G92 in the home commands."
        ),
    )
    visual_homing: bool = Field(
        default=False,
        description=(
            "After homing, center the top camera on the homing fiducial and correct the "
            "X/Y coordinates to its known position."
        ),
    )
    safe_z_input: str | None = Field(
        default=None,
        description="A digital input that reads true when every nozzle is at safe height.",
    )
    board_height: float | None = Field(
        default=None,
        description=(
            "Nozzle height at the top surface of a board on the staging plates. A job can "
            "give its own."
        ),
    )
    feeder_bus: Literal["photon"] | None = Field(
        default=None,
        description="The feeders' bus: 'photon' for Opulo's Photon feeders over RS-485.",
    )
    nozzles: list[Nozzle] = Field(default_factory=list[Nozzle])
    cameras: list[Camera] = Field(default_factory=list[Camera])
    locations: list[Location] = Field(default_factory=list[Location])
    feeder_banks: list[FeederBank] = Field(default_factory=list[FeederBank])
    staging_plates: list[StagingPlate] = Field(default_factory=list[StagingPlate])
    keep_outs: list[KeepOut] = Field(
        default_factory=list[KeepOut],
        description="Areas inside the placement area where boards cannot be mounted.",
    )
    inputs: list[Input] = Field(default_factory=list[Input])
    outputs: list[Output] = Field(default_factory=list[Output])
    marlin: MarlinSettings | None = Field(
        default=None,
        exclude=True,
        description="How the Marlin driver talks to this machine. Not sent to clients.",
    )

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        axes = {a.name for a in self.axes}
        inputs = {i.id: i for i in self.inputs}
        outputs = {o.id for o in self.outputs}
        _unique("axis", [a.name for a in self.axes])
        _unique("nozzle", [n.id for n in self.nozzles])
        _unique("camera", [c.id for c in self.cameras])
        _unique("location", [loc.id for loc in self.locations])
        _unique("feeder bank", [b.id for b in self.feeder_banks])
        _unique("input or output", [*inputs, *outputs])

        for n in self.nozzles:
            for axis in (n.z_axis, n.rotation_axis):
                if axis not in axes:
                    raise ValueError(f"nozzle {n.id}: unknown axis {axis!r}")
            if n.vacuum_input is not None and n.vacuum_input not in inputs:
                raise ValueError(f"nozzle {n.id}: unknown input {n.vacuum_input!r}")
            for out in (n.pump_output, n.valve_output):
                if out is not None and out not in outputs:
                    raise ValueError(f"nozzle {n.id}: unknown output {out!r}")
        for c in self.cameras:
            if c.light_output is not None and c.light_output not in outputs:
                raise ValueError(f"camera {c.id}: unknown output {c.light_output!r}")
        for i in self.inputs:
            if i.axis is not None and i.axis not in axes:
                raise ValueError(f"input {i.id}: unknown axis {i.axis!r}")
        if self.marlin is not None:
            for out in self.marlin.outputs:
                if out not in outputs:
                    raise ValueError(f"marlin: unknown output {out!r}")
            for inp in self.marlin.inputs:
                if inp not in inputs:
                    raise ValueError(f"marlin: unknown input {inp!r}")
        if (
            self.marlin is not None
            and self.safe_z_input is not None
            and self.safe_z_input not in self.marlin.inputs
        ):
            raise ValueError(
                f"marlin: the safe-Z input {self.safe_z_input!r} needs read commands in "
                "[marlin.inputs] so the driver can check it before every X/Y move"
            )
        addresses = [
            b.first_address + i
            for b in self.feeder_banks
            if b.first_address is not None
            for i in range(b.count)
        ]
        _unique("feeder slot address", [str(a) for a in addresses])
        if any(a > 254 for a in addresses):
            raise ValueError("feeder slot addresses must be 254 or less")
        if self.safe_z_input is not None:
            safe = inputs.get(self.safe_z_input)
            if safe is None or safe.kind != "digital":
                raise ValueError("safe_z_input must name a digital input")
        return self

    @property
    def x_sign(self) -> int:
        """+1 if +X points right (as the operator sees it), -1 if left."""
        return 1 if self.x_points == "right" else -1

    @property
    def y_sign(self) -> int:
        """+1 if +Y points toward the back, -1 if toward the front."""
        return 1 if self.y_points == "back" else -1

    def image_to_machine(self, right: float, down: float) -> tuple[float, float]:
        """A displacement seen in a top camera image (mm right and mm down, as shown to
        the operator, front at the bottom) as a machine X/Y displacement."""
        return self.x_sign * right, -self.y_sign * down

    @computed_field
    @property
    def nozzle_reach(self) -> dict[str, Rect]:
        """Where each nozzle can reach: the head's X/Y travel shifted by its offset."""
        x = next((a for a in self.axes if a.name == "X"), None)
        y = next((a for a in self.axes if a.name == "Y"), None)
        if x is None or y is None:
            return {}
        return {
            n.id: Rect(
                x_min=x.min + n.offset.x,
                y_min=y.min + n.offset.y,
                x_max=x.max + n.offset.x,
                y_max=y.max + n.offset.y,
            )
            for n in self.nozzles
        }

    @property
    def bed_area(self) -> Rect | None:
        """Where boards can be mounted at all: around the staging plates, or the work
        area without plates."""
        if self.staging_plates:
            return Rect(
                x_min=min(p.center.x - p.size.x / 2 for p in self.staging_plates),
                y_min=min(p.center.y - p.size.y / 2 for p in self.staging_plates),
                x_max=max(p.center.x + p.size.x / 2 for p in self.staging_plates),
                y_max=max(p.center.y + p.size.y / 2 for p in self.staging_plates),
            )
        return self.work_area

    @computed_field
    @property
    def placement_area(self) -> Rect | None:
        """Where boards can go so that every nozzle can place anywhere on them: the
        nozzles' reach, all of them, within the staging plates (or the work area). Boards
        must also stay clear of the keep-outs inside it."""
        areas = list(self.nozzle_reach.values())
        if self.bed_area is not None:
            areas.append(self.bed_area)
        if not areas:
            return None
        x_min = max(a.x_min for a in areas)
        y_min = max(a.y_min for a in areas)
        x_max = min(a.x_max for a in areas)
        y_max = min(a.y_max for a in areas)
        if x_min >= x_max or y_min >= y_max:
            return None
        return Rect(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)

    def board_keep_outs(self) -> list[KeepOut]:
        """Everywhere a board cannot go: the profile's keep-outs and the feeders, whose
        noses can reach over the staging plates."""
        return [
            *self.keep_outs,
            *(
                KeepOut(id=f"feeders-{b.id}", label=f"{b.label} feeders", area=b.footprint())
                for b in self.feeder_banks
            ),
        ]

    def axis(self, name: str) -> Axis:
        return next(a for a in self.axes if a.name == name)


def _unique(what: str, ids: list[str]) -> None:
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            raise ValueError(f"duplicate {what} id {i!r}")
        seen.add(i)


def nozzle_height(nozzle: Nozzle, z: float) -> float:
    """Height of a nozzle given its Z axis position. Zero is the top of its travel.

    With a shared Z motor, the nozzle moving up past its top stays parked at the top while
    the other nozzle moves down, so a nozzle's height never goes above zero.
    """
    return min(0.0, nozzle.z_sign * z)


def builtin_profiles() -> list[str]:
    """IDs of the profiles that ship with PicnicPlace."""
    folder = resources.files("picnicplace") / "profiles"
    return sorted(
        p.name.removesuffix(".toml") for p in folder.iterdir() if p.name.endswith(".toml")
    )


def is_profile_path(ref: str) -> bool:
    """Whether a profile reference is a file path rather than a built-in ID."""
    return ref.endswith(".toml") or "/" in ref


def merge_profile_data(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``over`` laid on ``base``: tables merge key by key, lists of tables with an ``id``
    merge item by item (the same ID merges, a new one is added at the end), and anything
    else in ``over`` replaces what ``base`` has. Neither argument is changed."""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = merge_profile_data(cast(dict[str, Any], old), cast(dict[str, Any], value))
        elif _by_id(cast(object, old)) and _by_id(cast(object, value)):
            items = cast(list[dict[str, Any]], old)
            index = {item["id"]: n for n, item in enumerate(items)}
            for item in cast(list[dict[str, Any]], value):
                n = index.get(item["id"])
                if n is None:
                    items.append(copy.deepcopy(item))
                else:
                    items[n] = merge_profile_data(items[n], item)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _by_id(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict) and "id" in item for item in cast(list[object], value)
    )


def _read_profile(ref: str, relative_to: Path | None) -> tuple[dict[str, Any], Path | None]:
    if is_profile_path(ref):
        path = Path(ref)
        if relative_to is not None and not path.is_absolute():
            path = relative_to / path
        try:
            return tomllib.loads(path.read_text(encoding="utf-8")), path.resolve()
        except OSError as e:
            raise ValueError(f"cannot read machine profile {ref!r}: {e.strerror}") from e
    resource = resources.files("picnicplace") / "profiles" / f"{ref}.toml"
    if not resource.is_file():
        known = ", ".join(builtin_profiles())
        raise ValueError(f"unknown machine profile {ref!r}; built-in profiles: {known}")
    return tomllib.loads(resource.read_text(encoding="utf-8")), None


def profile_data(
    ref: str, relative_to: Path | None = None, _chain: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A profile's TOML data, with any profile it ``extends`` merged in under it."""
    data, path = _read_profile(ref, relative_to)
    name = str(path) if path else ref
    if name in _chain:
        raise ValueError(f"machine profile {ref!r} extends itself: {' > '.join([*_chain, ref])}")
    parent = data.get("extends")
    if parent is None:
        return data
    if not isinstance(parent, str):
        raise ValueError(f"machine profile {ref!r}: extends must be a profile ID or path")
    if "id" not in data:
        raise ValueError(f"machine profile {ref!r} extends {parent!r}, so it needs its own id")
    base = profile_data(parent, path.parent if path else None, (*_chain, name))
    return merge_profile_data(base, data)


def load_profile(ref: str) -> MachineProfile:
    """Load a profile by built-in ID (such as ``lumenpnp-v4.1``) or by path to a TOML file."""
    return MachineProfile.model_validate(profile_data(ref))
