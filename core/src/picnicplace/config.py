"""Core configuration, loaded from a TOML file and validated with Pydantic."""

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from picnicplace.machine.profile import Location, MachineProfile, is_profile_path, load_profile


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    host: str = Field(
        default="127.0.0.1",
        description="Address to bind. Anything other than loopback requires a token.",
    )
    port: int = Field(default=7420, ge=1, le=65535)
    token: str | None = Field(
        default=None,
        min_length=16,
        description="Shared secret for API access. Required when not bound to loopback.",
    )
    studio_dir: Path | None = Field(
        default=None, description="Built Studio GUI to serve at '/', if any."
    )

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


class PresenceConfig(_Section):
    grace_period_s: float = Field(
        default=5.0,
        ge=0,
        description="How long the last client may be gone before running jobs pause.",
    )
    heartbeat_timeout_s: float = Field(
        default=6.0,
        gt=0,
        description="A client that sends nothing for this long is disconnected.",
    )


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "picnicplace"


class DataConfig(_Section):
    dir: Path = Field(
        default_factory=_default_data_dir,
        description=(
            "Where the core keeps its data, such as the parts library. "
            "Default: $XDG_DATA_HOME/picnicplace, or ~/.local/share/picnicplace."
        ),
    )

    @property
    def library_path(self) -> Path:
        return self.dir.expanduser() / "library.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.dir.expanduser() / "jobs"

    @property
    def feeders_path(self) -> Path:
        return self.dir.expanduser() / "feeders.json"

    @property
    def captures_dir(self) -> Path:
        return self.dir.expanduser() / "captures"

    @property
    def bringup_path(self) -> Path:
        return self.dir.expanduser() / "bring-up.json"


class MarlinConfig(_Section):
    port: str | None = Field(
        default=None, description="Serial device, such as /dev/ttyACM0. Required for 'marlin'."
    )
    baud: int = 115200
    variant: str | None = Field(
        default=None,
        description="Firmware variant from the machine profile, such as 'vacuum-24bit'.",
    )
    connect_wait_s: float = Field(
        default=3.0, ge=0, description="Wait after opening the port, while the board boots."
    )
    command_timeout_s: float = Field(default=10.0, gt=0)


class MachineConfig(_Section):
    driver: Literal["simulator", "marlin", "fake-marlin"] = Field(
        default="simulator",
        description=(
            "'simulator': no hardware. 'marlin': a real machine on marlin.port. "
            "'fake-marlin': the Marlin driver talking to a built-in fake firmware."
        ),
    )
    marlin: MarlinConfig = Field(default_factory=MarlinConfig)
    locations: list[Location] = Field(
        default_factory=list[Location],
        description=(
            "Places this machine's owner chooses, such as the discard and park positions. "
            "Added to the machine profile's locations, which describe only the design."
        ),
    )
    profile: str = Field(
        default="lumenpnp-v4.1",
        description="Built-in machine profile ID, or a path to a profile TOML file.",
    )
    sim_time_scale: float = Field(
        default=1.0, gt=0, description="Simulator only: values above 1 run faster than real time."
    )
    sim_home_error: tuple[float, float] = Field(
        default=(0.3, -0.2),
        description=(
            "Simulator only: how far (X, Y in mm) the head really is from where homing "
            "says, as with real endstops. Visual homing finds and corrects it."
        ),
    )
    sim_feeders: list[int] = Field(
        default_factory=lambda: [1, 2, 3, 4, 5, 26],
        description=(
            "Simulator and fake firmware: the numbers of the feeder slots that hold a "
            "simulated Photon feeder."
        ),
    )
    sim_board_error: tuple[float, float, float] = Field(
        default=(0.6, -0.4, 0.5),
        description=(
            "Simulator only: how far a job's board really is from where the job puts it, "
            "as X and Y in mm and a turn in degrees about its front-left corner. Fiducial "
            "alignment finds and corrects it."
        ),
    )
    speed_scale: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description=(
            "Cap every move at this fraction of each axis's maximum speed. Use 0.25 or less "
            "when bringing up a new machine. Homing speed is set by the firmware."
        ),
    )

    _profile: MachineProfile | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _load_profile(self) -> Self:
        profile = load_profile(self.profile)
        if self.locations:
            # Re-validate so the merged locations get the profile's checks (unique IDs).
            data = profile.model_dump(exclude_computed_fields=True)
            data["marlin"] = profile.marlin
            data["locations"] = [*profile.locations, *self.locations]
            profile = MachineProfile.model_validate(data)
        self._profile = profile
        if self.driver in ("marlin", "fake-marlin"):
            if self._profile.marlin is None:
                raise ValueError(f"machine profile {self.profile!r} has no [marlin] settings")
            self._profile.marlin.resolved(self.marlin.variant)  # check the variant exists
            if self.driver == "marlin" and not self.marlin.port:
                raise ValueError("machine.marlin.port is required for the marlin driver")
        return self

    @property
    def machine_profile(self) -> MachineProfile:
        assert self._profile is not None
        return self._profile


