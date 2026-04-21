from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    STORAGE_BACKEND: str = "disk"
    DATA_PATH: Path = Path("./data")
    DATABASE_URL: str = "sqlite:///./data/database.db"
    VIDEO_STORAGE_DIR: str = "videos"
    MAX_UPLOAD_SIZE_BYTES: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    MAX_THUMBNAIL_SIZE_BYTES: int = Field(default=1 * 1024 * 1024, gt=0)
    MAX_AVATAR_SIZE_BYTES: int = Field(default=512 * 1024, gt=0)
    THUMBNAIL_WIDTH: int = Field(default=320, gt=0)
    THUMBNAIL_JPEG_QUALITY: int = Field(default=5, ge=2, le=31)
    THUMBNAIL_GENERATION_TIMEOUT_SECONDS: int = Field(default=20, gt=0)
    VIDEO_TRANSCODE_MAX_HEIGHT: int = Field(default=1080, gt=0)
    VIDEO_TRANSCODE_QUALITIES: str = "360,480,720,1080"
    VIDEO_TRANSCODE_CRF: int = Field(default=28, ge=18, le=35)
    VIDEO_TRANSCODE_TIMEOUT_SECONDS: int = Field(default=1800, gt=0)
    VIDEO_TRANSCODE_THREADS: int = Field(default=2, ge=1, le=8)
    VIDEO_PROCESSING_STALE_AFTER_SECONDS: int = Field(default=1800, gt=0)
    CELERY_BROKER_URL: str = "redis://basicvids_redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://basicvids_redis:6379/1"
    CELERY_TASK_ALWAYS_EAGER: bool = False
    VIDEO_TRANSCODE_QUEUE: str = "video_transcode"
    VIDEO_TRANSCODE_WORKER_CONCURRENCY: int = Field(default=1, ge=1, le=8)
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
