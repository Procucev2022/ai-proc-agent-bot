"""
Global Error Handler for the AI Procurement Agent.

Handles all technical errors with consistent user responses and support team notifications.
Provides centralized error handling for API timeouts, database issues, and server errors.
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from app.config import get_settings
from app.services.whatsapp_service import WhatsAppService
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

@dataclass
class ErrorContext:
    """Context information for error handling."""
    error_type: str
    error_message: str
    user_phone: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    current_flow: Optional[str] = None
    api_name: Optional[str] = None
    endpoint: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    timestamp: Optional[datetime] = None

class GlobalErrorHandler:
    """
    Global error handler for technical issues.
    
    Provides consistent user responses and support team notifications
    for all technical error scenarios.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.whatsapp_service = WhatsAppService()
        self.email_service = EmailService()
        
        # Support team contact details from configuration
        self.support_team_numbers = self.settings.support_team_numbers
        self.support_email = self.settings.support_email
        self.notification_enabled = self.settings.enable_error_notifications
        
        # User-facing error message
        self.user_error_message = (
            "Currently, we are facing some technical issues. The team is actively working to get QUA up and running.\n"
            "We apologise for the inconvenience caused and request you to please try again after a while.\n"
            f"In case of anything urgent, feel free to reach us at {self.settings.support_contact_info}"
        )
    
    async def handle_error(self, error_context: ErrorContext) -> bool:
        """
        Handle technical error with user response and support notification.
        
        Args:
            error_context: Error context information
            
        Returns:
            bool: True if handled successfully, False otherwise
        """
        try:
            # Set timestamp if not provided
            if not error_context.timestamp:
                error_context.timestamp = datetime.now(timezone.utc)
            
            # Send user-friendly response if user phone is available
            if error_context.user_phone:
                await self._send_user_response(error_context.user_phone)
            
            # Notify support team if notifications are enabled
            if self.notification_enabled:
                await self._notify_support_team(error_context)
            
            return True
            
        except Exception as e:
            logger.error(f"Error in global error handler: {e}")
            return False
    
    async def _send_user_response(self, user_phone: str) -> None:
        """Send user-friendly error response."""
        try:
            await self.whatsapp_service.send_message(
                user_phone, 
                self.user_error_message,
                skip_concatenation=True
            )
            logger.info(f"Sent error response to user {user_phone}")
        except Exception as e:
            logger.error(f"Failed to send user error response: {e}")
    
    async def _notify_support_team(self, error_context: ErrorContext) -> None:
        """Notify support team about the error."""
        try:
            # Prepare notification message
            message = self._format_support_message(error_context)
            
            # Try WhatsApp notification first
            whatsapp_success = await self._send_whatsapp_notification(message)
            
            # If WhatsApp fails, send email
            if not whatsapp_success:
                await self._send_email_notification(error_context)
                
        except Exception as e:
            logger.error(f"Failed to notify support team: {e}")
    
    def _format_support_message(self, error_context: ErrorContext) -> str:
        """Format support notification message."""
        message = "Hi Procucev Team,\n"
        message += "We wanted to inform you about a technical issue we've encountered:\n\n"
        message += "Issue Details:\n"
        
        if error_context.api_name:
            message += f"API Name: {error_context.api_name}\n"
        
        if error_context.endpoint:
            message += f"Endpoint: {error_context.endpoint}\n"
        
        message += f"Error Type: {error_context.error_type}\n"
        message += f"Error Message: {error_context.error_message}\n"
        
        # Add database details for database errors
        if error_context.error_type == "Database Error":
            message += self._get_database_details()
        
        if error_context.payload:
            message += f"Request Payload: {str(error_context.payload)[:500]}...\n"
        
        message += f"Occurrence Time: {error_context.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        
        if error_context.user_phone:
            message += f"Affected User Phone: {error_context.user_phone}\n"
        
        if error_context.user_name:
            message += f"User Name: {error_context.user_name}\n"
        
        if error_context.user_email:
            message += f"User Email: {error_context.user_email}\n"
        
        if error_context.current_flow:
            message += f"Current Flow/Stage: {error_context.current_flow}\n"
        
        message += f"Environment: {self.settings.database_mode}\n"
        
        return message
    
    async def _send_whatsapp_notification(self, message: str) -> bool:
        """Send WhatsApp notification to support team."""
        try:
            success_count = 0
            for phone_number in self.support_team_numbers:
                try:
                    result = await self.whatsapp_service.send_message(
                        phone_number, 
                        message,
                        skip_concatenation=True  # Don't prepend irrelevant responses to notifications
                    )
                    if result.success:
                        success_count += 1
                        logger.info(f"WhatsApp notification sent to {phone_number}")
                    else:
                        logger.error(f"Failed to send WhatsApp to {phone_number}: {result.error}")
                except Exception as e:
                    logger.error(f"Error sending WhatsApp to {phone_number}: {e}")
            
            return success_count > 0
            
        except Exception as e:
            logger.error(f"WhatsApp notification failed: {e}")
            return False
    
    async def _send_email_notification(self, error_context: ErrorContext) -> None:
        """Send email notification as fallback."""
        try:
            subject = f"[ALERT] WhatsApp Bot Error - {error_context.error_type} - {error_context.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            
            body = self._format_support_message(error_context)
            
            email_data = {
                "to": [self.support_email],
                "subject": subject,
                "body": body
            }
            
            result = await self.email_service.email_api.send_email(email_data)
            
            if result.get("status") == "Success":
                logger.info("Email notification sent to support team")
            else:
                logger.error(f"Email notification failed: {result}")
                
        except Exception as e:
            logger.error(f"Email notification failed: {e}")
    
    def _get_database_details(self) -> str:
        """Get database configuration details for error reporting."""
        details = "\nDatabase Configuration:\n"
        
        # Database mode and URLs (mask sensitive parts)
        details += f"Database Mode: {self.settings.database_mode}\n"
        
        if self.settings.database_mode == "local" and self.settings.local_database_url:
            masked_url = self._mask_database_url(self.settings.local_database_url)
            details += f"Local DB URL: {masked_url}\n"
        
        if self.settings.database_mode == "client" and self.settings.client_database_url:
            masked_url = self._mask_database_url(self.settings.client_database_url)
            details += f"Client DB URL: {masked_url}\n"
        
        if self.settings.enable_remote_categorization and self.settings.remote_database_url:
            masked_url = self._mask_database_url(self.settings.remote_database_url)
            details += f"Remote DB URL: {masked_url}\n"
        
        # SSL configuration
        details += f"SSL Enabled: {self.settings.is_ssl_enabled()}\n"
        details += f"SQL Debug: {self.settings.sql_debug}\n"
        details += f"Remote Categorization: {self.settings.enable_remote_categorization}\n"
        
        return details
    
    def _mask_database_url(self, url: str) -> str:
        """Mask sensitive information in database URL."""
        import re
        # Pattern to match database URLs and mask password
        # mysql+pymysql://username:password@host:port/database
        pattern = r'(mysql\+pymysql://[^:]+:)([^@]+)(@.+)'
        return re.sub(pattern, r'\1****\3', url)

