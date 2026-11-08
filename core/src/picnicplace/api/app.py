"""Application factory."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter

from picnicplace import __version__
from picnicplace.api import cameras as camera_routes
from picnicplace.api import routes, websocket
from picnicplace.api.context import AppContext
from picnicplace.auth import Authenticator
from picnicplace.camera.manager import CameraManager
from picnicplace.config import Config
from picnicplace.events import EventBus
from picnicplace.lease import LeaseManager
from picnicplace.machine.errors import MachineError
from picnicplace.machine.factory import build_driver
from picnicplace.machine.service import MachineService
from picnicplace.operations import OperationManager
from picnicplace.presence import PresenceTracker
from picnicplace.schemas import ClientMessage, ErrorResponse, ServerEvent

TITLE = "PicnicPlace Core API"


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()
    bus = EventBus()
    presence = PresenceTracker(bus, config.presence.grace_period_s)
    operations = OperationManager(bus)
    profile = config.machine.machine_profile
    bundle = build_driver(config.machine)
    driver = bundle.driver
    machine = MachineService(profile, driver, bus, presence, operations)
    lease = LeaseManager(bus, presence)
    cameras = CameraManager(config, profile, machine.scene)
    lease.on_holder_left.append(machine.stop_manual_motion)
    ctx = AppContext(
        config=config,
        auth=Authenticator(config.server.token),
        bus=bus,
        presence=presence,
        operations=operations,
        machine=machine,
        lease=lease,
        cameras=cameras,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await machine.start()
        try:
            yield
        finally:
            await cameras.shutdown()
            await operations.shutdown()
            await presence.shutdown()
            await machine.shutdown()
            await bundle.shutdown()

    app = FastAPI(title=TITLE, version=__version__, lifespan=lifespan)
    app.state.ctx = ctx
    app.include_router(routes.public)
    app.include_router(routes.router)
    app.include_router(camera_routes.router)
    app.include_router(websocket.router)

    @app.exception_handler(MachineError)
    async def _machine_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, MachineError)
        body = ErrorResponse(detail=str(exc), code=exc.code)
        return JSONResponse(body.model_dump(), status_code=exc.http_status)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = openapi

    if config.server.studio_dir is not None:
        app.mount("/", StaticFiles(directory=config.server.studio_dir, html=True), name="studio")

    return app


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """OpenAPI document, extended with the WebSocket message schemas."""
    schema = get_openapi(
        title=TITLE,
        version=__version__,
        description=(
            "REST API for PicnicPlace Core. Live state streams over the WebSocket at "
            "`/api/v1/events`; its messages are described by the `ServerEvent` and "
            "`ClientMessage` schemas."
        ),
        routes=app.routes,
    )
    schemas: dict[str, Any] = schema.setdefault("components", {}).setdefault("schemas", {})
    ref = "#/components/schemas/{model}"
    for name, adapter in (
        ("ServerEvent", TypeAdapter(ServerEvent)),
        ("ClientMessage", TypeAdapter(ClientMessage)),
    ):
        adapter_any: TypeAdapter[Any] = adapter
        sub = adapter_any.json_schema(ref_template=ref, mode="serialization")
        schemas.update(sub.pop("$defs", {}))
        if "$ref" in sub and sub["$ref"] == ref.format(model=name):
            continue
        schemas[name] = sub
    return schema


def openapi_json(app: FastAPI | None = None) -> str:
    return json.dumps((app or create_app()).openapi(), indent=2, sort_keys=True) + "\n"
