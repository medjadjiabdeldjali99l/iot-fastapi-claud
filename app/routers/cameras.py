import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.db.deps import AuthDep
from app.models.camera import CameraCreate, CameraOut, CameraStatusOut, CameraUpdate
from app.models.camera_config import CameraConfigCreate, CameraConfigOut
from app.models.common import CameraStatus
from app.services.camera_ping import ping_stream_url

router = APIRouter(prefix="/cameras", tags=["cameras"])

_NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail="Not implemented",
)


# --- CRUD caméras (stubs) ---

@router.get("", response_model=list[CameraOut])
async def list_cameras() -> list[CameraOut]:
    raise _NOT_IMPLEMENTED


@router.post("", response_model=CameraOut, status_code=status.HTTP_201_CREATED)
async def create_camera(payload: CameraCreate) -> CameraOut:
    raise _NOT_IMPLEMENTED


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(camera_id: UUID) -> CameraOut:
    raise _NOT_IMPLEMENTED


@router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(camera_id: UUID, payload: CameraUpdate) -> CameraOut:
    raise _NOT_IMPLEMENTED


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(camera_id: UUID) -> None:
    raise _NOT_IMPLEMENTED


# --- Module 1 : ping (online / offline) ---

@router.get("/{camera_id}/status", response_model=CameraStatusOut)
async def get_camera_status(camera_id: UUID, auth: AuthDep) -> CameraStatusOut:
    fetch = (
        auth.db.table("cameras")
        .select("id, stream_url, last_seen_at")
        .eq("id", str(camera_id))
        .limit(1)
    )
    res = await asyncio.to_thread(fetch.execute)
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Camera not found")

    camera = res.data[0]
    is_online = await ping_stream_url(camera["stream_url"])
    now = datetime.now(timezone.utc)
    new_status = CameraStatus.ONLINE if is_online else CameraStatus.OFFLINE

    update_payload: dict = {"status": new_status.value}
    if is_online:
        update_payload["last_seen_at"] = now.isoformat()

    # Best-effort: if RLS denies (viewer role), we still return the live result.
    update_q = (
        auth.db.table("cameras").update(update_payload).eq("id", str(camera_id))
    )
    await asyncio.to_thread(update_q.execute)

    return CameraStatusOut(
        id=camera_id,
        status=new_status,
        last_seen_at=now if is_online else camera.get("last_seen_at"),
    )


# --- Module 1 : streaming (stubs) ---

@router.post("/{camera_id}/stream/open")
async def open_camera_stream(camera_id: UUID) -> dict:
    raise _NOT_IMPLEMENTED


@router.post("/{camera_id}/stream/close")
async def close_camera_stream(camera_id: UUID) -> dict:
    raise _NOT_IMPLEMENTED


# --- Module 1 : configuration (versionnée) ---

@router.get("/{camera_id}/configs/active", response_model=CameraConfigOut)
async def get_active_config(camera_id: UUID, auth: AuthDep) -> CameraConfigOut:
    """404 if no active config — client should fall back to defaults
    (trigger_line=0.80, confidence=0.50, yolov8n)."""
    q = (
        auth.db.table("camera_configs")
        .select("*")
        .eq("camera_id", str(camera_id))
        .eq("is_active", True)
        .limit(1)
    )
    res = await asyncio.to_thread(q.execute)
    if not res.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active config for this camera",
        )
    return CameraConfigOut.model_validate(res.data[0])


@router.post(
    "/{camera_id}/configs",
    response_model=CameraConfigOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_config(
    camera_id: UUID,
    payload: CameraConfigCreate,
    auth: AuthDep,
) -> CameraConfigOut:
    """The DB trigger `deactivate_other_configs` will deactivate the previous active row."""
    cam_q = (
        auth.db.table("cameras")
        .select("id")
        .eq("id", str(camera_id))
        .limit(1)
    )
    cam_res = await asyncio.to_thread(cam_q.execute)
    if not cam_res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Camera not found")

    insert_q = auth.db.table("camera_configs").insert(
        {
            "camera_id": str(camera_id),
            "trigger_line_position": float(payload.trigger_line_position),
            "yolo_confidence": float(payload.yolo_confidence),
            "yolo_model": payload.yolo_model.value,
            "is_active": True,
            "created_by": str(auth.user_id),
        }
    )
    ins_res = await asyncio.to_thread(insert_q.execute)
    if not ins_res.data:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to create config",
        )
    return CameraConfigOut.model_validate(ins_res.data[0])
