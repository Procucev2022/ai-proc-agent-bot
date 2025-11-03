"""
Celery application instance and configuration.

This module initializes the Celery application for background task processing
including auto-categorization and seller matching jobs.
"""

from celery import Celery
from app.config import get_settings

settings = get_settings()

# Create Celery instance
celery_app = Celery(
    'procucev_agent',
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        'app.tasks.auto_categorization_task'
    ]
)

# Configure Celery
celery_app.config_from_object('app.celery_config')

if __name__ == '__main__':
    celery_app.start()

