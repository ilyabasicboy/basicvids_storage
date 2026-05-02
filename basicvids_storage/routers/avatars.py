from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlmodel import Session

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.db import get_session
from basicvids_storage.models.avatars import Avatar, AvatarDeleteResponse, AvatarPublic, utc_now
from basicvids_storage.rate_limit import client_identifier, enforce_rate_limit
from basicvids_storage.settings import settings
from basicvids_storage.storage import get_storage
from basicvids_storage.storage.base import StorageBackend


router = APIRouter(tags=["Avatars"], prefix="/avatars")

DEFAULT_AVATAR_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" role="img" aria-label="Default avatar">
  <rect width="96" height="96" rx="18" fill="#d7dee8"/>
  <circle cx="48" cy="36" r="18" fill="#f7f9fc"/>
  <path d="M20 84c3-18 16-28 28-28s25 10 28 28" fill="#f7f9fc"/>
</svg>"""


def placeholder_avatar_response() -> Response:
    return Response(content=DEFAULT_AVATAR_SVG, media_type="image/svg+xml")


def validate_avatar_upload(file: UploadFile) -> None:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Avatar filename is required")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Only image avatars are supported")


async def save_avatar(
    user_id: int,
    avatar_file: UploadFile,
    session: Session,
    storage: StorageBackend,
    replace_existing: bool,
) -> Avatar:
    validate_avatar_upload(avatar_file)

    avatar = session.get(Avatar, user_id)
    if avatar and not replace_existing:
        raise HTTPException(status_code=409, detail="Avatar already exists")

    old_storage_key = avatar.storage_key if avatar else None
    stored_avatar = await storage.save_upload(avatar_file, settings.MAX_AVATAR_SIZE_BYTES)
    now = utc_now()

    if avatar:
        avatar.storage_backend = storage.name
        avatar.storage_key = stored_avatar.key
        avatar.content_type = avatar_file.content_type or "application/octet-stream"
        avatar.size_bytes = stored_avatar.size_bytes
        avatar.updated_at = now
    else:
        avatar = Avatar(
            user_id=user_id,
            storage_backend=storage.name,
            storage_key=stored_avatar.key,
            content_type=avatar_file.content_type or "application/octet-stream",
            size_bytes=stored_avatar.size_bytes,
            created_at=now,
            updated_at=now,
        )

    try:
        session.add(avatar)
        session.commit()
    except Exception:
        session.rollback()
        storage.delete(stored_avatar.key)
        raise

    if old_storage_key:
        storage.delete(old_storage_key)

    session.refresh(avatar)
    return avatar


@router.post("/users/{user_id}/registration/", response_model=AvatarPublic, status_code=201)
async def create_registration_avatar(
    user_id: int,
    request: Request,
    avatar: Annotated[UploadFile, File()],
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> Avatar:
    await enforce_rate_limit("registration_avatar_ip", client_identifier(request), 10, 3600)
    return await save_avatar(
        user_id=user_id,
        avatar_file=avatar,
        session=session,
        storage=storage,
        replace_existing=False,
    )


@router.put("/me/", response_model=AvatarPublic)
async def set_current_user_avatar(
    request: Request,
    avatar: Annotated[UploadFile, File()],
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> Avatar:
    await enforce_rate_limit("avatar_upload_user", f"user:{current_user.id}", 20, 3600)
    return await save_avatar(
        user_id=current_user.id,
        avatar_file=avatar,
        session=session,
        storage=storage,
        replace_existing=True,
    )


@router.get("/users/{user_id}/", response_model=AvatarPublic)
async def get_user_avatar_detail(
    user_id: int,
    session: Session = Depends(get_session),
) -> Avatar:
    avatar = session.get(Avatar, user_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    return avatar


@router.get("/users/{user_id}/image/")
async def download_user_avatar(
    user_id: int,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> StreamingResponse:
    avatar = session.get(Avatar, user_id)
    if not avatar:
        return placeholder_avatar_response()

    file_path = storage.path_for(avatar.storage_key)
    if not file_path.exists():
        return placeholder_avatar_response()

    async def stream_file():
        with file_path.open("rb") as stored_file:
            while chunk := stored_file.read(1024 * 1024):
                yield chunk

    return StreamingResponse(stream_file(), media_type=avatar.content_type)


@router.delete("/me/", response_model=AvatarDeleteResponse)
async def delete_current_user_avatar(
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> AvatarDeleteResponse:
    avatar = session.get(Avatar, current_user.id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")

    storage.delete(avatar.storage_key)
    session.delete(avatar)
    session.commit()
    return AvatarDeleteResponse(message="Avatar deleted successfully")
