from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.common import CrossingType, ORMModel


class DetectionEventCreate(BaseModel):
    session_id: UUID
    iteration_id: UUID | None = None
    crossing: CrossingType
    object_class: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    bbox: dict[str, Any] | None = None


class DetectionEventOut(ORMModel):
    id: UUID
    session_id: UUID
    iteration_id: UUID | None = None
    crossing: CrossingType
    object_class: str | None = None
    confidence: float | None = None
    bbox: dict[str, Any] | None = None
    detected_at: datetime
