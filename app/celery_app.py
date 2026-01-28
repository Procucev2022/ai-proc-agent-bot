"""
Celery application instance and configuration.

This module initializes the Celery application for background task processing
including auto-categorization, seller matching, and daily aggregation jobs.
"""

from celery import Celery
from app.config import get_settings

settings = get_settings()

# Create Celery instance
celery_app = Celery(
    'procucev_agent',
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        'app.tasks.auto_categorization_task',
        'app.tasks.vector_store_sync_task',
        'app.tasks.seller_matching_task',
        'app.tasks.daily_category_vector_rebuild_task',

        'app.tasks.log_cleanup_task',
        'app.tasks.bfs_notification_task',
        'app.tasks.whatsapp_report_automation_task'
    ]
)

# Configure Celery
celery_app.config_from_object('app.celery_config')

# Make celery_app available for imports
__all__ = ['celery_app']

if __name__ == '__main__':
    celery_app.start()

