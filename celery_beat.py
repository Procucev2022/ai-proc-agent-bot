#!/usr/bin/env python3
"""
Celery Beat scheduler entry point.

This script starts the Celery Beat scheduler for periodic task execution.
Run this script to schedule auto-categorization and seller matching tasks.

Usage:
    python celery_beat.py

Environment Variables:
    REDIS_URL: Redis connection URL for Celery broker
    CELERY_BEAT_LOGLEVEL: Logging level (default: info)
"""

import os
import sys
import logging
from app.celery_app import celery_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

def main():
    """Start the Celery Beat scheduler."""
    try:
        logger.info("Starting Celery Beat scheduler for procucev-agent periodic tasks")
        
        # Get configuration from environment
        loglevel = os.getenv('CELERY_BEAT_LOGLEVEL', 'info')
        
        # Start beat scheduler
        celery_app.start([
            'beat',
            '--loglevel', loglevel,
            '--schedule', 'celerybeat-schedule',  # SQLite database for schedule
        ])
        
    except KeyboardInterrupt:
        logger.info("Celery Beat scheduler stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to start Celery Beat scheduler: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()