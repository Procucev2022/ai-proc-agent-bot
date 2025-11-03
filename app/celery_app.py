"""
Celery application configuration for background tasks.

This module initializes and configures Celery for handling asynchronous
and scheduled tasks in the AI Procurement Agent application.

Key responsibilities:
- Celery app initialization
- Redis broker and backend configuration
- Task routing and scheduling
- Beat schedule for periodic tasks
"""

from celery import Celery
from celery.schedules import crontab
from app.config import get_settings

# Get settings
settings = get_settings()

# Initialize Celery app
celery_app = Celery(
    "procucev_proc_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.categorization_tasks"]
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    task_soft_time_limit=25 * 60,  # 25 minutes
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
)

# Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    "run-enhanced-categorization-every-5-minutes": {
        "task": "app.tasks.categorization_tasks.update_vector_store_task",
        "schedule": 300.0,  # 5 minutes in seconds (can also use crontab)
        # Alternative using crontab:
        # "schedule": crontab(minute="*/5"),
        "options": {
            "expires": 280.0,  # Task expires after 4 minutes 40 seconds
        }
    },
}

# Optional: Route different tasks to different queues
celery_app.conf.task_routes = {
    "app.tasks.categorization_tasks.*": {"queue": "categorization"},
}

if __name__ == "__main__":
    celery_app.start()
