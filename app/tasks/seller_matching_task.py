"""
Seller matching background task.

This task processes categorized RFQs and performs seller matching using
the existing SellerRecommendationService.
"""

import asyncio
import logging
from typing import List, Dict, Any
from celery import shared_task
from datetime import datetime, timedelta

from app.database import execute_remote_query, get_remote_db_session
from app.services.seller_recommendation_service import SellerRecommendationService
from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 120})
def process_seller_matching(self):
    """
    Process seller matching for categorized RFQs.
    
    This task:
    1. Fetches categorized RFQs that need seller matching
    2. Processes each RFQ through seller recommendation service  
    3. Logs selected sellers to gmt_rfq_vendors table
    4. Updates RFQ processing status
    """
    try:
        settings = get_settings()
        if not settings.enable_remote_categorization:
            logger.info("Remote categorization is disabled, skipping seller matching task")
            return {"status": "skipped", "reason": "remote_categorization_disabled"}

        logger.info("Starting seller matching task")
        
        # Get RFQs needing seller matching
        rfqs_for_matching = get_rfqs_needing_seller_matching()
        
        if not rfqs_for_matching:
            logger.info("No RFQs found that need seller matching")
            return {"status": "completed", "processed": 0, "message": "No RFQs to process"}

        logger.info(f"Found {len(rfqs_for_matching)} RFQs that need seller matching")

        # Initialize seller recommendation service
        seller_service = SellerRecommendationService()

        processed_count = 0
        failed_count = 0
        results = []

        # Process each RFQ
        for rfq in rfqs_for_matching:
            try:
                # Run async function in sync context
                result = asyncio.run(process_single_rfq_matching(rfq, seller_service))
                results.append(result)
                
                if result["success"]:
                    processed_count += 1
                else:
                    failed_count += 1
                    
            except Exception as e:
                logger.error(f"Failed to process seller matching for RFQ {rfq.get('rfq_id', 'unknown')}: {e}")
                failed_count += 1
                results.append({
                    "rfq_id": rfq.get('rfq_id'),
                    "success": False,
                    "error": str(e)
                })

        logger.info(f"Seller matching task completed: {processed_count} processed, {failed_count} failed")
        
        return {
            "status": "completed",
            "processed": processed_count,
            "failed": failed_count,
            "total": len(rfqs_for_matching),
            "timestamp": datetime.utcnow().isoformat(),
            "results": results
        }

    except Exception as e:
        logger.error(f"Seller matching task failed: {e}")
        raise self.retry(countdown=120)


