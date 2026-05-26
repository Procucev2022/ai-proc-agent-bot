"""
Service Monitor for automatic error detection and notification.

Monitors critical services and automatically triggers notifications
when failures are detected.
"""

import logging
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from app.services.error_notification_service import ErrorNotificationService
from app.services.whatsapp_service import WhatsAppService
from app.services.email_service import EmailService
from app.config import get_settings

logger = logging.getLogger(__name__)

class ServiceMonitor:
    """
    Monitor for critical services with automatic error notification.
    
    Detects service failures and triggers appropriate notifications.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.notification_service = ErrorNotificationService()
        self.whatsapp_service = WhatsAppService()
        self.email_service = EmailService()
        self._last_notification = {}
        self._cooldown_period = timedelta(
            minutes=self.settings.error_notification_cooldown_minutes
        )
    
    @asynccontextmanager
    async def monitor_whatsapp_operation(self, operation_name: str):
        """
        Context manager to monitor WhatsApp operations and notify on failure.
        
        Usage:
            async with service_monitor.monitor_whatsapp_operation("send_message"):
                await whatsapp_service.send_message(...)
        """
        try:
            yield
        except Exception as e:
            await self._handle_whatsapp_error(operation_name, str(e))
            raise
    
    @asynccontextmanager
    async def monitor_api_operation(self, operation_name: str, service_name: str = "API"):
        """
        Context manager to monitor API operations and notify on failure.
        
        Usage:
            async with service_monitor.monitor_api_operation("fetch_data", "GMT API"):
                result = await api_client.fetch_data(...)
        """
        try:
            yield
        except Exception as e:
            await self._handle_api_error(operation_name, service_name, str(e))
            raise
    
    async def _handle_whatsapp_error(self, operation: str, error_message: str):
        """Handle WhatsApp service errors."""
        if not self._should_notify("whatsapp_error"):
            return
        
        error_details = {
            'error_type': 'WhatsApp Service Error',
            'service': 'WhatsApp Service',
            'operation': operation,
            'message': error_message,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            await self.notification_service.notify_whatsapp_down(error_details)
            self._update_last_notification("whatsapp_error")
            logger.info(f"WhatsApp error notification sent for operation: {operation}")
        except Exception as e:
            logger.error(f"Failed to send WhatsApp error notification: {e}")
    
    async def _handle_api_error(self, operation: str, service_name: str, error_message: str):
        """Handle API service errors."""
        if not self._should_notify(f"api_error_{service_name}"):
            return
        
        error_details = {
            'error_type': 'API Service Error',
            'service': service_name,
            'operation': operation,
            'message': error_message,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            await self.notification_service.notify_api_down(error_details)
            self._update_last_notification(f"api_error_{service_name}")
            logger.info(f"API error notification sent for service: {service_name}")
        except Exception as e:
            logger.error(f"Failed to send API error notification: {e}")
    
    def _should_notify(self, error_key: str) -> bool:
        """Check if enough time has passed since last notification."""
        if not self.settings.enable_error_notifications:
            return False
        
        last_time = self._last_notification.get(error_key)
        if not last_time:
            return True
        
        return datetime.now() - last_time > self._cooldown_period
    
    def _update_last_notification(self, error_key: str):
        """Update the timestamp of last notification for an error type."""
        self._last_notification[error_key] = datetime.now()
    
    async def test_services(self) -> Dict[str, Any]:
        """Test all services and return their status."""
        results = {}
        
        # Test WhatsApp service
        try:
            whatsapp_response = await self.whatsapp_service.send_message(
                "test_number", "health_check"
            )
            results['whatsapp'] = {
                'status': 'healthy' if whatsapp_response.success else 'unhealthy',
                'details': whatsapp_response.error if not whatsapp_response.success else 'OK'
            }
        except Exception as e:
            results['whatsapp'] = {
                'status': 'error',
                'details': str(e)
            }
        
        # Test Email service
        try:
            templates = self.email_service.list_available_templates()
            results['email'] = {
                'status': 'healthy' if len(templates) > 0 else 'unhealthy',
                'details': f'{len(templates)} templates available'
            }
        except Exception as e:
            results['email'] = {
                'status': 'error',
                'details': str(e)
            }
        
        return results
    
    async def send_test_notification(self, error_type: str = "general_error") -> Dict[str, Any]:
        """Send a test notification to verify the notification system."""
        test_error = {
            'error_type': 'Test Notification',
            'service': 'Service Monitor',
            'message': 'This is a test notification to verify the error notification system.',
            'timestamp': datetime.now().isoformat()
        }
        
        return await self.notification_service.notify_error(error_type, test_error)

# Global service monitor instance
_service_monitor: Optional[ServiceMonitor] = None

def get_service_monitor() -> ServiceMonitor:
    """Get the global service monitor instance."""
    global _service_monitor
    if _service_monitor is None:
        _service_monitor = ServiceMonitor()
    return _service_monitor