"""
WhatsApp messaging service for sending and receiving messages.

This service handles all interactions with the WhatsApp Business API,
including sending messages to users, formatting responses, handling
message templates, and managing WhatsApp-specific constraints like
message size limits and media handling.

Key responsibilities:
- Send text messages to WhatsApp users
- Format and structure response messages for optimal readability
- Handle WhatsApp message templates and quick replies
- Manage WhatsApp API rate limits and error handling
- Support rich message formats (lists, buttons, media)
- Handle message delivery status and read receipts
"""

import requests
import json
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

from app.config import get_settings
from app.tools.retry_service import get_retry_service

logger = logging.getLogger(__name__)


@dataclass
class MessageResponse:
    """Response from WhatsApp API."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class WhatsAppService:
    """
    WhatsApp messaging service for API interactions.
    
    Handles all WhatsApp Business API operations including message sending,
    formatting, templates, and API response management.
    """
    
    def __init__(self):
        settings = get_settings()
        self.username = settings.WHATSAPP_USERNAME
        self.password = settings.WHATSAPP_PASSWORD
        self.from_number = settings.WHATSAPP_FROM_NUMBER
        self.base_url = settings.WHATSAPP_BASE_URL
        self.template_base_url = settings.WHATSAPP_TEMPLATE_BASE_URL
        self.mock_mode = getattr(settings, 'WHATSAPP_MOCK_MODE', True)
        
        # Initialize retry service
        self.retry_service = get_retry_service()
        self.retry_service.max_retries = settings.retry_max_attempts
        self.retry_service.initial_delay = settings.retry_initial_delay
        
    async def send_message(self, recipient_id: str, message: str) -> MessageResponse:
        """
        Send text message to WhatsApp user with retry mechanism.
        
        Sends formatted text message with API authentication,
        message formatting, error handling, and automatic retries.
        """
        async def send_text_message():
            if self.mock_mode:
                logger.info(f"[MOCK] Sending message to {recipient_id}: {message}")
                return MessageResponse(success=True, message_id="mock_message_id")
            
            payload = {
                "user": self.username,
                "pass": self.password,
                "sessiondata": {
                    "from": self.from_number,
                    "to": recipient_id,
                    "type": "text",
                    "message": {
                        "text": message
                    }
                }
            }
            
            response = requests.post(
                f"{self.base_url}/sessioncomm",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            
            return self._handle_api_response(response)
        
        # Use retry service for reliable delivery
        retry_result = await self.retry_service.retry_with_backoff(send_text_message)
        
        if retry_result["success"]:
            return retry_result["result"]
        else:
            logger.error(f"Failed to send message after {retry_result['attempts']} attempts: {retry_result['error']}")
            return MessageResponse(success=False, error=retry_result["error"])
        
    async def send_template_message(self, recipient_id: str, template_name: str, parameters: list) -> MessageResponse:
        """
        Send template message with parameters and retry mechanism.
        
        Uses WhatsApp approved message templates for sending
        formatted messages with dynamic content and automatic retries.
        """
        async def send_whatsapp_template():
            if self.mock_mode:
                logger.info(f"[MOCK] Sending template '{template_name}' to {recipient_id} with params: {parameters}")
                return MessageResponse(success=True, message_id="mock_template_id")
            
            # Build placeholder dict for template
            placeholders = {}
            for i, param in enumerate(parameters):
                placeholders[str(i)] = param
            
            payload = {
                "user": self.username,
                "pass": self.password,
                "whatsapptosend": [{
                    "from": self.from_number,
                    "to": recipient_id,
                    "templateid": template_name,
                    "smsgid": f"rfq_{recipient_id}_{template_name}",
                    "placeholders": [placeholders] if placeholders else [],
                    "buttons": []
                }]
            }
            
            response = requests.post(
                f"{self.base_url}/mediasend",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            
            return self._handle_api_response(response)
        
        # Use retry service for reliable delivery
        retry_result = await self.retry_service.retry_with_backoff(send_whatsapp_template)
        
        if retry_result["success"]:
            return retry_result["result"]
        else:
            logger.error(f"Failed to send template message after {retry_result['attempts']} attempts: {retry_result['error']}")
            return MessageResponse(success=False, error=retry_result["error"])
        
    async def send_interactive_message(self, recipient_id: str, message_type: str, content: Dict[str, Any]) -> MessageResponse:
        """
        Send interactive message (buttons or list).
        
        Args:
            recipient_id: WhatsApp number to send to
            message_type: 'button' or 'list'
            content: Message content with interactive elements
        """
        if self.mock_mode:
            logger.info(f"[MOCK] Sending {message_type} message to {recipient_id}: {content}")
            return MessageResponse(success=True, message_id="mock_interactive_id")
        
        try:
            payload = {
                "user": self.username,
                "pass": self.password,
                "sessiondata": {
                    "from": self.from_number,
                    "to": recipient_id,
                    "type": "interactive",
                    "message": {
                        "interactive": {
                            "type": message_type,
                            **content
                        }
                    }
                }
            }
            
            response = requests.post(
                f"{self.base_url}/sessioncomm",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            
            return self._handle_api_response(response)
            
        except Exception as e:
            logger.error(f"Error sending interactive message: {e}")
            return MessageResponse(success=False, error=str(e))
        
    async def send_list_message(self, recipient_id: str, header: str, body: str, items: List[Dict[str, str]]) -> MessageResponse:
        """
        Send interactive list message.
        
        Sends formatted list of items that users can select from
        for better user experience.
        """
        try:
            # Build sections for list message
            sections = []
            current_section = {"title": "Options", "rows": []}
            
            for i, item in enumerate(items):
                if len(current_section["rows"]) >= 10:  # Max 10 items per section
                    sections.append(current_section)
                    current_section = {"title": f"More Options", "rows": []}
                
                current_section["rows"].append({
                    "id": str(i),
                    "title": item.get("title", "Option"),
                    "description": item.get("description", "")
                })
            
            if current_section["rows"]:
                sections.append(current_section)
            
            content = {
                "header": {"type": "text", "text": header},
                "body": {"text": body},
                "footer": {"text": "Select an option"},
                "action": {
                    "button": "Choose",
                    "sections": sections
                }
            }
            
            return await self.send_interactive_message(recipient_id, "list", content)
            
        except Exception as e:
            logger.error(f"Error sending list message: {e}")
            return MessageResponse(success=False, error=str(e))
    
    async def send_button_message(self, recipient_id: str, header: str, body: str, buttons: List[Dict[str, str]]) -> MessageResponse:
        """
        Send interactive button message.
        
        Args:
            recipient_id: WhatsApp number
            header: Message header
            body: Message body
            buttons: List of buttons (max 3)
        """
        try:
            button_list = []
            for i, button in enumerate(buttons[:3]):  # Max 3 buttons
                button_list.append({
                    "type": "reply",
                    "reply": {
                        "id": str(i),
                        "title": button.get("title", f"Button {i+1}")
                    }
                })
            
            content = {
                "header": {"type": "text", "text": header},
                "body": {"text": body},
                "footer": {"text": "Choose an option"},
                "action": {"buttons": button_list}
            }
            
            return await self.send_interactive_message(recipient_id, "button", content)
            
        except Exception as e:
            logger.error(f"Error sending button message: {e}")
            return MessageResponse(success=False, error=str(e))
        
    def format_vendor_results(self, vendors: List[Dict[str, Any]]) -> str:
        """
        Format vendor search results for WhatsApp message.
        
        Creates readable format for displaying vendor information
        within WhatsApp message constraints.
        """
        if not vendors:
            return "No vendors found matching your requirements."
        
        formatted_text = "*Found Vendors:*\n\n"
        
        for i, vendor in enumerate(vendors[:5], 1):  # Limit to 5 vendors
            formatted_text += f"{i}. *{vendor.get('name', 'Unknown Vendor')}*\n"
            if vendor.get('description'):
                formatted_text += f"   Description: {vendor['description']}\n"
            if vendor.get('contact'):
                formatted_text += f"   Contact: {vendor['contact']}\n"
            if vendor.get('location'):
                formatted_text += f"   Location: {vendor['location']}\n"
            formatted_text += "\n"
        
        if len(vendors) > 5:
            formatted_text += f"_... and {len(vendors) - 5} more vendors available_"
        
        return formatted_text
        
    def format_bfs_results(self, products: List[Dict[str, Any]]) -> str:
        """
        Format BFS (Buy From Stock) product results for WhatsApp.
        
        Creates readable format for displaying available products
        with pricing and availability information.
        """
        if not products:
            return "No products available in stock."
        
        formatted_text = "*Available Products:*\n\n"
        
        for i, product in enumerate(products[:5], 1):  # Limit to 5 products
            formatted_text += f"{i}. *{product.get('name', 'Unknown Product')}*\n"
            if product.get('price'):
                formatted_text += f"   Price: ₹{product['price']}\n"
            if product.get('quantity'):
                formatted_text += f"   Stock: {product['quantity']} units\n"
            if product.get('description'):
                formatted_text += f"   Description: {product['description']}\n"
            formatted_text += "\n"
        
        if len(products) > 5:
            formatted_text += f"_... and {len(products) - 5} more products available_"
        
        return formatted_text
        
    def format_rfq_summary(self, rfq_data: Dict[str, Any]) -> str:
        """
        Format RFQ summary for user confirmation.
        
        Creates formatted summary of collected RFQ information
        for user review before submission.
        """
        summary = "*RFQ Summary:*\n\n"
        
        if rfq_data.get('product_name'):
            summary += f"*Product:* {rfq_data['product_name']}\n"
        
        if rfq_data.get('quantity'):
            summary += f"*Quantity:* {rfq_data['quantity']}\n"
        
        if rfq_data.get('unit_of_measure'):
            summary += f"*Unit:* {rfq_data['unit_of_measure']}\n"
        
        if rfq_data.get('deadline'):
            summary += f"*Delivery Date:* {rfq_data['deadline']}\n"
        
        if rfq_data.get('delivery_city'):
            summary += f"*Delivery Location:* {rfq_data['delivery_city']}"
            if rfq_data.get('delivery_state'):
                summary += f", {rfq_data['delivery_state']}"
            summary += "\n"
        
        if rfq_data.get('division'):
            summary += f"*Division:* {rfq_data['division']}\n"
        
        if rfq_data.get('specifications'):
            summary += f"*Specifications:* {rfq_data['specifications']}\n"
        
        if rfq_data.get('remarks'):
            summary += f"*Additional Notes:* {rfq_data['remarks']}\n"
        
        summary += "\nPlease confirm if this information is correct."
        
        return summary
        
    def _handle_api_response(self, response: requests.Response) -> MessageResponse:
        """Handle WhatsApp API response and extract relevant information."""
        try:
            if response.status_code == 200:
                data = response.json()
                
                # Handle different response formats
                if isinstance(data, list) and len(data) > 0:
                    # Bulk message response format
                    first_response = data[0]
                    if 'mid' in first_response:
                        return MessageResponse(success=True, message_id=str(first_response['mid']))
                elif isinstance(data, dict):
                    # Single message response format
                    if 'mid' in data:
                        return MessageResponse(success=True, message_id=str(data['mid']))
                    elif data.get('status') == 'ok':
                        return MessageResponse(success=True)
                
                return MessageResponse(success=True)
            
            else:
                error_msg = f"API error: {response.status_code}"
                try:
                    error_data = response.json()
                    if 'Error' in error_data:
                        error_msg += f" - {error_data['Error']}"
                except:
                    pass
                
                return MessageResponse(success=False, error=error_msg)
                
        except Exception as e:
            logger.error(f"Error handling API response: {e}")
            return MessageResponse(success=False, error=str(e))