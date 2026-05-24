from datetime import datetime
from uuid import UUID

from app.models.common import ORMModel


class IterationOut(ORMModel):
    id: UUID
    session_id: UUID
    iteration_number: int
    t0: datetime | None = None
    t1: datetime | None = None
    delta_seconds: float | None = None
    opm: float | None = None
    is_anomaly: bool
    anomaly_deviation: float | None = None
    created_at: datetime
    completed_at: datetime | None = None
