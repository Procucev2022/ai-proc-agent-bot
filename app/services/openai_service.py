"""
OpenAI API integration service for LLM-powered operations.

This service provides a wrapper around OpenAI's API for all language model
operations including intent classification, entity extraction, and response
generation. It handles API authentication, prompt engineering, response parsing,
and error handling for all LLM interactions in the procurement agent system.

Key responsibilities:
- Manage OpenAI API authentication and configuration
- Handle intent classification requests with structured prompts
- Process entity extraction from user messages
- Generate contextual responses for user queries
- Parse and validate LLM responses
- Handle API rate limits, timeouts, and error scenarios
- Support different OpenAI models based on use case
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional, List
from openai import OpenAI
from app.config import get_settings
from app.tools.interaction_logger import get_interaction_logger

logger = logging.getLogger(__name__)

class OpenAIService:
    """
    OpenAI API integration service for LLM operations.
    
    Provides wrapper around OpenAI API for intent classification,
    entity extraction, response generation, and validation.
    """
    
    def __init__(self):
        """Initialize OpenAI service with configuration."""
        self.settings = get_settings()
        self.client = OpenAI(api_key=self.settings.openai_api_key)
        self.default_model = self.settings.openai_model_default
        self.advanced_model = self.settings.openai_model_advanced
        self.tools_dir = Path(__file__).parent.parent / "tools"
        self.interaction_logger = get_interaction_logger()
        
    def classify_intent(self, message: str, context: dict = None) -> Dict[str, Any]:
        """
        Classify user intent using OpenAI function calling.
        
        Uses function calling with the Responses API to get structured
        intent classification with confidence scores.
        
        Args:
            message: User message to classify
            context: Optional conversation context
            
        Returns:
            Dict with intent, confidence, and any additional data
        """
        start_time = time.time()
        
        try:
            # Load intent classification tool
            with open(self.tools_dir / "intent_classification.json", 'r') as f:
                intent_tool = json.load(f)
            
            input_messages = [{"role": "user", "content": message}]
            
            # Add context if available
            if context and context.get('conversation_history'):
                input_messages.insert(0, {
                    "role": "developer",
                    "content": f"Previous conversation context: {json.dumps(context.get('conversation_history', []))}"
                })
            
            response = self.client.responses.create(
                model=self.default_model,
                input=input_messages,
                instructions=self._get_intent_system_prompt(),
                tools=[intent_tool],
                tool_choice={"type": "function", "name": "classify_intent"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "intent": args.get("intent"),
                        "confidence": args.get("confidence"),
                        "all_intent_scores": args.get("all_intent_scores", {}),
                        "reasoning": args.get("reasoning", ""),
                        "suggested_clarification": args.get("suggested_clarification"),
                        "success": True
                    }
                    
                    # Log successful interaction
                    self.interaction_logger.log_intent_classification(
                        user_input=message,
                        intent=result["intent"],
                        confidence=result["confidence"],
                        reasoning=result["reasoning"],
                        model_used=self.default_model,
                        processing_time=processing_time,
                        all_scores=result["all_intent_scores"]
                    )
                    
                    return result
            
            # Log failed interaction
            self.interaction_logger.log_error(
                interaction_type="intent_classification",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return self._get_fallback_intent_response("No function call in response")
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="intent_classification",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Intent classification failed: {error_msg}")
            return self._get_fallback_intent_response(error_msg)
        
    def extract_entities(self, message: str, workflow_type: str = "product_search") -> Dict[str, Any]:
        """
        Extract structured entities from user message using OpenAI.
        
        Uses function calling to extract structured entity information 
        from unstructured user messages based on workflow type.
        
        Args:
            message: User message to extract entities from
            workflow_type: Type of workflow (product_search, rfq_creation, buy_something)
            
        Returns:
            Dict with extracted entities and metadata
        """
        start_time = time.time()
        
        try:
            # Map workflow types to tool files
            workflow_mapping = {
                "buy_something": "rfq_creation",
                "rfq_creation": "rfq_creation", 
                "product_search": "product_search"
            }
            
            mapped_workflow = workflow_mapping.get(workflow_type, workflow_type)
            
            # Load appropriate entity extraction tool
            tool_file = f"entity_extraction_{mapped_workflow}.json"
            with open(self.tools_dir / tool_file, 'r') as f:
                entity_tool = json.load(f)
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": message}],
                instructions=self._get_entity_system_prompt(mapped_workflow),
                tools=[entity_tool],
                tool_choice={"type": "function", "name": "extract_entities"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "entities": args.get("entities", {}),
                        "completeness": args.get("completeness", 0),
                        "missing_fields": args.get("missing_fields", []),
                        "confidence": args.get("confidence", 0),
                        "next_questions": args.get("next_questions", []),
                        "success": True
                    }
                    
                    # Log successful entity extraction
                    self.interaction_logger.log_entity_extraction(
                        user_input=message,
                        entities=result["entities"],
                        completeness=result["completeness"],
                        workflow_type=mapped_workflow,
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=result["missing_fields"]
                    )
                    
                    return result
            
            # Log failed entity extraction
            self.interaction_logger.log_error(
                interaction_type="entity_extraction",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return {"entities": {}, "completeness": 0, "missing_fields": [], "confidence": 0, "next_questions": [], "success": False}
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="entity_extraction",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Entity extraction failed: {error_msg}")
            return {"entities": {}, "completeness": 0, "missing_fields": [], "confidence": 0, "next_questions": [], "success": False}
        
    def generate_response(self, context: dict, query_results: list = None) -> str:
        """
        Generate contextual response based on query results.
        
        Creates human-like responses that incorporate search results,
        user context, and appropriate next steps or recommendations.
        
        Args:
            context: User and conversation context
            query_results: Optional search/query results to incorporate
            
        Returns:
            Generated response string
        """
        try:
            prompt = self._build_response_prompt(context, query_results or [])
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._get_response_system_prompt()
            )
            
            return response.output_text or "I apologize, but I'm having trouble generating a response right now."
            
        except Exception as e:
            logger.error(f"Response generation failed: {str(e)}")
            return self._get_fallback_response(context, query_results or [])
        
    def validate_field_value(self, field_name: str, value: str, context: dict) -> Dict[str, Any]:
        """
        Validate RFQ field value using OpenAI for complex validation.
        
        Uses LLM to validate field values that require semantic understanding
        or complex business rule validation.
        
        Args:
            field_name: Name of the field being validated
            value: Value to validate
            context: Additional context for validation
            
        Returns:
            Dict with validation result and suggestions
        """
        try:
            # Load field validation tool
            with open(self.tools_dir / "field_validation.json", 'r') as f:
                validation_tool = json.load(f)
            
            prompt = f"Validate the field '{field_name}' with value '{value}'"
            if context:
                prompt += f" with context: {json.dumps(context)}"
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions="You are a validator for RFQ field values. Check if the provided value is valid and appropriate for the field.",
                tools=[validation_tool],
                tool_choice={"type": "function", "name": "validate_field"}
            )
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    return {
                        "is_valid": args.get("is_valid", False),
                        "validation_score": args.get("validation_score", 0),
                        "suggestions": args.get("suggestions", []),
                        "reason": args.get("reason", ""),
                        "normalized_value": args.get("normalized_value"),
                        "severity": args.get("severity", "info")
                    }
            
            return {"is_valid": True, "validation_score": 80, "suggestions": [], "reason": "Basic validation passed", "normalized_value": None, "severity": "info"}
            
        except Exception as e:
            logger.error(f"Field validation failed: {str(e)}")
            return {"is_valid": True, "validation_score": 50, "suggestions": [], "reason": f"Validation error: {str(e)}", "normalized_value": None, "severity": "warning"}
        
    def _get_intent_system_prompt(self) -> str:
        """System prompt for intent classification."""
        return """You are an expert intent classifier for a procurement AI agent. Your job is to analyze user messages and classify them, providing scores for ALL intent categories:

