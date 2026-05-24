from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.common import ORMModel


class OrganizationBase(BaseModel):
    name: str
    slug: str


class OrganizationCreate(OrganizationBase):
    pass


class OrganizationOut(OrganizationBase, ORMModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
