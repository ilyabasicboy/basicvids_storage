from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    STORAGE_BACKEND: str = "disk"
    DATA_PATH: Path = Path("./data")
    DATABASE_URL: str = "sqlite:///./data/database.db"
    VIDEO_STORAGE_DIR: str = "videos"
    MAX_UPLOAD_SIZE_BYTES: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    AUTH_CURRENT_USER_URL: str = "http://basicvids_auth:8000/api/v1/users/detail/"

    model_config = SettingsConfigDict(
        env_file="./data/.env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @property
    def video_storage_path(self) -> Path:
        return self.DATA_PATH / self.VIDEO_STORAGE_DIR


settings = Settings()
