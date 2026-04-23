from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from basicvids_storage.settings import settings


engine = create_engine(settings.DATABASE_URL)


def create_db_and_tables():
    settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
    settings.video_storage_path.mkdir(parents=True, exist_ok=True)
    settings.resumable_upload_path.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    migrate_video_columns()
    migrate_video_variant_table()
    migrate_video_upload_session_table()


def migrate_video_columns():
    inspector = inspect(engine)
    if "video" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("video")}
    with engine.begin() as connection:
        if "author_id" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN author_id INTEGER"))
        if "title" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN title VARCHAR(255) DEFAULT '' NOT NULL"))
        if "description" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN description VARCHAR(2000)"))
        if "author_username" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN author_username VARCHAR(100)"))
        if "author_first_name" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN author_first_name VARCHAR(100)"))
        if "author_last_name" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN author_last_name VARCHAR(100)"))
        if "thumbnail_storage_key" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN thumbnail_storage_key VARCHAR(500)"))
        if "thumbnail_content_type" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN thumbnail_content_type VARCHAR(100)"))
        if "thumbnail_size_bytes" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN thumbnail_size_bytes INTEGER"))
        if "duration_seconds" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN duration_seconds FLOAT"))
        if "hls_storage_prefix" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN hls_storage_prefix VARCHAR(500)"))
        if "hls_manifest_storage_key" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN hls_manifest_storage_key VARCHAR(500)"))
        if "status" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN status VARCHAR(20) DEFAULT 'ready' NOT NULL"))
        if "processing_error" not in columns:
            connection.execute(text("ALTER TABLE video ADD COLUMN processing_error VARCHAR(2000)"))


def migrate_video_variant_table():
    inspector = inspect(engine)
    if "video_variant" in inspector.get_table_names():
        return

    video_variant_table = SQLModel.metadata.tables.get("video_variant")
    if video_variant_table is None:
        return

    with engine.begin() as connection:
        video_variant_table.create(connection, checkfirst=True)


def migrate_video_upload_session_table():
    inspector = inspect(engine)
    if "videouploadsession" in inspector.get_table_names():
        return

    upload_session_table = SQLModel.metadata.tables.get("videouploadsession")
    if upload_session_table is None:
        return

    with engine.begin() as connection:
        upload_session_table.create(connection, checkfirst=True)


async def get_session():
    with Session(engine) as session:
        yield session
