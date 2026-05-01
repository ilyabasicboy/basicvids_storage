from __future__ import annotations

import re
import unicodedata

from fastapi import HTTPException
from sqlmodel import Session, select

from basicvids_storage.auth import CurrentUser
from basicvids_storage.models.categories import Category, CategoryPublic


MAX_CATEGORY_DEPTH = 3


def normalize_category_name(name: str) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Category name is required")
    return clean_name


def normalize_category_description(description: str | None) -> str | None:
    if description is None:
        return None

    clean_description = description.strip()
    return clean_description or None


def slugify_category(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="Category slug is invalid")
    return slug[:120]


def ensure_category_admin(current_user: CurrentUser) -> None:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can manage categories")


def get_category_or_404(session: Session, category_id: int) -> Category:
    category = session.get(Category, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


def get_category_depth(category: Category, categories_by_id: dict[int, Category]) -> int:
    depth = 1
    current_parent_id = category.parent_id
    visited = set()

    while current_parent_id is not None:
        if current_parent_id in visited:
            raise HTTPException(status_code=400, detail="Category hierarchy contains a cycle")
        visited.add(current_parent_id)
        parent = categories_by_id.get(current_parent_id)
        if parent is None:
            raise HTTPException(status_code=400, detail="Parent category not found")
        depth += 1
        current_parent_id = parent.parent_id

    return depth


def validate_category_parent(
    session: Session,
    *,
    parent_id: int | None,
    category_id: int | None = None,
) -> Category | None:
    if parent_id is None:
        return None

    if category_id is not None and parent_id == category_id:
        raise HTTPException(status_code=400, detail="Category cannot be its own parent")

    parent = get_category_or_404(session, parent_id)
    categories = session.exec(select(Category)).all()
    categories_by_id = {category.id: category for category in categories if category.id is not None}

    visited = set()
    current = parent
    while True:
        if current.id == category_id:
            raise HTTPException(status_code=400, detail="Category hierarchy contains a cycle")
        if current.parent_id is None:
            break
        if current.id in visited:
            raise HTTPException(status_code=400, detail="Category hierarchy contains a cycle")
        visited.add(current.id)
        next_parent = categories_by_id.get(current.parent_id)
        if next_parent is None:
            raise HTTPException(status_code=400, detail="Parent category not found")
        current = next_parent

    new_parent_depth = get_category_depth(parent, categories_by_id)
    subtree_height = get_category_subtree_height(category_id, categories)
    if new_parent_depth + subtree_height > MAX_CATEGORY_DEPTH:
        raise HTTPException(status_code=400, detail=f"Category nesting cannot exceed {MAX_CATEGORY_DEPTH} levels")

    return parent


def ensure_unique_category_slug(session: Session, slug: str, *, exclude_category_id: int | None = None) -> None:
    existing = session.exec(select(Category).where(Category.slug == slug)).first()
    if existing and existing.id != exclude_category_id:
        raise HTTPException(status_code=400, detail="Category slug already exists")


def build_category_tree(categories: list[Category]) -> list[CategoryPublic]:
    sorted_categories = sorted(categories, key=lambda item: ((item.parent_id or 0), item.name.lower(), item.id or 0))
    categories_by_id = {category.id: category for category in sorted_categories if category.id is not None}
    public_nodes = {
        category.id: category_to_public(category, categories_by_id)
        for category in sorted_categories
        if category.id is not None
    }

    roots: list[CategoryPublic] = []
    for category in sorted_categories:
        if category.id is None:
            continue
        node = public_nodes[category.id]
        if category.parent_id is None:
            roots.append(node)
            continue
        parent = public_nodes.get(category.parent_id)
        if parent is None:
            roots.append(node)
            continue
        parent.children.append(node)

    return roots


def category_to_public(category: Category, categories_by_id: dict[int, Category]) -> CategoryPublic:
    if category.id is None:
        raise HTTPException(status_code=500, detail="Category is missing an identifier")

    return CategoryPublic(
        id=category.id,
        name=category.name,
        slug=category.slug,
        description=category.description,
        parent_id=category.parent_id,
        depth=get_category_depth(category, categories_by_id),
        created_by_user_id=category.created_by_user_id,
        status=category.status,
        is_system=category.is_system,
        created_at=category.created_at,
    )


def collect_descendant_ids(categories: list[Category], category_id: int) -> set[int]:
    children_by_parent: dict[int | None, list[Category]] = {}
    for category in categories:
        children_by_parent.setdefault(category.parent_id, []).append(category)

    descendants: set[int] = set()
    stack = [category_id]
    while stack:
        current_id = stack.pop()
        descendants.add(current_id)
        for child in children_by_parent.get(current_id, []):
            if child.id is not None and child.id not in descendants:
                stack.append(child.id)

    return descendants


def get_category_subtree_height(category_id: int | None, categories: list[Category]) -> int:
    if category_id is None:
        return 1

    children_by_parent: dict[int | None, list[Category]] = {}
    for category in categories:
        children_by_parent.setdefault(category.parent_id, []).append(category)

    def walk(current_id: int) -> int:
        children = children_by_parent.get(current_id, [])
        if not children:
            return 1
        child_heights = [walk(child.id) for child in children if child.id is not None]
        return 1 + max(child_heights, default=0)

    return walk(category_id)
