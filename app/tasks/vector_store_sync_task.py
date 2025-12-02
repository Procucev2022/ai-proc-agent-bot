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
from datetime import datetime

# Add project root to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 300})
def sync_vector_store(self):
    """
    Sync seller data to vector store for enhanced semantic matching.

    This task:
    1. Maps sellers to learning categories using OpenAI
    2. Creates/updates vector embeddings in ChromaDB
    3. Enables Phase 1 of hybrid seller matching

    The vector store is used by EnhancedSellerMatchingService to quickly
    find semantically relevant seller candidates, which are then filtered
    by SellerRecommendationService business rules.

    Returns:
        Dict with sync status and statistics
    """
    try:
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
        logger.info("=" * 60)

        sync_results = {
            "status": "in_progress",
            "steps_completed": [],
            "steps_failed": [],
            "timestamp": datetime.utcnow().isoformat()
        }

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

        # STEP 2: Create vector embeddings in ChromaDB
        logger.info("Step 2/2: Creating vector embeddings in ChromaDB...")
        try:
            embedding_result = _run_vector_embedding_creation()

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

    except Exception as e:
        logger.error(f"Critical error in vector store sync: {str(e)}")
        return {
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


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
            "errors": []
        }

        start_time = time.time()
        batch_count = 0

        for i, seller in enumerate(sellers):
            try:
                logger.info(f"Processing seller {i+1}/{len(sellers)}: {seller.seller_name}")

                seller_categories = seller.categories if isinstance(seller.categories, list) else []
                if not seller_categories:
                    logger.warning(f"No categories for seller {seller.seller_name}")
                    continue

                for category in seller_categories:
                    try:
                        # Check if mapping already exists
                        existing_mapping = db.query(SellerLearningMapping).filter(
                            and_(
                                SellerLearningMapping.seller_id == seller.seller_id,
                                SellerLearningMapping.original_category == category
                            )
                        ).first()

                        if existing_mapping:
                            logger.info(f"  Category '{category}' already mapped")
                            results["existing_mappings"] += 1
                            continue

                        # Get existing learning categories
                        existing_categories = db.query(LearningCategory).all()

                        if not existing_categories:
                            logger.warning("No learning categories found! Run build_3_level_taxonomy.py first")
                            continue

                        # Convert to list for OpenAI
                        categories_for_ai = [
                            {
                                "id": cat.id,
                                "level_1_category": cat.level_1_category,
                                "level_2_category": cat.level_2_category,
                                "level_3_category": cat.level_3_category
                            }
                            for cat in existing_categories
                        ]

                        logger.info(f"  Mapping '{category}' using OpenAI (async)...")

                        # ASYNC CALL - properly await the OpenAI service
                        mapping_result = await openai_service.map_seller_category_to_existing_learning(
                            seller_category=category,
                            existing_categories=categories_for_ai,
                            seller_name=seller.seller_name,
                            location_info=seller.location
                        )

                        if mapping_result.get("success"):
                            selected_category = mapping_result["selected_category"]

                            learning_category = db.query(LearningCategory).filter(
                                LearningCategory.id == selected_category["id"]
                            ).first()

                            if learning_category:
                                category_path = f"{learning_category.level_1_category} > {learning_category.level_2_category} > {learning_category.level_3_category}"
                                logger.info(f"  SUCCESS: Mapped to: {category_path}")

                                # Update usage frequency
                                learning_category.usage_frequency += 1

                                # Create mapping
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
                            else:
                                error_msg = f"Learning category {selected_category['id']} not found"
                                logger.error(f"  ERROR: {error_msg}")
                                results["errors"].append(error_msg)
                        else:
                            error_msg = f"Failed to map '{category}': {mapping_result.get('error', 'Unknown error')}"
                            logger.error(f"  ERROR: {error_msg}")
                            results["errors"].append(error_msg)

                    except Exception as e:
                        error_msg = f"Exception mapping '{category}': {str(e)}"
                        logger.error(f"  ERROR: {error_msg}")
                        results["errors"].append(error_msg)

                results["processed_count"] += 1
                db.commit()

                batch_count += 1
                if batch_count >= batch_size:
                    logger.info(f"Completed batch of {batch_size} sellers. Pausing...")
                    await asyncio.sleep(3)  # Async sleep for rate limiting
                    batch_count = 0

            except Exception as e:
                error_msg = f"Exception processing seller {seller.seller_id}: {str(e)}"
                logger.error(f"ERROR: {error_msg}")
                results["errors"].append(error_msg)
                db.rollback()

        results["processing_time_ms"] = int((time.time() - start_time) * 1000)

        logger.info("=" * 60)
        logger.info(f"Processed: {results['processed_count']}/{results['total_sellers']}")
        logger.info(f"New mappings: {results['created_mappings']}")
        logger.info(f"Existing: {results['existing_mappings']}")
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


def _run_vector_embedding_creation() -> Dict[str, Any]:
    """
    Run vector embedding creation (Step 3 of setup).

    Creates/updates vector embeddings in ChromaDB for fast semantic search.
    """
    try:
        # Add Setup directory to path for imports
        setup_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../Setup/3-level_vector_store'))
        if setup_path not in sys.path:
            sys.path.insert(0, setup_path)

        from create_category_embeddings import create_unified_vector_store

        logger.info("Creating vector embeddings in ChromaDB...")
        # Don't clear existing on periodic sync (clear_existing=False)
        # This preserves existing embeddings and only updates changed ones
        success = create_unified_vector_store(clear_existing=False)

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
def trigger_vector_store_sync():
    """
    Manually trigger vector store sync (for testing).

    Usage:
        from app.tasks.vector_store_sync_task import trigger_vector_store_sync
        result = trigger_vector_store_sync()
    """
    logger.info("Manually triggering vector store sync...")
    result = sync_vector_store.apply_async()
    return {
        "task_id": result.id,
        "status": "triggered",
        "message": "Vector store sync task has been queued"
    }


if __name__ == "__main__":
    # Configure logging for direct execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("Running vector store sync task directly...")
    result = sync_vector_store()
    print(f"\nResult: {result}")
