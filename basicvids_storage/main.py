from contextlib import asynccontextmanager

from fastapi import FastAPI

from basicvids_storage.db import create_db_and_tables
from basicvids_storage.routers.root import router as root_router
from basicvids_storage.routers.videos import router as videos_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="BasicVids Storage", lifespan=lifespan)

app.include_router(videos_router, prefix="/api/v1")
app.include_router(root_router)
