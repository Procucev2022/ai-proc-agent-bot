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
from app.utils.message_restore_utils import restore_last_bot_message
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

    async def handle_exit_intent(self, user_phone: str, session: ConversationSession, show_message: bool = True,message: str = None,user: Optional[Any] = None) -> Dict[str, Any]:
        """
        Handle complete exit logic - ask for confirmation before clearing auth and session.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            show_message: Whether to send goodbye message (default: True)

        Returns:
            Dict with status and completion details
        """
        try:
            logger.info(f"Handling exit intent for user: {user_phone}")

            # Check if user is in a workflow
            if not session.workflow_type or session.workflow_type == WorkflowType.user_exit:
                message = (
                    "There is no active workflow to exit. What can I assist you with?"
                )
                await self.whatsapp_service.send_message(
                    user_phone,message)
                return {
                    "status": "no_workflow_to_exit",
                    "message": "No active workflow to exit"
                }

            # If show_message is False, directly proceed with exit without confirmation
            if not show_message:
                logger.info(f"Direct exit requested for {user_phone} (show_message=False)")
                return await self.handle_exit_confirmation(user_phone, session, True, user, show_message)

            # Check if exit is already pending - if so, this is a confirmation response
            if session.workflow_state and session.workflow_state.get("exit_pending"):
                logger.info(f"Exit already pending for {user_phone}, treating message '{message}' as confirmation response")

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
                if button_id in ["confirm_exit", "confirm_yes"]:
                    is_confirmed = True
                else:
                    # Normal text keywords
                    is_confirmed = any(
                        word in user_text for word in ["yes", "confirm", "cancel", "sure", "ok"])

                logger.info(f"Detected exit confirmation: {is_confirmed} from message: '{message}'")
                return await self.handle_exit_confirmation(user_phone, session, is_confirmed, user, show_message)

            # Set exit pending state and save last bot message
            if not session.workflow_state:
                session.workflow_state = {}

            # Save the last bot message before exit confirmation
            last_bot_message = self._get_last_bot_message(session)
            if last_bot_message:
                session.workflow_state["last_bot_message_before_exit"] = last_bot_message

            session.workflow_state["exit_pending"] = True

            # Save session with pending state
            if self.session_manager:
                await self.session_manager.save_session(session, session.workflow_type)

            # Send confirmation message with buttons
            confirmation_sent = await self._send_exit_confirmation_message(user_phone)
            logger.info(f"Exit confirmation message sent: {confirmation_sent}")

            return {
                "status": "exit_confirmation_pending",
                "confirmation_sent": confirmation_sent,
                "message": "Awaiting user confirmation for exit"
            }

        except Exception as e:
            logger.error(f"Error handling exit intent for {user_phone}: {e}")
            return {
                "status": "exit_error",
                "error": str(e),
                "message": "Error during exit intent handling"
            }

    async def _clear_session_data(self, session: ConversationSession) -> bool:
        """
        Save complete session data to database (preserving conversation history), then completely clear Redis.

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

            # STEP 2: Clear ALL Redis data (user is exiting, everything should be wiped)
            from app.redis_db import get_session_redis_service, get_redis_service
            from app.config import get_settings
            settings = get_settings()
            if settings.redis_session_storage_enabled:
                redis_session = get_session_redis_service()
                redis_base = get_redis_service()

                # Extract normalized phone for user-related key patterns
                normalized_phone = session.external_user_id.lstrip('+').replace(' ', '').replace('-', '')

                # Comprehensive Redis cleanup - delete all keys related to this user/session
                # IMPORTANT: Do NOT delete welcome_msg token - user should not see welcome message again today if they exit and return
                cleanup_patterns = [
                    f"session:{session.session_id}",  # Session key
                    f"auth:{normalized_phone}",  # Auth token
                    f"user_cache:{normalized_phone}",  # User cache
                    f"incoming_messages:{normalized_phone}*",  # Message queue incoming
                    f"outgoing_messages:{normalized_phone}*",  # Message queue outgoing
                    f"processing:*:{normalized_phone}",  # Processing locks
                    f"*:{session.session_id}*",  # Any session-related keys
                ]

                total_deleted = 0
                # First delete explicit keys that we know about
                explicit_keys_to_delete = [
                    session.session_id,
                    normalized_phone,
                ]

                for key_part in explicit_keys_to_delete:
                    # Delete session:{key}
                    deleted = await redis_session.delete_session(key_part)
                    if deleted:
                        total_deleted += 1
                        logger.info(f"Deleted session key for {key_part}")

                # Then do pattern-based cleanup for any remaining keys (excluding welcome message)
                for pattern in cleanup_patterns:
                    deleted_count = await redis_base.delete_pattern(pattern)
                    total_deleted += deleted_count

                # Explicitly delete user-related patterns but exclude welcome_msg key
                # Use scan to find and delete keys with phone number but skip welcome_msg
                try:
                    await redis_base.init_client()
                    cursor = 0
                    while True:
                        cursor, keys = await redis_base.client.scan(cursor, match=f"*:{normalized_phone}*", count=100)
                        if keys:
                            # Filter out welcome_msg keys - these should be preserved
                            keys_to_delete = [k for k in keys if not k.startswith("welcome_msg:")]
                            if keys_to_delete:
                                deleted = await redis_base.client.delete(*keys_to_delete)
                                total_deleted += deleted
                        if cursor == 0:
                            break
                except Exception as e:
                    logger.warning(f"Error during pattern cleanup for user-related keys: {e}")

                # Final verification - check if session still exists
                still_exists = await redis_session.session_exists(session.session_id)
                if still_exists:
                    logger.error(f"⚠️ WARNING: Session {session.session_id} still exists in Redis after comprehensive cleanup!")
                    # Try one more aggressive delete
                    await redis_base.delete(f"session:{session.session_id}")
                    still_exists = await redis_session.session_exists(session.session_id)
                    if still_exists:
                        logger.error(f"⚠️ CRITICAL BUG: Session {session.session_id} could NOT be deleted from Redis!")
                        return False

                logger.info(f"✓ Comprehensive Redis cleanup complete: {total_deleted} keys deleted, session verified removed")

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
    async def handle_exit_confirmation(self, user_phone: str, session: ConversationSession, confirmed: bool, user: Optional[Any] = None, show_message: bool = True) -> Dict[str, Any]:
        """
        Handle user's response to exit confirmation.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            confirmed: Whether user confirmed exit

        Returns:
            Dict with status and completion details
        """
        try:
            logger.info(f"Handling exit confirmation for user: {user_phone}, confirmed: {confirmed}")

            # Clear the exit_pending flag
            if session.workflow_state and "exit_pending" in session.workflow_state:
                del session.workflow_state["exit_pending"]

            if confirmed :
                # Perform complete exit
                # Step 1: Clear authentication token and ALL cached data (including meaningful messages)
                auth_cleared = False
                if self.authentication_service:
                    # preserve_meaningful_message=False ensures complete cleanup on exit
                    auth_cleared = await self.authentication_service.clear_user_token(user_phone, preserve_meaningful_message=False)
                    logger.info(f"Authentication token cleared: {auth_cleared}")

                # Step 2: Clear session data
                session_cleared = await self._clear_session_data(session)
                logger.info(f"Session data cleared: {session_cleared}")

                # Step 3: Send goodbye message only if show_message is True
                goodbye_sent = False
                if show_message:
                    goodbye_sent = await self._send_goodbye_message(user_phone)
                    logger.info(f"Goodbye message sent: {goodbye_sent}")


                return {
                    "status": "exit_completed",
                    "auth_cleared": auth_cleared,
                    "session_cleared": session_cleared,
                    "goodbye_sent": goodbye_sent,
                    "message": "System exit completed successfully"
                }
            else:
                # User declined - resume workflow
                logger.info(f"User declined exit - resuming workflow")

                # Restore the last bot message if available
                await restore_last_bot_message(
                    session,
                    self.whatsapp_service,
                    user_phone,
                    "The exit request has been declined. You may continue with your request.",
                    "last_bot_message_before_exit"
                )


                # Save session
                if self.session_manager:
                    await self.session_manager.save_session(session, session.workflow_type)

                return {
                    "status": "exit_aborted",
                    "resume_workflow": True,
                    "message": "User declined exit - resuming normal flow"
                }

        except Exception as e:
            logger.error(f"Error handling exit confirmation for {user_phone}: {e}")
            return {
                "status": "exit_confirmation_error",
                "error": str(e),
                "message": "Error during exit confirmation handling"
            }

    async def _send_exit_confirmation_message(self, user_phone: str) -> bool:
        """
        Send exit confirmation prompt to user with Yes/No buttons.

        Args:
            user_phone: User's phone number

        Returns:
            True if message sent successfully
        """
        try:
            confirmation_message = (
                "Are you sure you want to exit? This will end your session and clear all data."
            )

            buttons = [
                {"id": "confirm_exit", "title": "Yes, Exit"},
                {"id": "decline_exit", "title": "No, Continue"}
            ]

            result = await self.whatsapp_service.send_configurable_buttons(
                recipient_id=user_phone,
                body=confirmation_message,
                buttons_config=buttons
            )

            logger.info(f"Exit confirmation buttons sent to {user_phone}: {result.success}")
            return result.success

        except Exception as e:
            logger.error(f"Error sending exit confirmation message to {user_phone}: {e}")
            return False

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