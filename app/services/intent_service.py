"""
Intent classification service for routing user messages to appropriate workflows.

This service analyzes user messages to determine their intent using OpenAI.
It focuses solely on intent classification without business logic or routing decisions.

Key responsibilities:
- Classify user messages into predefined intent categories
- Calculate confidence scores for each potential intent
- Handle classification failures with fallback logic
- Return clean classification data for use by orchestrators
"""

import logging
from typing import Dict, Any
from app.services.openai_service import OpenAIService
from app.config import get_settings

logger = logging.getLogger(__name__)

class IntentService:
    """
    Intent classification service for routing user messages.
    
    Analyzes messages to determine user intent using OpenAI.
    Focused solely on classification without business logic.
    """
    
    def __init__(self):
        """Initialize Intent Service with OpenAI integration."""
        self.openai_service = OpenAIService()
        self.settings = get_settings()
        
    def classify_intent(self, message: str, context: dict = None) -> Dict[str, Any]:
        """
        Classify user message intent using OpenAI with conversation context awareness.
        
        Analyzes the message and conversation context to determine
        the user's intent, including modification requests and confirmation responses.
        
        Args:
            message: User message to classify
            context: Optional conversation context including session state, history, and entities
            
        Returns:
            Dict containing:
            - intent: classified intent (buy_something, general_inquiry, modification_request, confirmation_response, reference_request, ambiguous)
            - confidence: confidence score (0-100)
            - reasoning: explanation of classification including context analysis
            - all_intent_scores: scores for all possible intents
            - context_analysis: detailed analysis of conversation context including reference details
            - success: whether classification succeeded
        """
        try:
            # Get classification from OpenAI with context
            classification_result = self.openai_service.classify_intent(message, context)
            
            if not classification_result.get("success", False):
                logger.warning(f"OpenAI classification failed, using fallback")
                return self._get_fallback_classification(message, context)
            
            # Log context-aware classification result
            intent = classification_result['intent']
            confidence = classification_result['confidence']
            context_stage = classification_result.get('context_analysis', {}).get('conversation_stage', 'unknown')
            
            logger.info(f"Intent classified: {intent} (confidence: {confidence}%, stage: {context_stage})")
            
            return classification_result
            
        except Exception as e:
            logger.error(f"Intent classification failed: {str(e)}")
            return self._get_fallback_classification(message, context, error=str(e))
    
    def _get_fallback_classification(self, message: str, context: dict = None, error: str = None) -> Dict[str, Any]:
        """
        Provide fallback classification when OpenAI fails.
        
        Uses simple rule-based classification with context awareness as backup.
        
        Args:
            message: Original user message
            context: Optional conversation context
            error: Optional error message
            
        Returns:
            Fallback classification result
        """
        message_lower = message.lower()
        
        # Default context analysis
        default_context_analysis = {
            "references_existing_data": False,
            "conversation_stage": "unknown",
            "modification_details": {"target_entity": None, "modification_type": None},
            "confirmation_details": {"response_type": None, "has_conditions": False}
        }
        
        # Context-aware fallback classification
        if context:
            # Check for modification requests using context
            has_pending_confirmations = context.get('workflow_state', {}).get('pending_multiple_rfqs') or context.get('workflow_state', {}).get('pending_rfq')
            existing_entities = context.get('extracted_entities', {}) or context.get('workflow_state', {}).get('extracted_entities', [])
            
            # Modification request detection
            modification_keywords = ["change", "modify", "update", "actually", "instead", "make that", "switch to"]
            if (any(keyword in message_lower for keyword in modification_keywords) and 
                (existing_entities or has_pending_confirmations)):
                intent = "modification_request"
                confidence = 70
                default_context_analysis.update({
                    "references_existing_data": True,
                    "conversation_stage": "modifying"
                })
            
            # Confirmation response detection
            elif has_pending_confirmations:
                confirmation_keywords = ["yes", "confirm", "ok", "proceed", "no", "cancel", "decline"]
                if any(keyword in message_lower for keyword in confirmation_keywords):
                    intent = "confirmation_response"
                    confidence = 75
                    response_type = "accept" if any(word in message_lower for word in ["yes", "confirm", "ok", "proceed"]) else "decline"
                    default_context_analysis.update({
                        "conversation_stage": "confirming",
                        "confirmation_details": {"response_type": response_type, "has_conditions": "but" in message_lower or "change" in message_lower}
                    })
                else:
                    # In confirmation stage but not clear response
                    intent = "ambiguous"
                    confidence = 40
            else:
                # No specific context, use general rules
                intent, confidence = self._get_general_fallback_intent(message_lower)
        else:
            # No context available, use general rules
            intent, confidence = self._get_general_fallback_intent(message_lower)
        
        # Build all intent scores
        all_scores = {
            "buy_something": 20,
            "sell_something": 10,
            "general_inquiry": 20,
            "modification_request": 10,
            "confirmation_response": 10,
            "rfq_status_check": 10
        }
        all_scores[intent] = confidence
        
        return {
            "intent": intent,
            "confidence": confidence,
            "reasoning": f"Fallback rule-based classification{' due to error: ' + error if error else ''}",
            "all_intent_scores": all_scores,
            "context_analysis": default_context_analysis,
            "success": False,
            "is_fallback": True
        }
    
    def _get_general_fallback_intent(self, message_lower: str) -> tuple:
        """Get general intent classification without context."""
        if any(keyword in message_lower for keyword in ["status", "track", "progress", "update", "rfq id", "reference", "submitted", "pending", "completed", "check my order", "my request", "my rfq", "order status", "quote status", "vendor responses", "response received", "when will i receive"]):
            return "rfq_status_check", 70
        elif any(keyword in message_lower for keyword in ["do you have", "available", "stock", "inventory", "search", "rfq", "quote", "buy", "purchase", "need to buy", "looking for", "need"]):
            return "buy_something", 60
        elif any(keyword in message_lower for keyword in ["sell", "selling", "offer", "provide", "vendor", "supplier", "want to sell", "have to sell", "we offer", "can supply"]):
            return "sell_something", 60
        elif any(keyword in message_lower for keyword in ["help", "how", "what can", "explain"]):
            return "general_inquiry", 60
        else:
            return "ambiguous", 30