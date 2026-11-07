"""REST endpoints under /api/v1."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from picnicplace import __version__
from picnicplace.api.context import Context, require
from picnicplace.auth import Permission
from picnicplace.schemas import (
    PROTOCOL_VERSION,
    ErrorResponse,
    HealthResponse,
    MachineState,
    MoveRequest,
    Operation,
    PresenceState,
)

MACHINE_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "The machine cannot do this right now."},
}

public = APIRouter(prefix="/api/v1")
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require(Permission.VIEW))])


@public.get("/health", tags=["system"])
def health() -> HealthResponse:
    """Liveness check. Does not require authentication."""
    return HealthResponse(version=__version__, protocol=PROTOCOL_VERSION)


@router.get("/presence", tags=["system"])
async def get_presence(ctx: Context) -> PresenceState:
    return ctx.presence.state


@router.get("/machine", tags=["machine"])
async def get_machine(ctx: Context) -> MachineState:
    return ctx.machine.state


@router.post(
    "/machine/home",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses=MACHINE_ERRORS,
    dependencies=[Depends(require(Permission.MOTION))],
)
async def home(ctx: Context) -> Operation:
    """Start homing. Returns an operation; follow it over the event socket."""
    return ctx.machine.start_home()


@router.post(
    "/machine/move",
    tags=["machine"],
    status_code=status.HTTP_202_ACCEPTED,
    responses={**MACHINE_ERRORS, 422: {"model": ErrorResponse}},
    dependencies=[Depends(require(Permission.MOTION))],
)
async def move(ctx: Context, body: MoveRequest) -> Operation:
    """Start an absolute move. Returns an operation; follow it over the event socket."""
    return ctx.machine.start_move(body.position, body.speed)


@router.post("/machine/estop", tags=["machine"])
async def estop(ctx: Context) -> MachineState:
    """Emergency stop. Allowed for any authenticated client, in any state."""
    await ctx.machine.emergency_stop()
    return ctx.machine.state


@router.post(
    "/machine/reset",
    tags=["machine"],
    dependencies=[Depends(require(Permission.MOTION))],
)
async def reset(ctx: Context) -> MachineState:
    """Clear a latched emergency stop. The machine must be homed again afterward."""
    await ctx.machine.reset()
    return ctx.machine.state


@router.get(
    "/operations/{operation_id}",
    tags=["operations"],
    responses={404: {"model": ErrorResponse}},
)
async def get_operation(ctx: Context, operation_id: str) -> Operation:
    op = ctx.operations.get(operation_id)
    if op is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown operation")
    return op
