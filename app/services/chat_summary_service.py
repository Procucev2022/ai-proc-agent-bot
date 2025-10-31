"""
Simple Chat Summary Service that calls OpenAIService for summarization.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.database import get_db_session, get_db_session_context
from app.models import ConversationSession, ChatSummary
from app.services.openai_service import OpenAIService
from app.config import get_settings
from app.utils.logging_utils import log_service_method
from app.services.helpers.summarization_helpers import SummarizationHelpers
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

class ChatSummaryService:
    """Simple service for generating and storing chat session summaries."""

    def __init__(self, db_session: Session = None):
        """
        Initialize ChatSummaryService.

        Args:
            db_session: Optional database session to share across operations.
                       If not provided, will create sessions as needed (may cause connection leaks).
        """
        self.db_session = db_session
        self.openai_service = OpenAIService()
        self.settings = get_settings()
        
    @log_service_method("chat_summary_service")
    async def generate_session_summary(self, session: ConversationSession) -> Optional[ChatSummary]:
        """Generate and store a summary for a completed session."""
        try:
            if not self.settings.enable_session_summarization:
                return None
                
            # Prepare enhanced session data
            # Handle both single rfq_id (backward compatibility) and multiple rfq_ids
            rfq_list = []
            if hasattr(session, 'rfq_ids') and session.rfq_ids:
                # Ensure it's a list and filter out None/empty values
                if isinstance(session.rfq_ids, list):
                    rfq_list = [rfq for rfq in session.rfq_ids if rfq]
                else:
                    rfq_list = [session.rfq_ids] if session.rfq_ids else []
            elif hasattr(session, 'rfq_id') and session.rfq_id:
                rfq_list = [session.rfq_id]

            # Extract rich entities from session state for comprehensive summarization
            rich_entities = SummarizationHelpers.extract_rich_entities_for_summary(session)

            # Combine extracted_entities with rich entities and product_items
            combined_entities = {}
            if session.extracted_entities:
                combined_entities.update(session.extracted_entities)
            if rich_entities:
                combined_entities.update(rich_entities)

            # Add product_items to entities if available
            product_items = getattr(session, 'product_items', []) or []
            if product_items:
                combined_entities['product_items'] = product_items

            # Include conversation history for better summarization
            conversation_history = getattr(session, 'conversation_history', {})
            openai_messages = conversation_history.get('openai_messages', []) if conversation_history else []

            session_data = {
                'user_id': session.external_user_id,
                'user_type': getattr(session, 'user_type', None),
                'session_state': getattr(session, 'session_state', None),
                'workflow_type': session.workflow_type,
                'outcome': session.outcome,
                'extracted_entities': combined_entities,
                'rfq_ids': rfq_list,
                'rfq_metadata': getattr(session, 'rfq_metadata', {}) or {},
                'product_items': product_items,
                'seller_responses': getattr(session, 'seller_responses', []) or [],
                'interaction_metrics': getattr(session, 'interaction_metrics', {}) or {},
                'parent_session_id': getattr(session, 'parent_session_id', None),
                'conversation_history': openai_messages
            }
            
            # Log the data being sent for summarization (for debugging)
            logger.info(f"Generating summary for session {session.session_id}:")
            logger.info(f"  - RFQ IDs: {rfq_list}")
            logger.info(f"  - Product items count: {len(product_items)}")
            logger.info(f"  - Combined entities keys: {list(combined_entities.keys())}")
            logger.info(f"  - Conversation history length: {len(openai_messages)}")

            # Call OpenAI service for summary generation
            summary_text = await self.openai_service.generate_session_summary(session_data)

            # Store summary
            with get_db_session() as db:
                summary = ChatSummary(
                    session_id=session.session_id,
                    external_user_id=session.external_user_id,
                    ai_generated_summary=summary_text,
                    extracted_entities=combined_entities,
                    rfq_ids=rfq_list,
                    session_outcome=session.outcome,
                    session_duration=self._calculate_duration(session)
                )
                
                db.add(summary)
                db.commit()
                db.refresh(summary)
                
                logger.info(f"Created summary for session {session.session_id}")
                return summary
                
        except Exception as e:
            logger.error(f"Error generating summary for {session.session_id}: {e}")
            return None
    
    @log_service_method("chat_summary_service")
    async def load_user_context(self, user_id: str) -> List[Dict[str, Any]]:
        """Load recent summaries for user context."""
        try:
            # Use shared session if available, otherwise create one
            if self.db_session:
                summaries = self.db_session.query(ChatSummary)\
                    .filter(ChatSummary.external_user_id == user_id)\
                    .order_by(ChatSummary.created_at.desc())\
                    .limit(self.settings.max_chat_summaries_for_context)\
                    .all()

                return [
                    {
                        'summary': s.ai_generated_summary,
                        'entities': s.extracted_entities,
                        'rfq_ids': s.rfq_ids,
                        'outcome': s.session_outcome,
                        'date': s.created_at.strftime('%Y-%m-%d')
                    }
                    for s in summaries
                ]
            else:
                # Fallback to creating session (backward compatibility)
                with get_db_session_context() as db:
                    summaries = db.query(ChatSummary)\
                        .filter(ChatSummary.external_user_id == user_id)\
                        .order_by(ChatSummary.created_at.desc())\
                        .limit(self.settings.max_chat_summaries_for_context)\
                        .all()

                    return [
                        {
                            'summary': s.ai_generated_summary,
                            'entities': s.extracted_entities,
                            'rfq_ids': s.rfq_ids,
                            'outcome': s.session_outcome,
                            'date': s.created_at.strftime('%Y-%m-%d')
                        }
                        for s in summaries
                    ]

        except Exception as e:
            logger.error(f"Error loading context for {user_id}: {e}")
            return []
    
    def _calculate_duration(self, session: ConversationSession) -> Optional[int]:
        """Calculate session duration in minutes."""
        try:
            if session.created_at and session.completed_at:
                duration = session.completed_at - session.created_at
                return int(duration.total_seconds() / 60)
            return None
        except Exception:
            return None