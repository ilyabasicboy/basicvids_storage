#!/bin/bash
set -e

ENV_FILE=/basicvids_storage/data/.env

if [ ! -f "$ENV_FILE" ]; then
    echo "Creating .env file..."
    mkdir -p /basicvids_storage/data
    touch "$ENV_FILE"
fi

export $(grep -v '^#' "$ENV_FILE" | xargs)

WORKERS="${WORKERS:-1}"

echo "Starting server with $WORKERS workers"

exec gunicorn basicvids_storage.main:app \
    -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --workers $WORKERS \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
