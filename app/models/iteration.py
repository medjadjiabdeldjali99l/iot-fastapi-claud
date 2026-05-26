from datetime import datetime
from uuid import UUID

from app.models.common import CadenceStatus, ORMModel


class IterationOut(ORMModel):
    id: UUID
    session_id: UUID
    iteration_number: int
    measurement_started_at: datetime | None = None
    measurement_ended_at: datetime | None = None
    object_count: int
    avg_delta_seconds: float | None = None
    opm: float | None = None
    is_anomaly: bool
    anomaly_deviation: float | None = None
    cadence_status: CadenceStatus | None = None
    created_at: datetime
    completed_at: datetime | None = None
