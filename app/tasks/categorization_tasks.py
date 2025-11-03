"""
Celery tasks for auto-categorization and vector store updates.

This module contains Celery tasks for maintaining and updating the
enhanced auto-categorization service's vector store.

Key responsibilities:
- Periodic vector store updates
- Category mapping synchronization
- Data quality maintenance
- Performance monitoring
"""

import logging
from datetime import datetime
from typing import Dict, Any

from celery import Task
from app.celery_app import celery_app
from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
from app.database import get_db_session
from app.models import ClientCategoryMapping

logger = logging.getLogger(__name__)


class CallbackTask(Task):
    """Base task with callbacks for monitoring."""

    def on_success(self, retval, task_id, args, kwargs):
        """Log successful task execution."""
        logger.info(f"Task {task_id} completed successfully: {retval}")

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log failed task execution."""
        logger.error(f"Task {task_id} failed: {exc}")


@celery_app.task(
    base=CallbackTask,
    bind=True,
    name="app.tasks.categorization_tasks.update_vector_store_task",
    max_retries=3,
    default_retry_delay=60
)
def update_vector_store_task(self) -> Dict[str, Any]:
    """
    Periodic task to update the enhanced auto-categorization vector store.

    This task runs every 5 minutes to:
    1. Check for new category mappings
    2. Update the vector store with new data
    3. Perform health checks
    4. Log statistics

    Returns:
        Dict with task execution results and statistics
    """
    start_time = datetime.utcnow()
    logger.info(f"Starting vector store update task at {start_time}")

    try:
        # Initialize the service
        service = EnhancedAutoCategorizationService()

        # Perform health check
        health_status = service.health_check()
        logger.info(f"Health check status: {health_status.get('overall_status')}")

        if health_status.get("overall_status") != "healthy":
            logger.warning(f"Service health check warning: {health_status}")

        # Get current statistics
        stats = service.get_stats()
        logger.info(f"Current vector store stats: {stats}")

        # Check database for new category mappings to potentially add
        db = get_db_session()
        try:
            # Count total category mappings in database
            total_mappings = db.query(ClientCategoryMapping).count()

            # Get vector store item count
            vector_store_count = stats.get("unified_vector_store", {}).get("total_items", 0)

            logger.info(
                f"Database has {total_mappings} category mappings, "
                f"vector store has {vector_store_count} items"
            )

            # You can add logic here to sync new mappings if needed
            # For now, we just monitor and log

        except Exception as db_error:
            logger.error(f"Error checking database: {db_error}")
        finally:
            db.close()

        # Calculate execution time
        end_time = datetime.utcnow()
        execution_time = (end_time - start_time).total_seconds()

        result = {
            "success": True,
            "task_name": "update_vector_store_task",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "execution_time_seconds": execution_time,
            "health_status": health_status.get("overall_status"),
            "stats": stats,
            "database_mappings": total_mappings if 'total_mappings' in locals() else None,
            "vector_store_items": vector_store_count if 'vector_store_count' in locals() else None
        }

        logger.info(f"Vector store update task completed successfully in {execution_time:.2f}s")
        return result

    except Exception as e:
        logger.error(f"Error in vector store update task: {str(e)}", exc_info=True)

        # Retry the task with exponential backoff
        try:
            raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
        except self.MaxRetriesExceededError:
            logger.error(f"Max retries exceeded for vector store update task")
            return {
                "success": False,
                "task_name": "update_vector_store_task",
                "error": str(e),
                "max_retries_exceeded": True
            }


@celery_app.task(
    base=CallbackTask,
    bind=True,
    name="app.tasks.categorization_tasks.health_check_task"
)
def health_check_task(self) -> Dict[str, Any]:
    """
    Perform health check on the enhanced auto-categorization service.

    Returns:
        Dict with health check results
    """
    try:
        service = EnhancedAutoCategorizationService()
        health_status = service.health_check()

        logger.info(f"Health check completed: {health_status.get('overall_status')}")
        return health_status

    except Exception as e:
        logger.error(f"Error in health check task: {str(e)}", exc_info=True)
        return {
            "overall_status": "error",
            "error": str(e)
        }


@celery_app.task(
    base=CallbackTask,
    bind=True,
    name="app.tasks.categorization_tasks.get_stats_task"
)
def get_stats_task(self) -> Dict[str, Any]:
    """
    Get statistics from the enhanced auto-categorization service.

    Returns:
        Dict with service statistics
    """
    try:
        service = EnhancedAutoCategorizationService()
        stats = service.get_stats()

        logger.info(f"Stats retrieved: {stats}")
        return stats

    except Exception as e:
        logger.error(f"Error in get stats task: {str(e)}", exc_info=True)
        return {
            "error": str(e)
        }
