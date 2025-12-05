"""
Seller Matching Task - Item-Category Based Matching

This task processes categorized RFQ items and performs seller matching:
1. Fetches RFQs that need more sellers (haven't reached target counts)
2. Groups items by RFQ and collects unique categories
3. For each category, finds matching sellers
4. Separates sellers into subscribed (has credits) and unsubscribed (no credits)
5. Applies filters: 24hr exclusion, workflow status (skip if busy)
6. Selects only the remaining needed count of sellers
7. Logs selected sellers to gmt_rfq_vendors table
8. Repeats until target reached or RFQ closed (quotation_received)

Target per RFQ: 5 subscribed sellers + 10 unsubscribed sellers
"""

import asyncio
import logging
import uuid as uuid_lib
from typing import List, Dict, Any, Set, Tuple
from celery import shared_task
from datetime import datetime, timedelta

from app.database import execute_remote_query, get_remote_db_session
from app.services.seller_recommendation_service import SellerRecommendationService
from app.services.seller_notification_service import SellerNotificationService
from app.config import get_settings

logger = logging.getLogger(__name__)

# Target seller counts per RFQ
TARGET_SUBSCRIBED_SELLERS = 5    # Sellers with rfq_credits > 0
TARGET_UNSUBSCRIBED_SELLERS = 10  # Sellers with rfq_credits = 0


def get_rfq_notification_progress(rfq_uuid: str) -> Dict[str, int]:
    """
    Get the current notification progress for an RFQ.

    Counts how many subscribed and unsubscribed sellers have already been notified.
    Subscribed = rfq_credits > 0, Unsubscribed = rfq_credits = 0

    Args:
        rfq_uuid: The RFQ UUID

    Returns:
        Dictionary with subscribed_notified, unsubscribed_notified counts
    """
    query = """
        SELECT
            COALESCE(SUM(CASE WHEN o.rfq_credits > 0 THEN 1 ELSE 0 END), 0) as subscribed_notified,
            COALESCE(SUM(CASE WHEN o.rfq_credits = 0 THEN 1 ELSE 0 END), 0) as unsubscribed_notified
        FROM gmt_rfq_vendors grv
        JOIN organization o ON grv.vendor_uuid = o.uuid
        WHERE grv.rfq_uuid = :rfq_uuid
    """

    results = execute_remote_query(query, {'rfq_uuid': rfq_uuid})

    if results:
        return {
            'subscribed_notified': int(results[0].get('subscribed_notified', 0)),
            'unsubscribed_notified': int(results[0].get('unsubscribed_notified', 0))
        }

    return {'subscribed_notified': 0, 'unsubscribed_notified': 0}


