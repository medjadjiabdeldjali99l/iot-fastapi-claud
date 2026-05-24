from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.common import ORMModel, UserRole


class ProfileBase(BaseModel):
    full_name: str | None = None
    role: UserRole = UserRole.OPERATOR


class ProfileCreate(ProfileBase):
    id: UUID
    organization_id: UUID


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    role: UserRole | None = None


class ProfileOut(ProfileBase, ORMModel):
    id: UUID
    organization_id: UUID
    created_at: datetime
    updated_at: datetime
