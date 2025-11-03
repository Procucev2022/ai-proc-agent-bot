"""
OTP Service for Authentication and Registration.

Provides reusable OTP functionality including:
- OTP sending with retry logic
- OTP validation with failure handling
- Max attempts tracking with support redirect
- Common error handling and notifications
"""

import logging
import re
from typing import Dict, Any, List
from app.models import ConversationSession
from app.services.helpers.authentication_helpers import AuthenticationHelpers

logger = logging.getLogger(__name__)


class OTPService:
    """Reusable OTP service for authentication and registration workflows."""
    
    MAX_OTP_RETRIES = 3
    
    def __init__(self, register_api_service, whatsapp_service, support_notification_service):
        self.register_api_service = register_api_service
        self.whatsapp_service = whatsapp_service
        self.support_notification_service = support_notification_service
    
    async def send_otp(self, user_phone: str, email: str, session: ConversationSession) -> Dict[str, Any]:
        """Send OTP to email and initialize session state."""
        try:
            logger.info(f"OTP_SERVICE: Sending OTP to email: {email} for phone: {user_phone}")
            response = await self.register_api_service.send_otp(email, user_phone)
            logger.info(f"OTP_SERVICE: Send OTP API response: {response}")
            
            if response.get("statusCode") in ["1001", "200"] or response.get("status") == "Success":
                session.workflow_state["otp_email"] = email
                session.workflow_state["otp_retry_count"] = 0

                await self.whatsapp_service.send_message(
                    user_phone,
                    f"OTP sent to {email}.\nPlease provide the OTP sent on your email to complete your registration"
                )

                logger.info(f"OTP_SERVICE: OTP sent successfully to {email}")
                return {"status": "otp_sent", "email": email}
            else:
                logger.error(f"OTP_SERVICE: Failed to send OTP - {response.get('message', 'Unknown error')}")
                return await self._handle_send_failure(user_phone, email, response.get("message", "Failed to send OTP"))
                
        except Exception as e:
            logger.error(f"OTP_SERVICE: OTP send error for {user_phone}: {e}")
            await self.support_notification_service.notify_api_service_failure(
                f"OTP send error for {user_phone}: {str(e)}"
            )
            await self.whatsapp_service.send_message(user_phone, "Error sending OTP. Please contact support.")
            return {"status": "redirect_to_support", "reason": "otp_send_error"}
    
    async def validate_otp(self, user_phone: str, session: ConversationSession, otp: str) -> Dict[str, Any]:
        """Validate OTP and handle retry logic."""
        email = session.workflow_state.get("otp_email")
        if not email:
            logger.warning(f"OTP validation called without email for {user_phone}")
            return {"status": "restart_authentication"}
        
        try:
            logger.info(f"OTP_SERVICE: Validating OTP for {user_phone}, email: {email}")
            response = await self.register_api_service.validate_otp(email, otp, user_phone)
            logger.info(f"OTP_SERVICE: API response: {response}")
            
            if response.get("statusCode") in ["1001", "200"] or response.get("status") == "Success":
                # IMPORTANT: Don't clear otp_email or selected_user here!
                # The authentication_service needs these values after OTP validation succeeds
                # Only clear retry count since validation succeeded
                session.workflow_state.pop("otp_retry_count", None)

                # REMOVED: Don't send message here - let the calling service handle the message
                # This prevents duplicate "Email verified successfully!" messages
                logger.info(f"OTP_SERVICE: OTP validation successful for {user_phone}")
                return {"status": "otp_valid", "email": email}
            else:
                # Increment retry count and handle failure
                session.workflow_state["otp_retry_count"] = session.workflow_state.get("otp_retry_count", 0) + 1
                logger.warning(f"OTP_SERVICE: Invalid OTP for {user_phone}, retry count: {session.workflow_state['otp_retry_count']}")
                return await self._handle_invalid_otp(user_phone, session, email)
                
        except Exception as e:
            logger.error(f"OTP_SERVICE: OTP validation error for {user_phone}: {e}")
            await self.support_notification_service.notify_api_service_failure(
                f"OTP validation error for {user_phone}: {str(e)}"
            )
            await self.whatsapp_service.send_message(user_phone, "Error validating OTP. Please contact support.")
            return {"status": "redirect_to_support", "reason": "otp_validation_error"}
    
    async def handle_user_message(self, user_phone: str, message: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle user OTP input including resend requests."""
        retry_count = session.workflow_state.get("otp_retry_count", 0)
        otp_email = session.workflow_state.get("otp_email")
        
        logger.info(f"OTP_SERVICE: Handling user message '{message}' for {user_phone}, retry_count: {retry_count}")
        
        # Handle resend request
        if message.strip().upper() == "RESEND" and retry_count < self.MAX_OTP_RETRIES:
            logger.info(f"OTP_SERVICE: Resend request from {user_phone}")
            return await self.send_otp(user_phone, otp_email, session)
        
        # Extract and validate OTP format
        otp = self._extract_otp(message)
        if not otp:
            logger.warning(f"OTP_SERVICE: Invalid OTP format from {user_phone}: '{message}'")
            return await self._handle_invalid_format(user_phone, session)
        
        logger.info(f"OTP_SERVICE: Extracted OTP '{otp}' from message '{message}'")
        return await self.validate_otp(user_phone, session, otp)
    
    def _extract_otp(self, message: str) -> str:
        """Extract numeric OTP from message."""
        try:
            digits = re.findall(r'\d+', message.strip())
            if digits:
                otp = digits[0]
                return otp if AuthenticationHelpers.validate_otp_format(otp) else ""
            return ""
        except Exception as e:
            logger.error(f"OTP extraction error: {e}")
            return ""
    
    async def _handle_send_failure(self, user_phone: str, email: str, error_msg: str) -> Dict[str, Any]:
        """Handle OTP send failure."""
        logger.error(f"OTP send failed: {error_msg}")
        await self.support_notification_service.notify_otp_validation_failed("User", email, user_phone)
        
        if "email not exists" in error_msg.lower():
            await self.whatsapp_service.send_message(user_phone, "Email address not found in our system. Please contact support.")
            return {"status": "redirect_to_support", "reason": "email_not_exists"}
        else:
            await self.whatsapp_service.send_message(user_phone, "Failed to send OTP. Please contact support.")
            return {"status": "redirect_to_support", "reason": "otp_send_failed"}
    
    async def _handle_invalid_otp(self, user_phone: str, session: ConversationSession, email: str) -> Dict[str, Any]:
        """Handle invalid OTP with retry mechanism."""
        retry_count = session.workflow_state.get("otp_retry_count", 0)
        
        if retry_count >= self.MAX_OTP_RETRIES:
            await self.support_notification_service.notify_otp_validation_failed("User", email, user_phone)
            await self.whatsapp_service.send_message(user_phone, "Maximum OTP attempts exceeded. Please contact support.")
            # Call exit function without showing exit message
            from app.services.exit_service import ExitService
            exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
            await exit_service.handle_exit_intent(user_phone, session, show_message=False)
            return {"status": "redirect_to_support", "reason": "max_otp_retries_exceeded"}
        
        remaining = self.MAX_OTP_RETRIES - retry_count
        await self.whatsapp_service.send_message(
            user_phone,
            f"Invalid OTP. You have {remaining} attempts remaining.\nPlease enter the correct OTP or reply 'RESEND' to get a new OTP."
        )
        return {"status": "otp_invalid", "retry_count": retry_count, "remaining_attempts": remaining}
    
    async def _handle_invalid_format(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle invalid OTP format."""
        session.workflow_state["otp_retry_count"] = session.workflow_state.get("otp_retry_count", 0) + 1
        retry_count = session.workflow_state["otp_retry_count"]
        
        if retry_count >= self.MAX_OTP_RETRIES:
            await self.whatsapp_service.send_message(user_phone, "Maximum OTP attempts exceeded. Please contact support.")
            return {"status": "redirect_to_support", "reason": "max_otp_retries_exceeded"}
        
        remaining = self.MAX_OTP_RETRIES - retry_count
        await self.whatsapp_service.send_message(
            user_phone,
            f"Please enter a valid OTP. You have {remaining} attempts remaining, or reply 'RESEND' to get a new OTP."
        )
        return {"status": "otp_format_invalid", "retry_count": retry_count}