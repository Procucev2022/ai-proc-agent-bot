#!/bin/bash
# Start Celery Worker for AI Procurement Agent
# This script starts the Celery worker process

echo "Starting Celery Worker..."
echo ""

# Activate virtual environment
source venv/bin/activate

# Start Celery worker
celery -A app.celery_app worker --loglevel=info
