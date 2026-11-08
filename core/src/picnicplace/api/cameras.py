"""Camera endpoints: list, JPEG snapshot, and MJPEG stream (spec section 6.5).

Browsers cannot add headers to ``<img>`` requests, so these endpoints also accept the
token as a ``token`` query parameter, like the event socket. Streams do not count as
presence: watching video alone does not enable motion.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from picnicplace.api.context import Context
from picnicplace.auth import Permission, Principal
from picnicplace.camera.hub import CameraHub
from picnicplace.camera.source import Frame
from picnicplace.schemas import CameraInfo, ErrorResponse

BOUNDARY = "picnicplace-frame"
FIRST_FRAME_TIMEOUT_S = 5.0
KEEPALIVE_S = 2.0
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
