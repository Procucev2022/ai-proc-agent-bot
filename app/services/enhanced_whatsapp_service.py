"""
Enhanced WhatsApp Service with automatic error monitoring.

Extends the existing WhatsApp service with integrated error monitoring
and automatic notification capabilities.
"""

import logging
from typing import Dict, List, Any, Optional

from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.services.service_monitor import get_service_monitor

logger = logging.getLogger(__name__)

class EnhancedWhatsAppService(WhatsAppService):
    """
    Enhanced WhatsApp service with automatic error monitoring.
    
    Automatically detects failures and triggers notifications via email
    when WhatsApp service is unavailable.
    """
    
    def __init__(self):
        super().__init__()
        self.service_monitor = get_service_monitor()
    
    async def send_message(self, recipient_id: str, message: str) -> MessageResponse:
        """Send message with automatic error monitoring."""
        async with self.service_monitor.monitor_whatsapp_operation("send_message"):
            return await super().send_message(recipient_id, message)
    
    async def send_template_message(self, recipient_id: str, template_name: str, 
                                  parameters: list) -> MessageResponse:
        """Send template message with automatic error monitoring."""
        async with self.service_monitor.monitor_whatsapp_operation("send_template_message"):
            return await super().send_template_message(recipient_id, template_name, parameters)
    
    async def send_interactive_message(self, recipient_id: str, message_type: str, 
                                     content: Dict[str, Any]) -> MessageResponse:
        """Send interactive message with automatic error monitoring."""
        async with self.service_monitor.monitor_whatsapp_operation("send_interactive_message"):
            return await super().send_interactive_message(recipient_id, message_type, content)
    
    async def send_configurable_buttons(self, recipient_id: str, body: str, 
                                      buttons_config: List[Dict[str, str]], 
                                      header: Optional[str] = None,
                                      footer: str = "Please choose an option") -> MessageResponse:
        """Send configurable buttons with automatic error monitoring."""
        async with self.service_monitor.monitor_whatsapp_operation("send_configurable_buttons"):
            return await super().send_configurable_buttons(
                recipient_id, body, buttons_config, header, footer
            )