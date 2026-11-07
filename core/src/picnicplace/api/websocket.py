"""The event socket, /api/v1/events.

Each connection receives a ``hello`` message with full state, then a stream of events.
Clients send ``ping`` heartbeats; a client that stays silent past the heartbeat timeout
is dropped. An open, authenticated connection is what counts as presence (spec 5.3).
"""

import contextlib
import logging
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from picnicplace import __version__
from picnicplace.api.context import AppContext
from picnicplace.events import Subscription, SubscriptionOverflowError
from picnicplace.schemas import (
    PROTOCOL_VERSION,
    ClientMessage,
    ErrorEvent,
    HelloEvent,
    PongEvent,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

CLOSE_UNAUTHORIZED = 4401
CLOSE_HEARTBEAT_TIMEOUT = 4408
CLOSE_TOO_SLOW = 4429

_client_message: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


@router.websocket("/events")
async def events(ws: WebSocket, token: str | None = None) -> None:
    ctx: AppContext = ws.app.state.ctx
    await ws.accept()
    # Browsers cannot set headers on WebSocket requests, so the token is a query parameter.
    if ctx.auth.authenticate(token) is None:
        await ws.close(CLOSE_UNAUTHORIZED, "missing or invalid token")
        return

    client_id = uuid.uuid4().hex[:12]
    sub = ctx.bus.subscribe()
    ctx.presence.add(client_id)
    log.info("client %s connected", client_id)
    try:
        hello = HelloEvent(
            protocol=PROTOCOL_VERSION,
            server_version=__version__,
            client_id=client_id,
            machine=ctx.machine.state,
            presence=ctx.presence.state,
        )
        await ws.send_text(hello.model_dump_json())
        close_code = await _serve(ws, sub, ctx.config.presence.heartbeat_timeout_s)
        if close_code is not None:
            with contextlib.suppress(Exception):
                await ws.close(close_code)
    except WebSocketDisconnect:
        pass
    finally:
        ctx.bus.unsubscribe(sub)
        ctx.presence.remove(client_id)
        log.info("client %s disconnected", client_id)


async def _serve(ws: WebSocket, sub: Subscription, heartbeat_timeout_s: float) -> int | None:
    """Run the send and receive loops until either ends. Returns a close code, if any."""
    close_code: int | None = None

    async with anyio.create_task_group() as tg:

        async def run(loop: Awaitable[int]) -> None:
            nonlocal close_code
            try:
                close_code = await loop
            except Exception as e:
                # WebSocketDisconnect, or a send on a socket the client already closed.
                log.debug("event socket ended: %r", e)
            tg.cancel_scope.cancel()

        tg.start_soon(run, _send_loop(ws, sub))
        tg.start_soon(run, _receive_loop(ws, sub, heartbeat_timeout_s))
    return close_code


async def _send_loop(ws: WebSocket, sub: Subscription) -> int:
    try:
        while True:
            event = await sub.get()
            await ws.send_text(event.model_dump_json())
    except SubscriptionOverflowError:
        log.warning("client too slow; disconnecting")
        return CLOSE_TOO_SLOW


async def _receive_loop(ws: WebSocket, sub: Subscription, timeout_s: float) -> int:
    while True:
        raw: str | None = None
        with anyio.move_on_after(timeout_s):
            raw = await ws.receive_text()
        if raw is None:
            log.info("heartbeat timeout")
            return CLOSE_HEARTBEAT_TIMEOUT
        try:
            _client_message.validate_json(raw)
        except ValidationError:
            sub.push(ErrorEvent(message="unrecognized message"))
            continue
        # Only ping exists in protocol 1.
        sub.push(PongEvent(server_time=datetime.now(UTC)))
