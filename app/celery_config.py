"""
Celery configuration settings.

This module contains configuration for Celery workers, task routing,
and periodic task scheduling for all background tasks.
"""

from celery.schedules import crontab
import os

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "t")


# ============================================================================
# TASK ENABLE/DISABLE CONTROL - Configured via environment variables or defaults
# ============================================================================
ENABLE_AUTO_CATEGORIZATION = _env_bool('ENABLE_AUTO_CATEGORIZATION', True)
ENABLE_VECTOR_STORE_SYNC = _env_bool('ENABLE_VECTOR_STORE_SYNC', True)
ENABLE_SELLER_MATCHING = _env_bool('ENABLE_SELLER_MATCHING', True)
ENABLE_DAILY_AGGREGATION = _env_bool('ENABLE_DAILY_AGGREGATION', False)
ENABLE_WHATSAPP_REPORT_AUTOMATION = _env_bool('ENABLE_WHATSAPP_REPORT_AUTOMATION', True)
ENABLE_DAILY_CATEGORY_REBUILD = _env_bool('ENABLE_DAILY_CATEGORY_REBUILD', True)
ENABLE_CATEGORY_NAME_SYNC = _env_bool('ENABLE_CATEGORY_NAME_SYNC', True)
ENABLE_LOG_CLEANUP = _env_bool('ENABLE_LOG_CLEANUP', True)
ENABLE_BFS_NOTIFICATION = _env_bool('ENABLE_BFS_NOTIFICATION', True)
ENABLE_TAXONOMY_BUILD = _env_bool('ENABLE_TAXONOMY_BUILD', True)
# ============================================================================

# Basic Celery configuration
broker_url = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/1')
result_backend = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/1')

# Task serialization
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
enable_utc = True

# Task execution configuration
task_always_eager = False
task_eager_propagates = True
task_acks_late = True  # CRITICAL: Acknowledge AFTER task finishes - enables re-queue on crash
worker_prefetch_multiplier = 1  # CRITICAL: Hold ONE task at a time - prevents BFS blocking

# Task retry configuration
task_default_retry_delay = 60  # 60 seconds
task_max_retries = 3

# ============================================================================
# CRITICAL FIX: REMOVED GLOBAL TIME LIMITS
# Global task_soft_time_limit and task_time_limit were killing long-running
# taxonomy tasks after 10-12 minutes. Each task now declares its own limits.
# ============================================================================
# PHILOSOPHY: Time limits are for RUNAWAY PROTECTION only, not expected runtime
# 
# Strategy per task type:
#   SHORT tasks (BFS, cleanup):     Strict limits (OK to kill if stuck)
#   MEDIUM tasks (categorization):  Moderate limits + retry enabled
#   LONG tasks (taxonomy, vector):  VERY HIGH limits + resumable + checkpointed
#
# Time limits per task type (set in each @shared_task decorator):
#   bfs_notification       soft=300,  hard=600    (5-10 min - strict)
#   auto_categorization    soft=900,  hard=1200   (15-20 min - moderate)
#   seller_matching        soft=900,  hard=1200   (15-20 min - moderate)
#   vector_store_sync      soft=7200, hard=9000   (2-2.5 hrs - relaxed)
#   daily_category_rebuild soft=7200, hard=9000   (2-2.5 hrs - relaxed + resumable)
#   whatsapp_report        soft=3600, hard=4500   (1-1.25 hrs - moderate)
#   taxonomy_build         soft=14400, hard=18000 (4-5 hrs - VERY HIGH + resumable)
#   log_cleanup            soft=600,  hard=900    (10-15 min - strict)
#
# NOTE: For long AI/ML tasks, we prioritize COMPLETION over SPEED
# - OpenAI API can spike from 5s to 60s per call
# - Network issues may require retries
# - ChromaDB operations vary with data size
# ============================================================================

# Worker configuration
# Note: Actual value set via --concurrency flag in docker_entrypoint_celery_dynamic.sh:
#   reserved_worker: --concurrency=1
#   dynamic_pool:    --concurrency=5
worker_concurrency = int(os.getenv('CELERY_CONCURRENCY', 6))
worker_max_tasks_per_child = 500  # Restart after 500 tasks to prevent memory leaks
worker_max_tasks_per_child_jitter = 50  # Add randomness to prevent simultaneous restarts
worker_disable_rate_limits = False

# Result backend settings
result_expires = 3600  # 1 hour
result_persistent = True

# ============================================================================
# BROKER TRANSPORT - enables Redis priority queue ordering
# This ensures that within the same queue, higher-priority messages
# are dequeued first. Works alongside dedicated queue isolation.
# ============================================================================
broker_transport_options = {
    'priority_steps': list(range(10)),  # 0-9 priority levels (higher number = higher priority)
    'queue_order_strategy': 'priority',  # Process by priority
}

# ============================================================================
# BEAT SCHEDULE - All times in UTC (IST = UTC + 5:30)
# ============================================================================
# Periodic task schedule (Beat scheduler)
# Pipeline order: auto-categorization -> vector_store_sync -> seller_matching
beat_schedule = {}

# Auto-categorization: Runs every 15 minutes to process uncategorized RFQs
if ENABLE_AUTO_CATEGORIZATION:
    beat_schedule['auto-categorization-task'] = {
        'task': 'app.tasks.auto_categorization_task.process_uncategorized_rfqs',
        'schedule': crontab(minute='*/15'),
        'options': {
            'expires': 600,
            'queue': 'categorization',
            'priority': 6,  # High priority - frequent daytime task
        }
    }

