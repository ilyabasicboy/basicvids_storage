import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from basicvids_storage.settings import Settings
from basicvids_storage.storage.base import StorageBackend, StoredObject
from basicvids_storage.thumbnails import _run_command


@dataclass(frozen=True)
class TranscodedVideoVariant:
    quality: int
    stored_object: StoredObject
    content_type: str


@dataclass(frozen=True)
class GeneratedHlsVideo:
    storage_prefix: str
    manifest_stored_object: StoredObject
    stored_objects: list[StoredObject]


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


async def _generate_hls_variant(input_path: Path, output_directory: Path, quality: int, settings: Settings) -> bool:
    output_directory.mkdir(parents=True, exist_ok=True)
    playlist_path = output_directory / "playlist.m3u8"
    segment_pattern = output_directory / "segment-%05d.ts"

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
        "-f",
        "hls",
        "-hls_time",
        "6",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(segment_pattern),
        str(playlist_path),
        timeout=settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS,
    )
    return returncode == 0 and playlist_path.exists() and any(output_directory.glob("segment-*.ts"))


def _write_hls_master_playlist(output_path: Path, qualities: list[int]) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for quality in qualities:
        # Conservative fixed bandwidths are enough for client adaptation hints.
        bandwidth = max(300_000, quality * 1_800)
        width = int(quality * 16 / 9)
        width += width % 2
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{quality}")
        lines.append(f"{quality}p/playlist.m3u8")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


async def generate_hls_video(
    video_path: Path,
    storage: StorageBackend,
    settings: Settings,
) -> GeneratedHlsVideo | None:
    source_height = await _probe_height(video_path, settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS)
    qualities = _target_qualities(source_height, settings)

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        hls_path = temporary_path / "hls"
        generated_qualities = []

        for quality in qualities:
            if await _generate_hls_variant(video_path, hls_path / f"{quality}p", quality, settings):
                generated_qualities.append(quality)

        if not generated_qualities:
            return None

        _write_hls_master_playlist(hls_path / "master.m3u8", generated_qualities)

        storage_prefix = f"hls/{uuid4().hex}"
        stored_objects = []
        try:
            for asset_path in sorted(path for path in hls_path.rglob("*") if path.is_file()):
                relative_path = asset_path.relative_to(hls_path).as_posix()
                stored_objects.append(
                    storage.save_file_as(
                        asset_path,
                        f"{storage_prefix}/{relative_path}",
                        settings.MAX_UPLOAD_SIZE_BYTES,
                    )
                )
        except Exception:
            storage.delete_prefix(storage_prefix)
            raise

        manifest_key = f"{storage_prefix}/master.m3u8"
        manifest_stored_object = next((item for item in stored_objects if item.key == manifest_key), None)
        if not manifest_stored_object:
            return None

        return GeneratedHlsVideo(
            storage_prefix=storage_prefix,
            manifest_stored_object=manifest_stored_object,
            stored_objects=stored_objects,
        )
