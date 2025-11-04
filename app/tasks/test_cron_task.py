"""
Test cron job task.

Simple task to test Celery cron job functionality.
"""

import logging
from celery import shared_task
from datetime import datetime

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def test_cron_job(self):
    """
    Test cron job that prints a test message.
    """
    try:
        message = "testing celery cron job"
        timestamp = datetime.utcnow().isoformat()
        
        logger.info(f"{message} - {timestamp}")
        print(f"{message} - {timestamp}")
        
        return {
            "status": "completed",
            "message": message,
            "timestamp": timestamp
        }
        
    except Exception as e:
        logger.error(f"Test cron job failed: {e}")
        raise