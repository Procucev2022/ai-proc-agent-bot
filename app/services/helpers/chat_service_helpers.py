"""
Helper utilities for ChatService.

Contains pure utility methods that don't modify state and can be
extracted from the main ChatService class for better organization.
"""

import logging
from typing import Dict, Any
from datetime import datetime, date, timedelta
from dateutil import parser as date_parser
from app.models import ConversationSession
from app.schemas.rfq import RFQValidationSchema

logger = logging.getLogger(__name__)


class ChatServiceHelpers:
    """Pure utility methods extracted from ChatService."""
    
    @staticmethod
    def serialize_products_for_session(products_list):
        """Serialize products list for JSON storage by converting only datetime objects to strings."""
        from datetime import datetime
        
        def serialize_obj(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            elif isinstance(obj, dict):
                return {k: serialize_obj(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [serialize_obj(item) for item in obj]
            else:
                return obj
        
        return serialize_obj(products_list)
    
    @staticmethod
    def transform_entities_to_schema(entities: dict) -> dict:
        """Transform extracted entities to RFQValidationSchema format."""
        schema_data = {}
        
        # Map entity extraction fields to schema fields
        if entities.get("projectDesc") or entities.get("description"):
            schema_data["project_desc"] = entities.get("projectDesc") or entities.get("description")
        
        if entities.get("deliveryDate"):
            # Handle both string and datetime objects
            try:
                delivery_date = entities["deliveryDate"]
                if isinstance(delivery_date, datetime):
                    # Already a datetime object, use as-is
                    schema_data["delivery_date"] = delivery_date
                else:
                    # Parse natural language dates using dateutil
                    parsed_date = date_parser.parse(str(delivery_date))
                    schema_data["delivery_date"] = parsed_date
            except Exception as e:
                # If parsing fails, log and leave as None
                logger.warning(f"Failed to parse delivery date '{entities['deliveryDate']}': {e}")
                pass
        
        if entities.get("division"):
            schema_data["division"] = entities["division"]
        
        # Handle items
        if entities.get("description") or entities.get("quantity"):
            item = {}
            if entities.get("description"):
                item["description"] = entities["description"]
            if entities.get("quantity"):
                item["quantity"] = entities["quantity"]
            if entities.get("unitofMeasures"):
                item["unit_of_measures"] = entities["unitofMeasures"]
            
            if item:
                schema_data["items"] = [item]
        
        # Handle delivery locations
        if entities.get("state") or entities.get("city") or entities.get("pincode"):
            location_data = {
                "state": entities.get("state", ""),
                "city": entities.get("city", ""),
                "pincode": entities.get("pincode", "")
            }
            schema_data["delivery_locations"] = [location_data]
        
        # Optional fields
        if entities.get("remarks"):
            schema_data["remarks"] = entities["remarks"]
        if entities.get("brand"):
            schema_data["preferred_brand"] = entities["brand"]

        # Handle attachments
        if entities.get("attachments"):
            schema_data["attachments"] = entities["attachments"]

        return schema_data
    
    @staticmethod
    def create_rfq_schema_from_entities(entities: dict, openai_service=None):
        """Create RFQValidationSchema from entities with division auto-population."""
        schema_data = ChatServiceHelpers.transform_entities_to_schema(entities)
        
        # Check for date validation errors in entities
        has_date_validation_error = bool(entities.get("date_validation_error"))
        if has_date_validation_error:
            schema_data["date_validation_error"] = True
        
        schema = RFQValidationSchema(**schema_data)
        
        # Debug logging for optional questions
        logger.info(f"Schema data: preferred_brand={schema.preferred_brand}, remarks={schema.remarks}, items={bool(schema.items)}, entire_schena_data={schema_data}")
        optional_questions = schema.get_optional_questions()
        logger.info(f"Optional questions generated: {optional_questions}")
        
        return schema
    
    @staticmethod
    def create_combined_rfq_schema_from_multiple_products(products_list: list):
        """Create a single RFQValidationSchema from multiple product entities."""
        if not products_list:
            return None
        
        # Use the first product's common fields (delivery, project desc, etc.)
        base_entities = products_list[0]["entities"]
        schema_data = ChatServiceHelpers.transform_entities_to_schema(base_entities)
        
        # Check for date validation errors in any product
        has_date_validation_error = False
        for prod in products_list:
            entities = prod["entities"]
            if entities.get("date_validation_error"):
                has_date_validation_error = True
                break
        
        if has_date_validation_error:
            schema_data["date_validation_error"] = True
        
        # Combine all products into items array
        combined_items = []
        for prod in products_list:
            entities = prod["entities"]
            if entities.get("description") or entities.get("quantity"):
                item = {}
                if entities.get("description"):
                    item["description"] = entities["description"]
                if entities.get("quantity"):
                    item["quantity"] = entities["quantity"]
                if entities.get("unitofMeasures"):
                    item["unit_of_measures"] = entities["unitofMeasures"]
                if entities.get("brand"):
                    item["brand"] = entities["brand"]
                if entities.get("remarks"):
                    item["remarks"] = entities["remarks"]
                
                if item:
                    combined_items.append(item)
        
        # Update schema with combined items
        schema_data["items"] = combined_items
        
        # Use the project description from the first product or create a combined one
        if not schema_data.get("project_desc") and combined_items:
            descriptions = [item.get("description", "") for item in combined_items]
            schema_data["project_desc"] = f"RFQ for {', '.join(descriptions)}"
        
        return RFQValidationSchema(**schema_data)
    
    @staticmethod
    def build_context(stage: str, message: str = "", entities: dict = None, completeness: int = 0, **kwargs) -> dict:
        """Build standard context dictionary for response generation."""
        context = {
            "conversation_stage": stage,
            "user_message": message,
            "extracted_entities": entities or {},
            "missing_fields": [],
        }
        # Don't include completeness to avoid showing percentages to users
        context.update(kwargs)
        return context

    @staticmethod
    def build_conversation_context(session: ConversationSession, current_message: str) -> dict:
        """
        Build comprehensive conversation context for intent classification and context-aware services.

        Args:
            session: Current conversation session
            current_message: Current user message

        Returns:
            Dict containing full conversation context including session state, history, entities, and user context
        """
        # Extract bot's last message from conversation history for intent classification
        bot_last_message = None
        bot_last_message_type = None
        conversation_history = session.conversation_history or {"openai_messages": [], "metadata": []}

        # Look through recent messages to find the last bot message
        if conversation_history.get("metadata"):
            for msg_data in reversed(conversation_history["metadata"]):
                if msg_data.get("role") == "assistant":
                    bot_last_message = msg_data.get("content", "")
                    bot_last_message_type = msg_data.get("message_type", "text")
                    break

        return {
            'current_message': current_message,
            'session_metadata': {
                'session_id': session.session_id,
                'workflow_type': session.workflow_type,
                'outcome': session.outcome,
                'created_at': session.created_at.isoformat() if session.created_at else None
            },
            'conversation_history': conversation_history,
            'bot_last_message': bot_last_message,
            'bot_last_message_type': bot_last_message_type,
            'workflow_state': session.workflow_state or {},
            'extracted_entities': session.extracted_entities or {},
            'whatsapp_context': session.whatsapp_context or {},
            'session_status': {
                'has_pending_confirmations': bool(
                    session.workflow_state.get("pending_combined_rfq") or
                    session.workflow_state.get("pending_rfq")
                ),
                'has_extracted_entities': bool(
                    session.workflow_state.get("extracted_entities") or
                    session.extracted_entities
                ),
                'has_incomplete_products': bool(session.workflow_state.get("incomplete_products")),
                'has_pending_optional': bool(session.workflow_state.get("pending_optional_rfq")),
                'has_pending_attachment_decision': bool(session.workflow_state.get("pending_attachment_decision")),
                'current_stage': ChatServiceHelpers.determine_conversation_stage(session)
            }
        }
    
    @staticmethod
    def determine_conversation_stage(session: ConversationSession) -> str:
        """Determine current conversation stage based on session state."""
        workflow_state = session.workflow_state or {}

        if workflow_state.get("pending_combined_rfq") or workflow_state.get("pending_rfq"):
            return "confirming"
        elif workflow_state.get("pending_optional_rfq") or workflow_state.get("pending_attachment_decision"):
            return "optional_fields"
        elif workflow_state.get("incomplete_products"):
            return "collecting_details"
        elif workflow_state.get("extracted_entities"):
            entities = workflow_state["extracted_entities"]
            if isinstance(entities, list) and entities:
                return "processing_multiple"
            elif isinstance(entities, dict) and entities:
                return "processing_single"
            else:
                return "collecting"
        elif session.outcome:
            return "completed"
        else:
            return "collecting"

    @staticmethod
    def find_most_relevant_message_after_auth(session: ConversationSession,
                                             current_message: str, auth_result: dict = None) -> str:
        """
        Find the most relevant message to process after authentication/registration completion.

        Priority:
        1. Last message with 'buy_something' or 'sell_something' intent
        2. Fall back to original_message from auth_result or session
        3. Fall back to current_message

        Args:
            session: Current conversation session
            current_message: Current user message
            auth_result: Authentication result containing potential original_message

        Returns:
            str: The most relevant message to process
        """
        try:
            # Get original message from auth result or session
            original_message = None
            if isinstance(auth_result, dict):
                original_message = auth_result.get("original_message")
            if not original_message and session.workflow_state:
                original_message = session.workflow_state.get("original_message")

            # Get conversation history
            conversation_history = getattr(session, 'conversation_history', {})
            messages = conversation_history.get('messages', []) if conversation_history else []

            if not messages:
                logger.info("No conversation history - using original or current message")
                return original_message if original_message else current_message

            # Look for last user message with buy_something or sell_something intent (newest first)
            target_intents = ["buy_something", "sell_something"]

            for message in reversed(messages):
                if (message.get("sender") == "user" and
                    message.get("intent") in target_intents):
                    logger.info(f"Found message with {message.get('intent')} intent: {message.get('content')[:50]}...")
                    return message.get("content")

            # No buy/sell intent found - fall back to original message
            if original_message:
                logger.info(f"No buy/sell intent found - using original message: {original_message[:50]}...")
                return original_message

            # Final fallback to current message
            logger.info(f"No original message - using current message: {current_message[:50]}...")
            return current_message

        except Exception as e:
            logger.error(f"Error finding most relevant message after auth: {e}")
            # Safe fallback
            if original_message:
                return original_message
            return current_message