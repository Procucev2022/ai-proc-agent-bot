#!/bin/sh

# ============================================================================
# App Entrypoint — Gunicorn with Python-side log rotation
# ============================================================================
# Logs are written directly by Python (ConcurrentTimedRotatingFileHandler in
# app/utils/logging_utils.py → app.log, rotated to app.log.YYYY-MM-DD) and by
# Gunicorn (accesslog/errorlog in gunicorn_config.py). No FIFO, no background
# subshell. Archival/retention is handled by app/tasks/log_cleanup_task.py.
# ============================================================================

set -e

LOG_DIR="/app/logs/app"
mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_DIR/app.log"
}

log "Starting Procucev App..."
log "Log directory: $LOG_DIR"
log "Starting Gunicorn server..."

exec gunicorn app.main:app --config gunicorn_config.py --pid /app/gunicorn.pid
