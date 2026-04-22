import asyncio

from sqlmodel import Session
from sqlmodel import select

from basicvids_storage.celery_app import celery_app
from basicvids_storage.db import engine
from basicvids_storage.models.videos import Video, VideoVariant
from basicvids_storage.settings import settings
from basicvids_storage.storage import build_storage
from basicvids_storage.thumbnails import generate_video_thumbnail, probe_video_duration
from basicvids_storage.transcoding import generate_transcoded_video_variants


async def process_video_async(video_id: str) -> None:
    storage = build_storage()

    with Session(engine) as session:
        video = session.get(Video, video_id)
        if not video or video.status != "processing":
            return

        source_key = video.storage_key
        source_path = storage.path_for(source_key)
        if not source_path.exists():
            video.status = "failed"
            video.processing_error = "Uploaded source file not found"
            session.add(video)
            session.commit()
            return

    stored_variant_keys = []
    stored_thumbnail_key = None
    try:
        duration_seconds = await probe_video_duration(source_path, settings.THUMBNAIL_GENERATION_TIMEOUT_SECONDS)
        transcoded_variants = await generate_transcoded_video_variants(source_path, storage, settings)
        if not transcoded_variants:
            raise RuntimeError("Video transcoding failed")

        stored_variant_keys = [variant.stored_object.key for variant in transcoded_variants]
        with Session(engine) as session:
            current_video = session.get(Video, video_id)
            should_generate_thumbnail = bool(current_video and not current_video.thumbnail_storage_key)

        generated_thumbnail = await generate_video_thumbnail(source_path, storage, settings) if should_generate_thumbnail else None
        stored_thumbnail_key = generated_thumbnail.stored_object.key if generated_thumbnail else None
        primary_variant = max(transcoded_variants, key=lambda item: item.quality)

        with Session(engine) as session:
            video = session.get(Video, video_id)
            if not video:
                for key in stored_variant_keys:
                    storage.delete(key)
                if stored_thumbnail_key:
                    storage.delete(stored_thumbnail_key)
                storage.delete(source_key)
                return

            old_thumbnail_key = video.thumbnail_storage_key
            for variant in session.exec(select(VideoVariant).where(VideoVariant.video_id == video.id)).all():
                session.delete(variant)

            for transcoded_variant in transcoded_variants:
                session.add(
                    VideoVariant(
                        video_id=video.id,
                        quality=transcoded_variant.quality,
                        storage_key=transcoded_variant.stored_object.key,
                        content_type=transcoded_variant.content_type,
                        size_bytes=transcoded_variant.stored_object.size_bytes,
                    )
                )

            video.storage_key = primary_variant.stored_object.key
            video.content_type = primary_variant.content_type
            video.size_bytes = sum(variant.stored_object.size_bytes for variant in transcoded_variants)
            video.duration_seconds = duration_seconds
            video.thumbnail_storage_key = stored_thumbnail_key
            video.thumbnail_content_type = generated_thumbnail.content_type if generated_thumbnail else None
            video.thumbnail_size_bytes = generated_thumbnail.stored_object.size_bytes if generated_thumbnail else None
            video.status = "ready"
            video.processing_error = None
            session.add(video)
            session.commit()

            if old_thumbnail_key and old_thumbnail_key != stored_thumbnail_key:
                storage.delete(old_thumbnail_key)

        storage.delete(source_key)
    except Exception as error:
        for key in stored_variant_keys:
            storage.delete(key)
        if stored_thumbnail_key:
            storage.delete(stored_thumbnail_key)

        with Session(engine) as session:
            video = session.get(Video, video_id)
            if video:
                video.status = "failed"
                video.processing_error = str(error)
                session.add(video)
                session.commit()
        raise


@celery_app.task(name="basicvids_storage.tasks.process_video")
def process_video(video_id: str) -> None:
    asyncio.run(process_video_async(video_id))


def enqueue_video_processing(video_id: str) -> None:
    process_video.delay(video_id)
