import asyncio
import base64
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.db.deps import decode_supabase_jwt, supabase_client_for_token
from app.services.cadence import SessionRunner, registry
from app.services.stream import RTSPStream, StreamUnavailable

router = APIRouter(prefix="/ws", tags=["streams"])

# RFC 6455 close codes
WS_NORMAL = 1000
WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011


@router.websocket("/cameras/{camera_id}/preview")
async def camera_preview(
    websocket: WebSocket,
    camera_id: UUID,
    token: str | None = Query(default=None),
    fps: int = Query(default=10, ge=1, le=30),
    quality: int = Query(default=70, ge=10, le=95),
) -> None:
    """Live camera preview as base64-encoded JPEG over WebSocket.

    Auth via `?token=<supabase_jwt>` query param: browsers cannot set
    custom headers when opening a WebSocket connection.

    Message envelope (JSON):
        {"type": "frame", "ts": "<iso8601>", "data": "<base64-jpeg>"}
        {"type": "error", "message": "<reason>"}
    """
    await websocket.accept()

    if not token:
        await _safe_close(websocket, WS_POLICY_VIOLATION, "Missing token")
        return
    try:
        decode_supabase_jwt(token)
    except JWTError as exc:
        await _safe_close(websocket, WS_POLICY_VIOLATION, f"Invalid token: {exc}")
        return

    # If a cadence session is currently running on this camera, share its
    # annotated frames instead of opening a second VideoCapture. Most webcams
    # (and many RTSP servers) refuse a second concurrent reader; this also
    # gives the operator the live trigger-line + tracker overlays for free.
    runner = await registry().find_by_camera(camera_id)
    if runner is not None:
        await _run_with_runner(websocket, runner, fps)
        return

    try:
        db = supabase_client_for_token(token)
        fetch = (
            db.table("cameras")
            .select("id, stream_url")
            .eq("id", str(camera_id))
            .limit(1)
        )
        res = await asyncio.to_thread(fetch.execute)
    except Exception as exc:
        await _safe_close(websocket, WS_INTERNAL_ERROR, f"DB error: {exc}")
        return

    if not res.data:
        await _safe_close(websocket, WS_POLICY_VIOLATION, "Camera not found")
        return
    stream_url = res.data[0]["stream_url"]

    stream = RTSPStream(stream_url, target_fps=fps, jpeg_quality=quality)
    sender = asyncio.create_task(_send_loop(websocket, stream))
    receiver = asyncio.create_task(_receive_loop(websocket))
    try:
        # Whichever side finishes first (stream ended OR client disconnected)
        # tears down the other. asyncio.wait swallows exceptions in tasks;
        # gather(..., return_exceptions=True) ensures we never raise here.
        _, pending = await asyncio.wait(
            {sender, receiver},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await stream.close()
        await _safe_close(websocket, WS_NORMAL)


async def _run_with_runner(
    websocket: WebSocket, runner: SessionRunner, fps: int
) -> None:
    interval = 1.0 / max(1, min(fps, 30))
    sender = asyncio.create_task(_send_from_runner(websocket, runner, interval))
    receiver = asyncio.create_task(_receive_loop(websocket))
    try:
        _, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await _safe_close(websocket, WS_NORMAL)


async def _send_from_runner(
    websocket: WebSocket, runner: SessionRunner, interval: float
) -> None:
    """Forward the runner's annotated frames to the client until the runner
    finishes (status terminal) or the client disconnects."""
    last: bytes | None = None
    while True:
        if runner.task is None or runner.task.done():
            return
        frame = runner.latest_frame
        if frame is not None and frame is not last:
            last = frame
            payload = {
                "type": "frame",
                "ts": datetime.now(timezone.utc).isoformat(),
                "data": base64.b64encode(frame).decode("ascii"),
            }
            try:
                await websocket.send_text(json.dumps(payload))
            except (RuntimeError, WebSocketDisconnect):
                return
        await asyncio.sleep(interval)


async def _send_loop(websocket: WebSocket, stream: RTSPStream) -> None:
    try:
        async for jpeg_bytes in stream.frames():
            payload = {
                "type": "frame",
                "ts": datetime.now(timezone.utc).isoformat(),
                "data": base64.b64encode(jpeg_bytes).decode("ascii"),
            }
            await websocket.send_text(json.dumps(payload))
    except WebSocketDisconnect:
        return
    except StreamUnavailable as exc:
        await _safe_send_text(
            websocket,
            json.dumps({"type": "error", "message": str(exc)}),
        )
    except Exception as exc:
        await _safe_send_text(
            websocket,
            json.dumps({"type": "error", "message": f"stream error: {exc}"}),
        )


async def _receive_loop(websocket: WebSocket) -> None:
    """Drain client messages; the side effect we want is detecting disconnect."""
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return


async def _safe_send_text(websocket: WebSocket, text: str) -> None:
    try:
        await websocket.send_text(text)
    except (RuntimeError, WebSocketDisconnect):
        return


async def _safe_close(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        return
