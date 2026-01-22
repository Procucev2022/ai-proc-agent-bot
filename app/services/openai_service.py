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

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
from openai import AsyncOpenAI
from openai import APIError, APITimeoutError, RateLimitError, APIConnectionError
from app.config import get_settings
from app.tools.interaction_logger import get_interaction_logger
from app.utils.logging_utils import log_service_method
from app.utils.datetime_utils import format_date_display, format_date_for_validation_error, add_business_days, calculate_working_days_from_now

# from app.services.global_error_handler import handle_api_error  # Removed to avoid circular import

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
        self._client = None  # Lazy initialization
        self._client_closed = False  # Track if client was explicitly closed
        self.default_model = self.settings.openai_model_default
        self.advanced_model = self.settings.openai_model_advanced
        self.tools_dir = Path(__file__).parent.parent / "tools"
        self.prompts_dir = Path(__file__).parent.parent / "prompts"
        self.interaction_logger = get_interaction_logger()

        # Initialize error notification service (lazy loading to avoid circular imports)
        self._error_notification_service = None

        # OpenAI call tracking for performance monitoring
        self.call_counts = {}

    @property
    def client(self) -> AsyncOpenAI:
        """
        Get OpenAI client with lazy initialization.

        Automatically recreates the client if it was previously closed.
        This ensures resilience when close_sync() is called between requests.
        """
        if self._client is None or self._client_closed:
            self._client = AsyncOpenAI(
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
                default_query={"api-version": "preview"},
            )
            self._client_closed = False
            logger.debug("OpenAI client (re)initialized")
        return self._client

    async def close(self):
        """Close the OpenAI client and cleanup resources."""
        try:
            if self._client is not None:
                await self._client.close()
                self._client_closed = True
                logger.debug("OpenAI client closed successfully")
        except Exception as e:
            logger.warning(f"Error closing OpenAI client: {e}")
    
    def close_sync(self):
        """Synchronous close for Celery tasks to prevent event loop errors."""
        if self._client is None:
            return  # Nothing to close

        try:
            import asyncio
            try:
                # Try to get existing event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is running, schedule close as task
                    asyncio.create_task(self._client.close())
                else:
                    # If loop exists but not running, run close
                    loop.run_until_complete(self._client.close())
            except RuntimeError:
                # No event loop exists, create new one
                asyncio.run(self._client.close())

            self._client_closed = True
            logger.debug("OpenAI client closed synchronously (will auto-reinitialize on next use)")
        except Exception as e:
            logger.warning(f"Error closing OpenAI client synchronously: {e}")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup resources."""
        await self.close()

    def _track_openai_call(self, call_type: str, user_phone: str = None):
        """Track OpenAI API calls for performance monitoring."""
        request_key = user_phone or "global"
        if request_key not in self.call_counts:
            self.call_counts[request_key] = {}
        if call_type not in self.call_counts[request_key]:
            self.call_counts[request_key][call_type] = 0
        self.call_counts[request_key][call_type] += 1

        # Log the call for immediate visibility
        extra = {}
        if request_key != "global":
            extra['phone_number'] = request_key
        logger.debug(f"OpenAI call: {call_type} for {request_key} (total: {self.call_counts[request_key][call_type]})", extra=extra)

    def get_call_summary(self, user_phone: str = None) -> dict:
        """Get summary of OpenAI calls for a request."""
        request_key = user_phone or "global"
        return self.call_counts.get(request_key, {})
    
    def _get_error_notification_service(self):
        """Lazy load error notification service to avoid circular imports."""
        if self._error_notification_service is None:
            from app.services.error_notification_service import ErrorNotificationService
            self._error_notification_service = ErrorNotificationService()
        return self._error_notification_service
    
    async def _notify_openai_error(self, error_type: str, error_message: str, method_name: str):
        """Send WhatsApp notification when OpenAI service has errors."""
        try:
            error_notification_service = self._get_error_notification_service()
            
            error_details = {
                'error_type': f'OpenAI {error_type}',
                'message': f'{method_name}: {error_message}',
                'service': 'OpenAI Service',
                'timestamp': datetime.now().isoformat()
            }
            
            await error_notification_service.notify_general_error(error_details)
            logger.info(f"Sent WhatsApp notification for OpenAI error: {error_type}")
            
        except Exception as e:
            logger.error(f"Failed to send WhatsApp notification for OpenAI error: {e}")
        
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
            
            # Add support_email to kwargs if not already provided
            if 'support_email' not in kwargs:
                kwargs['support_email'] = self.settings.support_email
            if 'support_contact_info' not in kwargs:
                kwargs['support_contact_info']= self.settings.support_contact_info
            
            if kwargs:
                return prompt_template.format(**kwargs)
            return prompt_template
            
        except FileNotFoundError:
            logger.error(f"Prompt file not found: {prompt_file}")
            return f"Error: Prompt file {prompt_name} not found in {category}"
        except Exception as e:
            logger.error(f"Error loading prompt {prompt_name}: {str(e)}")
            return f"Error loading prompt: {str(e)}"
    
    def _build_messages_with_history(self, context: dict = None, current_message: str = "") -> list:
        """
        Build message array with conversation history for OpenAI API calls.

        This method extracts OpenAI-ready messages from context and optionally
        appends the current message, enabling conversation continuity.

        Args:
            context: Conversation context with openai_messages array
            current_message: Current user message to append

        Returns:
            List of message dicts ready for OpenAI API
        """
        input_messages = []

        # Add conversation history if available
        if context and context.get('conversation_history', {}).get('openai_messages'):
            openai_messages = context['conversation_history']['openai_messages']
            # Ensure it's a list before processing
            if isinstance(openai_messages, list):
                history_messages = openai_messages[-10:]  # Last 10 messages for context
                input_messages.extend(history_messages)
                logger.debug(f"Added {len(history_messages)} history messages to OpenAI context")
        else:
            logger.debug("No conversation history available - using only current message")

        # Add current message if provided - but handle image content properly
        if current_message:
            # Check if current_message contains image content that should be handled differently
            if self._is_image_content(current_message, context):
                # For image content, use a text description instead of raw content
                input_messages.append({"role": "user", "content": "User sent an image attachment"})
                logger.debug("Converted image content to text description for OpenAI")
            else:
                input_messages.append({"role": "user", "content": current_message})

        # Log the complete input being sent to OpenAI
        logger.debug(f"OpenAI Input ({len(input_messages)} messages):")
        for i, msg in enumerate(input_messages):
            content_preview = str(msg.get('content', ''))[:100] + ('...' if len(str(msg.get('content', ''))) > 100 else '')
            logger.debug(f"  {i+1}. {msg.get('role')}: {content_preview}")

        return input_messages

    def _is_image_content(self, message: str, context: dict = None) -> bool:
        """
        Check if the message content represents image data.

        Args:
            message: The message content
            context: Additional context that might contain image information

        Returns:
            True if this is image content, False otherwise
        """
        # Check if message looks like stringified JSON with image data
        if isinstance(message, str) and ('mime_type' in message and 'image' in message):
            return True

        # Check if context indicates this is from an image message
        if context:
            user_msg = context.get('user_message', '')
            if isinstance(user_msg, dict) and 'mime_type' in user_msg:
                return True
            elif isinstance(user_msg, str) and ('mime_type' in user_msg and 'image' in user_msg):
                return True

        return False

    def extract_text_from_message(self,msg):
        """
        Safely extracts meaningful user/assistant text from an OpenAI or WhatsApp message.
        Handles:
        - WhatsApp button replies
        - Normal text messages
        - Multimodal array messages
        - Dict-based messages
        """
        content = msg.get("content", "")

        # 1. Handle WhatsApp button reply
        if isinstance(content, dict) and content.get("type") == "button_reply":
            btn = content.get("button_reply", {})
            return btn.get("title") or btn.get("id") or "[button reply]"

        # 2. Content as simple text string
        if isinstance(content, str):
            return content

        # 3. Content as dict with text
        if isinstance(content, dict):
            return content.get("text", "[non-text content]")

        # 4. Content as list of fragments (multimodal)
        if isinstance(content, list):
            text_parts = [
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if text_parts:
                return " ".join(text_parts)
            return "[multimodal content]"

        # Fallback
        return "[unknown content]"

    @log_service_method("openai_service")
    async def classify_intent(self, message: str, context: dict = None,user_phone=None) -> Dict[str, Any]:
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
            
            # Handle image content properly for intent classification
            if self._is_image_content(message, context):
                input_messages = [{"role": "user", "content": "User sent an image attachment"}]
                logger.debug("Converted image content to text description for intent classification")
            else:
                # Ensure message is a string, not an object
                if isinstance(message, dict):
                    # Extract text from content object
                    message_text = message.get('text', str(message))
                elif isinstance(message, list):
                    # Extract text from array of content objects
                    text_parts = [m.get('text', '') for m in message if isinstance(m, dict)]
                    message_text = ' '.join(text_parts) if text_parts else str(message)
                else:
                    message_text = str(message)

                input_messages = [{"role": "user", "content": message_text}]
            
            # Build comprehensive context information for the prompt
            context_info = ""
            if context:
                # Add conversation history
                if context.get('conversation_history', {}).get('openai_messages'):
                    recent_messages = context['conversation_history']['openai_messages'][-2:]

                    history_parts = []
                    for msg in recent_messages:
                        role = msg.get("role", "unknown")
                        text = self.extract_text_from_message(msg)
                        history_parts.append(f"{role}: {text}")

                    history_text = "\n".join(history_parts)
                    context_info += f"\n\nRECENT CONVERSATION HISTORY:\n{history_text}"

                # Add user role for better intent classification
                if context.get('user_role'):
                    context_info += f"\n\nUSER ROLE: {context.get('user_role')}"

                # Add current session state (optimized - reduced verbosity)
                if context.get('workflow_state'):
                    workflow_state = context['workflow_state']
                    context_info += f"\n\nCURRENT SESSION STATE:"
                    context_info += f"\n- Workflow Type: {context.get('workflow_type', 'unknown')}"

                    # Add pre-computed conversation stage if available
                    if context.get('conversation_stage'):
                        context_info += f"\n- Conversation Stage: {context.get('conversation_stage')}"

                    # Add sectioned RFQ context if active
                    sectioned_rfq = workflow_state.get('sectioned_rfq', {})
                    if sectioned_rfq.get('active'):
                        context_info += f"\n- Sectioned RFQ Active: True"
                        context_info += f"\n- Current Section: {sectioned_rfq.get('current_section', 'unknown')}"
                        if sectioned_rfq.get('awaiting_delivery_modification'):
                            context_info += f"\n- Awaiting Delivery Modification: True"
                        if sectioned_rfq.get('awaiting_items_modification'):
                            context_info += f"\n- Awaiting Items Modification: True"

                    context_info += f"\n- Has Pending Confirmations: {bool(workflow_state.get('pending_combined_rfq') or workflow_state.get('pending_rfq'))}"
                    context_info += f"\n- Has Pending Optional Fields: {bool(workflow_state.get('pending_optional_rfq'))}"
                    context_info += f"\n- Has Pending Attachment Decision: {bool(workflow_state.get('pending_attachment_decision'))}"

                    # Add extracted entities count only (not details - intent classification doesn't need product names)
                    if workflow_state.get('extracted_entities'):
                        entities = workflow_state['extracted_entities']
                        if isinstance(entities, list) and entities:
                            context_info += f"\n- Existing Products: {len(entities)} products"
                        elif isinstance(entities, dict) and entities:
                            context_info += f"\n- Existing Products: 1 product"
                
                # Add context as developer message
                if context_info.strip():
                    input_messages.insert(0, {
                        "role": "developer", 
                        "content": f"CONVERSATION CONTEXT:{context_info}\n\nAnalyze the user's message considering this context."
                    })
            
            # Track this OpenAI call
            self._track_openai_call("intent_classification")

            # Load system prompt
            system_prompt = self._load_prompt("intent_classification", "_get_intent_system_prompt")

            # Log timing breakdown
            prep_time = time.time() - start_time
            logger.info(f"Intent classification prep time: {prep_time:.2f}s")

            api_call_start = time.time()
            response = await self.client.responses.create(
                model=self.default_model,
                input=input_messages,
                instructions=system_prompt,
                tools=[intent_tool],
                tool_choice={"type": "function", "name": "classify_intent"}
            )
            api_call_time = time.time() - api_call_start

            # Log cache usage information
            usage = getattr(response, 'usage', None)
            if usage:
                total_input_tokens = getattr(usage, 'input_tokens', 0)
                input_tokens_details = getattr(usage, 'input_tokens_details', None)
                cached_tokens = getattr(input_tokens_details, 'cached_tokens', 0) if input_tokens_details else 0
                output_tokens = getattr(usage, 'output_tokens', 0)

                cache_percentage = (cached_tokens / total_input_tokens * 100) if total_input_tokens > 0 else 0
                logger.info(f"OpenAI API call time: {api_call_time:.2f}s | Input tokens: {total_input_tokens}W | Cached: {cached_tokens} ({cache_percentage:.1f}%) | Output: {output_tokens}")
            else:
                logger.info(f"OpenAI API call time: {api_call_time:.2f}s (no usage data available)")

            processing_time = time.time() - start_time
            logger.info(f"Total intent classification time: {processing_time:.2f}s")

            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "intent": args.get("intent"),
                        "confidence": args.get("confidence"),
                        "relevant_message":args.get("relevant_message"),
                        "irrelevant_message":args.get("irrelevant_message"),
                        "all_intent_scores": args.get("all_intent_scores", {}),
                        "context_analysis": args.get("context_analysis", {}),
                        "reasoning": args.get("reasoning", ""),
                        "suggested_clarification": args.get("suggested_clarification"),
                        "success": True
                    }

                    # Prepare complete OpenAI input for logging
                    openai_input_data = {
                        "instructions_length": len(system_prompt),
                        "instructions_preview": system_prompt[:200] + "..." if len(system_prompt) > 200 else system_prompt,
                        "input_messages": input_messages,
                        "total_input_chars": len(system_prompt) + sum(len(str(msg.get('content', ''))) for msg in input_messages)
                    }

                    # Log successful interaction with complete OpenAI input
                    self.interaction_logger.log_intent_classification(
                        user_input=message,
                        intent=result["intent"],
                        confidence=result["confidence"],
                        reasoning=result["reasoning"],
                        model_used=self.default_model,
                        processing_time=processing_time,
                        all_scores=result["all_intent_scores"],
                        openai_input=openai_input_data,
                        relevant_message=result.get("relevant_message"),
                        irrelevant_message=result.get("irrelevant_message")
                    )
                    
                    return result
            
            # Log failed interaction
            self.interaction_logger.log_error(
                interaction_type="intent_classification",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return await self._get_fallback_classification(message, context, user_phone=user_phone)

        except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
            error_msg = str(e)
            
            # Log API error
            logger.error(f"OpenAI API error in intent classification: {error_msg}")
            
            # Send WhatsApp notification
            await self._notify_openai_error("API Error", error_msg, "classify_intent")
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="intent_classification",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Intent classification failed: {error_msg}")
            return await self._get_fallback_classification(message, context,user_phone=user_phone)
        except Exception as e:
            error_msg = str(e)

            # Send WhatsApp notification for unexpected errors
            await self._notify_openai_error("Unexpected Error", error_msg, "classify_intent")
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="intent_classification",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Intent classification failed: {error_msg}")
            return await self._get_fallback_classification(message, context, user_phone=user_phone)

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
        user = context.get('user_role')
        if user:
            cancel_service = CancelService()
            await cancel_service._send_cancellation_message(user_phone=user_phone, user_type=user, custom_message=f"Currently, we are facing some technical issues. The team is actively working to get QUA up and running. We apologise for the inconvenience caused and request you to please try again after a while.In case of anything urgent, feel free to reach us at {self.settings.support_contact_info}")

        
    @log_service_method("openai_service")
    async def extract_entities(self, message: str, workflow_type: str = "product_search") -> Dict[str, Any]:
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
                "modification_request": "modification",  # Use dedicated modification extraction tool
                "product_search": "product_search",
                "bfs": "bfs",  # BFS stock search
                "rfq_status_check": "rfq_status",
                "registration_buyer": "registration_buyer",
                "registration_seller": "registration_seller"
            }
            
            mapped_workflow = workflow_mapping.get(workflow_type, workflow_type)
            
            # Load appropriate tool (entity extraction, modification extraction, or registration extraction)
            if mapped_workflow == "modification":
                tool_file = "modification_extraction.json"
                tool_function_name = "extract_modification_values"
            elif mapped_workflow == "registration_buyer":
                tool_file = "entity_extraction_registration_buyer.json"
                tool_function_name = "extract_buyer_registration_entities"
            elif mapped_workflow == "registration_seller":
                tool_file = "entity_extraction_registration_seller.json"
                tool_function_name = "extract_seller_registration_entities"
            else:
                tool_file = f"entity_extraction_{mapped_workflow}.json"
                tool_function_name = "extract_entities"
            
            with open(self.tools_dir / tool_file, 'r') as f:
                entity_tool = json.load(f)
            
            # Load appropriate prompt and set tool choice
            if mapped_workflow == "modification":
                prompt_category = "modification_extraction"
                prompt_name = "_get_modification_system_prompt"
            elif mapped_workflow in ["registration_buyer", "registration_seller"]:
                prompt_category = "entity_extraction"
                prompt_name = f"_get_entity_system_prompt_{mapped_workflow}"
            else:
                prompt_category = "entity_extraction"
                prompt_name = f"_get_entity_system_prompt_{mapped_workflow}"
            
            # Add current year for date extraction
            from datetime import datetime
            current_year = datetime.now().year

            # Track this OpenAI call
            self._track_openai_call("entity_extraction")

            # Load system prompt (cached via instructions parameter)
            system_prompt = self._load_prompt(prompt_category, prompt_name, current_year=current_year)

            # Log timing breakdown
            prep_time = time.time() - start_time
            logger.info(f"Entity extraction prep time: {prep_time:.2f}s")

            api_call_start = time.time()
            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": message}],
                instructions=system_prompt,
                tools=[entity_tool],
                tool_choice={"type": "function", "name": tool_function_name}
            )
            api_call_time = time.time() - api_call_start

            # Log cache usage information
            usage = getattr(response, 'usage', None)
            if usage:
                total_input_tokens = getattr(usage, 'input_tokens', 0)
                input_tokens_details = getattr(usage, 'input_tokens_details', None)
                cached_tokens = getattr(input_tokens_details, 'cached_tokens', 0) if input_tokens_details else 0
                output_tokens = getattr(usage, 'output_tokens', 0)

                cache_percentage = (cached_tokens / total_input_tokens * 100) if total_input_tokens > 0 else 0
                logger.info(f"OpenAI API call time: {api_call_time:.2f}s | Input tokens: {total_input_tokens} | Cached: {cached_tokens} ({cache_percentage:.1f}%) | Output: {output_tokens}")
            else:
                logger.info(f"OpenAI API call time: {api_call_time:.2f}s (no usage data available)")

            processing_time = time.time() - start_time
            logger.info(f"Total entity extraction time: {processing_time:.2f}s")
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    print(f"OpenAI raw function call args: {args}")
                    
                    # Handle different response formats based on tool used
                    if "modifications" in args:
                        # Modification extraction format
                        modifications = args.get("modifications", [])
                        has_new_values = args.get("has_new_values", False)
                        print(f"OpenAI: Using modification format with {len(modifications)} modifications, has_new_values: {has_new_values}")
                        result = {
                            "modifications": modifications,
                            "has_new_values": has_new_values,
                            "modification_intent": args.get("modification_intent"),
                            "confidence": args.get("confidence", 0),
                            "success": True,
                            "is_modification_extraction": True
                        }
                    elif "products" in args:
                        # New multi-product format with global fields
                        products = args.get("products", [])
                        print(f"OpenAI: Using NEW multi-product format with {len(products)} products")
                        result = {
                            "products": products,
                            "deliveryDate": args.get("deliveryDate"),
                            "state": args.get("state"),
                            "city": args.get("city"),
                            "pincode": args.get("pincode"),
                            "confidence": args.get("confidence", 0),
                            "success": True
                        }
                    elif "rfq_id" in args:
                        # RFQ status check format
                        print(f"OpenAI: Using RFQ status format")
                        result = {
                            "rfq_id": args.get("rfq_id"),
                            "confidence": args.get("confidence", 0),
                            "success": True
                        }
                    elif mapped_workflow in ["registration_buyer", "registration_seller"]:
                        # Registration extraction format - raw data will be post-processed
                        print(f"OpenAI: Using registration format for {mapped_workflow}")
                        result = {
                            "entities": args.get("entities", {}),
                            "completeness": args.get("completeness", 0),
                            "missing_fields": args.get("missing_fields", []),
                            "validation_errors": args.get("validation_errors", []),
                            "confidence": args.get("confidence", 0),
                            "success": True,
                            "raw_extraction": True  # Flag to indicate this needs post-processing
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
                    try:
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
                        elif "rfq_id" in result:
                            # Log for RFQ status format
                            self.interaction_logger.log_entity_extraction(
                                user_input=message,
                                entities={"rfq_id": result["rfq_id"]},
                                completeness=100 if result["rfq_id"] else 0,
                                workflow_type=mapped_workflow,
                                model_used=self.default_model,
                                processing_time=processing_time,
                                missing_fields=[]
                            )
                        elif "is_modification_extraction" in result:
                            # Log for modification extraction format
                            self.interaction_logger.log_entity_extraction(
                                user_input=message,
                                entities={"modifications": result["modifications"], "modification_intent": result["modification_intent"]},
                                completeness=100 if result["has_new_values"] else 0,
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
                    except Exception as log_error:
                        print(f"OpenAI: Logging error (non-critical): {log_error}")
                        # Continue with the main result even if logging fails
                    
                    return result
            
            # Log failed entity extraction
            self.interaction_logger.log_error(
                interaction_type="entity_extraction",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return {"products": [], "completeness": 0, "missing_fields": [], "validation_errors": [], "confidence": 0, "next_questions": [], "success": False}
            
        except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
            error_msg = str(e)
            
            # Log API error
            logger.error(f"OpenAI API error in entity extraction: {error_msg}")

            # Send WhatsApp notification
            await self._notify_openai_error("API Error", error_msg, "extract_entities")
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="entity_extraction",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Entity extraction failed: {error_msg}")
            return {"products": [], "completeness": 0, "missing_fields": [], "validation_errors": [], "confidence": 0, "next_questions": [], "success": False}
        except Exception as e:
            error_msg = str(e)

            # Send WhatsApp notification for unexpected errors
            await self._notify_openai_error("Unexpected Error", error_msg, "extract_entities")
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="entity_extraction",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Entity extraction failed: {error_msg}")
            return {"products": [], "completeness": 0, "missing_fields": [], "validation_errors": [], "confidence": 0, "next_questions": [], "success": False}
        
    @log_service_method("openai_service")
    async def extract_entities_with_summary_context(self, message: str, chat_summaries: List[Dict], workflow_type: str = "rfq_creation", existing_products: List[Dict] = None) -> Dict[str, Any]:
        """
        Extract entities from current message while resolving references to previous conversations.
        
        Uses chat summaries to resolve references like "same as last time", "usual address", etc.
        with actual values from historical conversations. Also merges new information with 
        existing incomplete products when available.
        
        Args:
            message: User message to extract entities from
            chat_summaries: List of recent chat summaries with historical context
            workflow_type: Type of workflow (defaults to rfq_creation)
            existing_products: List of existing incomplete products to merge with new information
            
        Returns:
            Dict with extracted entities, resolved references, and metadata
        """
        start_time = time.time()
        
        try:
            # Load the new entity extraction tool for summaries
            tool_file = "entity_extraction_with_summaries.json"
            with open(self.tools_dir / tool_file, 'r') as f:
                entity_tool = json.load(f)
            
            # Format chat summaries for the prompt
            summaries_text = ""
            for i, summary in enumerate(chat_summaries):
                summaries_text += f"Summary {i+1} ({summary.get('date', 'unknown date')}):\n"
                summaries_text += f"- Summary: {summary.get('summary', '')}\n"
                summaries_text += f"- Entities: {json.dumps(summary.get('entities', {}))}\n"
                summaries_text += f"- RFQ IDs: {summary.get('rfq_ids', [])}\n"
                summaries_text += f"- Outcome: {summary.get('outcome', '')}\n\n"
            
            # Load prompt and format with message and summaries
            prompt_content = self._load_prompt(
                "entity_extraction", 
                "_get_entity_system_prompt_with_summaries",
                message=message,
                chat_summaries=summaries_text
            )
            
            # Track this OpenAI call
            self._track_openai_call("entity_extraction_with_summaries")

            # Log timing breakdown
            prep_time = time.time() - start_time
            logger.debug(f"Summary-aware entity extraction prep time: {prep_time:.2f}s")

            api_call_start = time.time()
            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": message}],
                instructions=prompt_content,
                tools=[entity_tool],
                tool_choice={"type": "function", "name": "extract_entities_with_summaries"}
            )
            api_call_time = time.time() - api_call_start

            # Log cache usage information
            usage = getattr(response, 'usage', None)
            if usage:
                total_input_tokens = getattr(usage, 'input_tokens', 0)
                input_tokens_details = getattr(usage, 'input_tokens_details', None)
                cached_tokens = getattr(input_tokens_details, 'cached_tokens', 0) if input_tokens_details else 0
                output_tokens = getattr(usage, 'output_tokens', 0)

                cache_percentage = (cached_tokens / total_input_tokens * 100) if total_input_tokens > 0 else 0
                logger.info(f"OpenAI API call time: {api_call_time:.2f}s | Input tokens: {total_input_tokens} | Cached: {cached_tokens} ({cache_percentage:.1f}%) | Output: {output_tokens}")
            else:
                logger.debug(f"OpenAI API call time: {api_call_time:.2f}s (no usage data available)")

            processing_time = time.time() - start_time
            logger.debug(f"Total summary-aware entity extraction time: {processing_time:.2f}s")
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    print(f"OpenAI summary-aware extraction args: {args}")
                    
                    result = {
                        "products": args.get("products", []),
                        "resolved_references": args.get("resolved_references", []),
                        "confidence": args.get("confidence", 0),
                        "success": True
                    }
                    
                    # Log successful summary-aware entity extraction
                    self.interaction_logger.log_entity_extraction(
                        user_input=message,
                        entities={"products": result["products"], "resolved_references": result["resolved_references"]},
                        completeness=100,  # Will be calculated per product later
                        workflow_type="summary_aware_" + workflow_type,
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Log failed entity extraction
            self.interaction_logger.log_error(
                interaction_type="summary_aware_entity_extraction",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return {"products": [], "resolved_references": [], "confidence": 0, "success": False}
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="summary_aware_entity_extraction",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Summary-aware entity extraction failed: {error_msg}")
            return {"products": [], "resolved_references": [], "confidence": 0, "success": False}
        
    @log_service_method("openai_service")
    async def analyze_reference_context(self, message: str) -> Dict[str, Any]:
        """
        Analyze if a message contains references to previous conversations.
        
        Uses AI to determine if the user is making references that would benefit
        from historical context and summary-aware entity extraction.
        
        Args:
            message: User message to analyze
            
        Returns:
            Dict with has_references (bool), confidence (float), and analysis details
        """
        start_time = time.time()
        
        try:
            # Load reference detection tool
            tool_file = "reference_detection.json"
            with open(self.tools_dir / tool_file, 'r') as f:
                reference_tool = json.load(f)
            
            # Load reference detection prompt
            prompt_content = self._load_prompt(
                "reference_detection",
                "_get_reference_detection_prompt",
                message=message
            )

            # Track this OpenAI call
            self._track_openai_call("reference_detection")

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": message}],
                instructions=prompt_content,
                tools=[reference_tool],
                tool_choice={"type": "function", "name": "analyze_references"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    print(f"OpenAI reference analysis result: {args}")
                    
                    result = {
                        "has_references": args.get("has_references", False),
                        "confidence": args.get("confidence", 0),
                        "reference_types": args.get("reference_types", []),
                        "detected_phrases": args.get("detected_phrases", []),
                        "reasoning": args.get("reasoning", ""),
                        "success": True
                    }
                    
                    # Log successful reference analysis
                    self.interaction_logger.log_entity_extraction(
                        user_input=message,
                        entities={"reference_analysis": result},
                        completeness=100,
                        workflow_type="reference_detection",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Log failed reference analysis
            self.interaction_logger.log_error(
                interaction_type="reference_analysis",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return {"has_references": False, "confidence": 0, "reference_types": [], "detected_phrases": [], "reasoning": "No analysis available", "success": False}
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="reference_analysis",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Reference analysis failed: {error_msg}")
            return {"has_references": False, "confidence": 0, "reference_types": [], "detected_phrases": [], "reasoning": f"Analysis failed: {error_msg}", "success": False}
        
    @log_service_method("openai_service")
    async def analyze_intent_switch_response(self, message: str, pending_switch_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze user's response to intent switch choice using OpenAI.
        
        Uses AI to understand ambiguous responses like "Continue with new request" 
        that contain conflicting keywords, providing better accuracy than simple keyword matching.
        
        Args:
            message: User's response to the intent switch prompt
            pending_switch_context: Context about the pending switch including current and new intents
            
        Returns:
            Dict with chosen_action, confidence, reasoning, and analysis details
        """
        start_time = time.time()
        
        try:
            # Load intent switch analysis tool
            with open(self.tools_dir / "intent_switch_analysis.json", 'r') as f:
                switch_tool = json.load(f)
            
            # Build context for better analysis
            current_intent = pending_switch_context.get("current_workflow", "unknown")
            new_intent = pending_switch_context.get("new_intent", "unknown") 
            new_message = pending_switch_context.get("new_intent_message", "")
            
            context_text = f"""
User's response: "{message}"

CONTEXT:
- Current workflow: {current_intent}
- New intent detected: {new_intent}
- New intent message was: "{new_message}"

The user was asked to choose between continuing their current workflow or switching to the new intent.
Analyze their response to determine their true choice.
"""

            # Track this OpenAI call
            self._track_openai_call("intent_switch_analysis")

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("intent_switch", "_get_intent_switch_analysis_prompt"),
                tools=[switch_tool],
                tool_choice={"type": "function", "name": "analyze_intent_switch_response"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "chosen_action": args.get("chosen_action"),
                        "confidence": args.get("confidence", 0),
                        "reasoning": args.get("reasoning", ""),
                        "detected_keywords": args.get("detected_keywords", []),
                        "ambiguity_level": args.get("ambiguity_level", "high"),
                        "success": True
                    }
                    
                    logger.debug(f"Intent switch analysis: {result['chosen_action']} (confidence: {result['confidence']}%)")
                    logger.debug(f"Reasoning: {result['reasoning']}")
                    
                    return result
            
            # Fallback response
            return {
                "chosen_action": "continue_current", 
                "confidence": 30, 
                "reasoning": "No function call in response, defaulting to continue current", 
                "detected_keywords": [],
                "ambiguity_level": "high",
                "success": False
            }
            
        except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
            error_msg = str(e)
            
            # Log API error
            logger.error(f"OpenAI API error in intent switch analysis: {error_msg}")
            
            # Send WhatsApp notification
            asyncio.create_task(self._notify_openai_error("API Error", error_msg, "analyze_intent_switch_response"))
            
            logger.error(f"Intent switch analysis failed: {error_msg}")
            return {
                "chosen_action": "continue_current",
                "confidence": 20, 
                "reasoning": f"Technical issue occurred: {error_msg}",
                "detected_keywords": [],
                "ambiguity_level": "high", 
                "success": False
            }
        except Exception as e:
            error_msg = str(e)
            
            # Send WhatsApp notification for unexpected errors
            asyncio.create_task(self._notify_openai_error("Unexpected Error", error_msg, "analyze_intent_switch_response"))
            
            logger.error(f"Intent switch analysis failed: {error_msg}")
            return {
                "chosen_action": "continue_current",
                "confidence": 20, 
                "reasoning": f"Analysis failed: {error_msg}",
                "detected_keywords": [],
                "ambiguity_level": "high", 
                "success": False
            }
        
    @log_service_method("openai_service")
    async def merge_resolved_references_with_entities(self, products: List[Dict], resolved_references: List[Dict], original_message: str) -> Dict[str, Any]:
        """
        Use AI to intelligently merge resolved references into product entities.
        
        Uses the established tool and prompt pattern to merge resolved reference data
        into appropriate product entity fields.
        
        Args:
            products: List of product entities to update
            resolved_references: List of resolved reference objects
            original_message: Original user message for context
            
        Returns:
            Dict with success flag and updated_products list
        """
        start_time = time.time()
        
        try:
            # Load reference merging tool
            tool_file = "reference_merging.json"
            with open(self.tools_dir / tool_file, 'r') as f:
                merge_tool = json.load(f)
            
            # Format data for the prompt
            products_text = json.dumps(products, indent=2)
            references_text = json.dumps(resolved_references, indent=2)
            
            # Load prompt and format with data
            prompt_content = self._load_prompt(
                "reference_merging",
                "_get_reference_merging_prompt",
                original_message=original_message,
                products=products_text,
                resolved_references=references_text
            )

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": original_message}],
                instructions=prompt_content,
                tools=[merge_tool],
                tool_choice={"type": "function", "name": "merge_resolved_references"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    print(f"OpenAI reference merging result: {args}")
                    
                    result = {
                        "success": args.get("success", True),
                        "updated_products": args.get("updated_products", products),
                        "merge_actions": args.get("merge_actions", [])
                    }
                    
                    # Log successful merge
                    self.interaction_logger.log_entity_extraction(
                        user_input=original_message,
                        entities={"merged_products": result["updated_products"]},
                        completeness=100,
                        workflow_type="reference_merging",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Log failed merge
            self.interaction_logger.log_error(
                interaction_type="reference_merging",
                user_input=original_message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            return {"success": False, "updated_products": products, "merge_actions": []}
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="reference_merging",
                user_input=original_message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Reference merging failed: {error_msg}")
            return {"success": False, "updated_products": products, "merge_actions": [], "error": error_msg}
        
    async def generate_response(self, context: dict, query_results: list = None, prompt_file: str = None) -> str:
        """
        Generate contextual response based on query results.
        
        Creates human-like responses that incorporate search results,
        user context, and appropriate next steps or recommendations.
        
        Args:
            context: User and conversation context
            query_results: Optional search/query results to incorporate
            prompt_file: Optional prompt file path in format "category/filename" (without .txt)
            
        Returns:
            Generated response string
        """
        start_time = time.time()
        try:
            if prompt_file:
                # Use prompt file if specified
                category, filename = prompt_file.split("/")
                prompt = self._load_prompt(category, filename, **context)
            else:
                # Build prompt inline (legacy behavior)
                prompt = f"User context: {json.dumps(context)}\n\n"
                if query_results:
                    prompt += f"Search results: {json.dumps(query_results)}\n\n"
                prompt += "Generate an appropriate response for the user based on their context and any available results."

            response = await self.client.responses.create(
                model=self.default_model,
                input=self._build_messages_with_history(context, prompt),
                instructions=self._load_prompt("response_generation", "_get_response_system_prompt")
            )
            processing_time = time.time() - start_time

            self.interaction_logger.log_response_generation(
                context={"prompt_type": "generating response"},
                generated_response=response,
                conversation_stage="generating_response",
                model_used=self.default_model,
                processing_time=processing_time
            )
            
            return response.output_text or "I apologize, but I'm having trouble generating a response right now."
            
        except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
            error_msg = str(e)
            
            # Log API error
            logger.error(f"OpenAI API error in response generation: {error_msg}")
            
            # Send WhatsApp notification
            asyncio.create_task(self._notify_openai_error("API Error", error_msg, "generate_response"))
            
            logger.error(f"Response generation failed: {error_msg}")
            return self._get_fallback_response(context, query_results or [])
        except Exception as e:
            logger.error(f"Response generation failed: {str(e)}")
            return self._get_fallback_response(context, query_results or [])

    async def generate_rfq_status_response(self, context: dict) -> str:
        """
            Generate contextual response for RFQ status check.

            Constructs a natural language response summarizing RFQ statuses,
            enforcing limits like `max_allowed`, and providing next steps or links.

            Args:
                context: Dictionary containing keys like 'rfq_statuses', 'rfq_ids', 'max_allowed', 'user_role', etc.

            Returns:
                A string response suitable for user-facing interfaces.
            """
        try:
            # Build prompt inline
            prompt = f"User context: {json.dumps(context)}\n\n"
            prompt += "Generate an appropriate response for the user based on their context and any available results."

            # Select prompt based on user role
            from app.schemas.user import UserRole
            prompt_name = "_get_buyer_rfq_status_response_prompt" if context.get('user_role') == UserRole.BUYER else "_get_seller_rfq_status_response_prompt"

            response = await self.client.responses.create(
                model=self.default_model,
                input=self._build_messages_with_history(context, prompt),
                instructions=self._load_prompt("response_generation", prompt_name)
            )

            return response.output_text or "I apologize, but I'm having trouble generating a response right now."

        except Exception as e:
            logger.error(f"Response generation failed: {str(e)}")
            return self._get_fallback_response(context,[])

    async def generate_seller_intent(self, context: dict) -> str:
        """
             Generate contextual seller intent response.

            Uses AI to classify the seller's intent based on the given context
            and generate an appropriate natural language response. The function
            builds a prompt, loads the classification tool definition, and calls
            the model to return the structured intent response.

            Args:
                context (dict): Dictionary containing conversation context, such as
                                recent messages, seller actions, and metadata.

            Returns:
                str: Natural language response reflecting the seller's intent.
                     Falls back to a default response in case of errors.
        """
        try:
            # Build prompt inline
            prompt = f"User context: {json.dumps(context)}\n\n"
            prompt += "Generate an appropriate response for the user based on their context and any available results."

            # Load reference merging tool
            tool_file = "classify_seller_intent.json"
            with open(self.tools_dir / tool_file, 'r') as f:
                merge_tool = json.load(f)

            # Extract required parameters for prompt template
            workflow_state = context.get('workflow_state', {}).get('seller_workflow_state', 'awaiting_general_response')
            message_type = context.get('message_type', 'general_assistance')
            credits_available = context.get('seller_credits', 0)
            context_data = json.dumps(context)

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                tools=[merge_tool],
                tool_choice={"type": "function", "name": "classify_seller_intent"},
                instructions=self._load_prompt(
                    "response_generation", 
                    "_get_seller_intent_response_prompt",
                    workflow_state=workflow_state,
                    message_type=message_type,
                    credits_available=credits_available,
                    context_data=context_data
                )
            )



            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    # Return the full intent classification dictionary
                    logger.debug(f"Seller intent classified: {args.get('intent')} (confidence: {args.get('confidence')}%)")
                    return args
            
            logger.warning("No valid function call response for seller intent")
            return {
                "intent": "general_question",
                "confidence": 30,
                "reasoning": "No function call in response",
                "context_clues": [],
                "suggested_response": "I apologize, but I'm having trouble generating a seller intent response right now."
            }

        except Exception as e:
            logger.error(f"Seller intent generation failed: {str(e)}")
            return self._get_fallback_response(context, [])

    async def generate_seller_rfq_overview_response(self, context: dict) -> str:
        """
            Generate contextual response for seller RFQ overview flow.

            Builds a concise message for sellers showing count of live RFQs,
            lists latest RFQs, and tailors CTA based on credits/subscription.

            Args:
                context: Dictionary containing keys like 'total_count', 'latest_rfqs',
                         'credits_available', 'plans', and 'workflow_step'.

            Returns:
                A string response suitable for user-facing interfaces.
        """
        try:
            # Build prompt inline
            prompt = f"User context: {json.dumps(context)}\n\n"
            prompt += "Generate an appropriate response for the user based on their context and any available results."

            response = await self.client.responses.create(
                model=self.default_model,
                input=self._build_messages_with_history(context, prompt),
                instructions=self._load_prompt("response_generation", "_get_seller_rfq_overview_prompt")
            )

            return response.output_text or "I apologize, but I'm having trouble generating a response right now."

        except Exception as e:
            logger.error(f"Response generation failed: {str(e)}")
            return self._get_fallback_response(context,[])

    def _load_prompt(self, prompt_type: str, prompt_name: str, **kwargs) -> str:
        """Load and format a prompt template."""
        try:
            # Add support_email to kwargs if not already provided
            if 'support_email' not in kwargs:
                kwargs['support_email'] = self.settings.support_email
            if 'support_info_email' not in kwargs:
                kwargs['support_info_email'] = self.settings.support_contact_info
            # Handle seller end-of-flow reminder prompts
            if prompt_name == "_get_seller_common_response_prompt":
                workflow_state = kwargs.get("workflow_state", "")
                if workflow_state in ["end_of_flow_reminder", "generic_closing_message", "standard_closing_message"]:
                    # Use the specialized end-of-flow prompt
                    prompt_path = os.path.join(
                        self.prompts_dir,
                        prompt_type,
                        "_get_seller_end_of_flow_reminder_prompt.txt"
                    )
                else:
                    # Use the regular seller prompt
                    prompt_path = os.path.join(
                        self.prompts_dir,
                        prompt_type,
                        f"{prompt_name}.txt"
                    )
            else:
                prompt_path = os.path.join(
                    self.prompts_dir,
                    prompt_type,
                    f"{prompt_name}.txt"
                )

            with open(prompt_path, 'r', encoding='utf-8') as f:
                template = f.read()

            # Add support_email to kwargs if not already provided
            if 'support_email' not in kwargs:
                kwargs['support_email'] = self.settings.support_email

            # Format the template with provided arguments
            if kwargs:
                return template.format(**kwargs)
            return template

        except Exception as e:
            logger.error(f"Error loading prompt {prompt_name}: {e}")
            # Return a fallback prompt
            return "Generate an appropriate response for the seller based on the current workflow state and context."

    async def validate_field_value(self, field_name: str, value: str, context: dict) -> Dict[str, Any]:
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

            response = await self.client.responses.create(
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
                "sell_something": 10,
                "bfs_search": 10,
                "account_switch": 5,
                "general_inquiry": 30,
                "modification_request": 10,
                "confirmation_response": 10,
                "rfq_status_check": 10,
                "reference_request": 5,
                "contextual_reference": 5,
                "session_inquiry": 5,
                "workflow_rejection": 5,
                "alternative_request": 5,
                "greeting": 10
            },
            "context_analysis": {
                "references_existing_data": False,
                "conversation_stage": "unknown",
                "modification_details": {"target_entity": None, "modification_type": None},
                "confirmation_details": {"response_type": None, "has_conditions": False},
                "reference_details": {"reference_type": None, "has_history": False},
                "account_switch_details": {"target_role": None, "switch_type": None}
            },
            "reasoning": f"Fallback classification due to technical issue: {error}",
            "suggested_clarification": "There seems to be a technical issue at the moment. Our team is working on it. Please try again later.",
            "success": False
        }
        
    def _get_fallback_response(self, context: dict, results: list) -> str:
        """Fallback response when OpenAI is unavailable."""
        return f"There seems to be a technical issue at the moment. Our team is working on it. Please try again later. For urgent requirements, contact {self.settings.support_email}. We apologize for the inconvenience."
    
    async def generate_contextual_response(self, context: dict, base_questions: list = None, conversation_stage: str = "collecting") -> str:
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

            response = await self.client.responses.create(
                model=self.default_model,
                input=self._build_messages_with_history(context, prompt),
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
                    # Format response with proper spacing
                    response_parts = []
                    if args.get("acknowledgment"):
                        response_parts.append(args["acknowledgment"])
                    if args.get("progress_update"):
                        response_parts.append(args["progress_update"])
                    if args.get("next_question"):
                        response_parts.append(args["next_question"])
                    generated_response = "\n\n".join(response_parts)
                    
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
    
    async def generate_completion_response(self, rfq_data: dict, context: dict) -> str:
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

            response = await self.client.responses.create(
                model=self.default_model,
                input=self._build_messages_with_history(context, prompt),
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
                    # Format response with proper spacing and sections
                    response_parts = []
                    if args.get("confirmation"):
                        response_parts.append(args["confirmation"])
                    if args.get("summary"):
                        response_parts.append(f"Summary:\n{args['summary']}")
                    if args.get("next_steps"):
                        response_parts.append(f"Next Steps:\n{args['next_steps']}")
                    if args.get("reference_id"):
                        response_parts.append(f"Reference: {args['reference_id']}")
                    generated_response = "\n\n".join(response_parts)
                    
                    # Log successful completion response
                    self.interaction_logger.log_response_generation(
                        context=context,
                        generated_response=generated_response,
                        conversation_stage="completion",
                        model_used=self.default_model,
                        processing_time=processing_time
                    )
                    
                    return generated_response
            
            return "RFQ completed. Processing request."
            
        except Exception as e:
            logger.error(f"Completion response generation failed: {str(e)}")
            return "RFQ completed. Processing request."
    
    async def generate_clarification_response(self, questions: list, completeness: float, context: dict) -> str:
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
            prompt = f"Starts with a polite acknowledgment of the user’s request {context.get('user_message', '')}"
            prompt += "Mention that Request for Quotation (RFQ) will be created"
            prompt += f"Generate clarification response:\n\n"
            prompt += f"Completeness: {completeness}%\n"
            prompt += f"Questions to ask: {questions}\n"
            prompt += f"User message: '{context.get('user_message', '')}'\n"

            if context.get('extracted_entities'):
                prompt += f"Current entities: {json.dumps(context['extracted_entities'])}\n"

            response = await self.client.responses.create(
                model=self.default_model,
                input=self._build_messages_with_history(context, prompt),
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
                    # Format response with proper spacing and structure
                    response_parts = []
                    if args.get("progress_acknowledgment"):
                        response_parts.append(args["progress_acknowledgment"])
                    if args.get("questions"):
                        questions_text = "\n".join(f"• {q}" for q in args["questions"])
                        
                        response_parts.append(f"Please provide the following:\n\n{questions_text}")
                    generated_response = "\n\n".join(response_parts)
                    
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
            questions_text = "\n".join(f"• {q}" for q in questions)
            return f"I need a few more details:\n\n{questions_text}"
            
        except Exception as e:
            logger.error(f"Clarification response generation failed: {str(e)}")
            # Fallback response inline
            questions_text = "\n".join(f"• {q}" for q in questions)
            return f"I need a few more details:\n\n{questions_text}"
    
    @log_service_method("openai_service")
    async def detect_excel_header_row(self, sample_rows: List[List]) -> Dict[str, Any]:
        """
        Detect header row in Excel data using OpenAI.
        
        Args:
            sample_rows: First few rows of Excel data as list of lists
            
        Returns:
            Dict with header row index and confidence
        """
        start_time = time.time()
        
        try:
            # Load Excel header detection tool
            with open(self.tools_dir / "excel_header_detection.json", 'r') as f:
                header_tool = json.load(f)
            
            # Prepare sample data for analysis
            rows_text = ""
            for i, row in enumerate(sample_rows):
                row_str = [str(cell) if cell is not None else "" for cell in row]
                rows_text += f"Row {i}: {row_str}\n"
            
            prompt = f"Analyze these Excel rows to detect the header row:\n\n{rows_text}"

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("excel_analysis", "_get_excel_header_detection_prompt"),
                tools=[header_tool],
                tool_choice={"type": "function", "name": "detect_header_row"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "header_row_index": args.get("header_row_index"),
                        "confidence": args.get("confidence", 0),
                        "reasoning": args.get("reasoning", ""),
                        "success": True
                    }
                    
                    logger.info(f"Header detection: row {result['header_row_index']}, confidence {result['confidence']}")
                    return result
            
            return {"header_row_index": 0, "confidence": 50, "reasoning": "Fallback to first row", "success": False}
            
        except Exception as e:
            logger.error(f"Excel header detection failed: {str(e)}")
            return {"header_row_index": 0, "confidence": 30, "reasoning": f"Error: {str(e)}", "success": False}
    
    @log_service_method("openai_service")
    async def select_division(self, extracted_data: dict) -> Dict[str, Any]:
        """
        Select the most relevant division from predefined list based on extracted RFQ data.
        
        Args:
            extracted_data: Dictionary containing extracted RFQ entities and product information
            
        Returns:
            Dict with selected division, confidence score, and reasoning
        """
        start_time = time.time()
        
        try:
            # Load division selection tool
            with open(self.tools_dir / "division_selection.json", 'r') as f:
                division_tool = json.load(f)
            
            # Build analysis prompt
            prompt = f"""
            Analyze the following RFQ data and select the most appropriate division:
            
            Extracted data: {json.dumps(extracted_data, indent=2)}
            
            Consider product descriptions, specifications, quantities, and any other relevant information to determine which division this procurement request should be assigned to.
            """

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("division_selection", "_get_division_selection_prompt"),
                tools=[division_tool],
                tool_choice={"type": "function", "name": "select_division"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "selected_division": args.get("selected_division"),
                        "confidence": args.get("confidence", 0),
                        "reasoning": args.get("reasoning", ""),
                        "alternative_divisions": args.get("alternative_divisions", []),
                        "product_category_analysis": args.get("product_category_analysis", {}),
                        "success": True
                    }
                    
                    logger.debug(f"Division selected: {result['selected_division']} (confidence: {result['confidence']}%)")
                    return result
            
            # Log failed division selection
            self.interaction_logger.log_error(
                interaction_type="division_selection",
                user_input=str(extracted_data),
                error_message="No function call in response",
                model_used=self.default_model
            )
            return {
                "selected_division": "Admin & IT",  # Fallback
                "confidence": 30,
                "reasoning": "No function call in response, using fallback",
                "alternative_divisions": [],
                "product_category_analysis": {},
                "success": False
            }
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="division_selection",
                user_input=str(extracted_data),
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Division selection failed: {error_msg}")
            return {
                "selected_division": "Admin & IT",  # Fallback
                "confidence": 20,
                "reasoning": f"Error occurred: {error_msg}",
                "alternative_divisions": [],
                "product_category_analysis": {},
                "success": False
            }
    
    @log_service_method("openai_service")
    async def generate_session_summary(self, session_data: Dict[str, Any]) -> str:
        """
        Generate session summary using OpenAI.
        
        Args:
            session_data: Dictionary containing session information
            
        Returns:
            Summary text string
        """
        try:
            # Build prompt with session data
            prompt = f"""
            Summarize this chat session for future context:
            
            User: {session_data.get('user_id', 'Unknown')}
            Workflow: {session_data.get('workflow_type', 'Unknown')}
            Outcome: {session_data.get('outcome', 'Unknown')}
            Entities: {json.dumps(session_data.get('extracted_entities', {}), indent=2)}
            RFQ IDs: {session_data.get('rfq_ids', [])}
            """
            
            # Add conversation history if available (keep it simple)
            conversation_messages = session_data.get('conversation_messages', [])
            if conversation_messages:
                prompt += f"\n\nRecent conversation (last few messages):\n"
                # Just include last 5 messages for context
                recent_messages = conversation_messages[-5:]
                for msg in recent_messages:
                    sender = msg.get('sender', 'unknown')
                    content = msg.get('content', '')[:100]  # Keep it short
                    prompt += f"{sender}: {content}\n"
            
            prompt += "\n\nCreate a brief, clear summary of what the user wanted and what was accomplished."

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("session_summary", "_get_session_summary_system_prompt")
            )
            
            return response.output_text or "Session completed"
            
        except Exception as e:
            logger.error(f"Session summary generation failed: {str(e)}")
            return "Session completed"

    @log_service_method("openai_service") 
    async def map_excel_columns(self, headers: List[str]) -> Dict[str, Any]:
        """
        Map Excel column headers to standard RFQ format using OpenAI.
        
        Args:
            headers: List of Excel column headers
            
        Returns:
            Dict with column mapping and confidence scores
        """
        start_time = time.time()
        
        try:
            # Load Excel column mapping tool
            with open(self.tools_dir / "excel_column_mapping.json", 'r') as f:
                mapping_tool = json.load(f)
            
            headers_text = ", ".join([f'"{header}"' for header in headers])
            target_columns = ['S.No', 'ItemDescription', 'Specification', 'Uom', 'Quantity', 'Remarks']
            target_text = ", ".join(target_columns)
            
            prompt = f"""
            Map these Excel headers to standard RFQ format:
            
            Excel headers: {headers_text}
            Target columns: {target_text}
            
            Find the best matches for each target column.
            """

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("excel_analysis", "_get_excel_column_mapping_prompt"),
                tools=[mapping_tool],
                tool_choice={"type": "function", "name": "map_columns"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    # Extract column mapping from mapping_details if column_mapping is empty
                    column_mapping = args.get("column_mapping", {})
                    if not column_mapping and args.get("mapping_details"):
                        for detail in args["mapping_details"]:
                            excel_header = detail.get("excel_header")
                            target_column = detail.get("target_column")
                            if excel_header and target_column:
                                column_mapping[excel_header] = target_column
                    
                    result = {
                        "column_mapping": column_mapping,
                        "confidence": args.get("confidence", 0),
                        "unmapped_headers": args.get("unmapped_headers", []),
                        "reasoning": args.get("reasoning", ""),
                        "success": True
                    }
                    

                    return result
            
            return {"column_mapping": {}, "confidence": 30, "unmapped_headers": headers, "reasoning": "No mapping found", "success": False}
            
        except Exception as e:
            logger.error(f"Excel column mapping failed: {str(e)}")
            return {"column_mapping": {}, "confidence": 20, "unmapped_headers": headers, "reasoning": f"Error: {str(e)}", "success": False}

    async def parse_seller_product_items(self, details: str) -> Dict[str, Any]:
        """
        Parse seller's product/service details string into individual categorizable items.
        This method uses AI to intelligently parse the seller's description of their
        products/services (which could be comma-separated, line-separated, or natural text)
        into individual items that can be categorized separately.
        Args:
            details: The seller's product/service details as a string
        Returns:
            Dict containing:
                - success: Boolean indicating if parsing was successful
                - items: List of individual product/service strings
                - confidence: Confidence score (0-100)
                - original_text: The original details text
        Example:
            Input: "Medical equipment, surgical instruments, hospital supplies"
            Output: {
                "success": True,
                "items": ["Medical equipment", "Surgical instruments", "Hospital supplies"],
                "confidence": 95,
                "original_text": "Medical equipment, surgical instruments, hospital supplies"
            }
        """
        start_time = time.time()

        try:
            # Track this OpenAI call
            self._track_openai_call("parse_seller_products")

            # Load tool definition
            with open(self.tools_dir / "parse_seller_products.json", 'r') as f:
                parse_tool = json.load(f)

            # Load system prompt
            system_prompt = self._load_prompt("seller_product_parsing", "_parse_seller_products_prompt")

            api_call_start = time.time()
            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": f"Parse this seller's product/service details:\n\n{details}"}],
                instructions=system_prompt,
                tools=[parse_tool],
                tool_choice={"type": "function", "name": "parse_product_items"}
            )
            api_call_time = time.time() - api_call_start

            # Log cache usage
            usage = getattr(response, 'usage', None)
            if usage:
                total_input_tokens = getattr(usage, 'input_tokens', 0)
                input_tokens_details = getattr(usage, 'input_tokens_details', None)
                cached_tokens = getattr(input_tokens_details, 'cached_tokens', 0) if input_tokens_details else 0
                output_tokens = getattr(usage, 'output_tokens', 0)

                cache_percentage = (cached_tokens / total_input_tokens * 100) if total_input_tokens > 0 else 0
                logger.info(f"Parse seller products API call: {api_call_time:.2f}s | Input: {total_input_tokens} | Cached: {cached_tokens} ({cache_percentage:.1f}%) | Output: {output_tokens}")

            processing_time = time.time() - start_time
            logger.info(f"Total seller product parsing time: {processing_time:.2f}s")

            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)

                    items = args.get("items", [])
                    confidence = args.get("confidence", 0)

                    logger.info(f"Parsed {len(items)} items from seller details with {confidence}% confidence")

                    return {
                        "success": True,
                        "items": items,
                        "confidence": confidence,
                        "original_text": details,
                        "processing_time": processing_time
                    }

            # Fallback: if parsing fails, return the original text as a single item
            logger.warning("Failed to parse seller details, using fallback")
            return {
                "success": True,
                "items": [details],
                "confidence": 50,
                "original_text": details,
                "processing_time": processing_time,
                "fallback_used": True
            }

        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time

            logger.error(f"Error parsing seller product items: {error_msg}")

            # Fallback: return original text as single item
            return {
                "success": True,
                "items": [details],
                "confidence": 30,
                "original_text": details,
                "processing_time": processing_time,
                "error": error_msg,
                "fallback_used": True
            }

    @log_service_method("openai_service")
    async def categorize_with_similar_items(self, item_description: str, similar_items: List[Dict[str, Any]], available_categories: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Categorize an RFQ item using similar items from vector search.

        Uses OpenAI function calling to make final categorization decision based on
        vector search results, with confidence scoring and reasoning.

        Args:
            item_description: Description of the item to categorize
            similar_items: List of similar items with category information and similarity scores
            available_categories: Optional list of available categories to constrain the selection

        Returns:
            Dict with categorization result, confidence score, and reasoning
        """
        start_time = time.time()

        try:
            # Create dynamic auto-categorization tool with enum constraint
            if available_categories:
                categories_list = list(set(available_categories)) + ["Other"]
            else:
                # Extract categories from similar items as fallback
                categories_list = list(set([item['category'] for item in similar_items])) + ["Other"]
            
            # Create tool with strict enum constraint
            categorization_tool = {
                "type": "function",
                "name": "categorize_item", 
                "description": "Categorize an RFQ item based on similar items and their categories. You must choose from the available categories provided.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": f"The category for the item. Must be one of: {', '.join(categories_list)}",
                            "enum": categories_list  # Strict constraint
                        },
                        "confidence_score": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Confidence score between 0 and 1 for the categorization"
                        },
                        "reasoning": {
                            "type": "string", 
                            "description": "Brief explanation of why this category was selected based on similar items and available options"
                        }
                    },
                    "required": ["category", "confidence_score", "reasoning"]
                }
            }
            
            # Format similar items for the prompt
            similar_items_text = ""
            for i, item in enumerate(similar_items, 1):
                similar_items_text += f"""
{i}. Item: "{item['item']}" (Similarity: {item['similarity_score']})
   Category: {item['category']}
"""
            
            # Extract available categories from similar items or use default
            if available_categories:
                categories_list = list(set(available_categories)) + ["Other"]
                # Format each category on its own line with bullet points
                categories_formatted = '\n'.join([f"- {cat}" for cat in categories_list])
                available_categories_text = f"\nAVAILABLE CATEGORIES (you must choose one of these):\n{categories_formatted}\n"
            else:
                # Extract categories from similar items as fallback
                item_categories = list(set([item['category'] for item in similar_items])) + ["Other"]
                categories_formatted = '\n'.join([f"- {cat}" for cat in item_categories])
                available_categories_text = f"\nAVAILABLE CATEGORIES (you must choose one of these):\n{categories_formatted}\n"
            
            prompt = f"""
Item to categorize: "{item_description}"

Top similar items from database (ranked by similarity):
{similar_items_text}
{available_categories_text}

IMPORTANT: You must choose from the available categories listed above. If none are appropriate, select 'Other'.

Determine the best category for the input item based on the similar items and their categories.
"""

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("auto_categorization", "_get_auto_categorization_system_prompt"),
                tools=[categorization_tool],
                tool_choice={"type": "function", "name": "categorize_item"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    result = {
                        "category": args.get("category"),
                        "confidence": args.get("confidence_score", 0),
                        "reasoning": args.get("reasoning", ""),
                        "success": True
                    }
                    
                    # Log successful categorization
                    self.interaction_logger.log_entity_extraction(
                        user_input=item_description,
                        entities=result["category"],
                        completeness=100,
                        workflow_type="auto_categorization",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    logger.debug(f"Auto-categorized item: {result['category']} (confidence: {result['confidence']})")
                    return result
            
            # Log failed categorization
            self.interaction_logger.log_error(
                interaction_type="auto_categorization",
                user_input=item_description,
                error_message="No function call in response",
                model_used=self.default_model
            )
            
            # Fallback to first similar item
            if similar_items:
                fallback_item = similar_items[0]
                return {
                    "category": fallback_item["category"],
                    "confidence_score": max(fallback_item["similarity_score"] * 0.7, 0.3),
                    "reasoning": "Fallback to most similar item due to AI categorization failure",
                    "success": False
                }
            
            return {
                "category": None,
                "confidence_score": 0,
                "reasoning": "No function call in response",
                "success": False
            }
            
        except Exception as e:
            error_msg = str(e)
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="auto_categorization",
                user_input=item_description,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Auto-categorization failed: {error_msg}")
            
            # Fallback to first similar item
            if similar_items:
                fallback_item = similar_items[0]
                return {
                    "category": fallback_item["category"],
                    "confidence_score": max(fallback_item["similarity_score"] * 0.5, 0.2),
                    "reasoning": f"Fallback to most similar item due to error: {error_msg}",
                    "success": False
                }
            
            return {
                "category": None,
                "confidence_score": 0,
                "reasoning": f"Error occurred: {error_msg}",
                "success": False
            }

    @log_service_method("openai_service")
    async def generate_3_level_categorization(
        self, 
        item_description: str, 
        similar_items: List[Dict[str, Any]], 
        client_category: str = None
    ) -> Dict[str, Any]:
        """
        Generate a 3-level categorization for an item using OpenAI.
        
        Creates a structured 3-level hierarchy (Level 1 > Level 2 > Level 3)
        based on item description and context from similar items.
        
        Args:
            item_description: Description of the item to categorize
            similar_items: List of similar items with categories for context
            client_category: Original client category for reference
            
        Returns:
            Dict with 3-level categorization and metadata
        """
        start_time = time.time()
        
        try:
            # Load learning categorization tool
            with open(self.tools_dir / "learning_categorization.json", 'r') as f:
                learning_tool = json.load(f)
            
            # Format context information
            context_text = f"Item to categorize: \"{item_description}\"\n"
            
            if client_category:
                context_text += f"Current client category: {client_category}\n"
            
            if similar_items:
                context_text += "\nSimilar items for context:\n"
                for i, item in enumerate(similar_items[:3], 1):  # Use top 3 similar items
                    context_text += f"{i}. \"{item.get('item', '')}\" -> {item.get('category', '')} (similarity: {item.get('similarity_score', 0):.2f})\n"

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("learning_categorization", "_generate_3_level_category"),
                tools=[learning_tool],
                tool_choice={"type": "function", "name": "generate_3_level_categorization"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    result = {
                        "success": True,
                        "categorization": {
                            "level_1": args.get("level_1_category"),
                            "level_2": args.get("level_2_category"),
                            "level_3": args.get("level_3_category")
                        },
                        "confidence_score": args.get("confidence_score", 0.8),
                        "reasoning": args.get("reasoning", ""),
                        "processing_time_ms": int(processing_time * 1000)
                    }
                    
                    logger.debug(f"Generated 3-level categorization: {result['categorization']}")
                    return result
            
            # Close client synchronously to prevent event loop errors
            self.close_sync()
            return {
                "success": False,
                "error": "No function call in response",
                "processing_time_ms": int(processing_time * 1000)
            }
            
        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time
            
            logger.error(f"3-level categorization failed: {error_msg}")
            # Close client synchronously to prevent event loop errors
            self.close_sync()
            return {
                "success": False,
                "error": error_msg,
                "processing_time_ms": int(processing_time * 1000)
            }

    @log_service_method("openai_service")
    async def validate_learning_category(
        self, 
        level_1: str, 
        level_2: str, 
        level_3: str, 
        item_description: str
    ) -> Dict[str, Any]:
        """
        Validate a 3-level learning category using OpenAI.
        
        Args:
            level_1: Level 1 category
            level_2: Level 2 category
            level_3: Level 3 category
            item_description: Item description for context
            
        Returns:
            Dict with validation results
        """
        try:
            # Load category validation tool
            with open(self.tools_dir / "category_validation.json", 'r') as f:
                validation_tool = json.load(f)
            
            validation_text = f"""
            Validate this 3-level categorization:
            
            Item: "{item_description}"
            Level 1: {level_1}
            Level 2: {level_2} 
            Level 3: {level_3}
            
            Check if this categorization makes logical sense and is appropriately specific.
            """

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": validation_text}],
                instructions=self._load_prompt("learning_categorization", "_validate_learning_category"),
                tools=[validation_tool],
                tool_choice={"type": "function", "name": "validate_categorization"}
            )
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    return {
                        "is_valid": args.get("is_valid", False),
                        "confidence_score": args.get("confidence_score", 0.0),
                        "validation_issues": args.get("validation_issues", []),
                        "suggestions": args.get("suggestions", [])
                    }
            
            return {
                "is_valid": True,
                "confidence_score": 0.5,
                "validation_issues": [],
                "suggestions": []
            }
            
        except Exception as e:
            logger.error(f"Category validation failed: {str(e)}")
            return {
                "is_valid": False,
                "confidence_score": 0.0,
                "validation_issues": [f"Validation error: {str(e)}"],
                "suggestions": []
            }

    @log_service_method("openai_service")
    async def map_seller_category_to_existing_learning(
        self, 
        seller_category: str, 
        existing_categories: List[Dict[str, Any]],
        seller_name: str = None,
        location_info: dict = None
    ) -> Dict[str, Any]:
        """
        Map a seller's simple category to existing 3-level learning categories.
        
        Args:
            seller_category: Simple seller category (e.g., "Electronics")
            existing_categories: List of existing learning categories to choose from
            seller_name: Seller name for context
            location_info: Seller location for context
            
        Returns:
            Dict with best matching existing category
        """
        start_time = time.time()
        
        try:
            # Load seller mapping tool
            with open(self.tools_dir / "seller_existing_category_mapping.json", 'r') as f:
                mapping_tool = json.load(f)
            
            # Build context with existing categories
            context_text = f"Seller category to map: \"{seller_category}\"\n\n"
            
            if seller_name:
                context_text += f"Seller name: {seller_name}\n"
            
            if location_info and isinstance(location_info, dict):
                city = location_info.get('city', '')
                state = location_info.get('state', '')
                if city or state:
                    context_text += f"Location: {city}, {state}\n"
            
            # Add existing categories for selection
            context_text += "\nExisting Learning Categories to choose from:\n"
            for i, cat in enumerate(existing_categories, 1):
                category_path = f"{cat['level_1_category']} > {cat['level_2_category']} > {cat['level_3_category']}"
                context_text += f"{i}. {category_path} (ID: {cat['id']})\n"

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("seller_mapping", "_map_seller_to_existing_categories"),
                tools=[mapping_tool],
                tool_choice={"type": "function", "name": "select_existing_category"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    # Find the selected category by ID
                    selected_category_id = args.get("selected_category_id")
                    selected_category = None
                    
                    for cat in existing_categories:
                        if cat['id'] == selected_category_id:
                            selected_category = cat
                            break
                    
                    if selected_category:
                        result = {
                            "success": True,
                            "selected_category": selected_category,
                            "similarity_score": args.get("similarity_score", 0.8),
                            "reasoning": args.get("reasoning", ""),
                            "processing_time_ms": int(processing_time * 1000)
                        }
                        
                        category_path = f"{selected_category['level_1_category']} > {selected_category['level_2_category']} > {selected_category['level_3_category']}"
                        logger.debug(f"Mapped seller category '{seller_category}' to existing: {category_path}")
                        return result
                    else:
                        return {
                            "success": False,
                            "error": f"Selected category ID {selected_category_id} not found",
                            "processing_time_ms": int(processing_time * 1000)
                        }
            
            return {
                "success": False,
                "error": "No function call in response",
                "processing_time_ms": int(processing_time * 1000)
            }
            
        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time
            
            logger.error(f"Seller category mapping failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "processing_time_ms": int(processing_time * 1000)
            }
    
    @log_service_method("openai_service")
    async def select_best_sellers(self, item_description: str, candidate_sellers: List[Dict[str, Any]], max_sellers: int = 10) -> Dict[str, Any]:
        """
        Use OpenAI to select and rank the best sellers from candidates for a specific item.
        
        Uses OpenAI function calling to intelligently select and rank sellers based on
        category match, seller quality, location, and overall suitability.
        
        Args:
            item_description: Description of the item needing sellers
            candidate_sellers: List of candidate sellers with their match information
            max_sellers: Maximum number of sellers to select
            
        Returns:
            Dict with selected sellers and reasoning
        """
        start_time = time.time()
        
        try:
            if not candidate_sellers:
                return {
                    "success": False,
                    "error": "No candidate sellers provided",
                    "processing_time_ms": int((time.time() - start_time) * 1000)
                }
            
            # Load seller selection tool
            with open(self.tools_dir / "seller_selection.json", 'r') as f:
                selection_tool = json.load(f)
            
            # Build context for OpenAI
            context_text = f"Item Description: {item_description}\n\n"
            context_text += f"Please select and rank the best sellers for this item from the following {len(candidate_sellers)} candidates:\n\n"
            
            for i, seller in enumerate(candidate_sellers, 1):
                context_text += f"{i}. Seller ID: {seller.get('seller_id', 'Unknown')}\n"
                context_text += f"   Name: {seller.get('seller_name', 'Unknown')}\n"
                context_text += f"   Ranking: {seller.get('ranking', 'Unknown')}\n"
                context_text += f"   Category Match: {seller.get('category_match', {}).get('original_category', 'Unknown')}\n"
                context_text += f"   Similarity Score: {seller.get('category_match', {}).get('similarity_score', 0):.3f}\n"
                context_text += f"   Distance: {seller.get('distance_km', 'Unknown')} km\n"
                context_text += f"   Phone: {seller.get('phone_number', 'Unknown')}\n"
                context_text += f"   Location: {seller.get('location', {}).get('city', 'Unknown')}, {seller.get('location', {}).get('state', 'Unknown')}\n\n"
            
            context_text += f"\nSelect up to {max_sellers} sellers, ranked from best to worst match."

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("seller_selection", "_get_seller_selection_prompt", max_sellers=max_sellers),
                tools=[selection_tool],
                tool_choice={"type": "function", "name": "select_best_sellers"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    selected_ids = args.get("selected_seller_ids", [])
                    reasoning = args.get("reasoning", "")
                    confidence = args.get("confidence_score", 0.8)
                    
                    # Find selected sellers and maintain order
                    selected_sellers = []
                    seller_lookup = {seller["seller_id"]: seller for seller in candidate_sellers}
                    
                    for seller_id in selected_ids:
                        if seller_id in seller_lookup:
                            selected_sellers.append(seller_lookup[seller_id])
                    
                    result = {
                        "success": True,
                        "selected_sellers": selected_sellers,
                        "total_selected": len(selected_sellers),
                        "reasoning": reasoning,
                        "confidence_score": confidence,
                        "processing_time_ms": int(processing_time * 1000),
                        "method": "openai_seller_selection"
                    }
                    
                    # Log successful seller selection
                    self.interaction_logger.log_entity_extraction(
                        user_input=item_description,
                        entities={"selected_sellers": [s["seller_id"] for s in selected_sellers]},
                        completeness=100,
                        workflow_type="seller_selection",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    logger.debug(f"Selected {len(selected_sellers)} sellers with confidence {confidence:.3f}")
                    return result
            
            # Log failed seller selection
            self.interaction_logger.log_error(
                interaction_type="seller_selection",
                user_input=item_description,
                error_message="No function call in response",
                model_used=self.default_model
            )
            
            return {
                "success": False,
                "error": "No function call in response",
                "processing_time_ms": int(processing_time * 1000)
            }
            
        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="seller_selection",
                user_input=item_description,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Seller selection failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "processing_time_ms": int(processing_time * 1000)
            }


    @log_service_method("openai_service")
    async def generate_rfq_confirmation(self, rfq_data: dict, context: dict) -> str:
        """
        Generate well-structured RFQ confirmation message with proper formatting.
        
        Creates a clear confirmation with summary, outstanding questions, and
        confirmation request using structured formatting.
        
        Args:
            rfq_data: RFQ data to summarize
            context: Conversation context
            
        Returns:
            Generated RFQ confirmation message string
        """
        start_time = time.time()
        
        try:
            # Load RFQ confirmation tool
            with open(self.tools_dir / "rfq_confirmation_generation.json", "r") as f:
                confirmation_tool = json.load(f)
            
            # Build context for OpenAI
            context_text = "Generate RFQ confirmation for the following data:\n\n"
            # Clean RFQ data for JSON serialization and format dates
            clean_rfq_data = self._clean_for_json_serialization(rfq_data)

            # Truncate items to max 5 for display (WhatsApp 1024 char limit)
            MAX_DISPLAY_ITEMS = 5
            MAX_SPEC_LENGTH = 70  # Max chars for brand/remarks fields
            items = clean_rfq_data.get("items", [])
            total_items = len(items)
            if total_items > MAX_DISPLAY_ITEMS:
                clean_rfq_data["items"] = items[:MAX_DISPLAY_ITEMS]
                clean_rfq_data["items_truncated"] = True
                clean_rfq_data["total_items"] = total_items
                clean_rfq_data["hidden_items_count"] = total_items - MAX_DISPLAY_ITEMS
                logger.info(f"Truncated RFQ items from {total_items} to {MAX_DISPLAY_ITEMS} for confirmation display")

            # Truncate brand/remarks fields and sanitize control characters in items
            import re
            def sanitize_and_truncate(text: str, max_len: int = MAX_SPEC_LENGTH) -> str:
                """Remove control chars and truncate to max length."""
                if not isinstance(text, str):
                    return text
                # Remove null bytes and control characters (except newlines/tabs)
                text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
                if len(text) > max_len:
                    return text[:max_len - 3] + '...'
                return text

            for item in clean_rfq_data.get("items", []):
                if isinstance(item, dict):
                    if item.get("brand"):
                        item["brand"] = sanitize_and_truncate(item["brand"])
                    if item.get("remarks"):
                        item["remarks"] = sanitize_and_truncate(item["remarks"])
                    # Also sanitize description (but don't truncate as harshly)
                    if item.get("description"):
                        item["description"] = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(item["description"]))

            # Format delivery date for display
            if clean_rfq_data.get("delivery_date"):

                
                delivery_date = clean_rfq_data["delivery_date"]
                if isinstance(delivery_date, str):
                    try:
                        # Parse ISO format date string
                        dt = datetime.fromisoformat(delivery_date.replace('Z', '+00:00'))
                        clean_rfq_data["delivery_date_display"] = format_date_display(dt)
                    except:
                        clean_rfq_data["delivery_date_display"] = delivery_date
                elif isinstance(delivery_date, datetime):
                    clean_rfq_data["delivery_date_display"] = format_date_display(delivery_date)
                else:
                    clean_rfq_data["delivery_date_display"] = str(delivery_date)
            
            context_text += f"RFQ Data: {json.dumps(clean_rfq_data, indent=2)}\n"

            if context.get("user_message"):
                context_text += f"User Message: {context['user_message']}\n"

            # Log the exact data being sent for debugging
            logger.info(f"RFQ confirmation input data: {json.dumps(clean_rfq_data, indent=2)}")

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("response_generation", "_get_rfq_confirmation_system_prompt"),
                tools=[confirmation_tool],
                tool_choice={"type": "function", "name": "generate_rfq_confirmation"}
            )

            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)

                    # Log the LLM's generated summary for debugging
                    logger.info(f"RFQ confirmation LLM output: {json.dumps(args, indent=2)}")

                    # Format response with proper spacing and sections
                    # Only return the summary - prefix/suffix are added by the handler
                    if args.get("summary"):
                        summary = args['summary']
                        # Fix literal \n in the output - the AI sometimes returns escaped newlines as text
                        # Replace all occurrences of literal \n with actual newline characters
                        if '\\n' in summary:
                            logger.warning("Found escaped newlines in summary, converting to actual newlines")
                            summary = summary.replace('\\n', '\n')

                        # Truncate summary to fit WhatsApp interactive message body limit (1024 chars)
                        # Reserve space for footer and buttons overhead
                        MAX_BODY_LENGTH = 900
                        if len(summary) > MAX_BODY_LENGTH:
                            logger.warning(f"RFQ confirmation summary too long ({len(summary)} chars), truncating to {MAX_BODY_LENGTH}")
                            # Find a good truncation point (end of a line)
                            truncated = summary[:MAX_BODY_LENGTH]
                            last_newline = truncated.rfind('\n')
                            if last_newline > MAX_BODY_LENGTH * 0.7:  # Keep at least 70% of content
                                truncated = truncated[:last_newline]
                            # Add indicator that content was truncated
                            if total_items > MAX_DISPLAY_ITEMS:
                                truncated += f"\n\n+{total_items - MAX_DISPLAY_ITEMS} more items..."
                            summary = truncated

                        return summary

                    # Fallback if no summary
                    return "No summary generated"
            
            # Fallback response
            return "Here's a summary of your RFQ."
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"RFQ confirmation generation failed: {error_msg}")
            return "Here's a summary of your RFQ."
    
    def _clean_for_json_serialization(self, obj):
        """Recursively clean object for JSON serialization."""
        from datetime import datetime, date

        if obj is None:
            return None
        elif hasattr(obj, 'value'):  # Enum object
            return obj.value
        elif isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, dict):
            return {key: self._clean_for_json_serialization(value) for key, value in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._clean_for_json_serialization(item) for item in obj]
        elif isinstance(obj, float):
            # Convert float to int if it's a whole number (e.g., 10.0 -> 10)
            if obj == int(obj):
                return int(obj)
            return obj
        elif isinstance(obj, (str, int, bool)):
            return obj
        else:
            # Try to serialize to test, if it fails, convert to string
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)

    @log_service_method("openai_service")
    async def generate_opt_out_confirmation(self, seller_name: str) -> str:
        """Generate opt-out confirmation message for sellers."""
        try:
            prompt = f"Generate a brief WhatsApp message confirming that seller '{seller_name}' has been opted out of RFQ notifications. Include how to opt back in (reply 'opt-in'). Keep friendly, under 50 words."

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions="You are a helpful assistant generating confirmation messages for sellers who opt out of notifications. Be brief, clear, and professional."
            )
            
            return response.output_text or f"Hi {seller_name}, you're now opted out of RFQ notifications. To opt back in, reply 'opt-in'."
            
        except Exception as e:
            logger.error(f"Error generating opt-out confirmation: {str(e)}")
            return f"Hi {seller_name}, you're now opted out of RFQ notifications. To opt back in, reply 'opt-in'."
    
    @log_service_method("openai_service")
    async def generate_opt_in_confirmation(self, seller_name: str, categories: list) -> str:
        """Generate opt-in confirmation message for sellers."""
        try:
            categories_text = ', '.join(categories) if categories else 'your business categories'
            prompt = f"Generate a brief WhatsApp welcome back message for seller '{seller_name}' who opted in for RFQ notifications. Mention their categories: {categories_text}. Include how to opt out (reply 'opt-out'). Keep friendly, under 60 words."

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions="You are a helpful assistant generating welcome back messages for sellers who opt in to notifications. Be brief, clear, and professional."
            )
            
            return response.output_text or f"Hi {seller_name}, welcome back! You'll receive RFQ notifications for {categories_text}. To opt out, reply 'opt-out'."
            
        except Exception as e:
            logger.error(f"Error generating opt-in confirmation: {str(e)}")
            return f"Hi {seller_name}, welcome back! You'll receive RFQ notifications for {categories_text}. To opt out, reply 'opt-out'."
    
    @log_service_method("openai_service")
    async def validate_delivery_date(self, raw_date_input: str, extracted_date: str = None) -> Dict[str, Any]:
        """Validate delivery date using AI-based parsing and validation.
        
        Args:
            raw_date_input: Raw date input from user
            extracted_date: Initially extracted date in YYYY-MM-DD format (optional)
            
        Returns:
            Dict with validation results and normalized date
        """
        start_time = time.time()
        
        try:
            # Load date validation tool
            with open(self.tools_dir / "date_validation.json", 'r') as f:
                date_tool = json.load(f)
            
            # Get current date for context
            from datetime import datetime
            current_date = datetime.now().date()
            current_year = current_date.year
            
            # Build context for AI date validation
            context_text = f"Date input to validate: '{raw_date_input}'\n\n"
            context_text += f"Current date: {current_date.strftime('%Y-%m-%d')} ({current_date.strftime('%d %B %Y')})\n"
            context_text += f"Current year: {current_year}\n\n"
            
            if extracted_date:
                context_text += f"Previously extracted date: {extracted_date}\n\n"
            
            context_text += "Parse and validate this date input. Handle ordinal numbers (30th sept), relative dates (tomorrow), and apply intelligent year assumptions."
            
            # Track this OpenAI call
            self._track_openai_call("ai_date_validation")
            
            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("date_validation", "_get_date_validation_prompt", current_date=current_date.isoformat(), current_year=current_year, current_month=current_date.month),
                tools=[date_tool],
                tool_choice={"type": "function", "name": "validate_delivery_date"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    result = {
                        "is_valid": args.get("is_valid", False),
                        "normalized_date": args.get("normalized_date"),
                        "validation_issues": args.get("validation_issues", []),
                        "user_friendly_message": args.get("user_friendly_message", ""),
                        "confidence": args.get("confidence", 0),
                        "success": True,
                        "parsing_method": "ai_validation",
                        "reasoning": args.get("reasoning", "")
                    }
                    # Additional validation: Check if normalized date is in the past
                    if result["normalized_date"]:
                        try:
                            normalized_date_obj = datetime.strptime(result["normalized_date"], "%Y-%m-%d").date()

                            if normalized_date_obj < current_date:
                                result["is_valid"] = False
                                result["validation_issues"].append("Date is in the past")
                                result[
                                    "user_friendly_message"] = "The delivery date cannot be in the past. Kindly share a valid delivery date from today onward"
                                result["confidence"] = 10
                                logger.warning(
                                    f"Date validation failed: {raw_date_input} -> {result['normalized_date']} is earlier than current date {current_date}")
                        except ValueError as ve:
                            logger.error(f"Failed to parse normalized date {result['normalized_date']}: {str(ve)}")
                            result["is_valid"] = False
                            result["validation_issues"].append("Invalid date format")
                            result["user_friendly_message"] = "Kindly share a valid delivery date from today onward"

                    if result["is_valid"]:
                        logger.debug(f"AI date validation: {raw_date_input} -> {result['normalized_date']} ({result['reasoning']})")
                    else:
                        logger.debug(f"AI date validation failed: {raw_date_input} -> {result['reasoning']}")
                    
                    # Log successful date validation
                    self.interaction_logger.log_entity_extraction(
                        user_input=raw_date_input,
                        entities={"date_validation": result},
                        completeness=100 if result["is_valid"] else 0,
                        workflow_type="ai_date_validation",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Log failed date validation
            self.interaction_logger.log_error(
                interaction_type="ai_date_validation",
                user_input=raw_date_input,
                error_message="No function call in response",
                model_used=self.default_model
            )
            
            return {
                "is_valid": False,
                "normalized_date": None,
                "validation_issues": ["No function call in response"],
                "user_friendly_message": "Kindly share a valid delivery date from today onward",
                "confidence": 20,
                "success": False,
                "parsing_method": "ai_validation"
            }
            
        except Exception as e:
            logger.error(f"AI date validation failed: {str(e)}")
            
            # Log exception in date validation
            processing_time = time.time() - start_time
            self.interaction_logger.log_error(
                interaction_type="ai_date_validation",
                user_input=raw_date_input,
                error_message=str(e),
                model_used=self.default_model
            )
            
            return {
                "is_valid": False,
                "normalized_date": None,
                "validation_issues": [f"Error: {str(e)}"],
                "user_friendly_message": "Kindly share a valid delivery date from today onward",
                "confidence": 20,
                "success": False,
                "parsing_method": "error"
            }
    


    @log_service_method("openai_service")
    async def detect_opt_out_intent(self, message: str) -> Dict[str, Any]:
        """Detect opt-out/opt-in intent in seller messages using function calling."""
        start_time = time.time()
        
        try:
            # Load opt-out intent detection tool
            with open(self.tools_dir / "opt_out_intent_detection.json", 'r') as f:
                intent_tool = json.load(f)

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": message}],
                instructions=self._load_prompt("opt_out_detection", "_get_opt_out_detection_prompt"),
                tools=[intent_tool],
                tool_choice={"type": "function", "name": "detect_opt_out_intent"}
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
                        "reasoning": args.get("reasoning", ""),
                        "detected_phrases": args.get("detected_phrases", []),
                        "success": True
                    }
                    
                    logger.debug(f"Opt-out intent detected: {result['intent']} (confidence: {result['confidence']}%)")
                    return result
            
            return {"intent": "none", "confidence": 30, "reasoning": "No function call in response", "detected_phrases": [], "success": False}
            
        except Exception as e:
            logger.error(f"Opt-out intent detection failed: {str(e)}")
            return {"intent": "none", "confidence": 20, "reasoning": f"Error: {str(e)}", "detected_phrases": [], "success": False}

    @log_service_method("openai_service")
    async def generate_permission_request(self, seller_name: str, categories: list) -> str:
        """Generate permission request message for new sellers."""
        try:
            categories_text = ', '.join(categories) if categories else 'your business categories'
            prompt = f"Generate a brief WhatsApp message asking seller '{seller_name}' for permission to send RFQ notifications. Mention their categories: {categories_text}. Ask them to reply 'yes' or 'no'. Keep friendly, under 70 words."

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions="You are a helpful assistant generating permission request messages for new sellers. Be brief, clear, and professional."
            )
            
            return response.output_text or f"Hi {seller_name}! We found an RFQ matching {categories_text}. Can we send you RFQ notifications? Reply 'yes' or 'no'."
            
        except Exception as e:
            logger.error(f"Error generating permission request: {str(e)}")
            return f"Hi {seller_name}! We found an RFQ matching {categories_text}. Can we send you RFQ notifications? Reply 'yes' or 'no'."
    
    @log_service_method("openai_service")
    def classify_auth_intent(self, message: str) -> Dict[str, Any]:
        """Classify authentication intent (buy/sell/unclear) using function calling."""
        try:
            # Simple implementation using existing classify_intent with auth context
            context = {"workflow_type": "authentication", "stage": "intent_classification"}
            result = self.classify_intent(message, context)
            
            # Map general intents to auth-specific intents
            intent_mapping = {
                "buy_something": "buy",
                "sell_something": "sell", 
                "general_inquiry": "unclear",
                "ambiguous": "unclear"
            }
            
            mapped_intent = intent_mapping.get(result.get("intent"), "unclear")
            
            return {
                "intent": mapped_intent,
                "confidence": result.get("confidence", 0),
                "success": result.get("success", False)
            }
            
        except Exception as e:
            logger.error(f"Auth intent classification error: {e}")
            return {"intent": "unclear", "confidence": 0, "success": False}
    
    @log_service_method("openai_service")
    async def extract_registration_entities(self, message: str, conversation_context: str = "", user_type: str = "buyer", existing_entities: Dict[str, Any] = None) -> Dict[str, Any]:
        """Extract registration entities using proper tools and prompts with validation."""
        start_time = time.time()
        
        try:
            # Load appropriate tool based on user type
            if user_type == "buyer":
                tool_file = "entity_extraction_registration_buyer.json"
                function_name = "extract_buyer_registration_entities"
                prompt_category = "entity_extraction"
                prompt_name = "_get_entity_system_prompt_registration_buyer"
            else:
                tool_file = "entity_extraction_registration_seller.json"
                function_name = "extract_seller_registration_entities"
                prompt_category = "entity_extraction"
                prompt_name = "_get_entity_system_prompt_registration_seller"
            
            with open(self.tools_dir / tool_file, 'r') as f:
                registration_tool = json.load(f)
            
            # Build context with existing entities and conversation history
            context_text = f"User message: {message}\n\n"
            if conversation_context:
                context_text += f"Conversation context: {conversation_context}\n\n"
            if existing_entities:
                context_text += f"Already collected: {json.dumps(existing_entities, indent=2)}\n\n"
            context_text += "Extract and validate new information from the message. Set invalid fields to null."

            # Track this OpenAI call
            self._track_openai_call("registration_entity_extraction")

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt(prompt_category, prompt_name),
                tools=[registration_tool],
                tool_choice={"type": "function", "name": function_name}
            )
            
            processing_time = time.time() - start_time
            
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    print(f"OpenAI registration extraction args: {args}")
                    
                    # Extract the response data - will be post-processed by EntityService
                    entities = args.get("entities", {})
                    completeness = args.get("completeness", 0)
                    missing_fields = args.get("missing_fields", [])
                    validation_errors = args.get("validation_errors", [])
                    confidence = args.get("confidence", 0)
                    
                    result = {
                        "entities": entities,
                        "completeness": completeness,
                        "missing_fields": missing_fields,
                        "validation_errors": validation_errors,
                        "confidence": confidence,
                        "success": True,
                        "extracted_fields": [k for k, v in entities.items() if v is not None],
                        "raw_extraction": True  # Flag for post-processing
                    }
                    
                    # Log successful extraction
                    self.interaction_logger.log_entity_extraction(
                        user_input=message,
                        entities=result["entities"],
                        completeness=result["completeness"],
                        workflow_type=f"registration_{user_type}",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=result["missing_fields"]
                    )
                    
                    return result
            
            return {"entities": {}, "completeness": 0, "missing_fields": [], "validation_errors": [], "confidence": 0, "success": False, "extracted_fields": []}
                
        except Exception as e:
            logger.error(f"Registration entity extraction error: {e}")
            return {"entities": {}, "completeness": 0, "missing_fields": [], "validation_errors": [], "confidence": 0, "success": False, "extracted_fields": []}
    
    async def parse_email_confirmation(self, message: str, emails: list) -> Dict[str, Any]:
        """Parse email confirmation response using OpenAI function calling."""
        try:
            # Load email confirmation tool
            with open(self.tools_dir / "email_confirmation_parsing.json", 'r') as f:
                confirmation_tool = json.load(f)
            
            prompt = f"""Parse this user response to email confirmation: "{message}"
            
Available emails: {', '.join(emails) if emails else 'Single email'}
            
Determine if user is:
- confirming the email (confirmed)
- declining/rejecting the email (declined) 
- giving unclear/invalid response (invalid)
            
If multiple emails and user selected a number, include selection."""

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=self._load_prompt("email_confirmation", "email_confirmation_parsing"),
                tools=[confirmation_tool],
                tool_choice={"type": "function", "name": "parse_email_confirmation"}
            )
            
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    return {"success": True, **args}
            
            return {"success": False}
            
        except Exception as e:
            logger.error(f"Email confirmation parsing error: {e}")
            return {"success": False}

    @log_service_method("openai_service")
    async def handle_contextual_interaction(self, message: str, conversation_history: dict,
                                    workflow_state: dict, extracted_entities: list) -> Dict[str, Any]:
        """
        Handle complex contextual interactions with comprehensive session management.
        
        This method processes user messages that reference previous conversation parts,
        request workflow changes, entity modifications, or state transitions using
        advanced AI reasoning to determine appropriate actions.
        
        Args:
            message: User's contextual message
            conversation_history: Full conversation history with messages
            workflow_state: Current workflow state and metadata
            extracted_entities: Currently extracted entities from session
            
        Returns:
            Dict containing:
            - response: Generated response text
            - actions: List of actions to perform (entity updates, state changes, etc.)
            - context_understanding: Analysis of user intent and referenced data
        """
        start_time = time.time()
        
        try:
            # Load contextual interaction tool
            with open(self.tools_dir / "contextual_interaction_handling.json", 'r') as f:
                contextual_tool = json.load(f)
            
            # Build comprehensive context for AI analysis
            context_text = self._build_contextual_analysis_prompt(
                message, conversation_history, workflow_state, extracted_entities
            )

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_text}],
                instructions=self._load_prompt("contextual_interaction", "_handle_contextual_interaction_prompt"),
                tools=[contextual_tool],
                tool_choice={"type": "function", "name": "handle_contextual_interaction"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    result = {
                        "success": True,
                        "response": args.get("response", "I understand your request."),
                        "actions": args.get("actions", []),
                        "context_understanding": args.get("context_understanding", {}),
                        "processing_time_ms": int(processing_time * 1000)
                    }
                    
                    # Log successful contextual interaction
                    user_intent = result["context_understanding"].get("user_intent", "unknown")
                    confidence = result["context_understanding"].get("confidence", 0)
                    
                    logger.debug(f"Contextual interaction handled - Intent: {user_intent}, Confidence: {confidence}%, Actions: {len(result['actions'])}")
                    
                    # Log detailed interaction for debugging
                    self.interaction_logger.log_entity_extraction(
                        user_input=message,
                        entities={"contextual_actions": result["actions"], "user_intent": user_intent},
                        completeness=confidence,
                        workflow_type="contextual_interaction",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Fallback response
            return {
                "success": False,
                "response": "I understand you're referring to our previous conversation. Could you please clarify what specific changes you'd like me to make?",
                "actions": [],
                "context_understanding": {
                    "user_intent": "unclear",
                    "referenced_data": [],
                    "confidence": 30
                },
                "processing_time_ms": int(processing_time * 1000)
            }
            
        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"Contextual interaction handling failed: {e}")
            
            return {
                "success": False,
                "response": "I had trouble processing your request. Could you please rephrase what you'd like me to do?",
                "actions": [],
                "context_understanding": {
                    "user_intent": "error",
                    "referenced_data": [],
                    "confidence": 0
                },
                "processing_time_ms": int(processing_time * 1000),
                "error": str(e)
            }
    
    def _build_contextual_analysis_prompt(self, message: str, conversation_history: dict,
                                        workflow_state: dict, extracted_entities: list) -> str:
        """Build comprehensive context prompt for AI analysis."""
        
        # Start with user message
        prompt = f"USER MESSAGE: '{message}'\n\n"
        
        # Add current workflow information
        prompt += "CURRENT SESSION STATE:\n"
        prompt += f"- Workflow Type: {workflow_state.get('workflow_type', 'unknown')}\n"
        prompt += f"- Current Stage: {workflow_state.get('stage', 'unknown')}\n"
        prompt += f"- Has Pending Confirmations: {bool(workflow_state.get('pending_combined_rfq') or workflow_state.get('pending_rfq'))}\n"
        
        # Add extracted entities
        if extracted_entities:
            prompt += f"\nCURRENT EXTRACTED ENTITIES ({len(extracted_entities)} items):\n"
            for i, entity in enumerate(extracted_entities[:5], 1):  # Show first 5
                product_name = entity.get('product_name', entity.get('description', f'Item {i}'))
                quantity = entity.get('quantity', 'Not specified')
                prompt += f"{i}. {product_name} - Quantity: {quantity}\n"
                if entity.get('specifications'):
                    prompt += f"   Specs: {entity.get('specifications')}\n"
        else:
            prompt += "\nCURRENT EXTRACTED ENTITIES: None\n"
        
        # Add recent conversation history
        messages = conversation_history.get('messages', [])
        if messages:
            prompt += f"\nRECENT CONVERSATION (last 5 messages):\n"
            for msg in messages[-5:]:
                role = msg.get('role', 'unknown')
                content = msg.get('content', '')[:150]  # Truncate long messages
                prompt += f"{role.capitalize()}: {content}\n"
        
        # Add workflow state details
        if workflow_state:
            prompt += "\nWORKFLOW STATE DETAILS:\n"
            for key, value in workflow_state.items():
                if key not in ['extracted_entities', 'conversation_history'] and value:
                    prompt += f"- {key}: {str(value)[:100]}\n"
        
        prompt += "\nBased on this context, analyze the user's message and determine what contextual actions they want to perform."
        
        return prompt


    async def parse_confirmation_response(self, user_message: str) -> str:
        """
        Parse confirmation response using OpenAI.
        
        Args:
            user_message: User's confirmation response
            
        Returns:
            Parsed response: "yes", "no", or original message if unclear
        """
        
        try:
            prompt_path = os.path.join(self.prompts_dir, "confirmation_response_classification.txt")
            with open(prompt_path, 'r', encoding='utf-8') as f:
                instructions = f.read()

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": user_message}],
                instructions=instructions
            )
            
            result = response.output_text.strip().lower()
            return result if result in ["yes", "no"] else "unclear"
            
        except Exception as e:
            logger.error(f"Error parsing confirmation response: {e}")
            return "unclear"
    
    @log_service_method("openai_service")
    async def detect_registration_type(self, message: str) -> Dict[str, Any]:
        """
        Detect registration type from user message using OpenAI function calling.
        
        Args:
            message: User message to analyze for registration type
            
        Returns:
            Dict with registration type detection result
        """
        try:
            # Load prompt and tool
            with open(self.prompts_dir / "profile_selection" / "registration_type_detection.txt", 'r') as f:
                system_prompt = f.read()
            
            with open(self.tools_dir / "registration_type_detection.json", 'r') as f:
                tool_def = json.load(f)
            
            user_prompt = f"User message: '{message}'"

            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": user_prompt}],
                instructions=system_prompt,
                tools=[tool_def],
                tool_choice={"type": "function", "name": "registration_type_detection"}
            )
            
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    result = json.loads(function_call.arguments)
                    logger.debug(f"Registration type detection result: {result}")
                    return result
            
            logger.warning("Registration type detection: No function call in response")
            return {"success": False, "error": "No function call in response"}
            
        except Exception as e:
            logger.error(f"Registration type detection failed: {str(e)}")
            return {"success": False, "error": str(e)}
    
    async def get_completion(self, prompt: str) -> str:
        """Get simple completion from OpenAI with logging."""
        start_time = time.time()
        
        try:
            response = await self.client.chat.completions.create(
                model=self.default_model,
                messages=[{"role": "user", "content": prompt}],
            )
            
            processing_time = time.time() - start_time
            result = response.choices[0].message.content
            
            self.interaction_logger.log_response_generation(
                context={"prompt_type": "completion"},
                generated_response=result,
                conversation_stage="completion",
                model_used=self.default_model,
                processing_time=processing_time
            )
            
            return result
            
        except Exception as e:
            self.interaction_logger.log_error(
                interaction_type="completion",
                user_input=prompt,
                error_message=str(e),
                model_used=self.default_model
            )
            raise e
    @log_service_method("openai_service")
    async def extract_rfq_ids_from_message(self, message: str, last_bot_message: str) -> Dict[str, Any]:
        """
        Extract RFQ IDs from seller's message using OpenAI function calling.
        
        Args:
            message: User's message containing RFQ references
            last_bot_message: Last bot message for context
            
        Returns:
            Dict with extracted RFQ IDs, confidence, and reasoning
        """
        start_time = time.time()
        
        try:
            # Load RFQ ID extraction tool
            with open(self.tools_dir / "rfq_id_extraction.json", 'r') as f:
                rfq_tool = json.load(f)
            
            # Build context message
            context_msg = f"""User message: {message}
            Last bot message: {last_bot_message}
            
            Extract the specific RFQ IDs the user is asking about based on the context."""
            
            # Track this OpenAI call
            self._track_openai_call("rfq_id_extraction")
            
            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": context_msg}],
                instructions=self._load_prompt("rfq_id_extraction", "rfq_id_extraction_prompt"),
                tools=[rfq_tool],
                tool_choice={"type": "function", "name": "extract_rfq_ids"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    result = {
                        "success": True,
                        "rfq_ids": args.get("rfq_ids", []),
                        "confidence": args.get("confidence", 0),
                        "reasoning": args.get("reasoning", "")
                    }
                    
                    logger.info(f"Extracted RFQ IDs: {result['rfq_ids']} with confidence {result['confidence']}%")
                    
                    # Log successful extraction
                    self.interaction_logger.log_entity_extraction(
                        user_input=message,
                        entities={"rfq_ids": result["rfq_ids"]},
                        completeness=result["confidence"],
                        workflow_type="rfq_id_extraction",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Log failed extraction
            self.interaction_logger.log_error(
                interaction_type="rfq_id_extraction",
                user_input=message,
                error_message="No function call in response",
                model_used=self.default_model
            )
            
            return {
                "success": False,
                "rfq_ids": [],
                "confidence": 0,
                "reasoning": "No function call in response"
            }
            
        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="rfq_id_extraction",
                user_input=message,
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"RFQ ID extraction failed: {error_msg}")
            return {
                "success": False,
                "rfq_ids": [],
                "confidence": 0,
                "reasoning": f"Error: {error_msg}"
            }

    @log_service_method("openai_service")
    async def process_excel_to_rfqs(self, excel_text: str, filename: str) -> Dict[str, Any]:
        """
        Process Excel data directly to multiple RFQ format using OpenAI.
        
        Args:
            excel_text: String representation of Excel data
            filename: Name of the Excel file
            
        Returns:
            Dict with RFQ processing results
        """
        start_time = time.time()
        
        try:
            # Load Excel RFQ processing tool
            with open(self.tools_dir / "excel_rfq_processing.json", 'r') as f:
                excel_tool = json.load(f)
            
            # Load system prompt for Excel processing
            system_prompt = self._load_prompt("excel_analysis", "_get_excel_rfq_processing_prompt")
            
            # Track this OpenAI call
            self._track_openai_call("excel_rfq_processing")
            
            response = await self.client.responses.create(
                model=self.default_model,
                input=[{"role": "user", "content": excel_text}],
                instructions=system_prompt,
                tools=[excel_tool],
                tool_choice={"type": "function", "name": "process_excel_to_rfqs"}
            )
            
            processing_time = time.time() - start_time
            
            # Parse function call response
            if response.output and len(response.output) > 0:
                function_call = response.output[0]
                if function_call.type == "function_call":
                    args = json.loads(function_call.arguments)
                    
                    logger.info(f"[EXCEL-OPENAI] Raw OpenAI extraction result: {json.dumps(args, indent=2)}")
                    
                    result = {
                        "success": True,
                        "rfqs": args.get("rfqs", []),
                        "processing_summary": args.get("processing_summary", {}),
                        "confidence": args.get("confidence", 0)
                    }
                    
                    logger.info(f"[EXCEL-OPENAI] Extracted {len(result['rfqs'])} RFQs with confidence {result['confidence']}%")
                    logger.info(f"[EXCEL-OPENAI] Processing summary: {json.dumps(result['processing_summary'], indent=2)}")
                    
                    # Log successful Excel processing
                    self.interaction_logger.log_entity_extraction(
                        user_input=f"Excel file: {filename}",
                        entities={"rfqs": result["rfqs"]},
                        completeness=result["confidence"],
                        workflow_type="excel_rfq_processing",
                        model_used=self.default_model,
                        processing_time=processing_time,
                        missing_fields=[]
                    )
                    
                    return result
            
            # Log failed processing
            self.interaction_logger.log_error(
                interaction_type="excel_rfq_processing",
                user_input=f"Excel file: {filename}",
                error_message="No function call in response",
                model_used=self.default_model
            )
            
            return {
                "success": False,
                "error": "No function call in response",
                "rfqs": [],
                "processing_summary": {},
                "confidence": 0
            }
            
        except Exception as e:
            error_msg = str(e)
            processing_time = time.time() - start_time
            
            # Log error
            self.interaction_logger.log_error(
                interaction_type="excel_rfq_processing",
                user_input=f"Excel file: {filename}",
                error_message=error_msg,
                model_used=self.default_model
            )
            
            logger.error(f"Excel RFQ processing failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "rfqs": [],
                "processing_summary": {},
                "confidence": 0
            }