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
from app.utils.message_restore_utils import restore_last_bot_message

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

    async def handle_cancel_intent(self, user_phone: str, session: ConversationSession, message: str = None, user: Optional[Any] = None) -> Dict[str, Any]:
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
            logger.debug(f"Handling cancel intent for user: {user_phone}")

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
                logger.debug(f"Cancel already pending for {user_phone}, treating message as confirmation response")

                # Extract user response
                user_text = ""
                button_id = None

                if isinstance(message, dict) and message.get("type") == "button_reply":
                    button_id = message["button_reply"].get("id", "")
                    user_text = message["button_reply"].get("title", "").lower()
                else:
                    user_text = (message or "").lower()

                # Detect confirmation
                is_confirmed = False

                # If button press such as confirm_cancel → treat as YES
                if button_id in ["confirm_cancel", "confirm_yes"]:
                    is_confirmed = True
                else:
                    # Normal text keywords
                    is_confirmed = any(
                        word in user_text for word in ["yes", "confirm", "cancel", "sure", "ok","restart"])

                logger.debug(f"Detected cancel confirmation: {is_confirmed}")
                return await self.handle_cancel_confirmation(user_phone, session, is_confirmed,user)

            # Set cancel pending state and save last bot message
            if not session.workflow_state:
                session.workflow_state = {}

            # Save the last bot message before cancel confirmation
            last_bot_message = self._get_last_bot_message(session)
            if last_bot_message:
                session.workflow_state["last_bot_message_before_cancel"] = last_bot_message

            session.workflow_state["cancel_pending"] = True

            # Save session with pending state
            if self.session_manager:
                await self.session_manager.save_session(session, session.workflow_type)

            # Send confirmation message
            confirmation_sent = await self._send_confirmation_message(user_phone,session=session)
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
                                        confirmed: bool, user: Optional[Any] = None) -> Dict[str, Any]:
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
            logger.debug(f"Handling cancel confirmation for user: {user_phone}, confirmed: {confirmed}")

            # Clear the cancel_pending flag
            if session.workflow_state and "cancel_pending" in session.workflow_state:
                del session.workflow_state["cancel_pending"]

            if confirmed:
                # Get user type from cache BEFORE clearing
                from app.services.user_cache_service import get_user_cache_service
                user_cache_service = get_user_cache_service()
                user_data_list = await user_cache_service.get_user_data(user_phone)
                user_type = None
                if user:
                    # selfClient: True = buyer, False = seller
                    is_self_client = user.role
                    logger.info(f"user role is:{is_self_client}")
                    user_type = "buyer" if is_self_client else "seller"
                logger.info(f"Retrieved user_type from cache (selfClient): {user_type}")

                # Perform cancellation
                cancellation_result = await self._clear_workflow_state(session)
                logger.info(f"Workflow state cleared: {cancellation_result}")

                # Clear meaningful message cache (user is starting fresh)
                meaningful_cleared = await user_cache_service.clear_meaningful_message(user_phone)
                logger.info(f"Meaningful message cleared: {meaningful_cleared}")

                # Send cancellation success message with buyer buttons
                message_sent = await self._send_cancellation_message(user_phone, user_type)
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
                await restore_last_bot_message(
                    session,
                    self.whatsapp_service,
                    user_phone,
                    "The cancellation request has been declined. You may continue with your request.",
                    "last_bot_message_before_cancel"
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
            from app.redis_db import get_redis_service

            # Clear pending_reply flag (user confirmed cancel, no reply owed)
            try:
                redis_service = get_redis_service()
                normalized_phone = session.external_user_id.lstrip('+') if session.external_user_id else None
                if normalized_phone:
                    await redis_service.delete(f"{normalized_phone}:pending_reply")
                    logger.debug(f"Cleared pending_reply flag for {normalized_phone} (cancel confirmed)")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clear pending_reply flag: {cleanup_error}")

            # Reset all session data for fresh start
            session.workflow_state = {"last_activity_at": utc_now().isoformat()}
            session.extracted_entities = {}
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

    async def _send_confirmation_message(self, user_phone: str,session=None) -> bool:
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
                buttons_config=buttons,
                session_id=session
            )

            logger.info(f"Confirmation buttons sent to {user_phone}: {result.success}")
            return result.success

        except Exception as e:
            logger.error(f"Error sending confirmation message to {user_phone}: {e}")
            return False

    async def _send_cancellation_message(self, user_phone: str, user_type=None, custom_message: str = None) -> bool:
        """
        Send cancellation success message to user with buyer/seller buttons.

        Args:
            user_phone: User's phone number
            user_type: User type (string from cache, optional)
            custom_message: Custom message to send instead of default (optional)

        Returns:
            True if message sent successfully
        """
        try:
            cancellation_message = custom_message or (
                "Your request has been cancelled. What can I assist you with next?"
            )

            # If user_type is unknown, fetch from auth system
            if not user_type:
                user_type = await self._get_user_type_from_auth(user_phone)

            # Check if user is a buyer and send buttons accordingly
            logger.info(f"User type from parameter: {user_type} (type: {type(user_type)})")

            # Check if user is a buyer (user_type from cache is a string)
            is_buyer = user_type and user_type.lower() == "buyer"
            is_seller = user_type and user_type.lower() == "seller"

            logger.info(f"Is buyer: {is_buyer}, user_type: {user_type}")

            if is_buyer:
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Ready Stocks"}
                ]
                logger.info(f"Sending cancellation with buyer buttons to {user_phone}")
                await self.whatsapp_service.send_configurable_buttons(
                    recipient_id=user_phone,
                    body=cancellation_message,
                    buttons_config=buttons_config
                )
            elif is_seller:
                buttons_config = [
                    {"id": "view_rfqs", "title": "Other Active RFQs"},
                    {"id": "rfq_status", "title": "My quoTe Status"},
                    {"id": "contact_support", "title": "Contact Support"}
                ]
                logger.info(f"Sending cancellation with seller buttons to {user_phone}")
                await self.whatsapp_service.send_configurable_buttons(
                    recipient_id=user_phone,
                    body=cancellation_message,
                    buttons_config=buttons_config
                )
            else:
                # Default - just send message without buttons
                logger.info(f"Sending cancellation without buttons to {user_phone}")
                await self.whatsapp_service.send_message(user_phone, cancellation_message)

            logger.info(f"Cancellation message sent to {user_phone}")
            return True

        except Exception as e:
            logger.error(f"Error sending cancellation message to {user_phone}: {e}")
            return False

    async def _get_user_type_from_auth(self, user_phone: str) -> str:
        """
        Get user type from Redis auth token when not available in cache.

        Args:
            user_phone: User's phone number

        Returns:
            User type string ("buyer" or "seller") or None if not found
        """
        try:
            from app.redis_db import get_auth_redis_service
            
            auth_redis_service = get_auth_redis_service()
            normalized_phone = user_phone.lstrip('+')
            user_data = await auth_redis_service.retrieve(user_phone)
            
            if user_data:
                role = user_data.role.value
                if role:
                    return role
            
            logger.warning(f"Could not find user type in Redis auth token for {user_phone}")
            return None
            
        except Exception as e:
            logger.error(f"Error getting user type from Redis auth for {user_phone}: {e}")
            return None

    def _get_last_bot_message(self, session: ConversationSession) -> str:
        """
        Extract the last bot message from conversation history.

        Args:
            session: Current conversation session

        Returns:
            Last bot message or None if not found
        """
        try:
            conversation_history = session.conversation_history or {}
            messages = conversation_history.get("messages", [])

            # Find the last assistant message
            for message in reversed(messages):
                if message.get("role") == "assistant":
                    return message.get("content", "")

            return None

        except Exception as e:
            logger.error(f"Error getting last bot message: {e}")
            return None
