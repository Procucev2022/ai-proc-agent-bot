"""
Daily Category Vector Store Rebuild Task.

This Celery periodic task rebuilds the AutoCategorizationService vector store
(chroma_db/category_items collection) daily by fetching fresh data from the
remote item_category table.

The vector store is used by AutoCategorizationService to perform semantic
similarity search for auto-categorizing RFQ items.

Schedule: Daily at 2:00 AM (configurable in celery_config.py)
"""

import logging
from typing import Dict, Any
from celery import shared_task
from datetime import datetime

from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 300})
def rebuild_category_vector_store(self) -> Dict[str, Any]:
    """
    Rebuild the AutoCategorizationService vector store (category_items collection).

    This task:
    1. Clears the existing category_items collection in ChromaDB
    2. Fetches fresh data from remote item_category table (or local CategoryMapping)
    3. Regenerates all embeddings for auto-categorization

    Returns:
        Dict with rebuild status and statistics
    """
    try:
        settings = get_settings()

        # Check if task is enabled
        if not getattr(settings, 'enable_daily_category_rebuild', True):
            logger.info("Daily category vector rebuild is disabled in settings")
            return {
                "status": "skipped",
                "reason": "daily_category_rebuild_disabled",
                "timestamp": datetime.utcnow().isoformat()
            }

        logger.info("=" * 60)
        logger.info("Starting daily category vector store rebuild")
        logger.info("=" * 60)

        start_time = datetime.utcnow()

        # Import here to avoid circular imports and ensure fresh instance
        from app.services.auto_categorization_service import AutoCategorizationService

        # Create new instance (don't use singleton to ensure fresh connection)
        service = AutoCategorizationService()

        # Get stats before rebuild
        stats_before = service.get_collection_stats()
        logger.info(f"Collection stats before rebuild: {stats_before}")

        # Rebuild the vector store (this clears and repopulates)
        logger.info("Rebuilding category_items collection from database...")
        items_count = service.populate_embeddings_from_db()

        # Get stats after rebuild
        stats_after = service.get_collection_stats()
        logger.info(f"Collection stats after rebuild: {stats_after}")

        # Calculate duration
        end_time = datetime.utcnow()
        duration_seconds = (end_time - start_time).total_seconds()

        result = {
            "status": "completed",
            "items_count": items_count,
            "stats_before": stats_before,
            "stats_after": stats_after,
            "duration_seconds": duration_seconds,
            "data_source": "remote" if settings.enable_remote_categorization else "local",
            "timestamp": end_time.isoformat()
        }

        logger.info("=" * 60)
        logger.info(f"Category vector store rebuild completed successfully!")
        logger.info(f"Items populated: {items_count}")
        logger.info(f"Duration: {duration_seconds:.2f} seconds")
        logger.info("=" * 60)

        return result

    except Exception as e:
        logger.error(f"Category vector store rebuild failed: {str(e)}")
        import traceback
        traceback.print_exc()

        return {
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


def trigger_category_vector_rebuild() -> Dict[str, Any]:
    """
    Manually trigger category vector store rebuild (for testing).

    Usage:
        from app.tasks.daily_category_vector_rebuild_task import trigger_category_vector_rebuild
        result = trigger_category_vector_rebuild()
    """
    logger.info("Manually triggering category vector store rebuild...")
    result = rebuild_category_vector_store.apply_async()
    return {
        "task_id": result.id,
        "status": "triggered",
        "message": "Category vector store rebuild task has been queued"
    }


if __name__ == "__main__":
    # Configure logging for direct execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("Running category vector store rebuild task directly...")
    result = rebuild_category_vector_store()
    print(f"\nResult: {result}")
