from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/ws", tags=["streams"])


@router.websocket("/sessions/{session_id}")
async def session_stream(websocket: WebSocket, session_id: UUID) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
