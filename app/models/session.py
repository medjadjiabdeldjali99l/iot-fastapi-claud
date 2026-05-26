from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.models.common import ORMModel, SessionStatus


class SessionBase(BaseModel):
    interval_minutes: int = Field(gt=0)
    measurement_window_seconds: int = Field(default=120, gt=0)
    anomaly_threshold_pct: float = Field(default=15.0, ge=0)
    # Plage de cadence attendue (OPM). Saisie côté UI dans la config caméra,
    # mais persistée sur la session. Soit les deux NULL (pas de comparaison),
    # soit les deux fournies avec min <= max.
    reference_cadence_min: float | None = Field(default=None, ge=0)
    reference_cadence_max: float | None = Field(default=None, ge=0)
    notes: str | None = None

    @model_validator(mode="after")
    def _check_reference_cadence(self) -> "SessionBase":
        mn, mx = self.reference_cadence_min, self.reference_cadence_max
        if (mn is None) != (mx is None):
            raise ValueError(
                "reference_cadence_min and reference_cadence_max must be both set or both null"
            )
        if mn is not None and mx is not None and mn > mx:
            raise ValueError("reference_cadence_min must be <= reference_cadence_max")
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
    status: SessionStatus
    interval_minutes: int
    measurement_window_seconds: int
    reference_cadence_min: float | None = None
    reference_cadence_max: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    iteration_count: int
    avg_opm: float | None = None
    min_opm: float | None = None
    max_opm: float | None = None
    anomaly_count: int
    below_count: int = 0
    normal_count: int = 0
    above_count: int = 0
