from sqlmodel import Session, delete
import httpx
import pytest

from basicvids_storage.models.videos import Video, VideoPublic
from basicvids_storage.tests import app, engine


pytestmark = pytest.mark.anyio


async def request(method: str, url: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


class BaseTestVideos:
    def setup_method(self):
        with Session(engine) as session:
            session.exec(delete(Video))
            session.commit()


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
        response = await request("DELETE", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200

        response = await request("GET", f"{self.method_url}/{video['id']}")
        assert response.status_code == 404
