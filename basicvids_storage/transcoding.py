import tempfile
from dataclasses import dataclass
from pathlib import Path

from basicvids_storage.settings import Settings
from basicvids_storage.storage.base import StorageBackend, StoredObject
from basicvids_storage.thumbnails import _run_command


@dataclass(frozen=True)
class TranscodedVideoVariant:
    quality: int
    stored_object: StoredObject
    content_type: str


async def _probe_height(video_path: Path, timeout: int) -> int | None:
    returncode, stdout, _stderr = await _run_command(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        timeout=timeout,
    )
    if returncode != 0:
        return None

    try:
        height = int(stdout.splitlines()[0])
    except (IndexError, ValueError):
        return None

    return height if height > 0 else None


def _target_qualities(source_height: int | None, settings: Settings) -> list[int]:
    max_height = min(source_height or settings.VIDEO_TRANSCODE_MAX_HEIGHT, settings.VIDEO_TRANSCODE_MAX_HEIGHT)
    configured_qualities = []

    for value in settings.VIDEO_TRANSCODE_QUALITIES.split(","):
        try:
            quality = int(value.strip())
        except ValueError:
            continue
        if 0 < quality <= max_height:
            configured_qualities.append(quality)

    if configured_qualities:
        return sorted(set(configured_qualities))

    return [max_height]


async def _transcode_video(input_path: Path, output_path: Path, quality: int, settings: Settings) -> bool:
    returncode, _stdout, _stderr = await _run_command(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        f"scale=-2:'min({quality},ih)'",
        "-c:v",
        "libx264",
        "-threads",
        str(settings.VIDEO_TRANSCODE_THREADS),
        "-preset",
        "veryfast",
        "-crf",
        str(settings.VIDEO_TRANSCODE_CRF),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
        timeout=settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS,
    )
    return returncode == 0 and output_path.exists() and output_path.stat().st_size > 0


async def generate_transcoded_video_variants(
    video_path: Path,
    storage: StorageBackend,
    settings: Settings,
) -> list[TranscodedVideoVariant]:
    source_height = await _probe_height(video_path, settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS)
    qualities = _target_qualities(source_height, settings)
    variants = []

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        for quality in qualities:
            output_path = temporary_path / f"video-{quality}p.mp4"
            if not await _transcode_video(video_path, output_path, quality, settings):
                continue

            stored_object = storage.save_file(output_path, ".mp4", settings.MAX_UPLOAD_SIZE_BYTES)
            variants.append(
                TranscodedVideoVariant(
                    quality=quality,
                    stored_object=stored_object,
                    content_type="video/mp4",
                )
            )

    return variants
