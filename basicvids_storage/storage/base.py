from pathlib import Path
from typing import Protocol

from fastapi import UploadFile


class StoredObject(Protocol):
    key: str
    size_bytes: int


class StorageBackend(Protocol):
    name: str

    async def save_upload(self, upload: UploadFile, max_size_bytes: int) -> StoredObject:
        ...

    def save_file(self, source_path: Path, suffix: str, max_size_bytes: int) -> StoredObject:
        ...

    def path_for(self, key: str) -> Path:
        ...

    def delete(self, key: str) -> None:
        ...
