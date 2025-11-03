#!/bin/bash
# Start Celery Worker with embedded Beat (Development Only)
# This script starts both worker and beat in a single process

echo "======================================"
echo "Starting Celery Worker with Beat"
echo "======================================"
echo ""
echo "WARNING: This is for DEVELOPMENT ONLY"
echo "For production, run worker and beat separately"
echo ""

# Activate virtual environment
source venv/bin/activate

# Start Celery worker with embedded beat
celery -A app.celery_app worker --beat --loglevel=info
