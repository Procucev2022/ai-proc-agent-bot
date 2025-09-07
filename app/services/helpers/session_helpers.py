"""
Session management helpers for ChatService.

Contains session-related utility methods extracted from the main 
ChatService class for better organization.
"""

from datetime import datetime, timedelta
from app.config import get_settings
from app.models import ConversationSession
from app.utils.datetime_utils import utc_now, utc_from_naive, is_expired, format_utc_display
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class SessionHelpers:
    """Session management helper methods extracted from ChatService."""
    
    @staticmethod
    async def is_session_expired(session: ConversationSession) -> bool:
        """Check if session has expired based on timeout, with proper activity-based renewal."""
        if not session:
            return False
        
        # If session doesn't have created_at, it's invalid
        if not session.created_at:
            logger.warning(f"Session {session.session_id} missing created_at timestamp")
            return True
        
        # Use last_activity_at if available, otherwise fall back to created_at
        last_activity = getattr(session, 'last_activity_at', None) or session.created_at
        
        timeout_minutes = get_settings().session_timeout_minutes
        timeout_hours = timeout_minutes / 60
        session_expired, last_activity_utc, expired_threshold_utc = is_expired(last_activity, timeout_hours)
        
        if session_expired:
            logger.info(f"Session {session.session_id} expired: last_activity={format_utc_display(last_activity_utc)}, expired_time={format_utc_display(expired_threshold_utc)}")
        
        return session_expired
    
    @staticmethod
    async def should_send_expiration_message(session: ConversationSession) -> bool:
        """Determine if we should send an expiration message to the user.
        
        Only send expiration message if:
        1. Session is truly expired (not just inactive)
        2. Session has meaningful conversation history
        3. User hasn't been notified of expiration recently
        4. Session is at least 1 hour old to avoid new session confusion
        """
        if not session:
            return False
        
        # Only send expiration message if session is actually expired
        if not await SessionHelpers.is_session_expired(session):
            return False
        
        # Don't send expiration message for very new sessions (less than session timeout)
        # This prevents confusion when users just started a conversation
        if session.created_at:
            created_utc = utc_from_naive(session.created_at)
            timeout_minutes = get_settings().session_timeout_minutes
            timeout_ago_utc = utc_now() - timedelta(minutes=timeout_minutes)
            if created_utc > timeout_ago_utc:
                return False
        
        # Don't send expiration message if session has no meaningful history
        if not session.conversation_history:
            return False
        
        messages = session.conversation_history.get('openai_messages', [])
        if len(messages) <= 1:  # Only auth placeholder or single message
            return False
        
        # Don't send expiration message if we already sent one recently
        workflow_state = session.workflow_state or {}
        last_expiry_notification = workflow_state.get('last_expiry_notification')
        if last_expiry_notification:
            try:
                last_notified = datetime.fromisoformat(last_expiry_notification)
                if utc_now() - utc_from_naive(last_notified) < timedelta(hours=1):  # Don't spam
                    return False
            except (ValueError, TypeError):
                pass  # Invalid timestamp format, proceed
        
        return True
    
    @staticmethod
    async def renew_session_activity(session: ConversationSession) -> ConversationSession:
        """Renew session activity timestamp to extend its lifetime."""
        if session:
            # Update last activity timestamp in database field (store as UTC)
            current_utc = utc_now()
            session.last_activity_at = current_utc.replace(tzinfo=None)  # Store as naive UTC
            
            # Also update in workflow_state for redundancy and backward compatibility
            if not session.workflow_state:
                session.workflow_state = {}
            session.workflow_state['last_activity_at'] = current_utc.isoformat()
            
            logger.debug(f"Renewed session {session.session_id} activity at {format_utc_display(current_utc)}")
        
        return session
    
    @staticmethod
    def generate_session_id(phone_number: str, strategy: str = "daily") -> str:
        """Generate session ID with different strategies.
        
        Args:
            phone_number: User's phone number
            strategy: 'daily', 'weekly', 'persistent', or 'uuid'
        
        Returns:
            Session ID string
        """
        now = utc_now()
        
        if strategy == "daily":
            return f"whatsapp_{phone_number}_{now.strftime('%Y%m%d')}"
        elif strategy == "weekly":
            # Use year and week number for weekly sessions
            year, week, _ = now.isocalendar()
            return f"whatsapp_{phone_number}_{year}W{week:02d}"
        elif strategy == "persistent":
            # Single persistent session per user
            return f"whatsapp_{phone_number}_persistent"
        elif strategy == "uuid":
            # Unique session per conversation start
            import uuid
            return f"whatsapp_{phone_number}_{now.strftime('%Y%m%d')}_{str(uuid.uuid4())[:8]}"
        else:
            # Default to daily
            return f"whatsapp_{phone_number}_{now.strftime('%Y%m%d')}"
    
    @staticmethod
    async def handle_session_expiry(session: ConversationSession, db_manager) -> ConversationSession:
        """Properly handle session expiration by refreshing the existing session.
        
        Instead of creating new sessions with different IDs, this method:
        1. Preserves conversation history and entities in archive
        2. Resets the existing session for a fresh start
        3. Updates timestamps to current time
        """
        if not session:
            return None
        
        current_utc = utc_now()
        
        # Archive existing conversation data in workflow_state for reference
        if not session.workflow_state:
            session.workflow_state = {}
            
        # Store archive of expired session
        session.workflow_state['session_archive'] = {
            'expired_at': current_utc.isoformat(),
            'previous_conversation_history': session.conversation_history,
            'previous_extracted_entities': session.extracted_entities,
            'previous_workflow_type': session.workflow_type.value if session.workflow_type else None
        }
        
        # Reset session for fresh start but keep the same ID
        session.workflow_type = None
        session.outcome = None
        session.conversation_history = {"openai_messages": [], "metadata": []}
        session.extracted_entities = {}
        session.completed_at = None  # Clear completion timestamp
        
        # Update timestamps to current UTC
        session.last_activity_at = current_utc.replace(tzinfo=None)
        session.workflow_state['last_activity_at'] = current_utc.isoformat()
        session.workflow_state['session_refreshed'] = current_utc.isoformat()
        
        # Save the refreshed session
        updated_session = db_manager.save_conversation_session({
            'session_id': session.session_id,
            'external_user_id': session.external_user_id,
            'workflow_type': session.workflow_type,
            'outcome': session.outcome,
            'workflow_state': session.workflow_state,
            'conversation_history': session.conversation_history,
            'extracted_entities': session.extracted_entities,
            'retention_date': session.retention_date,
            'last_activity_at': session.last_activity_at,
            'completed_at': session.completed_at
        })
        
        logger.info(f"Refreshed expired session {session.session_id} with new activity timestamp")
        
        return updated_session
    
    @staticmethod
    def update_bfs_activity(session: ConversationSession, search_data: Dict[str, Any]) -> ConversationSession:
        """Update session with BFS (Buy From Stock) activity."""
        try:
            # Initialize BFS fields if not present
            if not session.bfs_products_searched:
                session.bfs_products_searched = []
            if not session.bfs_search_count:
                session.bfs_search_count = 0
            if not session.bfs_price_accepted:
                session.bfs_price_accepted = []
            if not session.bfs_counter_offers:
                session.bfs_counter_offers = []
            
            # Update search data
            if 'searched_products' in search_data:
                session.bfs_products_searched.extend(search_data['searched_products'])
                session.bfs_search_count += len(search_data['searched_products'])
            
            if 'accepted_prices' in search_data:
                session.bfs_price_accepted.extend(search_data['accepted_prices'])
            
            if 'counter_offers' in search_data:
                session.bfs_counter_offers.extend(search_data['counter_offers'])
            
            logger.info(f"Updated BFS activity for session {session.session_id}")
            return session
            
        except Exception as e:
            logger.error(f"Error updating BFS activity: {e}")
            return session
    
    @staticmethod
    def update_bidding_activity(session: ConversationSession, bidding_data: Dict[str, Any]) -> ConversationSession:
        """Update session with bidding and counter-offer activity."""
        try:
            # Initialize bidding fields if not present
            if not session.products_bid_for:
                session.products_bid_for = []
            if not session.bids_received:
                session.bids_received = []
            if not session.bids_accepted:
                session.bids_accepted = []
            if not session.counter_offers_made:
                session.counter_offers_made = []
            if not session.counter_offers_accepted:
                session.counter_offers_accepted = []
            if not session.rfqs_with_response:
                session.rfqs_with_response = []
            
            # Update bidding data
            if 'products_bid_for' in bidding_data:
                session.products_bid_for.extend(bidding_data['products_bid_for'])
            
            if 'bids_received' in bidding_data:
                session.bids_received.extend(bidding_data['bids_received'])
            
            if 'bids_accepted' in bidding_data:
                session.bids_accepted.extend(bidding_data['bids_accepted'])
            
            if 'counter_offers_made' in bidding_data:
                session.counter_offers_made.extend(bidding_data['counter_offers_made'])
            
            if 'counter_offers_accepted' in bidding_data:
                session.counter_offers_accepted.extend(bidding_data['counter_offers_accepted'])
            
            if 'rfqs_with_response' in bidding_data:
                for rfq_id in bidding_data['rfqs_with_response']:
                    if rfq_id not in session.rfqs_with_response:
                        session.rfqs_with_response.append(rfq_id)
            
            logger.info(f"Updated bidding activity for session {session.session_id}")
            return session
            
        except Exception as e:
            logger.error(f"Error updating bidding activity: {e}")
            return session
    
    @staticmethod
    def calculate_session_averages(session: ConversationSession) -> ConversationSession:
        """Calculate and update session averages for products and categories per RFQ."""
        try:
            # Count total RFQs
            total_rfqs = 0
            if session.rfq_ids:
                total_rfqs = len([rfq for rfq in session.rfq_ids if rfq])
            elif session.rfq_id:
                total_rfqs = 1
            
            if total_rfqs == 0:
                session.avg_products_per_rfq = 0
                session.avg_categories_per_rfq = 0
                return session
            
            # Count total products
            total_products = 0
            if session.product_items and isinstance(session.product_items, list):
                total_products = len(session.product_items)
            
            # Count unique categories
            categories = set()
            if session.product_items and isinstance(session.product_items, list):
                for product in session.product_items:
                    if isinstance(product, dict) and product.get('category'):
                        categories.add(product['category'])
            
            # Also check extracted entities for categories
            if session.extracted_entities:
                entities = session.extracted_entities
                if isinstance(entities, dict):
                    if entities.get('category'):
                        categories.add(entities['category'])
                    if entities.get('description'):
                        categories.add(entities['description'])
                elif isinstance(entities, list):
                    for entity in entities:
                        if isinstance(entity, dict):
                            if entity.get('category'):
                                categories.add(entity['category'])
                            if entity.get('description'):
                                categories.add(entity['description'])
            
            # Calculate averages
            session.avg_products_per_rfq = round(total_products / total_rfqs, 2)
            session.avg_categories_per_rfq = round(len(categories) / total_rfqs, 2)
            
            logger.debug(f"Calculated averages for session {session.session_id}: {session.avg_products_per_rfq} products/RFQ, {session.avg_categories_per_rfq} categories/RFQ")
            return session
            
        except Exception as e:
            logger.error(f"Error calculating session averages: {e}")
            return session

    @staticmethod
    def clean_for_json_serialization(obj):
        """Recursively clean object for JSON serialization."""
        import json
        from datetime import datetime, date

        if obj is None:
            return None
        elif hasattr(obj, 'value'):  # Enum object
            return obj.value
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, dict):
            return {key: SessionHelpers.clean_for_json_serialization(value) for key, value in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [SessionHelpers.clean_for_json_serialization(item) for item in obj]
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        else:
            # Try to serialize to test, if it fails, convert to string
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)

    @staticmethod
    def should_use_summary_aware_extraction(message: str) -> bool:
        """
        Use AI to intelligently determine if we should use summary-aware entity extraction.

        This uses the OpenAI service to analyze the message and determine if the user is making
        references to previous conversations that would benefit from historical context.

        Args:
            message: User message to analyze

        Returns:
            True if summary-aware extraction should be used
        """
        # COMMENTED OUT: Disable automatic reference detection to enforce session timeout behavior
        # When sessions timeout, users should lose context and start fresh
        return False
        
        # try:
        #     # Use OpenAI service for intelligent reference detection
        #     reference_analysis = self.openai_service.analyze_reference_context(message)

        #     has_references = reference_analysis.get("has_references", False)
        #     confidence = reference_analysis.get("confidence", 0)
        #     reference_types = reference_analysis.get("reference_types", [])

        #     # Use summary-aware extraction if we have high confidence references
        #     should_use_summary = has_references and confidence >= 70

        #     print(f"ChatService: Reference analysis for '{message}':")
        #     print(f"  - Has references: {has_references}")
        #     print(f"  - Confidence: {confidence}%")
        #     print(f"  - Reference types: {reference_types}")
        #     print(f"  - Use summary-aware extraction: {should_use_summary}")

        #     return should_use_summary

        # except Exception as e:
        #     print(f"ChatService: Error in reference analysis: {e}")
        #     # Fallback: if analysis fails, don't use summary-aware extraction
        #     return False