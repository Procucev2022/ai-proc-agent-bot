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
        
    async def classify_intent(self, message: str, context: dict = None,user_phone=None) -> Dict[str, Any]:
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

            # Get session from context if available (for Track 2 checks)
            session = context.get('session') if context else None

            if isinstance(message, str):
                msg_clean = message.strip().lower()

                # Session-sensitive interruptions take precedence over greeting shortcuts.
                if session:
                    interruption_result = self.detect_interruption_intent(message, session)
                    if interruption_result["is_interruption"]:
                        interruption_type = interruption_result["interruption_type"]
                        return {
                            "intent": interruption_type,
                            "confidence": 85,
                            "reasoning": f"User interrupted RFQ flow with {interruption_type}",
                            "success": True,
                            "is_interruption": True,
                            "context_analysis": {"conversation_stage": "interrupted"}
                        }

                # Fast-path 1: Simple Greetings (0.001s response time)
                if msg_clean in ["hi", "hello", "hey", "hi!", "hello!", "hey!", "start", "menu", "help"]:
                    logger.info(f"[FAST_PATH] Simple greeting matched for '{message}'")
                    return {
                        "intent": "greeting",
                        "confidence": 100,
                        "reasoning": "Fast-path local matching: Simple greeting keyword",
                        "success": True,
                        "context_analysis": {"conversation_stage": "initial"}
                    }

                # Fast-path 2: Numeric & Menu Choices (0.001s response time)
                if msg_clean in ["1", "2", "3", "buy", "sell", "create_rfq", "new_rfq", "raise_rfq", "create new rfq"]:
                    is_rfq = msg_clean in ["buy", "create_rfq", "new_rfq", "raise_rfq", "create new rfq"]
                    target_intent = "buy_something" if is_rfq else ("sell_something" if msg_clean == "sell" else "ambiguous")
                    logger.info(f"[FAST_PATH] Menu choice matched for '{message}' -> {target_intent}")
                    return {
                        "intent": target_intent,
                        "confidence": 100 if is_rfq else 98,
                        "reasoning": f"Fast-path local matching: Menu option '{msg_clean}'",
                        "success": True,
                        "context_analysis": {}
                    }

                # PRIORITY 2: Check for format modification (Track 2)
                if session and self.detect_format_modification_intent(message, session):
                    return {
                        "intent": "format_modification",
                        "confidence": 98,
                        "reasoning": "User is providing formatted modification response",
                        "success": True,
                        "context_analysis": {"conversation_stage": "modifying"}
                    }

                # PRIORITY 3: Check for interruptions (Track 2)
                if session:
                    interruption_result = self.detect_interruption_intent(message, session)
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
            classification_result = await self.openai_service.classify_intent(message, context,user_phone)

            # If OpenAI service returned None
            if classification_result is None:
                logger.error(f"OpenAI classify_intent returned None, using fallback")
                return await self._get_fallback_classification(
                    message, context, error="OpenAI service returned None", user_phone=user_phone
                )
            
            # Check for timeout handling BEFORE checking success (timeout takes precedence)
            if classification_result.get("timeout_handled"):
                logger.info(f"Rate limit timeout was handled, passing through to chat service")
                return classification_result
            
            if not classification_result.get("success", False):
                logger.warning(f"OpenAI classification failed, using fallback")
                return await self._get_fallback_classification(
                    message, context, error="OpenAI classification unsuccessful", user_phone=user_phone
                )
            
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
            return await self._get_fallback_classification(message, context, error=str(e),user_phone=user_phone)
    
    async def _get_fallback_classification(self, message: str, context: dict = None, error: str = None,
                                           user_phone=None) -> Dict[str, Any]:
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
        # Use cancel service's method to send the appropriate message with buttons
        from app.services.cancel_service import CancelService
        user = (context or {}).get('user_role')
        if user:
            cancel_service = CancelService()
            await cancel_service._send_cancellation_message(user_phone=user_phone, user_type=user,custom_message="Currently, we are facing some technical issues. The team is actively working to get QUA up and running.\n"
            "We apologise for the inconvenience caused and request you to please try again after a while.\n"
            f"In case of anything urgent, feel free to reach us at {self.settings.support_contact_info}")

        # Always return a usable classification. Returning None here made
        # classify_intent() resolve to None, so chat_service's `.get()` call
        # raised an AttributeError and a genuine RFQ request was rebound to a
        # zero-confidence greeting. The rule-based classifier below already
        # existed for exactly this case but was never reached.
        intent, confidence = self._get_general_fallback_intent(self._message_to_text(message).lower())

        return {
            "intent": intent,
            "confidence": confidence,
            "relevant_message": None,
            "irrelevant_message": None,
            "all_intent_scores": {intent: confidence},
            "context_analysis": {
                "references_existing_data": False,
                "conversation_stage": "unknown",
            },
            "reasoning": f"Rule-based fallback classification (OpenAI unavailable: {error})",
            "suggested_clarification": None,
            "success": False,
            "fallback_used": True,
        }

    @staticmethod
    def _message_to_text(message: Any) -> str:
        """
        Reduce a message of any supported shape to plain text for keyword matching.

        Messages reach the classifier as a string, as a content dict, or as a list
        of content parts (multimodal payloads), so the rule-based fallback cannot
        assume str.
        """
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            return str(message.get('text') or message.get('content') or '')
        if isinstance(message, list):
            parts = [str(part.get('text', '')) for part in message if isinstance(part, dict)]
            return ' '.join(part for part in parts if part)
        return str(message or '')
    
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
            "exit", "quit", "abort",
            "start over", "reset"
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