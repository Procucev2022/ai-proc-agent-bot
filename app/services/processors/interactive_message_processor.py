"""
Interactive Message Processor.

Handles interactive message processing (buttons, lists) for WhatsApp.
Extracted from ChatService to reduce complexity.
"""

import logging
import json
from typing import Dict, Any
from app.models import User, ConversationSession
from app.services.seller_notification_service import SellerNotificationService

logger = logging.getLogger(__name__)

# Portal URL for RFQ details
RFQ_PORTAL_BASE_URL = "https://p2pdevuiindia.azurewebsites.net"


class InteractiveMessageProcessor:
    """Processes interactive messages (buttons, lists)."""
    
    def __init__(self, whatsapp_service, **kwargs):
        self.whatsapp_service = whatsapp_service
    
    async def process_interactive_message(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
        """Process interactive message responses (buttons, lists)."""
        try:
            # Parse interactive content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {"type": "unknown", "content": content}
            
            message_type = content.get("type")
            
            if message_type == "button_reply":
                button_id = content.get("button_reply", {}).get("id")
                return await self._handle_button_response(user, session, button_id)
            elif message_type == "list_reply":
                list_id = content.get("list_reply", {}).get("id")
                return await self._handle_list_response(user, session, list_id)
            else:
                # Treat as regular text message - delegate to text processor
                text_content = str(content)
                return {"status": "delegate_to_text_processor", "content": text_content}
                
        except Exception as e:
            logger.error(f"Error processing interactive message: {e}")
            raise
    
    async def _handle_button_response(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle button interaction responses."""
        logger.info(f"Button response from {user.phone_number}: {button_id}")

        # Handle RFQ notification button responses from sellers
        if button_id.startswith(SellerNotificationService.BUTTON_CHECK_DETAILS):
            return await self._handle_rfq_check_details(user, button_id)
        elif button_id.startswith(SellerNotificationService.BUTTON_INTERESTED):
            return await self._handle_rfq_interested(user, button_id)

        return {"status": "button_handled", "button_id": button_id}

    async def _handle_rfq_check_details(self, user: User, button_id: str) -> Dict[str, Any]:
        """Handle 'Check Details' button click from seller RFQ notification."""
        # Extract RFQ ID from button_id (format: rfq_check_details_{rfq_id})
        rfq_id = button_id.replace(f"{SellerNotificationService.BUTTON_CHECK_DETAILS}_", "")
        logger.info(f"Seller {user.phone_number} clicked Check Details for RFQ {rfq_id}")

        # Send response with link to RFQ details
        rfq_details_url = f"{RFQ_PORTAL_BASE_URL}/rfq/{rfq_id}"
        message = f"For more details about this RFQ, please visit:\n{rfq_details_url}"

        await self.whatsapp_service.send_message(
            recipient_id=user.phone_number,
            message=message
        )

        return {
            "status": "rfq_check_details_handled",
            "rfq_id": rfq_id,
            "response_sent": True
        }

    async def _handle_rfq_interested(self, user: User, button_id: str) -> Dict[str, Any]:
        """Handle 'I'm Interested' button click from seller RFQ notification."""
        # Extract RFQ ID from button_id (format: rfq_interested_{rfq_id})
        rfq_id = button_id.replace(f"{SellerNotificationService.BUTTON_INTERESTED}_", "")
        logger.info(f"Seller {user.phone_number} expressed interest in RFQ {rfq_id}")

        # For now, send "Feature coming soon" message
        message = "Thank you for your interest! The Seller Intimation feature is coming soon. We will notify you when it's available."

        await self.whatsapp_service.send_message(
            recipient_id=user.phone_number,
            message=message
        )

        return {
            "status": "rfq_interested_handled",
            "rfq_id": rfq_id,
            "response_sent": True
        }
    
    async def _handle_list_response(self, user: User, session: ConversationSession, list_id: str) -> Dict[str, Any]:
        """Handle list selection responses."""
        logger.info(f"List response from {user.phone_number}: {list_id}")
        return {"status": "list_handled", "list_id": list_id}