"""
Interactive Message Processor.

Handles interactive message processing (buttons, lists) for WhatsApp.
Extracted from ChatService to reduce complexity.
"""

import logging
import json
from typing import Dict, Any
from app.models import User, ConversationSession

logger = logging.getLogger(__name__)


class InteractiveMessageProcessor:
    """Processes interactive messages (buttons, lists)."""
    
    def __init__(self, whatsapp_service, **kwargs):
        self.whatsapp_service = whatsapp_service
    
    async def process_interactive_message(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
        """Process interactive message responses (buttons, lists)."""
        try:
            # Parse interactive content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {"type": "unknown", "content": content}
            
            message_type = content.get("type")
            
            if message_type == "button_reply":
                button_id = content.get("button_reply", {}).get("id")
                return await self._handle_button_response(user, session, button_id)
            elif message_type == "list_reply":
                list_id = content.get("list_reply", {}).get("id")
                return await self._handle_list_response(user, session, list_id)
            else:
                # Treat as regular text message - delegate to text processor
                text_content = str(content)
                return {"status": "delegate_to_text_processor", "content": text_content}
                
        except Exception as e:
            logger.error(f"Error processing interactive message: {e}")
            raise
    
    async def _handle_button_response(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle button interaction responses."""
        logger.info(f"Button response from {user.phone_number}: {button_id}")
        return {"status": "button_handled", "button_id": button_id}
    
    async def _handle_list_response(self, user: User, session: ConversationSession, list_id: str) -> Dict[str, Any]:
        """Handle list selection responses."""
        logger.info(f"List response from {user.phone_number}: {list_id}")
        return {"status": "list_handled", "list_id": list_id}