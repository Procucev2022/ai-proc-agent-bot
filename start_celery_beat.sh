#!/bin/bash
# Start Celery Beat for AI Procurement Agent
# This script starts the Celery beat scheduler

echo "Starting Celery Beat Scheduler..."
echo ""

# Activate virtual environment
source venv/bin/activate

# Start Celery beat
celery -A app.celery_app beat --loglevel=info
