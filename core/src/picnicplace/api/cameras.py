"""Camera endpoints: list, JPEG snapshot, MJPEG stream (spec section 6.5), and a real
camera's exposure, gain and white balance.

Browsers cannot add headers to ``<img>`` requests, so these endpoints also accept the
token as a ``token`` query parameter, like the event socket. Streams do not count as
presence: watching video alone does not enable motion.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from picnicplace.api.context import Context, require, require_control
from picnicplace.auth import Permission, Principal
from picnicplace.camera.device import ControlSettings, DeviceCamera
from picnicplace.camera.hub import CameraHub
from picnicplace.camera.source import Frame
from picnicplace.camera.v4l2 import CameraControls, Control
from picnicplace.capture import CaptureInfo
from picnicplace.machine.errors import MachineError, VisionError
from picnicplace.schemas import CameraInfo, ErrorResponse, WireModel

BOUNDARY = "picnicplace-frame"
FIRST_FRAME_TIMEOUT_S = 5.0
KEEPALIVE_S = 2.0
MONITOR_WAIT_S = 5.0
"""How long a capture waits for a monitor camera's first frame (it may have to start)."""
"""With no new frame for this long, the last one is sent again so the stream stays open."""

_bearer = HTTPBearer(auto_error=False)


def media_principal(
    ctx: Context,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    token: Annotated[
        str | None, Query(description="API token, for clients that cannot send headers.")
    ] = None,
) -> Principal:
    presented = credentials.credentials if credentials else token
    principal = ctx.auth.authenticate(presented)
    if principal is None or Permission.VIEW not in principal.permissions:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing or invalid token")
    return principal


router = APIRouter(
    prefix="/api/v1/cameras", tags=["cameras"], dependencies=[Depends(media_principal)]
)

NOT_FOUND = {404: {"model": ErrorResponse, "description": "No such camera."}}
UNAVAILABLE = {503: {"model": ErrorResponse, "description": "The camera is off or failing."}}

Width = Annotated[int | None, Query(ge=16, le=3840, description="Scale frames down to this width.")]
Quality = Annotated[int, Query(ge=10, le=95, description="JPEG quality.")]


def _hub(ctx: Context, camera_id: str) -> CameraHub:
    if not ctx.cameras.exists(camera_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no camera {camera_id!r}")
    hub = ctx.cameras.hub(camera_id)
    if hub is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"camera {camera_id!r} is off"
        )
    return hub


@router.get("")
async def list_cameras(ctx: Context) -> list[CameraInfo]:
    return ctx.cameras.info()


class CaptureRequest(WireModel):
    cameras: list[str] = Field(
        default_factory=list[str], description="Cameras to capture. Empty: every camera on."
    )
    label: str = Field(default="", max_length=200, description="What the frame shows.")


class ControlRange(WireModel):
    value: float
    minimum: float
    maximum: float
    default: float


class CameraControlsInfo(WireModel):
    """A real camera's exposure, gain and white balance now, and what each can be set to;
    null where the camera has no such setting."""

    exposure_auto: bool | None = Field(description="The camera sets its own exposure.")
    exposure_ms: ControlRange | None = Field(description="Exposure time, milliseconds.")
    gain: ControlRange | None
    white_balance_auto: bool | None
    white_balance: ControlRange | None = Field(description="White balance, kelvin.")


class CameraControlsRequest(WireModel):
    """What to set; what is left out stays as it is. Kept until the core restarts: put
    the values that work in the config file to keep them."""

    exposure_ms: Annotated[float, Field(gt=0)] | Literal["auto"] | None = Field(
        default=None, description="Exposure time in milliseconds, or 'auto'."
    )
    gain: int | None = Field(default=None, ge=0)
    white_balance: int | Literal["auto"] | None = Field(
        default=None, description="White balance in kelvin, or 'auto'."
    )


def _range(control: Control | None, scale: float = 1.0) -> ControlRange | None:
    if control is None:
        return None
    return ControlRange(
        value=control.value * scale,
        minimum=control.minimum * scale,
        maximum=control.maximum * scale,
        default=control.default * scale,
    )


def _controls_info(c: CameraControls) -> CameraControlsInfo:
    return CameraControlsInfo(
        exposure_auto=c.exposure_auto,
        exposure_ms=_range(c.exposure, 0.1),
        gain=_range(c.gain),
        white_balance_auto=c.white_balance_auto,
        white_balance=_range(c.white_balance),
    )


CONTROLS_ERRORS: dict[int | str, dict[str, Any]] = {
    **NOT_FOUND,
    **UNAVAILABLE,
    409: {"model": ErrorResponse, "description": "Not a camera with settings to set."},
    422: {"model": ErrorResponse, "description": "The camera refused the setting."},
}


def _device_camera(ctx: Context, camera_id: str) -> DeviceCamera:
    source = _hub(ctx, camera_id).source
    if not isinstance(source, DeviceCamera):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"camera {camera_id!r} is not a real camera: it has no exposure to set",
        )
    return source


def _refused(camera_id: str, error: OSError) -> HTTPException:
    reason = error.strerror or (str(error.args[0]) if error.args else str(error))
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"camera {camera_id!r}: {reason} (on Linux, its settings are set through V4L2)",
    )


@router.get("/{camera_id}/controls", responses=CONTROLS_ERRORS)
async def get_controls(ctx: Context, camera_id: str) -> CameraControlsInfo:
    """A real camera's exposure, gain and white balance (on Linux)."""
    camera = _device_camera(ctx, camera_id)
    try:
        return _controls_info(await asyncio.to_thread(camera.read_controls))
    except OSError as e:
        raise _refused(camera_id, e) from e


