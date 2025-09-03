"""
Summarization helper functions for enhanced chat summarization.

Provides utilities for conversation tracking, data extraction, and preparation
of rich context data for AI-powered session summarization.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from app.utils.datetime_utils import utc_now
import asyncio
from app.models import ConversationSession

logger = logging.getLogger(__name__)

class SummarizationHelpers:
    """Helper functions for enhanced chat summarization."""
    
    @staticmethod
    async def send_and_track_message(whatsapp_service, session, phone_number: str, message: str, message_type: str = "text") -> None:
        """Send message via WhatsApp and track it in conversation history."""
        try:
            # Send the message
            await whatsapp_service.send_message(phone_number, message)
            
            # Track assistant response in conversation history
            SummarizationHelpers.add_to_conversation_history(session, "assistant", message, message_type)
            
        except Exception as e:
            logger.error(f"Error sending and tracking message: {e}")
            # Still try to send the message even if tracking fails
            await whatsapp_service.send_message(phone_number, message)
    
    @staticmethod
    def add_to_conversation_history(session: ConversationSession, sender: str, message: str, message_type: str = "text") -> None:
        """
        Add message to conversation history for better summarization context.
        
        Args:
            session: ConversationSession object
            sender: "user" or "assistant" 
            message: Message content
            message_type: Type of message (text, interactive, etc.)
        """
        try:
            if not session.conversation_history:
                session.conversation_history = {"openai_messages": [], "metadata": []}
                logger.info("Initialized new conversation history")
            
            # Convert sender to OpenAI role format
            role = "assistant" if sender == "assistant" else "user"
            
            # Add to OpenAI-native message format
            session.conversation_history["openai_messages"].append({
                "role": role,
                "content": message
            })
            
            # Keep metadata separately for debugging/audit
            session.conversation_history["metadata"].append({
                "timestamp": utc_now().isoformat(),
                "sender": sender,
                "type": message_type,
                "role": role
            })
            
            message_count = len(session.conversation_history["openai_messages"])
            content_preview = message[:100] + ('...' if len(message) > 100 else '')
            logger.info(f"Stored message {message_count}: {role} -> {content_preview}")
            
            # Keep only last 50 messages to avoid database bloat
            if message_count > 50:
                session.conversation_history["openai_messages"] = session.conversation_history["openai_messages"][-50:]
                session.conversation_history["metadata"] = session.conversation_history["metadata"][-50:]
                logger.info(f"Trimmed conversation history to last 50 messages")
                
        except Exception as e:
            logger.error(f"Error adding to conversation history: {e}")
    
    @staticmethod
    def extract_rich_entities_for_summary(session: ConversationSession) -> Dict[str, Any]:
        """
        Extract rich entity data from session state for summarization.
        
        Captures all the detailed product information, user preferences,
        and workflow state that should be included in AI summaries.
        
        Args:
            session: ConversationSession object
            
        Returns:
            Dictionary of rich entity data for summarization
        """
        try:
            rich_entities = {}
            
            if session.workflow_state:
                # Extract RFQ information
                if session.workflow_state.get("pending_rfq"):
                    rich_entities["rfq_details"] = session.workflow_state["pending_rfq"]
                
                if session.workflow_state.get("pending_combined_rfq"):
                    rich_entities["combined_rfq"] = session.workflow_state["pending_combined_rfq"]
                
                # Extract product information
                if session.workflow_state.get("complete_products"):
                    rich_entities["completed_products"] = session.workflow_state["complete_products"]
                
                if session.workflow_state.get("incomplete_products"):
                    rich_entities["incomplete_products"] = session.workflow_state["incomplete_products"]
                
                if session.workflow_state.get("extracted_entities"):
                    rich_entities["products_discussed"] = session.workflow_state["extracted_entities"]
                
                # Extract user preferences and constraints
                preference_keys = [
                    "user_preferences", "budget_constraints", "delivery_requirements", 
                    "product_specifications", "optional_fields_asked", "pending_confirmations"
                ]
                for key in preference_keys:
                    if session.workflow_state.get(key):
                        rich_entities[key] = session.workflow_state[key]
            
            return rich_entities
            
        except Exception as e:
            logger.error(f"Error extracting rich entities: {e}")
            return {}
    
    @staticmethod
    def prepare_enhanced_summary_data(session: ConversationSession, rich_entities: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare comprehensive data structure for AI summarization.
        
        Combines session metadata, conversation history, and rich entities
        into a structured format optimized for AI summary generation.
        
        Args:
            session: ConversationSession object
            rich_entities: Rich entity data from extract_rich_entities_for_summary
            
        Returns:
            Enhanced data structure for AI summarization
        """
        try:
            # Get conversation messages for context
            conversation_messages = []
            if session.conversation_history and session.conversation_history.get("messages"):
                # Get last 15 messages for summary context
                conversation_messages = session.conversation_history["messages"][-15:]
            
            # Calculate session metrics
            session_duration = None
            if session.created_at and session.completed_at:
                duration = session.completed_at - session.created_at
                session_duration = int(duration.total_seconds() / 60)
            
            # Count interaction metrics
            total_interactions = len(conversation_messages)
            user_messages = [msg for msg in conversation_messages if msg.get("sender") == "user"]
            assistant_messages = [msg for msg in conversation_messages if msg.get("sender") == "assistant"]
            
            # Extract key topics/products
            products_count = 0
            if rich_entities.get("products_discussed"):
                products_count = len(rich_entities["products_discussed"])
            elif rich_entities.get("multiple_rfqs"):
                products_count = len(rich_entities["multiple_rfqs"])
            elif rich_entities.get("rfq_details"):
                products_count = 1
            
            return {
                # Basic session info
                'user_id': session.external_user_id,
                'workflow_type': str(session.workflow_type),
                'outcome': session.outcome.value if hasattr(session.outcome, 'value') else str(session.outcome) if session.outcome else None,
                'session_duration_minutes': session_duration,
                'rfq_ids': [session.rfq_id] if session.rfq_id else [],
                
                # Conversation context
                'conversation_messages': conversation_messages,
                'total_interactions': total_interactions,
                'user_message_count': len(user_messages),
                'assistant_message_count': len(assistant_messages),
                
                # Rich entity data
                'products_discussed': rich_entities.get("products_discussed", []),
                'rfq_details': rich_entities.get("rfq_details"),
                'multiple_rfqs': rich_entities.get("multiple_rfqs"),
                'completed_products': rich_entities.get("completed_products", []),
                'incomplete_products': rich_entities.get("incomplete_products", []),
                'products_count': products_count,
                
                # User preferences and constraints
                'user_preferences': rich_entities.get("user_preferences", {}),
                'budget_constraints': rich_entities.get("budget_constraints", {}),
                'delivery_requirements': rich_entities.get("delivery_requirements", {}),
                'product_specifications': rich_entities.get("product_specifications", {}),
                
                # Workflow progression
                'workflow_stage_reached': SummarizationHelpers._determine_workflow_stage(rich_entities),
                'completion_level': SummarizationHelpers._calculate_completion_level(rich_entities),
                'key_decisions_made': SummarizationHelpers._extract_key_decisions(conversation_messages, rich_entities)
            }
            
        except Exception as e:
            logger.error(f"Error preparing enhanced summary data: {e}")
            return {
                'user_id': session.external_user_id,
                'workflow_type': str(session.workflow_type),
                'outcome': session.outcome.value if hasattr(session.outcome, 'value') else str(session.outcome) if session.outcome else None,
                'extracted_entities': rich_entities,
                'rfq_ids': [session.rfq_id] if session.rfq_id else []
            }
    
    @staticmethod
    def _determine_workflow_stage(rich_entities: Dict[str, Any]) -> str:
        """Determine the furthest workflow stage reached."""
        if rich_entities.get("multiple_rfqs") or rich_entities.get("rfq_details"):
            return "rfq_confirmation"
        elif rich_entities.get("completed_products"):
            return "product_completion"
        elif rich_entities.get("incomplete_products"):
            return "information_gathering"
        elif rich_entities.get("products_discussed"):
            return "initial_discussion"
        else:
            return "conversation_start"
    
    @staticmethod
    def _calculate_completion_level(rich_entities: Dict[str, Any]) -> str:
        """Calculate how complete the user's request was."""
        if rich_entities.get("multiple_rfqs") or rich_entities.get("rfq_details"):
            return "fully_completed"
        elif rich_entities.get("completed_products"):
            return "nearly_completed"
        elif rich_entities.get("incomplete_products"):
            return "partially_completed"
        else:
            return "initial_stage"
    
    @staticmethod
    def _extract_key_decisions(conversation_messages: List[Dict[str, Any]], rich_entities: Dict[str, Any]) -> List[str]:
        """Extract key decisions and preferences from the conversation."""
        decisions = []
        
        try:
            # Look for decision-indicating phrases in user messages
            decision_keywords = [
                "I prefer", "I need", "I want", "yes", "no", "proceed", "confirm",
                "budget", "delivery", "quantity", "specification", "requirement"
            ]
            
            user_messages = [msg for msg in conversation_messages if msg.get("sender") == "user"]
            
            for msg in user_messages[-5:]:  # Last 5 user messages
                content = msg.get("content", "").lower()
                for keyword in decision_keywords:
                    if keyword in content:
                        decisions.append(f"User specified: {msg.get('content', '')[:100]}")
                        break
            
            # Add decisions from rich entities
            if rich_entities.get("user_preferences"):
                decisions.append(f"Preferences: {rich_entities['user_preferences']}")
            
            if rich_entities.get("budget_constraints"):
                decisions.append(f"Budget: {rich_entities['budget_constraints']}")
            
            return decisions[:5]  # Return top 5 decisions
            
        except Exception as e:
            logger.error(f"Error extracting key decisions: {e}")
            return []
    
    @staticmethod
    async def handle_session_completion_async(chat_summary_service, daily_summary_service, session_data: Dict[str, Any]) -> None:
        """
        Handle session completion in background without blocking user response.
        
        Args:
            chat_summary_service: ChatSummaryService instance
            daily_summary_service: DailySummaryService instance
            session_data: Enhanced session data for summarization
        """
        try:
            logger.info(f"Starting background summarization for session {session_data.get('session_id')}")
            
            # Create a mock session object with the enhanced data
            class MockSession:
                def __init__(self, data):
                    self.session_id = data.get('session_id')
                    self.external_user_id = data.get('user_id')
                    self.workflow_type = data.get('workflow_type')
                    self.outcome = data.get('outcome')
                    rfq_ids = data.get('rfq_ids', [])
                    self.rfq_id = rfq_ids[0] if rfq_ids else None
                    self.extracted_entities = data.get('enhanced_entities', {})
                    self.created_at = data.get('created_at')
                    self.completed_at = data.get('completed_at')
            
            mock_session = MockSession(session_data)
            
            # Generate chat session summary with enhanced data
            await chat_summary_service.generate_session_summary(mock_session)
            
            # Generate/update daily summary
            await daily_summary_service.generate_daily_summary(session_data.get('user_id'))
            
            logger.info(f"Background summarization completed for session {session_data.get('session_id')}")
            
        except Exception as e:
            logger.error(f"Error in background session completion: {e}")