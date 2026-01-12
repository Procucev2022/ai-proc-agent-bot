"""
BFS Notification Task - Seller Bid Notifications

This task processes unsent BFS bid notifications:
1. Fetches bfs_users records where is_sent = 0 (notification not yet sent)
2. JOINs with bfs_items to get item details (description, category, price)
3. JOINs with organization to get seller phone number
4. Sends WhatsApp notifications with Accept/Reject buttons
5. Updates is_sent = 1 after successful notification

The seller can then:
- Accept Bid → calls /bfs/acceptBfsItemBySeller API
- Reject Bid → bid is declined
"""

import asyncio
import logging
from typing import List, Dict, Any
from datetime import datetime

from celery import shared_task
from sqlalchemy import text

from app.database import get_remote_db_session
from app.services.seller_notification_service import get_seller_notification_service
from app.config import get_settings

logger = logging.getLogger(__name__)


def get_pending_bfs_notifications(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get BFS bid records that need seller notification.

    Fetches bfs_users records where is_sent = 0 or NULL,
    along with item details and seller contact information.

    Args:
        limit: Maximum number of records to fetch

    Returns:
        List of notification records with seller and item details
    """
    db = None
    try:
        db = get_remote_db_session()

        # Query for unsent notifications
        # JOIN bfs_items to get item details
        # JOIN organization (via bfs_items.org_uuid) to get seller phone
        # JOIN organization (via bfs_users.org_uuid) to get buyer name
        query = text("""
            SELECT
                bu.uuid as bfs_user_uuid,
                bu.ask_price,
                bu.buy_price,
                bu.quantity,
                bu.created_ts as bid_created_at,
                bi.uuid as item_uuid,
                bi.description as item_description,
                bi.category as item_category,
                bi.sell_price as listed_price,
                seller_org.uuid as seller_org_uuid,
                seller_org.organization_name as seller_name,
                seller_org.organization_phonenumber as seller_phone,
                buyer_org.organization_name as buyer_name
            FROM bfs_users bu
            JOIN bfs_items bi ON bu.items_uuid = bi.uuid
            JOIN organization seller_org ON bi.org_uuid = seller_org.uuid
            LEFT JOIN organization buyer_org ON bu.org_uuid = buyer_org.uuid
            WHERE (bu.is_sent = 0 OR bu.is_sent IS NULL)
            ORDER BY bu.created_ts ASC
            LIMIT :limit
        """)

        result = db.execute(query, {'limit': limit})
        columns = result.keys()
        records = [dict(zip(columns, row)) for row in result.fetchall()]

        logger.info(f"[BFS_TASK] Found {len(records)} pending BFS notifications")
        return records

    except Exception as e:
        logger.error(f"[BFS_TASK] Error fetching pending notifications: {e}")
        return []
    finally:
        if db:
            db.close()


def mark_notification_sent(bfs_user_uuid: str) -> bool:
    """
    Mark a BFS notification as sent by updating is_sent = 1.

    Args:
        bfs_user_uuid: The bfs_users record UUID

    Returns:
        True if update successful, False otherwise
    """
    db = None
    try:
        db = get_remote_db_session()

        query = text("""
            UPDATE bfs_users
            SET is_sent = 1,
                last_modified_ts = NOW(),
                last_modified_by = 'ai_agent'
            WHERE uuid = :uuid
        """)

        db.execute(query, {'uuid': bfs_user_uuid})
        db.commit()

        logger.debug(f"[BFS_TASK] Marked {bfs_user_uuid} as sent")
        return True

    except Exception as e:
        logger.error(f"[BFS_TASK] Error marking {bfs_user_uuid} as sent: {e}")
        if db:
            db.rollback()
        return False
    finally:
        if db:
            db.close()


def mark_notifications_sent_batch(bfs_user_uuids: List[str]) -> int:
    """
    Mark multiple BFS notifications as sent in a single transaction.

    Args:
        bfs_user_uuids: List of bfs_users record UUIDs

    Returns:
        Number of records successfully updated
    """
    if not bfs_user_uuids:
        return 0

    db = None
    try:
        db = get_remote_db_session()

        # Build parameterized query for batch update
        placeholders = ', '.join([f':uuid_{i}' for i in range(len(bfs_user_uuids))])
        params = {f'uuid_{i}': uuid for i, uuid in enumerate(bfs_user_uuids)}

        query = text(f"""
            UPDATE bfs_users
            SET is_sent = 1,
                last_modified_ts = NOW(),
                last_modified_by = 'ai_agent'
            WHERE uuid IN ({placeholders})
        """)

        result = db.execute(query, params)
        db.commit()

        updated_count = result.rowcount
        logger.info(f"[BFS_TASK] Marked {updated_count} notifications as sent")
        return updated_count

    except Exception as e:
        logger.error(f"[BFS_TASK] Error in batch update: {e}")
        if db:
            db.rollback()
        return 0
    finally:
        if db:
            db.close()


async def process_bfs_notifications() -> Dict[str, Any]:
    """
    Process all pending BFS notifications.

    Fetches unsent records, sends WhatsApp notifications,
    and marks them as sent.

    Returns:
        Dictionary with processing results
    """
    settings = get_settings()

    # Check if remote DB access is enabled
    if not settings.enable_remote_categorization:
        logger.info("[BFS_TASK] Remote categorization disabled, skipping")
        return {
            "status": "skipped",
            "reason": "remote_categorization_disabled"
        }

    # Fetch pending notifications
    pending = get_pending_bfs_notifications(limit=50)

    if not pending:
        logger.info("[BFS_TASK] No pending BFS notifications")
        return {
            "status": "completed",
            "processed": 0,
            "message": "No pending notifications"
        }

    logger.info(f"[BFS_TASK] Processing {len(pending)} pending notifications")

    # Build notification payloads
    notifications = []
    for record in pending:
        seller_phone = record.get('seller_phone')

        if not seller_phone:
            logger.warning(
                f"[BFS_TASK] No seller phone for bfs_user {record.get('bfs_user_uuid')}"
            )
            continue

        # Normalize phone number (ensure + prefix)
        if not seller_phone.startswith('+'):
            seller_phone = f"+{seller_phone}"

        bid_data = {
            'item_description': record.get('item_description', 'N/A'),
            'category': record.get('item_category', ''),
            'buy_price': record.get('buy_price') or record.get('listed_price') or 0,
            'ask_price': record.get('ask_price', 0),
            'quantity': record.get('quantity', 1),
            'buyer_name': record.get('buyer_name', '')
        }

        notifications.append({
            'seller_phone': seller_phone,
            'bid_data': bid_data,
            'bfs_user_uuid': record.get('bfs_user_uuid'),
            'seller_id': record.get('seller_org_uuid')
        })

    if not notifications:
        logger.warning("[BFS_TASK] No valid notifications to send (missing phone numbers)")
        return {
            "status": "completed",
            "processed": 0,
            "message": "No valid seller phone numbers"
        }

    # Send notifications via the service
    notification_service = get_seller_notification_service()
    results = await notification_service.send_bfs_bid_notifications_batch(
        notifications=notifications,
        skip_workflow_check=False  # Respect workflow state
    )

    # Mark successfully sent notifications
    sent_uuids = [
        r.get('bfs_user_uuid')
        for r in results.get('results', [])
        if r.get('success')
    ]

    if sent_uuids:
        mark_notifications_sent_batch(sent_uuids)

    return {
        "status": "completed",
        "total_pending": len(pending),
        "notifications_prepared": len(notifications),
        "sent": results.get('sent', 0),
        "failed": results.get('failed', 0),
        "skipped": results.get('skipped', 0),
        "timestamp": datetime.utcnow().isoformat()
    }


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3, 'countdown': 120}
)
def process_bfs_seller_notifications(self):
    """
    Celery task to process BFS seller notifications.

    This task:
    1. Fetches bfs_users records where is_sent = 0
    2. Sends WhatsApp notifications to sellers with bid details
    3. Marks notifications as sent after successful delivery

    Scheduled to run every 5 minutes (configurable in celery_config.py).
    """
    try:
        logger.info("[BFS_TASK] Starting BFS notification task")

        # Run async function in sync context
        result = asyncio.run(process_bfs_notifications())

        logger.info(f"[BFS_TASK] Task completed: {result}")
        return result

    except Exception as e:
        logger.error(f"[BFS_TASK] Task failed: {e}")
        raise self.retry(countdown=120)


# Allow direct execution for testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("Running BFS Notification Task Directly")
    print("=" * 60)

    result = asyncio.run(process_bfs_notifications())

    print("\n" + "=" * 60)
    print("Task Result:")
    print("=" * 60)
    import json
    print(json.dumps(result, indent=2, default=str))
