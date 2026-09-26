#!/bin/bash

# Configuration
APP_DIR="/home/azureuser/projects/procucev_proc_agent"
VENV_PYTHON="$APP_DIR/venv/bin/python"
LOG_BASE="$APP_DIR/logs"
DATE=$(date +%Y-%m-%d)
STARTUP_LOG_FILE="$LOG_BASE/startup_$DATE.log"

# Worker configuration (override with: WORKERS=8 ./restart_app.sh)
export WORKERS=${WORKERS:-2}

# Read by app/utils/logging_utils.py setup_basic_logging
export LOG_DIR="$LOG_BASE/app"

mkdir -p "$LOG_BASE"
mkdir -p "$LOG_DIR"

# Preserve existing app.log if it exists and is not empty
if [ -f "$APP_DIR/app.log" ] && [ -s "$APP_DIR/app.log" ]; then
    TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
    mv "$APP_DIR/app.log" "$LOG_BASE/app_$TIMESTAMP.log"
    echo "Preserved old log as app_$TIMESTAMP.log"
fi

# Kill existing processes
echo "Stopping old servers..."
sudo pkill -f "gunicorn.*app.main:app"
sudo pkill -f "uvicorn.*app.main:app"
sudo fuser -k 80/tcp 2>/dev/null || true
sleep 3

# Start the application with Gunicorn (sudo -E preserves LOG_DIR/WORKERS for the gunicorn process)
echo "Starting application server..."
sudo -E nohup $VENV_PYTHON -m gunicorn app.main:app \
    --config gunicorn_config.py \
    --bind 0.0.0.0:80 \
    >> "$STARTUP_LOG_FILE" 2>&1 &

echo "Application restarted."
echo "App logs: $LOG_DIR/app.log (rotated daily as app.log.YYYY-MM-DD)"
echo "Startup/crash log: $STARTUP_LOG_FILE"
echo "App PID: $!"