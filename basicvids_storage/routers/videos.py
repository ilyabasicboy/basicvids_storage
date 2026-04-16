from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, select

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.db import get_session
from basicvids_storage.models.videos import Video, VideoChange, VideoDeleteResponse, VideoList, VideoPublic
from basicvids_storage.settings import settings
from basicvids_storage.storage import get_storage
from basicvids_storage.storage.base import StorageBackend


router = APIRouter(tags=["Videos"], prefix="/videos")


def validate_video_upload(file: UploadFile) -> None:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=415, detail="Only video uploads are supported")


@router.post("/upload/", response_model=VideoPublic, status_code=201)
async def upload_video(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    description: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> Video:
    validate_video_upload(file)
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")

    stored_object = await storage.save_upload(file, settings.MAX_UPLOAD_SIZE_BYTES)
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
    )

    session.add(video)
    session.commit()
    session.refresh(video)
    return video


@router.get("/", response_model=VideoList)
async def list_videos(
    offset: int = 0,
    limit: int = Query(default=20, le=100),
    session: Session = Depends(get_session),
) -> VideoList:
    statement = select(Video).order_by(col(Video.created_at).desc()).offset(offset).limit(limit)
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
    session.delete(video)
    session.commit()
    return VideoDeleteResponse(message="Video deleted successfully")
