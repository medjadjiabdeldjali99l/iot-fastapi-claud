"""Mode Single & Mode Intervalle cadence runners.

Lifecycle of `sessions.status` driven by this module:
    Single   : pending -> waiting_first -> waiting_second -> completed
    Interval : pending -> (waiting_first -> waiting_second -> paused)+ -> stopped
    Both     : on cancel  -> stopped
               on error   -> failed

The OPM math (delta_seconds, opm, completed_at) is computed by the SQL trigger
`compute_iteration_metrics` when we UPDATE t1 — Python only writes timestamps.
Anomaly detection (is_anomaly, anomaly_deviation) is computed in Python after
each iteration completes, comparing the iteration's OPM to the average of the
previously completed iterations in the same session.

Runners live in an in-process registry. A worker restart loses them; the
corresponding session row will remain in waiting_*/paused until some external
cleanup re-marks it. Known limitation of the v1 single-worker deployment."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from supabase import Client

from app.models.common import CrossingType, SessionMode, SessionStatus
from app.services.detection import Detection, YoloDetector
from app.services.stream import RTSPStream, StreamUnavailable
from app.services.tracking import CrossingEvent, LineCrosser

# `stream` is imported first so its `OPENCV_FFMPEG_CAPTURE_OPTIONS` env var is
# set before cv2 is referenced anywhere else in the process.
import cv2  # noqa: E402

logger = logging.getLogger(__name__)

# BGR colors used for the live overlay sent to the preview WebSocket.
_LINE_COLOR = (0, 255, 255)         # yellow — trigger line
_BOX_NEW = (80, 220, 100)           # green — tracked, not yet crossed
_BOX_COUNTED = (160, 160, 160)      # gray  — already counted in this iteration
_TEXT_FG = (0, 255, 255)            # yellow text
_TEXT_OUTLINE = (0, 0, 0)           # black outline for legibility


def compute_anomaly(
    current_opm: float,
    baseline_opms: list[float],
    threshold_pct: float,
) -> tuple[bool, float] | None:
    """Pure helper: is `current_opm` an anomaly vs the average of `baseline_opms`?

    Returns (is_anomaly, deviation_pct) or None when there is no baseline
    yet (first iteration, or all baselines are non-positive)."""
    if not baseline_opms:
        return None
    avg = sum(baseline_opms) / len(baseline_opms)
    if avg <= 0:
        return None
    deviation_pct = abs(current_opm - avg) / avg * 100.0
    return deviation_pct > threshold_pct, round(deviation_pct, 2)


class SessionRunner:
    """Drives a cadence measurement to completion (Single) or until the
    operator stops it (Interval)."""

    def __init__(
        self,
        session_id: UUID,
        camera_id: UUID,
        stream_url: str,
        mode: SessionMode,
        trigger_line_position: float,
        yolo_confidence: float,
        yolo_model: str,
        db: Client,
        interval_minutes: int | None = None,
        anomaly_threshold_pct: float = 15.0,
        target_fps: int = 10,
        preview_jpeg_quality: int = 70,
    ) -> None:
        if mode == SessionMode.INTERVAL and (
            interval_minutes is None or interval_minutes <= 0
        ):
            raise ValueError("interval_minutes is required for INTERVAL mode")
        self.session_id = session_id
        self.camera_id = camera_id
        self.stream_url = stream_url
        self.mode = mode
        self.trigger_line_position = trigger_line_position
        self.yolo_confidence = yolo_confidence
        self.yolo_model = yolo_model
        self.db = db
        self.interval_minutes = interval_minutes
        self.anomaly_threshold_pct = anomaly_threshold_pct
        self.target_fps = target_fps
        self.preview_jpeg_quality = max(10, min(preview_jpeg_quality, 95))

        self._task: asyncio.Task | None = None
        self._stop_requested = False
        # Annotated JPEG of the most recent frame, consumed by the preview
        # WebSocket when this runner is the active owner of the camera.
        self._latest_frame: bytes | None = None

    @property
    def latest_frame(self) -> bytes | None:
        return self._latest_frame

    def start(self) -> asyncio.Task:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return self._task

    def request_stop(self) -> None:
        self._stop_requested = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    # ------------------------------------------------------------------
    # Top-level dispatch
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            if self.mode == SessionMode.SINGLE:
                await self._run_single()
            else:
                await self._run_interval()
        except asyncio.CancelledError:
            await self._mark_stopped()
            raise
        except StreamUnavailable as exc:
            logger.warning("Session %s stream unavailable: %s", self.session_id, exc)
            await self._fail(f"stream unavailable: {exc}")
        except Exception as exc:
            logger.exception("Session %s failed", self.session_id)
            await self._fail(f"runner error: {exc}")
        finally:
            try:
                await registry().remove(self.session_id)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    async def _run_single(self) -> None:
        detector = YoloDetector(self.yolo_model)
        crosser = LineCrosser(self.trigger_line_position)
        stream = RTSPStream(self.stream_url, target_fps=self.target_fps)
        try:
            iteration_id = await self._create_iteration(1)
            await self._update_session({"status": SessionStatus.WAITING_FIRST.value})
            await self._do_one_iteration(stream, detector, crosser, iteration_id)
            await self._update_session(
                {
                    "status": SessionStatus.COMPLETED.value,
                    "ended_at": _now_iso(),
                }
            )
        finally:
            await stream.close()

    async def _run_interval(self) -> None:
        assert self.interval_minutes is not None  # ctor enforces
        iteration_number = 0
        while not self._stop_requested:
            iteration_number += 1

            # Fresh detector + crosser per iteration: ultralytics' ByteTrack
            # persists tracker state across calls, so a new instance is the
            # simplest way to guarantee fresh IDs after the pause.
            detector = YoloDetector(self.yolo_model)
            crosser = LineCrosser(self.trigger_line_position)
            stream = RTSPStream(self.stream_url, target_fps=self.target_fps)
            iteration_id = await self._create_iteration(iteration_number)
            try:
                await self._update_session(
                    {"status": SessionStatus.WAITING_FIRST.value}
                )
                await self._do_one_iteration(stream, detector, crosser, iteration_id)
            finally:
                await stream.close()

            await self._compute_anomaly_for(iteration_id)

            if self._stop_requested:
                break

            await self._update_session({"status": SessionStatus.PAUSED.value})
            await asyncio.sleep(self.interval_minutes * 60)

        await self._update_session(
            {
                "status": SessionStatus.STOPPED.value,
                "ended_at": _now_iso(),
            }
        )

    # ------------------------------------------------------------------
    # Shared per-iteration frame loop
    # ------------------------------------------------------------------

    async def _do_one_iteration(
        self,
        stream: RTSPStream,
        detector: YoloDetector,
        crosser: LineCrosser,
        iteration_id: UUID,
    ) -> None:
        t0_recorded = False
        async for frame in stream.raw_frames():
            if self._stop_requested:
                return

            detections = await detector.track(frame, self.yolo_confidence)
            events = crosser.update(detections, frame.shape[1])

            # Update the preview buffer with the annotated frame. This is what
            # the WS preview consumes when a session owns the camera.
            self._latest_frame = self._encode_annotated(
                frame, detections, crosser.crossed_ids
            )

            for event in events:
                if not t0_recorded:
                    await self._record_event(event, CrossingType.T0, iteration_id)
                    await self._update_iteration(
                        iteration_id, {"t0": event.timestamp.isoformat()}
                    )
                    await self._update_session(
                        {"status": SessionStatus.WAITING_SECOND.value}
                    )
                    t0_recorded = True
                else:
                    await self._record_event(event, CrossingType.T1, iteration_id)
                    # Setting t1 fires the compute_iteration_metrics trigger
                    # which fills delta_seconds + opm + completed_at.
                    await self._update_iteration(
                        iteration_id, {"t1": event.timestamp.isoformat()}
                    )
                    return

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    async def _compute_anomaly_for(self, iteration_id: UUID) -> None:
        # opm is null if the iteration was stopped before T1 — skip silently.
        cur_q = (
            self.db.table("session_iterations")
            .select("opm")
            .eq("id", str(iteration_id))
            .limit(1)
        )
        cur_res = await asyncio.to_thread(cur_q.execute)
        if not cur_res.data:
            return
        opm = cur_res.data[0].get("opm")
        if opm is None:
            return
        current_opm = float(opm)

        others_q = (
            self.db.table("session_iterations")
            .select("opm")
            .eq("session_id", str(self.session_id))
            .neq("id", str(iteration_id))
        )
        others_res = await asyncio.to_thread(others_q.execute)
        baseline = [
            float(r["opm"])
            for r in (others_res.data or [])
            if r.get("opm") is not None
        ]

        result = compute_anomaly(
            current_opm, baseline, self.anomaly_threshold_pct
        )
        if result is None:
            return

        is_anomaly, deviation_pct = result
        await self._update_iteration(
            iteration_id,
            {
                "is_anomaly": is_anomaly,
                "anomaly_deviation": deviation_pct,
            },
        )

    # ------------------------------------------------------------------
    # Preview annotation
    # ------------------------------------------------------------------

    def _encode_annotated(
        self,
        frame,
        detections: list[Detection],
        crossed_ids: set[int],
    ) -> bytes | None:
        """Draw the trigger line + tracked boxes on a copy of the frame and
        return a JPEG-encoded byte string ready for base64 / WebSocket."""
        try:
            out = frame.copy()
            h, w = out.shape[:2]

            # Trigger line + position label
            line_x = int(w * self.trigger_line_position)
            cv2.line(out, (line_x, 0), (line_x, h), _LINE_COLOR, 2, cv2.LINE_AA)
            pct = f"{self.trigger_line_position * 100:.0f}%"
            self._put_label(out, pct, (line_x + 6, 22))

            # Detection boxes
            for d in detections:
                x1, y1, x2, y2 = (int(v) for v in d.bbox)
                color = (
                    _BOX_COUNTED
                    if d.tracker_id is not None and d.tracker_id in crossed_ids
                    else _BOX_NEW
                )
                cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
                tag = f"#{d.tracker_id}" if d.tracker_id is not None else "#?"
                self._put_label(
                    out,
                    f"{tag} {d.class_name} {d.confidence:.2f}",
                    (x1, max(15, y1 - 6)),
                    fg=color,
                )

            ok, jpeg = cv2.imencode(
                ".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, self.preview_jpeg_quality]
            )
            return bytes(jpeg) if ok else None
        except Exception:
            # Annotation is best-effort — never let a draw error stop the runner.
            logger.exception("annotate failed")
            return None

    @staticmethod
    def _put_label(img, text: str, org: tuple[int, int], fg=_TEXT_FG) -> None:
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    _TEXT_OUTLINE, 3, cv2.LINE_AA)
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    fg, 1, cv2.LINE_AA)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _create_iteration(self, n: int) -> UUID:
        q = self.db.table("session_iterations").insert(
            {"session_id": str(self.session_id), "iteration_number": n}
        )
        res = await asyncio.to_thread(q.execute)
        if not res.data:
            raise RuntimeError("Insert into session_iterations returned no row")
        return UUID(res.data[0]["id"])

    async def _update_iteration(self, iteration_id: UUID, data: dict) -> None:
        q = (
            self.db.table("session_iterations")
            .update(data)
            .eq("id", str(iteration_id))
        )
        await asyncio.to_thread(q.execute)

    async def _record_event(
        self,
        event: CrossingEvent,
        crossing: CrossingType,
        iteration_id: UUID,
    ) -> None:
        d = event.detection
        payload = {
            "session_id": str(self.session_id),
            "iteration_id": str(iteration_id),
            "crossing": crossing.value,
            "object_class": d.class_name,
            "confidence": d.confidence,
            "bbox": {
                "x1": d.bbox[0],
                "y1": d.bbox[1],
                "x2": d.bbox[2],
                "y2": d.bbox[3],
            },
            "detected_at": event.timestamp.isoformat(),
        }
        q = self.db.table("detection_events").insert(payload)
        await asyncio.to_thread(q.execute)

    async def _update_session(self, data: dict) -> None:
        q = self.db.table("sessions").update(data).eq("id", str(self.session_id))
        await asyncio.to_thread(q.execute)

    async def _mark_stopped(self) -> None:
        try:
            await self._update_session(
                {
                    "status": SessionStatus.STOPPED.value,
                    "ended_at": _now_iso(),
                }
            )
        except Exception:
            logger.exception("Failed to mark session %s as stopped", self.session_id)

    async def _fail(self, reason: str) -> None:
        try:
            await self._update_session(
                {
                    "status": SessionStatus.FAILED.value,
                    "ended_at": _now_iso(),
                    "notes": reason,
                }
            )
        except Exception:
            logger.exception("Failed to mark session %s as failed", self.session_id)


class RunnerRegistry:
    """In-process map of active session runners. Single-worker only."""

    def __init__(self) -> None:
        self._runners: dict[UUID, SessionRunner] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_id: UUID, runner: SessionRunner) -> None:
        async with self._lock:
            self._runners[session_id] = runner

    async def get(self, session_id: UUID) -> SessionRunner | None:
        async with self._lock:
            return self._runners.get(session_id)

    async def find_by_camera(self, camera_id: UUID) -> SessionRunner | None:
        """Return the still-running runner currently owning `camera_id`, or
        None if none. Used by the preview WebSocket to share frames with the
        runner instead of opening a second VideoCapture on the same device."""
        async with self._lock:
            for runner in self._runners.values():
                if (
                    runner.camera_id == camera_id
                    and runner.task is not None
                    and not runner.task.done()
                ):
                    return runner
        return None

    async def remove(self, session_id: UUID) -> None:
        async with self._lock:
            self._runners.pop(session_id, None)


_REGISTRY = RunnerRegistry()


def registry() -> RunnerRegistry:
    return _REGISTRY


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
