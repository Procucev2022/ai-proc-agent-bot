"""
RFQ Status Service for handling RFQ status inquiries.
This service can be used by both chat_service and seller_service.
"""

import logging
from typing import Dict, Any, List
from app.models import WorkflowType, User, ConversationSession
from app.schemas.user import UserRole
from app.services.rfq_service import RFQService
from app.services.workflow_manager import WorkflowManager
from app.services.whatsapp_service import WhatsAppService
from app.services.session_management_service import SessionManagementService
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.database import DatabaseManager


logger = logging.getLogger(__name__)


class RFQStatusService:
    """Dedicated service for handling RFQ status inquiries."""

    def __init__(self, whatsapp_service: WhatsAppService = None,
                 session_manager: SessionManagementService = None,
                 db_session=None):
        """
        Initialize RFQStatusService.

        Args:
            whatsapp_service: WhatsApp service instance (optional).
            session_manager: Session management service instance (optional).
            db_session: Database session (optional). If provided, will be passed to DatabaseManager.
        """
        self.rfq_service = RFQService()

        # Use provided whatsapp_service or create new instance as fallback
        self.whatsapp_service = whatsapp_service or WhatsAppService()

        self.db_manager = DatabaseManager(session=db_session)
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        
        # Use provided session_manager or create new instance as fallback
        if session_manager:
            self.session_manager = session_manager
        else:
            self.session_manager = SessionManagementService(
                self.db_manager, self.whatsapp_service,
                self.chat_summary_service, self.daily_summary_service
            )

    def _get_role_based_menu_options(self, user: User) -> List[Dict[str, str]]:
        """Get menu options based on user role."""
        try:
            if user.role == UserRole.BUYER:
                return [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "search_bfs", "title": "Search Stocks"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
            elif user.role == UserRole.SELLER:
                return [
                    {"id": "view_rfqs", "title": "Active RFQs"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
            else:
                return [
                    {"id": "get_support", "title": "Get Support Info"}
                ]
        except Exception as e:
            logger.error(f"Error getting role-based menu options: {e}")
            return []

    async def handle_rfq_status_inquiry(self, user: User,  message: str,session: ConversationSession) -> Dict[str, Any]:
        """Handle RFQ status inquiry requests."""
        try:
            result = await self.rfq_service.process_rfq_status_request(user=user, message=message)

            # Step: Send WhatsApp message with role-based menu options
            response_message = result["response_message"]
            
            # Get role-based menu options
            menu_buttons = self._get_role_based_menu_options(user)
            
            if menu_buttons:
                # Send message with role-based buttons
                await self.whatsapp_service.send_configurable_buttons(
                    recipient_id=user.phone_number,
                    body=response_message,
                    buttons_config=menu_buttons
                )
            else:
                # Send simple message if no buttons available
                await self.whatsapp_service.send_message(
                    recipient_id=user.phone_number,
                    message=response_message
                )

            # Update session workflow type for tracking
            WorkflowManager.set_workflow_type(session, WorkflowType.rfq_status_check, caller="rfq_status_service")
            await self.session_manager.save_session(session, WorkflowType.rfq_status_check)

            return {
                "status": result.get("status"),
                "message": result.get("response_message"),
                "rfq_ids": result.get("rfq_ids"),
                "rfq_statuses": result.get("rfq_statuses")
            }

        except Exception as e:
            logger.error(f"Error handling RFQ status inquiry: {e}")
            return {"status": "error", "error": str(e)}
