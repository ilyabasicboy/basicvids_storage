from pydantic import BaseModel, EmailStr
from fastapi import HTTPException, Request
import httpx

from basicvids_storage.settings import settings


class CurrentUser(BaseModel):
    id: int
    username: str
    first_name: str | None = None
    last_name: str | None = None
    email: EmailStr
    is_admin: bool = False


async def get_current_user(request: Request) -> CurrentUser:
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(
                settings.AUTH_CURRENT_USER_URL,
                headers={"Authorization": auth_header},
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="Auth service is unavailable")

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="User is not authenticated")
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail="Auth service rejected user")

    return CurrentUser.model_validate(response.json())
