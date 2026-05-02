from pathlib import Path

from sqlmodel import Session, delete
import httpx
import pytest

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.models.avatars import Avatar, AvatarPublic
from basicvids_storage.tests import app, engine, temporary_directory


pytestmark = pytest.mark.anyio


async def request(method: str, url: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


def user(user_id: int = 1, is_admin: bool = False) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        username=f"user-{user_id}",
        first_name="Test",
        last_name="Author",
        email=f"user-{user_id}@example.com",
        is_admin=is_admin,
    )


def set_current_user(current_user: CurrentUser) -> None:
    async def override_get_current_user():
        return current_user

    app.dependency_overrides[get_current_user] = override_get_current_user


class BaseTestAvatars:
    def setup_method(self):
        set_current_user(user())
        with Session(engine) as session:
            session.exec(delete(Avatar))
            session.commit()
        for path in Path(temporary_directory.name).iterdir():
            if path.is_file():
                path.unlink()


class TestAvatars(BaseTestAvatars):
    async def test_create_registration_avatar_success(self):
        response = await request(
            "POST",
            "/api/v1/avatars/users/10/registration/",
            files={"avatar": ("avatar.png", b"fake-avatar-bytes", "image/png")},
        )

        assert response.status_code == 201
        response_data = response.json()
        assert AvatarPublic(**response_data)
        assert response_data["user_id"] == 10
        assert response_data["content_type"] == "image/png"
        assert response_data["size_bytes"] == len(b"fake-avatar-bytes")

        response = await request("GET", "/api/v1/avatars/users/10/image/")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == b"fake-avatar-bytes"

    async def test_create_registration_avatar_rejects_overwrite(self):
        await request(
            "POST",
            "/api/v1/avatars/users/10/registration/",
            files={"avatar": ("avatar.png", b"fake-avatar-bytes", "image/png")},
        )

        response = await request(
            "POST",
            "/api/v1/avatars/users/10/registration/",
            files={"avatar": ("avatar.png", b"new-avatar-bytes", "image/png")},
        )

        assert response.status_code == 409

    async def test_create_registration_avatar_rejects_non_image(self):
        response = await request(
            "POST",
            "/api/v1/avatars/users/10/registration/",
            files={"avatar": ("avatar.txt", b"text", "text/plain")},
        )

        assert response.status_code == 415

    async def test_set_current_user_avatar_replaces_existing(self):
        response = await request(
            "PUT",
            "/api/v1/avatars/me/",
            files={"avatar": ("avatar.png", b"fake-avatar-bytes", "image/png")},
        )

        assert response.status_code == 200
        assert response.json()["user_id"] == 1
        assert len(list(Path(temporary_directory.name).iterdir())) == 1

        response = await request(
            "PUT",
            "/api/v1/avatars/me/",
            files={"avatar": ("avatar.jpg", b"new-avatar-bytes", "image/jpeg")},
        )

        assert response.status_code == 200
        assert response.json()["content_type"] == "image/jpeg"
        assert len(list(Path(temporary_directory.name).iterdir())) == 1

        response = await request("GET", "/api/v1/avatars/users/1/image/")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == b"new-avatar-bytes"

    async def test_delete_current_user_avatar_success(self):
        await request(
            "PUT",
            "/api/v1/avatars/me/",
            files={"avatar": ("avatar.png", b"fake-avatar-bytes", "image/png")},
        )

        response = await request("DELETE", "/api/v1/avatars/me/")

        assert response.status_code == 200
        assert response.json() == {"message": "Avatar deleted successfully"}
        assert list(Path(temporary_directory.name).iterdir()) == []

        response = await request("GET", "/api/v1/avatars/users/1/image/")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert "<svg" in response.text

    async def test_missing_avatar_returns_placeholder_image(self):
        response = await request("GET", "/api/v1/avatars/users/999/image/")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert "<svg" in response.text
