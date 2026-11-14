"""The setup wizard's choices, under /api/v1/setup (spec §7.7, §8.2): which machine this
is, its serial port and cameras, kept in machine-setup.json and used when the core next
starts. The config file still wins over them."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.auth import Permission
from picnicplace.machine.profile import builtin_profiles, load_profile
from picnicplace.machine_setup import (
    CameraChoice,
    CameraDevice,
    MachineSetup,
    ProbeResult,
    SerialPort,
    camera_devices,
    changed_keys,
    probe_port,
    serial_ports,
)
from picnicplace.schemas import WireModel

RESTART_DELAY_S = 0.3
"""The core restarts this long after saying it will, so the reply reaches the Studio."""

router = APIRouter(
    prefix="/api/v1/setup",
    tags=["setup"],
    dependencies=[Depends(require(Permission.VIEW))],
)


class MachineChoice(WireModel):
    id: str
    name: str


class SetupState(WireModel):
    saved: MachineSetup = Field(description="The wizard's choices, as kept.")
    running: dict[str, str | None] = Field(
        description="What the core is running now: profile, driver, serial_port, and each "
        "camera's device (None: off or simulated)."
    )
    pinned: list[str] = Field(
        description="Choices the config file sets itself, which win over the wizard's: "
        "machine.profile, machine.driver, machine.marlin.port, cameras.<id>."
    )
    machines: list[MachineChoice] = Field(description="The built-in machine profiles.")
    restart_needed: bool = Field(
        description="The wizard's choices differ from what the core runs: restart it."
    )
    can_restart: bool = Field(
        description="The core can restart itself; otherwise the owner restarts it."
    )


class SetupChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str | None = None
    driver: Literal["simulator", "marlin"] | None = None
    serial_port: str | None = Field(default=None, max_length=500)
    cameras: dict[str, CameraChoice] = Field(default_factory=dict[str, CameraChoice])


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial_port: str = Field(min_length=1, max_length=500)


def _state(ctx: Context) -> SetupState:
    config = ctx.config
    saved = ctx.setup.get()
    running: dict[str, str | None] = {
        "profile": config.machine.profile,
        "driver": config.machine.driver,
        "serial_port": config.machine.marlin.port,
    }
    for camera in ctx.machine.profile.cameras:
        c = config.cameras.get(camera.id)
        running[f"cameras.{camera.id}"] = c.device if c and c.source == "device" else None
    machines = [MachineChoice(id=p, name=load_profile(p).name) for p in builtin_profiles()]
    return SetupState(
        saved=saved,
        running=running,
        pinned=sorted(ctx.pinned),
        machines=machines,
        restart_needed=bool(changed_keys(config, saved, ctx.pinned)),
        can_restart=ctx.restart is not None,
    )


@router.get("")
def get_setup(ctx: Context) -> SetupState:
    """The wizard's choices, what the core is running, and what the config file pins."""
    return _state(ctx)


@router.put("", dependencies=[Depends(require(Permission.CONFIG)), Depends(require_control)])
def put_setup(ctx: Context, body: SetupChoice) -> SetupState:
    """Keep the wizard's choices. They are used when the core next starts; the last
    probe is kept while the serial port stays the same."""
    if body.profile is not None and body.profile not in builtin_profiles():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"no built-in machine {body.profile!r}"
        )
    profile = load_profile(body.profile) if body.profile else ctx.machine.profile
    known = {c.id for c in profile.cameras}
    unknown = sorted(set(body.cameras) - known)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{profile.name} has no camera {', '.join(unknown)} "
            f"(it has: {', '.join(sorted(known)) or 'none'})",
        )
    saved = ctx.setup.get()
    probe = saved.probe if saved.probe and saved.probe.serial_port == body.serial_port else None
    ctx.setup.put(
        MachineSetup(
            profile=body.profile,
            driver=body.driver,
            serial_port=body.serial_port,
            cameras=body.cameras,
            probe=probe,
        )
    )
    return _state(ctx)


@router.get("/serial-ports")
def list_serial_ports() -> list[SerialPort]:
    """The serial devices a machine might be on, found by name: none is opened."""
    return serial_ports()


@router.get("/cameras")
def list_cameras() -> list[CameraDevice]:
    """The cameras the wizard offers: stable paths on Linux, the first indexes elsewhere."""
    return camera_devices()


@router.post("/probe", dependencies=[Depends(require(Permission.CONFIG)), Depends(require_control)])
async def probe(ctx: Context, body: ProbeRequest) -> ProbeResult:
    """Ask the firmware on a serial port what it is, and read its vacuum sensors. Nothing
    moves or switches on. The result is kept with the wizard's choices."""
    config = ctx.config
    if (
        config.machine.driver == "marlin"
        and config.machine.marlin.port == body.serial_port
        and ctx.machine.state.connected
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the core is connected to the machine on {body.serial_port}: its firmware is "
            f"{ctx.machine.state.firmware or 'unknown'}",
        )
    saved = ctx.setup.get()
    profile = load_profile(saved.profile) if saved.profile else ctx.machine.profile
    try:
        result = await probe_port(body.serial_port, profile, config.machine.marlin.connect_wait_s)
    except (OSError, ValueError, TimeoutError) as e:
        reason = getattr(e, "strerror", None) or str(e) or "no answer in time"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"could not probe {body.serial_port}: {reason}. Check the machine is on and "
            "plugged in, and that nothing else (such as OpenPnP) has the port open.",
        ) from e
    saved = ctx.setup.get()
    saved.probe = result
    ctx.setup.put(saved)
    return result


@router.post(
    "/restart",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require(Permission.CONFIG)), Depends(require_control)],
)
async def restart(ctx: Context) -> SetupState:
    """Restart the core, to use the wizard's choices. Studios reconnect when it is back."""
    if ctx.restart is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this core cannot restart itself: stop it and start it again",
        )
    if ctx.machine.state.busy:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the machine is busy: restart when it is done"
        )
    asyncio.get_running_loop().call_later(RESTART_DELAY_S, ctx.restart)
    return _state(ctx)
