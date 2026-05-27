"""
Simple Daily Summary Service for aggregating user's daily activity.
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime, date
from sqlalchemy import func

from app.database import get_db_session
from app.models import ConversationSession, DailySummary, ChatSummary, UserType, SessionState
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class DailySummaryService:
    """Simple service for generating daily activity summaries."""
    
    def __init__(self):
        self.settings = get_settings()
        
    @log_service_method("daily_summary_service")
    async def generate_daily_summary(self, user_id: str, target_date: date = None) -> Optional[DailySummary]:
        """Generate daily summary for a user."""
        try:
            if not self.settings.enable_daily_summarization:
                return None
                
            if target_date is None:
                target_date = date.today()
            
            with get_db_session() as db:
                # Get all user sessions for the day
                sessions = db.query(ConversationSession)\
                    .filter(ConversationSession.external_user_id == user_id)\
                    .filter(func.date(ConversationSession.created_at) == target_date)\
                    .all()
                
                if not sessions:
                    return None
                
                # Calculate enhanced metrics
                sessions_count = len(sessions)
                
                # Count RFQs created (handle both rfq_id and rfq_ids)
                rfqs_created = 0
                product_items_count = 0
                seller_interaction_count = 0
                session_durations = []
                categories = set()
                session_states = {}
                session_continuation_count = 0
                user_types = []
                
                # TODO: Add new fields based on Excel Buyer Details requirements:
                # - bfs_searches_count (BFS Searches)
                # - products_bid_for_count (Products Bid For) 
                # - bids_accepted_count (Bids Accepted)
                # - rfqs_with_response_count (RFQs w/ Response)
                # - rfq_started_not_submitted_count (RFQ Started But not Submitted)
                
                for session in sessions:
                    # Count RFQs
                    if session.rfq_ids:
                        rfqs_created += len([rfq for rfq in session.rfq_ids if rfq])
                    elif session.rfq_id:
                        rfqs_created += 1
                    
                    # Count product items
                    if session.product_items:
                        product_items_count += len(session.product_items)
                    
                    # Count seller responses
                    if session.seller_responses:
                        seller_interaction_count += len(session.seller_responses)
                    
                    # Track session durations
                    duration = self._calculate_duration(session)
                    if duration is not None:
                        session_durations.append(duration)
                    
                    # Track session states
                    state = session.session_state.value if session.session_state else 'unknown'
                    session_states[state] = session_states.get(state, 0) + 1
                    
                    # Track user types
                    if session.user_type:
                        user_types.append(session.user_type)
                    
                    # Count session continuations
                    if session.parent_session_id:
                        session_continuation_count += 1
                
                # Calculate predominant user type
                predominant_user_type = None
                if user_types:
                    from collections import Counter
                    user_type_counts = Counter(user_types)
                    predominant_user_type = user_type_counts.most_common(1)[0][0]
                
                # Calculate average session duration
                avg_duration = int(sum(session_durations) / len(session_durations)) if session_durations else None
                
                # Extract categories from session entities and summaries
                for session in sessions:
                    if session.extracted_entities:
                        entities = session.extracted_entities
                        if isinstance(entities, dict):
                            if entities.get('description'):
                                categories.add(entities['description'])
                            if entities.get('category'):
                                categories.add(entities['category'])
                        elif isinstance(entities, list):
                            for entity in entities:
                                if isinstance(entity, dict):
                                    if entity.get('description'):
                                        categories.add(entity['description'])
                                    if entity.get('category'):
                                        categories.add(entity['category'])
                
                # Also get from summaries for additional context
                summaries = db.query(ChatSummary)\
                    .filter(ChatSummary.external_user_id == user_id)\
                    .filter(func.date(ChatSummary.created_at) == target_date)\
                    .all()
                
                for summary in summaries:
                    if summary.extracted_entities:
                        entities = summary.extracted_entities
                        if isinstance(entities, dict):
                            if entities.get('description'):
                                categories.add(entities['description'])
                            if entities.get('category'):
                                categories.add(entities['category'])
                        elif isinstance(entities, list):
                            for entity in entities:
                                if isinstance(entity, dict):
                                    if entity.get('description'):
                                        categories.add(entity['description'])
                                    if entity.get('category'):
                                        categories.add(entity['category'])
                
                unique_categories_count = len(categories)
                
                # Check if summary already exists
                existing = db.query(DailySummary)\
                    .filter(DailySummary.external_user_id == user_id)\
                    .filter(DailySummary.date == target_date)\
                    .first()
                
                if existing:
                    # Update existing with enhanced data
                    existing.user_type = predominant_user_type
                    existing.sessions_count = sessions_count
                    existing.rfqs_created_count = rfqs_created
                    existing.session_states_breakdown = session_states
                    existing.seller_interaction_count = seller_interaction_count
                    existing.avg_session_duration = avg_duration
                    existing.primary_product_categories = list(categories)
                    existing.product_items_count = product_items_count
                    existing.unique_categories_count = unique_categories_count
                    existing.session_continuation_count = session_continuation_count
                    db.commit()
                    db.refresh(existing)
                    logger.info(f"Updated daily summary for {user_id} on {target_date}")
                    return existing
                else:
                    # Create new with enhanced data
                    daily_summary = DailySummary(
                        date=target_date,
                        external_user_id=user_id,
                        user_type=predominant_user_type,
                        sessions_count=sessions_count,
                        rfqs_created_count=rfqs_created,
                        session_states_breakdown=session_states,
                        seller_interaction_count=seller_interaction_count,
                        avg_session_duration=avg_duration,
                        primary_product_categories=list(categories),
                        product_items_count=product_items_count,
                        unique_categories_count=unique_categories_count,
                        session_continuation_count=session_continuation_count
                    )
                    
                    db.add(daily_summary)
                    db.commit()
                    db.refresh(daily_summary)
                    
                    logger.info(f"Created daily summary for {user_id} on {target_date}")
                    return daily_summary
                
        except Exception as e:
            logger.error(f"Error generating daily summary for {user_id}: {e}")
            return None
    
    @log_service_method("daily_summary_service")
    async def get_user_daily_summary(self, user_id: str, target_date: date = None) -> Optional[Dict[str, Any]]:
        """Get daily summary for a user."""
        try:
            if target_date is None:
                target_date = date.today()
            
            with get_db_session() as db:
                summary = db.query(DailySummary)\
                    .filter(DailySummary.external_user_id == user_id)\
                    .filter(DailySummary.date == target_date)\
                    .first()
                
                if summary:
                    return {
                        'date': summary.date.strftime('%Y-%m-%d'),
                        'user_type': summary.user_type.value if summary.user_type else None,
                        'sessions_count': summary.sessions_count,
                        'rfqs_created_count': summary.rfqs_created_count,
                        'session_states_breakdown': summary.session_states_breakdown or {},
                        'seller_interaction_count': summary.seller_interaction_count,
                        'avg_session_duration': summary.avg_session_duration,
                        'primary_product_categories': summary.primary_product_categories or [],
                        'product_items_count': summary.product_items_count,
                        'unique_categories_count': summary.unique_categories_count,
                        'session_continuation_count': summary.session_continuation_count,
                        'total_rfq_value_estimate': float(summary.total_rfq_value_estimate) if summary.total_rfq_value_estimate else None
                    }
                
                return None
                
        except Exception as e:
            logger.error(f"Error getting daily summary for {user_id}: {e}")
            return None
    
    def _calculate_duration(self, session: ConversationSession) -> Optional[int]:
        """Calculate session duration in minutes."""
        try:
            if session.created_at and session.completed_at:
                duration = session.completed_at - session.created_at
                return int(duration.total_seconds() / 60)
            elif session.created_at and session.last_activity_at:
                # If not completed, use last activity
                duration = session.last_activity_at - session.created_at
                return int(duration.total_seconds() / 60)
            return None
        except Exception:
            return None