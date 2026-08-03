#!/bin/sh

# ============================================================================
# App Entrypoint — Gunicorn with Python-side log rotation
# ============================================================================
set -e

# Default to 1-2 workers for cloud containers (prevent OOM kill)
export WORKERS="${WORKERS:-2}"
export WORKER_THREADS="${WORKER_THREADS:-2}"

LOG_DIR="/app/logs/app"
mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_DIR/app.log"
}

log "Starting Procucev App with $WORKERS worker(s)..."
log "Log directory: $LOG_DIR"
log "Starting Gunicorn server..."

exec gunicorn app.main:app --config gunicorn_config.py --pid /app/gunicorn.pid
