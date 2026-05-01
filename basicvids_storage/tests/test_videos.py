from datetime import timedelta
from pathlib import Path

from sqlmodel import Session, delete
import httpx
import pytest

from basicvids_storage.routers import videos as videos_router
from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.models.categories import Category
from basicvids_storage.models.videos import Video, VideoPublic, VideoUploadSession, VideoVariant
from basicvids_storage import tasks as video_tasks
from basicvids_storage.tasks import process_video_async
from basicvids_storage.transcoding import GeneratedHlsVideo, TranscodedVideoVariant
from basicvids_storage.tests import app, engine, temporary_directory
from basicvids_storage.storage.disk import DiskStorage
from basicvids_storage.models.videos import utc_now


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
    @pytest.fixture(autouse=True)
    def mock_background_processing(self, monkeypatch):
        async def probe_duration(_video_path, _timeout):
            return 125.0

        async def generate_thumbnail(_video_path, _storage, _settings):
            return None

        async def generate_variants(video_path, storage, settings):
            stored_object = storage.save_file(video_path, ".mp4", settings.MAX_UPLOAD_SIZE_BYTES)
            return [
                TranscodedVideoVariant(
                    quality=1080,
                    stored_object=stored_object,
                    content_type="video/mp4",
                )
            ]

        async def generate_hls(_video_path, _storage, _settings):
            return None

        monkeypatch.setattr(video_tasks, "generate_transcoded_video_variants", generate_variants)
        monkeypatch.setattr(video_tasks, "generate_hls_video", generate_hls)
        monkeypatch.setattr(video_tasks, "probe_video_duration", probe_duration)
        monkeypatch.setattr(video_tasks, "generate_video_thumbnail", generate_thumbnail)
        monkeypatch.setattr(video_tasks, "engine", engine)
        monkeypatch.setattr(video_tasks, "build_storage", lambda: DiskStorage(root_path=temporary_directory.name))
        monkeypatch.setattr(videos_router.settings, "DATA_PATH", Path(temporary_directory.name))

        monkeypatch.setattr(videos_router, "enqueue_video_processing", lambda _video_id: None)

    def setup_method(self):
        set_current_user(user())
        with Session(engine) as session:
            session.exec(delete(VideoVariant))
            session.exec(delete(VideoUploadSession))
            session.exec(delete(Video))
            session.exec(delete(Category))
            session.commit()
        for path in Path(temporary_directory.name).iterdir():
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                path.rmdir()

    async def create_processing_video(
        self,
        title: str = "Test clip",
        description: str = "A test upload",
        content: bytes = b"fake-video-bytes",
        category_id: int | None = None,
    ):
        response = await request(
            "POST",
            "/api/v1/videos/uploads/",
            json={
                "title": title,
                "description": description,
                "original_filename": "clip.mp4",
                "content_type": "video/mp4",
                "total_size_bytes": len(content),
                "chunk_size_bytes": len(content),
                "category_id": category_id,
            },
        )
        upload_session = response.json()
        await request("PUT", f"/api/v1/videos/uploads/{upload_session['id']}/chunks/0", content=content)
        response = await request("POST", f"/api/v1/videos/uploads/{upload_session['id']}/complete/")
        return response.json()


