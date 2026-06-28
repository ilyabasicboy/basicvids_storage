import asyncio
import contextlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from basicvids_storage.settings import Settings
from basicvids_storage.storage.base import StorageBackend, StoredObject


@dataclass(frozen=True)
class GeneratedThumbnail:
    stored_object: StoredObject
    content_type: str


async def _run_command(*args: str, timeout: int) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        return 127, "", str(error)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        return 124, "", "Command timed out"

    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def probe_video_stream(video_path: Path, timeout: int) -> bool:
    returncode, stdout, _stderr = await _run_command(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        timeout=timeout,
    )
    return returncode == 0 and bool(stdout.strip())


async def probe_video_duration(video_path: Path, timeout: int) -> float | None:
    returncode, stdout, _stderr = await _run_command(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        timeout=timeout,
    )
    if returncode != 0:
        return None

    try:
        duration = float(stdout)
    except ValueError:
        return None

    return duration if duration > 0 else None


async def _extract_frame(video_path: Path, output_path: Path, seek_seconds: float, settings: Settings) -> bool:
    returncode, _stdout, _stderr = await _run_command(
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({settings.THUMBNAIL_WIDTH},iw)':-2",
        "-q:v",
        str(settings.THUMBNAIL_JPEG_QUALITY),
        str(output_path),
        timeout=settings.THUMBNAIL_GENERATION_TIMEOUT_SECONDS,
    )
    return returncode == 0 and output_path.exists() and output_path.stat().st_size > 0


async def generate_video_thumbnail(
    video_path: Path,
    storage: StorageBackend,
    settings: Settings,
) -> GeneratedThumbnail | None:
    duration = await probe_video_duration(video_path, settings.THUMBNAIL_GENERATION_TIMEOUT_SECONDS)
    seek_points = []
    if duration:
        seek_points.append(max(duration / 2, 0))
    seek_points.extend([1.0, 0.0])

    unique_seek_points = []
    for seek_point in seek_points:
        if seek_point not in unique_seek_points:
            unique_seek_points.append(seek_point)

    with tempfile.TemporaryDirectory() as temporary_directory:
        output_path = Path(temporary_directory) / "thumbnail.jpg"
        for seek_point in unique_seek_points:
            output_path.unlink(missing_ok=True)
            if await _extract_frame(video_path, output_path, seek_point, settings):
                stored_object = storage.save_file(output_path, ".jpg", settings.MAX_THUMBNAIL_SIZE_BYTES)
                return GeneratedThumbnail(stored_object=stored_object, content_type="image/jpeg")

    return None
