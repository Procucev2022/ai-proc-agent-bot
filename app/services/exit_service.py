"""
Exit Service for handling system exit logic.

Provides complete session and authentication cleanup when users want to exit the system.
Handles token clearing, session cleanup, and sends appropriate goodbye messages.
"""

import logging
from typing import Dict, Any, Optional
from app.models import ConversationSession
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
        Clear all session data and mark session as completed.

        Args:
            session: Current conversation session

        Returns:
            True if session cleared successfully
        """
        try:
            if not session:
                return True

            # Clear all session state
            session.workflow_type = None
            session.outcome = "abandoned"
            session.workflow_state = {
                "exit_completed": True,
                "exit_timestamp": session.workflow_state.get("last_activity_at") if session.workflow_state else None
            }
            session.conversation_history = {"messages": [], "metadata": []}
            session.extracted_entities = {}

            # Mark session as completed
            session.completed_at = utc_now().replace(tzinfo=None)

            # Save the cleared session
            if self.session_manager:
                await self.session_manager.save_session(session, "user_exit")
            else:
                # Fallback to direct database save
                self.db_manager.save_conversation_session({
                    'session_id': session.session_id,
                    'external_user_id': session.external_user_id,
                    'workflow_type': "user_exit",
                    'outcome': "abandoned",
                    'workflow_state': session.workflow_state,
                    'conversation_history': session.conversation_history,
                    'extracted_entities': session.extracted_entities,
                    'retention_date': session.retention_date,
                    'completed_at': session.completed_at
                })

            logger.info(f"Session {session.session_id} cleared and marked as completed")
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
                "Thank you for using QUA!"
            )

            await self.whatsapp_service.send_message(user_phone, goodbye_message)
            logger.info(f"Goodbye message sent to {user_phone}")
            return True

        except Exception as e:
            logger.error(f"Error sending goodbye message to {user_phone}: {e}")
            return False