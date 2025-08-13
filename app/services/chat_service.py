"""
Main chat orchestration service for processing user messages.

This service acts as the central orchestrator for all user interactions,
coordinating between authentication, intent classification, and workflow routing.
It manages conversation state, session handling, and ensures proper message flow
through the entire AI procurement agent system.

Key responsibilities:
- Orchestrate the complete message processing pipeline
- Manage user authentication and registration status
- Route messages to appropriate workflow handlers based on intent
- Maintain conversation context and session state
- Handle error scenarios and fallback mechanisms
- Coordinate responses back to users through WhatsApp
"""

import logging
from typing import Dict, Any, List
import json
import asyncio
from datetime import datetime, date, timedelta
from app.utils.datetime_utils import utc_now
from app.utils.logging_utils import log_service_method
from app.services.intent_service import IntentService
from app.services.entity_service import EntityService
from app.services.vendor_service import VendorService
from app.services.rfq_service import RFQService
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.excel_validation_service import ExcelValidationService
from app.services.excel_processing_service import ExcelProcessingService
from app.services.gmt_api_service import GMTAPIService
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.auto_categorization_service import AutoCategorizationService
from app.database import SessionLocal, DatabaseManager
from app.models import User, ConversationSession
from app.schemas.rfq import RFQValidationSchema
logger = logging.getLogger(__name__)

