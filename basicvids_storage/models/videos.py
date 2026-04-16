from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Video(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    title: str = Field(max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    original_filename: str = Field(max_length=255)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(ge=0)
    author_id: int | None = Field(default=None, index=True)
    author_username: str | None = Field(default=None, max_length=100)
    author_first_name: str | None = Field(default=None, max_length=100)
    author_last_name: str | None = Field(default=None, max_length=100)
    storage_backend: str = Field(default="disk", max_length=50)
    storage_key: str = Field(unique=True, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)


class VideoPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None = None
    original_filename: str
    content_type: str
    size_bytes: int
    author_id: int | None = None
    author_username: str | None = None
    author_first_name: str | None = None
    author_last_name: str | None = None
    storage_backend: str
    created_at: datetime


class VideoChange(BaseModel):
    title: str
    description: str | None = None


class VideoList(BaseModel):
    videos: list[VideoPublic]
    count: int


class VideoDeleteResponse(BaseModel):
    message: str
