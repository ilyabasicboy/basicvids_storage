from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field as PydanticField, computed_field
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
    thumbnail_storage_key: str | None = Field(default=None, max_length=500)
    thumbnail_content_type: str | None = Field(default=None, max_length=100)
    thumbnail_size_bytes: int | None = Field(default=None, ge=0)
    status: str = Field(default="processing", max_length=20, index=True)
    processing_error: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)


class VideoVariant(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    quality: int = Field(gt=0, index=True)
    storage_key: str = Field(unique=True, max_length=500)
    content_type: str = Field(default="video/mp4", max_length=100)
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class VideoQualityPublic(BaseModel):
    quality: int
    label: str
    size_bytes: int


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
    status: str
    processing_error: str | None = None
    thumbnail_storage_key: str | None = PydanticField(default=None, exclude=True)
    created_at: datetime
    qualities: list[VideoQualityPublic] = PydanticField(default_factory=list)

    @computed_field
    @property
    def has_thumbnail(self) -> bool:
        return bool(self.thumbnail_storage_key)


class VideoChange(BaseModel):
    title: str
    description: str | None = None


class VideoList(BaseModel):
    videos: list[VideoPublic]
    count: int


class VideoDeleteResponse(BaseModel):
    message: str