1. **product_search** (80%+ confidence) - User is looking for specific products, checking availability, or asking "do you have..."
   Examples: "Do you have industrial motors?", "Looking for generators in Chennai", "Available AC units?"

2. **rfq_creation** (75%+ confidence) - User wants to create an RFQ, request quotes, or buy something
   Examples: "I want to create an RFQ", "Need quote for motors", "Want to buy generators"

3. **general_inquiry** (60%+ confidence) - User asking how things work, general questions, help
   Examples: "How does this work?", "What can you do?", "Help me understand"

4. **ambiguous** (<60% confidence) - Unclear intent, mixed signals, or insufficient information
   Examples: "Hello", "Thanks", unclear mixed messages

IMPORTANT GUIDELINES:
- Provide confidence scores for ALL THREE main intents (product_search, rfq_creation, general_inquiry)
- If two intents have similar high scores (within 15 points and both >60%), suggest clarification
- Messages like "I need to buy X" could be product_search (checking availability) OR rfq_creation (ready to purchase)
- Be conservative with confidence scores. Only assign high confidence when very certain
- Consider context: bulk quantities often suggest RFQ, casual inquiries suggest product search"""
        
    def _get_entity_system_prompt(self, workflow_type: str) -> str:
        """System prompt for entity extraction."""
        if workflow_type == "product_search":
            return """You are an expert entity extractor for product search queries. Extract structured information from user messages about products they're looking for.

Focus on extracting:
- Product type and category
- Location requirements  
- Technical specifications
- Age/condition requirements
- Whether it's an availability check

Be precise but don't hallucinate information that isn't clearly stated. Mark missing information as null."""
        
        else:  # rfq_creation
            return """You are an expert entity extractor for RFQ creation workflows. Extract structured information needed to create a complete RFQ.

Required fields for RFQ:
- Item description
- Technical specifications
- Unit of measurement (UOM)
- Product category
- Quantity needed
- Delivery location
- Age of asset requirements
- Budget/price expectations

Calculate completeness as percentage of required fields that have values. List missing required fields and suggest questions to ask."""
        
    def _get_response_system_prompt(self) -> str:
        """System prompt for response generation."""
        return """You are a helpful procurement assistant. Generate natural, conversational responses that:

1. Address the user's query directly
2. Incorporate provided search results naturally
3. Suggest next steps when appropriate
4. Maintain a professional but friendly tone
5. Keep responses concise and actionable

If no search results are provided, acknowledge the request and explain what information you need to help further."""
        
    def _build_response_prompt(self, context: dict, results: list) -> str:
        """Build prompt for response generation."""
        prompt = f"User context: {json.dumps(context)}\n\n"
        
        if results:
            prompt += f"Search results: {json.dumps(results)}\n\n"
        
        prompt += "Generate an appropriate response for the user based on their context and any available results."
        
        return prompt
        
    def _get_fallback_intent_response(self, error: str) -> Dict[str, Any]:
        """Fallback response for intent classification failures."""
        logger.warning(f"Using fallback intent classification: {error}")
        return {
            "intent": "general_inquiry",
            "confidence": 30,
            "reasoning": f"Fallback classification due to error: {error}",
            "suggested_clarification": "Could you please rephrase your request?",
            "success": False
        }
        
    def _get_fallback_response(self, context: dict, results: list) -> str:
        """Fallback response when OpenAI is unavailable."""
        return "I'm experiencing some technical difficulties right now. Could you please rephrase your request or try again in a moment?"