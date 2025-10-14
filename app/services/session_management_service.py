"""
Session Management Service.

Dedicated service for managing conversation sessions, workflow state,
and session lifecycle operations. Extracted from ChatService to reduce complexity.
"""

import logging
import asyncio
import inspect
from typing import Dict, Any, Optional, Union
from datetime import date, timedelta
from app.models import User, ConversationSession, WorkflowType
from app.database import DatabaseManager
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
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

        # Try to get existing session first
        session = self.db_manager.get_conversation_session(session_id)

        # Check if session exists but has been completed/abandoned (e.g., after exit)
        if session and session.outcome:
            from app.models import ConversationOutcome
            # If session was abandoned (exit), completed, or timed out by cleanup job, reset it for new conversation
            if session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                logger.info(f"Session {session_id} was {session.outcome.value}, resetting for new conversation")
                # Reset the session state for fresh start
                session.workflow_state = {"extracted_entities": [], "last_activity_at": utc_now().isoformat()}
                session.conversation_history = {"messages": []}
                session.extracted_entities = {}
                session.outcome = None
                session.completed_at = None
                session.workflow_type = None
                # Save the reset session
                session = self.db_manager.save_conversation_session({
                    'session_id': session.session_id,
                    'external_user_id': session.external_user_id,
                    'workflow_type': None,
                    'outcome': None,
                    'workflow_state': session.workflow_state,
                    'conversation_history': session.conversation_history,
                    'extracted_entities': session.extracted_entities,
                    'retention_date': session.retention_date,
                    'completed_at': None
                })
                logger.info(f"Session {session_id} reset successfully")

        if not session:
            # Create new session - the save method now handles duplicates gracefully
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
        else:
            logger.info(f"Found existing session: {session_id}")
            # Debug: Check workflow_state immediately after retrieval
            if session.workflow_state:
                has_optional = 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state
                logger.info(f"[GET_CONTEXT_DEBUG] Session {session_id} has_optional_fields={has_optional}, keys={list(session.workflow_state.keys())}")

        return session
    
    async def create_session(self, phone_number: str, workflow_type: str = None, user_type: str = None) -> ConversationSession:
        """Create a new session with specified workflow type and user type."""
        session_id = SessionHelpers.generate_session_id(phone_number, "daily")
        
        # Check if session already exists first
        existing_session = self.db_manager.get_conversation_session(session_id)
        if existing_session:
            logger.info(f"Session {session_id} already exists, returning existing session")
            return existing_session
        
        session_data = {
            'session_id': session_id,
            'external_user_id': phone_number,
            'workflow_type': workflow_type,
            'outcome': None,
            'workflow_state': {
                "extracted_entities": [], 
                "last_activity_at": utc_now().isoformat(),
                "user_type": user_type
            },
            'conversation_history': {"messages": []},
            'extracted_entities': {},
            'retention_date': date.today() + timedelta(days=30)
        }
        
        session = self.db_manager.save_conversation_session(session_data)
        logger.info(f"Created new session: {session_id} with workflow: {workflow_type}, user_type: {user_type}")
        
        return session
    
    async def handle_session_expiry_check(self, user_phone: str, session: ConversationSession) -> ConversationSession:
        """Handle session expiry check and renewal."""
        # Check if session has expired
        if await SessionHelpers.is_session_expired(session):
            # Only send expiration message if appropriate
            if await SessionHelpers.should_send_expiration_message(session):    
                await self.whatsapp_service.send_message(
                    user_phone,
                    "Welcome Back!"
                )

                
                # Generate enhanced session summary for timeout (non-blocking)
                await self._handle_session_completion_enhanced(session)
            
            # Handle session expiry properly
            session = await SessionHelpers.handle_session_expiry(session, self.db_manager)
        else:
            # Session is active, renew its activity timestamp
            session = await SessionHelpers.renew_session_activity(session)
            current_workflow = session.workflow_type or WorkflowType.general_inquiry
            await self.save_session(session, current_workflow)
        
        return session
    
    def add_message_to_history(self, session: ConversationSession, role: str, content: str, message_type: str = "text", intent: str = None, confidence: float = None):
        """Add message to conversation history."""
        SummarizationHelpers.add_to_conversation_history(session, role, content, message_type, intent, confidence)
    
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
    
    async def save_session(self, session: ConversationSession,
                          workflow_type: Optional[Union[WorkflowType, str]] = None) -> ConversationSession:
        """
        Save updated session to database.

        Args:
            session: Conversation session to save
            workflow_type: Workflow type (MUST be WorkflowType enum, strings deprecated)

        Returns:
            Updated conversation session
        """
        try:
            # Initialize workflow_state if needed
            WorkflowManager.initialize_workflow_state(session)

            # Handle workflow_type - prefer enum, but support string during migration
            if workflow_type:
                if isinstance(workflow_type, WorkflowType):
                    # Preferred: Enum passed
                    workflow_value = workflow_type.value
                elif isinstance(workflow_type, str):
                    # Deprecated: String passed - convert to enum and warn
                    logger.warning(f"[DEPRECATED] save_session called with string workflow_type: '{workflow_type}'. "
                                 f"Use WorkflowType enum instead. Caller: {inspect.stack()[1].function}")
                    try:
                        workflow_enum = WorkflowType(workflow_type)
                        workflow_value = workflow_enum.value
                    except (ValueError, KeyError):
                        logger.error(f"[SESSION_SAVE_ERROR] Invalid workflow_type string: '{workflow_type}'. "
                                   f"Defaulting to general_inquiry.")
                        workflow_value = WorkflowType.general_inquiry.value
                else:
                    logger.error(f"[SESSION_SAVE_ERROR] workflow_type is neither enum nor string: {type(workflow_type)}")
                    workflow_value = WorkflowType.general_inquiry.value
            else:
                # No workflow_type passed - use current from session
                current_workflow = WorkflowManager.get_workflow_type(session)
                workflow_value = (
                    current_workflow.value
                    if current_workflow
                    else WorkflowType.general_inquiry.value
                )

            # Clean workflow_state to ensure JSON serialization
            clean_workflow_state = self._clean_for_json_serialization(session.workflow_state) if session.workflow_state else {}

            # Debug logging to track pending_optional fields
            if 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state:
                logger.info(f"[SESSION_SAVE_DEBUG] Saving session with optional fields: {list(clean_workflow_state.keys())}")
                if 'pending_optional_combined_rfq' in clean_workflow_state:
                    logger.info(f"[SESSION_SAVE_DEBUG] pending_optional_combined_rfq keys: {list(clean_workflow_state['pending_optional_combined_rfq'].keys())}")

            session_data = {
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'workflow_type': workflow_value,
                'outcome': session.outcome.value if session.outcome and hasattr(session.outcome, 'value') else session.outcome,
                'workflow_state': clean_workflow_state,
                'conversation_history': session.conversation_history,
                'extracted_entities': session.extracted_entities,
                'retention_date': session.retention_date,
                'last_activity_at': session.last_activity_at
            }
            saved_session = self.db_manager.save_conversation_session(session_data)

            # Debug logging to verify save
            if 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state:
                logger.info(f"[SESSION_SAVE_DEBUG] Session saved, verifying workflow_state keys: {list(saved_session.workflow_state.keys()) if saved_session.workflow_state else 'None'}")

            return saved_session
        except Exception as e:
            logger.error(f"Error saving session: {e}")
            # Ensure session state is preserved even if save fails
            if hasattr(session, 'workflow_state') and session.workflow_state:
                session.workflow_state = clean_workflow_state
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