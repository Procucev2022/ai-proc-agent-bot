"""
Category Name Sync Task for Hybrid Search.

This Celery periodic task syncs unique category names from the remote database
to the ChromaDB `category_names` collection for hybrid search support.

The category_names collection enables:
- Direct category-to-description matching (complementary to item-based search)
- Hybrid search in EnhancedAutoCategorizationService

Data Source: Remote `item_category` table (same source as auto_categorization_service)

Schedule: Daily at 3:00 AM (configurable in celery_config.py)
"""

import logging
import sys
import os
from typing import Dict, Any
from collections import Counter
from datetime import datetime
from celery import shared_task

import chromadb
import chromadb.utils.embedding_functions as embedding_functions

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 300})
def sync_category_names(self, clear_existing: bool = False) -> Dict[str, Any]:
    """
    Sync category names from remote database to ChromaDB vector store.

    This task:
    1. Fetches unique categories from remote item_category table
    2. Optionally includes categories from local LearningCategoryItem
    3. Creates/updates vector embeddings in ChromaDB category_names collection
    4. Enables hybrid search in EnhancedAutoCategorizationService

    Args:
        clear_existing: If True, clears the collection before rebuilding

    Returns:
        Dict with sync status and statistics
    """
    try:
        settings = get_settings()

        logger.info("=" * 60)
        logger.info("Starting category name sync task")
        logger.info(f"Options: clear_existing={clear_existing}")
        logger.info("=" * 60)

        sync_results = {
            "status": "in_progress",
            "steps_completed": [],
            "steps_failed": [],
            "timestamp": datetime.utcnow().isoformat()
        }

        # STEP 1: Load categories from remote database (PRIMARY source)
        logger.info("Step 1/3: Loading categories from remote database...")
        remote_categories = {}
        try:
            remote_categories = _load_categories_from_remote()
            if remote_categories:
                sync_results["steps_completed"].append("remote_database_load")
                sync_results["remote_category_count"] = len(remote_categories)
                logger.info(f"SUCCESS: Loaded {len(remote_categories)} unique categories from remote DB")
            else:
                logger.warning("No categories found in remote database")
                sync_results["steps_failed"].append({
                    "step": "remote_database_load",
                    "error": "No categories found or remote DB disabled"
                })
        except Exception as e:
            logger.error(f"ERROR: Failed to load from remote database: {e}")
            sync_results["steps_failed"].append({
                "step": "remote_database_load",
                "error": str(e)
            })

        # STEP 2: Load categories from local learning taxonomy (SECONDARY source)
        logger.info("Step 2/3: Loading categories from local learning taxonomy...")
        local_categories = {}
        try:
            local_categories = _load_categories_from_local_db()
            if local_categories:
                sync_results["steps_completed"].append("local_database_load")
                sync_results["local_category_count"] = len(local_categories)
                logger.info(f"SUCCESS: Loaded {len(local_categories)} unique categories from local DB")
            else:
                logger.info("No additional categories from local database")
        except Exception as e:
            logger.warning(f"WARNING: Failed to load from local database: {e}")
            sync_results["steps_failed"].append({
                "step": "local_database_load",
                "error": str(e)
            })

        # Merge categories (remote takes precedence for counts)
        all_categories = {}
        for cat, count in remote_categories.items():
            all_categories[cat] = all_categories.get(cat, 0) + count
        for cat, count in local_categories.items():
            all_categories[cat] = all_categories.get(cat, 0) + count

        if not all_categories:
            logger.error("No categories found from any source!")
            sync_results["status"] = "failed"
            sync_results["error"] = "No categories found from any source"
            return sync_results

        logger.info(f"Total unique categories to embed: {len(all_categories)}")

        # STEP 3: Create embeddings in ChromaDB
        logger.info("Step 3/3: Creating embeddings in ChromaDB...")
        try:
            embedding_result = _create_category_embeddings(
                all_categories,
                clear_existing=clear_existing
            )

            if embedding_result.get("success"):
                sync_results["steps_completed"].append("embedding_creation")
                sync_results["embedding_stats"] = {
                    "total_categories": embedding_result.get("total_categories", 0),
                    "collection_count": embedding_result.get("collection_count", 0)
                }
                logger.info(f"SUCCESS: Created embeddings for {embedding_result.get('total_categories', 0)} categories")
            else:
                error_msg = embedding_result.get("error", "Unknown error")
                logger.error(f"ERROR: Failed to create embeddings: {error_msg}")
                sync_results["steps_failed"].append({
                    "step": "embedding_creation",
                    "error": error_msg
                })
                sync_results["status"] = "partial_failure"
                return sync_results

        except Exception as e:
            logger.error(f"ERROR: Exception creating embeddings: {e}")
            sync_results["steps_failed"].append({
                "step": "embedding_creation",
                "error": str(e)
            })
            sync_results["status"] = "failed"
            return sync_results

        # All steps completed successfully
        sync_results["status"] = "completed"
        sync_results["completion_timestamp"] = datetime.utcnow().isoformat()
        sync_results["total_unique_categories"] = len(all_categories)

        logger.info("=" * 60)
        logger.info("SUCCESS: Category name sync completed!")
        logger.info(f"Total unique categories: {len(all_categories)}")
        logger.info(f"Steps completed: {', '.join(sync_results['steps_completed'])}")
        logger.info("=" * 60)

        return sync_results

    except Exception as e:
        logger.error(f"Critical error in category name sync: {e}")
        return {
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


def _load_categories_from_remote() -> Dict[str, int]:
    """
    Load unique categories from remote item_category table.

    Returns:
        Dict mapping category name to item count
    """
    from app.database import get_remote_item_categories, test_remote_connection
    from app.config import get_settings

    settings = get_settings()

    # Check if remote categorization is enabled
    if not settings.enable_remote_categorization:
        logger.warning("Remote categorization disabled (ENABLE_REMOTE_CATEGORIZATION=false)")
        return {}

    # Test connection
    if not test_remote_connection():
        logger.error("Remote database connection test failed")
        return {}

    # Fetch items from remote database
    logger.info("Fetching from remote item_category table...")
    remote_items = get_remote_item_categories()

    if not remote_items:
        logger.warning("No items returned from remote database")
        return {}

    # Count items per category
    category_counts = Counter()
    for item in remote_items:
        category = (item.get("category") or "").strip()
        if category:
            category_counts[category] += 1

    logger.info(f"Found {len(category_counts)} unique categories from {len(remote_items)} items")
    return dict(category_counts)


def _load_categories_from_local_db() -> Dict[str, int]:
    """
    Load unique categories from local LearningCategoryItem table.

    Returns:
        Dict mapping category name to item count
    """
    from app.database import get_db_session
    from app.models import LearningCategoryItem

    db = get_db_session()
    try:
        items = db.query(LearningCategoryItem).all()

        category_counts = Counter()
        for item in items:
            category = (item.client_category_name or "").strip()
            if category:
                category_counts[category] += 1

        logger.info(f"Found {len(category_counts)} unique categories from local learning taxonomy")
        return dict(category_counts)

    except Exception as e:
        logger.error(f"Error loading from local database: {e}")
        return {}
    finally:
        db.close()


def _create_category_embeddings(
    categories: Dict[str, int],
    clear_existing: bool = False
) -> Dict[str, Any]:
    """
    Create ChromaDB embeddings for category names.

    Args:
        categories: Dict mapping category name to item count
        clear_existing: Whether to clear existing collection

    Returns:
        Dict with success status and statistics
    """
    settings = get_settings()

    try:
        # Connect to ChromaDB server
        chroma_client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port
        )

        # Test connection
        chroma_client.heartbeat()
        logger.info(f"Connected to ChromaDB at {settings.chroma_host}:{settings.chroma_port}")

        embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )

        # Handle existing collection
        if clear_existing:
            try:
                chroma_client.delete_collection("category_names")
                logger.info("Cleared existing 'category_names' collection")
            except Exception:
                logger.info("No existing 'category_names' collection to clear")

        collection = chroma_client.get_or_create_collection(
            name="category_names",
            embedding_function=embedding_function
        )

        # Prepare data for embedding
        documents = []
        metadatas = []
        ids = []

        for i, (category_name, item_count) in enumerate(sorted(categories.items())):
            # Document is the category name itself
            documents.append(category_name)

            metadatas.append({
                "type": "category_name",
                "category_name": category_name,
                "item_count": item_count,
                "source": "remote_db"
            })

            ids.append(f"cat_{i}")

        # Add to ChromaDB in batches
        logger.info(f"Creating embeddings for {len(documents)} category names...")
        batch_size = 100
        for i in range(0, len(documents), batch_size):
            end_idx = min(i + batch_size, len(documents))
            collection.add(
                documents=documents[i:end_idx],
                metadatas=metadatas[i:end_idx],
                ids=ids[i:end_idx]
            )
            logger.info(f"Added batch {i//batch_size + 1}/{(len(documents) + batch_size - 1)//batch_size}")

        # Verify
        final_count = collection.count()
        logger.info(f"Collection 'category_names' now has {final_count} entries")

        # Log top categories
        sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:10]
        logger.info("Top 10 categories by item count:")
        for cat, count in sorted_cats:
            logger.info(f"  {cat}: {count} items")

        return {
            "success": True,
            "total_categories": len(documents),
            "collection_count": final_count
        }

    except Exception as e:
        logger.error(f"Error creating embeddings: {e}")
        return {
            "success": False,
            "error": str(e)
        }


# Manual trigger function for testing
def trigger_category_name_sync(clear_existing: bool = False) -> Dict[str, Any]:
    """
    Manually trigger category name sync (for testing).

    Args:
        clear_existing: If True, clears collection before rebuilding

    Usage:
        from app.tasks.category_name_sync_task import trigger_category_name_sync

        # Normal sync
        result = trigger_category_name_sync()

        # Full rebuild
        result = trigger_category_name_sync(clear_existing=True)
    """
    logger.info(f"Manually triggering category name sync (clear_existing={clear_existing})...")
    result = sync_category_names.apply_async(kwargs={'clear_existing': clear_existing})
    return {
        "task_id": result.id,
        "status": "triggered",
        "clear_existing": clear_existing,
        "message": "Category name sync task has been queued"
    }


if __name__ == "__main__":
    import argparse

    # Configure logging for direct execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='Sync category names to ChromaDB')
    parser.add_argument('--clear-existing', action='store_true',
                       help='Clear collection before rebuilding')
    args = parser.parse_args()

    print("Running category name sync task directly...")
    print(f"Options: clear_existing={args.clear_existing}")
    result = sync_category_names(clear_existing=args.clear_existing)
    print(f"\nResult: {result}")
