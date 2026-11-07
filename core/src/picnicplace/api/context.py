"""Shared application objects and FastAPI dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from picnicplace.auth import Authenticator, Permission, Principal
from picnicplace.config import Config
from picnicplace.events import EventBus
from picnicplace.machine.service import MachineService
from picnicplace.operations import OperationManager
from picnicplace.presence import PresenceTracker


@dataclass
class AppContext:
    config: Config
    auth: Authenticator
    bus: EventBus
    presence: PresenceTracker
    operations: OperationManager
    machine: MachineService


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
