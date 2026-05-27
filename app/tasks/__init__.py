"""
Celery background tasks package.

Contains task definitions for auto-categorization, seller matching,
WhatsApp report automation, BFS notifications, and taxonomy building.
"""

# Import all tasks to ensure they are registered with Celery
from .auto_categorization_task import process_uncategorized_rfqs
from .seller_matching_task import process_seller_matching
from .bfs_notification_task import process_bfs_seller_notifications
from .whatsapp_report_automation_task import (
    run_whatsapp_report_automation )
from .taxonomy_build_task import build_taxonomy

__all__ = [
    'process_uncategorized_rfqs',
    'process_seller_matching',
    'process_bfs_seller_notifications',
    'run_whatsapp_report_automation',
    'build_taxonomy',
]