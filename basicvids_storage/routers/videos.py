from datetime import timedelta
import math
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_
from sqlmodel import Session, col, select

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.categories import collect_descendant_ids, get_category_or_404
from basicvids_storage.db import get_session
from basicvids_storage.models.categories import Category, CategorySummary
from basicvids_storage.models.videos import (
    Video,
    VideoChange,
    VideoDeleteResponse,
    VideoList,
    VideoPublic,
    VideoQualityPublic,
    VideoUploadSession,
    VideoUploadSessionCreate,
    VideoUploadSessionPublic,
    VideoVariant,
    utc_now,
)
from basicvids_storage.rate_limit import client_identifier, enforce_rate_limit
from basicvids_storage.settings import settings
from basicvids_storage.storage import get_storage
from basicvids_storage.storage.base import StorageBackend
from basicvids_storage.tasks import enqueue_video_processing


router = APIRouter(tags=["Videos"], prefix="/videos")


def validate_thumbnail_upload(file: UploadFile) -> None:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Thumbnail filename is required")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Only image thumbnails are supported")


def validate_upload_session_payload(data: VideoUploadSessionCreate) -> None:
    if not data.original_filename.strip():
        raise HTTPException(status_code=400, detail="Filename is required")
    if not data.content_type or not data.content_type.startswith("video/"):
        raise HTTPException(status_code=415, detail="Only video uploads are supported")
    if data.total_size_bytes > settings.MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Upload is too large")
    if data.chunk_size_bytes and data.chunk_size_bytes > settings.MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Chunk size is too large")


def create_video_record(
    *,
    title: str,
    description: str | None,
    original_filename: str,
    content_type: str,
    size_bytes: int,
    storage_backend: str,
    storage_key: str,
    current_user: CurrentUser,
    category_id: int | None = None,
    thumbnail_storage_key: str | None = None,
    thumbnail_content_type: str | None = None,
    thumbnail_size_bytes: int | None = None,
) -> Video:
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")

    return Video(
        title=clean_title,
        description=description.strip() if description else None,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        author_id=current_user.id,
        author_username=current_user.username,
        author_first_name=current_user.first_name,
        author_last_name=current_user.last_name,
        category_id=category_id,
        storage_backend=storage_backend,
        storage_key=storage_key,
        thumbnail_storage_key=thumbnail_storage_key,
        thumbnail_content_type=thumbnail_content_type,
        thumbnail_size_bytes=thumbnail_size_bytes,
        status="processing",
    )


def to_video_public(
    video: Video,
    variants: list[VideoVariant] | None = None,
    category: Category | None = None,
) -> VideoPublic:
    video_public = VideoPublic.model_validate(video)
    if category is not None and category.id is not None:
        video_public.category = CategorySummary(
            id=category.id,
            name=category.name,
            slug=category.slug,
            parent_id=category.parent_id,
        )
    video_public.qualities = [
        VideoQualityPublic(quality=variant.quality, label=f"{variant.quality}p", size_bytes=variant.size_bytes)
        for variant in sorted(variants or [], key=lambda item: item.quality)
    ]
    return video_public


def get_video_variants(session: Session, video_id: str) -> list[VideoVariant]:
    return session.exec(select(VideoVariant).where(VideoVariant.video_id == video_id).order_by(col(VideoVariant.quality))).all()


def get_categories_by_ids(session: Session, category_ids: list[int]) -> dict[int, Category]:
    if not category_ids:
        return {}
    categories = session.exec(select(Category).where(col(Category.id).in_(category_ids))).all()
    return {category.id: category for category in categories if category.id is not None}


def hls_media_type(storage_key: str) -> str:
    if storage_key.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if storage_key.endswith(".ts"):
        return "video/mp2t"
    return "application/octet-stream"


