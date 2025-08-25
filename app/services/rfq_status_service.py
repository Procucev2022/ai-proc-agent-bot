"""
RFQ Status Service for handling RFQ status inquiries.
This service can be used by both chat_service and seller_service.
"""

import logging
from typing import Dict, Any
from app.models import User
from app.services.rfq_service import RFQService
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)


class RFQStatusService:
    """Dedicated service for handling RFQ status inquiries."""

    def __init__(self):
        self.rfq_service = RFQService()
        self.whatsapp_service = WhatsAppService()

    async def handle_rfq_status_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        """Handle RFQ status inquiry requests."""
        try:
            result = await self.rfq_service.process_rfq_status_request(user=user, message=message)

            # Step: Send WhatsApp message
            await self.whatsapp_service.send_message(user.phone_number, result["response_message"])

            return {
                "status": result.get("status"),
                "rfq_ids": result.get("rfq_ids"),
                "rfq_statuses": result.get("rfq_statuses")
            }

        except Exception as e:
            logger.error(f"Error handling RFQ status inquiry: {e}")
            return {"status": "error", "error": str(e)}
