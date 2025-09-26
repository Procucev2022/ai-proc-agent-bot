"""
RFQ Status Service for handling RFQ status inquiries.
This service can be used by both chat_service and seller_service.
"""

import logging
from typing import Dict, Any
from app.models import User, ConversationSession
from app.services.rfq_service import RFQService
from app.services.whatsapp_service import WhatsAppService
from app.services.session_management_service import SessionManagementService
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.database import DatabaseManager


logger = logging.getLogger(__name__)


class RFQStatusService:
    """Dedicated service for handling RFQ status inquiries."""

    def __init__(self):
        self.rfq_service = RFQService()
        self.whatsapp_service = WhatsAppService()
        self.db_manager = DatabaseManager()
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        # Initialize extracted services
        self.session_manager = SessionManagementService(
            self.db_manager, self.whatsapp_service,
            self.chat_summary_service, self.daily_summary_service
        )

    async def handle_rfq_status_inquiry(self, user: User,  message: str,session: ConversationSession) -> Dict[str, Any]:
        """Handle RFQ status inquiry requests."""
        try:
            result = await self.rfq_service.process_rfq_status_request(user=user, message=message)

            # Step: Send WhatsApp message with button for status details
            response_message = result["response_message"]
            
            # Check if we have RFQ statuses to show button
            if result.get("rfq_statuses") and self.rfq_service.settings.rfq_followup_note:
                # Send message with URL button for better UX using existing configurable buttons function
                button_config = [{
                    "id": "view_details",
                    "title": "Click to view details",
                    "url": self.rfq_service.settings.rfq_followup_note
                }]
                
                button_message = f"{response_message}\n\nUse the button for detailed status information."
                
                await self.whatsapp_service.send_configurable_buttons(
                    recipient_id=user.phone_number,
                    body=button_message,
                    buttons_config=button_config,
                    footer="Tap the button to open details page"
                )
            else:
                # Fallback to regular text message if no button needed
                await self.whatsapp_service.send_message(user.phone_number, response_message)

            # Update session workflow type for tracking
            session.workflow_type = "rfq_status_check"
            await self.session_manager.save_session(session, "rfq_status_check")

            return {
                "status": result.get("status"),
                "message": result.get("response_message"),
                "rfq_ids": result.get("rfq_ids"),
                "rfq_statuses": result.get("rfq_statuses")
            }

        except Exception as e:
            logger.error(f"Error handling RFQ status inquiry: {e}")
            return {"status": "error", "error": str(e)}
