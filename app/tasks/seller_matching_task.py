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

from app.database import execute_remote_query, get_remote_db_session, get_db_session
from app.services.seller_recommendation_service import SellerRecommendationService
from app.services.seller_notification_service import SellerNotificationService
from app.models import RFQSellerNotification, NotificationType
from app.config import get_settings
from app.tasks.task_utils import get_users_active_in_last_24hrs, normalize_phone_for_comparison

logger = logging.getLogger(__name__)

# Target seller counts per RFQ
TARGET_SUBSCRIBED_SELLERS = 10    # Sellers with rfq_credits > 0
TARGET_UNSUBSCRIBED_SELLERS = 25  # Sellers with rfq_credits = 0


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


@shared_task(
    bind=True,
    queue='seller_matching',
    priority=6,  # Medium-High priority - frequent daytime task
    time_limit=1200,       # 20 min max (runaway protection)
    soft_time_limit=900,   # 15 min soft limit
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3, 'countdown': 120}
)
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
                    notified_ids = result.get("seller_ids_notified", [])
                    if notified_ids:
                        excluded_seller_ids.update(notified_ids)
                        logger.info(f"Added {len(notified_ids)} sellers to batch exclusion set (total excluded: {len(excluded_seller_ids)})")

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
        # Rely on autoretry_for - will retry up to 3 times with 120s countdown
        raise


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
    cutoff_date = datetime.utcnow() - timedelta(hours=2)

    # Get RFQs that need more sellers
    # Uses subquery to count current subscribed/unsubscribed notifications

    query = """
        SELECT DISTINCT
            h.uuid as rfq_uuid,
            h.rfq_id,
            h.project_desc as description,
            h.delivery_date,
            cdl.pincode delivery_pincode,
            cdl.city AS delivery_city,
            cdl.state AS delivery_state,
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
        LEFT join client_delivery_location_rfq cdl on cdl.rfq_uuid= h.uuid
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
        AND h.status_uuid = '23'
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
    Get set of seller UUIDs who received any RFQ notification in the last 24 hours.

    Returns:
        Set of vendor_uuid strings to exclude from selection
    """
    cutoff_time = datetime.utcnow() - timedelta(hours=24)

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

        # Get sellers already notified for this specific RFQ (never notify again for same RFQ)
        already_notified_for_rfq = get_sellers_already_notified_for_rfq(rfq_uuid)
        logger.info(f"RFQ {rfq_id}: {len(already_notified_for_rfq)} sellers already notified for this RFQ (will be excluded)")

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

            seller_result = await seller_service.select_sellers_for_rfq(rfq_data=category_rfq_data)

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

        # Step 3: Apply filters - time-based exclusion AND per-RFQ exclusion
        # Combine both exclusion sets: 24hr global exclusion + already notified for this RFQ
        all_excluded_ids = excluded_seller_ids | already_notified_for_rfq

        filtered_subscribed = {
            sid: sdata for sid, sdata in subscribed_sellers.items()
            if sid not in all_excluded_ids
        }
        filtered_unsubscribed = {
            sid: sdata for sid, sdata in unsubscribed_sellers.items()
            if sid not in all_excluded_ids
        }

        excluded_count = len(subscribed_sellers) + len(unsubscribed_sellers) - \
                        len(filtered_subscribed) - len(filtered_unsubscribed)

        # Log breakdown of exclusions
        time_excluded = len([sid for sid in subscribed_sellers.keys() | unsubscribed_sellers.keys()
                            if sid in excluded_seller_ids])
        rfq_excluded = len([sid for sid in subscribed_sellers.keys() | unsubscribed_sellers.keys()
                           if sid in already_notified_for_rfq])

        logger.info(
            f"After filtering: {len(filtered_subscribed)} subscribed, "
            f"{len(filtered_unsubscribed)} unsubscribed available. "
            f"Excluded {excluded_count} total ({time_excluded} time-based, {rfq_excluded} already notified for RFQ)"
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
            # Check which sellers were active in the last 24 hours (for message type selection)
            seller_phones = [s.get('phone_number') for s in all_selected if s.get('phone_number')]
            active_seller_phones = get_users_active_in_last_24hrs(seller_phones)

            # Add use_template_message flag to each seller
            interactive_count = 0
            template_count = 0
            for seller in all_selected:
                phone = seller.get('phone_number', '')
                normalized_phone = normalize_phone_for_comparison(phone)
                user_recently_active = normalized_phone in active_seller_phones
                seller['use_template_message'] = not user_recently_active  # Use template if NOT active
                if user_recently_active:
                    interactive_count += 1
                else:
                    template_count += 1

            logger.info(
                f"RFQ {rfq_id} message type split: {interactive_count} interactive (active users), "
                f"{template_count} template (inactive users)"
            )

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
            # TODO: The notification service should check 'use_template_message' flag on each seller
            # and send WhatsApp template message for inactive users instead of interactive message
            notification_service = SellerNotificationService()
            notification_results = await notification_service.send_rfq_notifications(
                rfq_data=rfq_data,
                sellers=all_selected
            )
            logger.info(
                f"Notification results for RFQ {rfq_id}: "
                f"{notification_results['sent']} sent, {notification_results['failed']} failed"
            )

            # Record successful notifications in rfq_seller_notifications table
            record_rfq_seller_notifications(rfq_id, notification_results.get('results', []))

            return {
                "rfq_id": rfq_id,
                "success": True,
                "sellers_matched": len(all_selected),
                "subscribed_selected": len(selected_subscribed),
                "unsubscribed_selected": len(selected_unsubscribed),
                "sellers_excluded": excluded_count,
                "categories_used": categories,
                "interactive_messages": interactive_count,
                "template_messages": template_count,
                "notifications_sent": notification_results.get("sent", 0),
                "notifications_failed": notification_results.get("failed", 0),
                "seller_ids_notified": [s.get('seller_id') for s in all_selected],  # For batch exclusion
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
                "sellers_excluded": excluded_count,
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

    # Get delivery location from the database fields
    if rfq.get('delivery_city'):
        location['city'] = rfq['delivery_city']
    if rfq.get('delivery_state'):
        location['state'] = rfq['delivery_state']
    if rfq.get('delivery_pincode'):
        location['pincode'] = rfq['delivery_pincode']

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


_fk_dropped = False

def _ensure_seller_id_fk_dropped(db) -> None:
    """Drop the sellers FK constraint from rfq_seller_notifications if it still exists."""
    global _fk_dropped
    if _fk_dropped:
        return

    try:
        from sqlalchemy import text, inspect
        inspector = inspect(db.bind)
        fks = inspector.get_foreign_keys('rfq_seller_notifications')
        fk_names = [fk['name'] for fk in fks]

        if 'rfq_seller_notifications_ibfk_1' in fk_names:
            db.execute(text('ALTER TABLE rfq_seller_notifications DROP FOREIGN KEY rfq_seller_notifications_ibfk_1'))
            db.commit()
            logger.info("Dropped FK constraint rfq_seller_notifications_ibfk_1")

        _fk_dropped = True
    except Exception as e:
        db.rollback()
        logger.warning(f"Could not drop FK constraint on rfq_seller_notifications: {e}")


def record_rfq_seller_notifications(rfq_id: str, notification_results: List[Dict[str, Any]]) -> None:
    """
    Record successfully sent notifications in rfq_seller_notifications table.

    Only records sellers where the WhatsApp notification was actually sent.

    Args:
        rfq_id: The RFQ ID
        notification_results: Per-seller results from SellerNotificationService.send_rfq_notifications()
    """
    successful = [r for r in notification_results if r.get('success')]

    if not successful:
        return

    db = get_db_session()
    try:
        _ensure_seller_id_fk_dropped(db)

        for result in successful:
            notification = RFQSellerNotification(
                rfq_id=rfq_id,
                seller_id=result['seller_id'],
                notification_type=NotificationType.initial_notification,
                sent_at=datetime.utcnow()
            )
            db.add(notification)

        db.commit()
        logger.info(f"Recorded {len(successful)} notifications in rfq_seller_notifications for RFQ {rfq_id}")

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record rfq_seller_notifications for RFQ {rfq_id}: {e}")
    finally:
        db.close()


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