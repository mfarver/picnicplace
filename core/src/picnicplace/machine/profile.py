"""Machine profiles: the declarative description of a machine (spec section 7.1).

A profile says what a machine is made of and where things are: axes and limits, the head
with its nozzles and cameras, fixed cameras, fiducials, feeder slots, and the inputs and
outputs. Drivers, the machine service, and clients (through the machine description
endpoint) all work from the same profile.

Profiles are TOML files. Built-in ones ship in ``picnicplace/profiles``; a config can also
point at a profile file by path.

Coordinates are machine coordinates in millimeters: X increases to the right and Y
increases toward the back of the machine, seen from above. The head position (the X and Y
axis positions) is the center of the top camera; nozzles are offsets from it.
"""

import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    min: float
    max: float
    home: float = 0.0
    max_speed: float = Field(gt=0, description="Units per second (mm/s or deg/s).")

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.min >= self.max:
            raise ValueError(f"axis {self.name}: min must be less than max")
        if not self.min <= self.home <= self.max:
            raise ValueError(f"axis {self.name}: home must be within min..max")
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


class Location(ProfileModel):
    id: str
    label: str
    kind: Literal["homing_fiducial", "tip_holder", "discard"]
    position: Point


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


class ValueRange(ProfileModel):
    min: float
    max: float

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.min >= self.max:
            raise ValueError("range min must be less than max")
        return self


class Input(ProfileModel):
    id: str
    label: str
    kind: Literal["digital", "analog"]
    role: Literal["home", "safe_z", "vacuum", "other"]
    axis: str | None = Field(default=None, description="Home sensors: the axis they home.")
    unit: str | None = None
    range: ValueRange | None = Field(default=None, description="Analog inputs: the display range.")
    threshold: float | None = Field(
        default=None, description="Vacuum inputs: readings at or above this mean a part is held."
    )


class Output(ProfileModel):
    id: str
    label: str
    role: Literal["pump", "valve", "light", "other"]


class MachineProfile(ProfileModel):
    id: str
    name: str
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
    safe_z_input: str | None = Field(
        default=None,
        description="A digital input that reads true when every nozzle is at safe height.",
    )
    nozzles: list[Nozzle] = Field(default_factory=list[Nozzle])
    cameras: list[Camera] = Field(default_factory=list[Camera])
    locations: list[Location] = Field(default_factory=list[Location])
    feeder_banks: list[FeederBank] = Field(default_factory=list[FeederBank])
    inputs: list[Input] = Field(default_factory=list[Input])
    outputs: list[Output] = Field(default_factory=list[Output])

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
        if self.safe_z_input is not None:
            safe = inputs.get(self.safe_z_input)
            if safe is None or safe.kind != "digital":
                raise ValueError("safe_z_input must name a digital input")
        return self

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


def load_profile(ref: str) -> MachineProfile:
    """Load a profile by built-in ID (such as ``lumenpnp-v4``) or by path to a TOML file."""
    if is_profile_path(ref):
        try:
            data = tomllib.loads(Path(ref).read_text(encoding="utf-8"))
        except OSError as e:
            raise ValueError(f"cannot read machine profile {ref!r}: {e.strerror}") from e
    else:
        resource = resources.files("picnicplace") / "profiles" / f"{ref}.toml"
        if not resource.is_file():
            known = ", ".join(builtin_profiles())
            raise ValueError(f"unknown machine profile {ref!r}; built-in profiles: {known}")
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
    return MachineProfile.model_validate(data)
