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
from app.data.faq_config import FULL_FAQ_CONTEXT

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
        
    async def classify_intent(self, message: str, context: dict = None) -> Dict[str, Any]:
        """
        Classify user message intent using OpenAI with conversation context awareness.

        TRACK 2 PRIORITY ORDER (checked before OpenAI):
        1. Exit/cancel keywords
        2. Format modification (if awaiting flag set)
        3. Interruptions (FAQ/greeting/help during RFQ)
        4. Regular OpenAI classification

        Args:
            message: User message to classify
            context: Optional conversation context including session state, history, and entities

        Returns:
            Dict containing:
            - intent: classified intent (buy_something, general_inquiry, modification_request, confirmation_response, reference_request, ambiguous, contextual_reference, session_inquiry, exit_system, cancel_workflow, account_switch, register_account, alternative_request, support, faq, format_modification)
            - confidence: confidence score (0-100)
            - reasoning: explanation of classification including context analysis
            - all_intent_scores: scores for all possible intents
            - context_analysis: detailed analysis of conversation context including reference details
            - success: whether classification succeeded
            - contextual_response: ready-to-send response (if contextual intent detected)
            - entity_updates: extracted entities from contextual reference (if applicable)
            - should_handle_directly: whether ChatService should handle this directly
            - should_update_entities: whether entities should be updated from contextual reference
        """
        try:
            # Handle non-string message types early
            if isinstance(message, dict):
                message_str = "image attachment"
            elif not isinstance(message, str):
                message_str = str(message)
            else:
                message_str = message
            
            # Get session from context if available (for Track 2 checks)
            session = context.get('session') if context else None

            # PRIORITY 1: Check for exit/cancel keywords (Track 2)
            if self.detect_exit_keywords(message_str):
                return {
                    "intent": "exit_system",
                    "confidence": 95,
                    "reasoning": "User wants to exit or cancel workflow",
                    "success": True,
                    "context_analysis": {"conversation_stage": "exiting"}
                }

            # PRIORITY 2: Check for format modification (Track 2)
            if session and self.detect_format_modification_intent(message_str, session):
                return {
                    "intent": "format_modification",
                    "confidence": 98,
                    "reasoning": "User is providing formatted modification response",
                    "success": True,
                    "context_analysis": {"conversation_stage": "modifying"}
                }

            # PRIORITY 3: Check for interruptions (Track 2)
            if session:
                interruption_result = self.detect_interruption_intent(message_str, session)
                if interruption_result["is_interruption"]:
                    interruption_type = interruption_result["interruption_type"]
                    return {
                        "intent": interruption_type,  # "faq", "greeting", or "help"
                        "confidence": 85,
                        "reasoning": f"User interrupted RFQ flow with {interruption_type}",
                        "success": True,
                        "is_interruption": True,
                        "context_analysis": {"conversation_stage": "interrupted"}
                    }

            # PRIORITY 4: Regular OpenAI classification
            # Get classification from OpenAI (FAQ intent can be detected from prompt alone, no need for full FAQ context)
            classification_result = await self.openai_service.classify_intent(message, context)

            if not classification_result.get("success", False):
                logger.warning(f"OpenAI classification failed, using fallback")
                return self._get_fallback_classification(message, context)
            
            # Log context-aware classification result
            intent = classification_result['intent']
            confidence = classification_result['confidence']
            context_stage = classification_result.get('context_analysis', {}).get('conversation_stage', 'unknown')
            
            logger.info(f"Intent classified: {intent} (confidence: {confidence}%, stage: {context_stage})")
            
            # Handle contextual intents with intelligent responses
            if intent in ['contextual_reference', 'session_inquiry', 'alternative_request'] and confidence > 60:
                return await self._handle_contextual_intent(intent, message, context, classification_result)

            # Handle exit intent - return immediately without contextual processing
            if intent == 'exit_system' and confidence > 50:
                return classification_result
            
            return classification_result
            
        except Exception as e:
            logger.error(f"Intent classification failed: {str(e)}")
            return self._get_fallback_classification(message, context, error=str(e))
    
    def _get_fallback_classification(self, message: str, context: dict = None, error: str = None) -> Dict[str, Any]:
        """
        Provide fallback classification when OpenAI fails.

        Uses simple rule-based classification with context awareness as backup.

        Args:
            message: Original user message (can be string or dict for multimodal content)
            context: Optional conversation context
            error: Optional error message

        Returns:
            Fallback classification result
        """
        # Handle non-string message content (e.g., image data)
        if isinstance(message, dict):
            message_lower = "image attachment"
        elif not isinstance(message, str):
            message_lower = str(message).lower()
        else:
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
            # Extract key context information
            has_pending_confirmations = context.get('workflow_state', {}).get('pending_combined_rfq') or context.get('workflow_state', {}).get('pending_rfq')
            has_incomplete_products = context.get('session_status', {}).get('has_incomplete_products', False)
            existing_entities = context.get('extracted_entities', {}) or context.get('workflow_state', {}).get('extracted_entities', [])
            bot_last_message = context.get('bot_last_message', '')

            # Clarification response detection (highest priority)
            # If user has incomplete products, bot just sent a message, and user isn't using modification language
            modification_keywords = ["change", "modify", "update", "actually", "instead", "make that", "switch to"]
            has_modification_keywords = any(keyword in message_lower for keyword in modification_keywords)

            if (has_incomplete_products and bot_last_message and not has_modification_keywords):
                intent = "buy_something"  # Continue collection workflow
                confidence = 85
                default_context_analysis.update({
                    "references_existing_data": True,
                    "conversation_stage": "collecting"
                })

            # Modification request detection
            elif (has_modification_keywords and (existing_entities or has_pending_confirmations)):
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
            "bfs_search": 10,
            "account_switch": 5,
            "register_account": 5,
            "general_inquiry": 20,
            "modification_request": 10,
            "confirmation_response": 10,
            "rfq_status_check": 10,
            "reference_request": 5,
            "contextual_reference": 5,
            "session_inquiry": 5,
            "exit_system": 5,
            "cancel_workflow": 5,
            "alternative_request": 5,
            "support": 10,
            "greeting": 10,
            "ambiguous": 20
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
        # Check for exit keywords first (definitive exit)
        if any(keyword in message_lower for keyword in ["exit", "quit", "bye", "goodbye", "logout", "log out"]):
            return "exit_system", 90
        # Check for cancel keywords (workflow cancellation)
        elif any(keyword in message_lower for keyword in ["cancel", "start over", "restart", "clear", "forget"]):
            return "cancel_workflow", 85
        # Check for greeting messages
        elif any(keyword in message_lower for keyword in ["hello", "hi", "hey", "good morning", "good afternoon", "good evening", "greetings", "hola", "namaste"]):
            return "general_inquiry", 80
        elif any(keyword in message_lower for keyword in ["support", "contact support", "customer service", "technical support", "help me", "need help", "assistance", "contact customer"]):
            return "support", 80
        elif any(keyword in message_lower for keyword in ["status", "track", "progress", "update", "rfq id", "reference", "submitted", "pending", "completed", "check my order", "my request", "my rfq", "order status", "quote status", "vendor responses", "response received", "when will i receive"]):
            return "rfq_status_check", 70
        elif any(keyword in message_lower for keyword in ["do you have", "available", "stock", "inventory", "bfs", "buy from stock", "immediate", "urgent", "right now", "today", "asap", "quick delivery", "buy directly", "purchase now", "direct purchase", "what's in stock", "show me stock", "search inventory"]):
            return "bfs_search", 70
        elif any(keyword in message_lower for keyword in ["search", "rfq", "quote", "buy", "purchase", "need to buy", "looking for", "need"]):
            return "buy_something", 60
        elif any(keyword in message_lower for keyword in ["sell", "selling", "offer", "provide", "vendor", "supplier", "want to sell", "have to sell", "we offer", "can supply"]):
            return "sell_something", 60
        # Check for FAQ keywords (high priority) - enhanced detection
        elif any(keyword in message_lower for keyword in [
            "what is", "how does", "how to", "what are", "explain", "tell me about", 
            "information about", "details about", "faq", "frequently asked", 
            "question about", "help with", "is there any charge", "cost to use", 
            "free to use", "pricing", "fees", "charges", "benefits of", 
            "how can i", "what can", "do you provide", "tell me more","what can"
        ]):
            return "general_inquiry", 85
        elif any(keyword in message_lower for keyword in ["hi","hello","good evening"]):
            return "greeting", 60
        # Check for mixed intent (both buy and sell keywords)
        elif (any(buy_word in message_lower for buy_word in ["buy", "purchase", "need", "looking for"]) and
              any(sell_word in message_lower for sell_word in ["sell", "selling", "offer", "provide", "supply"])):
            return "ambiguous", 70
        else:
            return "ambiguous", 30
    
    async def _handle_contextual_intent(self, intent: str, message: str, context: dict, classification_result: dict) -> Dict[str, Any]:
        """
        Handle contextual intents by generating intelligent responses and extracting entities.
        
        Args:
            intent: Detected contextual intent
            message: User's message
            context: Conversation context
            classification_result: Original classification result
            
        Returns:
            Enhanced classification result with contextual response and entity updates
        """
        try:
            # Use comprehensive contextual interaction handler for all contextual intents
            contextual_data = await self.openai_service.handle_contextual_interaction(
                message=message,
                conversation_history=context.get('conversation_history', {}),
                workflow_state=context.get('workflow_state', {}),
                extracted_entities=context.get('extracted_entities', [])
            )
            
            # Enhance the classification result with contextual data
            enhanced_result = classification_result.copy()
            enhanced_result.update({
                'contextual_response': contextual_data.get('response', 'How can I help you?'),
                'contextual_actions': contextual_data.get('actions', []),
                'context_understanding': contextual_data.get('context_understanding', {}),
                'should_handle_directly': True,
                'should_update_entities': any(action.get('type') == 'update_entities' for action in contextual_data.get('actions', [])),
                'should_change_workflow': any(action.get('type') in ['change_workflow_state', 'change_workflow_type'] for action in contextual_data.get('actions', []))
            })
            
            logger.info(f"Generated contextual response for {intent}: {len(enhanced_result['contextual_response'])} chars")
            return enhanced_result
            
        except Exception as e:
            logger.error(f"Error handling contextual intent {intent}: {e}")
            # Return basic classification result with fallback response
            fallback_result = classification_result.copy()
            fallback_result.update({
                'contextual_response': 'I understand you\'re referring to our conversation. Could you please clarify what you need?',
                'contextual_actions': [],
                'context_understanding': {
                    'user_intent': 'unclear',
                    'referenced_data': [],
                    'confidence': 30
                },
                'should_handle_directly': True,
                'should_update_entities': False,
                'should_change_workflow': False
            })
            return fallback_result

    # ===== TRACK 2 INTENT DETECTION METHODS =====

    def detect_format_modification_intent(self, message: str, session) -> bool:
        """
        Detect if message should be treated as format modification intent.

        Track 2 responsibility: Check if we're awaiting modification.
        If yes, route to format modification handler (Track 1 will validate).

        Args:
            message: User message
            session: Conversation session

        Returns:
            True if this should be treated as format modification
        """
        from app.services.workflow_manager import WorkflowManager

        # Check if awaiting modification flag is set
        is_awaiting, subtype = WorkflowManager.is_awaiting_modification(session)
        return is_awaiting

    def detect_interruption_intent(self, message: str, session) -> Dict[str, Any]:
        """
        Detect if message is an interruption (FAQ/greeting/help) during RFQ flow.

        Args:
            message: User message
            session: Conversation session

        Returns:
            Dict with:
            - is_interruption: bool
            - interruption_type: "faq" | "greeting" | "help" | None
        """
        from app.models import WorkflowType
        from app.services.workflow_manager import WorkflowManager

        # Only check for interruptions if in active RFQ workflow
        workflow_type = WorkflowManager.get_workflow_type(session)
        if workflow_type not in [WorkflowType.rfq_creation, WorkflowType.buy_something]:
            return {"is_interruption": False, "interruption_type": None}

        # Check if actually started RFQ (has delivery or items data)
        has_delivery = WorkflowManager.get_delivery_details(session) is not None
        has_items = session.workflow_state and session.workflow_state.get('extracted_entities')

        if not (has_delivery or has_items):
            # Not yet started RFQ, no interruption
            return {"is_interruption": False, "interruption_type": None}

        message_lower = message.lower().strip()

        # Check for greeting patterns
        greeting_patterns = [
            "hello", "hi", "hey", "good morning", "good afternoon",
            "good evening", "namaste"
        ]
        if any(pattern in message_lower for pattern in greeting_patterns):
            return {
                "is_interruption": True,
                "interruption_type": "greeting"
            }

        # Check for help patterns
        help_patterns = [
            "help", "how to", "what can you do", "assist me"
        ]
        if any(pattern in message_lower for pattern in help_patterns):
            return {
                "is_interruption": True,
                "interruption_type": "help"
            }

        # Check for FAQ patterns (common question words)
        faq_patterns = [
            "what is", "how does", "why", "when", "where",
            "can i", "is it possible", "do you"
        ]
        if any(pattern in message_lower for pattern in faq_patterns):
            return {
                "is_interruption": True,
                "interruption_type": "faq"
            }

        return {"is_interruption": False, "interruption_type": None}

    def detect_exit_keywords(self, message: str) -> bool:
        """
        Detect if message contains exit/cancel keywords.

        Args:
            message: User message

        Returns:
            True if contains exit keywords
        """
        message_lower = message.lower().strip()

        exit_keywords = [
            "exit", "quit", "cancel", "stop", "abort",
            "start over", "restart", "reset"
        ]

        return any(keyword in message_lower for keyword in exit_keywords)

    def should_allow_exit(self, session) -> bool:
        """
        Check if exit is allowed from current workflow step.

        For Track 2, exit is always allowed (may show confirmation).

        Args:
            session: Conversation session

        Returns:
            True (always allowed for now)
        """
        # Track 2: Always allow exit, but may show confirmation in handler
        return True