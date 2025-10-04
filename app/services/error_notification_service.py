"""
Error Notification Service for intelligent error routing.

Routes error notifications based on service availability:
- WhatsApp down -> Email notification
- API down -> WhatsApp notification  
- Both working -> Both channels
"""

import logging
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.services.email_service import EmailService
from app.config import get_settings

logger = logging.getLogger(__name__)

class ErrorNotificationService:
    """
    Service for intelligent error notification routing.
    
    Determines service availability and routes notifications accordingly.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.whatsapp_service = WhatsAppService()
        self.email_service = EmailService()
        self._service_status_cache = {}
        self._cache_expiry = timedelta(minutes=2)
        
    async def notify_error(self, error_type: str, error_details: Dict[str, Any], 
                          recipients: Dict[str, List[str]] = None) -> Dict[str, Any]:
        """
        Send error notification using available channels.
        
        Args:
            error_type: Type of error (whatsapp_down, api_down, general_error)
            error_details: Error information
            recipients: Dict with 'whatsapp' and 'email' recipient lists
        """
        try:
            # Get default recipients if not provided
            if not recipients:
                recipients = self._get_default_recipients()
            
            # Check service availability
            whatsapp_available = await self._check_whatsapp_health()
            email_available = await self._check_email_health()
            
            results = {}
            
            # Route notifications based on availability and error type
            if error_type == "whatsapp_down":
                # WhatsApp is down, use email only
                if email_available:
                    results['email'] = await self._send_email_notification(
                        "whatsapp_service_failure", error_details, recipients['email']
                    )
                else:
                    logger.error("Both WhatsApp and Email services are down!")
                    results['error'] = "All notification channels unavailable"
                    
            elif error_type == "api_down":
                # API is down, use WhatsApp only
                if whatsapp_available:
                    results['whatsapp'] = await self._send_whatsapp_notification(
                        error_details, recipients['whatsapp']
                    )
                else:
                    logger.error("WhatsApp service unavailable for API error notification")
                    results['error'] = "WhatsApp service unavailable"
                    
            else:
                # General error - use both if available
                if whatsapp_available and email_available:
                    # Send to both channels
                    results['whatsapp'] = await self._send_whatsapp_notification(
                        error_details, recipients['whatsapp']
                    )
                    results['email'] = await self._send_email_notification(
                        "api_service_failure", error_details, recipients['email']
                    )
                elif whatsapp_available:
                    # Only WhatsApp available
                    results['whatsapp'] = await self._send_whatsapp_notification(
                        error_details, recipients['whatsapp']
                    )
                elif email_available:
                    # Only Email available
                    results['email'] = await self._send_email_notification(
                        "api_service_failure", error_details, recipients['email']
                    )
                else:
                    logger.error("No notification channels available!")
                    results['error'] = "All notification channels unavailable"
            
            return results
            
        except Exception as e:
            logger.error(f"Error in notification service: {e}")
            return {"error": str(e)}
    
    async def _check_whatsapp_health(self) -> bool:
        """Check if WhatsApp service is available."""
        try:
            # Check cache first
            cache_key = "whatsapp_health"
            if self._is_cache_valid(cache_key):
                return self._service_status_cache[cache_key]['status']
            
            # Test WhatsApp service with a mock message
            test_response = await self.whatsapp_service.send_message(
                "test_number", "health_check"
            )
            
            is_healthy = test_response.success or self.whatsapp_service.mock_mode
            
            # Cache result
            self._service_status_cache[cache_key] = {
                'status': is_healthy,
                'timestamp': datetime.now()
            }
            
            return is_healthy
            
        except Exception as e:
            logger.error(f"WhatsApp health check failed: {e}")
            return False
    
    async def _check_email_health(self) -> bool:
        """Check if Email service is available."""
        try:
            # Check cache first
            cache_key = "email_health"
            if self._is_cache_valid(cache_key):
                return self._service_status_cache[cache_key]['status']
            
            # Test email service by checking template availability
            templates = self.email_service.list_available_templates()
            is_healthy = len(templates) > 0
            
            # Cache result
            self._service_status_cache[cache_key] = {
                'status': is_healthy,
                'timestamp': datetime.now()
            }
            
            return is_healthy
            
        except Exception as e:
            logger.error(f"Email health check failed: {e}")
            return False
    
    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached service status is still valid."""
        if cache_key not in self._service_status_cache:
            return False
        
        cached_time = self._service_status_cache[cache_key]['timestamp']
        return datetime.now() - cached_time < self._cache_expiry
    
    async def _send_whatsapp_notification(self, error_details: Dict[str, Any], 
                                        recipients: List[str]) -> Dict[str, Any]:
        """Send error notification via WhatsApp."""
        try:
            message = self._format_whatsapp_message(error_details)
            results = []
            
            for recipient in recipients:
                response = await self.whatsapp_service.send_message(recipient, message)
                results.append({
                    'recipient': recipient,
                    'success': response.success,
                    'message_id': response.message_id,
                    'error': response.error
                })
            
            return {'results': results, 'success': True}
            
        except Exception as e:
            logger.error(f"Failed to send WhatsApp notification: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _send_email_notification(self, template_name: str, error_details: Dict[str, Any], 
                                     recipients: List[str]) -> Dict[str, Any]:
        """Send error notification via Email."""
        try:
            # Prepare email variables
            variables = {
                'error_type': error_details.get('error_type', 'Unknown Error'),
                'error_message': error_details.get('message', 'No details available'),
                'timestamp': error_details.get('timestamp', datetime.now().isoformat()),
                'service_name': error_details.get('service', 'Unknown Service'),
                'recipients': ','.join(recipients)
            }
            
            # Override recipients in template variables
            variables['support_email'] = ','.join(recipients)
            
            result = await self.email_service.send_email_by_template(
                template_name, variables
            )
            
            return {'success': result.get('status') != 'Failure', 'result': result}
            
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}")
            return {'success': False, 'error': str(e)}
    
    def _format_whatsapp_message(self, error_details: Dict[str, Any]) -> str:
        """Format error details for WhatsApp message."""
        error_type = error_details.get('error_type', 'System Error')
        message = error_details.get('message', 'Unknown error occurred')
        service = error_details.get('service', 'Unknown Service')
        timestamp = error_details.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        formatted_message = f"""🚨 *{error_type}*

*Service:* {service}
*Time:* {timestamp}
*Details:* {message}

Please investigate immediately."""
        
        return formatted_message
    
    def _get_default_recipients(self) -> Dict[str, List[str]]:
        """Get default notification recipients."""
        return {
            'whatsapp': self.settings.support_team_numbers,
            'email': [self.settings.support_email]
        }
    
    async def notify_whatsapp_down(self, error_details: Dict[str, Any]) -> Dict[str, Any]:
        """Convenience method for WhatsApp service down notifications."""
        return await self.notify_error("whatsapp_down", error_details)
    
    async def notify_api_down(self, error_details: Dict[str, Any]) -> Dict[str, Any]:
        """Convenience method for API service down notifications."""
        return await self.notify_error("api_down", error_details)
    
    async def notify_general_error(self, error_details: Dict[str, Any]) -> Dict[str, Any]:
        """Convenience method for general error notifications."""
        return await self.notify_error("general_error", error_details)