"""
Celery background tasks package.

Contains task definitions for auto-categorization, seller matching, and daily aggregation.
"""

# Import all tasks to ensure they are registered with Celery
from ...auto_categorization_task import process_uncategorized_rfqs
from ...seller_matching_task import process_seller_matching
from .daily_aggregation_task import (
    run_daily_aggregation,
    run_daily_aggregation_range,
    update_rolling_windows
)

__all__ = [
    'process_uncategorized_rfqs',
    'process_seller_matching', 
    'run_daily_aggregation',
    'run_daily_aggregation_range',
    'update_rolling_windows'
]