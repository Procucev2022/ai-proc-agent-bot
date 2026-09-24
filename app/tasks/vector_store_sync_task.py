"""
Vector Store Sync Task for Hybrid Seller Matching.

This Celery periodic task keeps the vector store fresh by syncing seller data
from the remote database and updating ChromaDB embeddings.

The vector store is used by EnhancedSellerMatchingService (Phase 1 of hybrid approach)
to perform fast semantic discovery of seller candidates.

Workflow:
1. Fetch latest sellers from remote DB via SellerDataAdapter
2. Map seller categories to learning taxonomy via OpenAI
3. Generate embeddings and update ChromaDB vector store

Schedule: Daily at 2:00 AM (configurable in celery_config.py)
"""

import logging
import sys
import os
from typing import Dict, Any
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from datetime import datetime

from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    queue='vector_store',
    priority=3,  # Low-medium priority
    time_limit=10800,       # 3 hrs max (runaway protection - extended to prevent premature kills)
    soft_time_limit=7200,   # 2 hrs soft limit (increased to allow completion)
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 2, 'countdown': 300}
)
def sync_vector_store(self, clear_existing: bool = False, cleanup_buyer_mappings: bool = False):
    """
    Sync seller data to vector store for enhanced semantic matching.

    This task:
    1. (Optional) Cleans up buyer mappings from SellerLearningMapping table
    2. Maps sellers to learning categories using OpenAI
    3. Creates/updates vector embeddings in ChromaDB
    4. Enables Phase 1 of hybrid seller matching

    The vector store is used by EnhancedSellerMatchingService to quickly
    find semantically relevant seller candidates, which are then filtered
    by SellerRecommendationService business rules.

    Args:
        clear_existing: If True, clears the ChromaDB collection before rebuilding
        cleanup_buyer_mappings: If True, removes buyer mappings from SellerLearningMapping

    Returns:
        Dict with sync status and statistics
    """
    try:
        # Add project root to path for imports (inside function to avoid polluting global path)
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
        
        settings = get_settings()

        # Check if vector store sync is enabled
        if not getattr(settings, 'enable_vector_store_sync', True):
            logger.info("Vector store sync is disabled in settings")
            return {
                "status": "skipped",
                "reason": "vector_store_sync_disabled",
                "timestamp": datetime.utcnow().isoformat()
            }

        logger.info("=" * 60)
        logger.info("Starting vector store sync task")
        logger.info(f"Options: clear_existing={clear_existing}, cleanup_buyer_mappings={cleanup_buyer_mappings}")
        logger.info("=" * 60)

        sync_results = {
            "status": "in_progress",
            "steps_completed": [],
            "steps_failed": [],
            "timestamp": datetime.utcnow().isoformat()
        }

        # STEP 0 (Optional): Cleanup buyer mappings from SellerLearningMapping
        if cleanup_buyer_mappings:
            logger.info("Step 0: Cleaning up buyer mappings from SellerLearningMapping...")
            try:
                cleanup_result = _cleanup_buyer_mappings()
                if cleanup_result.get("success"):
                    sync_results["steps_completed"].append("buyer_mapping_cleanup")
                    sync_results["cleanup_stats"] = cleanup_result
                    logger.info(f"SUCCESS: Removed {cleanup_result.get('deleted_count', 0)} buyer mappings")
                else:
                    logger.warning(f"Buyer mapping cleanup failed: {cleanup_result.get('error')}")
                    sync_results["steps_failed"].append({
                        "step": "buyer_mapping_cleanup",
                        "error": cleanup_result.get("error")
                    })
            except Exception as e:
                logger.warning(f"Buyer mapping cleanup exception: {str(e)}")
                sync_results["steps_failed"].append({
                    "step": "buyer_mapping_cleanup",
                    "error": str(e)
                })

        # STEP 1: Map sellers to learning categories
        logger.info("Step 1/2: Mapping sellers to learning categories...")
        try:
            mapping_result = _run_seller_category_mapping()

            if mapping_result.get("success"):
                sync_results["steps_completed"].append("seller_category_mapping")
                sync_results["mapping_stats"] = {
                    "processed_sellers": mapping_result.get("processed_count", 0),
                    "created_mappings": mapping_result.get("created_mappings", 0),
                    "existing_mappings": mapping_result.get("existing_mappings", 0),
                    "errors": len(mapping_result.get("errors", []))
                }
                logger.info(f"SUCCESS: Step 1 complete: Mapped {mapping_result.get('processed_count', 0)} sellers")
            else:
                error_msg = mapping_result.get("error", "Unknown error")
                logger.error(f"ERROR: Step 1 failed: {error_msg}")
                sync_results["steps_failed"].append({
                    "step": "seller_category_mapping",
                    "error": error_msg
                })
                # Don't continue if mapping fails
                sync_results["status"] = "partial_failure"
                return sync_results

        except Exception as e:
            logger.error(f"ERROR: Step 1 exception: {str(e)}")
            sync_results["steps_failed"].append({
                "step": "seller_category_mapping",
                "error": str(e)
            })
            sync_results["status"] = "failed"
            return sync_results

        # STEP 2: Create vector embeddings in ChromaDB. Step 1 above only touches
        # MySQL and OpenAI, so it still runs with vector search off.
        if not getattr(settings, 'enable_vector_search', True):
            logger.info("Vector search is disabled, skipping Step 2 (ChromaDB embeddings)")
            sync_results["steps_skipped"] = ["vector_embedding_creation"]
            sync_results["status"] = "completed"
            sync_results["completion_timestamp"] = datetime.utcnow().isoformat()
            return sync_results

        logger.info("Step 2/2: Creating vector embeddings in ChromaDB...")
        try:
            embedding_result = _run_vector_embedding_creation(clear_existing=clear_existing)

            if embedding_result.get("success"):
                sync_results["steps_completed"].append("vector_embedding_creation")
                sync_results["embedding_stats"] = {
                    "total_items": embedding_result.get("total_items", 0),
                    "seller_mappings": embedding_result.get("seller_mappings", 0),
                    "learning_items": embedding_result.get("learning_items", 0)
                }
                logger.info(f"SUCCESS: Step 2 complete: Created {embedding_result.get('total_items', 0)} embeddings")
            else:
                error_msg = embedding_result.get("error", "Unknown error")
                logger.error(f"ERROR: Step 2 failed: {error_msg}")
                sync_results["steps_failed"].append({
                    "step": "vector_embedding_creation",
                    "error": error_msg
                })
                sync_results["status"] = "partial_failure"
                return sync_results

        except Exception as e:
            logger.error(f"ERROR: Step 2 exception: {str(e)}")
            sync_results["steps_failed"].append({
                "step": "vector_embedding_creation",
                "error": str(e)
            })
            sync_results["status"] = "partial_failure"
            return sync_results

        # All steps completed successfully
        sync_results["status"] = "completed"
        sync_results["completion_timestamp"] = datetime.utcnow().isoformat()

        logger.info("=" * 60)
        logger.info("SUCCESS: Vector store sync completed successfully!")
        logger.info(f"Steps completed: {', '.join(sync_results['steps_completed'])}")
        logger.info("=" * 60)

        return sync_results

    except SoftTimeLimitExceeded:
        logger.error("Vector store sync task exceeded soft time limit (2 hours) - stopping gracefully")
        # Don't retry on timeout - just return status to avoid 3x restarts = 3x OpenAI cost
        return {
            "status": "timeout",
            "error": "Task exceeded maximum execution time",
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"Critical error in vector store sync: {str(e)}")
        return {
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


def _cleanup_buyer_mappings() -> Dict[str, Any]:
    """
    Remove buyer mappings from SellerLearningMapping table.

    Buyers are organizations with self_client = 1. Their mappings should not
    exist in the seller learning mapping table.

    Returns:
        Dict with cleanup results
    """
    from app.database import get_db_session, execute_remote_query
    from app.models import SellerLearningMapping

    db = None
    try:
        db = get_db_session()

        # Get buyer UUIDs from remote database
        buyer_query = """
            SELECT uuid FROM organization WHERE self_client = 1
        """
        buyer_results = execute_remote_query(buyer_query)
        buyer_ids = [r['uuid'] for r in buyer_results]

        if not buyer_ids:
            logger.info("No buyers found in remote database")
            return {
                "success": True,
                "deleted_count": 0,
                "buyer_count": 0
            }

        logger.info(f"Found {len(buyer_ids)} buyers to check for mappings")

        # Delete mappings for buyers
        deleted_count = db.query(SellerLearningMapping).filter(
            SellerLearningMapping.seller_id.in_(buyer_ids)
        ).delete(synchronize_session='fetch')

        db.commit()

        logger.info(f"Deleted {deleted_count} buyer mappings from SellerLearningMapping")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "buyer_count": len(buyer_ids)
        }

    except Exception as e:
        logger.error(f"Error cleaning up buyer mappings: {str(e)}")
        db.rollback()
        return {
            "success": False,
            "error": str(e),
            "deleted_count": 0
        }
    finally:
        db.close()


