"""Application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter
from starlette.responses import Response
from starlette.types import Scope

from picnicplace import __version__
from picnicplace.api import boards as board_routes
from picnicplace.api import cameras as camera_routes
from picnicplace.api import feeders as feeder_routes
from picnicplace.api import jobs as job_routes
from picnicplace.api import library as library_routes
from picnicplace.api import placement as placement_routes
from picnicplace.api import routes, websocket
from picnicplace.api import run as run_routes
from picnicplace.api.context import AppContext
from picnicplace.api.fingerprint import openapi_text
from picnicplace.auth import Authenticator
from picnicplace.bringup import BringUpStore
from picnicplace.camera.manager import CameraManager
from picnicplace.camera.source import Frame
from picnicplace.capture import CaptureStore
from picnicplace.config import Config
from picnicplace.events import EventBus
from picnicplace.feeders.service import FeederService
from picnicplace.feeders.setup import FeederSetupStore
from picnicplace.job.run import JobRunner
from picnicplace.job.store import JobStore
from picnicplace.lease import LeaseManager
from picnicplace.library import Library
from picnicplace.machine.errors import MachineError
from picnicplace.machine.factory import build_driver
from picnicplace.machine.service import MachineService
from picnicplace.operations import OperationManager
from picnicplace.placement import PlacementService
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
    machine = MachineService(
        profile, driver, bus, presence, operations, speed_scale=config.machine.speed_scale
    )
    lease = LeaseManager(bus, presence)
    cameras = CameraManager(config, profile, machine.scene)

    async def grab(camera_id: str, after: float, wait_s: float) -> Frame | None:
        hub = cameras.hub(camera_id)
        if hub is None:
            return None
        async with hub.viewer() as source:
            return await source.fresh_frame(after, wait_s)

    machine.attach_cameras(grab)
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
        bring_up=BringUpStore(config.data.bringup_path),
        captures=CaptureStore(config.data.captures_dir),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        ctx.library = Library(config.data.library_path)
        ctx.feeders = FeederService(
            machine,
            operations,
            FeederSetupStore(config.data.feeders_path),
            lambda: ctx.library,
            bundle.photon_sim,
        )
        ctx.placement = PlacementService(
            machine, ctx.feeders, lambda: ctx.library, config.machine.pick_correction
        )
        ctx.runner = JobRunner(
            machine,
            ctx.feeders,
            ctx.placement,
            lambda: ctx.library,
            JobStore(config.data.jobs_dir),
            config.machine.sim_board_error,
        )
        await machine.start()
        try:
            yield
        finally:
            ctx.library.close()
            ctx.library = None
            ctx.feeders = None
            ctx.placement = None
            ctx.runner = None
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
    app.include_router(library_routes.router)
    app.include_router(board_routes.router)
    app.include_router(job_routes.router)
    app.include_router(feeder_routes.router)
    app.include_router(placement_routes.router)
    app.include_router(run_routes.router)
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
        app.mount("/", StudioFiles(directory=config.server.studio_dir, html=True), name="studio")

    return app


class StudioFiles(StaticFiles):
    """The built Studio. Its pages must be re-checked on every load, or browsers keep an
    old page after an update (asset files have content hashes in their names, so they
    can be cached)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


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
    return openapi_text((app or create_app()).openapi())