class ChatService:
    """
    Central orchestrator for all user message processing.
    
    Coordinates authentication, intent classification, workflow routing,
    and response generation for the complete chat experience.
    """
    
    def __init__(self):
        self.intent_service = IntentService()
        self.entity_service = EntityService()
        self.vendor_service = VendorService()
        self.rfq_service = RFQService()
        self.whatsapp_service = WhatsAppService()
        self.openai_service = OpenAIService()
        self.db_manager = DatabaseManager()
        self.response_helpers = ResponseHelpers(self.openai_service)
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        self.auto_categorization_service = AutoCategorizationService()
        
    @log_service_method("chat_service")
    async def process_message(self, user_phone: str, message_content: str, message_type: str = "text") -> Dict[str, Any]:
        """
        Process incoming user message through complete pipeline.
        
        Orchestrates authentication check, intent classification,
        workflow routing, and response generation.
        """
        try:
            # Get or create user session
            user = await self._get_or_create_user(user_phone)
            session = await self._get_conversation_context(user_phone)
            
            # Check if session has expired
            if await SessionHelpers.is_session_expired(session):
                # Only send expiration message if appropriate
                if await SessionHelpers.should_send_expiration_message(session):
                    await self.whatsapp_service.send_message(
                        user_phone, 
                        "Your session has expired. Let's start fresh! What can I help you with?"
                    )
                    
                    # Generate enhanced session summary for timeout (non-blocking)
                    await self._handle_session_completion_enhanced(session)
                
                # Handle session expiry properly
                session = await SessionHelpers.handle_session_expiry(session, self.db_manager)
            else:
                # Session is active, renew its activity timestamp
                session = await SessionHelpers.renew_session_activity(session)
                await self._save_session(session, session.workflow_type or 'general_inquiry')
            
            # Track user message in conversation history
            SummarizationHelpers.add_to_conversation_history(session, "user", message_content, message_type)
            
            # Handle different message types
            if message_type == "text":
                result = await self._process_text_message(user, session, message_content)
            elif message_type == "interactive":
                result = await self._process_interactive_message(user, session, message_content)
            elif message_type == "excel_upload":
                result = await self._process_excel_upload(user, session, message_content)
            else:
                result = {"status": "error", "error": f"Unknown message type: {message_type}"}
            
            return result
                                
        except Exception as e:
            return await self._handle_error_response(e, user_phone, "processing_message", "Please try again")
    
    async def _process_text_message(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Process text message through intent classification and routing."""
        try:
            # Check if user needs registration
            if not user.is_registered:
                return await self._handle_registration_workflow(user, message)
            
            # Check if we're already in an RFQ workflow
            existing_entities = session.workflow_state.get("extracted_entities", [])
            has_existing_data = len(existing_entities) > 0 and any(
                any(v for v in product.values() if v is not None) 
                for product in existing_entities
            )
            
            # Also check if we have incomplete products or pending confirmations
            has_incomplete_products = bool(session.workflow_state.get("incomplete_products"))
            has_pending_confirmations = bool(session.workflow_state.get("pending_multiple_rfqs") or session.workflow_state.get("pending_rfq"))
            has_pending_optional = bool(session.workflow_state.get("pending_optional_rfq") or session.workflow_state.get("pending_optional_multiple_rfqs"))
            print(f"ChatService: has_existing_data={has_existing_data}, has_incomplete_products={has_incomplete_products}, has_pending_confirmations={has_pending_confirmations}, has_pending_optional={has_pending_optional}")
            
            # Handle pending optional field responses
            if has_pending_optional:
                # Check if user wants to skip optional fields
                if any(keyword in message.lower() for keyword in ["no", "skip", "proceed", "continue", "next"]):
                    # User wants to skip optional fields, proceed to confirmation
                    if session.workflow_state.get("pending_optional_rfq"):
                        # Single product
                        product_info = session.workflow_state["pending_optional_rfq"]
                        rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"], self.openai_service)
                        
                        # Load summaries for enhanced response generation
                        chat_summaries = await self.chat_summary_service.load_user_context(user.phone_number)
                        
                        summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
                            "user_message": message,
                            "extracted_entities": product_info["entities"]
                        }, chat_summaries)
                        await self.whatsapp_service.send_message(user.phone_number, summary_response)
                        
                        # Move to confirmation state
                        session.workflow_state["pending_rfq"] = product_info
                        del session.workflow_state["pending_optional_rfq"]
                        
                    elif session.workflow_state.get("pending_optional_multiple_rfqs"):
                        # Multiple products
                        complete_products = session.workflow_state["pending_optional_multiple_rfqs"]
                        all_schemas = []
                        all_entities = [prod["entities"] for prod in complete_products]
                        
                        for prod in complete_products:
                            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(prod["entities"], self.openai_service)
                            all_schemas.append(rfq_schema)
                        
                        # Load summaries for enhanced response generation
                        chat_summaries = await self.chat_summary_service.load_user_context(user.phone_number)
                        
                        summary_response = await self.response_helpers.generate_multiple_rfq_summary_and_confirmation(
                            all_schemas, 
                            {
                                "user_message": message,
                                "extracted_entities": all_entities,
                                "total_products": len(complete_products)
                            },
                            chat_summaries
                        )
                        await self.whatsapp_service.send_message(user.phone_number, summary_response)
                        
                        # Move to confirmation state
                        session.workflow_state["pending_multiple_rfqs"] = complete_products
                        del session.workflow_state["pending_optional_multiple_rfqs"]
                        
                    await self._save_session(session, 'rfq_creation')
                    return {"status": "optional_fields_skipped"}
                
                else:
                    # User provided optional information, process it and then proceed to confirmation
                    return await self._handle_purchase_intent(user, session, message)
            
            # Handle pending confirmations (user responding to "Would you like to proceed?")
            if has_pending_confirmations:
                # Use context-aware intent classification to determine user's response
                conversation_context = ChatServiceHelpers.build_conversation_context(session, message)
                confirmation_intent = self.intent_service.classify_intent(message, conversation_context)
                
                intent = confirmation_intent.get('intent')
                confidence = confirmation_intent.get('confidence', 0)
                context_analysis = confirmation_intent.get('context_analysis', {})
                
                print(f"Confirmation stage - Intent: {intent}, Confidence: {confidence}%, Context: {context_analysis}")
                
                if intent == "confirmation_response" and confidence > 0.7:
                    response_type = context_analysis.get('confirmation_details', {}).get('response_type')
                    has_conditions = context_analysis.get('confirmation_details', {}).get('has_conditions', False)
                    
                    if response_type == "accept" and not has_conditions:
                        # User confirmed - create RFQs
                        # Handle both single and multiple product confirmations
                        if session.workflow_state.get("pending_multiple_rfqs"):
                            complete_products = session.workflow_state["pending_multiple_rfqs"]
                        elif session.workflow_state.get("pending_rfq"):
                            complete_products = [session.workflow_state["pending_rfq"]]
                        else:
                            complete_products = []
                        
                        successful_count = 0
                        rfq_results = []
                        
                        for product_info in complete_products:
                            schema_data = product_info.get("schema_data", {})
                            if schema_data:
                                # Use existing schema data
                                rfq_schema = RFQValidationSchema(**schema_data)
                            else:
                                # Create schema from entities
                                rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"], self.openai_service)
                            gmt_result = await self._submit_rfq_to_backend(rfq_schema, user)
                            rfq_results.append(gmt_result)
                            if gmt_result.get("success"):
                                successful_count += 1
                        
                        # Generate completion response with RFQ IDs
                        rfq_ids = []
                        for result in rfq_results:
                            if result.get("success") and result.get("rfq_id"):
                                rfq_ids.append(result["rfq_id"])
                        
                        if rfq_ids:
                            rfq_ids_text = "\n".join([f"• {rfq_id}" for rfq_id in rfq_ids])
                            response = f"Thank you! All {successful_count} RFQs have been created successfully.\n\nYour RFQ IDs are:\n{rfq_ids_text}\n\nYou can use these reference numbers to track your requests."
                        else:
                            response = f"Thank you! All {successful_count} RFQs have been created successfully."
                        
                        await self.whatsapp_service.send_message(user.phone_number, response)
                        
                        # Run auto-categorization for each successful RFQ (offline process)
                        auto_cat = await self._run_auto_categorization_for_rfqs(rfq_results)
                        await self.whatsapp_service.send_message(user.phone_number, auto_cat)

                        # Check BFS availability after successful RFQ creation
                        await self._check_bfs_availability(user.phone_number)
                        
                        # Mark session as completed
                        session.outcome = 'completed'
                        session.completed_at = utc_now().replace(tzinfo=None)
                        
                        # Update core tracking fields (user type, categories, RFQ IDs)
                        if rfq_ids:
                            session.rfq_ids = rfq_ids
                            # Set user type as buyer (since they're creating RFQs)
                            session.user_type = 'buyer'
                            # Calculate averages based on session data
                            session = SessionHelpers.calculate_session_averages(session)
                        
                        # Generate enhanced session summary BEFORE clearing (non-blocking)
                        await self._handle_session_completion_enhanced(session)
                        
                        # Clear session AFTER summarization data is captured
                        session.workflow_state = {"extracted_entities": []}
                        await self._save_session(session, 'rfq_submitted')
                        
                        return {"status": "multiple_rfqs_created", "successful_count": successful_count}
                    else:
                        # User declined or has conditions - treat as modification request
                        # Keep pending confirmations and process as modification
                        logger.info("User declined or has conditions - treating as modification request")
                        # Don't delete pending_multiple_rfqs - let the modification flow handle it
                        return await self._handle_purchase_intent(user, session, message)
                        
                elif intent == "modification_request" and confidence > 0.7:
                    # User wants to modify - process the modification
                    logger.info("User requesting modification during confirmation stage")
                    # Keep pending confirmations and process the modification
                    return await self._handle_purchase_intent(user, session, message)
                    
                else:
                    # Unclear response - ask for clarification while keeping context
                    decline_context = {
                        "conversation_stage": "confirmation_clarification",
                        "user_message": message
                    }
                    
                    response = await self.response_helpers.generate_contextual_response(
                        decline_context,
                        ["I didn't quite understand. Please say 'yes' to confirm the RFQs or tell me what you'd like to change."],
                        "clarification_request"
                    )
                    
                    await self.whatsapp_service.send_message(user.phone_number, response)
                    # Keep pending confirmations for next attempt
                    await self._save_session(session, 'rfq_creation')
                    return {"status": "confirmation_clarification_requested"}
            
            
            if has_existing_data or has_incomplete_products:
                # Already in RFQ workflow, continue collecting
                logger.info("Continuing existing RFQ workflow")
                return await self._handle_purchase_intent(user, session, message)
            
            # Classify intent for new conversations with full context
            conversation_context = ChatServiceHelpers.build_conversation_context(session, message)
            intent_result = self.intent_service.classify_intent(message, conversation_context)
            logger.info(f"Intent classification result: {intent_result}")
            
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)
            
            # Route based on context-aware intent classification
            if intent == "buy_something" and confidence > 0.7:
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "modification_request" and confidence > 0.7:
                # Handle modification requests using existing purchase intent flow with modification context
                logger.info(f"Handling modification request with context: {intent_result.get('context_analysis', {})}")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "confirmation_response" and confidence > 0.7:
                # Handle confirmation responses - these should already be handled by pending confirmations check above
                # But if we reach here, treat as continuation of existing workflow
                logger.info(f"Handling confirmation response with context: {intent_result.get('context_analysis', {})}")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "reference_request" and confidence > 0.7:
                # Handle reference requests by routing to purchase intent flow
                # The EntityService will detect and handle the reference extraction
                logger.info(f"Handling reference request with context: {intent_result.get('context_analysis', {})}")
                return await self._handle_purchase_intent(user, session, message, intent_result)
            elif intent == "rfq_status_check" and confidence > 0.7:
                return await self._handle_rfq_status_inquiry(user, message)
            elif intent == "general_inquiry":
                return await self._handle_general_inquiry(user, message)
            elif confidence < 0.5:
                return await self._handle_clarification_request(user, message)
            else:
                return await self._handle_fallback(user, message)
                
        except Exception as e:
            logger.error(f"Error processing text message: {e}")
            raise
    
    async def _process_interactive_message(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
        """Process interactive message responses (buttons, lists)."""
        try:
            # Parse interactive content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {"type": "unknown", "content": content}
            
            message_type = content.get("type")
            
            if message_type == "button_reply":
                button_id = content.get("button_reply", {}).get("id")
                return await self._handle_button_response(user, session, button_id)
            elif message_type == "list_reply":
                list_id = content.get("list_reply", {}).get("id")
                return await self._handle_list_response(user, session, list_id)
            else:
                # Treat as regular text message
                text_content = str(content)
                return await self._process_text_message(user, session, text_content)
                
        except Exception as e:
            logger.error(f"Error processing interactive message: {e}")
            raise
    
    async def _process_excel_upload(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
        """Process Excel file upload for RFQ creation."""
        try:
            # Check if user needs registration
            if not user.is_registered:
                registration_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'registration_required'}
                registration_response = await self.response_helpers.generate_contextual_response(
                    registration_context,
                    ["Please complete your registration first before uploading files."],
                    "registration_required"
                )
                await self.whatsapp_service.send_message(user.phone_number, registration_response)
                return {"status": "handled", "response": "registration_required"}
            
            # Extract document information
            if not isinstance(content, dict):
                raise ValueError("Invalid Excel upload content format")
            
            document_info = content.get("document", {})
            file_url = document_info.get("link")
            filename = document_info.get("filename", "")
            
            if not file_url:
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'file_access_error'}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    ["I couldn't access your Excel file. Please try uploading again."],
                    "file_access_error"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "handled", "response": "file_access_error"}
            
            # Validate Excel file
            validation_service = ExcelValidationService()
            validation_result = await validation_service.validate_excel_file_from_url(file_url, filename)
            
            if not validation_result.get('valid'):
                validation_error = validation_result.get('error', 'Invalid Excel file')
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'validation_failed', 'error': validation_error}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    [validation_error],
                    "validation_failed"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "handled", "response": "validation_failed"}
            
            # Process Excel file
            processing_service = ExcelProcessingService(self.openai_service)
            processing_result = await processing_service.process_excel_file(
                content=validation_result['content'],
                filename=filename
            )
            
            if not processing_result.get('success'):
                processing_error = processing_result.get('error', 'Failed to process Excel file')
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'processing_failed', 'error': processing_error}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    [f"Error processing Excel: {processing_error}"],
                    "processing_failed"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "handled", "response": "processing_failed"}
            
            # Prepare context using helpers
            excel_context = ExcelHelpers.prepare_excel_context(processing_result, user.phone_number)
            
            # Update session  
            session.workflow_type = 'rfq_creation'
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update(excel_context)
            
            # Determine flow based on completeness
            completeness = excel_context['completeness']
            items = processing_result.get('items', [])
            
            if ExcelHelpers.should_complete_immediately(completeness, items):
                return await self._handle_complete_excel(user, session, processing_result)
            else:
                return await self._handle_incomplete_excel(user, session, excel_context)
            
        except Exception as e:
            logger.error(f"Error processing Excel upload: {e}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Sorry, I encountered an error processing your Excel file. Please try again."
            )
            return {"status": "error", "response": str(e)}
    
    async def _handle_complete_excel(self, user: User, session: ConversationSession, processing_result: Dict) -> Dict[str, Any]:
        """Handle complete Excel files that can create RFQ immediately."""
        try:
            # Generate processing response using OpenAI
            context = {
                'excel_data': processing_result,
                'workflow_type': 'excel_rfq_upload',
                'conversation_stage': 'excel_processing',
                'total_items': processing_result.get('total_items', 0),
                'filename': processing_result.get('filename', '')
            }
            
            processing_response = await self.response_helpers.generate_contextual_response(
                context,
                [f"Processing {processing_result.get('total_items', 0)} items from Excel file"],
                "excel_processing"
            )
            
            await self.whatsapp_service.send_message(user.phone_number, processing_response)
            
            # Validate items before creating template
            validation_result = processing_result.get('validation_result', {})
            if not validation_result.get('valid', False):
                # Items don't meet GMT API requirements
                error_msg = "Excel file doesn't meet GMT API requirements:\n"
                for error in validation_result.get('errors', []):
                    error_msg += f"• {error}\n"
                for warning in validation_result.get('warnings', []):
                    error_msg += f"• {warning}\n"
                
                error_response = await self.response_helpers.generate_contextual_response(
                    {**context, 'error': error_msg},
                    ["Please check your Excel file format and try again."],
                    "error"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "failed", "error": error_msg}
            
            # Create GMT template and submit
            processing_service = ExcelProcessingService(self.openai_service)
            template_bytes = processing_service.create_standard_template(processing_result['items'])
            api_data = processing_service.encode_for_api(template_bytes, processing_result['filename'])
            
            # Submit to GMT API
            gmt_service = GMTAPIService()
            gmt_result = await gmt_service.bulk_upload_rfq(api_data)
            
            if gmt_result.get('success'):
                # Generate completion response using OpenAI
                rfq_data = {
                    'items': processing_result['items'],
                    'filename': processing_result['filename'],
                    'total_items': processing_result['total_items']
                }
                
                completion_response = self.openai_service.generate_completion_response(rfq_data, context)
                await self.whatsapp_service.send_message(user.phone_number, completion_response)
                
                session.outcome = 'completed'
                session.completed_at = utc_now().replace(tzinfo=None)
                
                # Generate enhanced session summary (non-blocking)
                await self._handle_session_completion_enhanced(session)
                
                await self._save_session(session, 'rfq_submitted')
                
                return {"status": "completed", "response": "rfq_created"}
            else:
                # GMT API failed, fall back to conversation completion
                error_context = {**context, 'error': gmt_result.get('error', 'Unknown error')}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    ["There was an issue creating the RFQ. Let me help you complete it through conversation."],
                    "error_recovery"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return await self._handle_incomplete_excel(user, session, {"excel_data": processing_result})
            
        except Exception as e:
            logger.error(f"Error handling complete Excel: {e}")
            error_context = {'error': str(e), 'workflow_type': 'excel_rfq_upload'}
            error_response = await self.response_helpers.generate_contextual_response(
                error_context,
                ["There was an issue processing your Excel file. Let me help you through conversation."],
                "error_recovery"
            )
            await self.whatsapp_service.send_message(user.phone_number, error_response)
            return await self._handle_incomplete_excel(user, session, {"excel_data": processing_result})
    
    async def _handle_incomplete_excel(self, user: User, session: ConversationSession, excel_context: Dict) -> Dict[str, Any]:
        """Handle incomplete Excel files that need conversation completion."""
        try:
            processing_result = excel_context['excel_data']
            missing_fields = excel_context['missing_fields']
            
            # Generate reupload instructions using helper
            instructions = ExcelHelpers.generate_reupload_instructions(missing_fields, excel_context)
            
            # Prepare context for OpenAI response generation
            context = {
                'excel_data': processing_result,
                'workflow_type': 'excel_rfq_upload',
                'conversation_stage': 'excel_completion',
                'missing_fields': missing_fields,
                'total_items': processing_result.get('total_items', 0),
                'filename': processing_result.get('filename', ''),
                'completeness': excel_context.get('completeness', 0)
            }
            
            # Generate clarification response using OpenAI with reupload instructions
            clarification_response = self.openai_service.generate_clarification_response(
                instructions, 
                excel_context.get('completeness', 0), 
                context
            )
            
            await self.whatsapp_service.send_message(user.phone_number, clarification_response)
            
            # Update session state - waiting for excel reupload
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['stage'] = 'excel_reupload_required'
            session.workflow_state['pending_excel_reupload'] = True
            session.workflow_state['last_excel_issues'] = missing_fields
            await self._save_session(session, 'rfq_creation')
            
            return {"status": "excel_reupload_required", "response": "excel_reupload_instructions_sent"}
            
        except Exception as e:
            logger.error(f"Error handling incomplete Excel: {e}")
            raise
    
    async def _get_or_create_user(self, phone_number: str) -> User:
        """Get existing user or create new one."""
        # Mock user for testing without database
        class MockUser:
            def __init__(self, phone_number):
                self.id = 1
                self.phone_number = phone_number
                self.name = "Test User"
                self.is_registered = True
                self.role = "buyer"
        
        return MockUser(phone_number)
    
    async def _get_conversation_context(self, phone_number: str) -> ConversationSession:
        """Retrieve or create conversation context for user session."""
        # Generate session ID using helper method (configurable strategy)
        session_id = SessionHelpers.generate_session_id(phone_number, "daily")
    
        # Try to get existing session
        session = self.db_manager.get_conversation_session(session_id)
        
        if not session:
            # Create new session
            session_data = {
                'session_id': session_id,
                'external_user_id': phone_number,
                'workflow_type': None,
                'outcome': None,
                'workflow_state': {"extracted_entities": [], "last_activity_at": utc_now().isoformat()},
                'conversation_history': {"messages": []},
                'extracted_entities': {},
                'retention_date': date.today() + timedelta(days=30)
            }
            session = self.db_manager.save_conversation_session(session_data)
            
            logger.info(f"Created new session: {session_id}")
            # Show authentication placeholder for new session
            await self._show_auth_placeholder(phone_number)
        else:
            logger.info(f"Found existing session: {session_id}")
        
        return session
    
    async def _handle_registration_workflow(self, user: User, message: str) -> Dict[str, Any]:
        """Handle user registration process."""
        try:
            # Simple registration - just collect name
            if not user.name:
                # Extract name from message
                name = message.strip().title()
                
                with SessionLocal() as db:
                    db_user = db.query(User).filter(User.id == user.id).first()
                    db_user.name = name
                    db_user.is_registered = True
                    db.commit()
                
                welcome_message = f"Welcome {name}!\n\nI'm your AI Procurement Assistant. I can help you:\n\n-- Create RFQs (Request for Quotations)\n- Check product availability\n\nWhat would you like to procure today?"
                
                await self.whatsapp_service.send_message(user.phone_number, welcome_message)
                return {"status": "registered", "user_name": name}
            else:
                # User already has name, mark as registered
                with SessionLocal() as db:
                    db_user = db.query(User).filter(User.id == user.id).first()
                    db_user.is_registered = True
                    db.commit()
                
                return await self._process_text_message(user, await self._get_conversation_context(user.phone_number), message)
                
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "registration_workflow", "Please tell me your name to get started")
    
    async def _handle_purchase_intent(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Handle purchase intent with data model driven orchestration."""
        try:
            # 1. Extract entities using EntityService (focused service)
            # Include both existing entities and incomplete products in context
            existing_entities = session.workflow_state.get("extracted_entities", [])
            incomplete_products = session.workflow_state.get("incomplete_products", [])
            
            # Also check for pending confirmations that might need modification
            pending_multiple = session.workflow_state.get("pending_multiple_rfqs")
            pending_single = session.workflow_state.get("pending_rfq")
            
            if pending_multiple:
                pending_confirmations = pending_multiple
            elif pending_single:
                pending_confirmations = [pending_single]
            else:
                pending_confirmations = []
            
            print(f"ChatService: _handle_purchase_intent - pending_confirmations: {len(pending_confirmations)} items")
            if pending_confirmations:
                print(f"ChatService: Found pending confirmations, this might be a modification request")
            
            # If we have incomplete products, use them as the base entities
            if incomplete_products:
                context_entities = [prod["entities"] for prod in incomplete_products]
                print(f"ChatService: Using incomplete products as context: {len(context_entities)} products")
            else:
                context_entities = existing_entities
                
            # Load user's recent summaries for enhanced context
            chat_summaries = await self.chat_summary_service.load_user_context(user.phone_number)
            print(f"ChatService: Loaded {len(chat_summaries)} chat summaries for context")
            
            # Build comprehensive context for EntityService including pending confirmations, intent result, and summaries
            entity_context = {
                "extracted_entities": context_entities,
                "workflow_state": session.workflow_state,
                "session_metadata": {
                    "session_id": session.session_id,
                    "workflow_type": session.workflow_type
                },
                "intent_result": intent_result,  # Pass intent result to avoid duplicate OpenAI calls
                "chat_summaries": chat_summaries,  # Add summaries for smart entity extraction
            }
            print(f"ChatService: Passing context to EntityService - has pending confirmations: {bool(session.workflow_state.get('pending_multiple_rfqs'))}")
            print(f"ChatService: Debug session.workflow_state keys: {list(session.workflow_state.keys()) if session.workflow_state else 'None'}")
            print(f"ChatService: Debug pending_multiple_rfqs: {session.workflow_state.get('pending_multiple_rfqs') if session.workflow_state else 'No workflow_state'}")
            
            # Use summary-aware entity extraction if we have summaries, otherwise use standard extraction
            if chat_summaries and self._should_use_summary_aware_extraction(message):
                print(f"ChatService: Using summary-aware entity extraction")
                entity_result = self.entity_service.extract_entities_with_summary_context(message, context=entity_context)
            else:
                print(f"ChatService: Using standard entity extraction")
                entity_result = self.entity_service.extract_entities(message, context=entity_context, workflow_type="buy_something")
            logger.info(f"EntityService result: {entity_result}")
            
            # Debug: Check which path we're taking
            print(f"ChatService debug: entity_result keys = {entity_result.keys()}")
            print(f"ChatService debug: entity_result = {entity_result}")
            
            if "products" in entity_result and entity_result["products"]:
                print(f"ChatService: Taking PRODUCTS ARRAY path with {len(entity_result['products'])} products")
                logger.info(f"Taking PRODUCTS ARRAY path with {len(entity_result['products'])} products")
                # Products array detected - process all products and create RFQs
                products = entity_result["products"]
                return await self._handle_products_array(user, session, message, products, chat_summaries)
            elif "entities" in entity_result:
                print(f"ChatService: Taking BACKWARD COMPATIBILITY path with entities: {entity_result['entities']}")
                logger.info(f"Taking BACKWARD COMPATIBILITY path with entities: {entity_result['entities']}")
            else:
                print(f"ChatService: Taking NO ENTITIES path")
                logger.info(f"Taking NO ENTITIES path")
            
            # 3. Handle single product (backward compatibility)
            current_entities = session.workflow_state.get("extracted_entities", [])
            new_entities = entity_result.get("entities", {})
            
            # Convert single entity to array format
            if new_entities and not isinstance(current_entities, list):
                current_entities = [current_entities] if current_entities else []
            
            # Add new entities as a product
            if new_entities:
                current_entities.append(new_entities)
                session.workflow_state["extracted_entities"] = current_entities
                
                # Track categories from extracted entities in product_items
                category = new_entities.get('category') or new_entities.get('description')
                if category:
                    if not session.product_items:
                        session.product_items = []
                    # Update or add category info to product_items
                    product_info = {
                        'category': category,
                        'description': new_entities.get('description'),
                        'added_at': utc_now().isoformat()
                    }
                    session.product_items.append(product_info)
                
                # Process this as a single product array
                return await self._handle_products_array(user, session, message, current_entities, chat_summaries)
            
            # If no new entities, just continue with existing flow
            return {
                "status": "no_new_entities",
                "message": "Could you provide more details about what you need?"
            }
                
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "purchase_intent", "Could you tell me more about what you need?")
    
    async def _handle_products_array(self, user: User, session: ConversationSession, message: str, products: list, chat_summaries: list = None) -> Dict[str, Any]:
        """Handle products array (single or multiple products)."""
        try:
            print(f"_handle_products_array: Processing {len(products)} products")
            logger.info(f"Handling {len(products)} products from message")
            
            # Track categories from all products in product_items
            for product_entities in products:
                if isinstance(product_entities, dict):
                    category = product_entities.get('category') or product_entities.get('description')
                    if category:
                        if not session.product_items:
                            session.product_items = []
                        # Add product info with category to product_items
                        product_info = {
                            'category': category,
                            'description': product_entities.get('description'),
                            'added_at': utc_now().isoformat()
                        }
                        session.product_items.append(product_info)
            
            # Check completeness for each product and identify which ones need more info
            
            incomplete_products = []
            complete_products = []
            
            for i, product_entities in enumerate(products):
                try:
                    # Transform entities to schema format
                    rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_entities, self.openai_service)
                    
                    # Check if this product has all mandatory fields
                    missing_mandatory = rfq_schema.get_missing_mandatory_fields()
                    
                    # Debug logging
                    logger.info(f"Product {i+1} entities: {product_entities}")
                    logger.info(f"Product {i+1} missing mandatory: {missing_mandatory}")
                    
                    if len(missing_mandatory) == 0:
                        complete_products.append({
                            "index": i + 1,
                            "entities": product_entities
                        })
                    else:
                        incomplete_products.append({
                            "index": i + 1,
                            "entities": product_entities,
                            "missing_fields": missing_mandatory
                        })
                        
                except Exception as e:
                    logger.warning(f"Error validating product {i+1}: {e}")
                    incomplete_products.append({
                        "index": i + 1,
                        "entities": product_entities,
                        "missing_fields": ["project_desc", "delivery_date", "division"]
                    })
            
            # If any product is incomplete, collect all questions from data model
            if incomplete_products:
                print(f"_handle_products_array: Found {len(incomplete_products)} incomplete products")
                all_questions = []
                all_missing_fields = []
                
                # Group missing fields across all products to avoid repetition
                common_missing_fields = set()
                for prod in incomplete_products:
                    common_missing_fields.update(prod["missing_fields"])
                
                # Check if all products have the same missing fields
                all_same_missing = True
                first_missing = set(incomplete_products[0]["missing_fields"])
                print(f"  First product missing fields: {first_missing}")
                
                for prod in incomplete_products[1:]:
                    prod_missing = set(prod["missing_fields"])
                    print(f"  Product {prod['index']} missing fields: {prod_missing}")
                    if prod_missing != first_missing:
                        all_same_missing = False
                        break
                
                print(f"  All products have same missing fields: {all_same_missing}")
                
                if all_same_missing and len(incomplete_products) > 1:
                    # All products missing the same fields - ask once for all
                    rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(incomplete_products[0]["entities"], self.openai_service)
                    product_questions = rfq_schema.get_next_questions()
                    
                    if product_questions:
                        product_names = [prod["entities"].get("description", f"Product {prod['index']}") for prod in incomplete_products]
                        all_questions.append(f"For all products ({', '.join(product_names)}):")
                        all_questions.extend(product_questions)
                        all_missing_fields.extend(incomplete_products[0]["missing_fields"])
                else:
                    # Products have different missing fields - ask individually
                    for prod in incomplete_products:
                        print(f"  Processing incomplete product {prod['index']}: {prod['entities'].get('description', 'Unknown')}")
                        print(f"    Missing fields: {prod['missing_fields']}")
                        
                        rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(prod["entities"], self.openai_service)
                        
                        # Get questions from data model
                        product_questions = rfq_schema.get_next_questions()
                        product_desc = prod["entities"].get("description", f"Product {prod['index']}")
                        
                        print(f"    Data model questions: {product_questions}")
                        
                        # Format questions for this product
                        if product_questions:
                            if len(incomplete_products) > 1:
                                all_questions.append(f"For {product_desc}:")
                            all_questions.extend(product_questions)
                            all_missing_fields.extend(prod["missing_fields"])
                
                print(f"  All questions to ask: {all_questions}")
                
                # Calculate overall completeness
                total_mandatory_fields = sum(len(prod["missing_fields"]) for prod in incomplete_products)
                filled_fields = len(products) * 5 - total_mandatory_fields  # Rough estimate
                completeness = max(10, (filled_fields / (len(products) * 5)) * 100)
                
                # Store incomplete products for follow-up (serialize datetime objects)
                session.workflow_state["incomplete_products"] = ChatServiceHelpers.serialize_products_for_session(incomplete_products)
                session.workflow_state["complete_products"] = ChatServiceHelpers.serialize_products_for_session(complete_products)
                await self._save_session(session, 'rfq_creation')
                
                # Generate and send clarification response directly with our specific questions
                clarification_message = "\n".join(all_questions)
                print(f"  Final clarification message: {clarification_message}")
                
                # Build context and send response directly
                context = ChatServiceHelpers.build_context("clarification", message, {}, completeness,
                    missing_fields=all_missing_fields,
                    total_products=len(products),
                    incomplete_products=len(incomplete_products)
                )
                
                response = await self.response_helpers.generate_clarification_response([clarification_message], completeness, context, chat_summaries)
                await self.whatsapp_service.send_message(user.phone_number, response)
                
                return {
                    "status": "products_incomplete",
                    "total_products": len(products),
                    "incomplete_products": len(incomplete_products)
                }
            else:
                print(f"_handle_products_array: All {len(complete_products)} products are complete!")
            
            # All products are complete - check for optional fields or send confirmation
            if len(complete_products) == 1:
                # Single product - check for optional fields first
                product_info = complete_products[0]
                
                # Recreate schema from entities
                rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"])
                
                # Check if user wants to provide optional information
                optional_questions = rfq_schema.get_optional_questions()
                
                # Check if this is a response to optional questions (look for specific workflow state)
                if optional_questions and not session.workflow_state.get("optional_fields_asked"):
                    # Ask about optional fields first
                    optional_intro = "Your mandatory information is complete! Would you like to provide any additional details?\n\n"
                    optional_text = "\n".join(f"• {q}" for q in optional_questions)
                    optional_message = f"{optional_intro}{optional_text}\n\nYou can skip this by saying 'no' or 'proceed'."
                    
                    await self.whatsapp_service.send_message(user.phone_number, optional_message)
                    
                    # Mark that we've asked about optional fields
                    session.workflow_state["optional_fields_asked"] = True
                    session.workflow_state["pending_optional_rfq"] = ChatServiceHelpers.serialize_products_for_session({
                        "index": product_info["index"],
                        "entities": product_info["entities"],
                        "schema_data": rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else {}
                    })
                    
                    await self._save_session(session, 'rfq_creation')
                    
                    return {
                        "status": "optional_fields_inquiry",
                        "total_products": 1
                    }
                
                # Generate confirmation (either optional fields were completed or user declined)
                summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
                    "user_message": message,
                    "extracted_entities": product_info["entities"]
                }, chat_summaries)
                await self.whatsapp_service.send_message(user.phone_number, summary_response)
                
                # Store for confirmation (serialize schema to dict)
                product_info_serializable = {
                    "index": product_info["index"],
                    "entities": product_info["entities"],
                    "schema_data": rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else {}
                }
                session.workflow_state["pending_rfq"] = ChatServiceHelpers.serialize_products_for_session(product_info_serializable)
                
                # Clear incomplete products since we're now in confirmation phase
                if "incomplete_products" in session.workflow_state:
                    del session.workflow_state["incomplete_products"]
                if "complete_products" in session.workflow_state:
                    del session.workflow_state["complete_products"]
                if "optional_fields_asked" in session.workflow_state:
                    del session.workflow_state["optional_fields_asked"]
                    
                await self._save_session(session, 'rfq_creation')
                
                return {
                    "status": "single_product_confirmation",
                    "total_products": 1
                }
            else:
                # Multiple products - check for optional fields first
                all_schemas = []
                all_entities = [prod["entities"] for prod in complete_products]
                
                for prod in complete_products:
                    rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(prod["entities"])
                    all_schemas.append(rfq_schema)
                
                # Check if we should ask about optional fields
                all_optional_questions = []
                for i, schema in enumerate(all_schemas):
                    optional_questions = schema.get_optional_questions()
                    if optional_questions:
                        product_name = f"Product {i+1}"
                        all_optional_questions.extend([f"{product_name}: {q}" for q in optional_questions])
                
                # Check if this is a response to optional questions
                if all_optional_questions and not session.workflow_state.get("optional_fields_asked"):
                    # Ask about optional fields for all products
                    optional_intro = "Your mandatory information is complete for all products! Would you like to provide any additional details?\n\n"
                    optional_text = "\n".join(f"• {q}" for q in all_optional_questions)
                    optional_message = f"{optional_intro}{optional_text}\n\nYou can skip this by saying 'no' or 'proceed'."
                    
                    await self.whatsapp_service.send_message(user.phone_number, optional_message)
                    
                    # Mark that we've asked about optional fields
                    session.workflow_state["optional_fields_asked"] = True
                    session.workflow_state["pending_optional_multiple_rfqs"] = ChatServiceHelpers.serialize_products_for_session([
                        {
                            "index": prod["index"],
                            "entities": prod["entities"],
                            "schema_data": all_schemas[i].model_dump() if hasattr(all_schemas[i], 'model_dump') else {}
                        }
                        for i, prod in enumerate(complete_products)
                    ])
                    
                    await self._save_session(session, 'rfq_creation')
                    
                    return {
                        "status": "optional_fields_inquiry",
                        "total_products": len(complete_products)
                    }
                
                # Generate confirmation for all products
                summary_response = await self.response_helpers.generate_multiple_rfq_summary_and_confirmation(
                    all_schemas, 
                    {
                        "user_message": message,
                        "extracted_entities": all_entities,
                        "total_products": len(complete_products)
                    },
                    chat_summaries
                )
                await self.whatsapp_service.send_message(user.phone_number, summary_response)
                
                # Store for confirmation (serialize schemas to dicts)
                complete_products_serializable = []
                for i, product_info in enumerate(complete_products):
                    # Get the corresponding schema we created earlier
                    corresponding_schema = all_schemas[i]
                    serializable_product = {
                        "index": product_info["index"],
                        "entities": product_info["entities"],
                        "schema_data": corresponding_schema.dict() if hasattr(corresponding_schema, 'dict') else {}
                    }
                    complete_products_serializable.append(serializable_product)
                
                session.workflow_state["pending_multiple_rfqs"] = ChatServiceHelpers.serialize_products_for_session(complete_products_serializable)
                
                # Clear incomplete products since we're now in confirmation phase
                if "incomplete_products" in session.workflow_state:
                    del session.workflow_state["incomplete_products"]
                if "complete_products" in session.workflow_state:
                    del session.workflow_state["complete_products"]
                if "optional_fields_asked" in session.workflow_state:
                    del session.workflow_state["optional_fields_asked"]
                    
                await self._save_session(session, 'rfq_creation')
                
                return {
                    "status": "multiple_products_confirmation",
                    "total_products": len(products)
                }
                    
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "products_handling", "Could you tell me more about what you need?")
    
    
    async def _handle_general_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        """Handle general inquiries using OpenAI."""
        try:
            context = ChatServiceHelpers.build_context("general_inquiry", message)
            
            await self._send_contextual_response(user.phone_number, context, ["How can I help you with your procurement needs today?"], "general_inquiry")
            
            return {"status": "general_inquiry_handled"}
            
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "general_inquiry", "How can I assist you today?")
    
    async def _handle_clarification_request(self, user: User, message: str) -> Dict[str, Any]:
        """Handle ambiguous messages requiring clarification."""
        try:
            context = ChatServiceHelpers.build_context("clarification", message)
            
            clarification_questions = [
                "Could you be more specific about what you're looking for?",
                "Are you looking to create an RFQ or check product availability?"
            ]
            
            response = await self.response_helpers.generate_clarification_response(clarification_questions, 0, context)
            await self.whatsapp_service.send_message(user.phone_number, response)
            return {"status": "clarification_sent"}
            
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "clarification_request", "Could you be more specific about your procurement needs?")
    
    async def _handle_fallback(self, user: User, message: str) -> Dict[str, Any]:
        """Handle messages that don't fit other categories."""
        try:
            context = ChatServiceHelpers.build_context("fallback", message)
            
            fallback_questions = ["How can I help you with your procurement needs?"]
            
            await self._send_contextual_response(user.phone_number, context, fallback_questions, "fallback")
            return {"status": "fallback_handled"}
            
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "fallback_handler", "How can I assist you today?")
    
    async def _handle_button_response(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:  # noqa: ARG002
        """Handle button interaction responses."""
        # Implementation for button responses
        logger.info(f"Button response from {user.phone_number}: {button_id}")
        return {"status": "button_handled", "button_id": button_id}
    
    async def _handle_list_response(self, user: User, session: ConversationSession, list_id: str) -> Dict[str, Any]:  # noqa: ARG002
        """Handle list selection responses."""
        # Implementation for list responses
        logger.info(f"List response from {user.phone_number}: {list_id}")
        return {"status": "list_handled", "list_id": list_id}
    
    async def _generate_contextual_response(self, context: dict, base_questions: list = None, conversation_stage: str = "collecting") -> str:
        """Generate contextual response using OpenAI."""
        return await self.response_helpers.generate_contextual_response(context, base_questions, conversation_stage)
    
    async def _generate_completion_response(self, rfq_schema, context: dict) -> str:
        """Generate completion response using OpenAI."""
        return await self.response_helpers.generate_completion_response(rfq_schema, context)
    
    async def _generate_clarification_response(self, questions: list, completeness: float, context: dict) -> str:
        """Generate clarification response using OpenAI."""
        return await self.response_helpers.generate_clarification_response(questions, completeness, context)
    
    async def _send_contextual_response(self, user_phone: str, context: dict, questions: list, stage: str) -> None:
        """Generate and send contextual response."""
        response = await self.response_helpers.generate_contextual_response(context, questions, stage)
        await self.whatsapp_service.send_message(user_phone, response)

    async def _handle_error_response(self, error: Exception, user_phone: str, error_type: str, fallback_message: str) -> Dict[str, Any]:
        """Handle common error response pattern."""
        logger.error(f"Error in {error_type}: {error}")
        error_context = {"error_type": error_type, "conversation_stage": "error"}
        error_response = await self.response_helpers.generate_contextual_response(
            error_context, 
            [fallback_message], 
            "error"
        )
        await self.whatsapp_service.send_message(user_phone, error_response)
        return {"status": "error", "error": str(error)}
    
    async def _save_session(self, session: ConversationSession, workflow_type: str) -> ConversationSession:
        """Save updated session to database."""
        try:
            # Clean workflow_state to ensure JSON serialization
            clean_workflow_state = self._clean_for_json_serialization(session.workflow_state) if session.workflow_state else {}
            
            session_data = {
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'workflow_type': workflow_type,
                'outcome': session.outcome.value if session.outcome and hasattr(session.outcome, 'value') else session.outcome,
                'workflow_state': clean_workflow_state,
                'conversation_history': session.conversation_history,
                'extracted_entities': session.extracted_entities,
                'retention_date': session.retention_date,
                'last_activity_at': session.last_activity_at
            }
            return self.db_manager.save_conversation_session(session_data)
        except Exception as e:
            logger.error(f"Error saving session: {e}")
            return session

    def _clean_for_json_serialization(self, obj):
        """Recursively clean object for JSON serialization."""
        import json
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
        elif isinstance(obj, (str, int, float, bool)):
            return obj
        else:
            # Try to serialize to test, if it fails, convert to string
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)
    
    async def _handle_session_completion_enhanced(self, session: ConversationSession) -> None:
        """
        Handle session completion with enhanced summarization.
        
        Captures rich session data BEFORE clearing and runs summarization
        in background to avoid blocking user experience.
        """
        try:
            # Extract rich entities BEFORE session is cleared
            rich_entities = SummarizationHelpers.extract_rich_entities_for_summary(session)
            
            # Store rich entities in session.extracted_entities for persistence
            session.extracted_entities = rich_entities
            
            # Prepare enhanced summary data
            enhanced_summary_data = SummarizationHelpers.prepare_enhanced_summary_data(session, rich_entities)
            enhanced_summary_data.update({
                'session_id': session.session_id,
                'created_at': session.created_at,
                'completed_at': session.completed_at,
                'enhanced_entities': rich_entities
            })
            
            # Start background summarization (non-blocking)
            asyncio.create_task(
                SummarizationHelpers.handle_session_completion_async(
                    self.chat_summary_service,
                    self.daily_summary_service,
                    enhanced_summary_data
                )
            )
            
            logger.info(f"Started background summarization for session {session.session_id}")
            
        except Exception as e:
            logger.error(f"Error starting enhanced session completion for {session.session_id}: {e}") 
            # Fallback to original method
            await self._handle_session_completion_fallback(session)
    
    async def _handle_session_completion_fallback(self, session: ConversationSession) -> None:
        """Fallback session completion method (original logic)."""
        try:
            await self.chat_summary_service.generate_session_summary(session)
            await self.daily_summary_service.generate_daily_summary(session.external_user_id)
            logger.info(f"Generated summaries for completed session {session.session_id}")
        except Exception as e:
            logger.error(f"Error generating summaries for session {session.session_id}: {e}")
    
    async def _show_auth_placeholder(self, user_phone: str) -> None:
        """Show authentication placeholder message for new sessions."""
        try:
            message = "Authentication system is in progress, continuing with your request..."
            await self.whatsapp_service.send_message(user_phone, message)
            logger.info(f"Sent authentication placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending authentication placeholder: {e}")
    
    async def _check_bfs_availability(self, user_phone: str) -> None:
        """Check BFS availability after successful RFQ creation."""
        try:
            # Send initial checking message
            checking_message = "Checking our inventory for immediate availability..."
            await self.whatsapp_service.send_message(user_phone, checking_message)
            
            # Send placeholder message
            placeholder_message = "BFS inventory check feature is in progress."
            await self.whatsapp_service.send_message(user_phone, placeholder_message)
            
            logger.info(f"Sent BFS availability placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending BFS availability placeholder: {e}")


    async def _handle_rfq_status_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        # Help 1 : how to handle session here, like what data needs to be save in db and how to do it
        """Handle RFQ status inquiry requests."""
        try:
            result = await self.rfq_service.process_rfq_status_request(user=user, message=message)

            # Step: Send WhatsApp message
            await self.whatsapp_service.send_message(user.phone_number, result["response_message"])

            return {
                "status": result.get("status"),
                "rfq_ids": result.get("rfq_ids"),
                "rfq_statuses": result.get("rfq_statuses")
            }

        except Exception as e:
            logger.error(f"Error sending RFQ status placeholder: {e}")
            return {"status": "error", "error": str(e)}
    
    
    
    
    async def _submit_rfq_to_backend(self, rfq_schema, user) -> dict:  # noqa: ARG002
        """Submit RFQ directly to backend via GMT API service."""
        try:
            
            gmt_service = GMTAPIService()
            
            # Convert schema to dict using the newer method
            schema_dict = rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else rfq_schema.dict()
            
            # Transform to format expected by GMT API service
            # GMT service expects these fields based on its _transform_rfq_to_gmt_format method
            rfq_data = {
                "product_name": schema_dict.get("project_desc", "Unknown Product"),
                "quantity": schema_dict.get("items", [{}])[0].get("quantity", 1) if schema_dict.get("items") else 1,
                "unit_of_measure": schema_dict.get("items", [{}])[0].get("unit_of_measures", "pcs") if schema_dict.get("items") else "pcs",
                "division": schema_dict.get("division", "Admin & IT"),
                "specifications": schema_dict.get("items", [{}])[0].get("description", "") if schema_dict.get("items") else "",
                "preferred_brand": schema_dict.get("preferred_brand", ""),
                "delivery_state": schema_dict.get("delivery_locations", [{}])[0].get("state", "Karnataka") if schema_dict.get("delivery_locations") else "Karnataka",
                "delivery_city": schema_dict.get("delivery_locations", [{}])[0].get("city", "Bangalore") if schema_dict.get("delivery_locations") else "Bangalore",
                "delivery_pincode": schema_dict.get("delivery_locations", [{}])[0].get("pincode", "560001") if schema_dict.get("delivery_locations") else "560001",
                "deadline": schema_dict.get("delivery_date"),
                "remarks": schema_dict.get("remarks", "Created via AI Procurement WhatsApp Bot")
            }
            
            # Submit to backend via GMT API
            result = await gmt_service.create_rfq(rfq_data)
            
            # Log the GMT API response for debugging
            logger.info(f"GMT API Response: {result}")
            print(f"GMT API Response: {result}")
            
            # Include original RFQ data for auto-categorization
            if result.get("success"):
                result["rfq_data"] = {
                    "items": [
                        {
                            "description": rfq_data.get("specifications", ""),
                            "product_name": rfq_data.get("product_name", ""),
                            "quantity": rfq_data.get("quantity", 1),
                            "unit_of_measure": rfq_data.get("unit_of_measure", "pcs"),
                            "division": rfq_data.get("division", ""),
                            "preferred_brand": rfq_data.get("preferred_brand", "")
                        }
                    ]
                }
            
            return result
            
        except Exception as e:
            logger.error(f"Error submitting RFQ to backend: {e}")
            return {
                "success": False,
                "error": f"Failed to submit RFQ: {str(e)}"
            }
    
    def _should_use_summary_aware_extraction(self, message: str) -> bool:
        """
        Use AI to intelligently determine if we should use summary-aware entity extraction.
        
        This uses the OpenAI service to analyze the message and determine if the user is making
        references to previous conversations that would benefit from historical context.
        
        Args:
            message: User message to analyze
            
        Returns:
            True if summary-aware extraction should be used
        """
        try:
            # Use OpenAI service for intelligent reference detection
            reference_analysis = self.openai_service.analyze_reference_context(message)
            
            has_references = reference_analysis.get("has_references", False)
            confidence = reference_analysis.get("confidence", 0)
            reference_types = reference_analysis.get("reference_types", [])
            
            # Use summary-aware extraction if we have high confidence references
            should_use_summary = has_references and confidence >= 70
            
            print(f"ChatService: Reference analysis for '{message}':")
            print(f"  - Has references: {has_references}")
            print(f"  - Confidence: {confidence}%")
            print(f"  - Reference types: {reference_types}")
            print(f"  - Use summary-aware extraction: {should_use_summary}")
            
            return should_use_summary
            
        except Exception as e:
            print(f"ChatService: Error in reference analysis: {e}")
            # Fallback: if analysis fails, don't use summary-aware extraction
            return False

    async def _run_auto_categorization_for_rfqs(self, rfq_results: List[Dict[str, Any]]) -> str:
        """
        Run auto-categorization for successfully submitted RFQs.
        
        This is an offline process that runs after RFQ submission to categorize
        items using vector search and AI, without impacting user experience.
        
        Args:
            rfq_results: List of RFQ submission results with success status and RFQ IDs
            
        Returns:
            User-friendly message about categorization results
        """
        try:
            logger.info(f"Starting auto-categorization for {len(rfq_results)} RFQ results")
            
            categorization_summary = []
            total_items_processed = 0
            successful_categorizations = 0
            
            for rfq_result in rfq_results:
                if not rfq_result.get("success"):
                    continue
                    
                rfq_id = rfq_result.get("rfq_id")
                if not rfq_id:
                    logger.warning("RFQ result missing rfq_id, skipping auto-categorization")
                    continue
                
                # Get RFQ data to extract item descriptions
                rfq_data = rfq_result.get("rfq_data", {})
                items = rfq_data.get("items", [])
                
                if not items:
                    logger.warning(f"No items found in RFQ {rfq_id}, skipping auto-categorization")
                    continue
                
                # Process each item in the RFQ
                for item in items:
                    item_description = item.get("description", "")
                    if not item_description:
                        logger.warning(f"Empty item description in RFQ {rfq_id}, skipping")
                        continue
                    
                    total_items_processed += 1
                    logger.info(f"Auto-categorizing item: '{item_description}' for RFQ {rfq_id}")
                    
                    # Run categorization with new simplified interface
                    categorization_result = self.auto_categorization_service.categorize_item(
                        item_description=item_description,
                        user_id="system_auto_categorization",
                        session_id=f"rfq_{rfq_id}",
                        rfq_id=rfq_id
                    )
                    
                    if categorization_result.get("success"):
                        category = categorization_result.get("category")
                        confidence = categorization_result.get("confidence_score", 0)
                        successful_categorizations += 1
                        
                        # Add to summary for user
                        categorization_summary.append(f"• {item_description[:50]}{'...' if len(item_description) > 50 else ''} → {category}")
                        
                        logger.info(f"Successfully categorized '{item_description}' as {category} (confidence: {confidence:.2f})")
                    else:
                        error_msg = categorization_result.get("error", "Unknown error")
                        logger.error(f"Failed to categorize '{item_description}': {error_msg}")
                
                logger.info(f"Completed auto-categorization for RFQ {rfq_id}")
            
            logger.info("Auto-categorization process completed for all RFQs")
            
            # Generate user-friendly summary message
            if total_items_processed == 0:
                return "Auto-categorization completed, but no items were found to categorize."
            elif successful_categorizations == 0:
                return f"Auto-categorization completed for {total_items_processed} items, but categorization failed. Manual review may be needed."
            elif successful_categorizations == total_items_processed:
                summary_text = "\n".join(categorization_summary)
                return f"Auto-categorization completed successfully!\n\nCategorized items:\n{summary_text}"
            else:
                summary_text = "\n".join(categorization_summary)
                return f"Auto-categorization completed: {successful_categorizations}/{total_items_processed} items categorized successfully.\n\nSuccessfully categorized:\n{summary_text}"
            
        except Exception as e:
            logger.error(f"Error in auto-categorization process: {str(e)}")
            return "Auto-categorization encountered an error. Your RFQs have been created successfully, but manual categorization may be needed."