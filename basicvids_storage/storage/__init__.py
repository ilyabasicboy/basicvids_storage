from basicvids_storage.settings import settings
from basicvids_storage.storage.disk import DiskStorage


async def get_storage() -> DiskStorage:
    if settings.STORAGE_BACKEND != "disk":
        raise ValueError(f"Unsupported storage backend: {settings.STORAGE_BACKEND}")
    return DiskStorage(settings.video_storage_path)
