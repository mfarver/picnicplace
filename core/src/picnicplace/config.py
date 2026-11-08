"""Core configuration, loaded from a TOML file and validated with Pydantic."""

import ipaddress
import tomllib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from picnicplace.machine.profile import MachineProfile, is_profile_path, load_profile


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
    profile: str = Field(
        default="lumenpnp-v4",
        description="Built-in machine profile ID, or a path to a profile TOML file.",
    )
    sim_time_scale: float = Field(
        default=1.0, gt=0, description="Simulator only: values above 1 run faster than real time."
    )

    _profile: MachineProfile | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _load_profile(self) -> Self:
        self._profile = load_profile(self.profile)
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

    @model_validator(mode="after")
    def _device_needs_path(self) -> Self:
        if self.source == "device" and not self.device:
            raise ValueError("a camera with source 'device' needs a device")
        return self


class Config(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
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
