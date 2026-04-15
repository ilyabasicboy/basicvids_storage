from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from basicvids_storage.settings import settings


engine = create_engine(settings.DATABASE_URL)


def create_db_and_tables():
    settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
    settings.video_storage_path.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    migrate_video_author_id()


def migrate_video_author_id():
    inspector = inspect(engine)
    if "video" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("video")}
    if "author_id" in columns:
        return

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE video ADD COLUMN author_id INTEGER"))


async def get_session():
    with Session(engine) as session:
        yield session
