from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_
from sqlmodel import Session, col, select

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.db import get_session
from basicvids_storage.models.videos import Video, VideoChange, VideoDeleteResponse, VideoList, VideoPublic
from basicvids_storage.settings import settings
from basicvids_storage.storage import get_storage
from basicvids_storage.storage.base import StorageBackend
from basicvids_storage.thumbnails import generate_video_thumbnail


router = APIRouter(tags=["Videos"], prefix="/videos")


def validate_video_upload(file: UploadFile) -> None:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=415, detail="Only video uploads are supported")


def validate_thumbnail_upload(file: UploadFile) -> None:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Thumbnail filename is required")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Only image thumbnails are supported")


@router.post("/upload/", response_model=VideoPublic, status_code=201)
async def upload_video(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    thumbnail: Annotated[UploadFile | None, File()] = None,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> Video:
    validate_video_upload(file)
    if thumbnail:
        validate_thumbnail_upload(thumbnail)
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")

    stored_object = None
    stored_thumbnail = None
    try:
        stored_object = await storage.save_upload(file, settings.MAX_UPLOAD_SIZE_BYTES)
        if thumbnail:
            stored_thumbnail = await storage.save_upload(thumbnail, settings.MAX_THUMBNAIL_SIZE_BYTES)
            thumbnail_content_type = thumbnail.content_type
        else:
            generated_thumbnail = await generate_video_thumbnail(storage.path_for(stored_object.key), storage, settings)
            if generated_thumbnail:
                stored_thumbnail = generated_thumbnail.stored_object
                thumbnail_content_type = generated_thumbnail.content_type
            else:
                thumbnail_content_type = None

        video = Video(
            title=clean_title,
            description=description.strip() if description else None,
            original_filename=file.filename,
            content_type=file.content_type,
            size_bytes=stored_object.size_bytes,
            author_id=current_user.id,
            author_username=current_user.username,
            author_first_name=current_user.first_name,
            author_last_name=current_user.last_name,
            storage_backend=storage.name,
            storage_key=stored_object.key,
            thumbnail_storage_key=stored_thumbnail.key if stored_thumbnail else None,
            thumbnail_content_type=thumbnail_content_type if stored_thumbnail else None,
            thumbnail_size_bytes=stored_thumbnail.size_bytes if stored_thumbnail else None,
        )

        session.add(video)
        session.commit()
    except Exception:
        session.rollback()
        if stored_object:
            storage.delete(stored_object.key)
        if stored_thumbnail:
            storage.delete(stored_thumbnail.key)
        raise

    session.refresh(video)
    return video


@router.put("/{video_id}/thumbnail/", response_model=VideoPublic)
async def set_video_thumbnail(
    video_id: str,
    thumbnail: Annotated[UploadFile, File()],
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> Video:
    validate_thumbnail_upload(thumbnail)
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can change this video")

    old_thumbnail_key = video.thumbnail_storage_key
    stored_thumbnail = await storage.save_upload(thumbnail, settings.MAX_THUMBNAIL_SIZE_BYTES)
    video.thumbnail_storage_key = stored_thumbnail.key
    video.thumbnail_content_type = thumbnail.content_type
    video.thumbnail_size_bytes = stored_thumbnail.size_bytes

    try:
        session.add(video)
        session.commit()
    except Exception:
        session.rollback()
        storage.delete(stored_thumbnail.key)
        raise

    if old_thumbnail_key:
        storage.delete(old_thumbnail_key)
    session.refresh(video)
    return video


@router.get("/", response_model=VideoList)
async def list_videos(
    offset: int = 0,
    limit: int = Query(default=20, le=100),
    search: str | None = Query(default=None, max_length=255),
    session: Session = Depends(get_session),
) -> VideoList:
    statement = select(Video)
    clean_search = search.strip() if search else ""

    if clean_search:
        search_pattern = f"%{clean_search.lower()}%"
        title_match = func.lower(Video.title).like(search_pattern)
        description_match = func.lower(func.coalesce(Video.description, "")).like(search_pattern)
        statement = statement.where(or_(title_match, description_match)).order_by(
            case((title_match, 0), else_=1),
            col(Video.created_at).desc(),
        )
    else:
        statement = statement.order_by(col(Video.created_at).desc())

    statement = statement.offset(offset).limit(limit)
    videos = session.exec(statement).all()
    return VideoList(
        videos=[VideoPublic.model_validate(video) for video in videos],
        count=len(videos),
    )


@router.get("/{video_id}", response_model=VideoPublic)
async def get_video(
    video_id: str,
    session: Session = Depends(get_session),
) -> Video:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@router.get("/{video_id}/download/")
async def download_video(
    video_id: str,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> StreamingResponse:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    file_path = storage.path_for(video.storage_key)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    async def stream_file():
        with file_path.open("rb") as stored_file:
            while chunk := stored_file.read(1024 * 1024):
                yield chunk

    encoded_filename = quote(video.original_filename)
    return StreamingResponse(
        stream_file(),
        media_type=video.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/{video_id}/thumbnail/")
async def download_video_thumbnail(
    video_id: str,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> StreamingResponse:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.thumbnail_storage_key:
        raise HTTPException(status_code=404, detail="Video thumbnail not found")

    file_path = storage.path_for(video.thumbnail_storage_key)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video thumbnail file not found")

    async def stream_file():
        with file_path.open("rb") as stored_file:
            while chunk := stored_file.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        stream_file(),
        media_type=video.thumbnail_content_type or "application/octet-stream",
    )


@router.patch("/{video_id}", response_model=VideoPublic)
async def change_video(
    video_id: str,
    data: VideoChange,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Video:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can change this video")

    clean_title = data.title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")

    video.title = clean_title
    video.description = data.description.strip() if data.description else None
    session.add(video)
    session.commit()
    session.refresh(video)
    return video


@router.delete("/{video_id}", response_model=VideoDeleteResponse, status_code=200)
async def delete_video(
    video_id: str,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> VideoDeleteResponse:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can delete this video")

    storage.delete(video.storage_key)
    if video.thumbnail_storage_key:
        storage.delete(video.thumbnail_storage_key)
    session.delete(video)
    session.commit()
    return VideoDeleteResponse(message="Video deleted successfully")
