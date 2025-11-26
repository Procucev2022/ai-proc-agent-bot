"""
Sectioned RFQ Creation Handler.

Handles the new sectioned RFQ creation workflow with 4 distinct sections:
1. Date/Location - Delivery details confirmation
2. Items - Product items confirmation
3. Attachments - Optional attachment collection
4. Final Confirmation - RFQ submission

Key features:
- Section-by-section confirmation flow
- Direct format parsing for modifications (NO entity extraction after initial)
- Per-section retry counter (max 3 attempts)
- Restart flow for changing confirmed sections
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from app.models import User, ConversationSession, WorkflowType
from app.services.workflow_manager import WorkflowManager
from app.services.entity_service import EntityService
from app.services.whatsapp_service import WhatsAppService
from app.services.cancel_service import CancelService
from app.utils import sectioned_rfq_format_parser
from app.utils.datetime_utils import utc_now
from app.utils.pincode_lookup import get_location_from_pincode_async

logger = logging.getLogger(__name__)


def _format_date_for_display(date_str: str) -> str:
    """
    Format date string to 'dd Month' format (e.g., '30 November').

    Args:
        date_str: Date string in various formats (YYYY-MM-DD, dd Mon, etc.)

    Returns:
        Formatted date string as 'dd Month' or original string if parsing fails
    """
    if not date_str:
        return date_str

    # Try parsing common formats
    formats_to_try = [
        "%Y-%m-%d",      # 2025-11-30
        "%d-%m-%Y",      # 30-11-2025
        "%d/%m/%Y",      # 30/11/2025
        "%d %b %Y",      # 30 Nov 2025
        "%d %B %Y",      # 30 November 2025
        "%d %b",         # 30 Nov
        "%d %B",         # 30 November
    ]

    for fmt in formats_to_try:
        try:
            parsed_date = datetime.strptime(date_str.strip(), fmt)
            # If year was not in format, use current year
            if "%Y" not in fmt and "%y" not in fmt:
                parsed_date = parsed_date.replace(year=datetime.now().year)
            # Format as "d Month" (no leading zero for day)
            return f"{parsed_date.day} {parsed_date.strftime('%B')}"
        except ValueError:
            continue

    # If all parsing fails, return original
    return date_str

# Maximum retry attempts per section
MAX_RETRY_ATTEMPTS = 3


class SectionedRFQCreationHandler:
    """Handles sectioned RFQ creation workflow."""

    def __init__(self, entity_service: EntityService, whatsapp_service: WhatsAppService,
                 cancel_service: CancelService, session_manager, confirmation_handler=None,
                 attachment_decision_handler=None):
        self.entity_service = entity_service
        self.whatsapp_service = whatsapp_service
        self.cancel_service = cancel_service
        self.session_manager = session_manager
        self.confirmation_handler = confirmation_handler
        self.attachment_decision_handler = attachment_decision_handler

    async def handle_sectioned_rfq(self, user: User, session: ConversationSession,
                                   message: str, attachments: List = None) -> Dict[str, Any]:
        """
        Main entry point for sectioned RFQ workflow.
        Routes to appropriate section handler based on current section.

        Args:
            user: User object
            session: Conversation session
            message: User's message
            attachments: List of attachments (if any)

        Returns:
            Dict with response data
        """
        logger.info(f"[SECTIONED_RFQ] Handling sectioned RFQ for user {user.phone_number}")

        # Check if pending restart confirmation
        if WorkflowManager.is_sectioned_rfq_pending_restart(session):
            return await self._handle_restart_confirmation_response(user, session, message)

        # Get current section
        current_section = WorkflowManager.get_sectioned_rfq_section(session)
        logger.info(f"[SECTIONED_RFQ] Current section: {current_section}")

        # Common keyword handling for "Confirm" and "Modify" - applies to all sections
        # Check if user is confirming or requesting modification
        message_lower = message.lower().strip()

        # Handle "Confirm" keyword
        if message_lower in ["confirm", "yes", "y", "proceed", "continue", "ok"]:
            # Check if current section has data to confirm
            if current_section == "date_location":
                delivery_data = WorkflowManager.get_section_data(session, "date_location")
                if delivery_data and self._is_delivery_complete(delivery_data):
                    logger.info(f"[SECTIONED_RFQ] User confirmed {current_section}")
                    return await self._handle_section_confirm(user, session, current_section)
            elif current_section == "items":
                items_data = WorkflowManager.get_section_data(session, "items")
                if items_data and len(items_data) > 0:
                    logger.info(f"[SECTIONED_RFQ] User confirmed {current_section}")
                    return await self._handle_section_confirm(user, session, current_section)

        # Handle "Modify" keyword
        elif message_lower in ["modify", "change", "edit", "update"]:
            # Check if current section has data to modify
            if current_section == "date_location":
                delivery_data = WorkflowManager.get_section_data(session, "date_location")
                if delivery_data and self._is_delivery_complete(delivery_data):
                    logger.info(f"[SECTIONED_RFQ] User requested to modify {current_section}")
                    return await self._handle_section_modify(user, session, current_section)
            elif current_section == "items":
                items_data = WorkflowManager.get_section_data(session, "items")
                if items_data and len(items_data) > 0:
                    logger.info(f"[SECTIONED_RFQ] User requested to modify {current_section}")
                    return await self._handle_section_modify(user, session, current_section)

        # Route to appropriate section handler
        if current_section == "date_location":
            return await self._handle_date_location_section(user, session, message, attachments)
        elif current_section == "items":
            return await self._handle_items_section(user, session, message, attachments)
        elif current_section == "attachments":
            return await self._handle_attachments_section(user, session, message, attachments)
        elif current_section == "final_confirmation":
            return await self._handle_final_confirmation(user, session, message)
        else:
            logger.error(f"[SECTIONED_RFQ] Unknown section: {current_section}")
            return {"status": "error", "message": "Unknown section"}

    # ========================================================================
    # DATE/LOCATION SECTION
    # ========================================================================

    async def _handle_date_location_section(self, user: User, session: ConversationSession,
                                            message: str, attachments: List) -> Dict[str, Any]:
        """
        Handle date/location section.

        Flow:
        1. Check if awaiting modification → process format modification
        2. Check if we have delivery data → display confirmation
        3. Otherwise → extract delivery details from message
        """
        logger.info(f"[SECTIONED_RFQ] Handling date/location section")

        # Check if awaiting modification
        if WorkflowManager.is_awaiting_section_modification(session, "date_location"):
            logger.info(f"[SECTIONED_RFQ] Awaiting modification flag is set - processing modification")
            return await self._process_delivery_modification_direct(user, session, message)

        # Check if we have delivery data from initial extraction
        delivery_data = WorkflowManager.get_section_data(session, "date_location")
        logger.info(f"[SECTIONED_RFQ] Current delivery_data: {delivery_data}")
        logger.info(f"[SECTIONED_RFQ] Is complete: {self._is_delivery_complete(delivery_data) if delivery_data else False}")

        # If we already have complete delivery data, check if user sent data in structured format
        # This handles the case where user directly copies and modifies the confirmation format
        if delivery_data and self._is_delivery_complete(delivery_data):
            # Check if message looks like it's attempting the structured format
            # Look for format keywords to detect modification attempts (only date and pincode shown to user)
            if any(keyword in message.lower() for keyword in ["delivery date:", "delivery pincode:"]):
                logger.info(f"[SECTIONED_RFQ] User sent message in structured format - routing to modification handler")
                return await self._process_delivery_modification_direct(user, session, message)
        elif delivery_data:
            # We have delivery data but it's not complete - check if user sent data in format
            if any(keyword in message.lower() for keyword in ["delivery date:", "delivery pincode:"]):
                logger.info(f"[SECTIONED_RFQ] User sent message in structured format - routing to modification handler")
                return await self._process_delivery_modification_direct(user, session, message)

        # Track date validation error to show to user if needed
        date_validation_error = None

        if not delivery_data or not self._has_delivery_basics(delivery_data):
            # Need to extract delivery details - ONE TIME entity extraction
            logger.info(f"[SECTIONED_RFQ] Extracting delivery details from message")

            entity_context = self._build_entity_context(session)
            entity_result = await self.entity_service.extract_entities(
                message, context=entity_context, workflow_type="buy_something"
            )

            # Extract delivery fields
            delivery_data = {
                "deliveryDate": entity_result.get("deliveryDate", ""),
                "pincode": entity_result.get("pincode", ""),
                "city": entity_result.get("city", ""),
                "state": entity_result.get("state", "")
            }

            # Validate and normalize delivery date if provided
            if delivery_data.get("deliveryDate"):
                date_validation = await self._validate_delivery_date(delivery_data["deliveryDate"])
                if date_validation.get("is_valid"):
                    # Use normalized date format
                    delivery_data["deliveryDate"] = date_validation.get("normalized_date", delivery_data["deliveryDate"])
                    logger.info(f"[SECTIONED_RFQ] Delivery date validated and normalized: {delivery_data['deliveryDate']}")
                else:
                    # Invalid date - clear it and store error to show user
                    date_validation_error = date_validation.get("error", "Invalid delivery date")
                    logger.warning(f"[SECTIONED_RFQ] Invalid delivery date during extraction: {date_validation_error}")
                    delivery_data["deliveryDate"] = ""

            # Auto-fill city/state from pincode if pincode is available
            if delivery_data.get("pincode") and (not delivery_data.get("city") or not delivery_data.get("state")):
                logger.info(f"[SECTIONED_RFQ] Auto-filling city/state from pincode: {delivery_data['pincode']}")
                delivery_data = await self._autofill_location_from_pincode(delivery_data)

            # Store extracted delivery data
            WorkflowManager.update_section_data(session, "date_location", delivery_data)

            # Also check if items were provided in initial message
            if entity_result.get("products"):
                logger.info(f"[SECTIONED_RFQ] User provided items in initial message, storing for later")
                WorkflowManager.update_section_data(session, "items", entity_result["products"])

            # Save session after extraction to persist the delivery data
            await self.session_manager.save_session(session, persist_to_db=False)

        # Reload delivery_data from session to ensure we have the latest stored data
        delivery_data = WorkflowManager.get_section_data(session, "date_location")

        # Auto-fill city/state if we have pincode but missing location
        if delivery_data and delivery_data.get("pincode") and (not delivery_data.get("city") or not delivery_data.get("state")):
            logger.info(f"[SECTIONED_RFQ] Auto-filling missing city/state from pincode")
            delivery_data = await self._autofill_location_from_pincode(delivery_data)
            WorkflowManager.update_section_data(session, "date_location", delivery_data)
            # Save session after auto-fill to persist the location data
            await self.session_manager.save_session(session, persist_to_db=False)

        # Check if we have basics (date + pincode) - city/state will be auto-filled
        if not self._has_delivery_basics(delivery_data):
            # Check if user provided at least one field (partial data)
            has_date = bool(delivery_data and delivery_data.get("deliveryDate") and str(delivery_data.get("deliveryDate")).strip())
            has_pincode = bool(delivery_data and delivery_data.get("pincode") and str(delivery_data.get("pincode")).strip())

            if has_date or has_pincode:
                # Partial data - show format with missing field indicators
                return await self._display_delivery_missing_fields(user, session, delivery_data, date_validation_error)
            else:
                # No data extracted - check if there was a date validation error
                if date_validation_error:
                    msg = f"{date_validation_error}\n\nPlease provide a valid delivery date and delivery pincode."
                else:
                    msg = "Please provide your delivery date and delivery pincode."
                buttons_config = [
                    {"id": "restart_rfq", "title": "Restart"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    msg,
                    buttons_config,
                    "Details Required",
                    footer=""
                )
                await self.session_manager.save_session(session, persist_to_db=False)
                return {"status": "awaiting_delivery_details"}

        # Check full completeness (including city/state after auto-fill)
        if not self._is_delivery_complete(delivery_data):
            # We have date+pincode but city/state lookup failed
            error_msg = f"Unable to find location for pincode {delivery_data.get('pincode')}. Please provide a valid pincode."
            await self.whatsapp_service.send_message(user.phone_number, error_msg)
            # Save session before returning
            await self.session_manager.save_session(session, persist_to_db=False)
            return {"status": "invalid_pincode"}

        # Display delivery details with Confirm/Modify buttons
        return await self._display_delivery_confirmation(user, session, delivery_data)

    async def _process_delivery_modification_direct(self, user: User, session: ConversationSession,
                                                     message: str) -> Dict[str, Any]:
        """
        Process delivery modification using direct format parsing (NO entity extraction).

        Flow:
        1. Parse format using pure parsing
        2. If invalid → check if it's a question/general message
        3. If question → answer and remind about modification
        4. If modification attempt but invalid → increment retry, show error
        5. If retry >= 3 → cancel workflow
        6. If valid → update data directly, display confirmation
        """
        logger.info(f"[SECTIONED_RFQ] Processing delivery modification (direct format parsing)")

        # Step 1: Parse format (pure parsing, no AI)
        parsed_result = sectioned_rfq_format_parser.parse_delivery_format(message)

        # Check if there's additional text (e.g., a question) after the format
        additional_text = parsed_result.get("additional_text", "")
        if additional_text:
            logger.info(f"[SECTIONED_RFQ] Additional text found after format: {additional_text[:100]}...")
            # TODO: Handle the additional text (could be a question) after processing the format

        # Step 2: Check if format valid
        if parsed_result.get("error"):
            # Format invalid - increment retry
            retry_count = WorkflowManager.increment_section_retry(session, "date_location")
            logger.warning(f"[SECTIONED_RFQ] Delivery format invalid, retry count: {retry_count}")

            if retry_count >= MAX_RETRY_ATTEMPTS:
                # Max retries - cancel workflow
                return await self._cancel_after_max_retries(user, session, "date_location")

            # Send error with retry count and Restart button
            error_msg = f"Format is not valid. {parsed_result['error']}\n\n"
            error_msg += f"Please copy paste the format and change the values as needed.\n"
            error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"

            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons_config,
                "Format Error",
                footer=""
            )

            # Save session to persist retry count
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "format_error", "retry_count": retry_count}

        # Step 3: Format valid - validate date and pincode before accepting
        delivery_date = parsed_result["deliveryDate"]
        pincode = parsed_result["pincode"]

        # Step 3a: Validate delivery date (check if not in the past)
        date_validation = await self._validate_delivery_date(delivery_date)
        if not date_validation["is_valid"]:
            retry_count = WorkflowManager.increment_section_retry(session, "date_location")
            logger.warning(f"[SECTIONED_RFQ] Delivery date invalid: {date_validation['error']}, retry count: {retry_count}")

            if retry_count >= MAX_RETRY_ATTEMPTS:
                return await self._cancel_after_max_retries(user, session, "date_location")

            error_msg = f"{date_validation['error']}\n\n"
            error_msg += f"Please copy paste the format and provide a valid delivery date.\n"
            error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"

            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons_config,
                "Invalid Date",
                footer=""
            )
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "date_validation_error", "retry_count": retry_count}

        # Use normalized date from validation
        normalized_date = date_validation.get("normalized_date", delivery_date)

        # Step 3b: Validate pincode and get location
        pincode_validation = await self._validate_pincode_and_get_location(pincode)
        if not pincode_validation["is_valid"]:
            retry_count = WorkflowManager.increment_section_retry(session, "date_location")
            logger.warning(f"[SECTIONED_RFQ] Pincode invalid: {pincode_validation['error']}, retry count: {retry_count}")

            if retry_count >= MAX_RETRY_ATTEMPTS:
                return await self._cancel_after_max_retries(user, session, "date_location")

            error_msg = f"{pincode_validation['error']}\n\n"
            error_msg += f"Please copy paste the format and provide a valid 6-digit pincode.\n"
            error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"

            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons_config,
                "Invalid Pincode",
                footer=""
            )
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "pincode_validation_error", "retry_count": retry_count}

        # Step 4: All validations passed - update delivery data with validated values
        delivery_data = {
            "deliveryDate": normalized_date,
            "pincode": pincode,
            "city": pincode_validation.get("city", parsed_result["city"]),
            "state": pincode_validation.get("state", parsed_result["state"])
        }

        logger.info(f"[SECTIONED_RFQ] Updating delivery data: {delivery_data}")
        WorkflowManager.update_section_data(session, "date_location", delivery_data)
        WorkflowManager.reset_section_retry(session, "date_location")
        WorkflowManager.set_awaiting_section_modification(session, "date_location", False)

        # Save session immediately after update to persist changes
        await self.session_manager.save_session(session, persist_to_db=False)

        logger.info(f"[SECTIONED_RFQ] Delivery data updated successfully via direct modification")

        # Step 5: Re-display for confirmation
        return await self._display_delivery_confirmation(user, session, delivery_data)

    async def _display_delivery_confirmation(self, user: User, session: ConversationSession,
                                            delivery_data: Dict) -> Dict[str, Any]:
        """Display delivery details with Confirm/Modify buttons."""
        display_text = sectioned_rfq_format_parser.generate_delivery_display(delivery_data)

        message = f"Delivery Details:\n\n{display_text}"

        # Send message with Confirm/Modify/Restart buttons
        buttons_config = [
            {"id": "confirm_date_location", "title": "Confirm"},
            {"id": "modify_date_location", "title": "Modify"},
            {"id": "restart_rfq", "title": "Restart"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Confirmation Required",
            footer=""  # Empty footer to prevent accidental exit triggers
        )

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_delivery_confirmation"}

    async def _display_delivery_missing_fields(self, user: User, session: ConversationSession,
                                               delivery_data: Dict, validation_error: str = None) -> Dict[str, Any]:
        """Display delivery format with missing field indicators and Modify button."""
        display_text, missing_fields = sectioned_rfq_format_parser.generate_delivery_display_with_missing(delivery_data)

        # Build the missing field label
        missing_label = " / ".join(missing_fields) if missing_fields else "Missing Field"

        # Include validation error if provided
        if validation_error:
            message = f"{validation_error}\n\n{missing_label} Required\n\nDelivery Details:\n\n{display_text}\n\nPlease copy the format above and provide the correct details."
        else:
            message = f"{missing_label} Required\n\nDelivery Details:\n\n{display_text}\n\nPlease provide the missing details to move forward."

        # Send message with Modify/Restart buttons (no Confirm since data is incomplete)
        buttons_config = [
            {"id": "modify_date_location", "title": "Modify"},
            {"id": "restart_rfq", "title": "Restart"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Missing Some Details",
            footer=""
        )

        # Set awaiting modification so user can fill in the format directly
        WorkflowManager.set_awaiting_section_modification(session, "date_location", True)

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_delivery_details"}

    # ========================================================================
    # ITEMS SECTION
    # ========================================================================

    async def _handle_items_section(self, user: User, session: ConversationSession,
                                    message: str, attachments: List) -> Dict[str, Any]:
        """
        Handle items section.

        Flow:
        1. Check if awaiting modification → process format modification
        2. Check if we have items → display confirmation
        3. Otherwise → extract items from message
        """
        logger.info(f"[SECTIONED_RFQ] Handling items section")

        # Check if awaiting modification
        if WorkflowManager.is_awaiting_section_modification(session, "items"):
            return await self._process_items_modification_direct(user, session, message)

        # Check if we have items from initial extraction or previous entry
        items_data = WorkflowManager.get_section_data(session, "items")

        # If we already have items, check if user sent data in structured format
        # This handles the case where user directly copies and modifies the confirmation format
        if items_data and len(items_data) > 0:
            # Check if message looks like it's attempting the structured format
            # Look for format keywords to detect modification attempts
            message_lower = message.lower()
            if "item " in message_lower and ("qty:" in message_lower or "quantity:" in message_lower):
                logger.info(f"[SECTIONED_RFQ] User sent message in items format - routing to modification handler")
                return await self._process_items_modification_direct(user, session, message)

        if not items_data or len(items_data) == 0:
            # Need to extract items - ONE TIME entity extraction
            logger.info(f"[SECTIONED_RFQ] Extracting items from message")

            entity_context = self._build_entity_context(session)
            entity_result = await self.entity_service.extract_entities(
                message, context=entity_context, workflow_type="buy_something"
            )

            items_data = entity_result.get("products", [])
            WorkflowManager.update_section_data(session, "items", items_data)
        elif message and message.strip():
            # We have existing items AND user provided a message - user might be providing missing fields
            # Re-extract and merge with existing items
            logger.info(f"[SECTIONED_RFQ] Re-extracting to merge with existing {len(items_data)} items")

            # Build context with existing items
            entity_context = self._build_entity_context_with_items(session, items_data)
            entity_result = await self.entity_service.extract_entities(
                message, context=entity_context, workflow_type="buy_something"
            )

            # Merge new extraction with existing items
            new_items = entity_result.get("products", [])
            if new_items:
                items_data = new_items  # Use the merged result from entity service
                WorkflowManager.update_section_data(session, "items", items_data)
                logger.info(f"[SECTIONED_RFQ] Updated items after re-extraction: {len(items_data)} items")
        else:
            # We have existing items and message is empty - just use existing items
            logger.info(f"[SECTIONED_RFQ] Using existing {len(items_data)} items from initial message")

        # Check if we have at least one item
        if not items_data or len(items_data) == 0:
            # No items yet, ask user
            msg = ("Please share the items for your RFQ with name, brand/specs (if any), and quantity.\n\n"
                   "Example:\n"
                   "Laptop Dell Inspiron - 5, Printer HP LaserJet - 2, Desktop HP 17\" - 10")
            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                msg,
                buttons_config,
                "Items Required",
                footer=""
            )
            return {"status": "awaiting_items"}

        # Check if all items are complete (have mandatory fields)
        incomplete_items = self._get_incomplete_items(items_data)
        if incomplete_items:
            # Some items are missing mandatory fields - show format with missing field indicators
            logger.info(f"[SECTIONED_RFQ] {len(incomplete_items)} items are incomplete, showing format with missing fields")
            return await self._display_items_missing_fields(user, session, items_data, incomplete_items)

        # All items complete - display items with Confirm/Modify buttons
        return await self._display_items_confirmation(user, session, items_data)

    async def _process_items_modification_direct(self, user: User, session: ConversationSession,
                                                 message: str) -> Dict[str, Any]:
        """
        Process items modification using direct format parsing (NO entity extraction).

        Flow:
        1. Parse format using pure parsing
        2. If invalid → check if it's a question/general message
        3. If question → answer and remind about modification
        4. If modification attempt but invalid → increment retry, show error
        5. If retry >= 3 → cancel workflow
        6. If valid → update data directly, display confirmation
        """
        logger.info(f"[SECTIONED_RFQ] Processing items modification (direct format parsing)")

        # Step 1: Parse format (pure parsing, no AI)
        parsed_result = sectioned_rfq_format_parser.parse_items_format(message)

        # Check if there's additional text (e.g., a question) after the format
        additional_text = parsed_result.get("additional_text", "")
        if additional_text:
            logger.info(f"[SECTIONED_RFQ] Additional text found after format: {additional_text[:100]}...")
            # TODO: Handle the additional text (could be a question) after processing the format

        # Step 2: Check if format valid
        if parsed_result.get("error"):
            # Format invalid - increment retry
            retry_count = WorkflowManager.increment_section_retry(session, "items")
            logger.warning(f"[SECTIONED_RFQ] Items format invalid, retry count: {retry_count}")

            if retry_count >= MAX_RETRY_ATTEMPTS:
                # Max retries - cancel workflow
                return await self._cancel_after_max_retries(user, session, "items")

            # Send error with retry count and Restart button
            error_msg = f"Format is not valid. {parsed_result['error']}\n\n"
            error_msg += f"Please copy paste the format, change the values, add new products, or delete products as needed.\n"
            error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"

            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons_config,
                "Format Error",
                footer=""
            )

            # Save session to persist retry count
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "format_error", "retry_count": retry_count}

        # Step 3: Format valid - DIRECTLY replace entire items array (no AI processing)
        items_data = parsed_result["items"]

        logger.info(f"[SECTIONED_RFQ] Updating items data: {len(items_data)} items")
        WorkflowManager.update_section_data(session, "items", items_data)
        WorkflowManager.reset_section_retry(session, "items")
        WorkflowManager.set_awaiting_section_modification(session, "items", False)

        # Save session immediately after update to persist changes
        await self.session_manager.save_session(session, persist_to_db=False)

        logger.info(f"[SECTIONED_RFQ] Items data updated successfully via direct modification ({len(items_data)} items)")

        # Step 4: Re-display for confirmation
        return await self._display_items_confirmation(user, session, items_data)

    async def _display_items_confirmation(self, user: User, session: ConversationSession,
                                         items_data: List) -> Dict[str, Any]:
        """Display items with Confirm/Modify buttons."""
        display_text = sectioned_rfq_format_parser.generate_items_display(items_data)

        message = f"RFQ Items ({len(items_data)}):\n\n{display_text}"

        # Send message with Confirm/Modify/Restart buttons
        buttons_config = [
            {"id": "confirm_items", "title": "Confirm"},
            {"id": "modify_items", "title": "Modify"},
            {"id": "restart_rfq", "title": "Restart"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Confirmation Required",
            footer=""  # Empty footer to prevent accidental exit triggers
        )

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_items_confirmation"}

    async def _display_items_missing_fields(self, user: User, session: ConversationSession,
                                            items_data: List, incomplete_items: List[Dict]) -> Dict[str, Any]:
        """Display items format with missing field indicators and Modify button."""
        display_text, missing_labels = sectioned_rfq_format_parser.generate_items_display_with_missing(items_data, incomplete_items)

        # Build the missing field label
        missing_label = " / ".join(missing_labels) if missing_labels else "Missing Field"

        message = f"{missing_label} Required\n\nRFQ Items ({len(items_data)}):\n\n{display_text}\n\nPlease provide the missing details to move forward."

        # Send message with Modify/Restart buttons (no Confirm since data is incomplete)
        buttons_config = [
            {"id": "modify_items", "title": "Modify"},
            {"id": "restart_rfq", "title": "Restart"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Missing Some Details",
            footer=""
        )

        # Set awaiting modification so user can fill in the format directly
        WorkflowManager.set_awaiting_section_modification(session, "items", True)

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_missing_item_fields"}

    # ========================================================================
    # ATTACHMENTS SECTION
    # ========================================================================

    async def _handle_attachments_section(self, user: User, session: ConversationSession,
                                         message: str, attachments: List) -> Dict[str, Any]:
        """
        Handle attachments section.

        First time: Ask about attachments with Continue button
        Subsequent: Delegate to confirmation_handler for attachment collection
        """
        logger.info(f"[SECTIONED_RFQ] Handling attachments section")

        # First time entering this section - prepare data and ask about attachments
        if not session.workflow_state.get("sectioned_rfq_attachment_question_asked"):
            # Transform sectioned RFQ data to pending_optional_combined_rfq format
            delivery_data = WorkflowManager.get_section_data(session, "date_location")
            items_data = WorkflowManager.get_section_data(session, "items")

            # Build entities from sectioned data
            combined_data = self._build_combined_rfq_from_sections(delivery_data, items_data)

            # Set as pending_optional_combined_rfq so confirmation_handler can work with it
            session.workflow_state["pending_optional_combined_rfq"] = combined_data
            session.workflow_state["optional_fields_asked"] = True  # Mark as asked
            session.workflow_state["sectioned_rfq_attachment_question_asked"] = True

            await self.session_manager.save_session(session, persist_to_db=False)

            # Ask about attachments directly (don't call confirmation_handler yet)
            optional_message = (
                "Would you like to add any specification documents, product images, or attachments to your RFQ?\n\n"
                "If yes, please upload them now — or click on 'Continue' to skip and proceed."
            )

            # Send message with Continue button
            buttons_config = [
                {"id": "continue_rfq", "title": "Continue"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                optional_message,
                buttons_config,
                "Optional Information"
            )

            logger.info(f"[SECTIONED_RFQ] Asked about attachments with Continue button")
            return {"status": "awaiting_attachments_decision"}

        # User responded - delegate to existing handler
        result = await self.confirmation_handler.handle_optional_fields_response(user, session, message)

        # Check if we moved to confirmation phase (pending_combined_rfq was set)
        if session.workflow_state.get("pending_combined_rfq"):
            # Optional fields phase complete - move to final confirmation section
            logger.info(f"[SECTIONED_RFQ] Optional fields complete - moving to final confirmation section")
            WorkflowManager.confirm_sectioned_section(session, "attachments")
            WorkflowManager.set_sectioned_rfq_section(session, "final_confirmation")
            await self.session_manager.save_session(session, persist_to_db=False)

            # The confirmation_handler already sent the final confirmation message
            # Just return the result
            return result

        return result

    # ========================================================================
    # FINAL CONFIRMATION SECTION
    # ========================================================================

    async def _handle_final_confirmation(self, user: User, session: ConversationSession,
                                        message: str) -> Dict[str, Any]:
        """
        Handle final confirmation section.

        Delegates to existing confirmation_handler.handle_pending_confirmations()
        to reuse the robust confirmation and submission logic.
        """
        logger.info(f"[SECTIONED_RFQ] Handling final confirmation - delegating to confirmation handler")

        # The pending_combined_rfq should already be set by the attachments section
        # If not, we need to set it now
        if not session.workflow_state.get("pending_combined_rfq"):
            logger.info(f"[SECTIONED_RFQ] pending_combined_rfq not set, building it now")
            delivery_data = WorkflowManager.get_section_data(session, "date_location")
            items_data = WorkflowManager.get_section_data(session, "items")

            # Build combined RFQ data
            combined_data = self._build_combined_rfq_from_sections(delivery_data, items_data)

            # Set as pending_combined_rfq so confirmation_handler can work with it
            session.workflow_state["pending_combined_rfq"] = combined_data
            await self.session_manager.save_session(session, persist_to_db=False)

        # Delegate to existing confirmation handler
        # This will handle the confirmation message display, user response, and RFQ submission
        return await self.confirmation_handler.handle_pending_confirmations(user, session, message, {})

    # ========================================================================
    # BUTTON HANDLERS
    # ========================================================================

    async def handle_section_button_click(self, user: User, session: ConversationSession,
                                         button_id: str) -> Dict[str, Any]:
        """
        Handle Confirm/Modify button clicks for sections.

        Args:
            user: User object
            session: Conversation session
            button_id: Button ID (e.g., "confirm_delivery", "modify_items")

        Returns:
            Dict with response data
        """
        logger.info(f"[SECTIONED_RFQ] Button clicked: {button_id}")

        # Parse button ID
        if button_id.startswith("confirm_"):
            section = button_id.replace("confirm_", "")
            return await self._handle_section_confirm(user, session, section)
        elif button_id.startswith("modify_"):
            section = button_id.replace("modify_", "")
            return await self._handle_section_modify(user, session, section)
        elif button_id == "restart_rfq":
            return await self._handle_restart_rfq(user, session)
        elif button_id == "final_confirm_rfq":
            return await self._handle_final_rfq_submission(user, session)
        elif button_id == "final_cancel_rfq":
            return await self._handle_final_cancel(user, session)
        elif button_id.startswith("attachments_"):
            return await self._handle_attachment_decision(user, session, button_id)
        else:
            logger.error(f"[SECTIONED_RFQ] Unknown button ID: {button_id}")
            return {"status": "error"}

    async def _handle_section_confirm(self, user: User, session: ConversationSession,
                                      section: str) -> Dict[str, Any]:
        """Handle section confirmation."""
        logger.info(f"[SECTIONED_RFQ] Confirming section: {section}")

        # Mark section as confirmed
        WorkflowManager.confirm_sectioned_section(session, section)

        # Get next section
        next_section = self._get_next_section(section)

        if next_section:
            # Move to next section
            WorkflowManager.set_sectioned_rfq_section(session, next_section)
            await self.session_manager.save_session(session, persist_to_db=False)

            # Initiate next section
            return await self._initiate_next_section(user, session, next_section)
        else:
            # No next section, workflow complete
            logger.error(f"[SECTIONED_RFQ] No next section after {section}")
            return {"status": "error"}

    async def _handle_section_modify(self, user: User, session: ConversationSession,
                                     section: str) -> Dict[str, Any]:
        """Handle section modification request."""
        logger.info(f"[SECTIONED_RFQ] Modify requested for section: {section}")

        # Set awaiting modification flag
        WorkflowManager.set_awaiting_section_modification(session, section, True)
        WorkflowManager.reset_section_retry(session, section)  # Reset retry counter

        await self.session_manager.save_session(session, persist_to_db=False)

        # Send modification instructions with current data
        return await self._send_modification_instructions(user, session, section)

    async def _send_modification_instructions(self, user: User, session: ConversationSession,
                                             section: str) -> Dict[str, Any]:
        """Send modification instructions with current data in format."""
        if section == "date_location":
            current_data = WorkflowManager.get_section_data(session, "date_location")
            format_display = sectioned_rfq_format_parser.generate_delivery_display(current_data)

            message = (f"To modify, please copy and follow the same format. "
                      f"You can update any details:\n\n"
                      f"{format_display}")

        elif section == "items":
            current_data = WorkflowManager.get_section_data(session, "items")
            format_display = sectioned_rfq_format_parser.generate_items_display(current_data)

            message = (f"To modify, please copy and follow the same format. "
                      f"You can modify any details, add new items, or delete items:\n\n"
                      f"{format_display}")
        else:
            message = "Please provide your modifications."

        # Send with Restart button
        buttons_config = [
            {"id": "restart_rfq", "title": "Restart"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Modify Details",
            footer=""
        )

        return {"status": "awaiting_modification"}

    async def _handle_restart_rfq(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """
        Handle restart button click - asks for confirmation before cancelling workflow.

        Uses cancel_service to clear the sectioned RFQ workflow and show buyer menu.
        If user declines, re-displays the confirmation screen they were on.
        """
        logger.info(f"[SECTIONED_RFQ] Restart button clicked by user {user.phone_number}")

        # Get current section to redisplay if user declines
        current_section = WorkflowManager.get_sectioned_rfq_section(session)

        # Use cancel_service to handle restart with confirmation
        cancel_result = await self.cancel_service.handle_cancel_intent(
            user.phone_number,
            session,
            message="restart"
        )

        logger.info(f"[SECTIONED_RFQ] Cancel service result: {cancel_result}")

        # If user declined cancellation, re-display the confirmation screen
        if cancel_result.get("status") == "cancelled_aborted":
            logger.info(f"[SECTIONED_RFQ] User declined restart, re-displaying {current_section} confirmation")

            # Get section data and re-display confirmation
            if current_section == "date_location":
                delivery_data = WorkflowManager.get_section_data(session, "date_location")
                return await self._display_delivery_confirmation(user, session, delivery_data)
            elif current_section == "items":
                items_data = WorkflowManager.get_section_data(session, "items")
                return await self._display_items_confirmation(user, session, items_data)

        return cancel_result

    async def _handle_attachment_decision(self, user: User, session: ConversationSession,
                                          button_id: str) -> Dict[str, Any]:
        """Handle attachment yes/no decision."""
        if button_id == "attachments_yes":
            msg = "Please send your attachments (images, documents, etc.)."
            await self.whatsapp_service.send_message(user.phone_number, msg)
            return {"status": "awaiting_attachments"}
        else:
            # No attachments, move to final confirmation
            WorkflowManager.confirm_sectioned_section(session, "attachments")
            WorkflowManager.set_sectioned_rfq_section(session, "final_confirmation")
            await self.session_manager.save_session(session, persist_to_db=False)
            return await self._handle_final_confirmation(user, session, "")

    async def _handle_final_rfq_submission(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Handle final RFQ submission (to be implemented - integrate with existing submission service)."""
        logger.info(f"[SECTIONED_RFQ] Final RFQ submission")

        # TODO: Integrate with existing RFQ submission service
        # This would call the backend API to create the RFQ

        msg = "✅ Your RFQ has been created successfully! (Submission integration pending)"
        await self.whatsapp_service.send_message(user.phone_number, msg)

        # Mark workflow as completed
        WorkflowManager.confirm_sectioned_section(session, "final_confirmation")
        session.workflow_type = WorkflowType.rfq_submitted
        await self.session_manager.save_session(session, persist_to_db=True)

        return {"status": "rfq_submitted"}

    async def _handle_final_cancel(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Handle final cancel - offer restart."""
        return await self._offer_restart_confirmation(user, session)

    # ========================================================================
    # RESTART FLOW
    # ========================================================================

    async def _offer_restart_confirmation(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Offer restart confirmation dialog."""
        logger.info(f"[SECTIONED_RFQ] Offering restart confirmation")

        message = ("Restart workflow? All your progress will be lost and you'll start from the beginning. Please reply with 'Yes' to restart or 'No' to continue.")

        await self.whatsapp_service.send_message(user.phone_number, message)

        WorkflowManager.set_sectioned_rfq_pending_restart(session, True)
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_restart_confirmation"}

    async def _handle_restart_confirmation_response(self, user: User, session: ConversationSession,
                                                    message: str) -> Dict[str, Any]:
        """Handle restart confirmation response."""
        # Check for explicit yes/no in message
        message_lower = message.lower().strip()

        if message_lower in ["yes", "restart", "yes restart"]:
            confirmed = True
        elif message_lower in ["no", "continue", "no continue"]:
            confirmed = False
        else:
            # Not a clear response, wait for button click or clearer response
            msg = "Please click Yes or No, or type 'yes' to restart or 'no' to continue."
            await self.whatsapp_service.send_message(user.phone_number, msg)
            return {"status": "awaiting_restart_confirmation"}

        if confirmed:
            # Reset all sectioned RFQ state
            logger.info(f"[SECTIONED_RFQ] User confirmed restart, resetting workflow")
            WorkflowManager.reset_sectioned_rfq(session)
            WorkflowManager.set_sectioned_rfq_section(session, "date_location")
            await self.session_manager.save_session(session, persist_to_db=False)

            # Restart from beginning
            msg = "Workflow restarted. Let's start by the Delivery Date and Delivery Pincode.\n\n"
            msg += "Note: If you are expecting the delivery at different locations or on different dates, "
            msg += "we request you create separate RFQs."
            await self.whatsapp_service.send_message(user.phone_number, msg)

            return {"status": "workflow_restarted"}
        else:
            # Continue where they left off
            logger.info(f"[SECTIONED_RFQ] User declined restart, continuing")
            WorkflowManager.set_sectioned_rfq_pending_restart(session, False)
            await self.session_manager.save_session(session, persist_to_db=False)

            msg = "Continuing with your request..."
            await self.whatsapp_service.send_message(user.phone_number, msg)

            return {"status": "restart_declined"}

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def _build_entity_context(self, session: ConversationSession) -> Dict[str, Any]:
        """Build context for entity extraction."""
        # Get existing delivery data to pass as global_supplementary_fields
        delivery_data = WorkflowManager.get_section_data(session, "date_location")

        # Prepare global_supplementary_fields from existing delivery data
        global_supplementary_fields = {}
        if delivery_data:
            global_supplementary_fields = {
                "deliveryDate": delivery_data.get("deliveryDate", ""),
                "pincode": delivery_data.get("pincode", ""),
                "city": delivery_data.get("city", ""),
                "state": delivery_data.get("state", "")
            }
            # Filter out empty strings
            global_supplementary_fields = {k: v for k, v in global_supplementary_fields.items() if v and v.strip()}

        # Build workflow_state with global_supplementary_fields
        workflow_state = session.workflow_state.copy() if session.workflow_state else {}
        if global_supplementary_fields:
            workflow_state["global_supplementary_fields"] = global_supplementary_fields

        return {
            "session_id": session.session_id,
            "workflow_type": session.workflow_type.value if session.workflow_type else None,
            "conversation_history": session.conversation_history,
            "workflow_state": workflow_state
        }

    def _build_entity_context_with_items(self, session: ConversationSession, items_data: List[Dict]) -> Dict[str, Any]:
        """Build context for entity extraction with existing items."""
        # Get existing delivery data to pass as global_supplementary_fields
        delivery_data = WorkflowManager.get_section_data(session, "date_location")

        # Prepare global_supplementary_fields from existing delivery data
        global_supplementary_fields = {}
        if delivery_data:
            global_supplementary_fields = {
                "deliveryDate": delivery_data.get("deliveryDate", ""),
                "pincode": delivery_data.get("pincode", ""),
                "city": delivery_data.get("city", ""),
                "state": delivery_data.get("state", "")
            }
            # Filter out empty strings
            global_supplementary_fields = {k: v for k, v in global_supplementary_fields.items() if v and v.strip()}

        # Build workflow_state with global_supplementary_fields AND incomplete_products
        workflow_state = session.workflow_state.copy() if session.workflow_state else {}
        if global_supplementary_fields:
            workflow_state["global_supplementary_fields"] = global_supplementary_fields

        # Add items as incomplete_products so entity service can merge
        incomplete_products = []
        for item in items_data:
            incomplete_products.append({
                "entities": item,
                "missing_fields": []  # Will be recalculated by entity service
            })
        workflow_state["incomplete_products"] = incomplete_products

        return {
            "session_id": session.session_id,
            "workflow_type": session.workflow_type.value if session.workflow_type else None,
            "conversation_history": session.conversation_history,
            "workflow_state": workflow_state
        }

    def _has_delivery_basics(self, delivery_data: Dict) -> bool:
        """Check if delivery data has basic required fields (date + pincode)."""
        if not delivery_data:
            return False
        # Check for actual values, not just empty strings
        date = delivery_data.get("deliveryDate")
        pincode = delivery_data.get("pincode")
        return bool(date and date.strip()) and bool(pincode and str(pincode).strip())

    def _is_delivery_complete(self, delivery_data: Dict) -> bool:
        """Check if delivery data has all required fields including auto-filled city/state."""
        if not delivery_data:
            return False
        # Check all fields have actual values, not just empty strings
        for field in ["deliveryDate", "pincode", "city", "state"]:
            value = delivery_data.get(field)
            if not value or not str(value).strip():
                return False
        return True

    async def _autofill_location_from_pincode(self, delivery_data: Dict) -> Dict:
        """Auto-fill city and state from pincode using existing pincode lookup."""
        pincode = delivery_data.get("pincode")
        if not pincode:
            return delivery_data

        try:
            # Validate pincode format
            clean_pincode = str(pincode).strip()
            if not clean_pincode.isdigit() or len(clean_pincode) != 6:
                logger.warning(f"[SECTIONED_RFQ] Invalid pincode format: {pincode}")
                return delivery_data

            # Lookup location
            location_data = await get_location_from_pincode_async(clean_pincode)
            if location_data:
                if not delivery_data.get("city") and location_data.get("city"):
                    delivery_data["city"] = location_data["city"]
                    logger.info(f"[SECTIONED_RFQ] Auto-filled city: {location_data['city']}")

                if not delivery_data.get("state") and location_data.get("state"):
                    delivery_data["state"] = location_data["state"]
                    logger.info(f"[SECTIONED_RFQ] Auto-filled state: {location_data['state']}")
            else:
                logger.warning(f"[SECTIONED_RFQ] No location data found for pincode: {pincode}")

        except Exception as e:
            logger.error(f"[SECTIONED_RFQ] Error auto-filling location from pincode: {e}")

        return delivery_data

    async def _validate_delivery_date(self, date_str: str) -> Dict[str, Any]:
        """
        Validate delivery date using OpenAI service.

        Returns:
            Dict with is_valid, normalized_date (if valid), and error (if invalid)
        """
        try:
            validation_result = await self.entity_service.openai_service.validate_delivery_date(
                raw_date_input=date_str,
                extracted_date=date_str
            )

            if validation_result.get("is_valid"):
                normalized = validation_result.get("normalized_date", date_str)
                # Format date as "dd Month" for display
                formatted_date = _format_date_for_display(normalized)
                return {
                    "is_valid": True,
                    "normalized_date": formatted_date
                }
            else:
                error_msg = validation_result.get("user_friendly_message", "Invalid delivery date. Please provide a valid future date.")
                return {
                    "is_valid": False,
                    "error": error_msg
                }
        except Exception as e:
            logger.error(f"[SECTIONED_RFQ] Error validating delivery date: {e}")
            # On error, allow the date to pass through (fail open)
            return {"is_valid": True, "normalized_date": date_str}

    async def _validate_pincode_and_get_location(self, pincode: str) -> Dict[str, Any]:
        """
        Validate pincode format and lookup location.

        Returns:
            Dict with is_valid, city, state (if valid), and error (if invalid)
        """
        try:
            # Validate pincode format
            clean_pincode = str(pincode).strip()
            if not clean_pincode.isdigit() or len(clean_pincode) != 6:
                return {
                    "is_valid": False,
                    "error": f"Invalid pincode format: {pincode}. Please provide a valid 6-digit pincode."
                }

            # Lookup location
            location_data = await get_location_from_pincode_async(clean_pincode)
            if location_data and location_data.get("city") and location_data.get("state"):
                return {
                    "is_valid": True,
                    "city": location_data["city"],
                    "state": location_data["state"]
                }
            else:
                return {
                    "is_valid": False,
                    "error": f"Could not find location for pincode {pincode}. Please provide a valid Indian pincode."
                }
        except Exception as e:
            logger.error(f"[SECTIONED_RFQ] Error validating pincode: {e}")
            return {
                "is_valid": False,
                "error": f"Error validating pincode {pincode}. Please try again."
            }

    def _get_incomplete_items(self, items_data: List[Dict]) -> List[Dict]:
        """
        Check which items are incomplete (missing mandatory fields).

        Returns:
            List of dicts with format: {"index": int, "item": dict, "missing_fields": list}
        """
        incomplete = []

        for i, item in enumerate(items_data):
            missing_fields = []

            # Check mandatory fields
            if not item.get("description"):
                missing_fields.append("description")
            if not item.get("quantity"):
                missing_fields.append("quantity")

            if missing_fields:
                incomplete.append({
                    "index": i + 1,
                    "item": item,
                    "missing_fields": missing_fields
                })

        return incomplete

    def _generate_missing_items_fields_message(self, incomplete_items: List[Dict], all_items: List[Dict]) -> str:
        """
        Generate a message asking for missing fields in items.

        Args:
            incomplete_items: List of incomplete items with missing_fields
            all_items: All items (for context)

        Returns:
            Message string
        """
        msg = "I need some additional details:\n\n"

        for incomplete_item in incomplete_items:
            index = incomplete_item["index"]
            item = incomplete_item["item"]
            missing_fields = incomplete_item["missing_fields"]

            item_desc = item.get("description", f"Item {index}")
            msg += f"**{item_desc}**\n"

            for field in missing_fields:
                if field == "quantity":
                    msg += f"  • Quantity\n"
                elif field == "description":
                    msg += f"  • Product description\n"

            msg += "\n"

        msg += "Please provide these details."
        return msg

    def _generate_delivery_missing_fields_message(self, delivery_data: Dict) -> str:
        """Generate message for missing delivery fields."""
        missing = []
        if not delivery_data.get("deliveryDate"):
            missing.append("Delivery Date")
        if not delivery_data.get("pincode"):
            missing.append("Delivery Pincode")

        if missing:
            msg = f"I need the following details to proceed:\n"
            for field in missing:
                msg += f"• {field}\n"
            msg += f"\nPlease provide these details."
        else:
            # We have date and pincode, but missing city/state (should be auto-filled)
            msg = "Let me fetch the location details based on your pincode..."

        return msg

    def _get_next_section(self, current_section: str) -> Optional[str]:
        """Get next section after current."""
        section_order = ["date_location", "items", "attachments", "final_confirmation"]

        try:
            current_idx = section_order.index(current_section)
            if current_idx < len(section_order) - 1:
                return section_order[current_idx + 1]
        except ValueError:
            pass

        return None

    async def _initiate_next_section(self, user: User, session: ConversationSession,
                                     next_section: str) -> Dict[str, Any]:
        """Initiate next section with appropriate prompt."""
        if next_section == "items":
            # Check if items were already provided in the initial message
            items_data = WorkflowManager.get_section_data(session, "items")
            if items_data and len(items_data) > 0:
                logger.info(f"[SECTIONED_RFQ] Items already exist from initial message ({len(items_data)} items), displaying confirmation")
                # Items already exist, go directly to items section handler which will display confirmation
                return await self._handle_items_section(user, session, "", [])

            # No items yet, ask for them
            msg = ("Please share the items for your RFQ with name, brand/specs (if any), and quantity — "
                  "you can add multiple items together in one message.\n\n"
                  "📝 Example:\n"
                  "Laptop Dell Inspiron - 5, Printer HP LaserJet - 2, Desktop HP 17\" - 10")
            await self.whatsapp_service.send_message(user.phone_number, msg)
            return {"status": "awaiting_items"}

        elif next_section == "attachments":
            return await self._handle_attachments_section(user, session, "", [])

        elif next_section == "final_confirmation":
            return await self._handle_final_confirmation(user, session, "")

        return {"status": "section_initiated"}

    async def _cancel_after_max_retries(self, user: User, session: ConversationSession,
                                       section_name: str) -> Dict[str, Any]:
        """Cancel workflow after max retries - use existing cancel_service."""
        logger.warning(f"[SECTIONED_RFQ] Max retries ({MAX_RETRY_ATTEMPTS}) reached for section: {section_name}")

        # Use existing cancel_service to clear workflow
        await self.cancel_service._clear_workflow_state(session)

        # Send informative cancellation message
        msg = f"Maximum retry attempts reached for {section_name} section.\n"
        msg += f"The workflow has been cancelled.\n\n"
        msg += f"What would you like to do next?"

        await self.whatsapp_service.send_message(user.phone_number, msg)

        return {"status": "max_retries_cancelled"}

    def _build_combined_rfq_from_sections(self, delivery_data: Dict, items_data: List) -> Dict:
        """
        Build combined RFQ data structure from sectioned data.

        Transforms sectioned RFQ format into the format expected by confirmation_handler.

        Returns:
            {
                "combined_schema": RFQValidationSchema dict,
                "products": List of serialized product entities
            }
        """
        from app.services.helpers.chat_service_helpers import ChatServiceHelpers

        # Build product entities from items
        products = []
        for item in items_data:
            # Each item becomes a product entity
            product_entity = {
                "description": item.get("description", ""),
                "quantity": item.get("quantity"),
                "brand": item.get("brand", ""),
                "remarks": item.get("remarks", ""),
                "unitofMeasures": item.get("unitofMeasures", ""),
                # Add delivery details from delivery_data
                "deliveryDate": delivery_data.get("deliveryDate", ""),
                "pincode": delivery_data.get("pincode", ""),
                "city": delivery_data.get("city", ""),
                "state": delivery_data.get("state", "")
            }

            products.append({
                "entities": product_entity,
                "schema_data": None  # Will be built by create_combined_rfq_schema_from_multiple_products
            })

        # Use existing helper to create combined schema
        combined_schema = ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products(products)

        # Return in the format expected by confirmation_handler
        return {
            "combined_schema": combined_schema.model_dump() if hasattr(combined_schema, 'model_dump') else combined_schema.dict(),
            "products": ChatServiceHelpers.serialize_products_for_session(products)
        }