class CameraConfig(_Section):
    source: Literal["synthetic", "device", "none"] = Field(
        description=(
            "'device': a real camera. 'synthetic': rendered by the simulator. 'none': off."
        )
    )
    device: str | None = Field(
        default=None,
        description=(
            "For 'device': a path such as /dev/v4l/by-id/usb-...-video-index0, or an index."
        ),
    )
    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)
    fps: float = Field(default=30.0, gt=0)
    flip_x: bool = Field(default=False, description="Mirror the image left to right.")
    flip_y: bool = Field(default=False, description="Mirror the image top to bottom.")
    rotation: float = Field(
        default=0.0,
        ge=-45,
        le=45,
        description="Rotate the image this many degrees counterclockwise (after flips).",
    )

    @model_validator(mode="after")
    def _device_needs_path(self) -> Self:
        if self.source == "device" and not self.device:
            raise ValueError("a camera with source 'device' needs a device")
        return self


class Config(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    machine: MachineConfig = Field(default_factory=MachineConfig)
    cameras: dict[str, CameraConfig] = Field(
        default_factory=dict[str, CameraConfig],
        description=(
            "Per camera ID from the machine profile. Cameras left out are synthetic with "
            "the simulator and fake-marlin drivers, and off with a real machine."
        ),
    )

    @model_validator(mode="after")
    def _known_cameras(self) -> Self:
        known = {c.id for c in self.machine.machine_profile.cameras}
        for camera_id in self.cameras:
            if camera_id not in known:
                raise ValueError(
                    f"cameras.{camera_id}: the machine profile has no such camera "
                    f"(it has: {', '.join(sorted(known)) or 'none'})"
                )
        return self

    def camera(self, camera_id: str) -> CameraConfig:
        """Settings for a camera, with the default for the driver if not configured."""
        configured = self.cameras.get(camera_id)
        if configured is not None:
            return configured
        simulated = self.machine.driver in ("simulator", "fake-marlin")
        return CameraConfig(source="synthetic" if simulated else "none")

    @model_validator(mode="after")
    def _require_token_off_loopback(self) -> Self:
        if not self.server.is_loopback and self.server.token is None:
            raise ValueError(
                f"server.host is {self.server.host!r}, which is reachable from the network; "
                "set server.token to a shared secret of at least 16 characters"
            )
        return self


def load_config(path: Path | None) -> Config:
    """Load configuration from a TOML file, or return defaults when ``path`` is None."""
    if path is None:
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    # A profile given as a relative path is relative to the config file.
    machine: dict[str, Any] = data.get("machine", {})
    profile = machine.get("profile")
    if isinstance(profile, str) and is_profile_path(profile) and not Path(profile).is_absolute():
        machine["profile"] = str(path.parent / profile)
    return Config.model_validate(data)
