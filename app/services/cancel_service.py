"""
Cancel Service for handling workflow cancellation logic.

Provides workflow state reset while keeping workflow type intact.
Handles confirmation prompt and state clearing after user confirms.
"""

import logging
from typing import Dict, Any, Optional
from app.models import WorkflowType, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.session_management_service import SessionManagementService
from app.services.confirmation_service import ConfirmationService
from app.tools.confirmation_tool import ConfirmationTool
from app.services.openai_service import OpenAIService
from app.database import DatabaseManager


logger = logging.getLogger(__name__)


class CancelService:
    """Handles workflow cancellation with confirmation flow."""

    def __init__(self, whatsapp_service: WhatsAppService = None,
                 session_manager: SessionManagementService = None,
                 db_manager: DatabaseManager = None,
                 confirmation_service: ConfirmationService = None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.session_manager = session_manager
        self.db_manager = db_manager or DatabaseManager()

        # Initialize confirmation service for yes/no detection
        if confirmation_service:
            self.confirmation_service = confirmation_service
        else:
            openai_service = OpenAIService()
            confirmation_tool = ConfirmationTool(openai_service)
            self.confirmation_service = ConfirmationService(confirmation_tool)

    async def handle_cancel_intent(self, user_phone: str, session: ConversationSession, message: str = None) -> Dict[str, Any]:
        """
        Handle cancel intent - ask for confirmation before clearing workflow state.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            message: User's message (optional, for detecting yes/no confirmation)

        Returns:
            Dict with status and details
        """
        try:
            logger.info(f"Handling cancel intent for user: {user_phone}")

            # Check if user is in a workflow
            if not session.workflow_type or session.workflow_type == WorkflowType.user_exit:
                await self.whatsapp_service.send_message(
                    user_phone,
                    "There is no active workflow to cancel. What can I assist you with?"
                )
                return {
                    "status": "no_workflow",
                    "message": "No active workflow to cancel"
                }

            # Check if cancel is already pending - if so, this is a confirmation response
            if session.workflow_state and session.workflow_state.get("cancel_pending"):
                logger.info(f"Cancel already pending for {user_phone}, treating message '{message}' as confirmation response")
                # Simple yes/no detection - if message contains "yes" or similar, confirm; otherwise decline
                message_lower = (message or "").lower()
                is_confirmed = any(word in message_lower for word in ["yes", "confirm", "cancel", "sure", "ok"])
                logger.info(f"Detected confirmation: {is_confirmed} from message: '{message}'")
                return await self.handle_cancel_confirmation(user_phone, session, is_confirmed)

            # Set cancel pending state
            if not session.workflow_state:
                session.workflow_state = {}

            session.workflow_state["cancel_pending"] = True

            # Save session with pending state
            if self.session_manager:
                await self.session_manager.save_session(session, session.workflow_type)

            # Send confirmation message
            confirmation_sent = await self._send_confirmation_message(user_phone)
            logger.info(f"Confirmation message sent: {confirmation_sent}")

            return {
                "status": "confirmation_pending",
                "confirmation_sent": confirmation_sent,
                "message": "Awaiting user confirmation for cancellation"
            }

        except Exception as e:
            logger.error(f"Error handling cancel intent for {user_phone}: {e}")
            return {
                "status": "cancel_error",
                "error": str(e),
                "message": "Error during cancel intent handling"
            }

    async def handle_cancel_confirmation(self, user_phone: str, session: ConversationSession,
                                        confirmed: bool) -> Dict[str, Any]:
        """
        Handle user's response to cancel confirmation.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            confirmed: Whether user confirmed cancellation

        Returns:
            Dict with status and completion details
        """
        try:
            logger.info(f"Handling cancel confirmation for user: {user_phone}, confirmed: {confirmed}")

            # Clear the cancel_pending flag
            if session.workflow_state and "cancel_pending" in session.workflow_state:
                del session.workflow_state["cancel_pending"]

            if confirmed:
                # Perform cancellation
                cancellation_result = await self._clear_workflow_state(session)
                logger.info(f"Workflow state cleared: {cancellation_result}")

                # Clear meaningful message cache (user is starting fresh)
                from app.services.user_cache_service import get_user_cache_service
                user_cache_service = get_user_cache_service()
                meaningful_cleared = await user_cache_service.clear_meaningful_message(user_phone)
                logger.info(f"Meaningful message cleared: {meaningful_cleared}")

                # Send cancellation success message
                message_sent = await self._send_cancellation_message(user_phone)
                logger.info(f"Cancellation message sent: {message_sent}")

                return {
                    "status": "cancelled",
                    "state_cleared": cancellation_result,
                    "message_sent": message_sent,
                    "message": "Workflow cancelled successfully"
                }
            else:
                # User declined - resume workflow
                # Send a brief acknowledgment
                logger.info(f"User declined cancellation - resuming workflow")

                await self.whatsapp_service.send_message(
                    user_phone,
                    "Continuing with your request..."
                )

                # Save session
                if self.session_manager:
                    await self.session_manager.save_session(session, session.workflow_type)

                return {
                    "status": "cancelled_aborted",
                    "resume_workflow": True,
                    "message": "User declined cancellation - resuming normal flow"
                }

        except Exception as e:
            logger.error(f"Error handling cancel confirmation for {user_phone}: {e}")
            return {
                "status": "confirmation_error",
                "error": str(e),
                "message": "Error during cancel confirmation handling"
            }

    async def _clear_workflow_state(self, session: ConversationSession) -> bool:
        """
        Reset session completely for fresh start (user is continuing, not exiting).

        Args:
            session: Current conversation session

        Returns:
            True if state cleared successfully
        """
        try:
            if not session:
                return True

            from app.utils.datetime_utils import utc_now

            # Reset all session data for fresh start
            session.workflow_state = {"last_activity_at": utc_now().isoformat()}
            session.extracted_entities = {}
            session.conversation_history = {"messages": [], "metadata": [], "openai_messages": []}
            session.workflow_type = None
            session.outcome = None
            session.completed_at = None

            # Save reset session to Redis (and optionally DB)
            if self.session_manager:
                await self.session_manager.save_session(session, persist_to_db=False)
            else:
                # Fallback to direct database save
                self.db_manager.save_conversation_session({
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

            logger.info(f"Session {session.session_id} reset for fresh start (kept in Redis)")
            return True

        except Exception as e:
            logger.error(f"Error clearing workflow state: {e}")
            return False

    async def _send_confirmation_message(self, user_phone: str) -> bool:
        """
        Send confirmation prompt to user with Yes/No buttons.

        Args:
            user_phone: User's phone number

        Returns:
            True if message sent successfully
        """
        try:
            confirmation_message = (
                "Are you sure you want to cancel? This will clear all the information you've provided so far."
            )

            buttons = [
                {"id": "confirm_cancel", "title": "Yes, Cancel"},
                {"id": "decline_cancel", "title": "No, Continue"}
            ]

            result = await self.whatsapp_service.send_configurable_buttons(
                recipient_id=user_phone,
                body=confirmation_message,
                buttons_config=buttons
            )

            logger.info(f"Confirmation buttons sent to {user_phone}: {result.success}")
            return result.success

        except Exception as e:
            logger.error(f"Error sending confirmation message to {user_phone}: {e}")
            return False

    async def _send_cancellation_message(self, user_phone: str) -> bool:
        """
        Send cancellation success message to user.

        Args:
            user_phone: User's phone number

        Returns:
            True if message sent successfully
        """
        try:
            cancellation_message = (
                "Your request has been cancelled. What can I assist you with next?"
            )

            await self.whatsapp_service.send_message(user_phone, cancellation_message)
            logger.info(f"Cancellation message sent to {user_phone}")
            return True

        except Exception as e:
            logger.error(f"Error sending cancellation message to {user_phone}: {e}")
            return False
