# BasicVids Storage

Video storage microservice for BasicVids.

The first backend stores uploaded videos on local disk. Storage access is isolated behind a small backend interface so a cloud backend can be added later without changing the API routes.

## Stack

* Gunicorn
* FastAPI
* SQLModel
* Disk storage backend

## Development

Use a virtual environment. Do not install packages into the system Python:

```bash
virtualenv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run tests:

```bash
pytest
```

Run locally:

```bash
uvicorn basicvids_storage.main:app --reload
```

## Container

Each BasicVids microservice should run in its own container and use the shared `basicvids_gateway` project for public HTTP access. Start this service with:

```bash
mkdir -p data
docker compose up -d --build
```

The service is available through the shared gateway at:

```text
http://localhost:8080/api/v1/videos/
```

## Image Configuration

Environment variables:

| Variable              | Default                | Description                         |
| --------------------- | ---------------------- | ----------------------------------- |
| DATA_PATH             | ./data                 | Data directory mounted in container |
| STORAGE_BACKEND       | disk                   | Storage backend name                |
| DATABASE_URL          | sqlite:///./data/database.db | Metadata database URL        |
| VIDEO_STORAGE_DIR     | videos                 | Directory inside DATA_PATH for files |
| MAX_UPLOAD_SIZE_BYTES | 2147483648             | Maximum upload size                 |
| AUTH_CURRENT_USER_URL | http://basicvids_auth:8000/api/v1/users/detail/ | Auth service current-user endpoint |

Project environment can be placed in:

```text
./data/.env
```

## API Documentation

### Health Check

- **GET** `/health`
  - **Response:** `{ "status": "ok" }`

### Videos

- **POST** `/api/v1/videos/uploads/`
  - Create resumable upload session
- **PUT** `/api/v1/videos/uploads/{upload_id}/chunks/{chunk_index}`
  - Upload chunk
- **POST** `/api/v1/videos/uploads/{upload_id}/complete/`
  - Finalize resumable upload and create video
  - **Requires:** authentication
  - **Form fields:** `file`, `title`, `description` optional
  - **Accepts:** `video/*`
  - **Response:** `{ id, title, description, original_filename, content_type, size_bytes, author_id, author_username, author_first_name, author_last_name, storage_backend, created_at }`

- **GET** `/api/v1/videos/`
  - **Query parameters:** `offset` (default: 0), `limit` (default: 20, max: 100)
  - **Response:** `{ videos: [...], count }`

- **GET** `/api/v1/videos/{video_id}`
  - **Response:** `{ id, title, description, original_filename, content_type, size_bytes, author_id, author_username, author_first_name, author_last_name, storage_backend, created_at }`

- **PATCH** `/api/v1/videos/{video_id}`
  - **Requires:** authentication as the video author or an admin
  - **Body:** `{ "title": "Video title", "description": "Description or null" }`
  - **Response:** `{ id, title, description, original_filename, content_type, size_bytes, author_id, author_username, author_first_name, author_last_name, storage_backend, created_at }`

- **GET** `/api/v1/videos/{video_id}/download/`
  - **Response:** video file

- **DELETE** `/api/v1/videos/{video_id}`
  - **Requires:** authentication as the video author or an admin
  - **Response:** `{ "message": "Video deleted successfully" }`
