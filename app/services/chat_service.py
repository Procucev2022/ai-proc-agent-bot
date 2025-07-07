"""
Main chat orchestration service for processing user messages.

This service acts as the central orchestrator for all user interactions,
coordinating between authentication, intent classification, and workflow routing.
It manages conversation state, session handling, and ensures proper message flow
through the entire AI procurement agent system.

Key responsibilities:
- Orchestrate the complete message processing pipeline
- Manage user authentication and registration status
- Route messages to appropriate workflow handlers based on intent
- Maintain conversation context and session state
- Handle error scenarios and fallback mechanisms
- Coordinate responses back to users through WhatsApp
"""

import logging
from typing import Dict, Any, Optional
import json
from datetime import datetime

from app.services.intent_service import IntentService
from app.services.entity_service import EntityService
from app.services.vendor_service import VendorService
from app.services.rfq_service import RFQService
from app.services.whatsapp_service import WhatsAppService
from app.database import SessionLocal
from app.models import User, ConversationSession

logger = logging.getLogger(__name__)


class ChatService:
    """
    Central orchestrator for all user message processing.
    
    Coordinates authentication, intent classification, workflow routing,
    and response generation for the complete chat experience.
    """
    
    def __init__(self):
        self.intent_service = IntentService()
        self.entity_service = EntityService()
        self.vendor_service = VendorService()
        self.rfq_service = RFQService()
        self.whatsapp_service = WhatsAppService()
        
    async def process_message(self, user_phone: str, message_content: str, message_type: str = "text") -> Dict[str, Any]:
        """
        Process incoming user message through complete pipeline.
        
        Orchestrates authentication check, intent classification,
        workflow routing, and response generation.
        """
        try:
            logger.info(f"Processing message from {user_phone}: {message_content}")
            
            # Get or create user session
            user = await self._get_or_create_user(user_phone)
            session = await self._get_conversation_context(user_phone)
            
            # Handle different message types
            if message_type == "text":
                return await self._process_text_message(user, session, message_content)
            elif message_type == "interactive":
                return await self._process_interactive_message(user, session, message_content)
            else:
                await self.whatsapp_service.send_message(
                    user_phone, 
                    "I can currently process text messages. Please send your request as text."
                )
                return {"status": "handled", "response": "unsupported_message_type"}
                
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self.whatsapp_service.send_message(
                user_phone, 
                "Sorry, I encountered an error processing your message. Please try again."
            )
            return {"status": "error", "error": str(e)}
    
    async def _process_text_message(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Process text message through intent classification and routing."""
        try:
            # Check if user needs registration
            if not user.is_registered:
                return await self._handle_registration_workflow(user, message)
            
            # Classify intent
            intent_result = self.intent_service.classify_intent(message)
            logger.info(f"Intent classification result: {intent_result}")
            
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)
            
            # Route based on intent
            if intent == "buy_something" and confidence > 0.7:
                return await self._handle_purchase_intent(user, session, message)
            elif intent == "general_inquiry":
                return await self._handle_general_inquiry(user, message)
            elif confidence < 0.5:
                return await self._handle_clarification_request(user, message)
            else:
                return await self._handle_fallback(user, message)
                
        except Exception as e:
            logger.error(f"Error processing text message: {e}")
            raise
    
    async def _process_interactive_message(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
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
                # Treat as regular text message
                text_content = str(content)
                return await self._process_text_message(user, session, text_content)
                
        except Exception as e:
            logger.error(f"Error processing interactive message: {e}")
            raise
    
    async def _get_or_create_user(self, phone_number: str) -> User:
        """Get existing user or create new one."""
        # Mock user for testing without database
        class MockUser:
            def __init__(self, phone_number):
                self.id = 1
                self.phone_number = phone_number
                self.name = "Test User"
                self.is_registered = True
                self.role = "buyer"
        
        return MockUser(phone_number)
    
    async def _get_conversation_context(self, phone_number: str) -> ConversationSession:
        """Retrieve or create conversation context for user session."""
        # Mock session for testing without database
        class MockSession:
            def __init__(self, phone_number):
                self.id = 1
                self.user_phone = phone_number
                self.session_data = {}
                self.current_step = "greeting"
                self.is_active = True
        
        return MockSession(phone_number)
    
    async def _handle_registration_workflow(self, user: User, message: str) -> Dict[str, Any]:
        """Handle user registration process."""
        try:
            # Simple registration - just collect name
            if not user.name:
                # Extract name from message
                name = message.strip().title()
                
                with SessionLocal() as db:
                    db_user = db.query(User).filter(User.id == user.id).first()
                    db_user.name = name
                    db_user.is_registered = True
                    db.commit()
                
                welcome_message = f"Welcome {name}!\n\nI'm your AI Procurement Assistant. I can help you:\n\n- Find vendors for your requirements\n- Create RFQs (Request for Quotations)\n- Check product availability\n\nWhat would you like to procure today?"
                
                await self.whatsapp_service.send_message(user.phone_number, welcome_message)
                return {"status": "registered", "user_name": name}
            else:
                # User already has name, mark as registered
                with SessionLocal() as db:
                    db_user = db.query(User).filter(User.id == user.id).first()
                    db_user.is_registered = True
                    db.commit()
                
                return await self._process_text_message(user, await self._get_conversation_context(user.phone_number), message)
                
        except Exception as e:
            logger.error(f"Error in registration workflow: {e}")
            await self.whatsapp_service.send_message(
                user.phone_number, 
                "Welcome! Please tell me your name to get started."
            )
            return {"status": "error", "error": str(e)}
    
    async def _handle_purchase_intent(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Handle purchase intent with RFQ-first workflow using EntityService intelligence."""
        try:
            # Use EntityService to extract entities and get intelligent next questions
            entity_result = self.entity_service.extract_entities(message, context=None, workflow_type="buy_something")
            logger.info(f"EntityService result: {entity_result}")
            
            completeness = entity_result.get('completeness', 0)
            next_questions = entity_result.get('next_questions', [])
            missing_fields = entity_result.get('missing_required_fields', [])
            
            # Mock RFQ result for testing without database
            completeness = entity_result.get('completeness', 0)
            
            if completeness >= 80:
                rfq_result = {
                    "rfq_complete": True,
                    "rfq_data": {
                        "id": "mock_rfq_123",
                        "product_name": "Office chairs",
                        "quantity": "50",
                        "unit_of_measure": "pieces",
                        "delivery_city": "Mumbai"
                    },
                    "completeness": completeness
                }
            elif completeness >= 40:
                rfq_result = {
                    "needs_clarification": True,
                    "questions": entity_result.get('next_questions', [])[:2],
                    "completeness": completeness
                }
            else:
                rfq_result = {
                    "continue_collection": True,
                    "next_question": entity_result.get('next_questions', ["What would you like to procure?"])[0],
                    "completeness": completeness
                }
            
            # Show progress to user
            progress_message = f"RFQ Progress: {completeness}% complete"
            if rfq_result.get("updated_fields"):
                progress_message += f"\nUpdated: {', '.join(rfq_result['updated_fields'])}"
            
            if rfq_result.get("rfq_complete"):
                # RFQ is 100% complete - ready for submission
                rfq_data = rfq_result.get("rfq_data", {})
                
                # Send completion message with summary
                completion_message = f"{progress_message}\n\nYour RFQ is complete!"
                await self.whatsapp_service.send_message(user.phone_number, completion_message)
                
                # Format and send detailed RFQ summary
                summary = self.whatsapp_service.format_rfq_summary(rfq_data)
                await self.whatsapp_service.send_message(user.phone_number, summary)
                
                # Mock successful RFQ completion
                success_message = f"RFQ successfully created!\nReference: {rfq_data['id']}\n\nNext steps: We'll search for vendors and get back to you with quotes."
                await self.whatsapp_service.send_message(user.phone_number, success_message)
                
                return {
                    "status": "rfq_complete", 
                    "completeness": completeness
                }
            
            elif rfq_result.get("needs_clarification"):
                # Multiple specific questions needed
                questions = rfq_result.get("questions", [])
                
                await self.whatsapp_service.send_message(user.phone_number, progress_message)
                
                if questions:
                    question_text = "I need a bit more information:\n\n" + "\n".join(f"- {q}" for q in questions)
                    await self.whatsapp_service.send_message(user.phone_number, question_text)
                
                return {
                    "status": "clarification_needed", 
                    "questions": questions,
                    "completeness": completeness
                }
            
            else:
                # Continue collecting with single next question
                next_question = rfq_result.get("next_question", "Can you provide more details about your requirement?")
                
                await self.whatsapp_service.send_message(user.phone_number, progress_message)
                await self.whatsapp_service.send_message(user.phone_number, next_question)
                
                return {
                    "status": "collecting_info", 
                    "next_question": next_question,
                    "completeness": completeness,
                    "missing_fields": missing_fields
                }
                
        except Exception as e:
            logger.error(f"Error handling purchase intent: {e}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "I understand you want to procure something. Can you tell me more about what you need?"
            )
            return {"status": "error", "error": str(e)}
    
    async def _handle_general_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        """Handle general inquiries with template responses."""
        try:
            message_lower = message.lower()
            
            if any(word in message_lower for word in ["help", "what", "how"]):
                help_text = """I can help you with:

*Finding Vendors* - Tell me what you need and I'll find suitable vendors
*Creating RFQs* - I'll help collect requirements and create professional RFQs  
*Product Search* - Check our Buy From Stock inventory
*General Questions* - Ask me anything about procurement

Try saying something like:
"I need 100 office chairs"
"Looking for IT equipment suppliers"
"What products do you have in stock?"
"""
                await self.whatsapp_service.send_message(user.phone_number, help_text)
            
            else:
                general_response = "I'm here to help with your procurement needs! You can tell me what you want to buy, ask about vendors, or check product availability. How can I assist you today?"
                await self.whatsapp_service.send_message(user.phone_number, general_response)
            
            return {"status": "general_inquiry_handled"}
            
        except Exception as e:
            logger.error(f"Error handling general inquiry: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_clarification_request(self, user: User, message: str) -> Dict[str, Any]:
        """Handle ambiguous messages requiring clarification."""
        try:
            clarification_text = """I'm not quite sure what you're looking for. Could you be more specific?

For example, you could say:
- "I need office supplies"
- "Looking for a laptop vendor"
- "Do you have printers in stock?"
- "Help me create an RFQ"

What would you like to do?"""
            
            await self.whatsapp_service.send_message(user.phone_number, clarification_text)
            return {"status": "clarification_sent"}
            
        except Exception as e:
            logger.error(f"Error handling clarification request: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_fallback(self, user: User, message: str) -> Dict[str, Any]:
        """Handle messages that don't fit other categories."""
        try:
            fallback_text = "I understand you're trying to communicate with me, but I'm specifically designed to help with procurement tasks. You can:\n\n- Tell me what you want to buy\n- Ask about vendors\n- Check product availability\n- Get help with RFQs\n\nHow can I help with your procurement needs?"
            
            await self.whatsapp_service.send_message(user.phone_number, fallback_text)
            return {"status": "fallback_handled"}
            
        except Exception as e:
            logger.error(f"Error in fallback handler: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_button_response(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle button interaction responses."""
        # Implementation for button responses
        logger.info(f"Button response from {user.phone_number}: {button_id}")
        return {"status": "button_handled", "button_id": button_id}
    
    async def _handle_list_response(self, user: User, session: ConversationSession, list_id: str) -> Dict[str, Any]:
        """Handle list selection responses."""
        # Implementation for list responses
        logger.info(f"List response from {user.phone_number}: {list_id}")
        return {"status": "list_handled", "list_id": list_id}