import asyncio
from dataclasses import dataclass
from threading import Lock
from typing import Iterable

import numpy as np
from ultralytics import YOLO


@dataclass
class Detection:
    tracker_id: int | None
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    confidence: float
    class_id: int
    class_name: str


class YoloDetector:
    """ultralytics YOLO wrapper for detection + ByteTrack tracking.

    Tracker state lives inside `self._model.predictor` — create a new
    instance per measurement session to start with fresh tracker IDs.
    `model.track(..., persist=True)` accumulates state across calls."""

    def __init__(self, model_name: str = "yolov8n") -> None:
        self.model_name = model_name
        self._model: YOLO | None = None
        self._lock = Lock()

    def _ensure_loaded(self) -> YOLO:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                weights = (
                    self.model_name
                    if self.model_name.endswith(".pt")
                    else f"{self.model_name}.pt"
                )
                self._model = YOLO(weights)
        return self._model

    def detect_sync(
        self,
        frame: np.ndarray,
        confidence: float = 0.5,
    ) -> list[Detection]:
        """Stateless inference — no tracker IDs."""
        model = self._ensure_loaded()
        results = model.predict(frame, conf=confidence, verbose=False)
        return list(self._parse(results))

    def track_sync(
        self,
        frame: np.ndarray,
        confidence: float = 0.5,
    ) -> list[Detection]:
        """Stateful tracking — populates `tracker_id` on each detection."""
        model = self._ensure_loaded()
        results = model.track(frame, conf=confidence, persist=True, verbose=False)
        return list(self._parse(results))

    async def detect(
        self,
        frame: np.ndarray,
        confidence: float = 0.5,
    ) -> list[Detection]:
        return await asyncio.to_thread(self.detect_sync, frame, confidence)

    async def track(
        self,
        frame: np.ndarray,
        confidence: float = 0.5,
    ) -> list[Detection]:
        return await asyncio.to_thread(self.track_sync, frame, confidence)

    @staticmethod
    def _parse(results) -> Iterable[Detection]:
        for result in results:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                continue
            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy()
            classes = boxes.cls.cpu().numpy().astype(int)
            ids_tensor = getattr(boxes, "id", None)
            tracker_ids = (
                ids_tensor.cpu().numpy().astype(int).tolist()
                if ids_tensor is not None
                else [None] * len(boxes)
            )
            names = result.names
            for i in range(len(boxes)):
                cid = int(classes[i])
                yield Detection(
                    tracker_id=tracker_ids[i],
                    bbox=(
                        float(xyxy[i][0]),
                        float(xyxy[i][1]),
                        float(xyxy[i][2]),
                        float(xyxy[i][3]),
                    ),
                    confidence=float(confs[i]),
                    class_id=cid,
                    class_name=names.get(cid, str(cid)),
                )
