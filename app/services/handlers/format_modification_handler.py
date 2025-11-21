"""
Format Modification Handler (Track 2 - Module 2.5).

Handles format modification flow for RFQ creation:
- Routes delivery vs items modification
- Calls Track 1 functions for parsing/validation
- Updates state (retry counter, flags)
- Sends error messages or success responses
"""

import logging
from typing import Dict, Any, Optional
from app.models import ConversationSession, User
from app.services.workflow_manager import WorkflowManager
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)


class FormatModificationHandler:
    """Handles format modification orchestration for Track 2."""

    # Max retry attempts for format corrections
    MAX_RETRY_ATTEMPTS = 3

    def __init__(self, whatsapp_service: WhatsAppService):
        """
        Initialize format modification handler.

        Args:
            whatsapp_service: WhatsApp service for sending messages
        """
        self.whatsapp_service = whatsapp_service

    async def handle_format_modification(
        self,
        message: str,
        session: ConversationSession,
        user: User
    ) -> Dict[str, Any]:
        """
        Main orchestrator for format modification.
        Routes to delivery or items handler based on subtype.

        Args:
            message: User's edited format text
            session: Conversation session
            user: User object

        Returns:
            Dict with status and message
        """
        try:
            # Get modification subtype from state
            is_awaiting, subtype = WorkflowManager.is_awaiting_modification(session)

            if not is_awaiting:
                logger.warning(f"handle_format_modification called but not awaiting modification")
                return {
                    "status": "error",
                    "message": "Not currently awaiting format modification"
                }

            logger.info(f"[FORMAT_MOD] Session {session.session_id}: "
                       f"Handling {subtype} modification")

            # Route to appropriate handler
            if subtype == "delivery":
                return await self._handle_delivery_modification(message, session, user)
            elif subtype == "items":
                return await self._handle_items_modification(message, session, user)
            else:
                logger.error(f"[FORMAT_MOD_ERROR] Unknown subtype: {subtype}")
                return {
                    "status": "error",
                    "message": f"Unknown modification type: {subtype}"
                }

        except Exception as e:
            logger.error(f"[FORMAT_MOD_ERROR] Error in format modification: {e}", exc_info=True)
            return {
                "status": "error",
                "message": "An error occurred while processing your modification. Please try again."
            }

    async def _handle_delivery_modification(
        self,
        message: str,
        session: ConversationSession,
        user: User
    ) -> Dict[str, Any]:
        """
        Handle delivery format modification.

        Flow:
        1. Get retry count
        2. Call Track 1 to parse/validate (TODO: implement when Track 1 exists)
        3. If error: increment retry, check max, send error
        4. If success: update delivery_details, clear flags, send confirmation

        Args:
            message: User's edited delivery format
            session: Conversation session
            user: User object

        Returns:
            Dict with status and message
        """
        logger.info(f"[DELIVERY_MOD] Session {session.session_id}: "
                   f"Processing delivery modification")

        # Get current retry count
        retry_count = WorkflowManager.get_retry_count(session)

        # TODO: Call Track 1 function when it exists
        # result = parse_delivery_format(message, retry_count)
        #
        # For now, placeholder response
        result = {
            "success": False,
            "error_message": "Track 1 parse_delivery_format() not yet implemented"
        }

        if not result["success"]:
            # Track 1 generated error message
            return await self._handle_format_error(
                session,
                user,
                result["error_message"],
                "delivery"
            )

        # Success - Update state with parsed delivery data
        delivery_data = result["data"]
        WorkflowManager.set_delivery_details(session, delivery_data, caller="format_mod_handler")
        WorkflowManager.confirm_delivery(session, caller="format_mod_handler")
        WorkflowManager.clear_awaiting_modification(session, caller="format_mod_handler")
        WorkflowManager.reset_retry_count(session, caller="format_mod_handler")

        logger.info(f"[DELIVERY_MOD_SUCCESS] Session {session.session_id}: "
                   f"Delivery details updated successfully")

        # TODO: Format delivery for display using Track 1
        # display_message = format_delivery_for_display(delivery_data)

        # Send confirmation
        display_message = "Delivery details updated successfully!\n\n" \
                         f"Delivery Date: {delivery_data.get('delivery_date', 'N/A')}\n" \
                         f"Pincode: {delivery_data.get('pincode', 'N/A')}\n" \
                         f"City: {delivery_data.get('city', 'N/A')}\n" \
                         f"State: {delivery_data.get('state', 'N/A')}"

        await self.whatsapp_service.send_message(user.phone_number, display_message)

        return {
            "status": "delivery_updated",
            "message": "Delivery details updated"
        }

    async def _handle_items_modification(
        self,
        message: str,
        session: ConversationSession,
        user: User
    ) -> Dict[str, Any]:
        """
        Handle items format modification.

        Flow:
        1. Get retry count
        2. Call Track 1 to parse/validate (TODO: implement when Track 1 exists)
        3. If error: increment retry, check max, send error
        4. If success: replace products array, clear flags, send confirmation

        Args:
            message: User's edited items format
            session: Conversation session
            user: User object

        Returns:
            Dict with status and message
        """
        logger.info(f"[ITEMS_MOD] Session {session.session_id}: "
                   f"Processing items modification")

        # Get current retry count
        retry_count = WorkflowManager.get_retry_count(session)

        # TODO: Call Track 1 function when it exists
        # result = parse_items_format(message, retry_count)
        #
        # For now, placeholder response
        result = {
            "success": False,
            "error_message": "Track 1 parse_items_format() not yet implemented"
        }

        if not result["success"]:
            # Track 1 generated error message
            return await self._handle_format_error(
                session,
                user,
                result["error_message"],
                "items"
            )

        # Success - Replace products array
        parsed_items = result["data"]
        await self._replace_products_array(session, parsed_items)

        WorkflowManager.clear_awaiting_modification(session, caller="format_mod_handler")
        WorkflowManager.reset_retry_count(session, caller="format_mod_handler")

        logger.info(f"[ITEMS_MOD_SUCCESS] Session {session.session_id}: "
                   f"Items updated successfully - {len(parsed_items)} items")

        # TODO: Format items for display using Track 1
        # display_message = format_items_for_display(parsed_items)

        # Send confirmation
        items_list = "\n".join([
            f"Item {i+1}: {item.get('description', 'N/A')} - Qty: {item.get('quantity', 'N/A')}"
            for i, item in enumerate(parsed_items)
        ])
        display_message = f"Items updated successfully!\n\n" \
                         f"I've captured {len(parsed_items)} products:\n\n{items_list}"

        await self.whatsapp_service.send_configurable_buttons(
            recipient_id=user.phone_number,
            body=display_message,
            buttons_config=[
                {"id": "confirm_items", "title": "Confirm"},
                {"id": "modify_items", "title": "Modify"}
            ]
        )

        return {
            "status": "items_updated",
            "message": "Items updated",
            "total_items": len(parsed_items)
        }

    async def _handle_format_error(
        self,
        session: ConversationSession,
        user: User,
        error_message: str,
        subtype: str
    ) -> Dict[str, Any]:
        """
        Handle format validation error.

        Flow:
        1. Increment retry count
        2. Check if max retries reached
        3. Send error message (from Track 1) with retry count
        4. If max reached, cancel workflow

        Args:
            session: Conversation session
            user: User object
            error_message: Error message from Track 1
            subtype: "delivery" or "items"

        Returns:
            Dict with status
        """
        # Increment retry count
        new_retry_count = WorkflowManager.increment_retry_count(
            session,
            caller="format_mod_handler"
        )

        logger.warning(f"[FORMAT_ERROR] Session {session.session_id}: "
                      f"{subtype} format error - retry {new_retry_count}/{self.MAX_RETRY_ATTEMPTS}")

        # Check if max retries reached
        if new_retry_count >= self.MAX_RETRY_ATTEMPTS:
            # Clear modification state
            WorkflowManager.clear_awaiting_modification(session, caller="format_mod_handler")

            max_retry_message = (
                f"Maximum retry attempts ({self.MAX_RETRY_ATTEMPTS}) reached.\n\n"
                f"Your {subtype} modification has been cancelled. "
                f"Please start a new RFQ if you'd like to try again.\n\n"
                f"Type 'Create new RFQ' to start over."
            )

            await self.whatsapp_service.send_message(user.phone_number, max_retry_message)

            return {
                "status": "max_retries_reached",
                "message": "Max retries exceeded",
                "retry_count": new_retry_count
            }

        # Send error message with retry count info
        retry_info = f"\n\nAttempt {new_retry_count} of {self.MAX_RETRY_ATTEMPTS}"
        full_error_message = error_message + retry_info

        await self.whatsapp_service.send_message(user.phone_number, full_error_message)

        # Save session to persist retry count
        await self.session_manager.save_session(session, persist_to_db=False)

        return {
            "status": "format_error",
            "message": "Format validation failed",
            "retry_count": new_retry_count
        }

    async def _replace_products_array(
        self,
        session: ConversationSession,
        new_items: list
    ) -> None:
        """
        Replace products array in session.

        This is the Track 2 implementation of products array replacement:
        1. Clear old extracted_entities
        2. Merge delivery_details into each item
        3. Store new items as extracted_entities

        Args:
            session: Conversation session
            new_items: List of parsed item dicts from Track 1
        """
        logger.info(f"[REPLACE_ARRAY] Session {session.session_id}: "
                   f"Replacing products array with {len(new_items)} items")

        # Get delivery details to merge
        delivery_details = WorkflowManager.get_delivery_details(session)

        # Merge delivery details into each item
        merged_items = []
        for item in new_items:
            merged_item = item.copy()
            if delivery_details:
                # Add delivery fields if not already present
                if 'deliveryDate' not in merged_item:
                    merged_item['deliveryDate'] = delivery_details.get('delivery_date')
                if 'pincode' not in merged_item:
                    merged_item['pincode'] = delivery_details.get('pincode')
                if 'city' not in merged_item:
                    merged_item['city'] = delivery_details.get('city')
                if 'state' not in merged_item:
                    merged_item['state'] = delivery_details.get('state')

            merged_items.append(merged_item)

        # Clear old data and store new items
        session.workflow_state['extracted_entities'] = merged_items

        # Clear incomplete/complete products tracking
        if 'incomplete_products' in session.workflow_state:
            del session.workflow_state['incomplete_products']
        if 'complete_products' in session.workflow_state:
            del session.workflow_state['complete_products']

        logger.info(f"[REPLACE_ARRAY_SUCCESS] Session {session.session_id}: "
                   f"Products array replaced successfully")
