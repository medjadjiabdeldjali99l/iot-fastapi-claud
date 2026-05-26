"""Cadence runner — mode intervalle uniquement.

Lifecycle of `sessions.status`:
    pending -> (measuring -> paused)+ -> stopped
    on cancel  -> stopped
    on error   -> failed

Pour chaque cycle :
    1. Ouvre le flux pendant `measurement_window_seconds`.
    2. Collecte les timestamps de tous les franchissements de la ligne.
    3. Calcule la moyenne des écarts consécutifs (t1-t0, t2-t1, ...).
    4. UPDATE l'itération avec object_count + avg_delta_seconds ; le trigger SQL
       `compute_iteration_metrics` calcule opm = 60 / avg_delta_seconds.
    5. Détecte une anomalie en comparant l'OPM à la moyenne des itérations passées.
    6. Pause `interval_minutes` minutes, puis recommence.

On ne stocke pas les timestamps bruts en base — uniquement la moyenne agrégée.

Les runners vivent dans un registre in-process. Un redémarrage worker les perd ;
les sessions concernées restent en `measuring`/`paused` jusqu'à un nettoyage
externe. Limitation connue du déploiement v1 (single-worker)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from supabase import Client

from app.models.common import CadenceStatus, SessionStatus
from app.services.detection import Detection, YoloDetector
from app.services.mqtt_publisher import publish_cadence_iteration
from app.services.stream import RTSPStream, StreamUnavailable
from app.services.tracking import LineCrosser

# `stream` est importé en premier pour que sa var d'env
# `OPENCV_FFMPEG_CAPTURE_OPTIONS` soit posée avant le premier import de cv2.
import cv2  # noqa: E402

logger = logging.getLogger(__name__)

_LINE_COLOR = (0, 255, 255)         # jaune — ligne de trigger
_BOX_NEW = (80, 220, 100)           # vert — tracké, pas encore compté
_BOX_COUNTED = (160, 160, 160)      # gris — déjà compté dans cette itération
_TEXT_FG = (0, 255, 255)
_TEXT_OUTLINE = (0, 0, 0)


def compute_anomaly(
    current_opm: float,
    baseline_opms: list[float],
    threshold_pct: float,
) -> tuple[bool, float] | None:
    """Pure helper : `current_opm` est-il une anomalie vs la moyenne des `baseline_opms` ?

    Retourne (is_anomaly, deviation_pct) ou None s'il n'y a pas encore de baseline
    (première itération, ou tous les baselines non positifs)."""
    if not baseline_opms:
        return None
    avg = sum(baseline_opms) / len(baseline_opms)
    if avg <= 0:
        return None
    deviation_pct = abs(current_opm - avg) / avg * 100.0
    return deviation_pct > threshold_pct, round(deviation_pct, 2)


def compute_avg_delta_seconds(timestamps: list[datetime]) -> float | None:
    """Moyenne des écarts consécutifs entre franchissements, en secondes.

    Retourne None si moins de 2 franchissements (pas d'écart calculable)."""
    if len(timestamps) < 2:
        return None
    deltas = [
        (timestamps[i] - timestamps[i - 1]).total_seconds()
        for i in range(1, len(timestamps))
    ]
    return sum(deltas) / len(deltas)


def compute_cadence_status(
    opm: float | None,
    ref_min: float | None,
    ref_max: float | None,
) -> CadenceStatus | None:
    """Compare un OPM à la plage de référence de la session.

    Retourne None si l'OPM ou la plage est manquant (pas de comparaison
    possible). Sinon : BELOW (< min), NORMAL (min ≤ opm ≤ max) ou ABOVE (> max)."""
    if opm is None or ref_min is None or ref_max is None:
        return None
    if opm < ref_min:
        return CadenceStatus.BELOW
    if opm > ref_max:
        return CadenceStatus.ABOVE
    return CadenceStatus.NORMAL


class SessionRunner:
    """Drives a cadence measurement session jusqu'à un /stop manuel."""

    def __init__(
        self,
        session_id: UUID,
        camera_id: UUID,
        stream_url: str,
        trigger_line_position: float,
        yolo_confidence: float,
        yolo_model: str,
        db: Client,
        interval_minutes: int,
        measurement_window_seconds: int = 120,
        anomaly_threshold_pct: float = 15.0,
        reference_cadence_min: float | None = None,
        reference_cadence_max: float | None = None,
        target_fps: int = 10,
        preview_jpeg_quality: int = 70,
        # Méta MQTT alignée sur la convention de l'équipe IoT Miniros
        # (topic `Miniros/{factory}/{line}/{machine}/...`). Si None, les
        # fallbacks de settings sont utilisés au build du topic.
        mqtt_factory: str | None = None,
        mqtt_line: str | None = None,
        mqtt_machine: str | None = None,
    ) -> None:
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be > 0")
        if measurement_window_seconds <= 0:
            raise ValueError("measurement_window_seconds must be > 0")
        if (reference_cadence_min is None) != (reference_cadence_max is None):
            raise ValueError(
                "reference_cadence_min and reference_cadence_max must be both set or both None"
            )
        if (
            reference_cadence_min is not None
            and reference_cadence_max is not None
            and reference_cadence_min > reference_cadence_max
        ):
            raise ValueError("reference_cadence_min must be <= reference_cadence_max")
        self.session_id = session_id
        self.camera_id = camera_id
        self.stream_url = stream_url
        self.trigger_line_position = trigger_line_position
        self.yolo_confidence = yolo_confidence
        self.yolo_model = yolo_model
        self.db = db
        self.interval_minutes = interval_minutes
        self.measurement_window_seconds = measurement_window_seconds
        self.anomaly_threshold_pct = anomaly_threshold_pct
        self.reference_cadence_min = reference_cadence_min
        self.reference_cadence_max = reference_cadence_max
        self.target_fps = target_fps
        self.preview_jpeg_quality = max(10, min(preview_jpeg_quality, 95))
        self.mqtt_factory = mqtt_factory
        self.mqtt_line = mqtt_line
        self.mqtt_machine = mqtt_machine

        self._task: asyncio.Task | None = None
        self._stop_requested = False
        # Dernier JPEG annoté, consommé par le WebSocket de preview.
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
    # Boucle principale
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
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

    async def _run_interval(self) -> None:
        print(f">>> RUNNER START session={self.session_id} stream={self.stream_url}", flush=True)
        iteration_number = 0
        while not self._stop_requested:
            iteration_number += 1
            print(f">>> ITER {iteration_number} START", flush=True)

            # Nouveau détecteur + crosser par itération : ByteTrack persiste son
            # état entre appels, repartir à zéro garantit des IDs propres après
            # la pause.
            detector = YoloDetector(self.yolo_model)
            crosser = LineCrosser(self.trigger_line_position)
            stream = RTSPStream(self.stream_url, target_fps=self.target_fps)
            iteration_id = await self._create_iteration(iteration_number)

            await self._update_session({"status": SessionStatus.MEASURING.value})
            measurement_start = _utcnow()
            await self._update_iteration(
                iteration_id,
                {"measurement_started_at": measurement_start.isoformat()},
            )

            print(
                f"[session {self.session_id} iter {iteration_number}] "
                f"window opened ({self.measurement_window_seconds}s) — "
                f"collecting crossings…",
                flush=True,
            )
            try:
                timestamps = await self._collect_window(
                    stream, detector, crosser, measurement_start, iteration_number
                )
            finally:
                await stream.close()

            measurement_end = _utcnow()
            avg_delta = compute_avg_delta_seconds(timestamps)
            # On calcule l'OPM côté Python pour pouvoir l'utiliser tout de
            # suite dans la comparaison de plage. Le trigger SQL recalcule
            # ensuite la même valeur (no-op).
            opm: float | None = (
                round(60.0 / avg_delta, 2)
                if avg_delta is not None and avg_delta > 0
                else None
            )
            cadence_status = compute_cadence_status(
                opm, self.reference_cadence_min, self.reference_cadence_max
            )
            self._log_window_summary(
                iteration_number, timestamps, avg_delta, opm, cadence_status
            )
            await self._update_iteration(
                iteration_id,
                {
                    "measurement_ended_at": measurement_end.isoformat(),
                    "object_count": len(timestamps),
                    "avg_delta_seconds": avg_delta,  # déclenche le calcul de opm
                    "cadence_status": (
                        cadence_status.value if cadence_status is not None else None
                    ),
                },
            )

            # Envoi MQTT vers Odoo de la cadence moyenne calculée sur la
            # fenêtre qui vient de se terminer. Best-effort : si MQTT est
            # désactivé / broker injoignable, l'appel est silencieux.
            publish_cadence_iteration(
                factory=self.mqtt_factory,
                line=self.mqtt_line,
                machine=self.mqtt_machine,
                session_id=self.session_id,
                iteration_number=iteration_number,
                cadence_moyenne=opm,
                temps_debut=measurement_start,
                temps_fin=measurement_end,
                object_count=len(timestamps),
                avg_delta_seconds=avg_delta,
                cadence_status=(
                    cadence_status.value if cadence_status is not None else None
                ),
            )

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
    # Collecte sur une fenêtre
    # ------------------------------------------------------------------

    async def _collect_window(
        self,
        stream: RTSPStream,
        detector: YoloDetector,
        crosser: LineCrosser,
        window_start: datetime,
        iteration_number: int,
    ) -> list[datetime]:
        print(f">>> WINDOW OPEN iter={iteration_number} (line={self.trigger_line_position})", flush=True)
        deadline = window_start.timestamp() + self.measurement_window_seconds
        timestamps: list[datetime] = []
        frame_count = 0

        async for frame in stream.raw_frames():
            if self._stop_requested:
                return timestamps
            if _utcnow().timestamp() >= deadline:
                return timestamps

            detections = await detector.track(frame, self.yolo_confidence)
            events = crosser.update(detections, frame.shape[1])

            self._latest_frame = self._encode_annotated(
                frame, detections, crosser.crossed_ids
            )

            frame_count += 1
            # Heartbeat toutes les ~30 frames pour confirmer que le runner tourne
            if frame_count % 30 == 0:
                print(
                    f"... frame={frame_count} detections={len(detections)} "
                    f"crossings={len(timestamps)}",
                    flush=True,
                )

            for event in events:
                idx = len(timestamps)
                timestamps.append(event.timestamp)
                print(f"t{idx} = {event.timestamp.isoformat(timespec='milliseconds')}", flush=True)

        return timestamps

    # ------------------------------------------------------------------
    # Logging du calcul
    # ------------------------------------------------------------------

    def _log_window_summary(
        self,
        iteration_number: int,
        timestamps: list[datetime],
        avg_delta: float | None,
        opm: float | None,
        cadence_status: CadenceStatus | None,
    ) -> None:
        n = len(timestamps)
        tag = f"[session {self.session_id} iter {iteration_number}]"
        range_str = (
            f" ref=[{self.reference_cadence_min}, {self.reference_cadence_max}]"
            if self.reference_cadence_min is not None
            else ""
        )
        status_str = (
            f" status={cadence_status.value.upper()}"
            if cadence_status is not None
            else ""
        )
        if n < 2:
            print(
                f"{tag} window closed: {n} object(s) — pas assez pour un delta, "
                f"opm = NULL{range_str}",
                flush=True,
            )
            return
        deltas = [
            (timestamps[i] - timestamps[i - 1]).total_seconds()
            for i in range(1, n)
        ]
        deltas_str = ", ".join(f"{d:.3f}" for d in deltas)
        if opm is not None:
            print(
                f"{tag} window closed: {n} objects | deltas (s) = [{deltas_str}] | "
                f"avg_delta = {avg_delta:.3f}s -> opm = {opm:.2f}"
                f"{range_str}{status_str}",
                flush=True,
            )
        else:
            print(
                f"{tag} window closed: {n} objects | deltas (s) = [{deltas_str}] | "
                f"avg_delta = {avg_delta} -> opm = NULL{range_str}",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    async def _compute_anomaly_for(self, iteration_id: UUID) -> None:
        # opm est NULL si l'itération n'a pas eu assez d'objets — skip.
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
        try:
            out = frame.copy()
            h, w = out.shape[:2]

            line_x = int(w * self.trigger_line_position)
            cv2.line(out, (line_x, 0), (line_x, h), _LINE_COLOR, 2, cv2.LINE_AA)
            pct = f"{self.trigger_line_position * 100:.0f}%"
            self._put_label(out, pct, (line_x + 6, 22))

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
        None if none. Le WebSocket de preview s'en sert pour partager les
        frames du runner plutôt que d'ouvrir un second VideoCapture."""
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()