"""
Daily Category Vector Store Rebuild Task.

This Celery periodic task syncs remote item_category data to the local
category_mappings table, then rebuilds the AutoCategorizationService vector
store (chroma_db/category_items collection).

The category_mappings sync ensures the local table stays up-to-date with the
remote item_category data, which is required for FK constraints when building
the 3-level taxonomy (ClientCategoryMapping → CategoryMapping).

The vector store is used by AutoCategorizationService to perform semantic
similarity search for auto-categorizing RFQ items.

Schedule: Daily at 2:00 AM (configurable in celery_config.py)
"""

import logging
import uuid
from typing import Dict, Any
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from datetime import datetime

from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    queue='vector_store',
    priority=2,  # Low priority - night mode task
    time_limit=14400,       # 4 hrs max (runaway protection - extended for full rebuild)
    soft_time_limit=10800,  # 3 hrs soft limit (increased to allow completion)
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 2, 'countdown': 300}
)
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

        # STEP 0: Sync remote item_category → local category_mappings
        logger.info("Step 0: Syncing remote item_category to local category_mappings...")
        sync_result = _sync_category_mappings()
        if sync_result.get("success"):
            logger.info(f"SUCCESS: Synced {sync_result['inserted_count']} new category mappings "
                        f"({sync_result['total_remote_items']} remote items, "
                        f"{sync_result['unique_categories']} unique categories)")
        else:
            logger.warning(f"Category mappings sync failed: {sync_result.get('error')} — continuing with rebuild")

        # STEP 1: Rebuild vector store
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

    except SoftTimeLimitExceeded:
        logger.error("Category vector rebuild task exceeded soft time limit (3 hours) - stopping gracefully")
        # Don't retry on timeout - just return status to avoid 3x restarts = empty collection for hours
        return {
            "status": "timeout",
            "error": "Task exceeded maximum execution time",
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"Category vector store rebuild failed: {str(e)}")
        import traceback
        traceback.print_exc()

        return {
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


def _sync_category_mappings() -> Dict[str, Any]:
    """
    Sync remote item_category data into the local category_mappings table.

    This ensures the local table has matching records for FK constraints
    when build_3_level_taxonomy creates ClientCategoryMapping cross-references.
    """
    from app.database import get_db_session, get_remote_item_categories, test_remote_connection
    from app.models import CategoryMapping

    settings = get_settings()

    if not settings.enable_remote_categorization:
        return {"success": False, "error": "Remote categorization disabled"}

    if not test_remote_connection():
        return {"success": False, "error": "Remote database connection failed"}

    remote_items = get_remote_item_categories()
    if not remote_items:
        return {"success": False, "error": "No items returned from remote database"}

    logger.info(f"Fetched {len(remote_items)} items from remote item_category table")

    # Group items by category, skip nulls/empty
    category_items = {}
    for item in remote_items:
        category = (item.get('category') or '').strip()
        item_name = (item.get('item') or '').strip()
        if not category or not item_name:
            continue
        if category not in category_items:
            category_items[category] = []
        category_items[category].append(item_name)

    logger.info(f"Found {len(category_items)} unique categories")

    db = get_db_session()
    try:
        # Bulk-load all existing (category, item) pairs to avoid per-row SELECTs
        existing_pairs = set(
            db.query(CategoryMapping.category, CategoryMapping.item).all()
        )
        logger.info(f"Found {len(existing_pairs)} existing category mappings locally")

        total_inserted = 0
        batch = []

        for category, items in category_items.items():
            for item_name in items:
                if (category, item_name) not in existing_pairs:
                    batch.append(CategoryMapping(
                        id=str(uuid.uuid4()),
                        category=category,
                        item=item_name
                    ))
                    total_inserted += 1

                    if len(batch) >= 500:
                        db.add_all(batch)
                        db.commit()
                        batch = []

        if batch:
            db.add_all(batch)
            db.commit()

        logger.info(f"Inserted {total_inserted} new category mappings")

        return {
            "success": True,
            "total_remote_items": len(remote_items),
            "unique_categories": len(category_items),
            "inserted_count": total_inserted
        }

    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        db.close()


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
