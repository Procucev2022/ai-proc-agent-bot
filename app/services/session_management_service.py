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
from app.models import User, ConversationSession, WorkflowType, ConversationOutcome, UserType
from app.database import DatabaseManager
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.utils.datetime_utils import utc_now
from app.utils.turn_trace import stage

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
        
        # Generate base session ID using helper method (daily strategy)
        base_session_id = SessionHelpers.generate_session_id(phone_number, "daily")
        session = None
        target_session_id = base_session_id

        # Try Redis first if enabled
        if self.redis_enabled:
            # Check if user has an active visit session pointer in Redis
            try:
                active_sid = await self.redis_session.get_user_active_session_id(phone_number)
                if active_sid:
                    target_session_id = active_sid
            except Exception:
                target_session_id = base_session_id

            logger.debug(f"Checking Redis for session: {target_session_id}")
            with stage("session_redis_lookup"):
                session_data = await self.redis_session.get_session(target_session_id)

            if session_data:
                candidate_session = self._dict_to_session(session_data)
                if candidate_session.outcome and candidate_session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                    logger.info(f"Found ended session {target_session_id} ({candidate_session.outcome.value}) in Redis - clearing active pointer and starting fresh visit session")
                    try:
                        await self.redis_session.clear_user_active_session_id(phone_number)
                    except Exception:
                        pass
                    session = None
                else:
                    logger.info(f"Found active session in Redis: {target_session_id}")
                    session = candidate_session
                    # Refresh TTL on activity
                    await self.redis_session.refresh_ttl(target_session_id)
                    try:
                        await self.redis_session.set_user_active_session_id(phone_number, target_session_id)
                    except Exception:
                        pass
                    logger.debug(f"Refreshed TTL for session: {target_session_id}")
                    return session
            else:
                logger.info(
                    f"[SESSION] Session {target_session_id} not found in Redis - checking DB/creating fresh session."
                )
                session = None

        # Fallback to database if session was not found in Redis (or Redis disabled)
        if not session and hasattr(self.db_manager, "get_conversation_session"):
            logger.debug(f"Checking database for session: {target_session_id}")
            try:
                candidate_session = self.db_manager.get_conversation_session(target_session_id)
                if not candidate_session and target_session_id != base_session_id:
                    candidate_session = self.db_manager.get_conversation_session(base_session_id)

                if candidate_session:
                    if candidate_session.outcome and candidate_session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                        if not self.redis_enabled:
                            session = candidate_session
                        else:
                            logger.info(f"Found ended DB session {candidate_session.session_id} ({candidate_session.outcome})")
                            candidate_session = None
                    else:
                        logger.info(f"Found active session in DB: {candidate_session.session_id}, restoring to Redis")
                        session = candidate_session
                        if self.redis_enabled:
                            try:
                                redis_data = self._session_to_dict(session)
                                await self.redis_session.store_session(session.session_id, redis_data)
                                await self.redis_session.set_user_active_session_id(phone_number, session.session_id)
                            except Exception as re_err:
                                logger.warning(f"Failed to restore DB session to Redis: {re_err}")
            except Exception as db_err:
                logger.warning(f"DB session lookup error for {target_session_id}: {db_err}")

        # Check if session exists but has been completed/abandoned (e.g. when Redis is disabled)
        if session and session.outcome:
            if session.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                logger.info(f"Session {session.session_id} was {session.outcome.value if hasattr(session.outcome, 'value') else session.outcome}, resetting for COMPLETELY fresh workflow (no state preserved)")
                session.workflow_state = {"extracted_entities": [], "last_activity_at": utc_now().isoformat()}
                session.conversation_history = {"messages": [], "metadata": [], "openai_messages": []}
                session.extracted_entities = {}
                session.outcome = None
                session.completed_at = None
                session.workflow_type = None

                if self.redis_enabled:
                    await self.redis_session.store_session(session.session_id, self._session_to_dict(session))
                    logger.info(f"Session {session.session_id} reset in Redis for new workflow (DB data preserved)")
                else:
                    logger.warning(f"Redis disabled - resetting session {session.session_id} in database (history cleared for fresh workflow)")
                    if hasattr(self.db_manager, "save_conversation_session"):
                        session = self.db_manager.save_conversation_session({
                            'session_id': session.session_id,
                            'external_user_id': session.external_user_id,
                            'workflow_type': None,
                            'outcome': None,
                            'workflow_state': session.workflow_state,
                            'conversation_history': {"messages": [], "metadata": [], "openai_messages": []},
                            'extracted_entities': session.extracted_entities,
                            'retention_date': session.retention_date,
                            'completed_at': None
                        })

        if not session:
            # Check if this user already has an established user_type (buyer or seller) in past DB sessions
            established_user_type = 'unknown'
            if hasattr(self.db_manager, "session"):
                try:
                    past_known_session = self.db_manager.session.query(ConversationSession).filter(
                        ConversationSession.external_user_id == phone_number,
                        ConversationSession.user_type.in_([UserType.buyer, UserType.seller])
                    ).order_by(ConversationSession.started_at.desc()).first()
                    if past_known_session and past_known_session.user_type:
                        established_user_type = past_known_session.user_type.value if hasattr(past_known_session.user_type, 'value') else str(past_known_session.user_type)
                        logger.info(f"User {phone_number} previously established as {established_user_type}, preserving user_type")
                except Exception as e:
                    logger.debug(f"Could not check past user_type for {phone_number}: {e}")

            # Check if an ended base session already exists in DB (meaning this is a return visit after exit/completion)
            existing_ended_db_session = None
            if hasattr(self.db_manager, "get_conversation_session"):
                try:
                    candidate = self.db_manager.get_conversation_session(base_session_id)
                    if candidate and candidate.outcome in [ConversationOutcome.abandoned, ConversationOutcome.completed, ConversationOutcome.timeout]:
                        existing_ended_db_session = candidate
                except Exception:
                    existing_ended_db_session = None

            if existing_ended_db_session is not None and self.redis_enabled:
                import uuid
                session_id = f"{base_session_id}_{uuid.uuid4().hex[:6]}"
            else:
                session_id = base_session_id

            # Create new session
            logger.info(f"Creating new session: {session_id}")
            session_data = {
                'session_id': session_id,
                'external_user_id': phone_number,
                'user_type': established_user_type,
                'workflow_type': None,
                'outcome': None,
                'workflow_state': {"extracted_entities": [], "last_activity_at": utc_now().isoformat()},
                'conversation_history': {"messages": []},
                'extracted_entities': {},
                'retention_date': (date.today() + timedelta(days=30)).isoformat(),
                'created_at': utc_now().isoformat(),
                'last_activity_at': utc_now().isoformat()
            }

            # Save to Redis if enabled, and persist new session to DB for immediate dashboard analytics
            if self.redis_enabled:
                with stage("session_store"):
                    await self.redis_session.store_session(session_id, session_data)
                    if hasattr(self.redis_session, "set_user_active_session_id"):
                        await self.redis_session.set_user_active_session_id(phone_number, session_id)
                session = self._dict_to_session(session_data)
                logger.info(f"Created new session in Redis: {session_id}")

            with stage("session_store_db"):
                try:
                    saved_db_session = self.db_manager.save_conversation_session(session_data)
                    if not self.redis_enabled or session is None:
                        session = saved_db_session
                    logger.info(f"Created new session in DB: {session_id}")
                except Exception as e:
                    logger.warning(f"Failed to persist new session {session_id} to DB: {e}")

            if session is None:
                session = self._dict_to_session(session_data)
            
            # Check and send welcome message for new session
            from app.services.welcome_message_service import get_welcome_service
            welcome_service = get_welcome_service()
            wa_service = getattr(self, "whatsapp_service", None)
            if wa_service is not None:
                with stage("welcome_message"):
                    await welcome_service.check_and_send_welcome(phone_number, wa_service)
        else:
            logger.info(f"Found existing session: {session.session_id}")
            # Store in Redis for future requests if Redis enabled and not already there
            if self.redis_enabled:
                redis_exists = await self.redis_session.session_exists(session.session_id)
                if not redis_exists:
                    await self.redis_session.store_session(session.session_id, self._session_to_dict(session))
                    logger.debug(f"Cached session from DB to Redis: {session.session_id}")

            # Debug: Check workflow_state immediately after retrieval
            if session.workflow_state:
                has_optional = 'pending_optional_rfq' in session.workflow_state or 'pending_optional_combined_rfq' in session.workflow_state
                logger.info(f"[GET_CONTEXT_DEBUG] Session {session_id} has_optional_fields={has_optional}, keys={list(session.workflow_state.keys())}")

        # Emit real-time analytics event
        try:
            from app.services.realtime_analytics_service import get_realtime_analytics_service
            rt_service = get_realtime_analytics_service()
            u_type = session.user_type.value if hasattr(session, "user_type") and hasattr(session.user_type, "value") else str(session.user_type or "unknown")
            asyncio.create_task(
                rt_service.publish_event(
                    event_type="whatsapp_visitor",
                    user_id=phone_number,
                    data={"session_id": session_id, "user_type": u_type},
                    session_id=session_id,
                    persist_db=False
                )
            )
        except Exception:
            pass

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

            # Ensure user_type is properly captured
            u_type_enum = getattr(session, 'user_type', None)
            if isinstance(u_type_enum, str):
                try:
                    u_type_enum = UserType(u_type_enum.lower())
                except Exception:
                    u_type_enum = UserType.unknown
            elif not isinstance(u_type_enum, UserType):
                u_type_enum = UserType.unknown

            session_data = {
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'user_type': u_type_enum,
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
                try:
                    await self.redis_session.set_user_active_session_id(str(session.external_user_id or ""), session.session_id)
                except Exception:
                    pass
                logger.debug(f"Saved session to Redis: {session.session_id} (persist_to_db={persist_to_db})")

                # Emit real-time chat event so dashboard updates instantly
                try:
                    from app.services.realtime_analytics_service import get_realtime_analytics_service
                    rt_service = get_realtime_analytics_service()
                    u_type = session.user_type.value if hasattr(session, "user_type") and hasattr(session.user_type, "value") else str(session.user_type or "unknown")
                    asyncio.create_task(
                        rt_service.publish_event(
                            event_type="chat_message",
                            user_id=str(session.external_user_id or ""),
                            data={"session_id": session.session_id, "user_type": u_type, "phone": str(session.external_user_id or "")},
                            session_id=session.session_id,
                            persist_db=False
                        )
                    )
                except Exception:
                    pass
            elif self.redis_enabled and not should_save_to_redis:
                try:
                    await self.redis_session.delete_session(session.session_id)
                    await self.redis_session.clear_user_active_session_id(str(session.external_user_id or ""))
                except Exception:
                    pass
                logger.info(f"[PREVENT_REDIS_SAVE] Deleted ended session from Redis: {session.session_id}")
                logger.debug(f"[PREVENT_REDIS_SAVE] Reason - outcome={session.outcome}, exit_completed={session.workflow_state.get('exit_completed') if session.workflow_state else None}")

            # Only persist to DB when explicitly requested or Redis disabled
            if persist_to_db or not self.redis_enabled:
                try:
                    if hasattr(self.db_manager, "save_conversation_session"):
                        saved_session = self.db_manager.save_conversation_session(session_data)
                        logger.debug(f"Persisted session to database: {session.session_id}")
                        return saved_session
                except Exception as db_err:
                    logger.error(f"[SESSION_SAVE_DB_ERROR] Failed to save session to DB: {db_err}")
                return session
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
            })
            
            # Save enhanced summary data to session
            await self.save_session(session)
            
            # Run summarization in background to avoid blocking user
            asyncio.create_task(self._run_background_summarization(session, enhanced_summary_data))
            
        except Exception as e:
            logger.error(f"Error in enhanced session completion handling: {e}")

    async def _run_background_summarization(self, session: ConversationSession, enhanced_data: Dict[str, Any]) -> None:
        """Run summarization in background with rich context."""
        try:
            from app.services.chat_summary_service import ChatSummaryService
            summary_service = ChatSummaryService()
            await summary_service.generate_and_save_summary(session, enhanced_data)
        except Exception as e:
            logger.error(f"Background summarization failed for session {session.session_id}: {e}")
    
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
    
    def _clean_for_json_serialization(self, obj: Any, _visited: Optional[set] = None) -> Any:
        """Helper method to clean nested objects for JSON serialization."""
        if _visited is None:
            _visited = set()
        
        # Handle circular references
        obj_id = id(obj)
        if obj_id in _visited:
            return "<circular_reference>"
            
        if isinstance(obj, dict):
            _visited.add(obj_id)
            try:
                return {k: self._clean_for_json_serialization(v, _visited) for k, v in obj.items()}
            finally:
                _visited.discard(obj_id)
        elif isinstance(obj, (list, tuple)):
            _visited.add(obj_id)
            try:
                return [self._clean_for_json_serialization(item, _visited) for item in obj]
            finally:
                _visited.discard(obj_id)
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        else:
            try:
                import json
                json.dumps(obj)
                return obj
            except (TypeError, ValueError, RecursionError):
                return str(obj)

    def _session_to_dict(self, session: ConversationSession) -> Dict[str, Any]:
        """Convert ConversationSession object to dict for Redis storage."""
        from app.models import WorkflowType, ConversationOutcome, UserType
        from datetime import date, datetime
        from app.services.helpers.session_helpers import SessionHelpers

        # Helper to serialize dates/datetimes
        def serialize_date(value):
            if value is None:
                return None
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            return str(value)

        u_type_str = session.user_type.value if hasattr(session, "user_type") and hasattr(session.user_type, "value") else str(getattr(session, "user_type", None) or "unknown")

        # Build the dictionary with top-level serialization
        session_dict = {
            'session_id': session.session_id,
            'external_user_id': session.external_user_id,
            'user_type': u_type_str,
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
        from app.models import WorkflowType, ConversationOutcome, UserType

        raw_u_type = data.get('user_type')
        if raw_u_type:
            try:
                user_type_enum = UserType(raw_u_type.lower())
            except Exception:
                user_type_enum = UserType.unknown
        else:
            user_type_enum = UserType.unknown

        session = ConversationSession(
            session_id=data['session_id'],
            external_user_id=data['external_user_id'],
            user_type=user_type_enum,
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