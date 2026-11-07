"""Core configuration, loaded from a TOML file and validated with Pydantic."""

import ipaddress
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class AxisConfig(_Section):
    name: str = Field(min_length=1, max_length=8)
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


def _default_axes() -> list[AxisConfig]:
    # Simulator defaults, roughly the size of a desktop machine. Not a real machine profile.
    return [
        AxisConfig(name="X", min=0, max=450, home=0, max_speed=500),
        AxisConfig(name="Y", min=0, max=400, home=0, max_speed=500),
        AxisConfig(name="Z", min=-30, max=30, home=0, max_speed=100),
        AxisConfig(name="A", min=-360, max=360, home=0, max_speed=1000),
        AxisConfig(name="B", min=-360, max=360, home=0, max_speed=1000),
    ]


class MachineConfig(_Section):
    driver: Literal["simulator"] = "simulator"
    axes: list[AxisConfig] = Field(default_factory=_default_axes, min_length=1)
    sim_time_scale: float = Field(
        default=1.0, gt=0, description="Simulator only: values above 1 run faster than real time."
    )

    @model_validator(mode="after")
    def _unique_axes(self) -> Self:
        names = [a.name for a in self.axes]
        if len(names) != len(set(names)):
            raise ValueError("axis names must be unique")
        return self


class Config(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    machine: MachineConfig = Field(default_factory=MachineConfig)

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
        return Config.model_validate(tomllib.load(f))
