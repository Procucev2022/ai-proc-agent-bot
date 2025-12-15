#!/bin/bash

# Configuration
APP_DIR="/home/azureuser/projects/procucev_proc_agent"
VENV_PYTHON="$APP_DIR/venv/bin/python"
VENV_BIN="$APP_DIR/venv/bin"
LOG_DIR="$APP_DIR/logs"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/app_$DATE.log"
CHROMA_LOG_FILE="$LOG_DIR/chromadb_$DATE.log"

# ChromaDB configuration
CHROMA_HOST="0.0.0.0"
CHROMA_PORT="8100"
CHROMA_PATH="$APP_DIR/chroma_db"

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

# Kill existing processes
echo "Stopping old servers..."
sudo pkill -f "gunicorn.*app.main:app"
sudo pkill -f "uvicorn.*app.main:app"
pkill -f "chroma run"
sudo fuser -k 80/tcp 2>/dev/null || true
sleep 3

# Start ChromaDB server
echo "Starting ChromaDB server on port $CHROMA_PORT..."
cd "$APP_DIR"
nohup $VENV_BIN/chroma run --host $CHROMA_HOST --port $CHROMA_PORT --path "$CHROMA_PATH" >> "$CHROMA_LOG_FILE" 2>&1 &
CHROMA_PID=$!
echo "ChromaDB PID: $CHROMA_PID"

# Wait for ChromaDB to be ready
echo "Waiting for ChromaDB to start..."
for i in {1..30}; do
    if curl -s "http://localhost:$CHROMA_PORT/api/v1/heartbeat" > /dev/null 2>&1; then
        echo "ChromaDB is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "WARNING: ChromaDB may not be ready yet, continuing anyway..."
    fi
    sleep 1
done

# Start the application with Gunicorn
echo "Starting application server..."
sudo nohup $VENV_PYTHON -m gunicorn app.main:app \
    --config gunicorn_config.py \
    --bind 0.0.0.0:80 \
    >> "$LOG_FILE" 2>&1 &

echo "Application restarted. Logs: $LOG_FILE"
echo "ChromaDB logs: $CHROMA_LOG_FILE"
echo "App PID: $!"