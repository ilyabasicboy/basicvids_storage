from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Avatar(SQLModel, table=True):
    user_id: int = Field(primary_key=True)
    storage_backend: str = Field(default="disk", max_length=50)
    storage_key: str = Field(unique=True, max_length=500)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AvatarPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    storage_backend: str
    content_type: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime


class AvatarDeleteResponse(BaseModel):
    message: str
