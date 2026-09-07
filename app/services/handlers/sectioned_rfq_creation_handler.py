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
from app.services.entity_service import EntityService, strip_no_products_sentinel
from app.services.whatsapp_service import WhatsAppService
from app.services.cancel_service import CancelService
from app.utils import sectioned_rfq_format_parser
from app.utils.pincode_lookup import (
    get_fallback_location,
    get_location_from_pincode_async,
)

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
            return f"{parsed_date.day} {parsed_date.strftime('%B %Y')}"
        except ValueError:
            continue

    # If all parsing fails, return original
    return date_str

# Maximum retry attempts per section
MAX_RETRY_ATTEMPTS = 3

# Maximum items allowed for text input (Excel uploads can have more)
MAX_TEXT_INPUT_ITEMS = 5


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

        # Normalize message if passed as a dict (e.g. interactive button reply)
        if isinstance(message, dict):
            if "button_reply" in message:
                message = message["button_reply"].get("title", "") or message["button_reply"].get("id", "")
            elif "text" in message:
                txt = message.get("text", "")
                message = txt.get("body", "") if isinstance(txt, dict) else str(txt)
            elif "content" in message:
                message = str(message.get("content", ""))
            else:
                message = str(message)
        elif not isinstance(message, str):
            message = str(message or "")

        # Check if pending restart confirmation
        if WorkflowManager.is_sectioned_rfq_pending_restart(session):
            return await self._handle_restart_confirmation_response(user, session, message)

        # Get current section
        current_section = WorkflowManager.get_sectioned_rfq_section(session)

        # Check if data/message is from Excel upload
        is_excel_source = session.workflow_state.get('excel_source', False) if session.workflow_state else False
        

        # Common keyword handling for "Confirm" and "Modify" - applies to all sections
        # Check if user is confirming or requesting modification
        message_lower = message.lower().strip()

        # Handle "Confirm" keyword - but skip if data is from Excel upload to prevent auto-confirmation
        if message_lower in ["confirm", "yes", "y", "proceed", "continue", "ok"] and not is_excel_source:
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

        # Handle "Modify" keyword - but skip if data is from Excel upload to prevent auto-modification
        elif message_lower in ["modify", "change", "edit", "update"] and not is_excel_source:
            # Check if current section has data to modify
            if current_section == "date_location":
                delivery_data = WorkflowManager.get_section_data(session, "date_location")
                if delivery_data and self._is_delivery_complete(delivery_data):
                    return await self._handle_section_modify(user, session, current_section)
            elif current_section == "items":
                items_data = WorkflowManager.get_section_data(session, "items")
                if items_data and len(items_data) > 0:
                    return await self._handle_section_modify(user, session, current_section)

        # Clear excel_source flag after Excel confirmation is processed to allow normal flow
        if is_excel_source and current_section == "date_location":
            session.workflow_state['excel_source'] = False
            await self.session_manager.save_session(session, persist_to_db=False)

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
    # SECTION DATA HELPERS
    # ========================================================================

    @staticmethod
    def _usable_items(items_data: Optional[List]) -> List:
        """
        Keep only product entries worth showing to the user.

        Drops the extractor's "no products mentioned" sentinel and entries that carry no
        information at all. Without this an extraction from a message that mentioned no
        products stored one placeholder item, which made the empty-items checks below
        think items had been collected and produced a prompt asking the user for the
        quantity of an item literally named NO_PRODUCTS_MENTIONED.

        Args:
            items_data: Product dicts from extraction, session state or a parser

        Returns:
            A new list containing only the usable entries
        """
        if not items_data:
            return []

        usable = []
        for item in strip_no_products_sentinel(list(items_data)):
            if not isinstance(item, dict):
                continue
            desc = item.get("description")
            if isinstance(desc, str):
                clean_desc = desc.strip().upper().replace(" ", "_")
                if clean_desc in ("NO_PRODUCTS_MENTIONED", "NO_PRODUCTS", "NONE_MENTIONED"):
                    continue
            # An entry with neither a description nor a quantity holds nothing the user
            # can confirm or correct, so it is not a real item line.
            has_content = any(
                str(item.get(field) or "").strip()
                for field in ("description", "quantity", "brand", "remarks", "unitofMeasures")
            )
            if has_content:
                usable.append(item)
        return usable

    def _store_extracted_items(self, session: ConversationSession, entity_result: Dict[str, Any]) -> List:
        """
        Write extracted products into the items section, ignoring unusable entries.

        Args:
            session: Conversation session
            entity_result: Result from EntityService.extract_entities

        Returns:
            The items that were stored (empty list when there was nothing usable)
        """
        items = self._usable_items(entity_result.get("products"))
        if items:
            WorkflowManager.update_section_data(session, "items", items)
        return items

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

        # If we already have complete delivery data, check if user sent data in structured format
        # This handles the case where user directly copies and modifies the confirmation format
        if delivery_data and self._is_delivery_complete(delivery_data):
            # Check if message looks like it's attempting the structured format
            # Look for format keywords to detect modification attempts (only date and pincode shown to user)
            if any(keyword in message.lower() for keyword in ["delivery date:", "delivery pincode:"]):
                return await self._process_delivery_modification_direct(user, session, message)
        elif delivery_data:
            # We have delivery data but it's not complete - check if user sent data in format
            if any(keyword in message.lower() for keyword in ["delivery date:", "delivery pincode:"]):
                return await self._process_delivery_modification_direct(user, session, message)

        # Track validation errors to show to user if needed.
        # These are read after the extraction block below, which does not always run,
        # so they have to be bound here.
        date_validation_error = None
        date_error = None
        pincode_error = None

        # An empty message carries nothing to extract. This happens when the user
        # arrives from a menu button rather than typing, and calling the extractor on
        # it would spend an OpenAI round trip to learn nothing.
        has_message_text = bool(message and message.strip())

        if has_message_text and (not delivery_data or not self._has_delivery_basics(delivery_data)):
            # Need to extract delivery details - ONE TIME entity extraction
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
            date_error = None
            if delivery_data.get("deliveryDate"):
                date_validation = await self._validate_delivery_date(delivery_data["deliveryDate"])
                if date_validation.get("is_valid"):
                    # Use normalized date format
                    delivery_data["deliveryDate"] = date_validation.get("normalized_date", delivery_data["deliveryDate"])
                else:
                    # Invalid date - clear it and store error to show user
                    date_error = date_validation.get("error", "Invalid delivery date")
                    logger.warning(f"[SECTIONED_RFQ] Invalid delivery date during extraction: {date_error}")
                    delivery_data["deliveryDate"] = ""

            # Auto-fill city/state from pincode - pincode is authoritative source
            pincode_error = None
            if delivery_data.get("pincode"):
                autofill_result = await self._autofill_location_from_pincode(delivery_data)
                delivery_data = autofill_result["delivery_data"]
                # Check if pincode validation failed
                if not autofill_result["is_valid"]:
                    pincode_error = autofill_result["error"]
                    logger.warning(f"[SECTIONED_RFQ] Pincode validation failed during extraction: {pincode_error}")

            # Store extracted delivery data
            WorkflowManager.update_section_data(session, "date_location", delivery_data)

            # Also check if items were provided in initial message
            self._store_extracted_items(session, entity_result)

            # Save session after extraction to persist the delivery data
            await self.session_manager.save_session(session, persist_to_db=False)

        # Reload delivery_data from session to ensure we have the latest stored data
        delivery_data = WorkflowManager.get_section_data(session, "date_location")

        # Check if we have any validation errors - show them together
        if date_error or pincode_error:
            return await self._display_delivery_validation_error(
                user, session, delivery_data,
                date_error=date_error,
                pincode_error=pincode_error
            )

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
                    msg = "Please provide your RFQ items Delivery Date and Delivery Location Pincode."
                buttons_config = [
                    {"id": "restart_rfq", "title": "Restart"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    msg,
                    buttons_config,
                    "Details Required",
                    footer="",
                    session_id=session
                )
                # Do not set awaiting modification on initial prompt - user should be able to provide details naturally
                WorkflowManager.set_awaiting_section_modification(session, "date_location", False)
                await self.session_manager.save_session(session, persist_to_db=False)
                return {"status": "awaiting_delivery_details"}

        # Check full completeness (including city/state after auto-fill)
        if not self._is_delivery_complete(delivery_data):
            # We have date+pincode but city/state lookup failed - pincode is invalid
            pincode = delivery_data.get('pincode', '')
            return await self._display_invalid_pincode_message(user, session, delivery_data, pincode)

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
        # Step 1: Parse format (pure parsing, no AI)
        parsed_result = sectioned_rfq_format_parser.parse_delivery_format(message)

        # Check if there's additional text (e.g., a question) after the format
        additional_text = parsed_result.get("additional_text", "")
        if additional_text:
            logger.info(f"[SECTIONED_RFQ] Additional text found after format: {additional_text[:100]}...")
            # TODO: Handle the additional text (could be a question) after processing the format

        # Step 2: Check if format valid
        if parsed_result.get("error"):
            # Check if message looks like a delivery format attempt (contains delivery-related keywords)
            message_lower = message.lower()
            is_format_attempt = any(keyword in message_lower for keyword in [
                "delivery date:", "delivery pincode:", "date:", "pincode:"
            ])

            if not is_format_attempt:
                # User didn't attempt the format - they may have provided items or other info
                # Try entity extraction to see if there's any date/pincode in the message
                entity_context = self._build_entity_context(session)
                entity_result = await self.entity_service.extract_entities(
                    message, context=entity_context, workflow_type="buy_something"
                )

                # Check if any delivery data was extracted
                extracted_date = entity_result.get("deliveryDate", "")
                extracted_pincode = entity_result.get("pincode", "")

                if extracted_date or extracted_pincode:
                    # Found some delivery data - process it
                    delivery_data = {
                        "deliveryDate": extracted_date,
                        "pincode": extracted_pincode,
                        "city": entity_result.get("city", ""),
                        "state": entity_result.get("state", "")
                    }

                    # Validate date if provided
                    date_error = None
                    if delivery_data.get("deliveryDate"):
                        date_validation = await self._validate_delivery_date(delivery_data["deliveryDate"])
                        if date_validation.get("is_valid"):
                            delivery_data["deliveryDate"] = date_validation.get("normalized_date", delivery_data["deliveryDate"])
                        else:
                            date_error = date_validation.get("error", "Invalid delivery date")
                            delivery_data["deliveryDate"] = ""

                    # Auto-fill city/state from pincode - pincode is authoritative source
                    pincode_error = None
                    if delivery_data.get("pincode"):
                        autofill_result = await self._autofill_location_from_pincode(delivery_data)
                        delivery_data = autofill_result["delivery_data"]
                        # Check if pincode validation failed
                        if not autofill_result["is_valid"]:
                            pincode_error = autofill_result["error"]
                            logger.warning(f"[SECTIONED_RFQ] Pincode validation failed during modification: {pincode_error}")

                    WorkflowManager.update_section_data(session, "date_location", delivery_data)

                    # Store items if provided
                    self._store_extracted_items(session, entity_result)

                    await self.session_manager.save_session(session, persist_to_db=False)

                    # Check if we have any validation errors - show them together
                    if date_error or pincode_error:
                        return await self._display_delivery_validation_error(
                            user, session, delivery_data,
                            date_error=date_error,
                            pincode_error=pincode_error
                        )

                    # Check if we have complete delivery data now
                    if self._is_delivery_complete(delivery_data):
                        WorkflowManager.set_awaiting_section_modification(session, "date_location", False)
                        WorkflowManager.reset_section_retry(session, "date_location")
                        return await self._display_delivery_confirmation(user, session, delivery_data)
                    elif self._has_delivery_basics(delivery_data):
                        # We have date+pincode but city/state lookup failed - pincode is invalid
                        pincode = delivery_data.get('pincode', '')
                        return await self._display_invalid_pincode_message(user, session, delivery_data, pincode)
                    else:
                        return await self._display_delivery_missing_fields(user, session, delivery_data, None)
                else:
                    # No delivery data found - store any items and re-show the format prompt
                    # Don't increment retry since this wasn't a format attempt
                    if self._store_extracted_items(session, entity_result):
                        await self.session_manager.save_session(session, persist_to_db=False)

                    # Re-show the format prompt
                    delivery_data = WorkflowManager.get_section_data(session, "date_location") or {
                        "deliveryDate": "",
                        "pincode": "",
                        "city": "",
                        "state": ""
                    }
                    return await self._display_delivery_missing_fields(user, session, delivery_data, None)

            # User attempted the format but it's invalid - increment retry
            retry_count = WorkflowManager.increment_section_retry(session, "date_location")

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
                footer="",
                session_id=session
            )

            # Save session to persist retry count
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "format_error", "retry_count": retry_count}

        # Step 3: Format valid - validate date and pincode before accepting
        delivery_date = parsed_result["deliveryDate"]
        pincode = parsed_result["pincode"]

        # Validate both date and pincode before showing errors
        date_validation = await self._validate_delivery_date(delivery_date)
        pincode_validation = await self._validate_pincode_and_get_location(pincode)

        date_is_valid = date_validation["is_valid"]
        pincode_is_valid = pincode_validation["is_valid"]

        # Step 3a: Handle validation errors - show both if both are invalid
        if not date_is_valid or not pincode_is_valid:
            retry_count = WorkflowManager.increment_section_retry(session, "date_location")

            if retry_count >= MAX_RETRY_ATTEMPTS:
                return await self._cancel_after_max_retries(user, session, "date_location")

            # Build error message with both errors if both are invalid
            if not date_is_valid and not pincode_is_valid:
                error_msg = f"{date_validation['error']}\n\n{pincode_validation['error']}\n\n"
                error_msg += f"Please copy paste the format and provide a valid delivery date and 6-digit pincode.\n"
                error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"
                header = "Invalid Details"
            elif not date_is_valid:
                # Only date is invalid
                error_msg = f"{date_validation['error']}\n\n"
                error_msg += f"Please copy paste the format and provide a valid delivery date.\n"
                error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"
                header = "Invalid Date"
            else:
                # Only pincode is invalid
                error_msg = f"{pincode_validation['error']}\n\n"
                error_msg += f"Please copy paste the format and provide a valid 6-digit pincode.\n"
                error_msg += f"Attempt {retry_count}/{MAX_RETRY_ATTEMPTS}"
                header = "Invalid Pincode"

            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons_config,
                header,
                footer="",
                session_id=session
            )
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "validation_error", "retry_count": retry_count}

        # All validations passed - update delivery data with validated values
        normalized_date = date_validation.get("normalized_date", delivery_date)
        delivery_data = {
            "deliveryDate": normalized_date,
            "pincode": pincode,
            "city": pincode_validation.get("city", ""),
            "state": pincode_validation.get("state", "")
        }


        WorkflowManager.update_section_data(session, "date_location", delivery_data)
        WorkflowManager.reset_section_retry(session, "date_location")
        WorkflowManager.set_awaiting_section_modification(session, "date_location", False)

        # Save session immediately after update to persist changes
        await self.session_manager.save_session(session, persist_to_db=False)

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
            footer="",  # Empty footer to prevent accidental exit triggers
            session_id=session
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
        copy_paste_instruction = (
            "I'm sorry I do not have all the details to proceed. Can you provide some of the missing details. "
            "In order to do please copy and paste the text provided below this line and then update your information in the correct format.\n\n"
            "————————————————————————"
        )
        if validation_error:
            message = f"{validation_error}\n\n{missing_label} Required\n\n{copy_paste_instruction}\n{display_text}"
        else:
            message = f"{missing_label} Required\n\n{copy_paste_instruction}\n{display_text}"

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
            footer="",
            session_id=session
        )

        # Set awaiting modification so user can fill in the format directly
        WorkflowManager.set_awaiting_section_modification(session, "date_location", True)

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_delivery_details"}

    async def _display_delivery_validation_error(self, user: User, session: ConversationSession,
                                          delivery_data: Dict,
                                          date_error: str = None,
                                          pincode_error: str = None) -> Dict[str, Any]:
        """
        Display delivery validation errors (date, pincode, or both).

        Args:
            user: User object
            session: Conversation session
            delivery_data: Delivery data dictionary
            date_error: Optional date validation error message
            pincode_error: Optional pincode validation error message

        Returns:
            Dict with status
        """
        display_text = generate_delivery_display_with_invalid_pincode(delivery_data)

        copy_paste_instruction = (
            "In order to update, please copy and paste the text provided below this line and correct the details.\n\n"
            "————————————————————————"
        )

        # Build error message based on what's invalid
        if date_error and pincode_error:
            # Both date and pincode are invalid
            message = f"{date_error}\n\n{pincode_error}\n\n{copy_paste_instruction}\n{display_text}"
            header = "Invalid Details"
        elif date_error:
            # Only date is invalid
            message = f"{date_error}\n\n{copy_paste_instruction}\n{display_text}"
            header = "Invalid Date"
        elif pincode_error:
            # Only pincode is invalid
            message = f"{pincode_error}\n\n{copy_paste_instruction}\n{display_text}"
            header = "Invalid Pincode"
        else:
            # No errors - this shouldn't happen, but handle gracefully
            logger.warning("[SECTIONED_RFQ] _display_delivery_validation_error called with no errors")
            return await self._display_delivery_confirmation(user, session, delivery_data)

        # Send message with Modify/Restart buttons
        buttons_config = [
            {"id": "modify_date_location", "title": "Modify"},
            {"id": "restart_rfq", "title": "Restart"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            header,
            footer="",
            session_id=session
        )

        # Set awaiting modification so user can correct the errors
        WorkflowManager.set_awaiting_section_modification(session, "date_location", True)

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "validation_error"}

    async def _display_invalid_pincode_message(self, user: User, session: ConversationSession,
                                             delivery_data: Dict, pincode: str) -> Dict[str, Any]:
        """Display error message when pincode lookup fails or pincode is invalid."""
        pincode_error = f"Could not find location for pincode {pincode}. Please provide a valid 6-digit Indian pincode."
        return await self._display_delivery_validation_error(
            user, session, delivery_data, pincode_error=pincode_error
        )

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

        # Check if we have items from initial extraction or previous entry.
        # Filter here too: a session stored before this guard existed can still hold a
        # sentinel entry, and it must not resurface as an item line.
        stored_items = WorkflowManager.get_section_data(session, "items")
        items_data = self._usable_items(stored_items)
        if stored_items and len(items_data) != len(stored_items):
            WorkflowManager.update_section_data(session, "items", items_data)

        # If we already have items, check if user sent data in structured format
        # This handles the case where user directly copies and modifies the confirmation format
        if items_data and len(items_data) > 0:
            # Check if message looks like it's attempting the structured format
            # Look for format keywords to detect modification attempts
            message_lower = message.lower()
            if "item " in message_lower and ("qty:" in message_lower or "quantity:" in message_lower):
                return await self._process_items_modification_direct(user, session, message)

        # Check if data is from Excel upload (allows more than MAX_TEXT_INPUT_ITEMS)
        is_from_excel = WorkflowManager.is_sectioned_rfq_from_excel(session)

        if not items_data or len(items_data) == 0:
            # Need to extract items - ONE TIME entity extraction

            entity_context = self._build_entity_context(session)
            entity_result = await self.entity_service.extract_entities(
                message, context=entity_context, workflow_type="buy_something"
            )

            items_data = self._usable_items(entity_result.get("products"))

            # Enforce item limit for text input (not Excel)
            if not is_from_excel and len(items_data) > MAX_TEXT_INPUT_ITEMS:
                return await self._display_item_limit_exceeded(user, session, len(items_data))

            WorkflowManager.update_section_data(session, "items", items_data)
        elif message and message.strip():
            # We have existing items AND user provided a message - user might be providing missing fields
            # Re-extract and merge with existing items

            # Build context with existing items
            entity_context = self._build_entity_context_with_items(session, items_data)
            entity_result = await self.entity_service.extract_entities(
                message, context=entity_context, workflow_type="buy_something"
            )

            # Merge new extraction with existing items
            new_items = self._usable_items(entity_result.get("products"))
            if new_items:
                # Enforce item limit for text input (not Excel)
                if not is_from_excel and len(new_items) > MAX_TEXT_INPUT_ITEMS:
                    return await self._display_item_limit_exceeded(user, session, len(new_items))

                # Merge new extraction with existing items while preserving existing fields
                merged_items = []
                for idx, item in enumerate(new_items):
                    existing_item = items_data[idx] if idx < len(items_data) else {}
                    merged_item = existing_item.copy()
                    for k, v in item.items():
                        if v is not None and str(v).strip():
                            merged_item[k] = v
                    merged_items.append(merged_item)

                items_data = merged_items
                WorkflowManager.update_section_data(session, "items", items_data)
        else:
            # We have existing items and message is empty - just use existing items
            logger.info(f"[SECTIONED_RFQ] Using existing {len(items_data)} items from initial message")

        # Check if we have at least one item
        if not items_data or len(items_data) == 0:
            # No items yet, ask user
            msg= (
                "Please share your RFQ items in this format:\n"
                "*Qty [Number] - [Item Name] - UoM [Unit] - [Brand/Specs/Other Details]*\n\n"
                "Examples:\n"
                "• Qty 20 - Cement Bag - UoM 5Kgs - Ambuja White Cement\n"
                "• Qty 15 - Laptop - UoM pieces - HP, 10\" display, i7 processor, blue\n"
                "• Qty 25 - Cable - UoM meters - 10 mm thickness\n\n"
                "You can leave Brand or other Details empty if you don't have them.\n\n"
                "Alternatively, you can bulk upload an Excel file (Max 49 items) with all the above fields."
            )


            buttons_config = [
                {"id": "restart_rfq", "title": "Restart"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                msg,
                buttons_config,
                "Items Required",
                footer="",
                session_id=session
            )
            return {"status": "awaiting_items"}

        # Check if all items are complete (have mandatory fields)
        incomplete_items = self._get_incomplete_items(items_data)
        if incomplete_items:
            # Some items are missing mandatory fields - show format with missing field indicators
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
                footer="",
                session_id=session
            )

            # Save session to persist retry count
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "format_error", "retry_count": retry_count}

        # Step 3: Format valid - DIRECTLY replace entire items array (no AI processing)
        items_data = parsed_result["items"]

        # Enforce item limit for text input (not Excel)
        is_from_excel = WorkflowManager.is_sectioned_rfq_from_excel(session)
        if not is_from_excel and len(items_data) > MAX_TEXT_INPUT_ITEMS:
            return await self._display_item_limit_exceeded(user, session, len(items_data))

        WorkflowManager.update_section_data(session, "items", items_data)
        WorkflowManager.reset_section_retry(session, "items")
        WorkflowManager.set_awaiting_section_modification(session, "items", False)

        # Save session immediately after update to persist changes
        await self.session_manager.save_session(session, persist_to_db=False)

        # Step 4: Re-display for confirmation
        return await self._display_items_confirmation(user, session, items_data)

    async def _display_items_confirmation(self, user: User, session: ConversationSession,
                                         items_data: List) -> Dict[str, Any]:
        """Display items with Confirm/Modify buttons."""
        display_text = sectioned_rfq_format_parser.generate_items_display(items_data)

        message = f"RFQ Items ({len(items_data)}):\n\n{display_text}"

        # Check if data is from Excel upload
        is_from_excel = WorkflowManager.is_sectioned_rfq_from_excel(session)

        # Build buttons config - hide Modify button if data is from Excel upload
        buttons_config = [
            {"id": "confirm_items", "title": "Confirm"}
        ]

        # Only add Modify button if data is NOT from Excel upload
        if not is_from_excel:
            buttons_config.append({"id": "modify_items", "title": "Modify"})

        buttons_config.append({"id": "restart_rfq", "title": "Restart"})
        
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Confirmation Required",
            footer="",  # Empty footer to prevent accidental exit triggers
            session_id=session
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

        copy_paste_instruction = (
            "I'm sorry I do not have all the details to proceed. Can you provide some of the missing details. "
            "In order to do please copy and paste the text provided below this line and then update your information in the correct format.\n\n"
            "————————————————————————"
        )
        message = f"{missing_label} Required\n\n{copy_paste_instruction}\n{display_text}"

        # Check if data is from Excel upload
        is_from_excel = WorkflowManager.is_sectioned_rfq_from_excel(session)

        # Build buttons config - hide Modify button if data is from Excel upload
        buttons_config = []

        # Only add Modify button if data is NOT from Excel upload
        if not is_from_excel:
            buttons_config.append({"id": "modify_items", "title": "Modify"})

        buttons_config.append({"id": "restart_rfq", "title": "Restart"})

        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons_config,
            "Missing Some Details",
            footer="",
            session_id=session
        )

        # Set awaiting modification so user can fill in the format directly
        WorkflowManager.set_awaiting_section_modification(session, "items", True)

        # Save session
        await self.session_manager.save_session(session, persist_to_db=False)

        return {"status": "awaiting_missing_item_fields"}

    async def _display_item_limit_exceeded(self, user: User, session: ConversationSession,
                                            item_count: int) -> Dict[str, Any]:
        """Display error and cancel workflow when text input exceeds maximum item limit."""
        logger.warning(f"[SECTIONED_RFQ] Item limit exceeded: {item_count} items (max {MAX_TEXT_INPUT_ITEMS}) - cancelling workflow")

        message = (
            f"You've provided {item_count} items. Each RFQ can have a maximum of {MAX_TEXT_INPUT_ITEMS} items.\n\n"
            f"For RFQs with more items, please upload an Excel file.\n\n"
            f"What would you like to do next?"
        )

        # Cancel the workflow
        await self.cancel_service._clear_workflow_state(session)

        # Determine user type for appropriate menu buttons
        user_type = "buyer" if user.role else "seller"

        # Send message with menu buttons
        await self.cancel_service._send_cancellation_message(user.phone_number, user_type, custom_message=message)

        return {"status": "item_limit_exceeded_cancelled", "item_count": item_count}

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
                "You may upload files in the following formats: *JPEG, PNG, PDF, Excel, DOCX, or CSV*\n"
                "• *Maximum 4 attachments*\n"
                "• *Each file up to 1 MB*\n\n"
                "Please upload one file at a time.\n\n"
                "If yes, please upload the files now — or click *Continue* to skip this step and proceed.\n\n"
            )

            # Send message with Continue button
            buttons_config = [
                {"id": "continue_rfq", "title": "Continue"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                optional_message,
                buttons_config,
                "Optional Information",
                session_id=session
            )

            return {"status": "awaiting_attachments_decision"}

        # User responded - delegate to existing handler
        result = await self.confirmation_handler.handle_optional_fields_response(user, session, message)

        # Check if we moved to confirmation phase (pending_combined_rfq was set)
        if session.workflow_state.get("pending_combined_rfq"):
            # Optional fields phase complete - move to final confirmation section
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
        if button_id.startswith("confirm_") and button_id != "confirm_cancel":
            section = button_id.replace("confirm_", "")
            return await self._handle_section_confirm(user, session, section)
        elif button_id.startswith("modify_"):
            section = button_id.replace("modify_", "")
            return await self._handle_section_modify(user, session, section)
        elif button_id == "restart_rfq" or button_id == "confirm_cancel" or button_id == "decline_cancel":
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
            footer="",
            session_id=session
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
            message="restart",
            user=user
        )


        # If user declined cancellation, re-display the confirmation screen
        if cancel_result.get("status") == "cancelled_aborted":
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
            await self.whatsapp_service.send_message(user.phone_number, msg, session_id=session)
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
        await self.whatsapp_service.send_message(user.phone_number, msg, session_id=session)

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

        await self.whatsapp_service.send_message(user.phone_number, message, session_id=session)

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
            await self.whatsapp_service.send_message(user.phone_number, msg, session_id=session)
            return {"status": "awaiting_restart_confirmation"}

        if confirmed:
            # Reset all sectioned RFQ state
            WorkflowManager.reset_sectioned_rfq(session)
            WorkflowManager.set_sectioned_rfq_section(session, "date_location")
            await self.session_manager.save_session(session, persist_to_db=False)

            # Restart from beginning
            msg = "Workflow restarted. Let's start by the Delivery Date and Delivery Pincode.\n\n"
            msg += "Note: If you are expecting the delivery at different locations or on different dates, "
            msg += "we request you create separate RFQs."
            await self.whatsapp_service.send_message(user.phone_number, msg, session_id=session)

            return {"status": "workflow_restarted"}
        else:
            # Continue where they left off
            WorkflowManager.set_sectioned_rfq_pending_restart(session, False)
            await self.session_manager.save_session(session, persist_to_db=False)

            msg = "Continuing with your request..."
            await self.whatsapp_service.send_message(user.phone_number, msg, session_id=session)

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

        # Include existing section items in incomplete_products so entity service retains items
        items_data = WorkflowManager.get_section_data(session, "items")
        usable_items = self._usable_items(items_data) if items_data else []
        if usable_items:
            incomplete_products = []
            for item in usable_items:
                incomplete_products.append({
                    "entities": item,
                    "missing_fields": []
                })
            workflow_state["incomplete_products"] = incomplete_products

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

    async def _autofill_location_from_pincode(self, delivery_data: Dict) -> Dict[str, Any]:
        """
        Auto-fill city and state from pincode using existing pincode lookup.

        IMPORTANT: Pincode is the authoritative source for city/state.
        User-provided city/state values are OVERRIDDEN by pincode lookup results
        to ensure data accuracy (e.g., user says "Mumbai 411005" but 411005 is Pune).

        Returns:
            Dict with:
            - delivery_data: The updated delivery data (may have empty city/state if invalid)
            - is_valid: Boolean indicating if pincode is valid
            - error: Error message if invalid, None otherwise
        """
        pincode = delivery_data.get("pincode")
        if not pincode:
            return {
                "delivery_data": delivery_data,
                "is_valid": True,  # No pincode provided is not an error here
                "error": None
            }

        try:
            # Validate pincode format
            clean_pincode = str(pincode).strip()
            if not clean_pincode.isdigit() or len(clean_pincode) != 6:
                logger.warning(f"[SECTIONED_RFQ] Invalid pincode format: {pincode}")
                return {
                    "delivery_data": delivery_data,
                    "is_valid": False,
                    "error": f"Invalid pincode format: {pincode}. Please provide a valid 6-digit pincode."
                }

            # Lookup location
            location_data = await get_location_from_pincode_async(clean_pincode)
            if not location_data:
                location_data = get_fallback_location(clean_pincode)

            if location_data:
                # Always override city/state with pincode lookup results (pincode is authoritative)
                if location_data.get("city"):
                    delivery_data["city"] = location_data["city"]

                if location_data.get("state"):
                    delivery_data["state"] = location_data["state"]

                return {
                    "delivery_data": delivery_data,
                    "is_valid": True,
                    "error": None
                }
            else:
                logger.warning(f"[SECTIONED_RFQ] No location data found for pincode: {pincode}")
                return {
                    "delivery_data": delivery_data,
                    "is_valid": False,
                    "error": f"Could not find location for pincode {pincode}. Please provide a valid Indian pincode."
                }

        except Exception as e:
            logger.error(f"[SECTIONED_RFQ] Error auto-filling location from pincode: {e}")
            return {
                "delivery_data": delivery_data,
                "is_valid": False,
                "error": f"Error validating pincode {pincode}. Please try again."
            }

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
            if not location_data:
                location_data = get_fallback_location(clean_pincode)
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

            # Check for BFS pre-populated products (from BFS -> Raise RFQ flow)
            if not items_data or len(items_data) == 0:
                bfs_products = session.workflow_state.get("bfs_rfq_products", []) if session.workflow_state else []
                if bfs_products:
                    logger.info(f"[SECTIONED_RFQ] Pre-populating items from BFS search: {bfs_products}")
                    # Convert BFS product descriptions to items format
                    items_data = []
                    for product in bfs_products:
                        items_data.append({
                            "description": product,
                            "quantity": 1,  # Default quantity, user can modify
                            "brand": "",
                            "unit_of_measures": "unit(s)"
                        })
                    # Store in section data
                    WorkflowManager.update_section_data(session, "items", items_data)
                    # Clear BFS products from workflow state
                    session.workflow_state.pop("bfs_rfq_products", None)
                    await self.session_manager.save_session(session, persist_to_db=False)

            if items_data and len(items_data) > 0:
                logger.info(f"[SECTIONED_RFQ] Items already exist from initial message ({len(items_data)} items), displaying confirmation")
                # Items already exist, go directly to items section handler which will display confirmation
                return await self._handle_items_section(user, session, "", [])


            msg = (
                "Please share your RFQ items in this format:\n"
                "*Qty [Number] - [Item Name] - UoM [Unit] - [Brand/Specs/Other Details]*\n\n"
                "Examples:\n"
                "• Qty 20 - Cement Bag - UoM 5Kgs - Ambuja White Cement\n"
                "• Qty 15 - Laptop - UoM pieces - HP, 10\" display, i7 processor, blue\n"
                "• Qty 25 - Cable - UoM meters - 10 mm thickness\n\n"
                "You can leave Brand or other Details empty if you don't have them.\n\n"
                "Alternatively, you can bulk upload an Excel file (Max 49 items) with all the above fields."
            )

            await self.whatsapp_service.send_message(user.phone_number, msg, session_id=session)
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

        # Determine user type for appropriate menu buttons
        user_type = "buyer" if user.role else "seller"

        # Send cancellation message with appropriate menu buttons
        msg = f"Maximum retry attempts reached for {section_name.replace('_', ' ')} section.\n"
        msg += f"The workflow has been cancelled.\n\n"
        msg += f"What would you like to do next?"

        # Use cancel_service's method to send message with appropriate buttons
        await self.cancel_service._send_cancellation_message(user.phone_number, user_type)

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
