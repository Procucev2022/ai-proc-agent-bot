"""
Daily category mappings sync task.

This Celery periodic task syncs remote item_category data to the local
category_mappings table. The table must stay up-to-date with the remote
item_category data because it backs FK constraints when building the 3-level
taxonomy (ClientCategoryMapping -> CategoryMapping).

Schedule: Daily at 2:00 AM IST (configurable in celery_config.py)
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
    Sync remote item_category rows into the local category_mappings table.

    Returns:
        Dict with sync status and statistics
    """
    try:
        settings = get_settings()

        # Check if task is enabled
        if not getattr(settings, 'enable_daily_category_rebuild', True):
            logger.info("Daily category mappings sync is disabled in settings")
            return {
                "status": "skipped",
                "reason": "daily_category_rebuild_disabled",
                "timestamp": datetime.utcnow().isoformat()
            }

        logger.info("Starting daily category mappings sync")
        start_time = datetime.utcnow()

        sync_result = _sync_category_mappings()
        if sync_result.get("success"):
            logger.info(f"SUCCESS: Synced {sync_result['inserted_count']} new category mappings "
                        f"({sync_result['total_remote_items']} remote items, "
                        f"{sync_result['unique_categories']} unique categories)")
        else:
            logger.warning(f"Category mappings sync failed: {sync_result.get('error')}")

        end_time = datetime.utcnow()
        return {
            "status": "completed" if sync_result.get("success") else "failed",
            "category_mappings_sync": sync_result,
            "duration_seconds": (end_time - start_time).total_seconds(),
            "timestamp": end_time.isoformat()
        }

    except SoftTimeLimitExceeded:
        logger.error("Category mappings sync task exceeded soft time limit (3 hours) - stopping gracefully")
        # Don't retry on timeout - just return status
        return {
            "status": "timeout",
            "error": "Task exceeded maximum execution time",
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"Category mappings sync failed: {str(e)}")
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
    Manually trigger the category mappings sync (for testing).

    Usage:
        from app.tasks.daily_category_vector_rebuild_task import trigger_category_vector_rebuild
        result = trigger_category_vector_rebuild()
    """
    logger.info("Manually triggering category mappings sync...")
    result = rebuild_category_vector_store.apply_async()
    return {
        "task_id": result.id,
        "status": "triggered",
        "message": "Category mappings sync task has been queued"
    }


if __name__ == "__main__":
    # Configure logging for direct execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("Running category mappings sync task directly...")
    result = rebuild_category_vector_store()
    print(f"\nResult: {result}")
