"""
WhatsApp Webhook Service for processing incoming messages.

This service handles the business logic for processing WhatsApp webhook messages,
including message parsing, validation, and routing to the appropriate chat service.
It provides a clean separation between the API layer and business logic.

Key responsibilities:
- Process webhook data and extract message information
- Validate message format and content
- Route messages to ChatService for conversation handling
- Handle different message types (text, media, interactive)
- Provide error handling and logging for webhook processing
"""

import logging
from typing import Dict, Any, Optional
from app.services.chat_service import ChatService
from app.services.opt_out_service import OptOutService

logger = logging.getLogger(__name__)

class WhatsAppWebhookService:
    """
    WhatsApp webhook service for processing incoming messages.
    
    Handles webhook data processing and routes messages to the chat service
    for conversation management and response generation.
    """
    
    def __init__(self):
        """Initialize the WhatsApp webhook service."""
        self.chat_service = ChatService()
        self.opt_out_service = OptOutService()
    
    async def process_webhook(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process incoming webhook data and route to chat service.
        
        Args:
            webhook_data: Parsed webhook data from WhatsApp
            
        Returns:
            Dict with processing status and any response data
        """
        try:
            # Extract message details
            message_type = webhook_data.get("type", "text")
            from_number = webhook_data.get("from")
            content = webhook_data.get("content")
            
            # Validate required fields
            if not from_number or not content:
                logger.warning(f"Missing required webhook data: {webhook_data}")
                return {"status": "error", "message": "Missing required data"}
            
            # Log incoming message for debugging
            logger.info(f"Processing webhook - Type: {message_type}, From: {from_number}")
            
            # First check if this is an opt-out/opt-in message (for text messages only)
            if message_type == "text" and isinstance(content, str):
                opt_out_result = self.opt_out_service.detect_opt_out_intent(content)
                
                if opt_out_result.get("intent") in ["opt_out", "opt_in"] and opt_out_result.get("confidence", 0) > 60:
                    logger.info(f"Detected {opt_out_result['intent']} intent from {from_number}")
                    
                    # Handle opt-out/opt-in request
                    if opt_out_result["intent"] == "opt_out":
                        opt_result = await self.opt_out_service.handle_opt_out_request(from_number)
                    else:  # opt_in
                        opt_result = await self.opt_out_service.handle_opt_in_request(from_number)
                    
                    return {
                        "status": "success",
                        "response": opt_result,
                        "processed_message": {
                            "type": message_type,
                            "from": from_number,
                            "content_length": len(str(content)),
                            "handled_by": "opt_out_service",
                            "intent_detected": opt_out_result["intent"]
                        }
                    }
            
            # Process through chat service (normal flow)
            response = await self.chat_service.process_message(
                user_phone=from_number,
                message_content=content,
                message_type=message_type
            )
            
            # Return success response
            return {
                "status": "success", 
                "response": response,
                "processed_message": {
                    "type": message_type,
                    "from": from_number,
                    "content_length": len(str(content)),
                    "handled_by": "chat_service"
                }
            }
            
        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            return {"status": "error", "message": str(e)}
    
    def validate_webhook_data(self, webhook_data: Dict[str, Any]) -> bool:
        """
        Validate webhook data structure and required fields.
        
        Args:
            webhook_data: Webhook data to validate
            
        Returns:
            True if data is valid, False otherwise
        """
        try:
            # Check required fields
            required_fields = ["type", "from", "content"]
            for field in required_fields:
                if field not in webhook_data:
                    logger.warning(f"Missing required field: {field}")
                    return False
            
            # Validate message type
            valid_types = ["text", "document", "interactive", "image", "video"]
            if webhook_data["type"] not in valid_types:
                logger.warning(f"Invalid message type: {webhook_data['type']}")
                return False
            
            # Validate phone number format (basic check)
            phone = webhook_data["from"]
            if not phone or len(phone) < 10:
                logger.warning(f"Invalid phone number: {phone}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error validating webhook data: {e}")
            return False
    
    def get_processing_stats(self) -> Dict[str, Any]:
        """
        Get webhook processing statistics.
        
        Returns:
            Dict with processing statistics
        """
        # This could be enhanced with actual metrics tracking
        return {
            "service_status": "active",
            "supported_message_types": ["text", "document", "interactive", "image", "video"],
            "chat_service_available": bool(self.chat_service)
        }