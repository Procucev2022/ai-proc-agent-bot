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
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.helpers.attachment_helpers import AttachmentHelpers
from app.services.session_management_service import SessionManagementService
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.attachment_decision_handler import AttachmentDecisionHandler
from app.services.processors.image_message_processor import ImageMessageProcessor

from app.services.excel_validation_service import ExcelValidationService
from app.services.excel_processing_service import ExcelProcessingService
from app.services.gmt_api_service import GMTAPIService
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.auto_categorization_service import AutoCategorizationService
from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
from app.services.seller_recommendation_service import SellerRecommendationService
from app.services.enhanced_seller_matching_service import EnhancedSellerMatchingService
from app.services.rfq_background_service import RFQBackgroundService
from app.config import get_settings
from app.services.seller_service import SellerService
from app.database import SessionLocal, DatabaseManager
from app.models import User, ConversationSession

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
        self.seller_service = SellerService()
        self.rfq_service = RFQService()
        self.whatsapp_service = WhatsAppService()
        self.openai_service = OpenAIService()
        self.db_manager = DatabaseManager()
        self.response_helpers = ResponseHelpers(self.openai_service)
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        self.auto_categorization_service = AutoCategorizationService()
        self.enhanced_auto_categorization_service = EnhancedAutoCategorizationService()
        self.seller_recommendation_service = SellerRecommendationService()
        self.enhanced_seller_matching_service = EnhancedSellerMatchingService()
        self.rfq_background_service = RFQBackgroundService()

        # Initialize extracted services
        self.session_manager = SessionManagementService(
            self.db_manager, self.whatsapp_service,
            self.chat_summary_service, self.daily_summary_service
        )
        self.confirmation_handler = ConfirmationHandler(
            self.whatsapp_service, self.response_helpers,
            self.auto_categorization_service, self.enhanced_auto_categorization_service,
            self.seller_recommendation_service, self.enhanced_seller_matching_service
        )
        self.products_array_handler = ProductsArrayHandler(
            self.whatsapp_service, self.openai_service,
            self.response_helpers, self.session_manager
        )
        self.purchase_intent_handler = PurchaseIntentHandler(
            self.whatsapp_service, self.response_helpers, self.entity_service,
            self.chat_summary_service, self.products_array_handler, self.session_manager
        )
        self.attachment_decision_handler = AttachmentDecisionHandler(
            self.whatsapp_service, self.response_helpers,
            self.purchase_intent_handler, self.session_manager
        )
        self.image_processor = ImageMessageProcessor(self.whatsapp_service, self.response_helpers)

    @log_service_method("chat_service")
    async def process_message(self, user_phone: str, message_content: str, message_type: str = "text") -> Dict[
        str, Any]:
        """
        Process incoming user message through complete pipeline.

        Orchestrates authentication check, intent classification,
        workflow routing, and response generation.
        """
        try:
            # Get or create user session using extracted service
            user = await self.session_manager.get_or_create_user(user_phone)
            session = await self.session_manager.get_conversation_context(user_phone)

            # Handle session expiry using extracted service
            session = await self.session_manager.handle_session_expiry_check(user_phone, session)

            # Track user message in conversation history using extracted service
            self.session_manager.add_message_to_history(session, "user", message_content, message_type)

            # Handle different message types
            if message_type == "text":
                result = await self._process_text_message(user, session, message_content)
            elif message_type == "interactive":
                result = await self._process_interactive_message(user, session, message_content)
            elif message_type == "excel_upload":
                result = await self._process_excel_upload(user, session, message_content)
            elif message_type == "image" or message_type == "document":
                result = await self.image_processor.process_image_message(user, session, message_content)
                # Save session after image processing
                await self.session_manager.save_session(session, "rfq_creation")
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
            # Handle seller RFQ selection workflow BEFORE intent classification
            print("session work flow type ", session.workflow_type, " session work flow state", session.workflow_state)
            if session.workflow_type.value == "seller_rfq_view":
                workflow_state = session.workflow_state or {}
                current_seller_state = workflow_state.get("seller_workflow_state")
                print("cuurent seller state", current_seller_state)

                # if current_seller_state == "seller_respond_to_rfq_list":
                #     print("calling seller flow gaian")
                # Seller is responding to RFQ list - handle this immediately
                return await self._handle_seller_flow(user, session, message)

            # Handle pending seller RFQ selection BEFORE intent classification
            # try:
            #     workflow_state = session.workflow_state or {}
            #     seller_next_step = workflow_state.get("seller_next_step")
            #     candidate_rfqs = workflow_state.get("seller_candidate_rfqs", [])
            #     candidate_rfq_ids = workflow_state.get("seller_candidate_rfq_ids", [])
            #     if seller_next_step == "awaiting_rfq_selection" and (candidate_rfqs or candidate_rfq_ids):
            #         return await self._handle_seller_rfq_selection(user, session, message)
            # except Exception as _seller_state_err:
            #     logger.error(f"Error checking seller selection state: {_seller_state_err}")

            # Check if we're already in an RFQ workflow
            existing_entities = session.workflow_state.get("extracted_entities", [])
            has_existing_data = len(existing_entities) > 0 and any(
                any(v for v in product.values() if v is not None)
                for product in existing_entities
            )

            # Also check if we have incomplete products or pending confirmations
            has_incomplete_products = bool(session.workflow_state.get("incomplete_products"))
            has_pending_confirmations = bool(
                session.workflow_state.get("pending_combined_rfq") or session.workflow_state.get("pending_rfq"))
            has_pending_optional = bool(
                session.workflow_state.get("pending_optional_rfq") or session.workflow_state.get(
                    "pending_optional_combined_rfq"))
            has_pending_attachment_decision = bool(session.workflow_state.get("awaiting_attachment_decision"))
            print(
                f"ChatService: has_existing_data={has_existing_data}, has_incomplete_products={has_incomplete_products}, has_pending_confirmations={has_pending_confirmations}, has_pending_optional={has_pending_optional}, has_pending_attachment_decision={has_pending_attachment_decision}")

            # Classify intent FIRST - if modification_request is detected, handle immediately regardless of workflow state
            conversation_context = ChatServiceHelpers.build_conversation_context(session, message)
            intent_result = self.intent_service.classify_intent(message, conversation_context)
            logger.info(f"Intent classification result: {intent_result}")

            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)

            # Handle modification requests immediately if detected with sufficient confidence
            if intent == "modification_request" and confidence > 0.7:
                logger.info(f"Modification intent detected with {confidence}% confidence - handling immediately")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)

            # Handle pending attachment decisions
            if has_pending_attachment_decision:
                return await self.attachment_decision_handler.handle_attachment_decision(user, session, message,
                                                                                         self._should_use_summary_aware_extraction)

            # Handle pending optional field responses
            if has_pending_optional:
                result = await self.confirmation_handler.handle_optional_fields_response(user, session, message)

                if result.get("status") == "continue_with_purchase_intent":
                    # User provided optional information, process it and then proceed to confirmation
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                     self._should_use_summary_aware_extraction)
                else:
                    await self.session_manager.save_session(session, 'rfq_creation')
                    return result

            # Handle pending confirmations (user responding to "Would you like to proceed?")
            if has_pending_confirmations:
                result = await self.confirmation_handler.handle_pending_confirmations(user, session, message,
                                                                                      intent_result)

                # Handle session completion if RFQs were created
                if result.get("status") == "multiple_rfqs_created":
                    # Generate enhanced session summary BEFORE clearing (non-blocking)
                    await self.session_manager.handle_session_completion_enhanced(session)
                    await self.session_manager.save_session(session, 'rfq_submitted')
                elif result.get("continue_with_purchase_intent"):
                    # Continue with purchase intent flow for modifications
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                     self._should_use_summary_aware_extraction)

                else:
                    await self.session_manager.save_session(session, 'rfq_creation')

                return result

            if has_existing_data or has_incomplete_products:
                # Already in RFQ workflow, continue collecting
                logger.info("Continuing existing RFQ workflow")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                 self._should_use_summary_aware_extraction)

            # Route based on already classified intent (intent was classified earlier in the function)
            if intent == "buy_something" and confidence > 0.7:
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)
            elif intent == "confirmation_response" and confidence > 0.7:
                # Handle confirmation responses - these should already be handled by pending confirmations check above
                # But if we reach here, treat as continuation of existing workflow
                logger.info(f"Handling confirmation response with context: {intent_result.get('context_analysis', {})}")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)
            elif intent == "reference_request" and confidence > 0.7:
                # Handle reference requests by routing to purchase intent flow
                # The EntityService will detect and handle the reference extraction
                logger.info(f"Handling reference request with context: {intent_result.get('context_analysis', {})}")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)
            elif intent == "rfq_status_check" and confidence > 0.7:
                return await self._handle_rfq_status_inquiry(user, message)
            elif intent == "sell_something" and confidence > 0.7:
                print("intent is sell something")
                return await self._handle_seller_flow(user, session, message)
            elif intent == "general_inquiry":
                return await self._handle_general_inquiry(user, message)
            elif confidence < 0.5:
                return await self._handle_clarification_request(user, message)
            else:
                return await self._handle_fallback(user, message)

        except Exception as e:
            logger.error(f"Error processing text message: {e}")
            raise

    # Additional helper method for the seller workflow
    async def _validate_seller_workflow_transition(self, session: ConversationSession,
                                                   from_state: str, to_state: str) -> bool:
        """Validate seller workflow state transitions."""
        valid_transitions = {
            "list_rfq_to_seller": ["seller_respond_to_rfq_list"],
            "seller_respond_to_rfq_list": ["sending_email_to_seller"],
            "sending_email_to_seller": ["completed"]
        }

        allowed_next_states = valid_transitions.get(from_state, [])
        return to_state in allowed_next_states

    async def _process_interactive_message(self, user: User, session: ConversationSession, content: Any) -> Dict[
        str, Any]:
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
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'validation_failed',
                                 'error': validation_error}
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
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'processing_failed',
                                 'error': processing_error}
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

    async def _handle_complete_excel(self, user: User, session: ConversationSession, processing_result: Dict) -> Dict[
        str, Any]:
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

    async def _handle_incomplete_excel(self, user: User, session: ConversationSession, excel_context: Dict) -> Dict[
        str, Any]:
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
            await self.session_manager.save_session(session, 'rfq_creation')

            return {"status": "excel_reupload_required", "response": "excel_reupload_instructions_sent"}

        except Exception as e:
            logger.error(f"Error handling incomplete Excel: {e}")
            raise

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

                return await self._process_text_message(user, await self._get_conversation_context(user.phone_number),
                                                        message)

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "registration_workflow",
                                                     "Please tell me your name to get started")

    async def _handle_general_inquiry(self, user: User, message: str) -> Dict[str, Any]:
        """Handle general inquiries using OpenAI."""
        try:
            context = ChatServiceHelpers.build_context("general_inquiry", message)

            await self._send_contextual_response(user.phone_number, context,
                                                 ["How can I help you with your procurement needs today?"],
                                                 "general_inquiry")

            return {"status": "general_inquiry_handled"}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "general_inquiry",
                                                     "How can I assist you today?")

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
            return await self._handle_error_response(e, user.phone_number, "clarification_request",
                                                     "Could you be more specific about your procurement needs?")

    async def _handle_fallback(self, user: User, message: str) -> Dict[str, Any]:
        """Handle messages that don't fit other categories."""
        try:
            context = ChatServiceHelpers.build_context("fallback", message)

            fallback_questions = ["How can I help you with your procurement needs?"]

            await self._send_contextual_response(user.phone_number, context, fallback_questions, "fallback")
            return {"status": "fallback_handled"}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "fallback_handler",
                                                     "How can I assist you today?")

    async def _handle_button_response(self, user: User, session: ConversationSession, button_id: str) -> Dict[
        str, Any]:  # noqa: ARG002
        """Handle button interaction responses."""
        # Implementation for button responses
        logger.info(f"Button response from {user.phone_number}: {button_id}")
        return {"status": "button_handled", "button_id": button_id}

    async def _handle_list_response(self, user: User, session: ConversationSession, list_id: str) -> Dict[
        str, Any]:  # noqa: ARG002
        """Handle list selection responses."""
        # Implementation for list responses
        logger.info(f"List response from {user.phone_number}: {list_id}")
        return {"status": "list_handled", "list_id": list_id}

    async def _generate_contextual_response(self, context: dict, base_questions: list = None,
                                            conversation_stage: str = "collecting") -> str:
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

    async def _handle_error_response(self, error: Exception, user_phone: str, error_type: str, fallback_message: str) -> \
    Dict[str, Any]:
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
            clean_workflow_state = self._clean_for_json_serialization(
                session.workflow_state) if session.workflow_state else {}

            session_data = {
                'session_id': session.session_id,
                'external_user_id': session.external_user_id,
                'workflow_type': workflow_type,
                'outcome': session.outcome.value if session.outcome and hasattr(session.outcome,
                                                                                'value') else session.outcome,
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

    # async def _handle_seller_flow(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
    #     """Handle RFQ status inquiry requests."""
    #     try:
    #         result = await self.seller_service.handle_seller_workflow(user, message)
    #         # If we just showed RFQs, capture selection context in session
    #         workflow_step = result.get("workflow_step")
    #         print("work flow", workflow_step)
    #         if result.get("success") and workflow_step and (workflow_step.startswith("show_rfqs_") or "rfqs" in workflow_step):
    #             session.workflow_state = session.workflow_state or {}
    #             session.workflow_state["seller_next_step"] = "awaiting_rfq_selection"
    #             # Prefer the detailed RFQ objects if available
    #             session.workflow_state["seller_candidate_rfqs"] = result.get("latest_rfqs") or []
    #             session.workflow_state["seller_candidate_rfq_ids"] = result.get("rfq_ids") or []
    #             await self.session_manager.save_session(session, "seller_flow")
    #         # Send the message back to user if present
    #         msg = result.get("message")
    #         if msg:
    #             await self.whatsapp_service.send_message(user.phone_number, msg)
    #         return {"status": "seller_flow_handled", **result}
    #
    #
    #
    #
    #     except Exception as e:
    #         logger.error(f"Error handle_seller_flow: {e}")
    #         return {"status": "error", "error": str(e)}

    async def _handle_seller_flow(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Enhanced handler for seller flow with RFQ selection support."""
        try:
            # Check current workflow state
            workflow_state = session.workflow_state or {}
            current_seller_state = workflow_state.get("seller_workflow_state")

            print(f"ChatService: Current seller workflow state: {current_seller_state}")

            # If we're in seller_rfq_view workflow, handle accordingly
            if session.workflow_type == "seller_rfq_view":
                if current_seller_state == "seller_respond_to_rfq_list":
                    print("seller reposninf ti rfq list")
                    # Seller is responding to RFQ list with selections
                    result = await self.seller_service.handle_seller_workflow(user, session, message)

                    print(f"After seller flow, session.workflow_state: {session.workflow_state}")

                    # Update session after processing
                    # if result.get("success"):
                    #     print("result is suucess")
                    # Save updated session state
                    await self.session_manager.save_session(session, "seller_rfq_view")

                    # Send response message if provided
                    response_message = result.get("message")
                    if response_message and not result.get("message_already_sent"):
                        await self.whatsapp_service.send_message(user.phone_number, response_message)

                    return {
                        "status": "seller_rfq_selection_handled",
                        **result
                    }

            # Handle initial seller flow (existing logic)
            result = await self.seller_service.handle_seller_workflow(user, session, message)

            # Update session workflow type and state if RFQ list was sent
            if result.get("success") and result.get("workflow_step") == "list_rfq_to_seller":
                print("going isnide if ", result, result.get("latest_rfqs"))
                session.workflow_type = "seller_rfq_view"
                session.workflow_state = session.workflow_state or {}
                session.workflow_state.update({
                    "seller_workflow_state": "seller_respond_to_rfq_list",
                    "available_rfqs": result.get("rfq_ids", []),
                    "seller_next_step": "awaiting_rfq_selection"
                })
                await self.session_manager.save_session(session, "seller_rfq_view")

            # Send the message back to user if present and not already sent
            msg = result.get("message")
            if msg and not result.get("message_already_sent"):
                await self.whatsapp_service.send_message(user.phone_number, msg)

            return {"status": "seller_flow_handled", **result}

        except Exception as e:
            logger.error(f"Error handle_seller_flow: {e}")
            return {"status": "error", "error": str(e)}

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

    async def _handle_seller_rfq_selection(self, user: User, session: ConversationSession, message: str) -> Dict[
        str, Any]:
        """Handle seller replying with RFQ IDs after we displayed a list in their category.

        Extract RFQ IDs using the existing AI entity extraction for rfq_status_check
        and filter them against the candidate list we showed. This prevents routing
        to rfq_status_check intent and supports multiple IDs.
        """
        try:
            workflow_state = session.workflow_state or {}
            candidate_rfqs = workflow_state.get("seller_candidate_rfqs", [])
            candidate_ids = {str(r.get("rfq_id")) for r in candidate_rfqs if r.get("rfq_id") is not None}

            # Use existing AI extraction pipeline to parse RFQ IDs from free text
            extraction = self.openai_service.extract_entities(message=message, workflow_type="rfq_status_check")
            extracted_ids = extraction.get("rfq_id") or []

            # Normalize and filter to candidates
            normalized = []
            for rid in extracted_ids:
                if rid is None:
                    continue
                rid_str = str(rid).strip()
                if rid_str in candidate_ids:
                    normalized.append(rid_str)

            # Fallback: simple regex over message to capture numbers if AI missed
            if not normalized and candidate_ids:
                import re
                possible = set(re.findall(r"\b\d{2,}\b", message))
                normalized = [rid for rid in possible if rid in candidate_ids]

            # Enforce max allowed
            max_allowed = get_settings().rfq_max_allowed
            selected_ids = normalized[:max_allowed]

            if not selected_ids:
                # Ask user to pick valid IDs from the list
                prompt = "I couldn't detect valid RFQ IDs from your reply. Please type the RFQ IDs from the list above (e.g., 3343 or 3343, 3351)."
                await self.whatsapp_service.send_message(user.phone_number, prompt)
                return {"status": "awaiting_valid_rfq_ids"}

            # Update session to mark selection captured and clear the pending flag
            workflow_state["seller_selected_rfq_ids"] = selected_ids
            workflow_state["seller_next_step"] = None
            session.workflow_state = workflow_state
            await self.session_manager.save_session(session, "seller_flow")

            # Acknowledge selection
            ack = f"Thanks! Noted RFQ ID(s): {', '.join(selected_ids)}. We will proceed accordingly."
            await self.whatsapp_service.send_message(user.phone_number, ack)

            return {"status": "seller_rfq_ids_captured", "rfq_ids": selected_ids}
        except Exception as e:
            logger.error(f"Error handling seller RFQ selection: {e}")
            await self.whatsapp_service.send_message(user.phone_number,
                                                     "Sorry, I couldn't process the RFQ IDs. Please try again with the RFQ numbers from the list.")
            return {"status": "error", "error": str(e)}