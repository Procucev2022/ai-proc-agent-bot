#!/bin/sh
set -e

# Default to 1-2 workers for cloud containers to prevent OOM
export WORKERS="${WORKERS:-1}"
export WORKER_THREADS="${WORKER_THREADS:-2}"

LOG_DIR="/app/logs/app"
mkdir -p "$LOG_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Procucev App with $WORKERS worker(s) on port 8005..."

exec gunicorn app.main:app \
    --bind 0.0.0.0:8005 \
    --workers "$WORKERS" \
    --worker-class uvicorn.workers.UvicornWorker \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
