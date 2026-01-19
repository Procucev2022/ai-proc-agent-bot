"""
Image Message Processor.

Handles image/document message processing for RFQ attachments.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any
from datetime import datetime
from app.models import User, ConversationSession
from app.schemas.rfq import RFQValidationSchema
from app.services.whatsapp_service import WhatsAppService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.attachment_helpers import AttachmentHelpers

logger = logging.getLogger(__name__)


class ImageMessageProcessor:
    """Processes image and document messages."""

    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers, session_manager=None, **kwargs):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.session_manager = session_manager
        # Ignore other kwargs to handle factory initialization
    
    async def process_image_message(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
        """Process image/document message for RFQ attachment using contextual responses."""
        try:
            # Check if user needs registration
            if not user.is_registered:
                return await self._handle_registration_required(user)
            
            # Extract image information based on ICS V3.1 documentation format
            # ICS sends flat JSON structure: {"mime_type": "...", "id": "...", "filename": "..."}
            # NOT nested like {"image": {"link": "...", ...}}

            if isinstance(content, dict):
                # Check if this is ICS webhook format (has "id" and "mime_type")
                if "id" in content and "mime_type" in content:
                    # ICS webhook format - construct download URL from media ID
                    media_id = content.get("id")
                    mime_type = content.get("mime_type")
                    filename = content.get("filename")  # Only present for DOCUMENT type
                    caption = content.get("caption")  # Caption text sent with the attachment

                    # Auto-generate filename from MIME type if not provided
                    if not filename:
                        extension = mime_type.split('/')[-1] if mime_type else 'jpg'
                        filename = f"attachment_{int(datetime.now().timestamp())}.{extension}"

                    # Construct ICS media download URL
                    file_url = f"https://download.sendmsg.in/whatsapp-mediadownloader/{media_id}"
                    base64_data = None

                    logger.info(f"Processing ICS media - ID: {media_id}, Type: {mime_type}, URL: {file_url}")
                    if caption:
                        logger.info(f"Attachment has caption: {caption}")

                # Legacy format support (old nested structure or web UI)
                elif "image" in content or "document" in content:
                    image_info = content.get("image", {}) or content.get("document", {})
                    file_url = image_info.get("link")  # Old WhatsApp format
                    filename = image_info.get("filename")
                    mime_type = image_info.get("mime_type")
                    base64_data = image_info.get("data")  # Web UI format

                    logger.info(f"Processing legacy format - URL: {file_url}")

                # Web UI direct format
                elif "data" in content:
                    base64_data = content.get("data")
                    filename = content.get("filename")
                    mime_type = content.get("mime_type")
                    file_url = None

                    logger.info(f"Processing web UI format - Filename: {filename}")
                else:
                    file_url = None
                    filename = None
                    mime_type = None
                    base64_data = None
            else:
                file_url = None
                filename = None
                mime_type = None
                base64_data = None
            
            # Handle web UI format (base64 data) vs WhatsApp format (file URL)
            if base64_data:
                # Web UI format - create attachment directly from base64 data
                download_result = {
                    "success": True,
                    "attachment": {
                        "file_name": filename or "uploaded_image.jpg",
                        "file_type": mime_type or "image/jpeg",
                        "file_content": base64_data,
                        "uploaded_at": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z')
                    }
                }
            elif file_url:
                # WhatsApp format - download from URL and encode
                download_result = await AttachmentHelpers.download_and_encode_attachment(
                    file_url, filename, mime_type
                )
            else:
                return await self._handle_no_file_data(user)
            
            if not download_result["success"]:
                return await self._handle_download_error(user, download_result["error"])
            
            # Add attachment to session using helper
            add_result = AttachmentHelpers.add_attachment_to_session(
                session, download_result["attachment"]
            )

            # Check if limit was exceeded or other error occurred
            if not add_result["success"]:
                error_message = add_result.get("error", "Failed to add attachment")

                # If max limit reached and user is in confirmation phase, show confirmation with error
                is_max_limit_error = "Maximum" in error_message and "attachments" in error_message
                has_pending_confirmation = (
                    session.workflow_state.get("pending_rfq") or
                    session.workflow_state.get("pending_combined_rfq")
                )

                if is_max_limit_error and has_pending_confirmation:
                    # Regenerate confirmation with error message included
                    return await self._regenerate_confirmation_with_error(user, session, error_message)

                await self.whatsapp_service.send_message(user.phone_number, error_message)
                return {"status": "error", "response": "attachment_add_failed"}

            filename = download_result["attachment"]["file_name"]

            # Auto-approve the attachment
            AttachmentHelpers.approve_pending_attachment(session)

            # Store caption as pending remark if provided
            if isinstance(content, dict) and "caption" in content:
                caption = content.get("caption")
                if caption:
                    session.workflow_state["attachment_caption"] = caption
                    logger.info(f"Stored attachment caption as pending remark: {caption}")

            # Get attachment count for reference
            count = add_result.get("count", 1)
            max_count = add_result.get("max", 4)

            # Return appropriate status based on current workflow state
            return await self._determine_next_step(user, session, filename)
            
        except Exception as e:
            logger.error(f"Error processing image message: {e}")
            return await self._handle_processing_error(user, str(e))
    
    async def _handle_registration_required(self, user: User) -> Dict[str, Any]:
        """Handle case where user needs registration."""
        registration_context = {'workflow_type': 'image_upload', 'conversation_stage': 'registration_required'}
        registration_response = await self.response_helpers.generate_contextual_response(
            registration_context,
            ["Please complete your registration first before uploading files."],
            "registration_required"
        )
        await self.whatsapp_service.send_message(user.phone_number, registration_response)
        return {"status": "handled", "response": "registration_required"}
    
    async def _handle_no_file_data(self, user: User) -> Dict[str, Any]:
        """Handle case where no file data is found."""
        context = {
            "conversation_stage": "attachment_error",
            "error_type": "no_file_data",
            "user_message": "User sent an image but no file data found"
        }
        response = await self.response_helpers.generate_contextual_response(
            context,
            ["I couldn't access your image. Please try sending it again."],
            "attachment_error"
        )
        await self.whatsapp_service.send_message(user.phone_number, response)
        return {"status": "error", "response": "no_file_data"}
    
    async def _handle_download_error(self, user: User, error: str) -> Dict[str, Any]:
        """Handle download error."""
        context = {
            "conversation_stage": "attachment_error",
            "error_type": "download_failed",
            "error_message": error,
            "user_message": f"Failed to download image: {error}"
        }
        response = await self.response_helpers.generate_contextual_response(
            context,
            [f"I couldn't download your image: {error}. Please try again."],
            "attachment_error"
        )
        await self.whatsapp_service.send_message(user.phone_number, response)
        return {"status": "error", "response": error}
    
    async def _handle_save_error(self, user: User) -> Dict[str, Any]:
        """Handle attachment save error."""
        context = {
            "conversation_stage": "attachment_error",
            "error_type": "save_failed",
            "user_message": "Failed to save attachment"
        }
        response = await self.response_helpers.generate_contextual_response(
            context,
            ["I couldn't save your attachment. Please try again."],
            "attachment_error"
        )
        await self.whatsapp_service.send_message(user.phone_number, response)
        return {"status": "error", "response": "failed_to_save_attachment"}
    
    async def _handle_processing_error(self, user: User, error: str) -> Dict[str, Any]:
        """Handle general processing error."""
        context = {
            "conversation_stage": "attachment_error",
            "error_type": "processing_failed",
            "error_message": error,
            "user_message": f"Error processing image: {error}"
        }
        response = await self.response_helpers.generate_contextual_response(
            context,
            ["I encountered an error processing your image. Please try again."],
            "attachment_error"
        )
        await self.whatsapp_service.send_message(user.phone_number, response)
        return {"status": "error", "response": error}
    
    async def _determine_next_step(self, user: User, session: ConversationSession, filename: str) -> Dict[str, Any]:
        """Determine next step based on current workflow state."""
        # Check current state and continue with appropriate flow
        has_pending_optional = bool(session.workflow_state.get("pending_optional_rfq") or session.workflow_state.get("pending_optional_combined_rfq"))
        has_pending_confirmations = bool(session.workflow_state.get("pending_combined_rfq") or session.workflow_state.get("pending_rfq"))
        has_incomplete_products = bool(session.workflow_state.get("incomplete_products"))

        logger.info(f"_determine_next_step: has_pending_optional={has_pending_optional}, has_pending_confirmations={has_pending_confirmations}, has_incomplete_products={has_incomplete_products}")

        # If already in confirmation phase, send updated confirmation with new attachment count
        if has_pending_confirmations:
            # Already in confirmation phase - regenerate confirmation showing all attachments
            # This ensures user can still see Confirm/Modify buttons after adding more attachments
            logger.info("Regenerating confirmation with updated attachment count")
            return await self._regenerate_existing_confirmation(user, session, filename)
        elif has_pending_optional:
            # We're in optional questions phase - proceed to confirmation
            return await self._proceed_from_optional_to_confirmation(user, session, filename)
        elif has_incomplete_products:
            # We have incomplete products - continue with clarification
            return {"status": "handled", "response": "attachment_added_continue_clarification"}
        else:
            # Unknown state - just acknowledge
            return {"status": "handled", "response": "attachment_added"}
    
    async def _regenerate_existing_confirmation(self, user: User, session: ConversationSession, filename: str) -> Dict[str, Any]:
        """Regenerate confirmation message with updated attachments for existing pending_rfq."""
        from app.services.helpers.chat_service_helpers import ChatServiceHelpers

        # Get existing pending RFQ data
        if session.workflow_state.get("pending_rfq"):
            product_info = session.workflow_state["pending_rfq"]
            entities = product_info["entities"]

            # Merge latest attachments from extracted_entities
            if session.workflow_state.get("extracted_entities") and session.workflow_state["extracted_entities"]:
                extracted_attachments = session.workflow_state["extracted_entities"][0].get("attachments", [])
                if extracted_attachments:
                    entities["attachments"] = extracted_attachments
                    logger.info(f"Merged {len(extracted_attachments)} attachments for regenerated confirmation")

            # Apply attachment caption as remarks if present
            attachment_caption = session.workflow_state.get("attachment_caption")
            if attachment_caption and not entities.get("remarks"):
                logger.info(f"Applying attachment caption to regenerated confirmation: {attachment_caption}")
                entities["remarks"] = attachment_caption
                # Clear the caption after applying
                del session.workflow_state["attachment_caption"]

            # Rebuild RFQ schema with updated attachments
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(entities, None)

            # Generate updated confirmation message
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                rfq_schema,
                {
                    "user_message": f"User added attachment {filename}",
                    "extracted_entities": entities
                },
                []
            )

            # Add remaining attachments message if user has uploaded some
            extracted_entities = session.workflow_state.get("extracted_entities", [])
            current_attachments = extracted_entities[0].get("attachments", []) if extracted_entities else []
            attachment_count = len(current_attachments)
            if attachment_count > 0:
                remaining_slots = AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ - attachment_count
                if remaining_slots > 0:
                    attachment_word = "document" if remaining_slots == 1 else "documents"
                    summary_response += f"\n\nYou can still upload {remaining_slots} more {attachment_word} if needed."

            # Send confirmation message with buttons
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Add or Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                summary_response,
                buttons_config,
                "Confirmation Required",
                session_id=session
            )

            return {"status": "handled", "response": "confirmation_regenerated"}

        elif session.workflow_state.get("pending_combined_rfq"):
            # Handle combined RFQ case
            combined_data = session.workflow_state["pending_combined_rfq"]

            # Apply attachment caption as remarks to all products if present
            attachment_caption = session.workflow_state.get("attachment_caption")
            if attachment_caption:
                logger.info(f"Applying attachment caption to regenerated combined RFQ: {attachment_caption}")
                for product in combined_data.get("products", []):
                    entities = product.get("entities", {})
                    if not entities.get("remarks"):
                        entities["remarks"] = attachment_caption
                # Update the combined schema with the new remarks
                if "remarks" in combined_data["combined_schema"] and not combined_data["combined_schema"]["remarks"]:
                    combined_data["combined_schema"]["remarks"] = attachment_caption
                # Clear the caption after applying
                del session.workflow_state["attachment_caption"]

            combined_schema = RFQValidationSchema(**combined_data["combined_schema"])

            # Generate updated confirmation
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                combined_schema,
                {
                    "user_message": f"User added attachment {filename}",
                    "extracted_entities": [prod["entities"] for prod in combined_data["products"]],
                    "total_products": len(combined_data["products"])
                },
                []
            )

            # Add remaining attachments message if user has uploaded some
            extracted_entities = session.workflow_state.get("extracted_entities", [])
            current_attachments = extracted_entities[0].get("attachments", []) if extracted_entities else []
            attachment_count = len(current_attachments)
            if attachment_count > 0:
                remaining_slots = AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ - attachment_count
                if remaining_slots > 0:
                    attachment_word = "document" if remaining_slots == 1 else "documents"
                    summary_response += f"\n\nYou can still upload {remaining_slots} more {attachment_word} if needed."

            # Send confirmation message with buttons
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Add or Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                summary_response,
                buttons_config,
                "Confirmation Required",
                session_id=session
            )

            return {"status": "handled", "response": "confirmation_regenerated"}

        return {"status": "error", "response": "no_pending_confirmation_found"}

    async def _proceed_from_optional_to_confirmation(self, user: User, session: ConversationSession, filename: str) -> Dict[str, Any]:
        """Proceed from optional fields phase to confirmation."""
        from app.services.handlers.confirmation_handler import ConfirmationHandler

        # Delegate to confirmation handler to avoid duplicating button logic
        confirmation_handler = ConfirmationHandler(self.whatsapp_service, self.response_helpers, session_manager=self.session_manager)
        result = await confirmation_handler._proceed_to_confirmation_from_optional(
            user,
            session,
            f"User added attachment {filename}"
        )

        return {"status": "handled", "response": "attachment_added_proceeded_to_confirmation"}

    async def _regenerate_confirmation_with_error(self, user: User, session: ConversationSession, error_message: str) -> Dict[str, Any]:
        """Regenerate confirmation message with error when max attachments reached."""
        from app.services.helpers.chat_service_helpers import ChatServiceHelpers

        if session.workflow_state.get("pending_rfq"):
            # Handle single RFQ case
            product_info = session.workflow_state["pending_rfq"]
            entities = product_info["entities"]

            # Rebuild RFQ schema
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(entities, None)

            # Generate confirmation message
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                rfq_schema,
                {
                    "user_message": "User attempted to add attachment but limit reached",
                    "extracted_entities": entities
                },
                []
            )

            # Add error message at the end
            summary_response += f"\n\n{error_message}"

            # Send confirmation message with buttons
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Add or Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                summary_response,
                buttons_config,
                "Confirmation Required",
                session_id=session
            )

            return {"status": "handled", "response": "confirmation_with_error"}

        elif session.workflow_state.get("pending_combined_rfq"):
            # Handle combined RFQ case
            combined_data = session.workflow_state["pending_combined_rfq"]
            combined_schema = RFQValidationSchema(**combined_data["combined_schema"])

            # Generate confirmation message
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                combined_schema,
                {
                    "user_message": "User attempted to add attachment but limit reached",
                    "extracted_entities": [prod["entities"] for prod in combined_data["products"]],
                    "total_products": len(combined_data["products"])
                },
                []
            )

            # Add error message at the end
            summary_response += f"\n\n{error_message}"

            # Send confirmation message with buttons
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Add or Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                summary_response,
                buttons_config,
                "Confirmation Required",
                session_id=session
            )

            return {"status": "handled", "response": "confirmation_with_error"}

        # Fallback - just send error message
        await self.whatsapp_service.send_message(user.phone_number, error_message)
        return {"status": "error", "response": "no_pending_confirmation_found"}