def _run_seller_category_mapping() -> Dict[str, Any]:
    """
    Run seller to learning category mapping (Step 2 of setup).

    Uses OpenAI to map seller categories to the 3-level learning taxonomy.
    Creates an async wrapper to properly await the async OpenAI calls.
    """
    try:
        import asyncio

        logger.info("Executing seller category mapping with async support...")

        # Run the async mapping function
        result = asyncio.run(_async_map_sellers_to_categories(batch_size=10))

        return result

    except ImportError as e:
        logger.error(f"Failed to import required modules: {e}")
        return {
            "success": False,
            "error": f"Import error: {str(e)}",
            "processed_count": 0
        }
    except Exception as e:
        logger.error(f"Error running seller category mapping: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "processed_count": 0
        }


async def _async_map_sellers_to_categories(batch_size: int = 10) -> Dict[str, Any]:
    """
    Async version of seller category mapping that properly awaits OpenAI calls.

    This is a simplified version that works with the Celery task context.
    """
    from app.database import get_db_session
    from app.models import LearningCategory, SellerLearningMapping
    from app.services.openai_service import OpenAIService
    from app.services.seller_data_adapter import SellerDataAdapter
    from sqlalchemy import and_
    import uuid
    import time
    import asyncio

    db = get_db_session()
    openai_service = OpenAIService()

    try:
        # Get sellers from remote database
        logger.info("Fetching sellers from remote database...")
        adapter = SellerDataAdapter()

        if not adapter.test_connection():
            return {
                "success": False,
                "error": "Remote database connection failed",
                "processed_count": 0
            }

        sellers = adapter.get_sellers_from_remote()
        logger.info(f"Processing {len(sellers)} real sellers from remote database")

        if not sellers:
            return {
                "success": False,
                "error": "No sellers found",
                "processed_count": 0
            }

        results = {
            "success": True,
            "total_sellers": len(sellers),
            "processed_count": 0,
            "created_mappings": 0,
            "existing_mappings": 0,
            "cached_mappings": 0,
            "openai_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "errors": []
        }

        start_time = time.time()

        # Load all learning categories once
        all_learning_categories = db.query(LearningCategory).all()
        if not all_learning_categories:
            logger.warning("No learning categories found! Run build_3_level_taxonomy.py first")
            return {"success": False, "error": "No learning categories found", "processed_count": 0}

        categories_for_ai = [
            {
                "id": cat.id,
                "level_1_category": cat.level_1_category,
                "level_2_category": cat.level_2_category,
                "level_3_category": cat.level_3_category
            }
            for cat in all_learning_categories
        ]
        logger.info(f"Loaded {len(categories_for_ai)} learning categories for mapping")

        # ── Phase 1: Collect unique unmapped category strings ──
        already_mapped_pairs = set()  # (seller_id, category)
        unmapped_categories = set()

        for seller in sellers:
            seller_categories = seller.categories if isinstance(seller.categories, list) else []
            for category in seller_categories:
                existing = db.query(SellerLearningMapping).filter(
                    and_(
                        SellerLearningMapping.seller_id == seller.seller_id,
                        SellerLearningMapping.original_category == category
                    )
                ).first()
                if existing:
                    already_mapped_pairs.add((seller.seller_id, category))
                    results["existing_mappings"] += 1
                else:
                    unmapped_categories.add(category)

        logger.info(f"Found {len(unmapped_categories)} unique unmapped categories to process via OpenAI")
        logger.info(f"Already mapped: {results['existing_mappings']} seller-category pairs")

        # ── Phase 2: Map unique categories in batches via OpenAI ──
        category_mapping_cache: Dict[str, Dict] = {}
        batch_categories = list(unmapped_categories)
        categories_per_batch = 10  # small batches for accuracy

        for batch_start in range(0, len(batch_categories), categories_per_batch):
            batch = batch_categories[batch_start:batch_start + categories_per_batch]
            batch_num = batch_start // categories_per_batch + 1
            total_batches = (len(batch_categories) + categories_per_batch - 1) // categories_per_batch
            logger.info(f"Processing batch {batch_num}/{total_batches}: {len(batch)} categories")

            batch_results = await openai_service.map_seller_categories_batch(
                seller_categories=batch,
                existing_categories=categories_for_ai
            )

            results["openai_calls"] += 1
            for cat_name, mapping_result in batch_results.items():
                if mapping_result.get("success"):
                    category_mapping_cache[cat_name] = mapping_result
                else:
                    error_msg = f"Failed to map '{cat_name}': {mapping_result.get('error', 'Unknown error')}"
                    logger.error(f"  ERROR: {error_msg}")
                    results["errors"].append(error_msg)

            # Track tokens once per batch
            first_result = next(iter(batch_results.values()), {})
            batch_tokens = first_result.get("token_usage", {})
            results["total_input_tokens"] += batch_tokens.get("input_tokens", 0)
            results["total_output_tokens"] += batch_tokens.get("output_tokens", 0)
            results["total_tokens"] += batch_tokens.get("total_tokens", 0)

        logger.info(f"Phase 2 complete: {len(category_mapping_cache)} categories mapped, {results['openai_calls']} OpenAI calls")

        # ── Phase 3: Apply cached results to all sellers (DB only, fast) ──
        for i, seller in enumerate(sellers):
            try:
                seller_categories = seller.categories if isinstance(seller.categories, list) else []
                if not seller_categories:
                    continue

                for category in seller_categories:
                    if (seller.seller_id, category) in already_mapped_pairs:
                        continue

                    mapping_result = category_mapping_cache.get(category)
                    if not mapping_result:
                        continue

                    selected_category = mapping_result["selected_category"]
                    learning_category = db.query(LearningCategory).filter(
                        LearningCategory.id == selected_category["id"]
                    ).first()

                    if learning_category:
                        seller_mapping = SellerLearningMapping(
                            mapping_id=str(uuid.uuid4()),
                            seller_id=seller.seller_id,
                            original_category=category,
                            learning_category_id=learning_category.id,
                            level_1_category=learning_category.level_1_category,
                            level_2_category=learning_category.level_2_category,
                            level_3_category=learning_category.level_3_category,
                            confidence_score=mapping_result.get("similarity_score", 0.8),
                            ai_reasoning=mapping_result.get("reasoning", ""),
                            mapping_method="openai_async_category_selection"
                        )
                        db.add(seller_mapping)
                        results["created_mappings"] += 1
                        results["cached_mappings"] += 1
                    else:
                        error_msg = f"Learning category {selected_category['id']} not found for seller {seller.seller_id}"
                        logger.error(f"  ERROR: {error_msg}")
                        results["errors"].append(error_msg)

                results["processed_count"] += 1

                # Commit in batches
                if results["processed_count"] % batch_size == 0:
                    db.commit()
                    logger.debug(f"Committed batch — {results['processed_count']}/{len(sellers)} sellers processed")

            except Exception as e:
                error_msg = f"Exception processing seller {seller.seller_id}: {str(e)}"
                logger.error(f"ERROR: {error_msg}")
                results["errors"].append(error_msg)
                db.rollback()

        db.commit()  # Final commit

        results["processing_time_ms"] = int((time.time() - start_time) * 1000)

        logger.info("=" * 60)
        logger.info(f"Processed: {results['processed_count']}/{results['total_sellers']}")
        logger.info(f"New mappings: {results['created_mappings']}")
        logger.info(f"Existing: {results['existing_mappings']}")
        logger.info(f"Cached (no OpenAI call): {results['cached_mappings']}")
        logger.info(f"OpenAI calls: {results['openai_calls']}")
        logger.info(f"Total tokens: {results['total_tokens']} (input: {results['total_input_tokens']}, output: {results['total_output_tokens']})")
        logger.info(f"Errors: {len(results['errors'])}")
        logger.info("=" * 60)

        return results

    except Exception as e:
        db.rollback()
        logger.error(f"Critical error: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "processed_count": 0
        }
    finally:
        db.close()


