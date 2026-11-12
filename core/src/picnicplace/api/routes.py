"""REST endpoints under /api/v1."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from picnicplace import __version__
from picnicplace.api.context import Context, get_client_id, require, require_control
from picnicplace.api.fingerprint import api_fingerprint
from picnicplace.auth import Permission
from picnicplace.machine.bottom_camera import start_bottom_camera_calibration
from picnicplace.machine.errors import NotHomedError
from picnicplace.machine.local import LocalProfileState, LocationKind
from picnicplace.machine.profile import KeepOut, MachineProfile, Point
from picnicplace.schemas import (
    PROTOCOL_VERSION,
    ErrorResponse,
    HealthResponse,
    JogRequest,
    LeaseRequest,
    LeaseState,
    MachineState,
    MoveRequest,
    Operation,
    OutputRequest,
    PresenceState,
    WireModel,
)

MACHINE_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorResponse,
        "description": (
            "The machine cannot do this right now, or this client does not hold the control lease."
        ),
    },
}

public = APIRouter(prefix="/api/v1")
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require(Permission.VIEW))])


@public.get("/health", tags=["system"])
def health(request: Request) -> HealthResponse:
    """Liveness check. Does not require authentication."""
    return HealthResponse(
        version=__version__, protocol=PROTOCOL_VERSION, api=api_fingerprint(request.app)
    )


@router.get("/presence", tags=["system"])
async def get_presence(ctx: Context) -> PresenceState:
    return ctx.presence.state


@router.get("/machine", tags=["machine"])
async def get_machine(ctx: Context) -> MachineState:
    return ctx.machine.state


@router.get("/machine/description", tags=["machine"])
async def get_machine_description(ctx: Context) -> MachineProfile:
    """The machine profile: geometry, nozzles, cameras, feeders, and the list of I/O."""
    return ctx.machine.profile


class LocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: Point | None = Field(
        default=None, description="Where it is. Default: where the top camera is looking."
    )
    label: str | None = Field(default=None, min_length=1, max_length=100)


class LocalProfileUpdate(BaseModel):
    """Findings for the local profile, such as from the bring-up checklist. They take
    effect when the core next starts."""

    model_config = ConfigDict(extra="forbid")

    x_points: Literal["right", "left"] | None = None
    y_points: Literal["back", "front"] | None = None
    keep_outs: list[KeepOut] | None = Field(
        default=None, description="Added to the profile's, or replacing those with the same ID."
    )


@router.get("/machine/local-profile", tags=["machine"])
async def get_local_profile(ctx: Context) -> LocalProfileState:
    """This machine's own profile: what is true of it beyond its model's design."""
    return ctx.local_profile.state()


@router.put(
    "/machine/local-profile",
    tags=["machine"],
    responses={422: {"model": ErrorResponse, "description": "The result is not a valid profile."}},
    dependencies=[Depends(require_control)],
)
async def update_local_profile(ctx: Context, body: LocalProfileUpdate) -> LocalProfileState:
    """Save findings to this machine's profile. They take effect when the core restarts."""
    changes = body.model_dump(mode="json", exclude_none=True)
    try:
        ctx.local_profile.update(changes)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    return ctx.local_profile.state()


@router.put(
    "/machine/locations/{kind}",
    tags=["machine"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def set_location(ctx: Context, kind: LocationKind, body: LocationRequest) -> MachineProfile:
    """Set this machine's discard or park location, by default to where the top camera is
    looking. It takes effect at once, and is kept in the local profile."""
    position = body.position
    if position is None:
        if not ctx.machine.state.homed:
            raise NotHomedError("home the machine first, so the camera's position is known")
        position = ctx.machine.camera_position()
    ctx.local_profile.set_location(kind, position.x, position.y, body.label)
    return ctx.machine.profile


@router.delete(
    "/machine/locations/{kind}", tags=["machine"], dependencies=[Depends(require_control)]
)
async def reset_location(ctx: Context, kind: LocationKind) -> MachineProfile:
    """Forget this machine's own discard or park location: the profile's or the config's,
    if any, applies again."""
    ctx.local_profile.reset_location(kind)
    return ctx.machine.profile


@router.put(
    "/machine/outputs/{output_id}",
    tags=["machine"],
    responses={
        **MACHINE_ERRORS,
        404: {"model": ErrorResponse, "description": "No output with this ID."},
    },
    dependencies=[Depends(require_control)],
)
async def set_output(ctx: Context, output_id: str, body: OutputRequest) -> MachineState:
    """Switch an output on or off. Turning one on requires motion to be enabled."""
    await ctx.machine.set_output(output_id, body.on)
    return ctx.machine.state


@router.post(
    "/machine/connect",
    tags=["machine"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.MOTION))],
)
async def connect_machine(ctx: Context) -> MachineState:
    """Connect to the machine if it is not connected (for example, after it was off)."""
    await ctx.machine.connect()
    return ctx.machine.state


