#!/bin/bash

# Configuration
APP_DIR="/home/azureuser/projects/procucev_proc_agent"
VENV_PYTHON="$APP_DIR/venv/bin/python"
LOG_DIR="$APP_DIR/logs"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/app_$DATE.log"

# Worker configuration (override with: WORKERS=8 ./restart_app.sh)
export WORKERS=${WORKERS:-auto}

# Create logs directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Preserve existing app.log if it exists and is not empty
if [ -f "$APP_DIR/app.log" ] && [ -s "$APP_DIR/app.log" ]; then
    TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
    mv "$APP_DIR/app.log" "$LOG_DIR/app_$TIMESTAMP.log"
    echo "Preserved old log as app_$TIMESTAMP.log"
fi

# Kill existing process
echo "Stopping old server..."
sudo pkill -f "gunicorn.*app.main:app"
sudo pkill -f "uvicorn.*app.main:app"
sudo fuser -k 80/tcp 2>/dev/null || true
sleep 3

# Start the application with Gunicorn
echo "Starting new server..."
cd "$APP_DIR"
sudo nohup $VENV_PYTHON -m gunicorn app.main:app \
    --config gunicorn_config.py \
    --bind 0.0.0.0:80 \
    >> "$LOG_FILE" 2>&1 &

echo "Application restarted. Logs: $LOG_FILE"
echo "PID: $!"