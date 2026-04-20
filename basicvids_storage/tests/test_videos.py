from pathlib import Path

from sqlmodel import Session, delete
import httpx
import pytest

from basicvids_storage.routers import videos as videos_router
from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.models.videos import Video, VideoPublic
from basicvids_storage.thumbnails import GeneratedThumbnail
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
            data={
                "title": "Test clip",
                "description": "A test upload",
            },
            files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
        )

        assert response.status_code == 201
        response_data = response.json()
        assert VideoPublic(**response_data)
        assert response_data["original_filename"] == "clip.mp4"
        assert response_data["content_type"] == "video/mp4"
        assert response_data["size_bytes"] == len(b"fake-video-bytes")
        assert response_data["author_id"] == 1
        assert response_data["author_first_name"] == "Test"
        assert response_data["author_last_name"] == "Author"
        assert response_data["author_username"] == "user-1"
        assert response_data["title"] == "Test clip"
        assert response_data["description"] == "A test upload"
        assert response_data["has_thumbnail"] is False

    async def test_upload_video_with_thumbnail_success(self):
        response = await request(
            "POST",
            self.method_url,
            data={
                "title": "Test clip",
                "description": "A test upload",
            },
            files={
                "file": ("clip.mp4", b"fake-video-bytes", "video/mp4"),
                "thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg"),
            },
        )

        assert response.status_code == 201
        response_data = response.json()
        assert response_data["has_thumbnail"] is True

    async def test_upload_video_generates_thumbnail_when_missing(self, monkeypatch, tmp_path):
        async def generate_thumbnail(video_path, storage, settings):
            assert video_path.exists()
            source_path = tmp_path / "generated.jpg"
            source_path.write_bytes(b"generated-image-bytes")
            return GeneratedThumbnail(
                stored_object=storage.save_file(source_path, ".jpg", settings.MAX_THUMBNAIL_SIZE_BYTES),
                content_type="image/jpeg",
            )

        monkeypatch.setattr(videos_router, "generate_video_thumbnail", generate_thumbnail)

        response = await request(
            "POST",
            self.method_url,
            data={
                "title": "Test clip",
                "description": "A test upload",
            },
            files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
        )

        assert response.status_code == 201
        response_data = response.json()
        assert response_data["has_thumbnail"] is True

        response = await request("GET", f"/api/v1/videos/{response_data['id']}/thumbnail/")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == b"generated-image-bytes"

    async def test_upload_video_rejects_non_image_thumbnail(self):
        response = await request(
            "POST",
            self.method_url,
            data={"title": "Test clip"},
            files={
                "file": ("clip.mp4", b"fake-video-bytes", "video/mp4"),
                "thumbnail": ("notes.txt", b"text", "text/plain"),
            },
        )

        assert response.status_code == 415

    async def test_upload_video_unauthorized(self):
        app.dependency_overrides.pop(get_current_user, None)
        response = await request(
            "POST",
            self.method_url,
            data={"title": "Test clip"},
            files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
        )

        assert response.status_code == 401

    async def test_upload_rejects_non_video(self):
        response = await request(
            "POST",
            self.method_url,
            data={"title": "Notes"},
            files={"file": ("notes.txt", b"text", "text/plain")},
        )

        assert response.status_code == 415


class TestVideosRead(BaseTestVideos):
    method_url = "/api/v1/videos"

    async def create_video(self, title: str = "Test clip", description: str = "A test upload"):
        response = await request(
            "POST",
            f"{self.method_url}/upload/",
            data={
                "title": title,
                "description": description,
            },
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

    async def test_list_videos_paginates_with_maximum_page_size(self):
        with Session(engine) as session:
            for index in range(31):
                session.add(
                    Video(
                        title=f"Video {index}",
                        description=None,
                        original_filename=f"clip-{index}.mp4",
                        content_type="video/mp4",
                        size_bytes=10,
                        author_id=1,
                        author_username="user-1",
                        storage_backend="disk",
                        storage_key=f"video-{index}",
                    )
                )
            session.commit()

        response = await request("GET", f"{self.method_url}/")

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 31
        assert len(response_data["videos"]) == 30

        response = await request("GET", f"{self.method_url}/", params={"offset": 30, "limit": 30})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 31
        assert len(response_data["videos"]) == 1

    async def test_list_videos_rejects_page_size_over_30(self):
        response = await request("GET", f"{self.method_url}/", params={"limit": 31})

        assert response.status_code == 422

    async def test_list_videos_searches_title_and_description_with_title_priority(self):
        await self.create_video(title="Cooking basics", description="Intro lesson")
        description_match = await self.create_video(title="Travel diary", description="Cooking on a mountain")
        title_match = await self.create_video(title="Mountain cooking", description="Camp stove guide")

        response = await request("GET", f"{self.method_url}/", params={"search": "cooking"})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 3
        assert response_data["videos"][0]["id"] == title_match["id"]
        assert response_data["videos"][1]["title"] == "Cooking basics"
        assert response_data["videos"][2]["id"] == description_match["id"]

    async def test_list_videos_search_returns_no_matches(self):
        await self.create_video(title="Cooking basics", description="Intro lesson")

        response = await request("GET", f"{self.method_url}/", params={"search": "missing"})

        assert response.status_code == 200
        assert response.json() == {"videos": [], "count": 0}

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

    async def test_download_video_thumbnail_success(self):
        response = await request(
            "POST",
            f"{self.method_url}/upload/",
            data={"title": "Test clip"},
            files={
                "file": ("clip.mp4", b"fake-video-bytes", "video/mp4"),
                "thumbnail": ("thumb.png", b"fake-image-bytes", "image/png"),
            },
        )
        video = response.json()

        response = await request("GET", f"{self.method_url}/{video['id']}/thumbnail/")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == b"fake-image-bytes"

    async def test_download_video_thumbnail_not_found(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}/thumbnail/")

        assert response.status_code == 404

    async def test_set_video_thumbnail_success(self):
        video = await self.create_video()
        stored_files = list(Path(temporary_directory.name).iterdir())
        assert len(stored_files) == 1

        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
        )

        assert response.status_code == 200
        assert response.json()["has_thumbnail"] is True
        assert len(list(Path(temporary_directory.name).iterdir())) == 2

        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("new-thumb.jpg", b"new-image-bytes", "image/jpeg")},
        )

        assert response.status_code == 200
        assert len(list(Path(temporary_directory.name).iterdir())) == 2

    async def test_set_video_thumbnail_forbidden_for_non_author(self):
        video = await self.create_video()
        set_current_user(user(user_id=2))

        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
        )

        assert response.status_code == 403

    async def test_change_video_success(self):
        video = await self.create_video()
        response = await request(
            "PATCH",
            f"{self.method_url}/{video['id']}",
            json={
                "title": "Changed title",
                "description": "Changed description",
            },
        )

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["title"] == "Changed title"
        assert response_data["description"] == "Changed description"

    async def test_change_video_forbidden_for_non_author(self):
        video = await self.create_video()
        set_current_user(user(user_id=2))

        response = await request(
            "PATCH",
            f"{self.method_url}/{video['id']}",
            json={
                "title": "Changed title",
                "description": "Changed description",
            },
        )

        assert response.status_code == 403

    async def test_delete_video_success(self):
        response = await request(
            "POST",
            f"{self.method_url}/upload/",
            data={"title": "Test clip"},
            files={
                "file": ("clip.mp4", b"fake-video-bytes", "video/mp4"),
                "thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg"),
            },
        )
        video = response.json()
        stored_files = list(Path(temporary_directory.name).iterdir())
        assert len(stored_files) == 2

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