@router.post(
    "/machine/home",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def home(ctx: Context) -> Operation:
    """Start homing. Returns an operation; follow it over the event socket."""
    return ctx.machine.start_home()


@router.post(
    "/machine/move",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **MACHINE_ERRORS,
        422: {"model": ErrorResponse, "description": "The target is invalid or out of limits."},
    },
    dependencies=[Depends(require_control)],
)
async def move(ctx: Context, body: MoveRequest) -> Operation:
    """Start an absolute move. Returns an operation; follow it over the event socket."""
    return ctx.machine.start_move(body.position, body.speed)


@router.post(
    "/machine/visual-home",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def visual_home(ctx: Context) -> Operation:
    """Find the homing fiducial with the top camera and correct X and Y. Homing does
    this by itself when the profile enables visual homing; this runs it again."""
    return ctx.machine.start_visual_home()


@router.post(
    "/machine/camera-scale",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def measure_camera_scale(ctx: Context) -> Operation:
    """Measure the top camera's millimeters per pixel by moving over the homing fiducial.
    The result is kept (the calibration checklist) and used by vision from then on."""
    return ctx.machine.start_measure_camera_scale()


class BottomCameraRequest(WireModel):
    nozzle_id: str | None = Field(
        default=None, description="The nozzle to calibrate with. Empty: the first."
    )


@router.post(
    "/machine/bottom-camera",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require_control)],
)
async def calibrate_bottom_camera(ctx: Context, body: BottomCameraRequest) -> Operation:
    """Calibrate the bottom camera with an empty nozzle tip: center the tip in its image
    (its place), step the nozzle within 2 mm of the profile's focus height for the
    sharpest picture (its focus), and step the head to measure its scale and turn. The
    result is kept (the calibration checklist) and used from then on."""
    return start_bottom_camera_calibration(ctx.machine, body.nozzle_id)


@router.post(
    "/machine/jog",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **MACHINE_ERRORS,
        422: {"model": ErrorResponse, "description": "The axis is unknown."},
    },
    dependencies=[Depends(require_control)],
)
async def jog(ctx: Context, body: JogRequest) -> Operation:
    """Move one axis by a relative distance, clamped to its limits. X and Y jogs are
    refused while a nozzle is below safe Z."""
    return ctx.machine.start_jog(body.axis, body.distance, body.speed)


@router.post("/machine/estop", tags=["machine"])
async def estop(ctx: Context) -> MachineState:
    """Emergency stop. Allowed for any authenticated client, in any state."""
    await ctx.machine.emergency_stop()
    return ctx.machine.state


@router.post(
    "/machine/reset",
    tags=["machine"],
    dependencies=[Depends(require_control)],
)
async def reset(ctx: Context) -> MachineState:
    """Clear a latched emergency stop. The machine must be homed again afterward."""
    await ctx.machine.reset()
    return ctx.machine.state


@router.get("/lease", tags=["lease"])
async def get_lease(ctx: Context) -> LeaseState:
    return ctx.lease.state


@router.post(
    "/lease",
    tags=["lease"],
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.MOTION))],
)
async def acquire_lease(
    ctx: Context,
    body: LeaseRequest,
    client_id: Annotated[str | None, Depends(get_client_id)],
) -> LeaseState:
    """Take control of the machine. With ``force``, take it from another client."""
    return ctx.lease.acquire(client_id, force=body.force)


@router.delete("/lease", tags=["lease"], responses=MACHINE_ERRORS)
async def release_lease(
    ctx: Context, client_id: Annotated[str | None, Depends(get_client_id)]
) -> LeaseState:
    """Give up control of the machine, if this client holds it."""
    return ctx.lease.release(client_id)


@router.get(
    "/operations/{operation_id}",
    tags=["operations"],
    responses={404: {"model": ErrorResponse, "description": "No operation with this ID."}},
)
async def get_operation(ctx: Context, operation_id: str) -> Operation:
    op = ctx.operations.get(operation_id)
    if op is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown operation")
    return op
