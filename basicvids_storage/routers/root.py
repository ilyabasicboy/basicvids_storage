from fastapi import APIRouter

router = APIRouter(tags=["Root"])


@router.get("/health")
async def health():
    return {"status": "ok"}