@router.put(
    "/{camera_id}/controls",
    responses=CONTROLS_ERRORS,
    dependencies=[Depends(require_control)],
)
async def set_controls(
    ctx: Context, camera_id: str, body: CameraControlsRequest
) -> CameraControlsInfo:
    """Set a real camera's exposure, gain or white balance now (on Linux), until the core
    restarts; the config file's ``exposure_ms``, ``gain`` and ``white_balance`` keep them."""
    camera = _device_camera(ctx, camera_id)
    settings = ControlSettings(body.exposure_ms, body.gain, body.white_balance)
    try:
        return _controls_info(await asyncio.to_thread(camera.set_controls, settings))
    except OSError as e:
        raise _refused(camera_id, e) from e


@router.get("/captures")
async def list_captures(ctx: Context) -> list[CaptureInfo]:
    """Recent captures, newest first."""
    return ctx.captures.list()


@router.post(
    "/captures",
    responses={**NOT_FOUND, **UNAVAILABLE},
    dependencies=[Depends(require(Permission.MOTION))],
)
async def capture(ctx: Context, body: CaptureRequest) -> list[CaptureInfo]:
    """Save a full-resolution frame from each camera, losslessly, with the machine's
    position and outputs, to the data directory's captures folder."""
    ids = body.cameras or [
        *(c.id for c in ctx.machine.profile.cameras if ctx.cameras.hub(c.id)),
        *ctx.cameras.monitor_ids,
    ]
    for camera_id in ids:
        _hub(ctx, camera_id)
    state = ctx.machine.state
    saved: list[CaptureInfo] = []
    monitors = set(ctx.cameras.monitor_ids)
    for camera_id in ids:
        try:
            if camera_id in monitors:
                # Not the machine's: no light to switch on, and nothing to settle.
                async with _hub(ctx, camera_id).viewer() as source:
                    found = await source.fresh_frame(time.time(), MONITOR_WAIT_S)
                if found is None:
                    raise VisionError(f"the {camera_id} camera sent no frame")
                frame = found
            else:
                frame = await ctx.machine.frame(camera_id, settle=False)
        except MachineError as e:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e
        saved.append(
            await asyncio.to_thread(
                ctx.captures.save,
                frame,
                camera_id,
                body.label.strip(),
                dict(state.position),
                dict(state.outputs),
                state.homed,
            )
        )
    return saved


@router.get(
    "/captures/{capture_id:path}.png",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}, 404: {"model": ErrorResponse}},
)
async def capture_image(ctx: Context, capture_id: str) -> FileResponse:
    try:
        png, _ = ctx.captures.paths(capture_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    if not png.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such capture")
    return FileResponse(png, media_type="image/png")


@router.get(
    "/{camera_id}/snapshot.jpg",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "One frame."},
        **NOT_FOUND,
        **UNAVAILABLE,
    },
)
async def snapshot(
    ctx: Context, camera_id: str, width: Width = None, quality: Quality = 85
) -> Response:
    """The latest frame as a JPEG. The ``X-Timestamp`` header says when it was captured."""
    hub = _hub(ctx, camera_id)
    async with hub.viewer() as source:
        frame = source.latest or await source.next_frame(0, FIRST_FRAME_TIMEOUT_S)
    if frame is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=hub.source.error or f"camera {camera_id!r} sent no frame",
        )
    data = await hub.jpeg(frame, width, quality)
    return Response(data, media_type="image/jpeg", headers=_frame_headers(frame))


@router.get(
    "/{camera_id}/stream.mjpg",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {f"multipart/x-mixed-replace; boundary={BOUNDARY}": {}},
            "description": (
                "An endless MJPEG stream. Each part carries X-Timestamp (capture time, Unix "
                "seconds) and X-Frame (frame number) headers."
            ),
        },
        **NOT_FOUND,
        **UNAVAILABLE,
    },
)
async def stream(
    ctx: Context,
    camera_id: str,
    width: Width = None,
    quality: Quality = 75,
    fps: Annotated[float, Query(gt=0, le=60, description="Maximum frame rate.")] = 15,
    frames: Annotated[int | None, Query(ge=1, description="Stop after this many frames.")] = None,
) -> StreamingResponse:
    """MJPEG for ``<img>`` tags, scripts, and small displays (``?width=320&fps=5``)."""
    hub = _hub(ctx, camera_id)
    return StreamingResponse(
        _parts(hub, width, quality, 1.0 / fps, frames),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


async def _parts(
    hub: CameraHub, width: int | None, quality: int, interval: float, limit: int | None
) -> AsyncIterator[bytes]:
    sent = 0
    async with hub.viewer() as source:
        seq = 0
        while not hub.closed:
            frame = await source.next_frame(seq, KEEPALIVE_S)
            if frame is None:
                frame = source.latest  # nothing new: repeat the last frame
                if frame is None:
                    continue
            seq = frame.seq
            data = await hub.jpeg(frame, width, quality)
            headers = "".join(f"{k}: {v}\r\n" for k, v in _frame_headers(frame).items())
            yield (
                (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n"
                    f"{headers}\r\n"
                ).encode()
                + data
                + b"\r\n"
            )
            sent += 1
            if limit is not None and sent >= limit:
                return
            await asyncio.sleep(interval)


def _frame_headers(frame: Frame) -> dict[str, str]:
    return {"X-Timestamp": f"{frame.timestamp:.3f}", "X-Frame": str(frame.seq)}
