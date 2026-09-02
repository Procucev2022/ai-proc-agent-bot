"""
Verify Database stats against Dashboard metrics.
"""
from datetime import datetime, timezone
from sqlalchemy import func, distinct
from app.database import get_db_session_context
from app.models import ConversationSession, RFQ, RFQNotificationFact

def verify_today():
    with get_db_session_context() as db:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        
        # 1. Total Visits / Sessions
        total_sessions = db.query(func.count(ConversationSession.session_id)).filter(
            ConversationSession.created_at >= today_start
        ).scalar() or 0
        
        # 2. Unique Users Visited
        unique_users = db.query(func.count(distinct(ConversationSession.external_user_id))).filter(
            ConversationSession.created_at >= today_start,
            ConversationSession.external_user_id.isnot(None)
        ).scalar() or 0
        
        # 3. RFQs in ConversationSession & RFQ table
        session_rfq_rows = db.query(
            ConversationSession.rfq_ids,
            ConversationSession.rfq_id
        ).filter(
            ConversationSession.created_at >= today_start
        ).all()
        
        created_rfq_ids = set()
        for r_ids, single_id in session_rfq_rows:
            if isinstance(r_ids, list):
                for item in r_ids:
                    if item:
                        created_rfq_ids.add(str(item))
            if single_id:
                created_rfq_ids.add(str(single_id))
                
        # Also RFQ table
        rfq_table_ids = db.query(RFQ.rfq_id).filter(
            RFQ.created_at >= today_start
        ).all()
        for (rfq_id,) in rfq_table_ids:
            if rfq_id:
                created_rfq_ids.add(str(rfq_id))

        total_rfqs = len(created_rfq_ids)
        
        # 4. Seller Responses
        responses = db.query(func.count(RFQNotificationFact.id)).filter(
            RFQNotificationFact.created_at >= today_start,
            RFQNotificationFact.seller_response_at.isnot(None)
        ).scalar() or 0
        
        print("\n" + "="*55)
        print(f"LIVE DATABASE STATS FOR TODAY ({today_start.date()} UTC):")
        print("="*55)
        print(f" 1. Active on WhatsApp      : (Tracked in Redis live heartbeat)")
        print(f" 2. Total Visits (Sessions) : {total_sessions}")
        print(f" 3. Unique Users Visited    : {unique_users}")
        print(f" 4. RFQs Created (Buyers)   : {total_rfqs}")
        print(f" 5. Seller Responses        : {responses}")
        print("="*55 + "\n")

if __name__ == "__main__":
    verify_today()
