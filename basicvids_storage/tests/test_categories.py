from sqlmodel import Session, delete, select
import httpx
import pytest

from basicvids_storage.auth import CurrentUser, get_current_user
from basicvids_storage.models.categories import Category
from basicvids_storage.models.videos import Video
from basicvids_storage.tests import app, engine


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


class BaseTestCategories:
    def setup_method(self):
        set_current_user(user(user_id=1, is_admin=True))
        with Session(engine) as session:
            session.exec(delete(Video))
            session.exec(delete(Category))
            session.commit()

    def create_category(self, name: str, slug: str, parent_id: int | None = None) -> Category:
        with Session(engine) as session:
            category = Category(name=name, slug=slug, parent_id=parent_id)
            session.add(category)
            session.commit()
            session.refresh(category)
            return category


class TestCategories(BaseTestCategories):
    method_url = "/api/v1/categories/"

    async def test_admin_can_create_category(self):
        response = await request(
            "POST",
            self.method_url,
            json={"name": "Education", "slug": "education"},
        )

        assert response.status_code == 201
        response_data = response.json()
        assert response_data["name"] == "Education"
        assert response_data["slug"] == "education"
        assert response_data["depth"] == 1

    async def test_non_admin_cannot_create_category(self):
        set_current_user(user())

        response = await request(
            "POST",
            self.method_url,
            json={"name": "Education", "slug": "education"},
        )

        assert response.status_code == 403

    async def test_create_subcategory_rejects_fourth_level(self):
        root = self.create_category("Education", "education")
        child = self.create_category("Math", "math", parent_id=root.id)
        grandchild = self.create_category("Algebra", "algebra", parent_id=child.id)

        response = await request(
            "POST",
            self.method_url,
            json={"name": "Linear", "slug": "linear", "parent_id": grandchild.id},
        )

        assert response.status_code == 400
        assert "cannot exceed 3 levels" in response.json()["detail"]

    async def test_list_categories_returns_tree(self):
        root = self.create_category("Education", "education")
        child = self.create_category("Math", "math", parent_id=root.id)
        self.create_category("Algebra", "algebra", parent_id=child.id)

        response = await request("GET", self.method_url)

        assert response.status_code == 200
        response_data = response.json()
        assert len(response_data) == 1
        assert response_data[0]["slug"] == "education"
        assert response_data[0]["children"][0]["slug"] == "math"
        assert response_data[0]["children"][0]["children"][0]["slug"] == "algebra"

    async def test_change_category_rejects_cycle(self):
        root = self.create_category("Education", "education")
        child = self.create_category("Math", "math", parent_id=root.id)

        response = await request(
            "PATCH",
            f"{self.method_url}{root.id}",
            json={"parent_id": child.id},
        )

        assert response.status_code == 400
        assert "cycle" in response.json()["detail"].lower()

    async def test_delete_category_rejects_when_it_has_children(self):
        root = self.create_category("Education", "education")
        self.create_category("Math", "math", parent_id=root.id)

        response = await request("DELETE", f"{self.method_url}{root.id}")

        assert response.status_code == 409

    async def test_delete_category_reassigns_videos_to_parent(self):
        parent = self.create_category("Education", "education")
        category = self.create_category("Math", "math", parent_id=parent.id)
        with Session(engine) as session:
            session.add(
                Video(
                    title="Course",
                    description=None,
                    original_filename="course.mp4",
                    content_type="video/mp4",
                    size_bytes=10,
                    author_id=1,
                    author_username="user-1",
                    storage_backend="disk",
                    storage_key="course-video",
                    category_id=category.id,
                )
            )
            session.commit()

        response = await request("DELETE", f"{self.method_url}{category.id}")

        assert response.status_code == 204
        with Session(engine) as session:
            stored_video = session.exec(select(Video).where(Video.storage_key == "course-video")).first()
            assert stored_video is not None
            assert stored_video.category_id == parent.id

    async def test_delete_root_category_clears_video_category(self):
        category = self.create_category("Education", "education")
        with Session(engine) as session:
            session.add(
                Video(
                    title="Course",
                    description=None,
                    original_filename="course.mp4",
                    content_type="video/mp4",
                    size_bytes=10,
                    author_id=1,
                    author_username="user-1",
                    storage_backend="disk",
                    storage_key="course-video",
                    category_id=category.id,
                )
            )
            session.commit()

        response = await request("DELETE", f"{self.method_url}{category.id}")

        assert response.status_code == 204
        with Session(engine) as session:
            stored_video = session.exec(select(Video).where(Video.storage_key == "course-video")).first()
            assert stored_video is not None
            assert stored_video.category_id is None
