# BasicVids Storage

Video storage microservice for BasicVids.

The service stores uploaded videos on local disk, tracks metadata in the database, and offloads transcoding and HLS generation to Celery workers.

## Stack

- Gunicorn
- FastAPI
- SQLModel
- Redis
- Celery
- ffmpeg / ffprobe

## Development

Use a virtual environment:

```bash
virtualenv venv
source venv/bin/activate
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

```bash
mkdir -p data
cp .env.example data/.env
docker compose up -d --build
```

The service is available through the shared gateway at:

```text
http://localhost:8080/api/v1/videos/
http://localhost:8080/api/v1/categories/
```

## Configuration

Project environment is loaded from:

```text
./data/.env
```

Start from:

```text
./.env.example
```

Database examples:

```env
# SQLite default
# DATABASE_URL=sqlite:///./data/database.db

# PostgreSQL example
DATABASE_URL=postgresql://basicvids_storage_user:change_me@host.docker.internal:5432/basicvids_storage
```

Important variables:

| Variable | Default | Description |
| --- | --- | --- |
| `DATA_PATH` | `./data` | Data directory mounted in container |
| `DATABASE_URL` | `sqlite:///./data/database.db` | Metadata database URL |
| `REDIS_URL` | `redis://localhost:6379/2` | Redis connection for rate limiting and app state |
| `CELERY_BROKER_URL` | `redis://basicvids_redis:6379/0` | Celery broker |
| `CELERY_RESULT_BACKEND` | `redis://basicvids_redis:6379/1` | Celery result backend |
| `AUTH_CURRENT_USER_URL` | `http://basicvids_auth:8000/api/v1/users/detail/` | Auth service current-user endpoint |
| `VIDEO_TRANSCODE_WORKER_CONCURRENCY` | `1` | Celery worker concurrency |
| `VIDEO_TRANSCODE_THREADS` | `2` | ffmpeg thread count per transcode |

## Runtime Notes

Local development requires more than Python packages:

- Redis
- Celery worker
- `ffmpeg`
- `ffprobe`

The docker compose file starts both API and worker containers.

## Healthcheck

```text
http://localhost:8080/storage/health
```
