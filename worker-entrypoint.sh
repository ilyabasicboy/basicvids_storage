#!/bin/bash
set -e

ENV_FILE=/basicvids_storage/data/.env

if [ ! -f "$ENV_FILE" ]; then
    echo "Creating .env file..."
    mkdir -p /basicvids_storage/data
    touch "$ENV_FILE"
fi

export $(grep -v '^#' "$ENV_FILE" | xargs)

echo "Starting Celery worker with concurrency ${VIDEO_TRANSCODE_WORKER_CONCURRENCY:-1}"

python -c "from basicvids_storage.db import create_db_and_tables; create_db_and_tables()"

exec celery -A basicvids_storage.celery_app:celery_app worker \
    --loglevel=info \
    --queues="${VIDEO_TRANSCODE_QUEUE:-video_transcode}" \
    --concurrency="${VIDEO_TRANSCODE_WORKER_CONCURRENCY:-1}"
