from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.models.common import ORMModel, SessionMode, SessionStatus, YoloModel


class SessionBase(BaseModel):
    mode: SessionMode
    interval_minutes: int | None = None
    anomaly_threshold_pct: float = Field(default=15.0, ge=0)
    yolo_model_override: YoloModel | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_interval(self) -> "SessionBase":
        if self.mode == SessionMode.SINGLE and self.interval_minutes is not None:
            raise ValueError("interval_minutes must be null for single mode")
        if self.mode == SessionMode.INTERVAL and (
            self.interval_minutes is None or self.interval_minutes <= 0
        ):
            raise ValueError(
                "interval_minutes must be a positive integer for interval mode"
            )
        return self


class SessionCreate(SessionBase):
    camera_id: UUID


class SessionOut(SessionBase, ORMModel):
    id: UUID
    camera_id: UUID
    config_id: UUID
    organization_id: UUID
    status: SessionStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    started_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class SessionSummary(ORMModel):
    session_id: UUID
    organization_id: UUID
    camera_id: UUID
    mode: SessionMode
    status: SessionStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    iteration_count: int
    avg_opm: float | None = None
    min_opm: float | None = None
    max_opm: float | None = None
    anomaly_count: int