class TestVideoResumableUpload(BaseTestVideos):
    method_url = "/api/v1/videos/uploads/"

    async def create_upload_session(self, total_size_bytes: int, chunk_size_bytes: int = 4):
        response = await request(
            "POST",
            self.method_url,
            json={
                "title": "Chunked clip",
                "description": "Resumable upload",
                "original_filename": "chunked.mp4",
                "content_type": "video/mp4",
                "total_size_bytes": total_size_bytes,
                "chunk_size_bytes": chunk_size_bytes,
            },
        )
        assert response.status_code == 201
        return response.json()

    async def test_create_upload_session_success(self):
        response = await request(
            "POST",
            self.method_url,
            json={
                "title": "Chunked clip",
                "description": "Resumable upload",
                "original_filename": "chunked.mp4",
                "content_type": "video/mp4",
                "total_size_bytes": 10,
                "chunk_size_bytes": 4,
            },
        )

        assert response.status_code == 201
        response_data = response.json()
        assert response_data["title"] == "Chunked clip"
        assert response_data["received_chunks"] == []
        assert response_data["received_size_bytes"] == 0
        assert response_data["total_chunks"] == 3
        assert response_data["is_complete"] is False

    async def test_upload_chunks_and_complete_session(self):
        upload_session = await self.create_upload_session(total_size_bytes=10, chunk_size_bytes=4)
        upload_id = upload_session["id"]

        response = await request("PUT", f"{self.method_url}{upload_id}/chunks/0", content=b"fake")
        assert response.status_code == 200
        assert response.json()["received_chunks"] == [0]

        response = await request("PUT", f"{self.method_url}{upload_id}/chunks/1", content=b"-vid")
        assert response.status_code == 200
        assert response.json()["received_chunks"] == [0, 1]

        response = await request("GET", f"{self.method_url}{upload_id}")
        assert response.status_code == 200
        assert response.json()["received_size_bytes"] == 8
        assert response.json()["is_complete"] is False

        response = await request("PUT", f"{self.method_url}{upload_id}/chunks/2", content=b"eo")
        assert response.status_code == 200
        assert response.json()["is_complete"] is True

        response = await request("POST", f"{self.method_url}{upload_id}/complete/")

        assert response.status_code == 201
        response_data = response.json()
        assert response_data["original_filename"] == "chunked.mp4"
        assert response_data["content_type"] == "video/mp4"
        assert response_data["status"] == "processing"

        await process_video_async(response_data["id"])
        response = await request("GET", f"/api/v1/videos/{response_data['id']}/download/")
        assert response.status_code == 200
        assert response.content == b"fake-video"

        with Session(engine) as session:
            assert session.get(VideoUploadSession, upload_id) is None

    async def test_complete_upload_requires_all_chunks(self):
        upload_session = await self.create_upload_session(total_size_bytes=10, chunk_size_bytes=4)
        upload_id = upload_session["id"]
        await request("PUT", f"{self.method_url}{upload_id}/chunks/0", content=b"fake")

        response = await request("POST", f"{self.method_url}{upload_id}/complete/")

        assert response.status_code == 409

    async def test_upload_chunk_rejects_wrong_size(self):
        upload_session = await self.create_upload_session(total_size_bytes=10, chunk_size_bytes=4)
        upload_id = upload_session["id"]

        response = await request("PUT", f"{self.method_url}{upload_id}/chunks/0", content=b"bad")

        assert response.status_code == 400

    async def test_delete_upload_session_success(self):
        upload_session = await self.create_upload_session(total_size_bytes=10, chunk_size_bytes=4)
        upload_id = upload_session["id"]
        await request("PUT", f"{self.method_url}{upload_id}/chunks/0", content=b"fake")

        response = await request("DELETE", f"{self.method_url}{upload_id}")

        assert response.status_code == 204
        response = await request("GET", f"{self.method_url}{upload_id}")
        assert response.status_code == 404


