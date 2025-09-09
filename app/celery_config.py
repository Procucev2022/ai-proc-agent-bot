"""
Celery configuration settings.

This module contains configuration for Celery workers, task routing,
and periodic task scheduling.
"""

from celery.schedules import crontab
import os

# Basic Celery configuration
broker_url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
result_backend = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# Task serialization
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
timezone = 'UTC'
enable_utc = True

# Task execution configuration
task_always_eager = False
task_eager_propagates = True
task_acks_late = True
worker_prefetch_multiplier = 1

# Task retry configuration
task_default_retry_delay = 60  # 60 seconds
task_max_retries = 3

# Task time limits
task_soft_time_limit = 300  # 5 minutes
task_time_limit = 360       # 6 minutes

# Worker configuration
worker_concurrency = 2
worker_max_tasks_per_child = 1000

# Periodic task schedule
beat_schedule = {
    'auto-categorization-task': {
        'task': 'app.tasks.auto_categorization_task.process_uncategorized_rfqs',
        'schedule': crontab(minute='*/15'),  # Every 15 minutes
        'options': {'expires': 300}  # Task expires after 5 minutes if not picked up
    },
    'seller-matching-task': {
        'task': 'app.tasks.seller_matching_task.process_seller_matching',
        'schedule': crontab(minute='*/30'),  # Every 30 minutes  
        'options': {'expires': 600}  # Task expires after 10 minutes if not picked up
    },
}

# Task routing (optional - for when you have multiple workers)
task_routes = {
    'app.tasks.auto_categorization_task.*': {'queue': 'categorization'},
    'app.tasks.seller_matching_task.*': {'queue': 'seller_matching'},
}