def safe_hls_asset_key(video: Video, asset_path: str) -> str:
    if not video.hls_storage_prefix:
        raise HTTPException(status_code=404, detail="HLS stream not found")

    path = PurePosixPath(asset_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise HTTPException(status_code=400, detail="Invalid HLS asset path")

    return f"{video.hls_storage_prefix}/{path.as_posix()}"


def mark_stale_processing_videos(session: Session, videos: list[Video]) -> None:
    now = utc_now()
    updated = False
    for video in videos:
        if video.status != "processing":
            continue
        comparison_now = now if video.created_at.tzinfo else now.replace(tzinfo=None)
        if video.created_at + timedelta(seconds=settings.VIDEO_PROCESSING_STALE_AFTER_SECONDS) > comparison_now:
            continue

        video.status = "failed"
        video.processing_error = "Video processing timed out"
        session.add(video)
        updated = True

    if updated:
        session.commit()
        for video in videos:
            if video.id:
                session.refresh(video)


def get_upload_session(session: Session, upload_id: str) -> VideoUploadSession:
    upload_session = session.get(VideoUploadSession, upload_id)
    if not upload_session:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return upload_session


def ensure_upload_session_access(upload_session: VideoUploadSession, current_user: CurrentUser) -> None:
    if upload_session.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can access this upload")


def upload_session_path(upload_id: str) -> PurePosixPath:
    return PurePosixPath(upload_id)


def upload_session_root(upload_id: str):
    path = settings.resumable_upload_path / upload_session_path(upload_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def upload_chunk_path(upload_id: str, chunk_index: int):
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="Chunk index must be non-negative")
    return upload_session_root(upload_id) / f"{chunk_index:08d}.part"


def list_received_chunks(upload_session: VideoUploadSession) -> list[int]:
    root = settings.resumable_upload_path / upload_session_path(upload_session.id)
    if not root.exists():
        return []

    chunks = []
    for chunk_path in sorted(root.glob("*.part")):
        try:
            chunks.append(int(chunk_path.stem))
        except ValueError:
            continue
    return chunks


def upload_session_progress(upload_session: VideoUploadSession) -> tuple[list[int], int, int, bool]:
    chunks = list_received_chunks(upload_session)
    received_size = sum(upload_chunk_path(upload_session.id, chunk_index).stat().st_size for chunk_index in chunks)
    total_chunks = math.ceil(upload_session.total_size_bytes / upload_session.chunk_size_bytes)
    is_complete = len(chunks) == total_chunks and received_size == upload_session.total_size_bytes
    return chunks, received_size, total_chunks, is_complete


def to_upload_session_public(upload_session: VideoUploadSession) -> VideoUploadSessionPublic:
    received_chunks, received_size_bytes, total_chunks, is_complete = upload_session_progress(upload_session)
    public_session = VideoUploadSessionPublic.model_validate(upload_session)
    public_session.received_chunks = received_chunks
    public_session.received_size_bytes = received_size_bytes
    public_session.total_chunks = total_chunks
    public_session.is_complete = is_complete
    return public_session


def cleanup_upload_session_files(upload_id: str) -> None:
    path = settings.resumable_upload_path / upload_session_path(upload_id)
    if path.exists():
        shutil.rmtree(path)


def expected_chunk_size(upload_session: VideoUploadSession, chunk_index: int) -> int:
    total_chunks = math.ceil(upload_session.total_size_bytes / upload_session.chunk_size_bytes)
    if chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Chunk index is out of range")
    if chunk_index == total_chunks - 1:
        remainder = upload_session.total_size_bytes % upload_session.chunk_size_bytes
        return remainder or upload_session.chunk_size_bytes
    return upload_session.chunk_size_bytes


@router.post("/uploads/", response_model=VideoUploadSessionPublic, status_code=201)
async def create_upload_session(
    request: Request,
    data: Annotated[VideoUploadSessionCreate, Body()],
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> VideoUploadSessionPublic:
    await enforce_rate_limit("upload_video_ip", client_identifier(request), 20, 3600)
    await enforce_rate_limit("upload_video_user", f"user:{current_user.id}", 5, 3600)
    validate_upload_session_payload(data)

    chunk_size_bytes = min(
        data.chunk_size_bytes or settings.VIDEO_UPLOAD_CHUNK_SIZE_BYTES,
        settings.VIDEO_UPLOAD_CHUNK_SIZE_BYTES,
    )
    upload_session = VideoUploadSession(
        title=data.title.strip(),
        description=data.description.strip() if data.description else None,
        original_filename=data.original_filename.strip(),
        content_type=data.content_type,
        total_size_bytes=data.total_size_bytes,
        chunk_size_bytes=chunk_size_bytes,
        author_id=current_user.id,
        author_username=current_user.username,
        author_first_name=current_user.first_name,
        author_last_name=current_user.last_name,
        category_id=data.category_id,
    )
    if not upload_session.title:
        raise HTTPException(status_code=400, detail="Title is required")
    if data.category_id is not None:
        get_category_or_404(session, data.category_id)

    session.add(upload_session)
    session.commit()
    session.refresh(upload_session)
    upload_session_root(upload_session.id)
    return to_upload_session_public(upload_session)


@router.get("/uploads/{upload_id}", response_model=VideoUploadSessionPublic)
async def get_upload_session_status(
    upload_id: str,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> VideoUploadSessionPublic:
    upload_session = get_upload_session(session, upload_id)
    ensure_upload_session_access(upload_session, current_user)
    return to_upload_session_public(upload_session)


@router.put("/uploads/{upload_id}/chunks/{chunk_index}", response_model=VideoUploadSessionPublic)
async def upload_video_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> VideoUploadSessionPublic:
    upload_session = get_upload_session(session, upload_id)
    ensure_upload_session_access(upload_session, current_user)
    if upload_session.status != "uploading":
        raise HTTPException(status_code=409, detail="Upload session is not accepting new chunks")

    chunk_bytes = await request.body()
    if not chunk_bytes:
        raise HTTPException(status_code=400, detail="Chunk payload is required")

    required_chunk_size = expected_chunk_size(upload_session, chunk_index)
    if len(chunk_bytes) != required_chunk_size:
        raise HTTPException(status_code=400, detail="Chunk size does not match upload session")

    chunk_path = upload_chunk_path(upload_id, chunk_index)
    chunk_path.write_bytes(chunk_bytes)
    return to_upload_session_public(upload_session)


@router.post("/uploads/{upload_id}/complete/", response_model=VideoPublic, status_code=201)
async def complete_upload_session(
    upload_id: str,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> VideoPublic:
    upload_session = get_upload_session(session, upload_id)
    ensure_upload_session_access(upload_session, current_user)
    if upload_session.status != "uploading":
        raise HTTPException(status_code=409, detail="Upload session is not ready to complete")

    received_chunks, received_size_bytes, total_chunks, is_complete = upload_session_progress(upload_session)
    if not is_complete:
        raise HTTPException(status_code=409, detail="Upload is incomplete")

    stored_object = None
    assembled_path = None
    upload_session.status = "assembling"
    session.add(upload_session)
    session.commit()

    try:
        with tempfile.NamedTemporaryFile(delete=False) as temporary_file:
            assembled_path = Path(temporary_file.name)

        with assembled_path.open("wb") as assembled_file:
            for chunk_index in range(total_chunks):
                assembled_file.write(upload_chunk_path(upload_id, chunk_index).read_bytes())

        suffix = Path(upload_session.original_filename).suffix or ".mp4"
        stored_object = storage.save_file(assembled_path, suffix, settings.MAX_UPLOAD_SIZE_BYTES)
        video = create_video_record(
            title=upload_session.title,
            description=upload_session.description,
            original_filename=upload_session.original_filename,
            content_type=upload_session.content_type,
            size_bytes=stored_object.size_bytes,
            storage_backend=storage.name,
            storage_key=stored_object.key,
            current_user=current_user,
            category_id=upload_session.category_id,
        )
        video.original_filename = upload_session.original_filename
        video.content_type = upload_session.content_type

        session.add(video)
        session.delete(upload_session)
        session.commit()
        session.refresh(video)
    except Exception:
        session.rollback()
        with Session(session.get_bind()) as retry_session:
            retry_upload_session = retry_session.get(VideoUploadSession, upload_id)
            if retry_upload_session:
                retry_upload_session.status = "uploading"
                retry_session.add(retry_upload_session)
                retry_session.commit()
        if stored_object:
            storage.delete(stored_object.key)
        raise
    finally:
        cleanup_upload_session_files(upload_id)
        if assembled_path is not None:
            assembled_path.unlink(missing_ok=True)

    enqueue_video_processing(video.id)
    categories_by_id = get_categories_by_ids(session, [video.category_id] if video.category_id is not None else [])
    return to_video_public(video, get_video_variants(session, video.id), categories_by_id.get(video.category_id))


@router.delete("/uploads/{upload_id}", status_code=204)
async def delete_upload_session(
    upload_id: str,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    upload_session = get_upload_session(session, upload_id)
    ensure_upload_session_access(upload_session, current_user)
    cleanup_upload_session_files(upload_id)
    session.delete(upload_session)
    session.commit()


@router.put("/{video_id}/thumbnail/", response_model=VideoPublic)
async def set_video_thumbnail(
    video_id: str,
    request: Request,
    thumbnail: Annotated[UploadFile, File()],
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
    current_user: CurrentUser = Depends(get_current_user),
) -> VideoPublic:
    await enforce_rate_limit("upload_thumbnail_user", f"user:{current_user.id}", 20, 3600)
    validate_thumbnail_upload(thumbnail)
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status != "ready":
        raise HTTPException(status_code=409, detail="Video is still processing")
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
    categories_by_id = get_categories_by_ids(session, [video.category_id] if video.category_id is not None else [])
    return to_video_public(video, get_video_variants(session, video.id), categories_by_id.get(video.category_id))


@router.get("/", response_model=VideoList)
async def list_videos(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=30),
    search: str | None = Query(default=None, max_length=255),
    author_id: int | None = Query(default=None, ge=1),
    category_id: int | None = Query(default=None, ge=1),
    include_subcategories: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> VideoList:
    statement = select(Video)
    count_statement = select(func.count()).select_from(Video)
    clean_search = search.strip() if search else ""

    if author_id is not None:
        author_filter = Video.author_id == author_id
        statement = statement.where(author_filter)
        count_statement = count_statement.where(author_filter)

    if category_id is not None:
        all_categories = session.exec(select(Category)).all()
        if not any(category.id == category_id for category in all_categories):
            raise HTTPException(status_code=404, detail="Category not found")
        category_ids = (
            collect_descendant_ids(all_categories, category_id)
            if include_subcategories
            else {category_id}
        )
        category_filter = col(Video.category_id).in_(category_ids)
        statement = statement.where(category_filter)
        count_statement = count_statement.where(category_filter)

    if clean_search:
        search_pattern = f"%{clean_search.lower()}%"
        title_match = func.lower(Video.title).like(search_pattern)
        description_match = func.lower(func.coalesce(Video.description, "")).like(search_pattern)
        filters = or_(title_match, description_match)
        statement = statement.where(or_(title_match, description_match)).order_by(
            case((title_match, 0), else_=1),
            col(Video.created_at).desc(),
        )
        count_statement = count_statement.where(filters)
    else:
        statement = statement.order_by(col(Video.created_at).desc())

    statement = statement.offset(offset).limit(limit)
    videos = session.exec(statement).all()
    mark_stale_processing_videos(session, videos)
    total_count = session.exec(count_statement).one()
    video_ids = [video.id for video in videos]
    category_ids = [video.category_id for video in videos if video.category_id is not None]
    categories_by_id = get_categories_by_ids(session, category_ids)
    variants_by_video_id = {video_id: [] for video_id in video_ids}
    if video_ids:
        variants = session.exec(select(VideoVariant).where(col(VideoVariant.video_id).in_(video_ids))).all()
        for variant in variants:
            variants_by_video_id.setdefault(variant.video_id, []).append(variant)
    return VideoList(
        videos=[
            to_video_public(video, variants_by_video_id.get(video.id, []), categories_by_id.get(video.category_id))
            for video in videos
        ],
        count=total_count,
    )


@router.get("/{video_id}", response_model=VideoPublic)
async def get_video(
    video_id: str,
    session: Session = Depends(get_session),
) -> VideoPublic:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    mark_stale_processing_videos(session, [video])
    categories_by_id = get_categories_by_ids(session, [video.category_id] if video.category_id is not None else [])
    return to_video_public(video, get_video_variants(session, video.id), categories_by_id.get(video.category_id))


@router.get("/{video_id}/download/")
async def download_video(
    video_id: str,
    request: Request,
    quality: int | None = Query(default=None, gt=0),
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> StreamingResponse:
    await enforce_rate_limit("download_video_ip", client_identifier(request), 600, 60)
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    mark_stale_processing_videos(session, [video])
    if video.status != "ready":
        raise HTTPException(status_code=409, detail="Video is still processing")

    variants = get_video_variants(session, video.id)
    selected_variant = None
    if variants:
        if quality:
            selected_variant = next((variant for variant in variants if variant.quality == quality), None)
            if not selected_variant:
                raise HTTPException(status_code=404, detail="Video quality not found")
        else:
            selected_variant = max(variants, key=lambda item: item.quality)

    storage_key = selected_variant.storage_key if selected_variant else video.storage_key
    content_type = selected_variant.content_type if selected_variant else video.content_type
    file_path = storage.path_for(storage_key)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    async def stream_file():
        with file_path.open("rb") as stored_file:
            while chunk := stored_file.read(1024 * 1024):
                yield chunk

    encoded_filename = quote(video.original_filename)
    return StreamingResponse(
        stream_file(),
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/{video_id}/hls/master.m3u8")
async def download_hls_master_playlist(
    video_id: str,
    request: Request,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> StreamingResponse:
    return await download_hls_asset(video_id, "master.m3u8", request, session, storage)


@router.get("/{video_id}/hls/{asset_path:path}")
async def download_hls_asset(
    video_id: str,
    asset_path: str,
    request: Request,
    session: Session = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> StreamingResponse:
    await enforce_rate_limit("download_video_ip", client_identifier(request), 600, 60)
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    mark_stale_processing_videos(session, [video])
    if video.status != "ready":
        raise HTTPException(status_code=409, detail="Video is still processing")

    storage_key = safe_hls_asset_key(video, asset_path)
    file_path = storage.path_for(storage_key)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="HLS asset not found")

    async def stream_file():
        with file_path.open("rb") as stored_file:
            while chunk := stored_file.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        stream_file(),
        media_type=hls_media_type(storage_key),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
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
    mark_stale_processing_videos(session, [video])
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
) -> VideoPublic:
    video = session.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    mark_stale_processing_videos(session, [video])
    if video.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can change this video")
    if video.status != "ready":
        raise HTTPException(status_code=409, detail="Video is still processing")

    clean_title = data.title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")

    video.title = clean_title
    video.description = data.description.strip() if data.description else None
    if "category_id" in data.model_fields_set:
        if data.category_id is None:
            video.category_id = None
        else:
            get_category_or_404(session, data.category_id)
            video.category_id = data.category_id
    session.add(video)
    session.commit()
    session.refresh(video)
    categories_by_id = get_categories_by_ids(session, [video.category_id] if video.category_id is not None else [])
    return to_video_public(video, get_video_variants(session, video.id), categories_by_id.get(video.category_id))


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
    mark_stale_processing_videos(session, [video])
    if video.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can delete this video")

    variants = get_video_variants(session, video.id)
    deleted_keys = set()
    storage.delete(video.storage_key)
    deleted_keys.add(video.storage_key)
    for variant in variants:
        if variant.storage_key not in deleted_keys:
            storage.delete(variant.storage_key)
            deleted_keys.add(variant.storage_key)
        session.delete(variant)
    if video.thumbnail_storage_key:
        storage.delete(video.thumbnail_storage_key)
    if video.hls_storage_prefix:
        storage.delete_prefix(video.hls_storage_prefix)
    session.delete(video)
    session.commit()
    return VideoDeleteResponse(message="Video deleted successfully")
