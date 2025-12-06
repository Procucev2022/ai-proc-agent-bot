"""
Celery configuration settings.

This module contains configuration for Celery workers, task routing,
and periodic task scheduling for all background tasks.
"""

from celery.schedules import crontab
import os

# Basic Celery configuration
broker_url = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6380/0')
result_backend = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6380/0')

# Task serialization
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
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
task_soft_time_limit = 600   # 10 minutes
task_time_limit = 720        # 12 minutes

# Worker configuration
worker_concurrency = 4
worker_max_tasks_per_child = 1000
worker_disable_rate_limits = False

# Result backend settings
result_expires = 3600  # 1 hour
result_persistent = True

# Periodic task schedule (Beat scheduler)
# Pipeline order: auto-categorization -> vector_store_sync -> seller_matching
beat_schedule = {
    # Auto-categorization task - runs every 15 minutes
    # This runs first to categorize new RFQs
    'auto-categorization-task': {
        'task': 'app.tasks.auto_categorization_task.process_uncategorized_rfqs',
        'schedule': crontab(minute='*/15'),
        'options': {
            'expires': 600,  # Task expires after 10 minutes if not picked up
            'queue': 'categorization'
        }
    },

    # Vector store sync task - runs every hour at minute 5
    # Runs after auto-categorization to update seller embeddings
    'vector-store-sync-task': {
        'task': 'app.tasks.vector_store_sync_task.sync_vector_store',
        'schedule': crontab(minute=5),  # Runs at :05 every hour
        'options': {
            'expires': 1800,  # Task expires after 30 minutes if not picked up
            'queue': 'vector_store'
        }
    },

    # Seller matching task - runs every hour at minute 10
    # Runs after vector store sync to match sellers to categorized RFQs
    'seller-matching-task': {
        'task': 'app.tasks.seller_matching_task.process_seller_matching',
        'schedule': crontab(minute=10),  # Runs at :10 every hour
        'options': {
            'expires': 1800,  # Task expires after 30 minutes if not picked up
            'queue': 'seller_matching'
        }
    },

    # Test cron job - runs every 5 minutes
    'test-cron-job': {
        'task': 'app.tasks.test_cron_task.test_cron_job',
        'schedule': crontab(minute='*/5'),
        'options': {
            'expires': 300,  # Task expires after 5 minutes if not picked up
            'queue': 'default'
        }
    }
}

# Task routing - distribute tasks across different queues
task_routes = {
    'app.tasks.auto_categorization_task.*': {'queue': 'categorization'},
    'app.tasks.vector_store_sync_task.*': {'queue': 'vector_store'},
    'app.tasks.seller_matching_task.*': {'queue': 'seller_matching'},
    'app.tasks.test_cron_task.*': {'queue': 'default'},
}

# Queue configuration
task_default_queue = 'default'
task_create_missing_queues = True

# Monitoring and logging
worker_send_task_events = True
task_send_sent_event = True

# Security settings
worker_hijack_root_logger = False
worker_log_color = False

# Beat scheduler settings
beat_scheduler = 'celery.beat:PersistentScheduler'
beat_schedule_filename = 'celerybeat-schedule'
beat_sync_every = 1