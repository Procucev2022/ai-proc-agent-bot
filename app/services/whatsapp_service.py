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
import re
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

from app.config import get_settings
from app.tools.retry_service import get_retry_service
from app.redis_db import get_redis_service

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

    async def send_message(self, recipient_id: str, message: str, session_id: str = None) -> MessageResponse:
        """
        Send text message to WhatsApp user with retry mechanism.

        Sends formatted text message with API authentication,
        message formatting, error handling, and automatic retries.

        Args:
            recipient_id: WhatsApp number to send to
            message: Message content to send
            session_id: Optional session ID for tracking message in conversation history
        """

        async def send_text_message():
            if self.mock_mode:
                logger.info(f"[MOCK] Sending message to {recipient_id}: {message}")
                return MessageResponse(success=True, message_id="mock_message_id")

            # Format phone number for WhatsApp API
            logger.info(f"Original recipient_id: {recipient_id}")
            formatted_recipient = self._format_phone_number(recipient_id)
            logger.info(f"Formatted recipient: {formatted_recipient}")
            if not formatted_recipient:
                logger.error(f"Invalid phone number format: {recipient_id}")
                return MessageResponse(success=False, error=f"Invalid phone number format: {recipient_id}")

            # Prepare message
            combined_message = message  # default
            # Access the saved irrelevant response from user cache
            redis_service = get_redis_service()
            cache_key = f"user_cache:{formatted_recipient}"
            cache_data = await redis_service.get(cache_key, as_json=True)
            if cache_data and cache_data.get("irrelevant_response"):
                irrelevant_response = cache_data["irrelevant_response"].get("user_message")
                if irrelevant_response:
                    combined_message = f"{irrelevant_response}\n\n{message}"
                    # Clear the irrelevant response after using it
                    cache_data.pop("irrelevant_response", None)
                    await redis_service.set(cache_key, cache_data, ex=43200)
            payload = {
                "user": self.username,
                "pass": self.password,
                "sessiondata": {
                    "from": self.from_number,
                    "to": formatted_recipient,
                    "type": "text",
                    "message": {
                        "text": combined_message or message
                    }
                }
            }
            logger.info(f"\n\npaylof is :{payload}\n\n")

            logger.info(f"WhatsApp payload - from: {self.from_number}, to: {formatted_recipient}")
            logger.info(f"FROM_NUMBER config: {self.from_number}")

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
            result = retry_result["result"]
            # Track message in conversation history if session_id provided
            if session_id and result.success:
                await self._track_message_in_history(session_id, message)
            return result
        else:
            logger.error(f"Failed to send message after {retry_result['attempts']} attempts: {retry_result['error']}")
            return MessageResponse(success=False, error=retry_result["error"])

    async def _track_message_in_history(self, session_id: str, message: str, message_type: str = "text") -> None:
        """
        Track bot message in session conversation history via Redis.

        Args:
            session_id: Session ID to track message for
            message: Message content that was sent
            message_type: Type of message (default: 'text')
        """
        try:
            from app.redis_db import get_session_redis_service
            redis_session = get_session_redis_service()
            await redis_session.append_message_to_history(session_id, "assistant", message, message_type)
        except Exception as e:
            # Don't fail the send if tracking fails - just log
            logger.warning(f"Failed to track message in history for session {session_id}: {e}")

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

            # Format phone number for WhatsApp API
            logger.info(f"Original recipient_id: {recipient_id}")
            formatted_recipient = self._format_phone_number(recipient_id)
            logger.info(f"Formatted recipient: {formatted_recipient}")
            if not formatted_recipient:
                logger.error(f"Invalid phone number format: {recipient_id}")
                return MessageResponse(success=False, error=f"Invalid phone number format: {recipient_id}")

            # Build placeholder dict for template
            placeholders = {}
            for i, param in enumerate(parameters):
                placeholders[str(i)] = param

            payload = {
                "user": self.username,
                "pass": self.password,
                "whatsapptosend": [{
                    "from": self.from_number,
                    "to": formatted_recipient,
                    "templateid": template_name,
                    "smsgid": f"rfq_{formatted_recipient}_{template_name}",
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
        # Force interactive messages to be sent even in mock mode for testing
        if self.mock_mode:
            logger.info(f"[MOCK MODE] Attempting to send {message_type} message to {recipient_id}")
            logger.info(f"[MOCK MODE] Content: {content}")
            # Continue to actual sending instead of returning mock response

        try:
            # Format phone number for WhatsApp API
            logger.info(f"Original recipient_id: {recipient_id}")
            formatted_recipient = self._format_phone_number(recipient_id)
            logger.info(f"Formatted recipient: {formatted_recipient}")
            if not formatted_recipient:
                logger.error(f"Invalid phone number format: {recipient_id}")
                return MessageResponse(success=False, error=f"Invalid phone number format: {recipient_id}")

            payload = {
                "user": self.username,
                "pass": self.password,
                "sessiondata": {
                    "from": self.from_number,
                    "to": formatted_recipient,
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
    
    async def send_cta_button_message(self, recipient_id: str, body_text: str, button_text: str, url: str) -> MessageResponse:
        """
        Send message with CTA (Call-to-Action) button.
        
        Args:
            recipient_id: WhatsApp number to send to
            body_text: Main message text
            button_text: Text displayed on the button
            url: URL to open when button is clicked
        """
        content = {
            "body": {"text": body_text},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": button_text,
                    "url": url
                }
            }
        }
        
        return await self.send_interactive_message(recipient_id, "cta_url", content)
        
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
    

    
    async def send_configurable_buttons(self,
                                      recipient_id: str,
                                      body: str,
                                      buttons_config: List[Dict[str, str]],
                                      header: Optional[str] = None,
                                      footer: str = "(Type 'Exit' anytime to end the chat)",
                                      session_id: str = None) -> MessageResponse:
        """
        Send fully configurable button message that can be used anywhere with any button configuration.

        Args:
            recipient_id: WhatsApp number
            header: Message header text
            body: Message body text
            buttons_config: List of button configurations with 'id', 'title', and optional 'action'
            footer: Footer text (optional)
            session_id: Optional session ID for tracking message in conversation history
        """
        try:
            if not buttons_config:
                raise ValueError("At least one button configuration is required")
                
            if len(buttons_config) > 3:
                logger.warning(f"WhatsApp supports maximum 3 buttons, trimming to first 3 from {len(buttons_config)} provided")
                buttons_config = buttons_config[:3]
            
            # Format phone number for WhatsApp API
            formatted_recipient = self._format_phone_number(recipient_id)
            if not formatted_recipient:
                logger.error(f"Invalid phone number format: {recipient_id}")
                return MessageResponse(success=False, error=f"Invalid phone number format: {recipient_id}")

            # Access the saved irrelevant response from user cache
            redis_service = get_redis_service()
            cache_key = f"user_cache:{formatted_recipient}"
            cache_data = await redis_service.get(cache_key, as_json=True)
            combined_body = body  # default
            if cache_data and cache_data.get("irrelevant_response"):
                irrelevant_response = cache_data["irrelevant_response"].get("user_message")
                if irrelevant_response:
                    combined_body = f"{irrelevant_response}\n\n{body}"
                    logger.info(f"combined message for buttons is  :{combined_body}")
                    # Clear the irrelevant response after using it
                    cache_data.pop("irrelevant_response", None)
                    await redis_service.set(cache_key, cache_data, ex=43200)
            
            # Log button details for debugging
            button_titles = [btn.get('title', 'Unknown') for btn in buttons_config]
            logger.info(f"Sending buttons to {recipient_id}: {button_titles}")
            
            button_list = []
            for i, button in enumerate(buttons_config):
                if not button.get("title"):
                    raise ValueError(f"Button {i} must have a 'title' field")
                
                # WhatsApp interactive buttons only support reply type
                # URL buttons are not supported in interactive messages
                button_list.append({
                    "type": "reply",
                    "reply": {
                        "id": button.get("id", f"btn_{i}"),
                        "title": button.get("title")
                    }
                })
            
            content = {
                "body": {"text": combined_body},
                "footer": {"text": footer},
                "action": {"buttons": button_list}
            }

            
            if header:
                content["header"] = {"type": "text", "text": header}

            result = await self.send_interactive_message(recipient_id, "button", content)

            # Track message in conversation history if session_id provided
            if session_id and result.success:
                # For buttons, track the body text as the message content
                await self._track_message_in_history(session_id, combined_body, "interactive_button")

            return result

        except Exception as e:
            logger.error(f"Error sending configurable buttons: {e}")
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

    def _format_phone_number(self, phone: str) -> str:
        """
        Format phone number for WhatsApp API.

        Ensures phone number is in correct international format without + sign.
        Expected format: country_code + number (e.g., 919876543210)
        """
        if not phone:
            return ""

        # Remove all non-digit characters
        phone = re.sub(r'\D', '', str(phone))

        # If empty after cleaning, return empty
        if not phone:
            return ""

        # Handle different input formats
        if len(phone) == 10:
            # Assume Indian number without country code
            phone = '91' + phone
        elif len(phone) == 11 and phone.startswith('0'):
            # Remove leading 0 and add Indian country code
            phone = '91' + phone[1:]
        elif len(phone) == 13 and phone.startswith('91'):
            # Already has Indian country code
            pass
        elif len(phone) == 12 and not phone.startswith('91'):
            # Might have different country code, keep as is
            pass
        elif len(phone) < 10:
            # Too short, invalid
            logger.warning(f"Phone number too short: {phone}")
            return ""
        elif len(phone) > 15:
            # Too long, invalid (E.164 max is 15 digits)
            logger.warning(f"Phone number too long: {phone}")
            return ""

        # Final validation - should be between 10-15 digits
        if not (10 <= len(phone) <= 15):
            logger.warning(f"Invalid phone number length: {phone}")
            return ""

        logger.info(f"Formatted phone number: {phone[:5]}...{phone[-3:]} (length: {len(phone)})")
        return phone