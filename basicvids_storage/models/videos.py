from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Video(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    original_filename: str = Field(max_length=255)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(ge=0)
    storage_backend: str = Field(default="disk", max_length=50)
    storage_key: str = Field(unique=True, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)


class VideoPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    original_filename: str
    content_type: str
    size_bytes: int
    storage_backend: str
    created_at: datetime


class VideoList(BaseModel):
    videos: list[VideoPublic]
    count: int
