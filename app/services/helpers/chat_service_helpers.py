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
            # Parse natural language dates using dateutil
            try:
                parsed_date = date_parser.parse(entities["deliveryDate"])
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
        
        return schema_data
    
    @staticmethod
    def create_rfq_schema_from_entities(entities: dict, openai_service=None):
        """Create RFQValidationSchema from entities with division auto-population."""
        schema_data = ChatServiceHelpers.transform_entities_to_schema(entities)
        schema = RFQValidationSchema(**schema_data)
        
        # Attempt division auto-population if not already set and OpenAI service is available
        if not schema.division and openai_service and entities:
            logger.info("Attempting division auto-population...")
            try:
                success = schema.auto_populate_division(openai_service, entities)
                if success:
                    logger.info(f"Division auto-populated: {schema.division} (confidence: {schema.division_confidence}%)")
                else:
                    logger.warning("Division auto-population failed")
            except Exception as e:
                logger.error(f"Error during division auto-population: {e}")
        
        # Debug logging for optional questions
        logger.info(f"Schema data: preferred_brand={schema.preferred_brand}, remarks={schema.remarks}, items={bool(schema.items)}")
        optional_questions = schema.get_optional_questions()
        logger.info(f"Optional questions generated: {optional_questions}")
        
        return schema
    
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
        return {
            'current_message': current_message,
            'session_metadata': {
                'session_id': session.session_id,
                'workflow_type': session.workflow_type,
                'outcome': session.outcome,
                'created_at': session.created_at.isoformat() if session.created_at else None
            },
            'conversation_history': session.conversation_history or {"messages": []},
            'workflow_state': session.workflow_state or {},
            'extracted_entities': session.extracted_entities or {},
            'whatsapp_context': session.whatsapp_context or {},
            'session_status': {
                'has_pending_confirmations': bool(
                    session.workflow_state.get("pending_multiple_rfqs") or 
                    session.workflow_state.get("pending_rfq")
                ),
                'has_extracted_entities': bool(
                    session.workflow_state.get("extracted_entities") or 
                    session.extracted_entities
                ),
                'has_incomplete_products': bool(session.workflow_state.get("incomplete_products")),
                'current_stage': ChatServiceHelpers.determine_conversation_stage(session)
            }
        }
    
    @staticmethod
    def determine_conversation_stage(session: ConversationSession) -> str:
        """Determine current conversation stage based on session state."""
        workflow_state = session.workflow_state or {}
        
        if workflow_state.get("pending_multiple_rfqs") or workflow_state.get("pending_rfq"):
            return "confirming"
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