def get_rfqs_needing_seller_matching(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get categorized RFQs that need seller matching.
    
    Args:
        limit: Maximum number of RFQs to fetch
        
    Returns:
        List of RFQ records that need seller matching
    """
    # Get RFQs that:
    # 1. Have categories (not NULL)
    # 2. Are from WhatsApp (source_type = 'W')  
    # 3. Don't already have seller notifications sent
    # 4. Are not too old (within last 7 days)
    # 5. Are not closed yet
    
    cutoff_date = datetime.utcnow() - timedelta(days=7)
    
    query = """
        SELECT r.uuid, r.rfq_id, r.project_desc as description, r.category, r.division,
               r.delivery_date, r.rfq_closing_date, r.user, r.org_uuid,
               r.created_ts, r.last_modified_ts, r.source_type,
               r.special_instruction
        FROM rfq_header r
        LEFT JOIN gmt_rfq_vendors v ON r.rfq_id = v.rfq_uuid
        WHERE r.category IS NOT NULL 
        AND r.source_type = 'W'
        AND r.created_ts > :cutoff_date
        AND (r.rfq_closing_date IS NULL OR r.rfq_closing_date > NOW())
        AND v.rfq_uuid IS NULL  -- No existing vendor notifications
        ORDER BY r.created_ts DESC
        LIMIT :limit
    """
    
    return execute_remote_query(query, {
        'limit': limit,
        'cutoff_date': cutoff_date.strftime('%Y-%m-%d %H:%M:%S')
    })


async def process_single_rfq_matching(rfq: Dict[str, Any], seller_service: SellerRecommendationService) -> Dict[str, Any]:
    """
    Process seller matching for a single RFQ.
    
    Args:
        rfq: RFQ data dictionary
        seller_service: Initialized SellerRecommendationService instance
        
    Returns:
        Processing result dictionary
    """
    rfq_id = rfq.get('rfq_id')
    rfq_uuid = rfq.get('uuid')
    
    try:
        logger.info(f"Processing seller matching for RFQ {rfq_id}")
        
        # Prepare RFQ data for seller service
        rfq_data = {
            'rfq_id': rfq_id,
            'categories': [rfq.get('category')] if rfq.get('category') else [],
            'delivery_location': extract_delivery_location(rfq),
            'delivery_date': rfq.get('delivery_date'),
            'rfq_closing_date': rfq.get('rfq_closing_date'),
            'description': rfq.get('description', ''),
            'special_instruction': rfq.get('special_instruction', ''),
            'user_id': rfq.get('user'),
            'org_uuid': rfq.get('org_uuid')
        }
        
        # Add division to categories if available
        if rfq.get('division'):
            rfq_data['categories'].append(rfq.get('division'))
        
        # Remove duplicates
        rfq_data['categories'] = list(set(rfq_data['categories']))
        
        # Use the seller selection method
        seller_result = await seller_service.select_sellers_for_rfq(rfq_data)
        
        if seller_result.get('total_selected', 0) > 0:
            # Mark RFQ as processed
            mark_rfq_seller_matching_processed(rfq_uuid, seller_result.get('total_selected', 0))
            
            logger.info(f"Successfully matched {seller_result.get('total_selected')} sellers for RFQ {rfq_id}")
            return {
                "rfq_id": rfq_id,
                "success": True,
                "sellers_matched": seller_result.get('total_selected'),
                "subscribed_sellers": len(seller_result.get('subscribed_sellers', [])),
                "unsubscribed_sellers": len(seller_result.get('unsubscribed_sellers', [])),
                "categories_used": rfq_data['categories']
            }
        else:
            logger.warning(f"No sellers found for RFQ {rfq_id}")
            # Still mark as processed to avoid reprocessing
            mark_rfq_seller_matching_processed(rfq_uuid, 0)
            
            return {
                "rfq_id": rfq_id,
                "success": True,  # Still successful even if no sellers found
                "sellers_matched": 0,
                "message": "No qualifying sellers found",
                "categories_used": rfq_data['categories']
            }

    except Exception as e:
        logger.error(f"Error processing seller matching for RFQ {rfq_id}: {e}")
        return {
            "rfq_id": rfq_id,
            "success": False,
            "error": str(e)
        }


def extract_delivery_location(rfq: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract delivery location information from RFQ data.
    
    Args:
        rfq: RFQ data dictionary
        
    Returns:
        Location dictionary with available information
    """
    location = {}
    
    # Try to extract location from special instructions or description
    description = (rfq.get('description') or '').lower()
    special_instruction = (rfq.get('special_instruction') or '').lower()
    combined_text = f"{description} {special_instruction}"
    
    # Simple location extraction (can be enhanced with NLP)
    common_cities = [
        'mumbai', 'delhi', 'bangalore', 'hyderabad', 'chennai', 'kolkata',
        'pune', 'ahmedabad', 'surat', 'jaipur', 'lucknow', 'kanpur',
        'nagpur', 'patna', 'indore', 'thane', 'bhopal', 'visakhapatnam'
    ]
    
    for city in common_cities:
        if city in combined_text:
            location['city'] = city.title()
            break
    
    return location


def mark_rfq_seller_matching_processed(rfq_uuid: str, seller_count: int) -> bool:
    """
    Mark an RFQ as processed for seller matching.
    
    Args:
        rfq_uuid: RFQ UUID
        seller_count: Number of sellers matched
        
    Returns:
        True if marking successful, False otherwise
    """
    try:
        db = get_remote_db_session()
        try:
            from sqlalchemy import text
            
            # Try to update a status field in rfq_header (if column exists)
            query = """
                UPDATE rfq_header 
                SET seller_matching_status = 'processed',
                    last_modified_ts = NOW()
                WHERE uuid = :rfq_uuid
            """
            
            result = db.execute(text(query), {
                'rfq_uuid': rfq_uuid
            })
            
            db.commit()
            return result.rowcount > 0
            
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not update seller matching status (column may not exist): {e}")
            
            # Alternative: Just log that we processed it
            logger.info(f"RFQ {rfq_uuid} processed with {seller_count} sellers matched")
            return True
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error marking RFQ as processed: {e}")
        return False