# Global error handler instance
_global_error_handler: Optional[GlobalErrorHandler] = None

def get_global_error_handler() -> GlobalErrorHandler:
    """Get global error handler singleton."""
    global _global_error_handler
    if _global_error_handler is None:
        _global_error_handler = GlobalErrorHandler()
    return _global_error_handler

# Convenience functions for different error types
async def handle_api_error(
    api_name: str,
    endpoint: str,
    error_message: str,
    payload: Optional[Dict[str, Any]] = None,
    user_phone: Optional[str] = None,
    user_name: Optional[str] = None,
    user_email: Optional[str] = None,
    current_flow: Optional[str] = None
) -> None:
    """Handle API-related errors."""
    error_context = ErrorContext(
        error_type="API Error",
        error_message=error_message,
        api_name=api_name,
        endpoint=endpoint,
        payload=payload,
        user_phone=user_phone,
        user_name=user_name,
        user_email=user_email,
        current_flow=current_flow
    )
    
    handler = get_global_error_handler()
    await handler.handle_error(error_context)

async def handle_database_error(
    error_message: str,
    user_phone: Optional[str] = None,
    user_name: Optional[str] = None,
    user_email: Optional[str] = None,
    current_flow: Optional[str] = None
) -> None:
    """Handle database-related errors with detailed configuration info."""
    error_context = ErrorContext(
        error_type="Database Error",
        error_message=error_message,
        user_phone=user_phone,
        user_name=user_name,
        user_email=user_email,
        current_flow=current_flow
    )
    
    handler = get_global_error_handler()
    await handler.handle_error(error_context)

async def handle_server_error(
    error_message: str,
    user_phone: Optional[str] = None,
    user_name: Optional[str] = None,
    user_email: Optional[str] = None,
    current_flow: Optional[str] = None
) -> None:
    """Handle server-related errors."""
    error_context = ErrorContext(
        error_type="Server Error",
        error_message=error_message,
        user_phone=user_phone,
        user_name=user_name,
        user_email=user_email,
        current_flow=current_flow
    )
    
    handler = get_global_error_handler()
    await handler.handle_error(error_context)