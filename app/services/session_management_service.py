"""
Session Management Service.

Dedicated service for managing conversation sessions, workflow state,
and session lifecycle operations. Extracted from ChatService to reduce complexity.
"""

import logging
import asyncio
from typing import Dict, Any, Optional
from datetime import date, timedelta
from app.models import User, ConversationSession
from app.database import DatabaseManager
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.whatsapp_service import WhatsAppService
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class SessionManagementService:
    """Manages conversation sessions and workflow state."""
    
    def __init__(self, db_manager: DatabaseManager, whatsapp_service: WhatsAppService,
                 chat_summary_service: ChatSummaryService, daily_summary_service: DailySummaryService):
        self.db_manager = db_manager
        self.whatsapp_service = whatsapp_service
        self.chat_summary_service = chat_summary_service
        self.daily_summary_service = daily_summary_service
    
    async def get_or_create_user(self, phone_number: str) -> User:
        """Get existing user or create new one."""
        # Mock user for testing without database
        class MockUser:
            def __init__(self, phone_number):
                self.id = 1
                self.phone_number = phone_number
                self.name = "Test User"
                self.is_registered = True
                self.role = "buyer"
        
        return MockUser(phone_number)
    
    async def get_conversation_context(self, phone_number: str) -> ConversationSession:
        """Retrieve or create conversation context for user session."""
        # Generate session ID using helper method (configurable strategy)
        session_id = SessionHelpers.generate_session_id(phone_number, "daily")
    
        # Try to get existing session
        session = self.db_manager.get_conversation_session(session_id)
        
        if not session:
            # Create new session
            session_data = {
                'session_id': session_id,
                'external_user_id': phone_number,
                'workflow_type': None,
                'outcome': None,
                'workflow_state': {"extracted_entities": [], "last_activity_at": utc_now().isoformat()},
                'conversation_history': {"messages": []},
                'extracted_entities': {},
                'retention_date': date.today() + timedelta(days=30)
            }
            session = self.db_manager.save_conversation_session(session_data)
            
            logger.info(f"Created new session: {session_id}")
            # Show authentication placeholder for new session
            # await self._show_auth_placeholder(phone_number)
        else:
            logger.info(f"Found existing session: {session_id}")
        
        return session
    
    async def handle_session_expiry_check(self, user_phone: str, session: ConversationSession) -> ConversationSession:
        """Handle session expiry check and renewal."""
        # Check if session has expired
        if await SessionHelpers.is_session_expired(session):
            # Only send expiration message if appropriate
            if await SessionHelpers.should_send_expiration_message(session):    
                await self.whatsapp_service.send_message(
                    user_phone, 
                    "Your session has expired. Let's start fresh!"
                )
                
                # Generate enhanced session summary for timeout (non-blocking)
                await self._handle_session_completion_enhanced(session)
            
            # Handle session expiry properly
            session = await SessionHelpers.handle_session_expiry(session, self.db_manager)
        else:
            # Session is active, renew its activity timestamp
            session = await SessionHelpers.renew_session_activity(session)
            await self.save_session(session, session.workflow_type or 'general_inquiry')
        
        return session
    
    def add_message_to_history(self, session: ConversationSession, role: str, content: str, message_type: str = "text"):
        """Add message to conversation history."""
        SummarizationHelpers.add_to_conversation_history(session, role, content, message_type)
    
    async def send_and_track_message(self, phone_number: str, message: str, 
                                    session: ConversationSession, message_type: str = "text") -> None:
        """Send message via WhatsApp and automatically track in conversation history."""
        try:
            # Send the message
            await self.whatsapp_service.send_message(phone_number, message)
            
            # Track assistant response in conversation history
            self.add_message_to_history(session, "assistant", message, message_type)
            
            logger.info(f"Sent and tracked message for session {session.session_id}")
            
        except Exception as e:
            logger.error(f"Error sending and tracking message: {e}")
            # Still try to send the message even if tracking fails
            try:
                await self.whatsapp_service.send_message(phone_number, message)
            except Exception as send_error:
                logger.error(f"Failed to send message after tracking error: {send_error}")
                raise
    
    async def save_session(self, session: ConversationSession, workflow_type: str) -> ConversationSession:
        """Save updated session to database."""
        try:
            # Clean workflow_state to ensure JSON serialization
            clean_workflow_state = self._clean_for_json_serialization(session.workflow_state) if session.workflow_state else {}
            
            session_data = {
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'workflow_type': workflow_type,
                'outcome': session.outcome.value if session.outcome and hasattr(session.outcome, 'value') else session.outcome,
                'workflow_state': clean_workflow_state,
                'conversation_history': session.conversation_history,
                'extracted_entities': session.extracted_entities,
                'retention_date': session.retention_date,
                'last_activity_at': session.last_activity_at
            }
            return self.db_manager.save_conversation_session(session_data)
        except Exception as e:
            logger.error(f"Error saving session: {e}")
            return session

    async def handle_session_completion_enhanced(self, session: ConversationSession) -> None:
        """
        Handle session completion with enhanced summarization.
        
        Captures rich session data BEFORE clearing and runs summarization
        in background to avoid blocking user experience.
        """
        try:
            # Extract rich entities BEFORE session is cleared
            rich_entities = SummarizationHelpers.extract_rich_entities_for_summary(session)
            
            # Store rich entities in session.extracted_entities for persistence
            session.extracted_entities = rich_entities
            
            # Prepare enhanced summary data
            enhanced_summary_data = SummarizationHelpers.prepare_enhanced_summary_data(session, rich_entities)
            enhanced_summary_data.update({
                'session_id': session.session_id,
                'created_at': session.created_at,
                'completed_at': session.completed_at,
                'enhanced_entities': rich_entities
            })
            
            # Start background summarization (non-blocking)
            asyncio.create_task(
                SummarizationHelpers.handle_session_completion_async(
                    self.chat_summary_service,
                    self.daily_summary_service,
                    enhanced_summary_data
                )
            )
            
            logger.info(f"Started background summarization for session {session.session_id}")
            
        except Exception as e:
            logger.error(f"Error starting enhanced session completion for {session.session_id}: {e}") 
            # Fallback to original method
            await self._handle_session_completion_fallback(session)
    
    async def _handle_session_completion_enhanced(self, session: ConversationSession) -> None:
        """Wrapper for enhanced session completion."""
        await self.handle_session_completion_enhanced(session)
    
    async def _handle_session_completion_fallback(self, session: ConversationSession) -> None:
        """Fallback session completion method (original logic)."""
        try:
            await self.chat_summary_service.generate_session_summary(session)
            await self.daily_summary_service.generate_daily_summary(session.external_user_id)
            logger.info(f"Generated summaries for completed session {session.session_id}")
        except Exception as e:
            logger.error(f"Error generating summaries for session {session.session_id}: {e}")
    
    async def _show_auth_placeholder(self, user_phone: str) -> None:
        """Show authentication placeholder message for new sessions."""
        try:
            message = "Authentication system is in progress, continuing with your request..."
            await self.whatsapp_service.send_message(user_phone, message)
            logger.info(f"Sent authentication placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending authentication placeholder: {e}")
    
    def _clean_for_json_serialization(self, obj):
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
            return {key: self._clean_for_json_serialization(value) for key, value in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._clean_for_json_serialization(item) for item in obj]
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        else:
            # Try to serialize to test, if it fails, convert to string
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)