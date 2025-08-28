"""
Support Helper Functions.

Utility functions for support team operations.
"""

import logging
from typing import Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class SupportHelpers:
    """Helper functions for support operations."""
    
    def __init__(self, whatsapp_service):
        self.whatsapp_service = whatsapp_service
    
    @staticmethod
    def format_support_message(issue_type: str) -> str:
        """Format support message based on issue type."""
        try:
            messages = {
                "registration_failed": "Our support team will contact you shortly to complete your registration.",
                "email_otp_failed": "Our support team will contact you shortly to verify your email.",
                "authentication_error": "Our support team will contact you shortly to assist with login.",
                "default": "Our support team will contact you shortly to assist with your request."
            }
            
            return messages.get(issue_type, messages["default"])
            
        except Exception as e:
            logger.error(f"Support message formatting error: {e}")
            return "Our support team will contact you shortly."
    
    @staticmethod
    def log_support_request(user_phone: str, issue_type: str, error_details: str, 
                          user_details: Dict = None) -> Dict[str, Any]:
        """Log support request with structured data."""
        try:
            support_log = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "phone_number": user_phone,
                "issue_type": issue_type,
                "error_details": error_details,
                "user_details": user_details or {},
                "status": "pending"
            }
            
            logger.info(f"Support request logged: {support_log}")
            return support_log
            
        except Exception as e:
            logger.error(f"Support logging error: {e}")
            return {}
    
    @staticmethod
    def create_support_ticket_data(user_phone: str, issue_type: str, 
                                 context: Dict = None) -> Dict[str, Any]:
        """Create structured support ticket data."""
        try:
            return {
                "phone_number": user_phone,
                "issue_type": issue_type,
                "context": context or {},
                "priority": "normal",
                "created_at": datetime.utcnow().isoformat() + "Z",
                "status": "open"
            }
            
        except Exception as e:
            logger.error(f"Support ticket creation error: {e}")
            return {}
    
    async def redirect_to_support(self, user_phone: str, issue_type: str, 
                                error_details: str = "", user_details: Dict = None) -> Dict[str, Any]:
        """Redirect user to support team with context."""
        try:
            # Send user message
            support_message = "Our support team will contact you shortly to assist with your request."
            await self.whatsapp_service.send_message(user_phone, support_message)
            
            # Log support request with details
            support_context = {
                "phone_number": user_phone,
                "issue_type": issue_type,
                "error_details": error_details,
                "user_details": user_details or {},
                "timestamp": self._get_current_timestamp()
            }
            
            # Send to support team (email/notification system)
            await self._notify_support_team(support_context)
            
            logger.error(f"Support redirect for {user_phone}: {issue_type} - {error_details}")
            
            return {
                "status": "redirected_to_support",
                "issue_type": issue_type,
                "support_ticket_created": True
            }
            
        except Exception as e:
            logger.error(f"Support redirect error: {e}")
            return {"status": "error", "error": "Failed to redirect to support"}
    
    async def _notify_support_team(self, support_context: Dict) -> bool:
        """Notify support team about user issue."""
        try:
            # In production, this would send email/notification to support team
            # For now, just log the support request
            
            support_message = f"""
            Support Request:
            - Phone: {support_context['phone_number']}
            - Issue: {support_context['issue_type']}
            - Details: {support_context['error_details']}
            - User Details: {support_context['user_details']}
            - Timestamp: {support_context['timestamp']}
            """
            
            logger.info(f"Support team notification: {support_message}")
            
            # TODO: Implement actual notification system
            # - Send email to support team
            # - Create ticket in support system
            # - Send Slack/Teams notification
            
            return True
            
        except Exception as e:
            logger.error(f"Support team notification error: {e}")
            return False
    
    def _get_current_timestamp(self) -> str:
        """Get current timestamp for support logging."""
        from datetime import datetime
        return datetime.utcnow().isoformat() + "Z"