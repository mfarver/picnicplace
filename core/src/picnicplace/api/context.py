"""Shared application objects and FastAPI dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from picnicplace.auth import Authenticator, Permission, Principal
from picnicplace.bringup import BringUpStore
from picnicplace.camera.manager import CameraManager
from picnicplace.capture import CaptureStore
from picnicplace.config import Config
from picnicplace.events import EventBus
from picnicplace.feeders.service import FeederService
from picnicplace.lease import LeaseManager
from picnicplace.library import Library
from picnicplace.machine.service import MachineService
from picnicplace.operations import OperationManager
from picnicplace.placement import PlacementService
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import ClientInfo


@dataclass
class AppContext:
    config: Config
    auth: Authenticator
    bus: EventBus
    presence: PresenceTracker
    operations: OperationManager
    machine: MachineService
    lease: LeaseManager
    cameras: CameraManager
    bring_up: BringUpStore
    captures: CaptureStore
    library: Library | None = None
    """Opened when the app starts, so building the app (as for the OpenAPI document)
    touches no files."""
    feeders: FeederService | None = None
    """Also made when the app starts: it loads the feeder setup."""
    placement: PlacementService | None = None


def get_context(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Context = Annotated[AppContext, Depends(get_context)]

_bearer = HTTPBearer(auto_error=False)


def get_principal(
    ctx: Context,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    principal = ctx.auth.authenticate(credentials.credentials if credentials else None)
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory: the request's principal must hold ``permission``."""

    def check(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=f"requires the {permission} permission"
            )
        return principal

    return check


CLIENT_HEADER = "X-PicnicPlace-Client"


def get_client_id(
    client_id: Annotated[
        str | None,
        Header(
            alias=CLIENT_HEADER,
            description="Client ID from the event socket's hello message.",
        ),
    ] = None,
) -> str | None:
    return client_id


def require_control(
    ctx: Context,
    _principal: Annotated[Principal, Depends(require(Permission.MOTION))],
    client_id: Annotated[str | None, Depends(get_client_id)],
) -> ClientInfo:
    """Dependency for motion and actuator endpoints: the calling client must hold the
    control lease. A free lease is taken implicitly."""
    return ctx.lease.require(client_id)


Controller = Annotated[ClientInfo, Depends(require_control)]
