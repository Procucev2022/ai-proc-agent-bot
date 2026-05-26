"""
Session Management Service.

Dedicated service for managing conversation sessions, workflow state,
and session lifecycle operations. Extracted from ChatService to reduce complexity.
"""

import logging
import asyncio
import inspect
from typing import Dict, Any, Optional, Union, Tuple
from datetime import date, timedelta
from app.models import User, ConversationSession, WorkflowType, ConversationOutcome
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
        self.summarization_helpers = SummarizationHelpers()

        # Initialize Redis session service
        from app.redis_db import get_session_redis_service
        from app.config import get_settings
        self.redis_session = get_session_redis_service()
        self.settings = get_settings()
        self.redis_enabled = self.settings.redis_session_storage_enabled

        # Service is instantiated per request - keep at DEBUG
        logger.debug(f"SessionManagementService initialized with Redis storage: {self.redis_enabled}")
    
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
    
    def _validate_license(self) -> Tuple[bool, str]:
        """Validate license before session creation."""
        try:
            # Skip if license validation is disabled
            if not self.settings.license_enabled:
                logger.debug("License validation skipped (disabled in config)")
                return True, "License validation disabled"

            from app.license import validate_license
            is_valid, message = validate_license()

            if not is_valid:
                logger.warning(f"License validation failed: {message}")
            else:
                logger.info(f"License validation passed: {message}")

            return is_valid, message
        except ImportError as e:
            logger.error(f"License module import failed: {e}")
            return True, f"License validation skipped: {e}"
        except Exception as e:
            logger.error(f"License validation error: {e}")
            return False, "License validation failed"
    
    async def get_conversation_context(self, phone_number: str) -> ConversationSession:
        """Retrieve or create conversation context for user session."""
        # Validate license before creating session
        is_valid, message = self._validate_license()
        if not is_valid:
            logger.warning(f"License validation failed for {phone_number}: {message}")
            raise Exception("The session could not be initiated due to a technical issue. Please try again after some time. If the issue persists, please contact support.")
        
        # Generate session ID using helper method (configurable strategy)
        session_id = SessionHelpers.generate_session_id(phone_number, "daily")
        session = None

        # Try Redis first if enabled
        if self.redis_enabled:
            logger.debug(f"Checking Redis for session: {session_id}")
            session_data = await self.redis_session.get_session(session_id)

            if session_data:
                logger.info(f"Found session in Redis: {session_id}")
                # Convert dict back to ConversationSession object
                session = self._dict_to_session(session_data)

                # Refresh TTL on activity
                await self.redis_session.refresh_ttl(session_id)
                logger.debug(f"Refreshed TTL for session: {session_id}")

                return session
            else:
                # Session not in Redis (TTL expired or first time)
                # Don't do anything here - just create fresh session below
                # The welcome message logic will check DB later if needed
                logger.info(f"Session {session_id} not found in Redis - will create fresh session")
                session = None

        # Fallback to database ONLY if Redis is disabled
        if not session and not self.redis_enabled:
            logger.debug(f"Checking database for session: {session_id}")
            session = self.db_manager.get_conversation_session(session_id)

        # Check if session exists but has been completed/abandoned
        if session and session.outcome:
            if session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                # IMPORTANT: For role switches and exits, we ALWAYS want a completely fresh session
                # Do NOT preserve ANY workflow_state from the abandoned session
                logger.info(f"Session {session_id} was {session.outcome.value}, resetting for COMPLETELY fresh workflow (no state preserved)")

                # CRITICAL: Clear ALL workflow_state, conversation_history, and entities
                # This ensures role switches get a truly fresh start with no data from previous sessions
                session.workflow_state = {"extracted_entities": [], "last_activity_at": utc_now().isoformat()}
                session.conversation_history = {"messages": [], "metadata": [], "openai_messages": []}
                session.extracted_entities = {}
                session.outcome = None
                session.completed_at = None
                session.workflow_type = None

                # Save reset state to Redis ONLY (don't touch database)
                # Database keeps all accumulated history via append_session_data
                if self.redis_enabled:
                    await self.redis_session.store_session(session_id, self._session_to_dict(session))
                    logger.info(f"Session {session_id} reset in Redis for new workflow (DB data preserved)")
                else:
                    # If Redis disabled, we have no choice but to update DB
                    # Clear conversation_history for fresh workflow (prevent stale data leak)
                    logger.warning(f"Redis disabled - resetting session {session_id} in database (history cleared for fresh workflow)")
                    session = self.db_manager.save_conversation_session({
                        'session_id': session.session_id,
                        'external_user_id': session.external_user_id,
                        'workflow_type': None,
                        'outcome': None,
                        'workflow_state': session.workflow_state,
                        'conversation_history': {"messages": [], "metadata": [], "openai_messages": []},  # Cleared for fresh workflow
                        'extracted_entities': session.extracted_entities,  # Empty for new workflow
                        'retention_date': session.retention_date,
                        'completed_at': None
                    })

        if not session:
            # Create new session
            logger.info(f"Creating new session: {session_id}")
            session_data = {
                'session_id': session_id,
                'external_user_id': phone_number,
                'workflow_type': None,
                'outcome': None,
                'workflow_state': {"extracted_entities": [], "last_activity_at": utc_now().isoformat()},
                'conversation_history': {"messages": []},
                'extracted_entities': {},
                'retention_date': (date.today() + timedelta(days=30)).isoformat(),
                'created_at': utc_now().isoformat(),
                'last_activity_at': utc_now().isoformat()
            }

            # Save to Redis if enabled, otherwise to DB
            if self.redis_enabled:
                await self.redis_session.store_session(session_id, session_data)
                session = self._dict_to_session(session_data)
                logger.info(f"Created new session in Redis: {session_id}")
            else:
                session = self.db_manager.save_conversation_session(session_data)
                logger.info(f"Created new session in DB: {session_id}")
            
            # Check and send welcome message for new session
            from app.services.welcome_message_service import get_welcome_service
            welcome_service = get_welcome_service()
            await welcome_service.check_and_send_welcome(phone_number, self.whatsapp_service)
        else:
            logger.info(f"Found existing session: {session_id}")
            # Store in Redis for future requests if Redis enabled and not already there
            if self.redis_enabled:
                redis_exists = await self.redis_session.session_exists(session_id)
                if not redis_exists:
                    await self.redis_session.store_session(session_id, self._session_to_dict(session))
                    logger.debug(f"Cached session from DB to Redis: {session_id}")

            # Debug: Check workflow_state immediately after retrieval
            if session.workflow_state:
                has_optional = 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state
                logger.info(f"[GET_CONTEXT_DEBUG] Session {session_id} has_optional_fields={has_optional}, keys={list(session.workflow_state.keys())}")

        return session
    
    async def create_session(self, phone_number: str, workflow_type: str = None, user_type: str = None) -> ConversationSession:
        """Create a new session with specified workflow type and user type."""
        # Validate license before creating session
        is_valid, message = self._validate_license()
        if not is_valid:
            logger.warning(f"License validation failed for {phone_number}: {message}")
            raise Exception("The session could not be initiated due to a technical issue. Please try again after some time. If the issue persists, please contact support.")
        
        session_id = SessionHelpers.generate_session_id(phone_number, "daily")

        # Check if session already exists first
        existing_session = self.db_manager.get_conversation_session(session_id)
        if existing_session:
            # CRITICAL: If session was abandoned/completed/timeout, clear ALL workflow_state for fresh start
            # This is essential for role switches to get a completely fresh session
            if existing_session.outcome and existing_session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                logger.info(f"Session {session_id} already exists with outcome={existing_session.outcome.value}, clearing workflow_state for fresh start")
                existing_session.workflow_state = {"extracted_entities": [], "last_activity_at": utc_now().isoformat(), "user_type": user_type}
                existing_session.conversation_history = {"messages": [], "metadata": [], "openai_messages": []}
                existing_session.extracted_entities = {}
                existing_session.outcome = None
                existing_session.completed_at = None
                existing_session.workflow_type = workflow_type

                # Save cleared state to Redis
                if self.redis_enabled:
                    await self.redis_session.store_session(session_id, self._session_to_dict(existing_session))
                    logger.info(f"Cleared abandoned session {session_id} and saved to Redis for fresh workflow")

                return existing_session
            else:
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
        
        # Check and send welcome message for new session
        from app.services.welcome_message_service import get_welcome_service
        welcome_service = get_welcome_service()
        await welcome_service.check_and_send_welcome(phone_number, self.whatsapp_service)
        
        return session
    
    async def handle_session_expiry_check(self, user_phone: str, session: ConversationSession) -> ConversationSession:
        """
        DEPRECATED: Session expiry now handled by InactivityTimeoutService.
        
        This method is kept for backward compatibility but does nothing.
        The InactivityTimeoutService actively monitors user activity and handles
        timeouts at the configured interval (default: 30 minutes).
        
        Redis TTL serves as a safety net for any sessions that bypass timeout monitoring.
        
        To be removed in future version after InactivityTimeoutService is stable.
        
        Args:
            user_phone: User's phone number
            session: Current conversation session
            
        Returns:
            Unchanged session (no processing)
        """
        logger.debug(
            f"handle_session_expiry_check called for {user_phone} but disabled "
            f"(using InactivityTimeoutService for timeout management)"
        )
        return session  # Just return session unchanged
    
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
                          workflow_type: Optional[Union[WorkflowType, str]] = None,
                          persist_to_db: bool = False) -> ConversationSession:
        """
        Save updated session to Redis (and optionally database).

        Args:
            session: Conversation session to save
            workflow_type: Workflow type (MUST be WorkflowType enum, strings deprecated)
            persist_to_db: If True, also persist to database (for RFQ completion, exit, etc.)

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

            # CRITICAL: Update session.workflow_type attribute so Redis gets the correct value
            # Previously, workflow_value was only used for DB persistence, but Redis reads from session.workflow_type
            if workflow_type:
                if isinstance(workflow_type, WorkflowType):
                    session.workflow_type = workflow_type
                elif isinstance(workflow_type, str):
                    # Convert string to enum if valid
                    try:
                        session.workflow_type = WorkflowType(workflow_type)
                    except (ValueError, KeyError):
                        session.workflow_type = WorkflowType.general_inquiry
                else:
                    session.workflow_type = WorkflowType.general_inquiry
            # If no workflow_type passed and session.workflow_type is None, set to general_inquiry
            elif not session.workflow_type:
                session.workflow_type = WorkflowType.general_inquiry

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

            # Only save to Redis if session is NOT completed/abandoned/exited
            # Once a session is exited, it should stay deleted from Redis (data is in database for audit trail)
            should_save_to_redis = True
            if session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                should_save_to_redis = False
                logger.info(f"[PREVENT_REDIS_SAVE] Session {session.session_id} marked as {session.outcome.value}, NOT saving to Redis (staying deleted)")
            elif session.workflow_state and session.workflow_state.get("exit_completed"):
                should_save_to_redis = False
                logger.info(f"[PREVENT_REDIS_SAVE] Session {session.session_id} has exit_completed flag, NOT saving to Redis (staying deleted)")

            if self.redis_enabled and should_save_to_redis:
                # Use _session_to_dict helper which properly serializes dates
                redis_data = self._session_to_dict(session)
                await self.redis_session.store_session(session.session_id, redis_data)
                await self.redis_session.refresh_ttl(session.session_id)
                logger.debug(f"Saved session to Redis: {session.session_id} (persist_to_db={persist_to_db})")
            elif self.redis_enabled and not should_save_to_redis:
                logger.info(f"[PREVENT_REDIS_SAVE] Skipped Redis save for exited session: {session.session_id}")
                logger.debug(f"[PREVENT_REDIS_SAVE] Reason - outcome={session.outcome}, exit_completed={session.workflow_state.get('exit_completed') if session.workflow_state else None}")

            # Only persist to DB when explicitly requested or Redis disabled
            if persist_to_db or not self.redis_enabled:
                saved_session = self.db_manager.save_conversation_session(session_data)
                logger.info(f"Persisted session to database: {session.session_id}")

                # Debug logging to verify save
                if 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state:
                    logger.info(f"[SESSION_SAVE_DEBUG] Session saved to DB, verifying workflow_state keys: {list(saved_session.workflow_state.keys()) if saved_session.workflow_state else 'None'}")

                return saved_session
            else:
                # Return the session object unchanged (data saved to Redis)
                return session

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
                'enhanced_entities': rich_entities,
                'conversation_history': session.conversation_history if hasattr(session, 'conversation_history') else {}
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
    
    def _clean_for_json_serialization(self, obj, _visited=None):
        """Recursively clean object for JSON serialization with circular reference protection."""
        import json
        from datetime import datetime, date

        # Initialize visited set for circular reference detection
        if _visited is None:
            _visited = set()

        # Check for circular reference using object id
        obj_id = id(obj)
        if obj_id in _visited:
            logger.warning(f"[CIRCULAR_REF] Detected circular reference in object, converting to placeholder")
            return "<circular_reference>"

        if obj is None:
            return None
        elif hasattr(obj, 'value'):  # Enum object
            return obj.value
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, dict):
            # Add to visited set before recursion
            _visited.add(obj_id)
            try:
                return {key: self._clean_for_json_serialization(value, _visited) for key, value in obj.items()}
            finally:
                # Remove after processing to allow same object in different branches
                _visited.discard(obj_id)
        elif isinstance(obj, (list, tuple)):
            # Add to visited set before recursion
            _visited.add(obj_id)
            try:
                return [self._clean_for_json_serialization(item, _visited) for item in obj]
            finally:
                _visited.discard(obj_id)
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        else:
            # Try to serialize to test, if it fails, convert to string
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError, RecursionError):
                return str(obj)

    def _session_to_dict(self, session: ConversationSession) -> Dict[str, Any]:
        """Convert ConversationSession object to dict for Redis storage."""
        from app.models import WorkflowType, ConversationOutcome
        from datetime import date, datetime
        from app.services.helpers.session_helpers import SessionHelpers

        # Helper to serialize dates/datetimes
        def serialize_date(value):
            if value is None:
                return None
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            return str(value)

        # Build the dictionary with top-level serialization
        session_dict = {
            'session_id': session.session_id,
            'external_user_id': session.external_user_id,
            'workflow_type': session.workflow_type.value if isinstance(session.workflow_type, WorkflowType) else session.workflow_type,
            'outcome': session.outcome.value if isinstance(session.outcome, ConversationOutcome) else session.outcome,
            'workflow_state': session.workflow_state or {},
            'conversation_history': session.conversation_history or {},
            'extracted_entities': session.extracted_entities or {},
            'whatsapp_context': session.whatsapp_context or {},
            'retention_date': serialize_date(session.retention_date),
            'created_at': serialize_date(session.created_at),
            'last_activity_at': serialize_date(session.last_activity_at),
            'completed_at': serialize_date(session.completed_at),
        }

        # Recursively clean all nested data to ensure JSON serialization
        return SessionHelpers.clean_for_json_serialization(session_dict)

    def _dict_to_session(self, data: Dict[str, Any]) -> ConversationSession:
        """Convert dict from Redis to ConversationSession object."""
        from datetime import datetime
        from app.models import WorkflowType, ConversationOutcome

        session = ConversationSession(
            session_id=data['session_id'],
            external_user_id=data['external_user_id'],
            workflow_state=data.get('workflow_state', {}),
            conversation_history=data.get('conversation_history', {}),
            extracted_entities=data.get('extracted_entities', {}),
            whatsapp_context=data.get('whatsapp_context', {}),
            retention_date=datetime.fromisoformat(data['retention_date']).date() if data.get('retention_date') else None,
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else None,
            last_activity_at=datetime.fromisoformat(data['last_activity_at']) if data.get('last_activity_at') else None,
            completed_at=datetime.fromisoformat(data['completed_at']) if data.get('completed_at') else None,
        )

        # Handle enums
        if data.get('workflow_type'):
            try:
                session.workflow_type = WorkflowType(data['workflow_type'])
            except (ValueError, KeyError):
                session.workflow_type = data['workflow_type']

        if data.get('outcome'):
            try:
                session.outcome = ConversationOutcome(data['outcome'])
            except (ValueError, KeyError):
                session.outcome = data['outcome']

        return session

    def _should_persist_abandoned(self, session: ConversationSession) -> bool:
        """Check if abandoned session should be persisted to DB."""
        # Only persist if session has meaningful conversation
        if not session.conversation_history:
            return False

        messages = session.conversation_history.get('openai_messages', [])
        return len(messages) > 2  # More than just greeting + auth