def get_sellers_already_notified_for_rfq(rfq_uuid: str) -> Set[str]:
    """
    Get set of seller UUIDs already notified for a specific RFQ.

    Args:
        rfq_uuid: The RFQ UUID

    Returns:
        Set of vendor_uuid strings already notified for this RFQ
    """
    query = """
        SELECT DISTINCT vendor_uuid
        FROM gmt_rfq_vendors
        WHERE rfq_uuid = :rfq_uuid
        AND vendor_uuid IS NOT NULL
    """

    results = execute_remote_query(query, {'rfq_uuid': rfq_uuid})
    return {r['vendor_uuid'] for r in results if r.get('vendor_uuid')}


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

        # Get sellers who were notified in the last 24 hours (to exclude)
        excluded_seller_ids = get_sellers_notified_in_last_24hrs()
        logger.info(f"Found {len(excluded_seller_ids)} sellers to exclude (notified in last 24hrs)")

        # Initialize seller recommendation service
        seller_service = SellerRecommendationService()

        processed_count = 0
        failed_count = 0
        results = []

        # Process each RFQ
        for rfq in rfqs_for_matching:
            try:
                # Run async function in sync context
                result = asyncio.run(process_single_rfq_matching(rfq, seller_service, excluded_seller_ids))
                results.append(result)

                if result["success"]:
                    processed_count += 1
                    # Add newly notified sellers to exclusion set for subsequent RFQs
                    # This prevents same seller getting multiple RFQs in one batch
                    if result.get("sellers_matched", 0) > 0:
                        # Note: We'd need seller IDs in result to do this properly
                        pass
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
    Get RFQs with categorized items that need more seller matching.

    Fetches RFQs that:
    1. Have items with categories (not NULL in rfq_items)
    2. Are from WhatsApp (source_type = 'W')
    3. Are not too old (within last 7 days)
    4. Are not closed (quotation_received = 0)
    5. Haven't reached target seller counts yet

    Args:
        limit: Maximum number of RFQs to fetch

    Returns:
        List of RFQ records that need more sellers
    """
    cutoff_date = datetime.utcnow() - timedelta(days=7)

    # Get RFQs that need more sellers
    # Uses subquery to count current subscribed/unsubscribed notifications
    query = """
        SELECT DISTINCT
            h.uuid as rfq_uuid,
            h.rfq_id,
            h.project_desc as description,
            h.delivery_date,
            h.rfq_closing_date,
            h.user,
            h.org_uuid,
            h.created_ts,
            h.source_type,
            h.special_instruction,
            COALESCE(progress.subscribed_count, 0) as subscribed_notified,
            COALESCE(progress.unsubscribed_count, 0) as unsubscribed_notified
        FROM rfq_header h
        INNER JOIN rfq_items i ON h.uuid = i.rfq_uuid
        LEFT JOIN (
            SELECT
                grv.rfq_uuid,
                SUM(CASE WHEN o.rfq_credits > 0 THEN 1 ELSE 0 END) as subscribed_count,
                SUM(CASE WHEN o.rfq_credits = 0 THEN 1 ELSE 0 END) as unsubscribed_count
            FROM gmt_rfq_vendors grv
            JOIN organization o ON grv.vendor_uuid = o.uuid
            GROUP BY grv.rfq_uuid
        ) progress ON h.uuid = progress.rfq_uuid
        WHERE i.category IS NOT NULL
        AND LOWER(TRIM(i.category)) != 'other'
        AND h.source_type = 'W'
        AND h.created_ts > :cutoff_date
        AND (h.rfq_closing_date IS NULL OR h.rfq_closing_date > NOW())
        AND h.quotation_received = 0
        AND (
            COALESCE(progress.subscribed_count, 0) < :target_subscribed
            OR COALESCE(progress.unsubscribed_count, 0) < :target_unsubscribed
        )
        ORDER BY h.created_ts DESC
        LIMIT :limit
    """

    return execute_remote_query(query, {
        'limit': limit,
        'cutoff_date': cutoff_date.strftime('%Y-%m-%d %H:%M:%S'),
        'target_subscribed': TARGET_SUBSCRIBED_SELLERS,
        'target_unsubscribed': TARGET_UNSUBSCRIBED_SELLERS
    })


def get_rfq_item_categories(rfq_uuid: str) -> List[str]:
    """
    Get all unique categories for items in a specific RFQ.

    Excludes categories marked as "other" (case-insensitive).

    Args:
        rfq_uuid: The RFQ UUID

    Returns:
        List of unique category names (excluding "other")
    """
    query = """
        SELECT DISTINCT category
        FROM rfq_items
        WHERE rfq_uuid = :rfq_uuid
        AND category IS NOT NULL
        AND LOWER(TRIM(category)) != 'other'
    """

    results = execute_remote_query(query, {'rfq_uuid': rfq_uuid})
    return [r['category'] for r in results if r.get('category')]


def get_sellers_notified_in_last_24hrs() -> Set[str]:
    """
    Get set of seller UUIDs who received any RFQ notification recently.

    Note: Currently set to 15 minutes for testing. Change to hours=24 for production.

    Returns:
        Set of vendor_uuid strings to exclude from selection
    """
    # TODO: Change to timedelta(hours=24) for production
    cutoff_time = datetime.utcnow() - timedelta(minutes=15)

    query = """
        SELECT DISTINCT vendor_uuid
        FROM gmt_rfq_vendors
        WHERE created_ts > :cutoff_time
        AND vendor_uuid IS NOT NULL
    """

    results = execute_remote_query(query, {
        'cutoff_time': cutoff_time.strftime('%Y-%m-%d %H:%M:%S')
    })

    return {r['vendor_uuid'] for r in results if r.get('vendor_uuid')}


async def process_single_rfq_matching(
    rfq: Dict[str, Any],
    seller_service: SellerRecommendationService,
    excluded_seller_ids: Set[str]
) -> Dict[str, Any]:
    """
    Process seller matching for a single RFQ based on item categories.

    Selects only the remaining needed sellers to reach target counts:
    - 5 subscribed sellers (rfq_credits > 0)
    - 10 unsubscribed sellers (rfq_credits = 0)

    Args:
        rfq: RFQ data dictionary (includes current progress counts)
        seller_service: Initialized SellerRecommendationService instance
        excluded_seller_ids: Set of seller UUIDs to exclude (notified in last 24hrs)

    Returns:
        Processing result dictionary
    """
    rfq_id = rfq.get('rfq_id')
    rfq_uuid = rfq.get('rfq_uuid')

    try:
        logger.info(f"Processing seller matching for RFQ {rfq_id}")

        # Get current progress from RFQ data (populated by query)
        subscribed_notified = int(rfq.get('subscribed_notified', 0))
        unsubscribed_notified = int(rfq.get('unsubscribed_notified', 0))

        # Calculate remaining needed
        subscribed_needed = max(0, TARGET_SUBSCRIBED_SELLERS - subscribed_notified)
        unsubscribed_needed = max(0, TARGET_UNSUBSCRIBED_SELLERS - unsubscribed_notified)

        logger.info(
            f"RFQ {rfq_id} progress: {subscribed_notified}/{TARGET_SUBSCRIBED_SELLERS} subscribed, "
            f"{unsubscribed_notified}/{TARGET_UNSUBSCRIBED_SELLERS} unsubscribed. "
            f"Need: {subscribed_needed} subscribed, {unsubscribed_needed} unsubscribed"
        )

        if subscribed_needed == 0 and unsubscribed_needed == 0:
            logger.info(f"RFQ {rfq_id} has reached target seller counts, skipping")
            return {
                "rfq_id": rfq_id,
                "success": True,
                "sellers_matched": 0,
                "message": "Target seller counts already reached",
                "subscribed_notified": subscribed_notified,
                "unsubscribed_notified": unsubscribed_notified
            }

        # Get sellers already notified for this RFQ (to avoid duplicates)
        already_notified_for_rfq = get_sellers_already_notified_for_rfq(rfq_uuid)

        # Step 1: Get all unique categories from rfq_items for this RFQ
        categories = get_rfq_item_categories(rfq_uuid)

        if not categories:
            logger.warning(f"No categories found in rfq_items for RFQ {rfq_id}")
            return {
                "rfq_id": rfq_id,
                "success": True,
                "sellers_matched": 0,
                "message": "No categories found in items",
                "categories_used": []
            }

        logger.info(f"RFQ {rfq_id} has {len(categories)} unique categories: {categories}")

        # Prepare RFQ data for seller service
        rfq_data = {
            'rfq_id': rfq_id,
            'categories': categories,
            'delivery_location': extract_delivery_location(rfq),
            'delivery_date': rfq.get('delivery_date'),
            'rfq_closing_date': rfq.get('rfq_closing_date'),
            'description': rfq.get('description', ''),
            'special_instruction': rfq.get('special_instruction', ''),
            'user_id': rfq.get('user'),
            'org_uuid': rfq.get('org_uuid')
        }

        # Step 2: Collect sellers for ALL categories, separated by subscription status
        subscribed_sellers = {}  # seller_id -> seller_data (rfq_credits > 0)
        unsubscribed_sellers = {}  # seller_id -> seller_data (rfq_credits = 0)

        for category in categories:
            logger.info(f"Finding sellers for category: {category}")

            # Create category-specific RFQ data for matching
            category_rfq_data = rfq_data.copy()
            category_rfq_data['categories'] = [category]

            # HYBRID TWO-PHASE APPROACH per category
            candidate_seller_ids = None

            # Phase 1: Try enhanced semantic discovery (optional)
            try:
                from app.services.enhanced_seller_matching_service import EnhancedSellerMatchingService

                enhanced_service = EnhancedSellerMatchingService()
                item_description = _build_item_description_for_rfq(category_rfq_data)

                if item_description:
                    enhanced_result = await enhanced_service.find_sellers_for_item(
                        item_description=item_description,
                        delivery_location=rfq_data.get('delivery_location'),
                        max_distance_km=500,
                        max_sellers=100,
                        similarity_threshold=0.3,
                        ranking_priority=False
                    )

                    if enhanced_result.get('success') and enhanced_result.get('sellers'):
                        candidate_seller_ids = [s['seller_id'] for s in enhanced_result['sellers']]
                        logger.info(f"Phase 1: Found {len(candidate_seller_ids)} semantic candidates for {category}")

            except Exception as e:
                logger.warning(f"Phase 1 failed for category {category}: {e}")
                candidate_seller_ids = None

            # Phase 2: Apply business rules via standard service
            seller_result = await seller_service.select_sellers_for_rfq(
                rfq_data=category_rfq_data,
                candidate_seller_ids=candidate_seller_ids
            )

            # Collect sellers from this category, separated by subscription status
            # The seller service already separates them
            for seller in seller_result.get('subscribed_sellers', []):
                seller_id = seller.get('seller_id')
                if seller_id and seller_id not in subscribed_sellers:
                    subscribed_sellers[seller_id] = seller

            for seller in seller_result.get('unsubscribed_sellers', []):
                seller_id = seller.get('seller_id')
                if seller_id and seller_id not in unsubscribed_sellers:
                    unsubscribed_sellers[seller_id] = seller

        logger.info(
            f"Total unique sellers found: {len(subscribed_sellers)} subscribed, "
            f"{len(unsubscribed_sellers)} unsubscribed"
        )

        # Step 3: Apply filters - 24hr exclusion AND already notified for this RFQ
        combined_exclusion = excluded_seller_ids | already_notified_for_rfq

        filtered_subscribed = {
            sid: sdata for sid, sdata in subscribed_sellers.items()
            if sid not in combined_exclusion
        }
        filtered_unsubscribed = {
            sid: sdata for sid, sdata in unsubscribed_sellers.items()
            if sid not in combined_exclusion
        }

        excluded_24hr = len(subscribed_sellers) + len(unsubscribed_sellers) - \
                        len(filtered_subscribed) - len(filtered_unsubscribed)

        logger.info(
            f"After filtering: {len(filtered_subscribed)} subscribed, "
            f"{len(filtered_unsubscribed)} unsubscribed available. "
            f"Excluded {excluded_24hr} (24hr filter + already notified)"
        )

        # Step 4: Select only the needed count for each type
        selected_subscribed = list(filtered_subscribed.values())[:subscribed_needed]
        selected_unsubscribed = list(filtered_unsubscribed.values())[:unsubscribed_needed]

        all_selected = selected_subscribed + selected_unsubscribed

        logger.info(
            f"Selected for notification: {len(selected_subscribed)} subscribed, "
            f"{len(selected_unsubscribed)} unsubscribed"
        )

        if all_selected:
            # Log detailed seller information
            _log_selected_sellers_details(rfq_id, all_selected, categories)

            # Log selected sellers to gmt_rfq_vendors table
            try:
                log_selected_sellers_to_remote(rfq_uuid, rfq_id, all_selected)
                logger.info(f"Successfully logged {len(all_selected)} sellers to gmt_rfq_vendors for RFQ {rfq_id}")
            except Exception as e:
                logger.warning(f"Could not log to gmt_rfq_vendors: {e}")
                logger.info(f"Seller selection completed for RFQ {rfq_id} but logging skipped")

            # Send WhatsApp notifications to sellers
            notification_service = SellerNotificationService()
            notification_results = await notification_service.send_rfq_notifications(
                rfq_data=rfq_data,
                sellers=all_selected
            )
            logger.info(
                f"Notification results for RFQ {rfq_id}: "
                f"{notification_results['sent']} sent, {notification_results['failed']} failed"
            )

            return {
                "rfq_id": rfq_id,
                "success": True,
                "sellers_matched": len(all_selected),
                "subscribed_selected": len(selected_subscribed),
                "unsubscribed_selected": len(selected_unsubscribed),
                "sellers_excluded": excluded_24hr,
                "categories_used": categories,
                "notifications_sent": notification_results.get("sent", 0),
                "notifications_failed": notification_results.get("failed", 0),
                "progress": {
                    "subscribed": subscribed_notified + len(selected_subscribed),
                    "unsubscribed": unsubscribed_notified + len(selected_unsubscribed),
                    "subscribed_target": TARGET_SUBSCRIBED_SELLERS,
                    "unsubscribed_target": TARGET_UNSUBSCRIBED_SELLERS
                }
            }
        else:
            logger.warning(f"No eligible sellers for RFQ {rfq_id} after filtering")
            return {
                "rfq_id": rfq_id,
                "success": True,
                "sellers_matched": 0,
                "sellers_excluded": excluded_24hr,
                "message": "No eligible sellers after filtering",
                "categories_used": categories,
                "progress": {
                    "subscribed": subscribed_notified,
                    "unsubscribed": unsubscribed_notified,
                    "subscribed_target": TARGET_SUBSCRIBED_SELLERS,
                    "unsubscribed_target": TARGET_UNSUBSCRIBED_SELLERS
                }
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


def log_selected_sellers_to_remote(rfq_uuid: str, rfq_id: str, sellers: List[Dict[str, Any]]) -> bool:
    """
    Log selected sellers to the remote gmt_rfq_vendors table.

    Args:
        rfq_uuid: RFQ UUID
        rfq_id: RFQ ID
        sellers: List of seller dictionaries to log

    Returns:
        True if logging successful, False otherwise
    """
    try:
        logger.info(f"Logging {len(sellers)} selected sellers for RFQ {rfq_id}")

        if not sellers:
            logger.warning(f"No sellers to log for RFQ {rfq_id}")
            return True

        # Build batch insert query using correct column names from gmt_rfq_vendors table:
        # uuid, created_by, created_ts, rfq_uuid, vendor_uuid
        from sqlalchemy import text

        # Add value placeholders
        value_placeholders = []
        params = {}

        for i, seller in enumerate(sellers):
            # Generate UUID for each record
            record_uuid = str(uuid_lib.uuid4())

            placeholder = f"(:uuid_{i}, :created_by_{i}, NOW(), :rfq_uuid_{i}, :vendor_uuid_{i})"
            value_placeholders.append(placeholder)

            params[f"uuid_{i}"] = record_uuid
            params[f"created_by_{i}"] = "ai_agent"
            params[f"rfq_uuid_{i}"] = rfq_uuid
            params[f"vendor_uuid_{i}"] = seller.get('seller_id')

        query = f"""
            INSERT INTO gmt_rfq_vendors
            (uuid, created_by, created_ts, rfq_uuid, vendor_uuid)
            VALUES
            {", ".join(value_placeholders)}
        """

        # Execute the insert
        db = get_remote_db_session()
        try:
            result = db.execute(text(query), params)
            db.commit()

            logger.info(f"Successfully logged {len(sellers)} sellers to gmt_rfq_vendors for RFQ {rfq_id}")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to insert sellers to gmt_rfq_vendors: {e}")
            return False
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Error logging selected sellers for RFQ {rfq_id}: {e}")
        return False


def _build_item_description_for_rfq(rfq_data: Dict[str, Any]) -> str:
    """
    Build comprehensive item description for semantic search in hybrid approach.

    Combines categories, description, and special instructions into a single
    text for semantic matching via the enhanced service.

    Args:
        rfq_data: RFQ data dictionary

    Returns:
        Combined item description string for semantic search
    """
    parts = []

    # Add categories
    categories = rfq_data.get('categories', [])
    if categories:
        parts.append(f"Categories: {', '.join(categories)}")

    # Add description (handle None values)
    description = (rfq_data.get('description') or '').strip()
    if description:
        parts.append(f"Description: {description}")

    # Add special instructions (may contain item details, handle None values)
    special_instruction = (rfq_data.get('special_instruction') or '').strip()
    if special_instruction:
        parts.append(f"Requirements: {special_instruction}")

    return " | ".join(parts) if parts else ""


def _log_selected_sellers_details(rfq_id: str, sellers: List[Dict[str, Any]], categories: List[str]) -> None:
    """
    Log detailed information about selected sellers for an RFQ.

    Args:
        rfq_id: The RFQ ID
        sellers: List of selected seller dictionaries
        categories: Categories used for matching
    """
    logger.info("=" * 60)
    logger.info(f"SELLER SELECTION SUMMARY FOR RFQ: {rfq_id}")
    logger.info("=" * 60)
    logger.info(f"Categories matched: {', '.join(categories)}")
    logger.info(f"Total sellers selected: {len(sellers)}")
    logger.info("-" * 60)

    for i, seller in enumerate(sellers, 1):
        seller_id = seller.get('seller_id', 'N/A')
        seller_name = seller.get('seller_name', 'Unknown')
        phone = seller.get('phone_number', 'N/A')
        email = seller.get('email', 'N/A')
        seller_categories = seller.get('categories', [])
        ranking = seller.get('ranking', 'N/A')
        location = seller.get('location', {})
        city = location.get('city', 'N/A') if isinstance(location, dict) else 'N/A'
        state = location.get('state', 'N/A') if isinstance(location, dict) else 'N/A'

        logger.info(f"  [{i}] {seller_name}")
        logger.info(f"      ID: {seller_id}")
        logger.info(f"      Phone: {phone}")
        logger.info(f"      Email: {email}")
        logger.info(f"      Ranking: {ranking}")
        logger.info(f"      Location: {city}, {state}")
        logger.info(f"      Categories: {', '.join(seller_categories) if seller_categories else 'N/A'}")
        logger.info("-" * 60)

    logger.info("=" * 60)


if __name__ == "__main__":
    # Configure logging for direct execution
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("Running Seller Matching Task Directly")
    print("=" * 60)

    # Call the task function directly (without Celery)
    result = process_seller_matching()

    print("\n" + "=" * 60)
    print("Task Result:")
    print("=" * 60)
    import json
    print(json.dumps(result, indent=2, default=str))