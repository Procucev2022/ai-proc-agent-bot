"""
Auto-categorization background task.

This task processes uncategorized RFQs from the remote database and applies
auto-categorization using the existing AutoCategorizationService.
"""

import logging
import asyncio
from typing import List, Dict, Any
from celery import shared_task
from datetime import datetime

from app.database import execute_remote_query, get_remote_db_session
from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def process_uncategorized_rfqs(self):
    """
    Process uncategorized RFQs from the remote database.
    
    This task:
    1. Fetches RFQs where category IS NULL and source_type = 'W'
    2. Processes each RFQ through auto-categorization
    3. Updates the category field in the remote database
    """
    try:
        settings = get_settings()
        if not settings.enable_remote_categorization:
            logger.info("Remote categorization is disabled, skipping task")
            return {"status": "skipped", "reason": "remote_categorization_disabled"}

        logger.info("Starting auto-categorization task")
        
        # Get uncategorized items
        uncategorized_items = get_uncategorized_items()
        
        if not uncategorized_items:
            logger.info("No uncategorized items found")
            return {"status": "completed", "processed": 0, "message": "No items to process"}

        logger.info(f"Found {len(uncategorized_items)} uncategorized items to process")

        # Initialize enhanced auto-categorization service
        auto_cat_service = EnhancedAutoCategorizationService()

        processed_count = 0
        failed_count = 0
        results = []

        for item in uncategorized_items:
            try:
                result = asyncio.run(process_single_item(item, auto_cat_service))
                results.append(result)

                if result["success"]:
                    processed_count += 1
                else:
                    failed_count += 1

            except Exception as e:
                logger.error(f"Failed to process item {item.get('uuid', 'unknown')} from RFQ {item.get('rfq_id', 'unknown')}: {e}")
                failed_count += 1
                results.append({
                    "item_uuid": item.get('uuid'),
                    "rfq_id": item.get('rfq_id'),
                    "success": False,
                    "error": str(e)
                })

        logger.info(f"Auto-categorization task completed: {processed_count} processed, {failed_count} failed")
        
        return {
            "status": "completed",
            "processed": processed_count,
            "failed": failed_count,
            "total": len(uncategorized_items),
            "timestamp": datetime.utcnow().isoformat(),
            "results": results
        }

    except Exception as e:
        logger.error(f"Auto-categorization task failed: {e}")
        raise self.retry(countdown=60)


def get_uncategorized_items(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get uncategorized items from RFQs with source_type = 'W'.
    
    Args:
        limit: Maximum number of items to fetch
        
    Returns:
        List of RFQ item records that need categorization
    """
    query = """
        SELECT i.uuid, i.description, i.rfq_uuid, h.rfq_id, h.user
        FROM rfq_items i
        JOIN rfq_header h ON i.rfq_uuid = h.uuid
        WHERE i.category IS NULL 
        AND h.source_type = 'W'
        AND i.description IS NOT NULL
        ORDER BY i.created_ts DESC
        LIMIT :limit
    """
    
    return execute_remote_query(query, {'limit': limit})


async def process_single_item(item: Dict[str, Any], auto_cat_service: EnhancedAutoCategorizationService) -> Dict[str, Any]:
    """
    Process a single item for auto-categorization.

    Args:
        item: Item data dictionary
        auto_cat_service: Initialized EnhancedAutoCategorizationService instance

    Returns:
        Processing result dictionary
    """
    item_uuid = item.get('uuid')
    rfq_id = item.get('rfq_id')
    description = item.get('description', '')
    user_id = item.get('user', 'system')

    try:
        logger.info(f"Processing item {item_uuid} from RFQ {rfq_id} for auto-categorization")

        # Categorize the item
        result = await auto_cat_service.categorize_item(
            item_description=description,
            user_id=user_id,
            rfq_id=rfq_id
        )
        
        if result.get('success') and result.get('client_category'):
            category = result['client_category']
            
            # Update category in rfq_items table
            success = update_rfq_item_category(item_uuid, category)
            
            if success:
                logger.info(f"Successfully categorized item {item_uuid} as '{category}'")
                return {
                    "item_uuid": item_uuid,
                    "rfq_id": rfq_id,
                    "success": True,
                    "category": category
                }
            else:
                logger.error(f"Failed to update category for item {item_uuid}")
                return {
                    "item_uuid": item_uuid,
                    "rfq_id": rfq_id,
                    "success": False,
                    "error": "Database update failed"
                }
        else:
            logger.warning(f"Could not determine category for item {item_uuid}")
            return {
                "item_uuid": item_uuid,
                "rfq_id": rfq_id,
                "success": False,
                "error": "Categorization failed"
            }

    except Exception as e:
        logger.error(f"Error processing item {item_uuid}: {e}")
        return {
            "item_uuid": item_uuid,
            "rfq_id": rfq_id,
            "success": False,
            "error": str(e)
        }


def get_rfq_items(rfq_uuid: str) -> List[Dict[str, Any]]:
    """
    Get items for a specific RFQ from the rfq_items table.
    
    Args:
        rfq_uuid: RFQ UUID
        
    Returns:
        List of RFQ items
    """
    try:
        query = """
            SELECT uuid, item_desc, description, quantity, unit_measure,
                   created_ts, last_modified_ts
            FROM rfq_items 
            WHERE rfq_uuid = :rfq_uuid
            ORDER BY created_ts
        """
        
        return execute_remote_query(query, {'rfq_uuid': rfq_uuid})
    except Exception as e:
        logger.warning(f"Could not fetch items for RFQ {rfq_uuid}: {e}")
        return []


def update_rfq_item_category(item_uuid: str, category: str) -> bool:
    """
    Update the category field for an RFQ item in the remote database.
    
    Args:
        item_uuid: Item UUID
        category: Category to set
        
    Returns:
        True if update successful, False otherwise
    """
    try:
        db = get_remote_db_session()
        try:
            from sqlalchemy import text
            
            query = """
                UPDATE rfq_items 
                SET category = :category, last_modified_ts = NOW()
                WHERE uuid = :item_uuid
            """
            
            result = db.execute(text(query), {
                'item_uuid': item_uuid,
                'category': category
            })
            
            db.commit()
            return result.rowcount > 0
            
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update item category: {e}")
            return False
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error updating item category: {e}")
        return False


if __name__ == "__main__":
    """Run the auto-categorization task directly."""
    import logging

    logging.basicConfig(level=logging.INFO)

    print("Running auto-categorization task...")
    result = process_uncategorized_rfqs()

    print(f"Result: {result}")
    print(f"Status: {result.get('status')}")
    print(f"Processed: {result.get('processed', 0)}")
    print(f"Failed: {result.get('failed', 0)}")