def _run_vector_embedding_creation(clear_existing: bool = False) -> Dict[str, Any]:
    """
    Run vector embedding creation (Step 3 of setup).

    Creates/updates vector embeddings in ChromaDB for fast semantic search.

    Args:
        clear_existing: If True, clears the ChromaDB collection before rebuilding
    """
    try:
        # Add Setup directory to path for imports
        setup_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Setup/3-level_vector_store'))
        if setup_path not in sys.path:
            sys.path.insert(0, setup_path)

        from create_category_embeddings import create_unified_vector_store

        logger.info(f"Creating vector embeddings in ChromaDB (clear_existing={clear_existing})...")
        success = create_unified_vector_store(clear_existing=clear_existing)

        if success:
            return {
                "success": True,
                "total_items": "updated",  # Actual count from create_unified_vector_store
                "seller_mappings": "updated",
                "learning_items": "updated"
            }
        else:
            return {
                "success": False,
                "error": "Vector store creation returned False"
            }

    except ImportError as e:
        logger.error(f"Failed to import embedding script: {e}")
        return {
            "success": False,
            "error": f"Import error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Error creating vector embeddings: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


# Manual trigger function for testing
def trigger_vector_store_sync(clear_existing: bool = False, cleanup_buyer_mappings: bool = False):
    """
    Manually trigger vector store sync (for testing).

    Args:
        clear_existing: If True, clears ChromaDB collection before rebuilding
        cleanup_buyer_mappings: If True, removes buyer mappings from SellerLearningMapping

    Usage:
        from app.tasks.vector_store_sync_task import trigger_vector_store_sync

        # Normal sync
        result = trigger_vector_store_sync()

        # Full rebuild with cleanup
        result = trigger_vector_store_sync(clear_existing=True, cleanup_buyer_mappings=True)
    """
    logger.info(f"Manually triggering vector store sync (clear_existing={clear_existing}, cleanup_buyer_mappings={cleanup_buyer_mappings})...")
    result = sync_vector_store.apply_async(kwargs={
        'clear_existing': clear_existing,
        'cleanup_buyer_mappings': cleanup_buyer_mappings
    })
    return {
        "task_id": result.id,
        "status": "triggered",
        "clear_existing": clear_existing,
        "cleanup_buyer_mappings": cleanup_buyer_mappings,
        "message": "Vector store sync task has been queued"
    }


if __name__ == "__main__":
    import argparse

    # Configure logging for direct execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description='Run vector store sync task')
    parser.add_argument('--clear-existing', action='store_true',
                       help='Clear ChromaDB collection before rebuilding')
    parser.add_argument('--cleanup-buyer-mappings', action='store_true',
                       help='Remove buyer mappings from SellerLearningMapping table')
    args = parser.parse_args()

    print("Running vector store sync task directly...")
    print(f"Options: clear_existing={args.clear_existing}, cleanup_buyer_mappings={args.cleanup_buyer_mappings}")
    result = sync_vector_store(
        clear_existing=args.clear_existing,
        cleanup_buyer_mappings=args.cleanup_buyer_mappings
    )
    print(f"\nResult: {result}")
