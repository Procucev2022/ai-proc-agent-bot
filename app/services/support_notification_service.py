"""
Support Notification Service.

Handles all support team notifications throughout the application.
Integrates with EmailService to send template-based notifications.
Now deprecated in favor of GlobalErrorHandler for technical errors.
"""

import logging
from typing import Dict, Any

from app.services.email_service import EmailService
from app.services.global_error_handler import get_global_error_handler, ErrorContext

logger = logging.getLogger(__name__)

class SupportNotificationService:
    """Service for sending notifications to support team."""
    
    def __init__(self):
        self.email_service = EmailService()
    
    async def notify_registration_failed(self, full_name: str, email: str, contact_number: str, user_role: str = "buyer") -> Dict[str, Any]:
        """Notify support when user registration fails."""
        variables = {
            "user_email": email,
            "full_name": full_name,
            "email": email,
            "contact_number": contact_number
        }
        logger.info(f"Notifying support for registration failure: {email}")
        return await self.email_service.send_support_email("user_registration_failed", variables, user_role)
    
    async def notify_otp_validation_failed(self, full_name: str, email: str, contact_number: str, user_role: str = "buyer") -> Dict[str, Any]:
        """Notify support when OTP validation fails."""
        variables = {
            "user_email": email,
            "full_name": full_name,
            "email": email,
            "contact_number": contact_number
        }
        logger.info(f"Notifying support for OTP validation failure: {email}")
        return await self.email_service.send_support_email("otp_validation_failed", variables, user_role)
    
    async def notify_buyer_registration_not_approved(self, full_name: str, email: str, contact_number: str) -> Dict[str, Any]:
        """Notify support when buyer registration is not approved."""
        variables = {
            "user_email": email,
            "full_name": full_name,
            "email": email,
            "contact_number": contact_number
        }
        return await self.email_service.send_support_email("buyer_registration_not_approved", variables, "buyer")
    
    async def notify_seller_registration_declined(self, phone_number: str) -> Dict[str, Any]:
        """Notify support when seller declines registration."""
        variables = {"phone_number": phone_number}
        return await self.email_service.send_support_email("seller_registration_notification", variables, "seller")
    
    async def notify_rfq_categorization_issue(self, rfq_id: str) -> Dict[str, Any]:
        """Notify support when RFQ items are marked as 'Others'."""
        variables = {"rfq_id": rfq_id}
        return await self.email_service.send_support_email("rfq_item_categorization", variables, "buyer")
    
    async def notify_non_standard_request(self, request_details: str, full_name: str, email: str, contact_number: str, user_role: str = "buyer") -> Dict[str, Any]:
        """Notify support for non-standard requests."""
        variables = {
            "request_details": request_details,
            "full_name": full_name,
            "email": email,
            "contact_number": contact_number
        }
        return await self.email_service.send_support_email("support_non_standard_request", variables, user_role)
    
    async def notify_rfq_selected(self, buyer_email: str, rfq_id: str) -> Dict[str, Any]:
        """Notify buyer when seller selects their RFQ."""
        variables = {
            "buyer_email": buyer_email,
            "rfq_id": rfq_id
        }
        return await self.email_service.send_support_email("rfq_selected_notification", variables, "seller")
    
    async def notify_bid_submission_otp_failed(self, full_name: str, email: str, phone_number: str) -> Dict[str, Any]:
        """Notify support when bid submission OTP fails."""
        variables = {
            "full_name": full_name,
            "email": email,
            "phone_number": phone_number
        }
        return await self.email_service.send_support_email("bid_submission_otp_failed", variables, "seller")
    
    async def notify_api_service_failure(self, error_message: str, user_role: str = "system") -> Dict[str, Any]:
        """Notify support when API service fails. DEPRECATED - Use GlobalErrorHandler instead."""
        logger.warning("notify_api_service_failure is deprecated. Use GlobalErrorHandler.handle_api_error instead.")
        
        # Use global error handler instead
        error_context = ErrorContext(
            error_type="API Service Failure",
            error_message=error_message
        )
        
        global_handler = get_global_error_handler()
        success = await global_handler.handle_error(error_context)
        
        if success:
            return {"statusCode": "200", "message": "Notification sent", "status": "Success"}
        else:
            return {"statusCode": "500", "message": "Failed to send notification", "status": "Failure"}