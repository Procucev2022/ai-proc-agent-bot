"""
Purchase Intent Workflow Handler.

Handles the complete purchase intent workflow including entity extraction,
product processing, optional fields, and confirmation flow.
"""

import logging
from typing import Dict, Any, List
from app.models import WorkflowType, User, ConversationSession
from app.services.entity_service import EntityService
from app.services.workflow_manager import WorkflowManager
from app.services.rfq_service import RFQService
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.response_helpers import ResponseHelpers
from app.utils.rfq_message_formatter import format_rfq_response_message

logger = logging.getLogger(__name__)


class PurchaseWorkflowHandler:
    """Handles purchase intent workflow processing."""
    
    def __init__(self, entity_service: EntityService, rfq_service: RFQService, 
                 whatsapp_service: WhatsAppService, openai_service: OpenAIService,
                 response_helpers: ResponseHelpers):
        self.entity_service = entity_service
        self.rfq_service = rfq_service
        self.whatsapp_service = whatsapp_service
        self.openai_service = openai_service
        self.response_helpers = response_helpers
    
    async def handle_purchase_intent(self, user: User, session: ConversationSession, 
                                   message: str, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Handle purchase intent with data model driven orchestration."""
        try:
            # Extract entities using EntityService
            existing_entities = session.workflow_state.get("extracted_entities", [])
            incomplete_products = session.workflow_state.get("incomplete_products", [])
            
            # Handle pending confirmations
            pending_combined = session.workflow_state.get("pending_combined_rfq")
            pending_single = session.workflow_state.get("pending_rfq")
            
            if pending_combined:
                pending_confirmations = pending_combined.get("products", [])
            elif pending_single:
                pending_confirmations = [pending_single]
            else:
                pending_confirmations = []
            
            # Use incomplete products as context if available
            if incomplete_products:
                context_entities = [prod["entities"] for prod in incomplete_products]
            else:
                context_entities = existing_entities
            
            # Build comprehensive context for EntityService
            entity_context = {
                "extracted_entities": context_entities,
                "workflow_state": session.workflow_state,
                "session_metadata": {
                    "session_id": session.session_id,
                    "workflow_type": session.workflow_type
                },
                "intent_result": intent_result,
            }
            
            # Determine workflow type
            workflow_type = "buy_something"
            if intent_result and intent_result.get("intent") == "modification_request":
                workflow_type = "modification_request"
            elif intent_result and intent_result.get("intent") == "rfq_status_check":
                workflow_type = "rfq_status_check"
            
            # Extract entities
            entity_result = await self.entity_service.extract_entities(message, context=entity_context, workflow_type=workflow_type)
            logger.info(f"EntityService result: {entity_result}")
            
            # Check for modification clarification needed
            if entity_result.get("modification_intent_detected") and entity_result.get("requires_clarification"):
                return await self._handle_modification_clarification(user, session, message, entity_result)
            
            # Handle products array
            if "products" in entity_result and entity_result["products"]:
                products = entity_result["products"]
                return await self._handle_products_array(user, session, message, products)
            
            # Handle single product (backward compatibility)
            elif "entities" in entity_result:
                return await self._handle_single_entity(user, session, message, entity_result)
            
            # No new entities
            return {
                "status": "no_new_entities",
                "message": "Could you provide more details about what you need?"
            }
                
        except Exception as e:
            logger.error(f"Error in purchase intent handler: {e}")
            raise
    
    async def _handle_modification_clarification(self, user: User, session: ConversationSession, 
                                               message: str, entity_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle modification requests that need clarification."""
        modification_context = {
            "conversation_stage": "modification_clarification",
            "workflow_type": "modification_request", 
            "existing_products": entity_result.get("existing_products", []),
            "user_message": message,
            "modification_context": True
        }
        print("modification context", modification_context.get("existing_products"))

        # --- Generate AI-powered clarification response for modification context ---
        existing_products = modification_context.get("existing_products", [])
        if existing_products:
            entities = existing_products[0].get("entities", {})
            global_fields = {
                "deliveryDate": entities.get("deliveryDate"),
                "city": entities.get("city"),
                "state": entities.get("state"),
                "pincode": entities.get("pincode")
            }

            # Generate formatted summary from extracted entities + global fields
            # format_rfq_response_message expects a list of entities, not a single entity
            summary_message = format_rfq_response_message(
                extracted_entities=[entities],  # Wrap in list
                global_fields=global_fields,
                missing_fields=[]
            )

            # Build final clarification message with helpful examples
            clarification_questions = (
                f"{summary_message}\n\n"
                "If you feel you need to modify any details or if you missed adding any item, you can do it now.\n\n"
                "Here are a few examples you can follow:\n\n"
                "• To add an item you missed:  Add 10 motors\n"
                "• To remove an item you added:  Remove Desktop\n"
                "• To change the quantity of an item:  Change laptops to 20\n"
                "• To change the delivery date:  Change delivery date to 30 Dec\n"
                "• To change the delivery location:  Change delivery location to 411005"
            )

        else:
            clarification_questions = "What would you like to change it to?"

        response = await self.response_helpers.generate_clarification_response(
            clarification_questions, 
            completeness=50,
            context=modification_context
        )
        
        await self.whatsapp_service.send_message(user.phone_number, response)
        
        return {
            "status": "modification_clarification_sent",
            "message": "Asked for clarification on modification details"
        }
    
    async def _handle_products_array(self, user: User, session: ConversationSession, 
                                   message: str, products: list) -> Dict[str, Any]:
        """Handle multiple products processing."""
        # This would contain the logic from _handle_products_array
        # Implementation would be moved from ChatService
        pass
    
    async def _handle_single_entity(self, user: User, session: ConversationSession, 
                                   message: str, entity_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle single entity processing (backward compatibility)."""
        # Implementation for single entity handling
        pass