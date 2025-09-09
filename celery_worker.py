#!/usr/bin/env python3
"""
Celery worker entry point.

This script starts the Celery worker processes for background task execution.
Run this script to start processing auto-categorization and seller matching tasks.

Usage:
    python celery_worker.py

Environment Variables:
    REDIS_URL: Redis connection URL for Celery broker
    CELERY_CONCURRENCY: Number of concurrent worker processes (default: 2)
    CELERY_LOGLEVEL: Logging level (default: info)
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
    """Start the Celery worker."""
    try:
        logger.info("Starting Celery worker for procucev-agent tasks")
        
        # Get configuration from environment
        concurrency = int(os.getenv('CELERY_CONCURRENCY', '2'))
        loglevel = os.getenv('CELERY_LOGLEVEL', 'info')
        
        # Start worker with configuration
        celery_app.worker_main([
            'worker',
            '--concurrency', str(concurrency),
            '--loglevel', loglevel,
            '--queues', 'categorization,seller_matching,celery',  # Listen to all queues
            '--prefetch-multiplier', '1',  # Process one task at a time
            '--max-tasks-per-child', '1000',  # Restart worker after 1000 tasks
        ])
        
    except KeyboardInterrupt:
        logger.info("Celery worker stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to start Celery worker: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()