from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile


@dataclass(frozen=True)
class DiskStoredObject:
    key: str
    size_bytes: int


class DiskStorage:
    name = "disk"

    def __init__(self, root_path: Path):
        self.root_path = Path(root_path)

    async def save_upload(self, upload: UploadFile, max_size_bytes: int) -> DiskStoredObject:
        self.root_path.mkdir(parents=True, exist_ok=True)

        suffix = Path(upload.filename or "").suffix.lower()
        key = f"{uuid4().hex}{suffix}"
        final_path = self.path_for(key)
        temporary_path = final_path.with_suffix(f"{final_path.suffix}.part")

        size_bytes = 0
        with temporary_path.open("wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > max_size_bytes:
                    destination.close()
                    temporary_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Upload is too large")
                destination.write(chunk)

        temporary_path.replace(final_path)
        return DiskStoredObject(key=key, size_bytes=size_bytes)

    def path_for(self, key: str) -> Path:
        candidate = (self.root_path / key).resolve()
        root = self.root_path.resolve()
        if root not in candidate.parents and candidate != root:
            raise HTTPException(status_code=400, detail="Invalid storage key")
        return candidate

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)
