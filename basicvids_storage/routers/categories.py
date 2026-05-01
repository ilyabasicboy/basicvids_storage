from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.categories import (
    build_category_tree,
    category_to_public,
    ensure_category_admin,
    ensure_unique_category_slug,
    get_category_or_404,
    normalize_category_description,
    normalize_category_name,
    slugify_category,
    validate_category_parent,
)
from basicvids_storage.db import get_session
from basicvids_storage.models.categories import Category, CategoryChange, CategoryCreate, CategoryPublic
from basicvids_storage.models.videos import Video


router = APIRouter(tags=["Categories"], prefix="/categories")


@router.get("/", response_model=list[CategoryPublic])
async def list_categories(session: Session = Depends(get_session)) -> list[CategoryPublic]:
    categories = session.exec(
        select(Category)
        .where(Category.status == "approved")
        .order_by(Category.parent_id, Category.name, Category.id)
    ).all()
    return build_category_tree(categories)


@router.post("/", response_model=CategoryPublic, status_code=201)
async def create_category(
    data: CategoryCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> CategoryPublic:
    ensure_category_admin(current_user)
    validate_category_parent(session, parent_id=data.parent_id)

    name = normalize_category_name(data.name)
    slug = slugify_category(data.slug or name)
    ensure_unique_category_slug(session, slug)

    category = Category(
        name=name,
        slug=slug,
        description=normalize_category_description(data.description),
        parent_id=data.parent_id,
        created_by_user_id=current_user.id,
        status="approved",
        is_system=False,
    )
    session.add(category)
    session.commit()
    session.refresh(category)
    categories_by_id = {item.id: item for item in session.exec(select(Category)).all() if item.id is not None}
    return category_to_public(category, categories_by_id)


@router.patch("/{category_id}", response_model=CategoryPublic)
async def change_category(
    category_id: int,
    data: CategoryChange,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> CategoryPublic:
    ensure_category_admin(current_user)
    category = get_category_or_404(session, category_id)

    if "parent_id" in data.model_fields_set:
        validate_category_parent(session, parent_id=data.parent_id, category_id=category_id)
        category.parent_id = data.parent_id

    if "name" in data.model_fields_set:
        category.name = normalize_category_name(data.name or "")

    if "description" in data.model_fields_set:
        category.description = normalize_category_description(data.description)

    if "slug" in data.model_fields_set:
        slug_source = data.slug or category.name
        category.slug = slugify_category(slug_source)

    ensure_unique_category_slug(session, category.slug, exclude_category_id=category_id)

    session.add(category)
    session.commit()
    session.refresh(category)
    categories_by_id = {item.id: item for item in session.exec(select(Category)).all() if item.id is not None}
    return category_to_public(category, categories_by_id)


@router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    ensure_category_admin(current_user)
    category = get_category_or_404(session, category_id)

    has_children = session.exec(select(Category.id).where(Category.parent_id == category_id)).first()
    if has_children is not None:
        raise HTTPException(status_code=409, detail="Category has subcategories")

    attached_videos = session.exec(select(Video).where(Video.category_id == category_id)).all()
    for video in attached_videos:
        video.category_id = category.parent_id
        session.add(video)

    session.delete(category)
    session.commit()
