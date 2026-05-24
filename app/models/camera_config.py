from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.common import ORMModel, YoloModel


class CameraConfigBase(BaseModel):
    trigger_line_position: float = Field(default=0.80, ge=0, le=1)
    yolo_confidence: float = Field(default=0.50, ge=0, le=1)
    yolo_model: YoloModel = YoloModel.V8N


class CameraConfigCreate(CameraConfigBase):
    pass


class CameraConfigOut(CameraConfigBase, ORMModel):
    id: UUID
    camera_id: UUID
    is_active: bool
    created_by: UUID | None = None
    created_at: datetime
