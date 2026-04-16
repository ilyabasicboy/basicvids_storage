from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from basicvids_storage.settings import settings


engine = create_engine(settings.DATABASE_URL)


def create_db_and_tables():
    settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
    settings.video_storage_path.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    migrate_video_columns()


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


async def get_session():
    with Session(engine) as session:
        yield session
