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
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers, **kwargs):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
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

                    # Auto-generate filename from MIME type if not provided
                    if not filename:
                        extension = mime_type.split('/')[-1] if mime_type else 'jpg'
                        filename = f"attachment_{int(datetime.now().timestamp())}.{extension}"

                    # Construct ICS media download URL
                    file_url = f"https://download.sendmsg.in/whatsapp-mediadownloader/{media_id}"
                    base64_data = None

                    logger.info(f"Processing ICS media - ID: {media_id}, Type: {mime_type}, URL: {file_url}")

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
            attachment_added = AttachmentHelpers.add_attachment_to_session(
                session, download_result["attachment"]
            )
            
            if not attachment_added:
                return await self._handle_save_error(user)
            
            filename = download_result["attachment"]["file_name"]
            
            # Auto-approve the attachment
            AttachmentHelpers.approve_pending_attachment(session)
            
            # Acknowledge the attachment
            await self.whatsapp_service.send_message(user.phone_number, f"Thanks! I've added your image '{filename}' to your RFQ.")
            
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
        
        if has_pending_optional:
            # We're in optional questions phase - proceed to confirmation
            return await self._proceed_from_optional_to_confirmation(user, session, filename)
        elif has_incomplete_products:
            # We have incomplete products - continue with clarification
            return {"status": "handled", "response": "attachment_added_continue_clarification"}
        elif has_pending_confirmations:
            # Already in confirmation phase - just acknowledge attachment
            return {"status": "handled", "response": "attachment_added_to_pending_confirmation"}
        else:
            # Unknown state - just acknowledge
            return {"status": "handled", "response": "attachment_added"}
    
    async def _proceed_from_optional_to_confirmation(self, user: User, session: ConversationSession, filename: str) -> Dict[str, Any]:
        """Proceed from optional fields phase to confirmation."""
        from app.services.helpers.chat_service_helpers import ChatServiceHelpers
        
        if session.workflow_state.get("pending_optional_rfq"):
            # Single product
            product_info = session.workflow_state["pending_optional_rfq"]
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"], None)
            
            # Generate confirmation message
            chat_summaries = []  # Could load from chat summary service if needed
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
                "user_message": f"User added attachment {filename}",
                "extracted_entities": product_info["entities"]
            }, chat_summaries)
            await self.whatsapp_service.send_message(user.phone_number, summary_response)
            
            # Move to confirmation state
            session.workflow_state["pending_rfq"] = product_info
            del session.workflow_state["pending_optional_rfq"]
            
        elif session.workflow_state.get("pending_optional_combined_rfq"):
            # Combined RFQ with multiple items
            combined_data = session.workflow_state["pending_optional_combined_rfq"]
            combined_schema = RFQValidationSchema(**combined_data["combined_schema"])
            
            chat_summaries = []
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                combined_schema,
                {"user_message": f"User added attachment {filename}"}, 
                chat_summaries
            )
            await self.whatsapp_service.send_message(user.phone_number, summary_response)
            
            # Move to confirmation state
            session.workflow_state["pending_combined_rfq"] = combined_data
            del session.workflow_state["pending_optional_combined_rfq"]
        
        return {"status": "handled", "response": "attachment_added_proceeded_to_confirmation"}