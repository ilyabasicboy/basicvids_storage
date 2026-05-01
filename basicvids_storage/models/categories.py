from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field as PydanticField
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Category(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=100)
    slug: str = Field(unique=True, max_length=120, index=True)
    description: str | None = Field(default=None, max_length=500)
    parent_id: int | None = Field(default=None, foreign_key="category.id", index=True)
    created_by_user_id: int | None = Field(default=None, index=True)
    status: str = Field(default="approved", max_length=20, index=True)
    is_system: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class CategoryBase(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    parent_id: int | None = None


class CategoryCreate(CategoryBase):
    pass


class CategoryChange(BaseModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None
    parent_id: int | None = None


class CategorySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    parent_id: int | None = None


class CategoryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None = None
    parent_id: int | None = None
    depth: int
    created_by_user_id: int | None = None
    status: str
    is_system: bool
    created_at: datetime
    children: list["CategoryPublic"] = PydanticField(default_factory=list)
