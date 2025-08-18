"""
Text Message Processor.

Handles text message processing including intent classification and routing.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any
from app.models import User, ConversationSession
from app.services.intent_service import IntentService
from app.services.entity_service import EntityService
from app.services.whatsapp_service import WhatsAppService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.chat_summary_service import ChatSummaryService

logger = logging.getLogger(__name__)


class TextMessageProcessor:
    """Processes text messages."""
    
    def __init__(self, intent_service: IntentService, entity_service: EntityService, 
                 whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers,
                 confirmation_handler: ConfirmationHandler, chat_summary_service: ChatSummaryService,
                 rfq_service, openai_service):
        self.intent_service = intent_service
        self.entity_service = entity_service
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.confirmation_handler = confirmation_handler
        self.chat_summary_service = chat_summary_service
        self.rfq_service = rfq_service
        self.openai_service = openai_service
    
    async def process_text_message(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Process text message through intent classification and routing."""
        try:
            # Check if user needs registration
            if not user.is_registered:
                return await self._handle_registration_workflow(user, message)
            
            # Check workflow state
            existing_entities = session.workflow_state.get("extracted_entities", [])
            has_existing_data = len(existing_entities) > 0 and any(
                any(v for v in product.values() if v is not None) 
                for product in existing_entities
            )
            
            # Check for incomplete products or pending confirmations
            has_incomplete_products = bool(session.workflow_state.get("incomplete_products"))
            has_pending_confirmations = bool(session.workflow_state.get("pending_combined_rfq") or session.workflow_state.get("pending_rfq"))
            has_pending_optional = bool(session.workflow_state.get("pending_optional_rfq") or session.workflow_state.get("pending_optional_combined_rfq"))
            has_pending_attachment_decision = bool(session.workflow_state.get("awaiting_attachment_decision"))
            
            print(f"TextProcessor: has_existing_data={has_existing_data}, has_incomplete_products={has_incomplete_products}, has_pending_confirmations={has_pending_confirmations}, has_pending_optional={has_pending_optional}, has_pending_attachment_decision={has_pending_attachment_decision}")
            
            # Classify intent FIRST
            conversation_context = ChatServiceHelpers.build_conversation_context(session, message)
            intent_result = self.intent_service.classify_intent(message, conversation_context)
            logger.info(f"Intent classification result: {intent_result}")
            
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)
            
            # Handle modification requests immediately if detected with sufficient confidence
            if intent == "modification_request" and confidence > 0.7:
                logger.info(f"Modification intent detected with {confidence}% confidence - handling immediately")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            
            # Handle pending attachment decisions
            if has_pending_attachment_decision:
                return await self._handle_attachment_decision(user, session, message)
            
            # Handle pending optional field responses
            if has_pending_optional:
                result = await self.confirmation_handler.handle_optional_fields_response(user, session, message)
                return result
            
            # Handle pending confirmations
            if has_pending_confirmations:
                result = await self.confirmation_handler.handle_pending_confirmations(user, session, message, intent_result)
                return result
            
            # Continue existing RFQ workflow
            if has_existing_data or has_incomplete_products:
                logger.info("Continuing existing RFQ workflow")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            
            # Route based on classified intent
            if intent == "buy_something" and confidence > 0.7:
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "confirmation_response" and confidence > 0.7:
                logger.info(f"Handling confirmation response with context: {intent_result.get('context_analysis', {})}")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "reference_request" and confidence > 0.7:
                logger.info(f"Handling reference request with context: {intent_result.get('context_analysis', {})}")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "rfq_status_check" and confidence > 0.7:
                return await self._handle_rfq_status_inquiry(user, message)
            elif intent == "general_inquiry":
                return await self._handle_general_inquiry(user, message)
            elif confidence < 0.5:
                return await self._handle_clarification_request(user, message)
            else:
                return await self._handle_fallback(user, message)
                
        except Exception as e:
            logger.error(f"Error processing text message: {e}")
            raise
    
    async def _handle_registration_workflow(self, user: User, message: str) -> Dict[str, Any]:
        """Handle user registration process."""
        # Registration logic would be implemented here
        return {"status": "registration_needed", "message": "Please complete registration"}
    
    async def _handle_purchase_intent(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Handle purchase intent - delegate to main ChatService for now."""
        return {"status": "delegate_to_chat_service", "method": "_handle_purchase_intent"}
    
    async def _handle_attachment_decision(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Handle attachment decisions - delegate to main ChatService for now."""
        return {"status": "delegate_to_chat_service", "method": "_handle_attachment_decision"}
    
    async def _handle_rfq_status_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        """Handle RFQ status inquiry requests."""
        try:
            result = await self.rfq_service.process_rfq_status_request(user=user, message=message)
            await self.whatsapp_service.send_message(user.phone_number, result["response_message"])
            return {
                "status": result.get("status"),
                "rfq_ids": result.get("rfq_ids"),
                "rfq_statuses": result.get("rfq_statuses")
            }
        except Exception as e:
            logger.error(f"Error handling RFQ status inquiry: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_general_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        """Handle general inquiries using OpenAI."""
        try:
            context = ChatServiceHelpers.build_context("general_inquiry", message)
            response = await self.response_helpers.generate_contextual_response(
                context, 
                ["How can I help you with your procurement needs today?"], 
                "general_inquiry"
            )
            await self.whatsapp_service.send_message(user.phone_number, response)
            return {"status": "general_inquiry_handled"}
        except Exception as e:
            logger.error(f"Error handling general inquiry: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_clarification_request(self, user: User, message: str) -> Dict[str, Any]:
        """Handle ambiguous messages requiring clarification."""
        try:
            context = ChatServiceHelpers.build_context("clarification", message)
            clarification_questions = [
                "Could you be more specific about what you're looking for?",
                "Are you looking to create an RFQ or check product availability?"
            ]
            response = await self.response_helpers.generate_clarification_response(clarification_questions, 0, context)
            await self.whatsapp_service.send_message(user.phone_number, response)
            return {"status": "clarification_sent"}
        except Exception as e:
            logger.error(f"Error handling clarification request: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_fallback(self, user: User, message: str) -> Dict[str, Any]:
        """Handle messages that don't fit other categories."""
        try:
            context = ChatServiceHelpers.build_context("fallback", message)
            response = await self.response_helpers.generate_contextual_response(
                context,
                ["How can I help you with your procurement needs?"],
                "fallback"
            )
            await self.whatsapp_service.send_message(user.phone_number, response)
            return {"status": "fallback_handled"}
        except Exception as e:
            logger.error(f"Error handling fallback: {e}")
            return {"status": "error", "error": str(e)}