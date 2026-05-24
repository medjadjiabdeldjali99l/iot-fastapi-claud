import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.db.deps import AuthDep
from app.models.common import SessionStatus
from app.models.iteration import IterationOut
from app.models.session import SessionCreate, SessionOut, SessionSummary
from app.services.cadence import SessionRunner, registry

router = APIRouter(prefix="/sessions", tags=["cadence"])


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(payload: SessionCreate, auth: AuthDep) -> SessionOut:
    """Create a cadence session and start the runner in the background.

    Mode 'single'   : one iteration (waiting_first → waiting_second → completed).
    Mode 'interval' : iterations every `interval_minutes` until the operator
                      calls /stop; each iteration's OPM is flagged as anomaly
                      when it deviates from the running average by more than
                      `anomaly_threshold_pct` (default 15%)."""
    cam_q = (
        auth.db.table("cameras")
        .select("id, stream_url, organization_id")
        .eq("id", str(payload.camera_id))
        .limit(1)
    )
    cam_res = await asyncio.to_thread(cam_q.execute)
    if not cam_res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Camera not found")
    camera = cam_res.data[0]

    cfg_q = (
        auth.db.table("camera_configs")
        .select("id, trigger_line_position, yolo_confidence, yolo_model")
        .eq("camera_id", str(payload.camera_id))
        .eq("is_active", True)
        .limit(1)
    )
    cfg_res = await asyncio.to_thread(cfg_q.execute)
    if not cfg_res.data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active config for this camera. Save a config first.",
        )
    config = cfg_res.data[0]

    yolo_model = (
        payload.yolo_model_override.value
        if payload.yolo_model_override is not None
        else config["yolo_model"]
    )

    session_payload = {
        "camera_id": str(payload.camera_id),
        "config_id": config["id"],
        "organization_id": camera["organization_id"],
        "mode": payload.mode.value,
        "interval_minutes": payload.interval_minutes,
        "anomaly_threshold_pct": float(payload.anomaly_threshold_pct),
        "yolo_model_override": (
            payload.yolo_model_override.value
            if payload.yolo_model_override is not None
            else None
        ),
        "status": SessionStatus.PENDING.value,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "started_by": str(auth.user_id),
        "notes": payload.notes,
    }
    sess_q = auth.db.table("sessions").insert(session_payload)
    sess_res = await asyncio.to_thread(sess_q.execute)
    if not sess_res.data:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to create session"
        )
    session = sess_res.data[0]
    session_id = UUID(session["id"])

    runner = SessionRunner(
        session_id=session_id,
        camera_id=payload.camera_id,
        stream_url=camera["stream_url"],
        mode=payload.mode,
        trigger_line_position=float(config["trigger_line_position"]),
        yolo_confidence=float(config["yolo_confidence"]),
        yolo_model=yolo_model,
        db=auth.db,
        interval_minutes=payload.interval_minutes,
        anomaly_threshold_pct=float(payload.anomaly_threshold_pct),
    )
    await registry().register(session_id, runner)
    runner.start()

    return SessionOut.model_validate(session)


@router.post("/{session_id}/stop", response_model=SessionOut)
async def stop_session(session_id: UUID, auth: AuthDep) -> SessionOut:
    """Cancel a running session. The runner marks the session as
    'stopped' from inside its CancelledError handler."""
    runner = await registry().get(session_id)
    if runner is not None:
        runner.request_stop()
        await registry().remove(session_id)

    q = (
        auth.db.table("sessions")
        .select("*")
        .eq("id", str(session_id))
        .limit(1)
    )
    res = await asyncio.to_thread(q.execute)
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return SessionOut.model_validate(res.data[0])


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: UUID, auth: AuthDep) -> SessionOut:
    q = (
        auth.db.table("sessions")
        .select("*")
        .eq("id", str(session_id))
        .limit(1)
    )
    res = await asyncio.to_thread(q.execute)
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return SessionOut.model_validate(res.data[0])


@router.get("/{session_id}/iterations", response_model=list[IterationOut])
async def list_session_iterations(
    session_id: UUID,
    auth: AuthDep,
) -> list[IterationOut]:
    q = (
        auth.db.table("session_iterations")
        .select("*")
        .eq("session_id", str(session_id))
        .order("iteration_number")
    )
    res = await asyncio.to_thread(q.execute)
    return [IterationOut.model_validate(row) for row in (res.data or [])]


@router.get("/{session_id}/summary", response_model=SessionSummary)
async def get_session_summary(session_id: UUID, auth: AuthDep) -> SessionSummary:
    q = (
        auth.db.table("session_summaries")
        .select("*")
        .eq("session_id", str(session_id))
        .limit(1)
    )
    res = await asyncio.to_thread(q.execute)
    if not res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return SessionSummary.model_validate(res.data[0])


@router.get("", response_model=list[SessionSummary])
async def list_sessions(auth: AuthDep) -> list[SessionSummary]:
    q = (
        auth.db.table("session_summaries")
        .select("*")
        .order("started_at", desc=True)
        .limit(100)
    )
    res = await asyncio.to_thread(q.execute)
    return [SessionSummary.model_validate(row) for row in (res.data or [])]
