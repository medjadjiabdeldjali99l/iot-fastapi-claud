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
    """Crée une session de cadence (mode intervalle) et démarre le runner
    en arrière-plan.

    Toutes les `interval_minutes` minutes, le runner ouvre une fenêtre de
    mesure de `measurement_window_seconds` (défaut 120), collecte les
    franchissements et calcule la cadence moyenne. Une itération dont l'OPM
    s'écarte de la moyenne du run de plus de `anomaly_threshold_pct`
    (défaut 15%) est marquée comme anomalie."""
    cam_q = (
        auth.db.table("cameras")
        .select("id, name, location, stream_url, organization_id")
        .eq("id", str(payload.camera_id))
        .limit(1)
    )
    cam_res = await asyncio.to_thread(cam_q.execute)
    if not cam_res.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Camera not found")
    camera = cam_res.data[0]

    # Méta pour le topic MQTT vers Odoo : on récupère le slug de l'orga
    # (= factory) en plus du nom et de la location de la caméra (= machine /
    # line). Best-effort : si la lecture échoue ou que les champs sont vides,
    # le publisher applique ses fallbacks.
    org_slug: str | None = None
    try:
        org_q = (
            auth.db.table("organizations")
            .select("slug")
            .eq("id", camera["organization_id"])
            .limit(1)
        )
        org_res = await asyncio.to_thread(org_q.execute)
        if org_res.data:
            org_slug = org_res.data[0].get("slug")
    except Exception:
        pass

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

    session_payload = {
        "camera_id": str(payload.camera_id),
        "config_id": config["id"],
        "organization_id": camera["organization_id"],
        "interval_minutes": payload.interval_minutes,
        "measurement_window_seconds": payload.measurement_window_seconds,
        "anomaly_threshold_pct": float(payload.anomaly_threshold_pct),
        "reference_cadence_min": (
            float(payload.reference_cadence_min)
            if payload.reference_cadence_min is not None
            else None
        ),
        "reference_cadence_max": (
            float(payload.reference_cadence_max)
            if payload.reference_cadence_max is not None
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
        trigger_line_position=float(config["trigger_line_position"]),
        yolo_confidence=float(config["yolo_confidence"]),
        yolo_model=config["yolo_model"],
        db=auth.db,
        interval_minutes=payload.interval_minutes,
        measurement_window_seconds=payload.measurement_window_seconds,
        anomaly_threshold_pct=float(payload.anomaly_threshold_pct),
        reference_cadence_min=(
            float(payload.reference_cadence_min)
            if payload.reference_cadence_min is not None
            else None
        ),
        reference_cadence_max=(
            float(payload.reference_cadence_max)
            if payload.reference_cadence_max is not None
            else None
        ),
        mqtt_factory=org_slug,
        mqtt_line=camera.get("location"),
        mqtt_machine=camera.get("name"),
    )
    await registry().register(session_id, runner)
    runner.start()

    return SessionOut.model_validate(session)


@router.post("/{session_id}/stop", response_model=SessionOut)
async def stop_session(session_id: UUID, auth: AuthDep) -> SessionOut:
    """Cancel a running session. Le runner marque la session comme 'stopped'
    depuis son handler CancelledError."""
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