from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.common import CameraStatus, ORMModel


class CameraBase(BaseModel):
    name: str
    location: str | None = None
    stream_url: str


class CameraCreate(CameraBase):
    pass


class CameraUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    stream_url: str | None = None


class CameraOut(CameraBase, ORMModel):
    id: UUID
    organization_id: UUID
    status: CameraStatus
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CameraStatusOut(BaseModel):
    id: UUID
    status: CameraStatus
    last_seen_at: datetime | None = None
