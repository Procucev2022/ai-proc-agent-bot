"""
Intent Switch Handler.

Handles intent switching during active workflows, presenting users with
choices to continue current workflow or switch to new intent.
"""

import logging
from typing import Dict, Any
from app.models import User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.openai_service import OpenAIService
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class IntentSwitchHandler:
    """Handles intent switching during active workflows."""
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.openai_service = OpenAIService()
    
    async def should_handle_intent_switch(self, session: ConversationSession, intent: str, confidence: float) -> bool:
        """
        Determine if we should handle an intent switch during active workflow.
        
        Returns True if:
        1. User has an active workflow (existing entities or pending states)
        2. New intent is different from current workflow 
        3. New intent confidence is high enough (>70%)
        4. Not already in intent switch choice state
        """
        # Check if user has active workflow
        has_active_workflow = (
            len(session.workflow_state.get("extracted_entities", [])) > 0 or
            bool(session.workflow_state.get("incomplete_products")) or
            bool(session.workflow_state.get("pending_combined_rfq")) or
            bool(session.workflow_state.get("pending_rfq")) or
            bool(session.workflow_state.get("pending_optional_rfq")) or
            bool(session.workflow_state.get("pending_optional_combined_rfq")) or
            # ADD THIS LINE - Check for seller workflow
            bool(session.workflow_state.get("seller_workflow_state"))
        )
        print("has active workflow", has_active_workflow)
        
        logger.info(f"Intent switch check: intent={intent}, confidence={confidence}, has_active_workflow={has_active_workflow}")
        logger.info(f"Workflow state keys: {list(session.workflow_state.keys())}")
        logger.info(f"Current workflow_type: {session.workflow_type}")
        
        # Don't handle if no active workflow
        if not has_active_workflow:
            logger.info("Intent switch: No active workflow - returning False")
            return False
        
        # Don't handle if already in intent switch choice state
        if session.workflow_state.get("pending_intent_switch"):
            logger.info("Intent switch: Already in pending_intent_switch - returning False")
            return False
            
        # Don't handle if confidence is too low
        if confidence < 0.7:
            logger.info(f"Intent switch: Confidence too low ({confidence}) - returning False")
            return False
        
        # Handle intent switches for different intents
        current_workflow = session.workflow_type
        logger.info(f"Intent switch: current_workflow={current_workflow}, intent={intent}")
        
        # Special case: if user is in rfq_creation and wants to "buy_something",
        # this could be a new product request that should trigger intent switch
        if (current_workflow and current_workflow.value == "rfq_creation") and intent == "buy_something":
            # This is a potential new product request during existing RFQ collection
            # Let the system present the choice to continue vs start new
            logger.info(f"Potential new product request detected during RFQ creation - returning True")
            return True

        # Check if switching from seller to buyer workflow
        if (current_workflow and current_workflow.value == "seller_rfq_view") and intent == "buy_something":
            logger.info(f"Switching from selling to buying")

            return True

        # Define intent to workflow mapping for other cases
        intent_workflow_map = {
            "rfq_status_check": "rfq_status_check", 
            "general_inquiry": "general_inquiry",
            "sell_something": "vendor_registration"
        }
        
        new_workflow = intent_workflow_map.get(intent)
        
        # Handle switch if new intent is different from current workflow
        if new_workflow and new_workflow != current_workflow:
            logger.info(f"Intent switch detected: {current_workflow} -> {new_workflow}")
            return True
            
        return False
    
    async def handle_intent_switch_choice(self, user: User, session: ConversationSession, 
                                        message: str, new_intent: str, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle intent switch by presenting user with choice options."""
        
        # Get current workflow description
        current_workflow_desc = self._get_workflow_description(session)
        new_intent_desc = self._get_intent_description(new_intent)
        
        # Store intent switch state
        session.workflow_state["pending_intent_switch"] = {
            "new_intent": new_intent,
            "new_intent_message": message,
            "intent_result": intent_result,
            "current_workflow": session.workflow_type,
            "timestamp": utc_now().isoformat()
        }
        
        # Generate choice message using OpenAI
        choice_context = {
            "conversation_stage": "intent_switch_choice",
            "current_request": current_workflow_desc,
            "new_intent": new_intent_desc,
            "user_message": message
        }
        
        choice_response = await self.response_helpers.generate_contextual_response(
            choice_context,
            [
                f"I see you want to {new_intent_desc}. This will abandon your current {current_workflow_desc}.",
                "1. Continue with current request",  
                f"2. {new_intent_desc.capitalize()} (abandon current)"
            ],
            "intent_switch_choice"
        )
        
        await self.whatsapp_service.send_message(user.phone_number, choice_response)
        
        return {"status": "intent_switch_choice_presented"}
    
    async def handle_intent_switch_response(self, user: User, session: ConversationSession, 
                                          message: str) -> Dict[str, Any]:
        """Handle user's response to intent switch choice."""
        
        pending_switch = session.workflow_state.get("pending_intent_switch")
        logger.info(f"Intent switch response: pending_switch={pending_switch}")
        logger.info(f"Pending switch type: {type(pending_switch)}")
        
        # Validate pending_switch exists and is a dictionary
        if not pending_switch:
            return {"status": "no_pending_switch"}
        
        # Fix for JSON deserialization issue - ensure pending_switch is a dictionary
        if not isinstance(pending_switch, dict):
            logger.error(f"pending_intent_switch is not a dictionary, got {type(pending_switch)}: {pending_switch}")
            # Clear corrupted state
            if "pending_intent_switch" in session.workflow_state:
                del session.workflow_state["pending_intent_switch"]
            return {"status": "corrupted_intent_switch_data"}
        
        # Use OpenAI to analyze user's intent switch response
        try:
            analysis_result = self.openai_service.analyze_intent_switch_response(
                message=message,
                pending_switch_context=pending_switch
            )
            
            logger.info(f"OpenAI intent switch analysis: {analysis_result}")
            
            if analysis_result.get("chosen_action") == "continue_current":
                # User wants to continue with current workflow
                logger.info("User chose to continue with current workflow")
                
                # Clear pending switch state
                if "pending_intent_switch" in session.workflow_state:
                    del session.workflow_state["pending_intent_switch"]
                
                # Continue with original workflow
                return {
                    "status": "continue_current_workflow",
                    "continue_with_purchase_intent": True
                }
                
            elif analysis_result.get("chosen_action") == "switch_to_new":
                # User wants to switch to new intent - proceed to switch handling below
                logger.info("User chose to switch to new intent")
                
        except Exception as e:
            logger.error(f"OpenAI intent switch analysis failed: {str(e)}")
            # Fallback to simple keyword matching if OpenAI fails
            message_lower = message.lower()
            if any(keyword in message_lower for keyword in ["1", "continue current", "current", "first"]) and "new" not in message_lower:
                # User wants to continue with current workflow
                logger.info("User chose to continue with current workflow (fallback)")
                
                # Clear pending switch state
                if "pending_intent_switch" in session.workflow_state:
                    del session.workflow_state["pending_intent_switch"]
                
                # Continue with original workflow
                return {
                    "status": "continue_current_workflow", 
                    "continue_with_purchase_intent": True
                }
            # If fallback doesn't match continue, assume they want to switch
        
        # Handle switch to new intent (OpenAI determined switch_to_new or fallback default)
        logger.info(f"Accessing pending_switch keys: {list(pending_switch.keys())}")
        
        # Validate required keys exist before accessing them
        required_keys = ["new_intent", "new_intent_message", "intent_result"]
        missing_keys = [key for key in required_keys if key not in pending_switch]
        
        if missing_keys:
            logger.error(f"Missing required keys in pending_switch: {missing_keys}")
            # Clear the incomplete pending switch state
            del session.workflow_state["pending_intent_switch"]
            return {"status": "error", "error": f"Incomplete intent switch data: missing {missing_keys}"}
        
        new_intent = pending_switch["new_intent"]
        new_message = pending_switch["new_intent_message"]
        intent_result = pending_switch["intent_result"]
        
        # Clear pending switch state BEFORE abandoning workflow (since abandon clears workflow_state)
        if "pending_intent_switch" in session.workflow_state:
            del session.workflow_state["pending_intent_switch"]
        
        # Clear current workflow state (abandon current request)
        self._abandon_current_workflow(session)
        
        return {
            "status": "switch_to_new_intent",
            "new_intent": new_intent,
            "new_message": new_message,
            "intent_result": intent_result
        }
    
    def _get_workflow_description(self, session: ConversationSession) -> str:
        """Get human-readable description of current workflow."""
        workflow_type = session.workflow_type
        
        descriptions = {
            "rfq_creation": "product request",
            "rfq_status_check": "status check", 
            "general_inquiry": "inquiry",
            "authentication": "authentication",
            "registration": "registration"
        }
        
        # Try to get specific product info from entities
        entities = session.workflow_state.get("extracted_entities", [])
        if entities and len(entities) > 0:
            # Get first product name if available
            first_product = entities[0]
            if isinstance(first_product, dict) and first_product.get("product_name"):
                return f"{first_product['product_name']} request"
        
        return descriptions.get(workflow_type, "current request")
    
    def _get_intent_description(self, intent: str) -> str:
        """Get human-readable description of new intent."""
        descriptions = {
            "buy_something": "create a new product request",
            "rfq_status_check": "check order status",
            "general_inquiry": "ask a question", 
            "sell_something": "register as vendor"
        }
        
        return descriptions.get(intent, "handle your request")
    
    def _abandon_current_workflow(self, session: ConversationSession) -> None:
        """Abandon current workflow by clearing workflow state."""
        logger.info(f"Abandoning current workflow: {session.workflow_type}")
        
        # Mark session as abandoned
        session.outcome = 'abandoned'
        
        # Clear workflow state but keep conversation history
        session.workflow_state = {"extracted_entities": []}
        session.workflow_type = None