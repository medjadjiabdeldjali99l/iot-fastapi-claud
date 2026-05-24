from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.services.detection import Detection


Direction = Literal["left_to_right", "right_to_left", "any"]


@dataclass
class CrossingEvent:
    tracker_id: int
    detection: Detection
    timestamp: datetime
    direction: Direction
    line_x_px: int


class LineCrosser:
    """Detects when tracked objects cross a vertical line in the frame.

    Each tracker_id may emit at most one crossing event in a session; later
    re-crossings by the same id are suppressed. `reset()` clears state.

    `line_position` is a ratio of frame width in [0, 1] — matches the value
    saved on `camera_configs.trigger_line_position`."""

    def __init__(
        self,
        line_position: float = 0.80,
        direction: Direction = "any",
    ) -> None:
        if not 0.0 <= line_position <= 1.0:
            raise ValueError("line_position must be in [0, 1]")
        self.line_position = line_position
        self.direction = direction
        self._last_x: dict[int, float] = {}
        self._crossed: set[int] = set()

    def update(
        self,
        detections: list[Detection],
        frame_width: int,
    ) -> list[CrossingEvent]:
        line_x = frame_width * self.line_position
        events: list[CrossingEvent] = []
        active_ids: set[int] = set()

        for det in detections:
            if det.tracker_id is None:
                continue
            tid = det.tracker_id
            active_ids.add(tid)
            cx = (det.bbox[0] + det.bbox[2]) / 2.0
            prev_x = self._last_x.get(tid)
            self._last_x[tid] = cx

            if prev_x is None or tid in self._crossed:
                continue

            crossed_lr = prev_x < line_x <= cx
            crossed_rl = prev_x > line_x >= cx
            actual: Direction | None = None
            if crossed_lr and self.direction in ("any", "left_to_right"):
                actual = "left_to_right"
            elif crossed_rl and self.direction in ("any", "right_to_left"):
                actual = "right_to_left"

            if actual is not None:
                self._crossed.add(tid)
                events.append(
                    CrossingEvent(
                        tracker_id=tid,
                        detection=det,
                        timestamp=datetime.now(timezone.utc),
                        direction=actual,
                        line_x_px=int(line_x),
                    )
                )

        # Forget ids that left the frame, so memory stays bounded over
        # long sessions. `_crossed` is intentionally kept — if the same id
        # is reused by the tracker after a gap, we still suppress duplicates.
        for tid in list(self._last_x.keys()):
            if tid not in active_ids:
                self._last_x.pop(tid, None)

        return events

    def reset(self) -> None:
        self._last_x.clear()
        self._crossed.clear()

    @property
    def crossed_ids(self) -> set[int]:
        """tracker_ids that have already been counted in this iteration —
        used by the cadence runner to grey out boxes once they've crossed."""
        return self._crossed
