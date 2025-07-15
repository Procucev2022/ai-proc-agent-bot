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
from app.utils.logging_utils import log_service_method

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
        self.client = OpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
            default_query={"api-version": "preview"}, 
        )
        self.default_model = self.settings.openai_model_default
        self.advanced_model = self.settings.openai_model_advanced
        self.tools_dir = Path(__file__).parent.parent / "tools"
        self.prompts_dir = Path(__file__).parent.parent / "prompts"
        self.interaction_logger = get_interaction_logger()
        
    def _load_prompt(self, category: str, prompt_name: str, **kwargs) -> str:
        """
        Load prompt from text file and format with provided kwargs.
        
        Args:
            category: Subdirectory under prompts (e.g., 'intent_classification')
            prompt_name: Name of the prompt file without .txt extension
            **kwargs: Variables to format in the prompt template
            
        Returns:
            Formatted prompt string
        """
        try:
            prompt_file = self.prompts_dir / category / f"{prompt_name}.txt"
            with open(prompt_file, 'r', encoding='utf-8') as f:
                prompt_template = f.read()
            
            if kwargs:
                return prompt_template.format(**kwargs)
            return prompt_template
            
        except FileNotFoundError:
            logger.error(f"Prompt file not found: {prompt_file}")
            return f"Error: Prompt file {prompt_name} not found in {category}"
        except Exception as e:
            logger.error(f"Error loading prompt {prompt_name}: {str(e)}")
            return f"Error loading prompt: {str(e)}"
        
    @log_service_method("openai_service")
    def classify_intent(self, message: str, context: dict = None) -> Dict[str, Any]:
        """
        Classify user intent using OpenAI function calling with conversation context awareness.
        
        Uses function calling with the Responses API to get structured
        intent classification with confidence scores and context analysis.
        
        Args:
            message: User message to classify
            context: Optional conversation context including session state, history, and entities
            
        Returns:
            Dict with intent, confidence, context_analysis, and additional data
        """
        start_time = time.time()
        
        try:
            # Load intent classification tool
            with open(self.tools_dir / "intent_classification.json", 'r') as f:
                intent_tool = json.load(f)
            
            input_messages = [{"role": "user", "content": message}]
            
            # Build comprehensive context information for the prompt
            context_info = ""
            if context:
                # Add conversation history
                if context.get('conversation_history', {}).get('messages'):
                    recent_messages = context['conversation_history']['messages'][-5:]  # Last 5 messages for context
                    history_text = "\n".join([f"{msg.get('sender', 'unknown')}: {msg.get('content', '')}" for msg in recent_messages])
                    context_info += f"\n\nRECENT CONVERSATION HISTORY:\n{history_text}"
                
                # Add current session state
                if context.get('workflow_state'):
                    workflow_state = context['workflow_state']
                    context_info += f"\n\nCURRENT SESSION STATE:"
                    context_info += f"\n- Workflow Type: {context.get('workflow_type', 'unknown')}"
                    context_info += f"\n- Has Pending Confirmations: {bool(workflow_state.get('pending_multiple_rfqs') or workflow_state.get('pending_rfq'))}"
                    
                    # Add extracted entities information
                    if workflow_state.get('extracted_entities'):
                        entities = workflow_state['extracted_entities']
                        if isinstance(entities, list) and entities:
                            # Multiple products
                            context_info += f"\n- Existing Products: {len(entities)} products collected"
                            for i, product in enumerate(entities[:3]):  # Show first 3 products
                                desc = product.get('description', f'Product {i+1}')
                                qty = product.get('quantity', 'unknown')
                                context_info += f"\n  - {desc}: {qty}"
                        elif isinstance(entities, dict) and entities:
                            # Single product
                            desc = entities.get('description', 'Product')
                            qty = entities.get('quantity', 'unknown')
                            context_info += f"\n- Existing Product: {desc}: {qty}"
                
                # Add extracted entities from top level
                if context.get('extracted_entities'):
                    context_info += f"\n- Additional Entities: {list(context['extracted_entities'].keys())}"
                
                # Add context as developer message
                if context_info.strip():
                    input_messages.insert(0, {
                        "role": "developer", 
                        "content": f"CONVERSATION CONTEXT:{context_info}\n\nAnalyze the user's message considering this context."
                    })
            
            response = self.client.responses.create(
                model=self.default_model,
                input=input_messages,
                instructions=self._load_prompt("intent_classification", "_get_intent_system_prompt"),
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
                        "context_analysis": args.get("context_analysis", {}),
                        "reasoning": args.get("reasoning", ""),
                        "suggested_clarification": args.get("suggested_clarification"),
                        "success": True
                    }
                    
                    # Log successful interaction with context info
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
        
    @log_service_method("openai_service")
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
                instructions=self._load_prompt("entity_extraction", f"_get_entity_system_prompt_{mapped_workflow}"),
                tools=[entity_tool],
                tool_choice={"type": "function", "name": "extract_entities"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    print(f"OpenAI raw function call args: {args}")
                    
                    # Handle both old single entity format and new multi-product format
                    if "products" in args:
                        # New multi-product format
                        products = args.get("products", [])
                        print(f"OpenAI: Using NEW multi-product format with {len(products)} products")
                        result = {
                            "products": products,
                            "confidence": args.get("confidence", 0),
                            "success": True
                        }
                    else:
                        # Backward compatibility for old single entity format
                        print(f"OpenAI: Using OLD single entity format")
                        result = {
                            "entities": args.get("entities", {}),
                            "completeness": args.get("completeness", 0),
                            "missing_fields": args.get("missing_fields", []),
                            "confidence": args.get("confidence", 0),
                            "next_questions": args.get("next_questions", []),
                            "success": True
                        }
                    
                    # Log successful entity extraction
                    if "products" in result:
                        # Log for multi-product format
                        self.interaction_logger.log_entity_extraction(
                            user_input=message,
                            entities={"products": result["products"]},
                            completeness=100,  # Will be calculated per product later
                            workflow_type=mapped_workflow,
                            model_used=self.default_model,
                            processing_time=processing_time,
                            missing_fields=[]
                        )
                    else:
                        # Log for single entity format
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
            return {"products": [], "completeness": 0, "missing_fields": [], "confidence": 0, "next_questions": [], "success": False}
            
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
            return {"products": [], "completeness": 0, "missing_fields": [], "confidence": 0, "next_questions": [], "success": False}
        
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
            # Build prompt inline
            prompt = f"User context: {json.dumps(context)}\n\n"
            if query_results:
                prompt += f"Search results: {json.dumps(query_results)}\n\n"
            prompt += "Generate an appropriate response for the user based on their context and any available results."
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("response_generation", "_get_response_system_prompt")
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
                instructions=self._load_prompt("validation", "field_validation_system_prompt"),
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
        
    def _get_fallback_intent_response(self, error: str) -> Dict[str, Any]:
        """Fallback response for intent classification failures."""
        logger.warning(f"Using fallback intent classification: {error}")
        return {
            "intent": "general_inquiry",
            "confidence": 30,
            "all_intent_scores": {
                "buy_something": 20,
                "general_inquiry": 30,
                "modification_request": 10,
                "confirmation_response": 10,
                "rfq_status_check": 10
            },
            "context_analysis": {
                "references_existing_data": False,
                "conversation_stage": "unknown",
                "modification_details": {"target_entity": None, "modification_type": None},
                "confirmation_details": {"response_type": None, "has_conditions": False}
            },
            "reasoning": f"Fallback classification due to error: {error}",
            "suggested_clarification": "Could you please rephrase your request?",
            "success": False
        }
        
    def _get_fallback_response(self, context: dict, results: list) -> str:
        """Fallback response when OpenAI is unavailable."""
        return "I'm experiencing some technical difficulties right now. Could you please rephrase your request or try again in a moment?"
    
    def generate_contextual_response(self, context: dict, base_questions: list = None, conversation_stage: str = "collecting") -> str:
        """
        Generate contextual response for conversation flow using function calling.
        
        Uses OpenAI function calling to create structured, natural responses based on conversation
        state, missing fields, and current stage of the interaction.
        
        Args:
            context: Dictionary containing conversation context including:
                - completeness: Current completeness percentage
                - missing_fields: List of missing required fields
                - conversation_history: Previous messages
                - extracted_entities: Current entities
                - user_message: Latest user message
            base_questions: Optional list of base questions from data model
            conversation_stage: Stage of conversation (collecting, clarifying, completing)
            
        Returns:
            Generated contextual response string
        """
        start_time = time.time()
        
        try:
            # Load contextual response tool
            with open(self.tools_dir / "contextual_response_generation.json", 'r') as f:
                response_tool = json.load(f)
            
            # Build prompt inline
            prompt = f"Generate a contextual response for RFQ collection:\n\n"
            prompt += f"Stage: {conversation_stage}\n"
            prompt += f"Completeness: {context.get('completeness', 0)}%\n"
            prompt += f"User message: '{context.get('user_message', '')}'\n"
            
            if context.get('extracted_entities'):
                prompt += f"Current entities: {json.dumps(context['extracted_entities'])}\n"
            
            if base_questions:
                prompt += f"Questions from data model: {base_questions}\n"
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("response_generation", "_get_contextual_response_system_prompt", conversation_stage=conversation_stage),
                tools=[response_tool],
                tool_choice={"type": "function", "name": "generate_contextual_response"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    # Format response inline
                    generated_response = ""
                    if args.get("acknowledgment"):
                        generated_response += args["acknowledgment"]
                    if args.get("progress_update"):
                        generated_response += f"\n\n{args['progress_update']}"
                    if args.get("next_question"):
                        generated_response += f"\n\n{args['next_question']}"
                    generated_response = generated_response.strip()
                    
                    # Log successful response generation
                    self.interaction_logger.log_response_generation(
                        context=context,
                        generated_response=generated_response,
                        conversation_stage=conversation_stage,
                        model_used=self.default_model,
                        processing_time=processing_time
                    )
                    
                    return generated_response
            
            # Fallback response inline
            if base_questions:
                return f"Thank you for the information! {base_questions[0]}"
            else:
                return "Could you provide more details to help me assist you?"
            
        except Exception as e:
            logger.error(f"Contextual response generation failed: {str(e)}")
            # Fallback response inline
            if base_questions:
                return f"Thank you for the information! {base_questions[0]}"
            else:
                return "Could you provide more details to help me assist you?"
    
    def generate_completion_response(self, rfq_data: dict, context: dict) -> str:
        """
        Generate RFQ completion response with summary using function calling.
        
        Creates a comprehensive response when RFQ is complete, including
        summary of collected information and next steps.
        
        Args:
            rfq_data: Complete RFQ data dictionary
            context: Conversation context
            
        Returns:
            Generated completion response string
        """
        start_time = time.time()
        
        try:
            # Load completion response tool
            with open(self.tools_dir / "completion_response_generation.json", 'r') as f:
                completion_tool = json.load(f)
            
            # Build prompt inline
            prompt = f"Generate RFQ completion response:\n\n"
            prompt += f"RFQ Data: {json.dumps(rfq_data)}\n"
            prompt += f"User context: {context.get('user_message', '')}\n"
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("response_generation", "_get_completion_response_system_prompt"),
                tools=[completion_tool],
                tool_choice={"type": "function", "name": "generate_completion_response"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    # Format response inline
                    generated_response = ""
                    if args.get("celebration"):
                        generated_response += args["celebration"]
                    if args.get("summary"):
                        generated_response += f"\n\n Summary: {args['summary']}"
                    if args.get("next_steps"):
                        generated_response += f"\n\n Next Steps: {args['next_steps']}"
                    if args.get("reference_id"):
                        generated_response += f"\n\n Reference: {args['reference_id']}"
                    generated_response = generated_response.strip()
                    
                    # Log successful completion response
                    self.interaction_logger.log_response_generation(
                        context=context,
                        generated_response=generated_response,
                        conversation_stage="completion",
                        model_used=self.default_model,
                        processing_time=processing_time
                    )
                    
                    return generated_response
            
            return "Excellent! Your RFQ is now complete. I'll process this request and get back to you soon."
            
        except Exception as e:
            logger.error(f"Completion response generation failed: {str(e)}")
            return "Excellent! Your RFQ is now complete. I'll process this request and get back to you soon."
    
    def generate_clarification_response(self, questions: list, completeness: float, context: dict) -> str:
        """
        Generate clarification response when multiple questions needed using function calling.
        
        Creates natural response when multiple fields are missing and
        clarification is needed from the user.
        
        Args:
            questions: List of questions to ask
            completeness: Current completeness percentage
            context: Conversation context
            
        Returns:
            Generated clarification response string
        """
        start_time = time.time()
        
        try:
            # Load clarification response tool
            with open(self.tools_dir / "clarification_response_generation.json", 'r') as f:
                clarification_tool = json.load(f)
            
            # Build prompt inline
            prompt = f"Generate clarification response:\n\n"
            prompt += f"Completeness: {completeness}%\n"
            prompt += f"Questions to ask: {questions}\n"
            prompt += f"User message: '{context.get('user_message', '')}'\n"
            
            if context.get('extracted_entities'):
                prompt += f"Current entities: {json.dumps(context['extracted_entities'])}\n"
            
            response = self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("response_generation", "_get_clarification_response_system_prompt"),
                tools=[clarification_tool],
                tool_choice={"type": "function", "name": "generate_clarification_response"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    # Format response inline
                    generated_response = ""
                    if args.get("progress_acknowledgment"):
                        generated_response += args["progress_acknowledgment"]
                    if args.get("questions"):
                        questions_text = "\n".join(f"• {q}" for q in args["questions"])
                        generated_response += f"\n\nI need a bit more information:\n{questions_text}"
                    if args.get("reason"):
                        generated_response += f"\n\n{args['reason']}"
                    generated_response = generated_response.strip()
                    
                    # Log successful clarification response
                    self.interaction_logger.log_response_generation(
                        context=context,
                        generated_response=generated_response,
                        conversation_stage="clarification",
                        model_used=self.default_model,
                        processing_time=processing_time
                    )
                    
                    return generated_response
            
            # Fallback response inline
            questions_text = "\n".join(f"• {q}" for q in questions[:2])
            return f"I need a bit more information:\n\n{questions_text}"
            
        except Exception as e:
            logger.error(f"Clarification response generation failed: {str(e)}")
            # Fallback response inline
            questions_text = "\n".join(f"• {q}" for q in questions[:2])
            return f"I need a bit more information:\n\n{questions_text}"
