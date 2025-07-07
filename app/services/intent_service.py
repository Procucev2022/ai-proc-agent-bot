"""
Intent classification service for routing user messages to appropriate workflows.

This service analyzes user messages to determine their intent and routes them
to the correct workflow handler (product search, RFQ creation, or general inquiry).
It uses OpenAI's language models to classify messages with confidence scores
and threshold-based routing to ensure accurate workflow selection.

Key responsibilities:
- Classify user messages into predefined intent categories
- Calculate confidence scores for each potential intent
- Apply threshold-based routing logic for reliable classification
- Handle ambiguous messages that don't meet confidence thresholds
- Analyze conversation context to improve classification accuracy
- Support multiple intent types: buy_something, general_inquiry
"""

import logging
from typing import Dict, Any, List
from app.services.openai_service import OpenAIService

logger = logging.getLogger(__name__)

class IntentService:
    """
    Intent classification service for routing user messages.
    
    Analyzes messages to determine user intent and route to appropriate workflows.
    Uses confidence-based thresholds for reliable classification.
    """
    
    # Confidence thresholds for RFQ-first workflow
    THRESHOLDS = {
        "buy_something": 75,
        "general_inquiry": 60,
        "ambiguous": 60  # Below this threshold = ambiguous
    }
    
    def __init__(self):
        """Initialize Intent Service with OpenAI integration."""
        self.openai_service = OpenAIService()
        
    def classify_intent(self, message: str, context: dict = None) -> Dict[str, Any]:
        """
        Classify user message intent with confidence-based routing.
        
        Analyzes the message and conversation context to determine
        the most appropriate workflow to handle the user's request.
        
        Args:
            message: User message to classify
            context: Optional conversation context for better classification
            
        Returns:
            Dict containing:
            - intent: classified intent (buy_something, general_inquiry, ambiguous)
            - confidence: confidence score (0-100)
            - routing_decision: final routing decision after threshold check
            - requires_clarification: bool indicating if clarification is needed
            - reasoning: explanation of classification
            - next_action: what should happen next (proceed_to_rfq, request_clarification, general_chat)
        """
        try:
            # Get classification from OpenAI
            classification_result = self.openai_service.classify_intent(message, context)
            
            if not classification_result.get("success", False):
                logger.warning(f"OpenAI classification failed, using fallback")
                return self._get_fallback_classification(message)
            
            # Apply threshold-based routing
            routing_result = self._route_by_threshold(classification_result)
            
            # Determine next action based on routing decision
            next_action = self._determine_next_action(routing_result)
            
            # Enhance with additional metadata
            final_result = {
                **classification_result,
                "routing_decision": routing_result["final_intent"],
                "threshold_met": routing_result["threshold_met"],
                "requires_clarification": routing_result["requires_clarification"],
                "next_action": next_action,
                "applied_threshold": routing_result.get("applied_threshold")
            }
            
            logger.info(f"Intent classified: {final_result['intent']} (confidence: {final_result['confidence']}%, next_action: {next_action})")
            
            return final_result
            
        except Exception as e:
            logger.error(f"Intent classification failed: {str(e)}")
            return self._get_fallback_classification(message, error=str(e))
        
    def _route_by_threshold(self, classification_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Apply threshold-based routing logic with multi-intent analysis.
        
        Determines final routing decision based on confidence thresholds
        and detects ambiguous cases where multiple intents have similar scores.
        
        Args:
            classification_result: Result from OpenAI classification
            
        Returns:
            Dict with routing decision and metadata
        """
        intent = classification_result.get("intent")
        confidence = classification_result.get("confidence", 0)
        all_scores = classification_result.get("all_intent_scores", {})
        
        # Check for multi-intent ambiguity
        ambiguity_detected = self._detect_multi_intent_ambiguity(all_scores)
        
        if ambiguity_detected:
            # Multiple intents have similar high scores - requires clarification
            return {
                "final_intent": "ambiguous",
                "threshold_met": False,
                "requires_clarification": True,
                "applied_threshold": "multi_intent_ambiguity",
                "ambiguous_intents": ambiguity_detected
            }
        
        # Check if confidence meets threshold for the classified intent
        threshold = self.THRESHOLDS.get(intent, 100)
        threshold_met = confidence >= threshold
        
        if threshold_met:
            # Confidence meets threshold, proceed with classified intent
            final_intent = intent
            requires_clarification = False
        else:
            # Confidence below threshold, classify as ambiguous
            final_intent = "ambiguous"
            requires_clarification = True
            
        return {
            "final_intent": final_intent,
            "threshold_met": threshold_met,
            "requires_clarification": requires_clarification,
            "applied_threshold": threshold
        }
        
    def _determine_next_action(self, routing_result: Dict[str, Any]) -> str:
        """
        Determine what action to take next based on routing decision.
        
        Args:
            routing_result: Result from threshold-based routing
            
        Returns:
            Next action to take: proceed_to_entities, request_clarification, or general_chat
        """
        final_intent = routing_result["final_intent"]
        requires_clarification = routing_result["requires_clarification"]
        
        if requires_clarification:
            return "request_clarification"
        elif final_intent == "buy_something":
            return "proceed_to_rfq"
        elif final_intent == "general_inquiry":
            return "general_chat"
        else:
            return "request_clarification"
    
    def get_clarification_message(self, classification_result: Dict[str, Any]) -> str:
        """
        Generate appropriate clarification message for ambiguous intents.
        
        This is used when we cannot determine user intent with sufficient confidence.
        After clarification, we'll reclassify and proceed to entity extraction if intent becomes clear.
        
        Args:
            classification_result: Result from intent classification
            
        Returns:
            Clarification message to send to user
        """
        # Use suggested clarification from OpenAI if available
        if classification_result.get("suggested_clarification"):
            return classification_result["suggested_clarification"]
        
        # Check if this is a multi-intent ambiguity case
        if classification_result.get("ambiguous_intents"):
            ambiguous_intents = classification_result["ambiguous_intents"]
            if "buy_something" in ambiguous_intents and "general_inquiry" in ambiguous_intents:
                return ("I can help you with that! To provide the best assistance, could you clarify:\n\n"
                        "• Are you looking to purchase or procure something?\n"
                        "• Or do you need general information about our services?")
        
        # Generate clarification to help determine intent
        return ("I'd like to help you! Could you clarify what you're looking for?\n\n"
                "• Are you looking to purchase or procure something?\n"
                "• Do you need general help or information about our services?")
    
    def is_high_confidence_classification(self, classification_result: Dict[str, Any]) -> bool:
        """
        Check if classification has high confidence and can proceed to entity extraction.
        
        Args:
            classification_result: Result from intent classification
            
        Returns:
            True if classification is confident enough to proceed to entity extraction
        """
        return (classification_result.get("threshold_met", False) and 
                classification_result.get("next_action") == "proceed_to_entities")
    
    def should_proceed_to_entities(self, classification_result: Dict[str, Any]) -> bool:
        """
        Check if we should proceed directly to entity extraction.
        
        Args:
            classification_result: Result from intent classification
            
        Returns:
            True if we should start collecting entities
        """
        return classification_result.get("next_action") == "proceed_to_rfq"
    
    def should_request_clarification(self, classification_result: Dict[str, Any]) -> bool:
        """
        Check if we should request clarification from user.
        
        Args:
            classification_result: Result from intent classification
            
        Returns:
            True if clarification is needed
        """
        return classification_result.get("next_action") == "request_clarification"
    
    def _detect_multi_intent_ambiguity(self, all_scores: Dict[str, float]) -> List[str]:
        """
        Detect when multiple intents have similar high scores.
        
        Args:
            all_scores: Dictionary of intent scores
            
        Returns:
            List of ambiguous intent names, empty if no ambiguity
        """
        if not all_scores:
            return []
        
        # Find intents with scores above minimum threshold
        high_scoring_intents = []
        for intent, score in all_scores.items():
            if score >= 60:  # Minimum score to be considered
                high_scoring_intents.append((intent, score))
        
        if len(high_scoring_intents) < 2:
            return []
        
        # Sort by score descending
        high_scoring_intents.sort(key=lambda x: x[1], reverse=True)
        
        # Check if top two scores are within 15 points and both above 60
        top_score = high_scoring_intents[0][1]
        second_score = high_scoring_intents[1][1]
        
        if (top_score - second_score) <= 15 and second_score >= 60:
            return [intent for intent, score in high_scoring_intents[:2]]
        
        return []
    
    def _get_fallback_classification(self, message: str, error: str = None) -> Dict[str, Any]:
        """
        Provide fallback classification when OpenAI fails.
        
        Uses simple rule-based classification as backup.
        
        Args:
            message: Original user message
            error: Optional error message
            
        Returns:
            Fallback classification result
        """
        message_lower = message.lower()
        
        # Simple keyword-based fallback
        if any(keyword in message_lower for keyword in ["do you have", "available", "stock", "inventory", "search", "rfq", "quote", "buy", "purchase", "need to buy", "looking for", "need"]):
            intent = "buy_something"
            confidence = 60
        elif any(keyword in message_lower for keyword in ["help", "how", "what can", "explain"]):
            intent = "general_inquiry"
            confidence = 60
        else:
            intent = "ambiguous"
            confidence = 30
        
        routing_result = self._route_by_threshold({"intent": intent, "confidence": confidence})
        next_action = self._determine_next_action(routing_result)
        
        return {
            "intent": intent,
            "confidence": confidence,
            "reasoning": f"Fallback rule-based classification{' due to error: ' + error if error else ''}",
            "suggested_clarification": None,
            "success": False,
            "routing_decision": routing_result["final_intent"],
            "threshold_met": routing_result["threshold_met"],
            "requires_clarification": routing_result["requires_clarification"],
            "next_action": next_action,
            "applied_threshold": routing_result.get("applied_threshold"),
            "is_fallback": True
        }