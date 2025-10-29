"""
Exit Service for handling system exit logic.

Provides complete session and authentication cleanup when users want to exit the system.
Handles token clearing, session cleanup, and sends appropriate goodbye messages.
"""

import logging
from typing import Dict, Any, Optional
from app.models import WorkflowType, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.authentication_service import AuthenticationService
from app.services.session_management_service import SessionManagementService
from app.database import DatabaseManager
from app.utils.datetime_utils import utc_now


logger = logging.getLogger(__name__)


class ExitService:
    """Handles complete system exit logic including cleanup and goodbye messages."""

    def __init__(self, whatsapp_service: WhatsAppService = None,
                 authentication_service: AuthenticationService = None,
                 session_manager: SessionManagementService = None,
                 db_manager: DatabaseManager = None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.authentication_service = authentication_service
        self.session_manager = session_manager
        self.db_manager = db_manager or DatabaseManager()

    async def handle_exit_intent(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Handle complete exit logic - clear auth, session, and send goodbye message.

        Args:
            user_phone: User's phone number
            session: Current conversation session

        Returns:
            Dict with status and completion details
        """
        try:
            logger.info(f"Handling exit intent for user: {user_phone}")

            # Step 1: Clear authentication token
            auth_cleared = False
            if self.authentication_service:
                auth_cleared = await self.authentication_service.clear_user_token(user_phone)
                logger.info(f"Authentication token cleared: {auth_cleared}")

            # Step 2: Clear session data
            session_cleared = await self._clear_session_data(session)
            logger.info(f"Session data cleared: {session_cleared}")

            # Step 3: Send goodbye message
            goodbye_sent = await self._send_goodbye_message(user_phone)
            logger.info(f"Goodbye message sent: {goodbye_sent}")

            return {
                "status": "exit_completed",
                "auth_cleared": auth_cleared,
                "session_cleared": session_cleared,
                "goodbye_sent": goodbye_sent,
                "message": "System exit completed successfully"
            }

        except Exception as e:
            logger.error(f"Error handling exit intent for {user_phone}: {e}")
            return {
                "status": "exit_error",
                "error": str(e),
                "message": "Error during system exit"
            }

    async def _clear_session_data(self, session: ConversationSession) -> bool:
        """
        Save complete session data to database (preserving conversation history), then clear Redis.

        Args:
            session: Current conversation session

        Returns:
            True if session cleared successfully
        """
        try:
            if not session:
                return True

            from app.models import ConversationOutcome

            # Mark session as completed and abandoned (but keep all data intact)
            session.outcome = ConversationOutcome.abandoned
            session.completed_at = utc_now().replace(tzinfo=None)

            # Add exit metadata to workflow_state without clearing other data
            if not session.workflow_state:
                session.workflow_state = {}
            session.workflow_state["exit_completed"] = True
            session.workflow_state["exit_timestamp"] = utc_now().isoformat()

            # IMPORTANT: Keep conversation_history intact for audit trail
            # All messages with timestamps are preserved in the database

            # STEP 1: APPEND complete session to database (preserves all history)
            self.db_manager.append_session_data({
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'workflow_type': WorkflowType.user_exit.value,
                'outcome': ConversationOutcome.abandoned.value,
                'workflow_state': session.workflow_state,
                'conversation_history': session.conversation_history,  # Appended to existing
                'extracted_entities': session.extracted_entities,
                'retention_date': session.retention_date,
                'completed_at': session.completed_at
            })

            logger.info(f"Session {session.session_id} saved to database with complete conversation history preserved (outcome=abandoned)")

            # STEP 2: Clear Redis (user is exiting, session is complete)
            from app.redis_db import get_session_redis_service
            from app.config import get_settings
            settings = get_settings()
            if settings.redis_session_storage_enabled:
                redis_session = get_session_redis_service()
                await redis_session.delete_session(session.session_id)
                logger.info(f"Session {session.session_id} deleted from Redis after exit")

            return True

        except Exception as e:
            logger.error(f"Error clearing session data: {e}")
            return False

    async def _send_goodbye_message(self, user_phone: str) -> bool:
        """
        Send goodbye/thank you message to user.

        Args:
            user_phone: User's phone number

        Returns:
            True if message sent successfully
        """
        try:
            goodbye_message = (
               "Thank you for using QUA! I’ll be here whenever you need procurement support."
            )

            await self.whatsapp_service.send_message(user_phone, goodbye_message)
            logger.info(f"Goodbye message sent to {user_phone}")
            return True

        except Exception as e:
            logger.error(f"Error sending goodbye message to {user_phone}: {e}")
            return False