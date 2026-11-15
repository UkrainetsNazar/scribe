import asyncio
import contextlib
from uuid import UUID

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from scribe.application.interfaces import TokenService
from scribe.application.progress import (
    TERMINAL_TYPES,
    Message,
    ProgressSubscriber,
    initial_message,
)
from scribe.application.use_cases.auth import GetUser
from scribe.application.use_cases.recordings import GetRecording
from scribe.container import Container
from scribe.domain.errors import AppError, UnauthorizedError

router = APIRouter(tags=["websocket"])

POLL_INTERVAL_SEC = 1.0


async def _send(websocket: WebSocket, message: Message) -> bool:
    """False if the client is already gone."""
    try:
        await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        return False
    return True


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    """Client messages are ignored; this only notices when the client goes away."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/ws/recordings/{recording_id}")
@inject
async def recording_events(
    websocket: WebSocket,
    recording_id: UUID,
    token: str | None = None,
    tokens: TokenService = Depends(Provide[Container.token_service]),
    get_user: GetUser = Depends(Provide[Container.get_user]),
    get_recording: GetRecording = Depends(Provide[Container.get_recording]),
    subscriber: ProgressSubscriber = Depends(Provide[Container.progress_bus]),
) -> None:
    try:
        if token is None:
            raise UnauthorizedError
        user = await get_user.execute(tokens.decode(token, "access").sub)
        view = await get_recording.execute(user.id, recording_id)
    except AppError:
        # Closing before accept() makes the server answer the handshake with HTTP 403.
        # A foreign or unknown recording looks the same as a bad token (no existence leak).
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    watcher: asyncio.Task[None] | None = None
    try:
        async with subscriber.subscribe(recording_id) as subscription:
            first = initial_message(await subscription.last_state(), view.recording, view.stages)
            alive = await _send(websocket, first)
            if alive and first.get("type") not in TERMINAL_TYPES:
                watcher = asyncio.create_task(_wait_for_disconnect(websocket))
                while alive and not watcher.done():
                    update = await subscription.next_message(POLL_INTERVAL_SEC)
                    if update is None:
                        continue
                    alive = await _send(websocket, update)
                    if update.get("type") in TERMINAL_TYPES:
                        break
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()
