from tempfile import TemporaryDirectory

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from basicvids_storage.db import get_session
from basicvids_storage.main import app
from basicvids_storage.storage import get_storage
from basicvids_storage.storage.disk import DiskStorage


TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

SQLModel.metadata.create_all(engine)

temporary_directory = TemporaryDirectory()


async def override_get_session():
    with Session(engine) as session:
        yield session


async def override_get_storage():
    return DiskStorage(root_path=temporary_directory.name)


app.dependency_overrides[get_session] = override_get_session
app.dependency_overrides[get_storage] = override_get_storage
