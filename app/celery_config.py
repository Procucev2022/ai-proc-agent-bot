"""
Celery configuration settings.

This module contains configuration for Celery workers, task routing,
and periodic task scheduling for all background tasks.
"""

from celery.schedules import crontab
import os

# ============================================================================
# TASK ENABLE/DISABLE CONTROL - Comment/Uncomment to enable/disable tasks
# ============================================================================
ENABLE_AUTO_CATEGORIZATION = True
ENABLE_VECTOR_STORE_SYNC = True
ENABLE_SELLER_MATCHING = False
ENABLE_DAILY_AGGREGATION = False
ENABLE_WHATSAPP_REPORT_AUTOMATION = True  # WhatsApp report automation orchestrator
ENABLE_DAILY_CATEGORY_REBUILD = True  # Daily rebuild of category_items vector store
ENABLE_CATEGORY_NAME_SYNC = True  # Daily sync of category_names collection from remote DB
ENABLE_LOG_CLEANUP = True  # Daily log cleanup and archival
ENABLE_BFS_NOTIFICATION = True  # BFS seller bid notifications
# ============================================================================

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
beat_schedule = {}

if ENABLE_AUTO_CATEGORIZATION:
    beat_schedule['auto-categorization-task'] = {
        'task': 'app.tasks.auto_categorization_task.process_uncategorized_rfqs',
        'schedule': crontab(minute='*/15'),
        'options': {
            'expires': 600,
            'queue': 'categorization'
        }
    }

if ENABLE_VECTOR_STORE_SYNC:
    beat_schedule['vector-store-sync-task'] = {
        'task': 'app.tasks.vector_store_sync_task.sync_vector_store',
        'schedule': crontab(minute=0, hour='*/1'),
        'options': {
            'expires': 3600,
            'queue': 'vector_store'
        }
    }

if ENABLE_SELLER_MATCHING:
    beat_schedule['seller-matching-task'] = {
        'task': 'app.tasks.seller_matching_task.process_seller_matching',
        'schedule': crontab(minute=0, hour='*/1'),
        'options': {
            'expires': 3600,
            'queue': 'seller_matching'
        }
    }

if ENABLE_DAILY_CATEGORY_REBUILD:
    beat_schedule['daily-category-vector-rebuild-task'] = {
        'task': 'app.tasks.daily_category_vector_rebuild_task.rebuild_category_vector_store',
        'schedule': crontab(minute=0, hour=2),  # Daily at 2:00 AM
        'options': {
            'expires': 7200,
            'queue': 'vector_store'
        }
    }

if ENABLE_CATEGORY_NAME_SYNC:
    beat_schedule['category-name-sync-task'] = {
        'task': 'app.tasks.category_name_sync_task.sync_category_names',
        'schedule': crontab(minute=0, hour=3),  # Daily at 3:00 AM (after category rebuild)
        'options': {
            'expires': 7200,
            'queue': 'vector_store'
        }
    }

if ENABLE_LOG_CLEANUP:
    beat_schedule['log-cleanup-task'] = {
        'task': 'app.tasks.log_cleanup_task.cleanup_logs',
        'schedule': crontab(minute=0, hour=2),  # Daily at 2:00 AM
        'options': {
            'expires': 7200,
            'queue': 'maintenance'
        }
    }

if ENABLE_BFS_NOTIFICATION:
    beat_schedule['bfs-notification-task'] = {
        'task': 'app.tasks.bfs_notification_task.process_bfs_seller_notifications',
        'schedule': crontab(minute='*/5'),  # TODO: Change to run every 30 minutes
        'options': {
            'expires': 300,
            'queue': 'bfs_notification'
        }
    }

if ENABLE_WHATSAPP_REPORT_AUTOMATION:
    beat_schedule['whatsapp-report-automation-task'] = {
        'task': 'app.tasks.whatsapp_report_automation_task.run_whatsapp_report_automation',
        'schedule': crontab(minute=20, hour=5),  # Daily at 11:00 AM IST (01:30 AM UTC)
        'options': {
            'expires': 7200,
            'queue': 'report_automation'
        }
    }


# Task routing - distribute tasks across different queues
task_routes = {
    'app.tasks.auto_categorization_task.*': {'queue': 'categorization'},
    'app.tasks.vector_store_sync_task.*': {'queue': 'vector_store'},
    'app.tasks.seller_matching_task.*': {'queue': 'seller_matching'},
    'app.tasks.daily_category_vector_rebuild_task.*': {'queue': 'vector_store'},
    'app.tasks.category_name_sync_task.*': {'queue': 'vector_store'},
    'app.tasks.log_cleanup_task.*': {'queue': 'maintenance'},
    'app.tasks.bfs_notification_task.*': {'queue': 'bfs_notification'},
    'app.tasks.whatsapp_report_automation_task.*': {'queue': 'report_automation'},
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