# Vector Store Sync: Runs hourly at minute 0
if ENABLE_VECTOR_STORE_SYNC:
    beat_schedule['vector-store-sync-task'] = {
        'task': 'app.tasks.vector_store_sync_task.sync_vector_store',
        'schedule': crontab(minute=0, hour='*/1'),  # Every hour
        'options': {
            'expires': 3600,
            'queue': 'vector_store',
            'priority': 3,  # Low-medium priority
        }
    }

# Seller Matching: Runs hourly to match sellers with RFQs (currently disabled)
if ENABLE_SELLER_MATCHING:
    beat_schedule['seller-matching-task'] = {
        'task': 'app.tasks.seller_matching_task.process_seller_matching',
        'schedule': crontab(minute=0, hour='*/2'),  # Every 2 hour at minute 0
        'options': {
            'expires': 7200,
            'queue': 'seller_matching',
            'priority': 5,  # Medium priority
        }
    }

# Daily Category Vector Rebuild: Daily at 2:00 AM IST (20:30 UTC previous day)
if ENABLE_DAILY_CATEGORY_REBUILD:
    beat_schedule['daily-category-vector-rebuild-task'] = {
        'task': 'app.tasks.daily_category_vector_rebuild_task.rebuild_category_vector_store',
        'schedule': crontab(minute=30, hour=20),  # 2:00 AM IST
        'options': {
            'expires': 7200,
            'queue': 'vector_store',
            'priority': 2,  # Low priority - night mode task
        }
    }

# Category Name Sync: Daily at 3:00 AM IST (21:30 UTC previous day)
if ENABLE_CATEGORY_NAME_SYNC:
    beat_schedule['category-name-sync-task'] = {
        'task': 'app.tasks.category_name_sync_task.sync_category_names',
        'schedule': crontab(minute=0, hour=21),  # 3:00 AM IST (after rebuild)
        'options': {
            'expires': 7200,
            'queue': 'vector_store',
            'priority': 2,  # Low priority - runs after category rebuild
        }
    }

# Log Cleanup: Daily at 2:00 AM IST (20:30 UTC previous day)
if ENABLE_LOG_CLEANUP:
    beat_schedule['log-cleanup-task'] = {
        'task': 'app.tasks.log_cleanup_task.cleanup_logs',
        'schedule': crontab(minute=30, hour=20),  # 2:00 AM IST
        'options': {
            'expires': 7200,
            'queue': 'maintenance',
            'priority': 1,  # Lowest priority
        }
    }

# BFS Notifications: Every 5 minutes (TODO: Change to every 30 minutes when stable)
if ENABLE_BFS_NOTIFICATION:
    beat_schedule['bfs-notification-task'] = {
        'task': 'app.tasks.bfs_notification_task.process_bfs_seller_notifications',
        'schedule': crontab(minute='*/5'),
        'options': {
            'expires': 270,  # Expire after 4.5 min - drop old if new arrives
            'queue': 'bfs_notification',
            'priority': 9,  # HIGHEST priority - urgent notifications
        }
    }

# WhatsApp Report Automation: Daily at 7:30 AM IST (02:00 UTC)
if ENABLE_WHATSAPP_REPORT_AUTOMATION:
    beat_schedule['whatsapp-report-automation-task'] = {
        'task': 'app.tasks.whatsapp_report_automation_task.run_whatsapp_report_automation',
        'schedule': crontab(minute=0, hour=2),  # 7:30 AM IST
        'options': {
            'expires': 7200,
            'queue': 'report_automation',
            'priority': 5,  # Medium priority
        }
    }


if ENABLE_TAXONOMY_BUILD:
    beat_schedule['taxonomy-build-task'] = {
        'task': 'app.tasks.taxonomy_build_task.build_taxonomy',
        'schedule': crontab(minute=30, hour=19),  # 1:00 AM IST
        'kwargs': {
            'batch_size': 50,
            'process_all': True,
            'parallel': True,  # Use parallel processing across all workers
            'resume': True  # Resume from last checkpoint on restart
        },
        'options': {
            'expires': 14400,  # 4 hours - drop if not started within 4 hrs
            'queue': 'taxonomy_build',
            'priority': 1,  # LOWEST priority - bulk processing
        }
    }


# ============================================================================
# TASK ROUTING - Each task family goes to a dedicated queue.
# The reserved_worker only listens on bfs_notification.
# The dynamic_pool listens on all queues EXCEPT bfs_notification,
# so long-running tasks physically cannot block BFS regardless of timing.
# ============================================================================
task_routes = {
    'app.tasks.bfs_notification_task.*':            {'queue': 'bfs_notification', 'priority': 9},
    'app.tasks.auto_categorization_task.*':         {'queue': 'categorization',   'priority': 6},
    'app.tasks.seller_matching_task.*':             {'queue': 'seller_matching',   'priority': 5},
    'app.tasks.whatsapp_report_automation_task.*':  {'queue': 'report_automation', 'priority': 5},
    'app.tasks.vector_store_sync_task.*':           {'queue': 'vector_store',      'priority': 3},
    'app.tasks.daily_category_vector_rebuild_task.*': {'queue': 'vector_store',    'priority': 2},
    'app.tasks.category_name_sync_task.*':          {'queue': 'vector_store',      'priority': 2},
    'app.tasks.taxonomy_build_task.*':              {'queue': 'taxonomy_build',    'priority': 1},
    'app.tasks.log_cleanup_task.*':                 {'queue': 'maintenance',       'priority': 1},
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