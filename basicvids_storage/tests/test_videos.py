from pathlib import Path

from sqlmodel import Session, delete
import httpx
import pytest

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.models.videos import Video, VideoPublic
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
        email=f"user-{user_id}@example.com",
        is_admin=is_admin,
    )


def set_current_user(current_user: CurrentUser) -> None:
    async def override_get_current_user():
        return current_user

    app.dependency_overrides[get_current_user] = override_get_current_user


class BaseTestVideos:
    def setup_method(self):
        set_current_user(user())
        with Session(engine) as session:
            session.exec(delete(Video))
            session.commit()
        for path in Path(temporary_directory.name).iterdir():
            if path.is_file():
                path.unlink()


class TestVideosUpload(BaseTestVideos):
    method_url = "/api/v1/videos/upload/"

    async def test_upload_video_success(self):
        response = await request(
            "POST",
            self.method_url,
            files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
        )

        assert response.status_code == 201
        response_data = response.json()
        assert VideoPublic(**response_data)
        assert response_data["original_filename"] == "clip.mp4"
        assert response_data["content_type"] == "video/mp4"
        assert response_data["size_bytes"] == len(b"fake-video-bytes")
        assert response_data["author_id"] == 1

    async def test_upload_video_unauthorized(self):
        app.dependency_overrides.pop(get_current_user, None)
        response = await request(
            "POST",
            self.method_url,
            files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
        )

        assert response.status_code == 401

    async def test_upload_rejects_non_video(self):
        response = await request(
            "POST",
            self.method_url,
            files={"file": ("notes.txt", b"text", "text/plain")},
        )

        assert response.status_code == 415


class TestVideosRead(BaseTestVideos):
    method_url = "/api/v1/videos"

    async def create_video(self):
        response = await request(
            "POST",
            f"{self.method_url}/upload/",
            files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
        )
        return response.json()

    async def test_list_videos_success(self):
        await self.create_video()
        response = await request("GET", f"{self.method_url}/")

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 1
        assert VideoPublic(**response_data["videos"][0])

    async def test_get_video_success(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == video["id"]

    async def test_download_video_success(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}/download/")

        assert response.status_code == 200
        assert response.content == b"fake-video-bytes"

    async def test_delete_video_success(self):
        video = await self.create_video()
        stored_files = list(Path(temporary_directory.name).iterdir())
        assert len(stored_files) == 1

        response = await request("DELETE", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200
        assert response.json() == {"message": "Video deleted successfully"}

        response = await request("GET", f"{self.method_url}/{video['id']}")
        assert response.status_code == 404
        assert list(Path(temporary_directory.name).iterdir()) == []

    async def test_delete_video_forbidden_for_non_author(self):
        video = await self.create_video()
        set_current_user(user(user_id=2))

        response = await request("DELETE", f"{self.method_url}/{video['id']}")

        assert response.status_code == 403

    async def test_delete_video_success_for_admin(self):
        video = await self.create_video()
        set_current_user(user(user_id=2, is_admin=True))

        response = await request("DELETE", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200

    async def test_delete_video_not_found(self):
        response = await request("DELETE", f"{self.method_url}/missing-video")

        assert response.status_code == 404