class TestVideosRead(BaseTestVideos):
    method_url = "/api/v1/videos"

    async def create_video(
        self,
        title: str = "Test clip",
        description: str = "A test upload",
        category_id: int | None = None,
    ):
        video = await self.create_processing_video(title=title, description=description, category_id=category_id)
        await process_video_async(video["id"])
        response = await request("GET", f"{self.method_url}/{video['id']}")
        return response.json()

    def create_category(self, name: str, slug: str, parent_id: int | None = None) -> Category:
        with Session(engine) as session:
            category = Category(name=name, slug=slug, parent_id=parent_id)
            session.add(category)
            session.commit()
            session.refresh(category)
            return category

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

    async def test_list_videos_filters_by_author_id(self):
        await self.create_video(title="User one video")
        set_current_user(user(user_id=2))
        await self.create_video(title="User two video")
        set_current_user(user())

        response = await request("GET", f"{self.method_url}/", params={"author_id": 2})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 1
        assert response_data["videos"][0]["author_id"] == 2

    async def test_list_videos_filters_by_category_with_descendants(self):
        root = self.create_category("Education", "education")
        child = self.create_category("Math", "math", parent_id=root.id)
        grandchild = self.create_category("Algebra", "algebra", parent_id=child.id)
        other = self.create_category("Travel", "travel")

        await self.create_video(title="Linear equations", category_id=grandchild.id)
        await self.create_video(title="Road trip", category_id=other.id)

        response = await request("GET", f"{self.method_url}/", params={"category_id": root.id})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 1
        assert response_data["videos"][0]["category"]["slug"] == "algebra"

    async def test_list_videos_filters_by_exact_category_without_descendants(self):
        root = self.create_category("Education", "education")
        child = self.create_category("Math", "math", parent_id=root.id)

        await self.create_video(title="General education", category_id=root.id)
        await self.create_video(title="Trigonometry", category_id=child.id)

        response = await request(
            "GET",
            f"{self.method_url}/",
            params={"category_id": root.id, "include_subcategories": "false"},
        )

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 1
        assert response_data["videos"][0]["category"]["slug"] == "education"

    async def test_list_videos_filters_by_multiple_categories(self):
        education = self.create_category("Education", "education")
        travel = self.create_category("Travel", "travel")
        cooking = self.create_category("Cooking", "cooking")

        await self.create_video(title="Course", category_id=education.id)
        await self.create_video(title="Road trip", category_id=travel.id)
        await self.create_video(title="Recipe", category_id=cooking.id)

        response = await request(
            "GET",
            f"{self.method_url}/",
            params=[("category_id", education.id), ("category_id", travel.id)],
        )

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 2
        assert {video["category"]["slug"] for video in response_data["videos"]} == {"education", "travel"}

    async def test_list_videos_filters_by_duration(self):
        with Session(engine) as session:
            for title, duration_seconds in [
                ("Short clip", 120),
                ("Class recording", 600),
                ("Feature lesson", 1500),
            ]:
                session.add(
                    Video(
                        title=title,
                        description=None,
                        original_filename=f"{title}.mp4",
                        content_type="video/mp4",
                        size_bytes=10,
                        author_id=1,
                        author_username="user-1",
                        storage_backend="disk",
                        storage_key=title,
                        status="ready",
                        duration_seconds=duration_seconds,
                    )
                )
            session.commit()

        response = await request("GET", f"{self.method_url}/", params={"duration": "3_20"})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 1
        assert response_data["videos"][0]["title"] == "Class recording"

    async def test_list_videos_filters_by_upload_period(self):
        now = utc_now()
        with Session(engine) as session:
            session.add(
                Video(
                    title="Fresh clip",
                    description=None,
                    original_filename="fresh.mp4",
                    content_type="video/mp4",
                    size_bytes=10,
                    author_id=1,
                    author_username="user-1",
                    storage_backend="disk",
                    storage_key="fresh-video",
                    status="ready",
                    created_at=now,
                )
            )
            session.add(
                Video(
                    title="Old clip",
                    description=None,
                    original_filename="old.mp4",
                    content_type="video/mp4",
                    size_bytes=10,
                    author_id=1,
                    author_username="user-1",
                    storage_backend="disk",
                    storage_key="old-video",
                    status="ready",
                    created_at=now - timedelta(days=400),
                )
            )
            session.commit()

        response = await request("GET", f"{self.method_url}/", params={"uploaded": "year"})

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["count"] == 1
        assert response_data["videos"][0]["title"] == "Fresh clip"

    async def test_get_video_marks_stale_processing_as_failed(self):
        response = await request(
            "POST",
            "/api/v1/videos/uploads/",
            json={
                "title": "Stuck clip",
                "original_filename": "clip.mp4",
                "content_type": "video/mp4",
                "total_size_bytes": len(b"fake-video-bytes"),
                "chunk_size_bytes": len(b"fake-video-bytes"),
            },
        )
        upload_session = response.json()
        response = await request("PUT", f"/api/v1/videos/uploads/{upload_session['id']}/chunks/0", content=b"fake-video-bytes")
        assert response.status_code == 200
        response = await request("POST", f"/api/v1/videos/uploads/{upload_session['id']}/complete/")
        video = response.json()

        with Session(engine) as session:
            stored_video = session.get(Video, video["id"])
            stored_video.created_at = utc_now() - timedelta(seconds=4000)
            session.add(stored_video)
            session.commit()

        response = await request("GET", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["status"] == "failed"
        assert response_data["processing_error"] == "Video processing timed out"

    async def test_get_video_success(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == video["id"]
        assert response.json()["duration_seconds"] == 125.0

    async def test_get_video_returns_category(self):
        category = self.create_category("Education", "education")
        video = await self.create_video(category_id=category.id)

        response = await request("GET", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200
        assert response.json()["category"]["id"] == category.id
        assert response.json()["category"]["slug"] == "education"

    async def test_download_video_success(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}/download/")

        assert response.status_code == 200
        assert response.content == b"fake-video-bytes"

    async def test_download_video_quality_success(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}/download/", params={"quality": 1080})

        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert response.content == b"fake-video-bytes"

    async def test_download_video_quality_not_found(self):
        video = await self.create_video()
        response = await request("GET", f"{self.method_url}/{video['id']}/download/", params={"quality": 720})

        assert response.status_code == 404

    async def test_hls_playlist_and_segments_success(self, monkeypatch, tmp_path):
        async def generate_hls(_video_path, storage, settings):
            master_path = tmp_path / "master.m3u8"
            playlist_path = tmp_path / "playlist.m3u8"
            segment_path = tmp_path / "segment-00000.ts"
            master_path.write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=600000\n720p/playlist.m3u8\n")
            playlist_path.write_text("#EXTM3U\n#EXTINF:6.0,\nsegment-00000.ts\n#EXT-X-ENDLIST\n")
            segment_path.write_bytes(b"fake-hls-segment")
            prefix = "hls/test-video"
            manifest = storage.save_file_as(master_path, f"{prefix}/master.m3u8", settings.MAX_UPLOAD_SIZE_BYTES)
            playlist = storage.save_file_as(playlist_path, f"{prefix}/720p/playlist.m3u8", settings.MAX_UPLOAD_SIZE_BYTES)
            segment = storage.save_file_as(segment_path, f"{prefix}/720p/segment-00000.ts", settings.MAX_UPLOAD_SIZE_BYTES)
            return GeneratedHlsVideo(
                storage_prefix=prefix,
                manifest_stored_object=manifest,
                stored_objects=[manifest, playlist, segment],
            )

        monkeypatch.setattr(video_tasks, "generate_hls_video", generate_hls)
        video = await self.create_video()

        assert video["has_hls"] is True

        response = await request("GET", f"{self.method_url}/{video['id']}/hls/master.m3u8")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert b"720p/playlist.m3u8" in response.content

        response = await request("GET", f"{self.method_url}/{video['id']}/hls/720p/segment-00000.ts")
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp2t"
        assert response.content == b"fake-hls-segment"

    async def test_hls_asset_rejects_path_traversal(self, monkeypatch, tmp_path):
        async def generate_hls(_video_path, storage, settings):
            master_path = tmp_path / "master.m3u8"
            master_path.write_text("#EXTM3U\n")
            prefix = "hls/test-video"
            manifest = storage.save_file_as(master_path, f"{prefix}/master.m3u8", settings.MAX_UPLOAD_SIZE_BYTES)
            return GeneratedHlsVideo(
                storage_prefix=prefix,
                manifest_stored_object=manifest,
                stored_objects=[manifest],
            )

        monkeypatch.setattr(video_tasks, "generate_hls_video", generate_hls)
        video = await self.create_video()

        response = await request("GET", f"{self.method_url}/{video['id']}/hls/../database.db")

        assert response.status_code in {400, 404}

    async def test_download_video_thumbnail_success(self):
        response = await request(
            "POST",
            "/api/v1/videos/uploads/",
            json={
                "title": "Test clip",
                "original_filename": "clip.mp4",
                "content_type": "video/mp4",
                "total_size_bytes": len(b"fake-video-bytes"),
                "chunk_size_bytes": len(b"fake-video-bytes"),
            },
        )
        upload_session = response.json()
        await request("PUT", f"/api/v1/videos/uploads/{upload_session['id']}/chunks/0", content=b"fake-video-bytes")
        response = await request("POST", f"/api/v1/videos/uploads/{upload_session['id']}/complete/")
        video = response.json()
        await process_video_async(video["id"])

        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("thumb.png", b"fake-image-bytes", "image/png")},
        )
        assert response.status_code == 200

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
        stored_files = [path for path in Path(temporary_directory.name).iterdir() if path.is_file()]
        assert len(stored_files) == 1

        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
        )

        assert response.status_code == 200
        assert response.json()["has_thumbnail"] is True
        assert len([path for path in Path(temporary_directory.name).iterdir() if path.is_file()]) == 2

        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("new-thumb.jpg", b"new-image-bytes", "image/jpeg")},
        )

        assert response.status_code == 200
        assert len([path for path in Path(temporary_directory.name).iterdir() if path.is_file()]) == 2

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
        category = self.create_category("Education", "education")
        video = await self.create_video()
        response = await request(
            "PATCH",
            f"{self.method_url}/{video['id']}",
            json={
                "title": "Changed title",
                "description": "Changed description",
                "category_id": category.id,
            },
        )

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["title"] == "Changed title"
        assert response_data["description"] == "Changed description"
        assert response_data["category"]["slug"] == "education"

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
            "/api/v1/videos/uploads/",
            json={
                "title": "Test clip",
                "original_filename": "clip.mp4",
                "content_type": "video/mp4",
                "total_size_bytes": len(b"fake-video-bytes"),
                "chunk_size_bytes": len(b"fake-video-bytes"),
            },
        )
        upload_session = response.json()
        await request("PUT", f"/api/v1/videos/uploads/{upload_session['id']}/chunks/0", content=b"fake-video-bytes")
        response = await request("POST", f"/api/v1/videos/uploads/{upload_session['id']}/complete/")
        video = response.json()
        await process_video_async(video["id"])
        response = await request(
            "PUT",
            f"{self.method_url}/{video['id']}/thumbnail/",
            files={"thumbnail": ("thumb.jpg", b"fake-image-bytes", "image/jpeg")},
        )
        assert response.status_code == 200
        stored_files = [path for path in Path(temporary_directory.name).iterdir() if path.is_file()]
        assert len(stored_files) == 2

        response = await request("DELETE", f"{self.method_url}/{video['id']}")

        assert response.status_code == 200
        assert response.json() == {"message": "Video deleted successfully"}

        response = await request("GET", f"{self.method_url}/{video['id']}")
        assert response.status_code == 404
        assert [path for path in Path(temporary_directory.name).iterdir() if path.is_file()] == []

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
