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

from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService
from app.services.welcome_message_service import get_welcome_service
from app.utils.datetime_utils import utc_now
from app.utils.logging_utils import log_service_method
from app.context import session_context, user_context, get_request_id
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
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.processors.image_message_processor import ImageMessageProcessor
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.excel_validation_service import ExcelValidationService
from app.services.excel_processing_service import ExcelProcessingService
from app.procucev_apis.rfq_apis import RFQAPIService
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.rfq_background_service import RFQBackgroundService
from app.services.rfq_status_service import RFQStatusService
from app.config import get_settings
from app.services.seller_service import SellerService
from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService
from app.services.exit_service import ExitService
from app.services.cancel_service import CancelService
from app.tools.confirmation_tool import ConfirmationTool
from app.services.confirmation_service import ConfirmationService
from app.services.workflow_manager import WorkflowManager, WorkflowStage, PendingFlag

from app.database import SessionLocal, DatabaseManager
from app.models import ConversationSession, WorkflowType
from app.schemas.user import User

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
        self.rfq_status_service = RFQStatusService()
        self.whatsapp_service = WhatsAppService()
        self.openai_service = OpenAIService()
        self.db_manager = DatabaseManager()
        self.response_helpers = ResponseHelpers(self.openai_service)
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        self.rfq_background_service = RFQBackgroundService()
        
        # Initialize extracted services first
        self.session_manager = SessionManagementService(
            self.db_manager, self.whatsapp_service,
            self.chat_summary_service, self.daily_summary_service
        )
        
        # Initialize confirmation service and tools

        
        confirmation_tool = ConfirmationTool(self.openai_service)
        confirmation_service = ConfirmationService(confirmation_tool)
        
        # Initialize authentication and registration services with session_manager
        self.authentication_service = AuthenticationService(
            self.whatsapp_service, self.openai_service, self.response_helpers, self.session_manager
        )
        self.registration_service = RegistrationService(
            self.whatsapp_service, self.openai_service, self.entity_service, self.response_helpers, confirmation_service, self.session_manager
        )
        self.exit_service = ExitService(
            self.whatsapp_service, self.authentication_service, self.session_manager, self.db_manager
        )
        self.cancel_service = CancelService(
            self.whatsapp_service, self.session_manager, self.db_manager
        )
        self.confirmation_handler = ConfirmationHandler(
            self.whatsapp_service, self.response_helpers
        )
        self.intent_switch_handler = IntentSwitchHandler(
            self.whatsapp_service, self.response_helpers
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

    def _get_workflow_or_default(self, session: ConversationSession, default: str = 'general_inquiry') -> WorkflowType:
        """
        Helper to get current workflow type as enum with fallback.

        Args:
            session: Conversation session
            default: Default workflow type string (will be converted to enum)

        Returns:
            WorkflowType enum
        """
        current = WorkflowManager.get_workflow_type(session)
        if current:
            return current
        try:
            return WorkflowType(default)
        except (ValueError, KeyError):
            return WorkflowType.general_inquiry

    @log_service_method("chat_service")
    async def process_message(self, user_phone: str, message_content: str, message_type: str = "text") -> Dict[
        str, Any]:
        """
        Process incoming user message through complete pipeline.

        Orchestrates authentication check, intent classification,
        workflow routing, and response generation.
        """
        try:
            # Check and send welcome message if needed (before session creation)
            welcome_service = get_welcome_service()
            welcome_sent = False
            if await welcome_service.should_send_welcome(user_phone):
                welcome_text = "Hello 👋, I'm QUA – your Procurement Assistant."
                message_response = await self.whatsapp_service.send_message(user_phone, welcome_text)
                if message_response.success:
                    await welcome_service.mark_welcome_sent(user_phone)
                    welcome_sent = True
                    logger.info(f"Welcome message sent to {user_phone}, continuing to process their message")
                # Continue processing user's message instead of returning early
            
            # Get or create user session using extracted service
            session = await self.session_manager.get_conversation_context(user_phone)

            # Handle session expiry using extracted service
            session = await self.session_manager.handle_session_expiry_check(user_phone, session)

            # Track user message in conversation history using extracted service
            # Classify intent for all user messages to enable proper message routing after auth
            message_intent_result = None

            # Classify intent once for all message routing and tracking
            try:
                conversation_context = ChatServiceHelpers.build_conversation_context(session, message_content)
                message_intent_result = self.intent_service.classify_intent(message_content, conversation_context)
                intent = message_intent_result.get('intent')
                confidence = message_intent_result.get('confidence', 0)
                self.session_manager.add_message_to_history(session, "user", message_content, message_type, intent, confidence)
            except Exception as e:
                # If intent classification fails, still track the message without intent
                logger.warning(f"Intent classification failed during message tracking: {e}")
                self.session_manager.add_message_to_history(session, "user", message_content, message_type)
                message_intent_result = {"intent": "general_inquiry", "confidence": 0}

            # Track meaningful messages during auth/registration flows for later processing
            self._track_meaningful_message_during_auth_flow(session, message_content, message_intent_result)

            # User Authentication flow
            # Preserve meaningful message in cache for post-auth/registration processing
            from app.services.user_cache_service import get_user_cache_service
            user_cache_service = get_user_cache_service()

            if session.workflow_state:
                last_meaningful = session.workflow_state.get("last_meaningful_message")
                last_meaningful_intent = session.workflow_state.get("last_meaningful_intent_result")

                if last_meaningful and last_meaningful_intent:
                    # Store in cache (survives workflow_state clears)
                    await user_cache_service.store_meaningful_message(user_phone, last_meaningful, last_meaningful_intent)
                    logger.info(f"Preserved meaningful message in cache: '{last_meaningful[:50]}...'")

            auth_result = await self.authentication_orchestrator_flow(user_phone, message_content, session, message_intent_result)

            # Check if authentication is still in progress
            if isinstance(auth_result, dict):
                auth_status = auth_result.get("status")
                logger.info(f"Authentication in progress - status: {auth_status}")
                
                # Authentication/registration flow statuses - stay in auth loop
                auth_in_progress_statuses = [
                    "clarification_sent", "general_inquiry_handled", "fallback_handled",
                    "redirected_to_registration", "redirected_to_email_confirmation", "otp_sent",
                    "email_selection_requested", "registration_initiated", "data_collection_in_progress",
                    "awaiting_confirmation", "registration_restarted", "otp_validated", "otp_invalid",
                    "domain_approved", "domain_approval_required", "email_confirmation_requested",
                    "auth_reg_switch_choice_presented", "exit_completed", "switch_authentication_started",
                    "role_switch_clarification_requested"
                ]
                
                if auth_status in auth_in_progress_statuses:
                    # Save session and return - do not proceed to main flow
                    current_workflow = WorkflowManager.get_workflow_type(session)
                    if current_workflow == WorkflowType.registration:
                        workflow_type = WorkflowType.registration
                    else:
                        workflow_type_str = auth_result.get("workflow_type", "authentication")
                        try:
                            workflow_type = WorkflowType(workflow_type_str)
                        except (ValueError, KeyError):
                            workflow_type = WorkflowType.authentication
                    await self.session_manager.save_session(session, workflow_type)
                    logger.info(f"Authentication flow handled - returning without main flow processing")
                    return auth_result
                elif auth_status == "registration_completed":
                    # Registration completed - check user type and handle appropriately
                    user = await self.authentication_service.validate_token(user_phone)
                    if user:
                        # Refresh user cache after successful registration to include the new account
                        logger.info(f"Refreshing user cache after registration completion for {user_phone}")
                        try:
                            # Clear existing cache first to force fresh API call
                            from app.services.user_cache_service import get_user_cache_service
                            user_cache_service = get_user_cache_service()
                            await user_cache_service.clear_user_data(user_phone)

                            # Make fresh API call to get updated user data including new account
                            auth_response = await self.authentication_service.user_authenticate(
                                user_phone, "refresh_cache_post_registration", session, intent="general_inquiry"
                            )
                            if auth_response.get("success"):
                                logger.info(f"User cache refreshed successfully after registration for {user_phone}")
                            else:
                                logger.warning(f"Failed to refresh user cache after registration for {user_phone}")
                        except Exception as e:
                            logger.error(f"Error refreshing user cache after registration for {user_phone}: {e}")
                            # Continue with flow even if cache refresh fails
                        user_type = auth_result.get("user_type", "buyer")

                        if user_type == "seller":
                            # Sellers: Registration is complete, don't process the OTP message further
                            logger.info(f"Seller registration completed - registration flow finished")
                            return {"status": "registration_completed", "message": "Seller registration successful"}
                        else:
                            # Buyers: Check if approved for main flow
                            redirect_to_main_flow = auth_result.get("redirect_to_main_flow", False)
                            approved = auth_result.get("approved", False)

                            if redirect_to_main_flow and approved:
                                # Domain approved buyers: Continue to main flow
                                # Restore meaningful message from cache if workflow_state was cleared
                                cached_meaningful = await user_cache_service.get_meaningful_message(user_phone)

                                if cached_meaningful and not session.workflow_state.get("last_meaningful_message"):
                                    session.workflow_state["last_meaningful_message"] = cached_meaningful["message"]
                                    session.workflow_state["last_meaningful_intent_result"] = cached_meaningful["intent_result"]
                                    logger.info(f"Restored meaningful message from cache after registration: {cached_meaningful['message'][:50]}...")

                                    # Clear from cache since we've restored it
                                    await user_cache_service.clear_meaningful_message(user_phone)
                                elif cached_meaningful:
                                    logger.info(f"Meaningful message already exists in workflow_state, not overwriting")
                                else:
                                    logger.warning(f"No cached meaningful message to restore after registration")

                                message_to_process, intent_to_process = self._get_meaningful_message_after_auth(
                                    session, message_content, message_intent_result
                                )
                                logger.info(f"Approved buyer registration completed - processing message: {message_to_process[:50]}...")
                                return await self._process_text_message(user, session, message_to_process, intent_to_process)
                            else:
                                # Domain NOT approved buyers: Registration complete, no further processing
                                logger.info(f"Unapproved buyer registration completed - awaiting manual approval")
                                return {"status": "registration_completed", "message": "Buyer registration successful - awaiting approval"}
                    else:
                        return {"status": "error", "error": "Session not found after registration"}
                elif auth_status == "authentication_completed":
                    # Authentication completed - check user type before processing
                    user_type = auth_result.get("user_type")

                    if user_type == "seller":
                        # For sellers, just complete authentication without processing further messages
                        # Sellers will interact with RFQs on their own initiative
                        logger.info(f"Seller authentication completed - skipping message processing")
                        await self.session_manager.save_session(session, None)
                        return {"status": "authentication_completed", "user_type": "seller"}

                    # For buyers, process the meaningful message after authentication
                    # Restore meaningful message from cache if workflow_state was cleared
                    cached_meaningful = await user_cache_service.get_meaningful_message(user_phone)

                    if cached_meaningful and not session.workflow_state.get("last_meaningful_message"):
                        session.workflow_state["last_meaningful_message"] = cached_meaningful["message"]
                        session.workflow_state["last_meaningful_intent_result"] = cached_meaningful["intent_result"]
                        logger.info(f"Restored meaningful message from cache after authentication: {cached_meaningful['message'][:50]}...")

                        # Clear from cache since we've restored it
                        await user_cache_service.clear_meaningful_message(user_phone)

                    message_to_process, intent_to_process = self._get_meaningful_message_after_auth(
                        session, message_content, message_intent_result
                    )

                    logger.info(f"Authentication completed - processing message: {message_to_process[:50]}...")
                    user = await self.authentication_service.validate_token(user_phone)
                    if user:
                        # Ensure user cache is populated after authentication
                        logger.info(f"Ensuring user cache is populated after authentication for {user_phone}")
                        try:
                            from app.services.user_cache_service import get_user_cache_service
                            user_cache_service = get_user_cache_service()
                            cached_data = await user_cache_service.get_user_data(user_phone)

                            if not cached_data:
                                # No cache exists, make API call to populate it
                                auth_response = await self.authentication_service.user_authenticate(
                                    user_phone, "refresh_cache_post_auth", session, intent="general_inquiry"
                                )
                                if auth_response.get("success"):
                                    logger.info(f"User cache populated successfully after authentication for {user_phone}")
                                else:
                                    logger.warning(f"Failed to populate user cache after authentication for {user_phone}")
                            else:
                                logger.info(f"User cache already exists after authentication for {user_phone}")
                        except Exception as e:
                            logger.error(f"Error ensuring user cache after authentication for {user_phone}: {e}")
                            # Continue with flow even if cache population fails
                        return await self._process_text_message(user, session, message_to_process, intent_to_process)
                    else:
                        return {"status": "error", "error": "Session not found after authentication"}

            # Check if auth returned User (authenticated user)
            if isinstance(auth_result, User):
                if auth_result.is_registered:
                    # User is authenticated, proceed to main flow
                    logger.info(f"User authenticated - proceeding to main flow: {auth_result.phone_number}")
                else:
                    # Invalid user but not registered, handle as general inquiry
                    return await self._process_text_message(auth_result, session, message_content, message_intent_result)
            else:
                logger.error(f"Unexpected auth_result type: {type(auth_result)}")
                return {"status": "error", "error": "Authentication failed"}

            # Only proceed to main flow if user is properly authenticated
            user = auth_result
            if message_type == "text":
                result = await self._process_text_message(user, session, message_content, message_intent_result)
            elif message_type == "interactive":
                result = await self._process_interactive_message(user, session, message_content)
            elif message_type == "excel_upload":
                result = await self._process_excel_upload(user, session, message_content)
            elif message_type == "image" or message_type == "document":
                result = await self.image_processor.process_image_message(user, session, message_content)
                await self.session_manager.save_session(session, WorkflowType.rfq_creation)
            else:
                result = {"status": "error", "error": f"Unknown message type: {message_type}"}

            # Log OpenAI call summary for performance monitoring
            call_summary = self.openai_service.get_call_summary(user_phone)
            if call_summary:
                total_calls = sum(call_summary.values())
                call_breakdown = ", ".join([f"{call_type}: {count}" for call_type, count in call_summary.items()])
                logger.info(f"OpenAI calls for {user_phone}: {total_calls} total ({call_breakdown})")

            # Include welcome message information in the result if it was sent
            if welcome_sent:
                if isinstance(result, dict):
                    result["welcome_message_sent"] = True
                    logger.info(f"Both welcome message and user message processed for {user_phone}")

            return result

        except Exception as e:
            return await self._handle_error_response(e, user_phone, "processing_message", "Please try again")

    async def authentication_orchestrator_flow(self, user_phone: str, message_content: str,
                                               session: ConversationSession, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Main authentication orchestrator function."""
        try:
            # Initialize authentication orchestrator
            from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
            from app.services.helpers.support_helpers import SupportHelpers

            auth_orchestrator = AuthenticationOrchestrator(
                self.whatsapp_service, self.response_helpers,
                self.authentication_service, self.registration_service,
                self.intent_service, SupportHelpers(self.whatsapp_service), self
            )

            return await auth_orchestrator.authentication_orchestrator_flow(
                user_phone, message_content, session, intent_result
            )

        except Exception as e:
            logger.error(f"Authentication orchestrator error for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}





    async def _process_text_message(self, user: User, session: ConversationSession, message: str, message_intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Process text message through intent classification and routing."""
        try:
            # Check if user needs registration
            if not user.is_registered:
                return await self._handle_registration_workflow(user, message)

            logger.info(f"user  phone number {user.phone_number}")
            
            # Access user details from global context
            from app.context import user_context
            # Normalize phone number (remove + prefix for consistent Redis keys)
            normalized_phone = user.phone_number.lstrip('+')
            context_data = user_context.get(normalized_phone)
            user_details = context_data.get("user_details") if context_data else None
            if user_details:
                logger.info(f"Processing with user details from global context: {user_details}")



            
            # Handle pending role switch confirmation FIRST
            if session.workflow_state.get("pending_role_switch"):
                logger.info(f"Detected pending role switch for user {user.phone_number}")
                from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
                auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
                result = await auth_reg_switch.handle_role_switch_response(user, session, message, self.authentication_service)
                await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                logger.info(f"Role switch response result: {result}")

                # If role switch completed with original message, process it
                if result.get("status") == "authentication_completed" and result.get("original_message"):
                    original_msg = result["original_message"]
                    original_intent = result.get("original_intent_result")
                    logger.info(f"Processing original message after role switch: '{original_msg[:50]}...'")

                    # Get fresh user object after switch
                    user = await self.authentication_service.validate_token(user.phone_number)
                    if user:
                        return await self._process_text_message(user, session, original_msg, original_intent)

                return result
            else:
                logger.info(f"No pending role switch detected. Workflow state keys: {list(session.workflow_state.keys()) if session.workflow_state else 'None'}")

            # Handle pending account switch confirmation (same-role switches)
            if session.workflow_state.get("pending_account_switch"):
                from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
                auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
                result = await auth_reg_switch.handle_account_switch_response(user, session, message, self.authentication_service)
                await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                return result
            
            # Handle pending intent switch choices FIRST (user responding to "1. Continue or 2. Switch")
            if session.workflow_state.get("pending_intent_switch"):
                result = await self.intent_switch_handler.handle_intent_switch_response(user, session, message)

                # Handle corrupted intent switch data
                if result.get("status") == "corrupted_intent_switch_data":
                    logger.error("Corrupted intent switch data detected, continuing with normal flow")
                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    # Fall through to normal intent processing below
                elif result.get("status") == "continue_current_workflow":
                    # User chose to continue with current workflow
                    await self.session_manager.save_session(session, self._get_workflow_or_default(session, WorkflowType.rfq_creation.value))
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                     self._should_use_summary_aware_extraction)
                elif result.get("status") == "switch_to_new_intent":
                    # User chose to switch to new intent, route to appropriate handler
                    new_intent = result["new_intent"]
                    new_message = result["new_message"]
                    intent_result = result["intent_result"]

                    # Map intent to valid workflow type (using enums)
                    if new_intent == "buy_something":
                        workflow_type = WorkflowType.rfq_creation
                    elif new_intent == "rfq_status_check":
                        workflow_type = WorkflowType.rfq_status_check
                    elif new_intent == "general_inquiry":
                        workflow_type = WorkflowType.general_inquiry
                    else:
                        workflow_type = WorkflowType.general_inquiry  # Safe default

                    WorkflowManager.set_workflow_type(session, workflow_type, caller='intent_switch_handler')
                    await self.session_manager.save_session(session, workflow_type)

                    # Route to appropriate handler based on new intent
                    if new_intent == "buy_something":
                        return await self.purchase_intent_handler.handle_purchase_intent(user, session, new_message,
                                                                                         intent_result,
                                                                                         self._should_use_summary_aware_extraction)
                    elif new_intent == "rfq_status_check":
                        return await self._handle_rfq_status_inquiry(user, new_message,session)
                    elif new_intent == "sell_something":
                        return await self._handle_seller_flow(user, session, message)
                    elif new_intent == "general_inquiry":
                        return await self._handle_general_inquiry(user, new_message)
                    else:
                        return await self._handle_fallback(user, new_message)
                elif result.get("status") == "error":
                    # Handle intent switch errors
                    logger.error(f"Intent switch error: {result.get('error')}")
                    error_response = await self.response_helpers.generate_contextual_response(
                        {"error_type": "intent_switch_error", "conversation_stage": "error"},
                        ["Sorry, there was an issue processing your request. What would you like to do?"],
                        "error"
                    )
                    await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    return result
                else:
                    # Clarification requested or other status
                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    return result

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

            # Use already-classified intent from message tracking or fallback to classification
            intent_result = message_intent_result
            if not intent_result:
                # Fallback: classify intent if not provided (shouldn't happen with our optimization)
                conversation_context = ChatServiceHelpers.build_conversation_context(session, message)
                intent_result = self.intent_service.classify_intent(message, conversation_context)
                logger.warning(f"Had to fallback to intent classification - this shouldn't happen")

            logger.info(f"Intent classification result: {intent_result}")

            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)

            # Update the last user message in conversation history with intent data
            self._update_last_user_message_with_intent(session, intent, confidence)

            # Handle exit intent immediately - highest priority
            if intent == "exit_system" and confidence > 50:
                logger.info(f"Exit intent detected with {confidence}% confidence - handling system exit")
                # Use the same phone format as used in authentication flow
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')
                exit_result = await self.exit_service.handle_exit_intent(user_phone, session)
                await self.session_manager.save_session(session, WorkflowType.user_exit)
                return exit_result

            # Handle cancel workflow intent
            if intent == "cancel_workflow" and confidence > 50:
                logger.info(f"Cancel workflow intent detected with {confidence}% confidence")
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

                # Trigger cancel confirmation flow (will send buttons)
                cancel_result = await self.cancel_service.handle_cancel_intent(user_phone, session)
                await self.session_manager.save_session(session, session.workflow_type)
                return cancel_result

            # Handle cancel confirmation response (when cancel_pending is true)
            cancel_pending = session.workflow_state and session.workflow_state.get("cancel_pending", False)
            cancel_was_declined = False
            if cancel_pending:
                logger.info(f"Cancel confirmation pending - processing user response: {message}")
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

                # Detect confirmation from the message using confirmation service
                confirmation_result = await self.cancel_service.confirmation_service.parse_confirmation(message)
                is_confirmed = confirmation_result == "yes"
                cancel_result = await self.cancel_service.handle_cancel_confirmation(user_phone, session, is_confirmed)

                if cancel_result.get("status") == "cancelled":
                    # Workflow was cancelled, save session and return
                    await self.session_manager.save_session(session, session.workflow_type)
                    return cancel_result
                elif cancel_result.get("status") == "cancelled_aborted":
                    # User declined, save session and continue with normal flow
                    await self.session_manager.save_session(session, session.workflow_type)
                    # Set flag to prevent workflow_rejection from triggering exit
                    cancel_was_declined = True
                    # Don't return - let the flow continue below to re-ask pending questions
                    logger.info("Cancellation declined - continuing with normal workflow processing")
                else:
                    # Any other status, return the result
                    return cancel_result

            # Handle workflow rejection as exit intent (but not if we just declined cancel)
            if intent == "workflow_rejection" and confidence > 60 and not cancel_was_declined:
                logger.info(f"Workflow rejection detected with {confidence}% confidence - exiting user")
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')
                exit_result = await self.exit_service.handle_exit_intent(user_phone, session)
                await self.session_manager.save_session(session, WorkflowType.user_exit)
                return exit_result

            # Handle support requests immediately - even during active workflows
            if intent == "support" and confidence > 0.7:
                logger.info(f"Support intent detected with {confidence}% confidence - handling immediately")
                result = await self._handle_support_request(user, message)
                return result

            # Handle contextual intents with direct response capability (but not if we just declined cancel)
            if intent in ['contextual_reference', 'session_inquiry', 'workflow_rejection', 'alternative_request'] and confidence > 60:
                # Skip workflow_rejection if we just declined a cancel confirmation
                if intent == 'workflow_rejection' and cancel_was_declined:
                    logger.info(f"Skipping workflow_rejection handler - user just declined cancel confirmation")
                elif intent_result.get('should_handle_directly'):
                    logger.info(f"Contextual intent detected: {intent} with {confidence}% confidence - handling directly")
                    return await self._handle_contextual_interaction(user, session, message, intent_result)
            
            # Handle modification requests immediately if detected with sufficient confidence
            if intent == "modification_request" and confidence > 0.7:
                logger.info(f"Modification intent detected with {confidence}% confidence - handling immediately")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)

            # Handle pending attachment decisions
            if has_pending_attachment_decision:
                return await self.attachment_decision_handler.handle_attachment_decision(user, session, message,
                                                                                         self._should_use_summary_aware_extraction)

            # Check for intent switch during pending optional/confirmation states BEFORE handling them
            if (
                    has_pending_optional or has_pending_confirmations) and await self.intent_switch_handler.should_handle_intent_switch(
                    session, intent, confidence, intent_result.get('context_analysis')):
                result = await self.intent_switch_handler.handle_intent_switch_choice(user, session, message, intent,
                                                                                      intent_result)
                await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                return result

            # Handle pending optional field responses
            if has_pending_optional:
                result = await self.confirmation_handler.handle_optional_fields_response(user, session, message)

                if result.get("status") == "continue_with_purchase_intent":
                    # User provided optional information, process it and then proceed to confirmation
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                     self._should_use_summary_aware_extraction)
                else:
                    await self.session_manager.save_session(session, WorkflowType.rfq_creation)
                    return result

            # Handle pending confirmations (user responding to "Would you like to proceed?")
            if has_pending_confirmations:
                result = await self.confirmation_handler.handle_pending_confirmations(user, session, message,
                                                                                      intent_result)

                # Handle session completion if RFQs were created
                if result.get("status") == "multiple_rfqs_created":
                    # Generate enhanced session summary BEFORE clearing (non-blocking)
                    await self.session_manager.handle_session_completion_enhanced(session)
                    await self.session_manager.save_session(session, WorkflowType.rfq_submitted)
                elif result.get("continue_with_purchase_intent"):
                    # Continue with purchase intent flow for modifications
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                     self._should_use_summary_aware_extraction)

                else:
                    await self.session_manager.save_session(session, WorkflowType.rfq_creation)

                return result

            if has_existing_data or has_incomplete_products:
                # Check for intent switch during active workflow BEFORE continuing
                if await self.intent_switch_handler.should_handle_intent_switch(session, intent, confidence, intent_result.get('context_analysis')):
                    result = await self.intent_switch_handler.handle_intent_switch_choice(user, session, message,
                                                                                          intent, intent_result)
                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    return result

                # Already in RFQ workflow, continue collecting
                logger.info("Continuing existing RFQ workflow")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                 self._should_use_summary_aware_extraction)

            # Handle seller RFQ selection workflow BEFORE intent classification
            if session.workflow_type and hasattr(session.workflow_type,
                                                 'value') and session.workflow_type.value == "seller_rfq_view":
                workflow_state = session.workflow_state or {}
                current_seller_state = workflow_state.get("seller_workflow_state")
                # Seller is responding to RFQ list - handle this immediately
                return await self._handle_seller_flow(user, session, message)

            # Check if user recently completed registration and handle follow-up messages
            recently_registered = session.workflow_state.get("recently_completed_registration", False)
            if recently_registered and confidence < 0.6:
                # User just completed registration, treat ambiguous messages as potential purchase intent
                logger.info(f"Post-registration message detected, treating as purchase intent: {message}")
                # Clear the registration completion flag
                session.workflow_state.pop("recently_completed_registration", None)
                session.workflow_state.pop("registration_completion_time", None)
                # Route to purchase intent with higher confidence
                modified_intent_result = intent_result.copy()
                modified_intent_result["intent"] = "buy_something"
                modified_intent_result["confidence"] = 75
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, modified_intent_result, self._should_use_summary_aware_extraction)
            
            # Check for registration intent (new account registration)
            if intent == "register_account" and confidence > 0.6:
                registration_details = intent_result.get("context_analysis", {}).get("registration_details", {})
                registration_type = registration_details.get("registration_type")

                if registration_type in ["buyer", "seller"]:
                    if user.is_registered:
                        # User is authenticated - treat as account switch with registration option
                        current_role = user.role.value if hasattr(user.role, 'value') else user.role
                        from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
                        auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)

                        if registration_type != current_role:
                            # Cross-role registration (buyer wants to register seller)
                            result = await auth_reg_switch.handle_role_switch_confirmation(user, session, message, registration_type)
                        else:
                            # Same-role registration (buyer wants to register another buyer)
                            result = await auth_reg_switch.handle_account_switch_confirmation(user, session, message, registration_type)

                        await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                        return result
                    else:
                        # User not authenticated - direct to registration
                        result = await self.registration_service.initiate_registration(
                            user.phone_number, session, registration_type, message
                        )
                        await self.session_manager.save_session(session, WorkflowType.registration)
                        return result
                else:
                    # Unclear registration type - ask for clarification
                    await self.whatsapp_service.send_message(
                        user.phone_number,
                        "Would you like to register as a buyer or seller?"
                    )
                    return {"status": "registration_clarification_requested"}

            # Check for role switch (authenticated user wanting to switch from buyer to seller or vice versa)
            if user.is_registered and confidence > 0.7:
                current_role = user.role.value if hasattr(user.role, 'value') else user.role
                if intent == "sell_something" and current_role == "buyer":
                    # Cross-role switch: buyer -> seller, check if user has seller accounts
                    from app.services.user_cache_service import get_user_cache_service
                    from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch

                    user_cache_service = get_user_cache_service()
                    account_options = await user_cache_service.get_account_options_for_intent_switch(
                        user.phone_number, "sell_something", getattr(user, 'email', None) or getattr(user, 'username', None)
                    )

                    auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
                    if account_options and account_options.get("success") and account_options.get("has_target_accounts"):
                        # User has seller accounts - show enhanced selection
                        logger.info("Cross-role switch (buyer->seller) with cached seller accounts - using enhanced selection")
                        result = await auth_reg_switch.handle_role_switch_confirmation(user, session, message, "seller")
                    else:
                        # No cached seller accounts - use standard role switch
                        logger.info("Cross-role switch (buyer->seller) without cached seller accounts - using standard flow")
                        result = await auth_reg_switch.handle_role_switch_confirmation(user, session, message, "seller")

                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    return result

                elif intent == "buy_something" and current_role == "seller":
                    # Cross-role switch: seller -> buyer, check if user has buyer accounts
                    from app.services.user_cache_service import get_user_cache_service
                    from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch

                    user_cache_service = get_user_cache_service()
                    account_options = await user_cache_service.get_account_options_for_intent_switch(
                        user.phone_number, "buy_something", getattr(user, 'email', None) or getattr(user, 'username', None)
                    )

                    auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
                    if account_options and account_options.get("success") and account_options.get("has_target_accounts"):
                        # User has buyer accounts - show enhanced selection
                        logger.info("Cross-role switch (seller->buyer) with cached buyer accounts - using enhanced selection")
                        result = await auth_reg_switch.handle_role_switch_confirmation(user, session, message, "buyer")
                    else:
                        # No cached buyer accounts - use standard role switch
                        logger.info("Cross-role switch (seller->buyer) without cached buyer accounts - using standard flow")
                        result = await auth_reg_switch.handle_role_switch_confirmation(user, session, message, "buyer")

                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    return result
            
            # Route based on already classified intent (intent was classified earlier in the function)
            if intent == "buy_something" and confidence > 0.7:
                # Normal buy_something flow - user wants to buy with current account
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
                return await self._handle_rfq_status_inquiry(user, message, session)
            elif intent == "sell_something" and confidence > 0.7:
                # Normal sell_something flow - user wants to sell with current account
                return await self._handle_seller_flow(user, session, message)
            elif intent == "account_switch" and confidence > 0.7:
                return await self._handle_account_switch_intent(user, session, message, intent_result)
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

                await self.session_manager.send_and_track_message(user.phone_number, registration_response, session)
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
                await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
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
                await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
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
                await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
                return {"status": "handled", "response": "processing_failed"}

            # Prepare context using helpers
            excel_context = ExcelHelpers.prepare_excel_context(processing_result, user.phone_number)

            # Update session
            WorkflowManager.set_workflow_type(session, WorkflowType.rfq_creation, caller='excel_upload_handler')
            WorkflowManager.initialize_workflow_state(session)
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
                await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
                return {"status": "failed", "error": error_msg}

            # Create GMT template and submit
            processing_service = ExcelProcessingService(self.openai_service)
            template_bytes = processing_service.create_standard_template(processing_result['items'])
            api_data = processing_service.encode_for_api(template_bytes, processing_result['filename'])

            # Submit to GMT API
            rfq_service = RFQAPIService()
            gmt_result = await rfq_service.bulk_upload_rfq(api_data)

            if gmt_result.get('success'):
                # Generate completion response using OpenAI
                rfq_data = {
                    'items': processing_result['items'],
                    'filename': processing_result['filename'],
                    'total_items': processing_result['total_items']
                }

                completion_response = self.openai_service.generate_completion_response(rfq_data, context)
                await self.session_manager.send_and_track_message(user.phone_number, completion_response, session)
                
                session.outcome = 'completed'
                session.completed_at = utc_now().replace(tzinfo=None)

                # Generate enhanced session summary (non-blocking)
                await self._handle_session_completion_enhanced(session)

                await self._save_session(session, WorkflowType.rfq_submitted)

                return {"status": "completed", "response": "rfq_created"}
            else:
                # GMT API failed, fall back to conversation completion
                error_context = {**context, 'error': gmt_result.get('error', 'Unknown error')}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    ["There was an issue creating the RFQ. Let me help you complete it through conversation."],
                    "error_recovery"
                )
                await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
                return await self._handle_incomplete_excel(user, session, {"excel_data": processing_result})

        except Exception as e:
            logger.error(f"Error handling complete Excel: {e}")
            error_context = {'error': str(e), 'workflow_type': 'excel_rfq_upload'}
            error_response = await self.response_helpers.generate_contextual_response(
                error_context,
                ["There was an issue processing your Excel file. Let me help you through conversation."],
                "error_recovery"
            )
            await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
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
            
            await self.session_manager.send_and_track_message(user.phone_number, clarification_response, session)
            
            # Update session state - waiting for excel reupload
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['stage'] = 'excel_reupload_required'
            session.workflow_state['pending_excel_reupload'] = True
            session.workflow_state['last_excel_issues'] = missing_fields
            await self.session_manager.save_session(session, WorkflowType.rfq_creation)

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
                
                # Note: This is registration workflow - session may not be available
                # Need to get session for proper tracking
                session = await self.session_manager.get_conversation_context(user.phone_number)
                await self.session_manager.send_and_track_message(user.phone_number, welcome_message, session)
                return {"status": "registered", "user_name": name}
            else:
                # User already has name, mark as registered
                with SessionLocal() as db:
                    db_user = db.query(User).filter(User.id == user.id).first()
                    db_user.is_registered = True
                    db.commit()

                return await self._process_text_message(user, await self.session_manager.get_conversation_context(user.phone_number),
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

    async def _handle_support_request(self, user: User, message: str) -> Dict[str, Any]:
        """Handle support requests by providing contact information."""
        try:
            settings = get_settings()
            support_contact = settings.support_contact_info
            
            support_message = f"For support assistance, please contact us at: {support_contact}"
            
            await self.whatsapp_service.send_message(user.phone_number, support_message)
            
            return {"status": "support_handled"}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "support_request",
                                                     "For support, please contact info.support.com")

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

    async def _handle_account_switch_intent(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle account switch intent."""
        try:
            from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch

            # Extract target role from context analysis
            context_analysis = intent_result.get("context_analysis", {})
            account_switch_details = context_analysis.get("account_switch_details", {})
            target_role = account_switch_details.get("target_role", "buyer")
            switch_type = account_switch_details.get("switch_type", "unclear")

            logger.info(f"Handling account switch intent: target_role={target_role}, switch_type={switch_type}")

            auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)

            # Check current user role
            current_role = user.role.value if hasattr(user.role, 'value') else user.role

            if target_role != current_role:
                # Cross-role switch (buyer -> seller or seller -> buyer)
                result = await auth_reg_switch.handle_role_switch_confirmation(user, session, message, target_role)
            else:
                # Same-role switch (buyer -> different buyer, seller -> different seller)
                result = await auth_reg_switch.handle_account_switch_confirmation(user, session, message, target_role)

            await self.session_manager.save_session(session, self._get_workflow_or_default(session))
            return result

        except Exception as e:
            logger.error(f"Error handling account switch intent: {e}")
            return await self._handle_error_response(e, user.phone_number, "account_switch_error",
                                                     "I had trouble processing your account switch request. Please try again.")

    async def _check_for_enhanced_account_selection(self, user: User, session: ConversationSession, message: str, target_intent: str) -> Dict[str, Any]:
        """
        Check if we should use enhanced account selection for same-role switches.

        This method checks the cached user data to see if the user has multiple accounts
        for the target intent and shows them actual account options instead of generic choices.

        Args:
            user: Current user
            session: Current session
            message: User's message
            target_intent: Target intent (buy_something/sell_something)

        Returns:
            Dict with status and result - 'handled' if enhanced selection was used, 'not_applicable' otherwise
        """
        try:
            from app.services.user_cache_service import get_user_cache_service
            from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch

            current_role = user.role.value if hasattr(user.role, 'value') else user.role
            target_role = "buyer" if target_intent == "buy_something" else "seller"
            current_email = getattr(user, 'email', None) or getattr(user, 'username', None)

            logger.info(f"Enhanced account selection check: current_role={current_role}, target_role={target_role}, target_intent={target_intent}")

            # Only apply enhanced selection for same-role switches
            if current_role != target_role:
                logger.info("Cross-role switch detected - enhanced selection not applicable")
                return {"status": "not_applicable", "reason": "cross_role_switch"}

            # Check if we have cached user data
            user_cache_service = get_user_cache_service()
            account_options = await user_cache_service.get_account_options_for_intent_switch(
                user.phone_number, target_intent, current_email
            )

            if not account_options or not account_options.get("success"):
                logger.info("No cached data available - enhanced selection not applicable")
                return {"status": "not_applicable", "reason": "no_cached_data"}

            has_target_accounts = account_options.get("has_target_accounts", False)

            if not has_target_accounts:
                logger.info("No target accounts available - enhanced selection not applicable")
                return {"status": "not_applicable", "reason": "no_target_accounts"}

            # We have target accounts - use enhanced account selection
            logger.info("Enhanced account selection applicable - showing actual account options")

            auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
            result = await auth_reg_switch.handle_account_switch_confirmation(user, session, message, target_role)

            await self.session_manager.save_session(session, self._get_workflow_or_default(session))

            return {"status": "handled", "result": result}

        except Exception as e:
            logger.error(f"Error in enhanced account selection check: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_button_response(self, user: User, session: ConversationSession, button_id: str) -> Dict[
        str, Any]:
        """Handle button interaction responses."""
        logger.info(f"Button response from {user.phone_number}: {button_id}")

        # Handle cancel workflow confirmation buttons
        if button_id in ["confirm_cancel", "decline_cancel"]:
            return await self._handle_cancel_confirmation_button(user, session, button_id)

        # Handle modify button by simulating "modify" message
        if button_id == "no_rfq":
            return await self._process_text_message(user, session, "modify")

        # Check if this is a confirmation button response
        if button_id == "confirm_rfq":
            # Route to confirmation handler
            return await self.confirmation_handler.handle_confirmation_button(user, session, button_id)

        # Check if this is an email confirmation button response during authentication
        if button_id in ["confirm_email", "reject_email"]:
            # Route to authentication email confirmation handler
            return await self._handle_authentication_email_button(user, session, button_id)

        # Default button handling
        return {"status": "button_handled", "button_id": button_id}

    async def _handle_authentication_email_button(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle email confirmation button responses during authentication."""
        logger.info(f"Authentication email button response from {user.phone_number}: {button_id}")
        
        try:
            if button_id == "confirm_email":
                # User confirmed the email - simulate "yes" response
                return await self.authentication_service.handle_email_confirmation(
                    user.phone_number, "yes", session
                )
            elif button_id == "reject_email":
                # User rejected the email - simulate "no" response  
                return await self.authentication_service.handle_email_confirmation(
                    user.phone_number, "no", session
                )
            else:
                return {"status": "unknown_email_button", "button_id": button_id}
                
        except Exception as e:
            logger.error(f"Error handling authentication email button: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_cancel_confirmation_button(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle cancel workflow confirmation button responses."""
        logger.info(f"Cancel confirmation button response from {user.phone_number}: {button_id}")

        try:
            user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

            # Map button ID to confirmation result
            # confirm_cancel -> Yes, decline_cancel -> No
            is_confirmed = button_id == "confirm_cancel"

            cancel_result = await self.cancel_service.handle_cancel_confirmation(user_phone, session, is_confirmed)

            if cancel_result.get("status") == "cancelled":
                # Workflow was cancelled, save session and return
                await self.session_manager.save_session(session, session.workflow_type)
                return cancel_result
            elif cancel_result.get("status") == "cancelled_aborted":
                # User declined, save session and continue with normal flow
                await self.session_manager.save_session(session, session.workflow_type)
                # Don't return - let the message processing continue
                logger.info("User declined cancel via button - would need to re-process as normal message")
                # Since this is a button response, we can't continue the flow here
                # We need to return a special status to trigger the workflow to continue
                return cancel_result

        except Exception as e:
            logger.error(f"Error handling cancel confirmation button: {e}")
            return {"status": "error", "error": str(e)}

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
        # Use the fallback message directly instead of generating with OpenAI to avoid unnecessary API calls
        await self.whatsapp_service.send_message(user_phone, fallback_message)
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
            message = "Registration system is in progress, continuing with your request..."
            await self.whatsapp_service.send_message(user_phone, message)
            logger.info(f"Sent authentication placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending authentication placeholder: {e}")

    async def _show_seller_flow_placeholder(self, user_phone: str) -> None:
        """Show seller flow placeholder message."""
        try:
            message = "Seller flow is in progress. Our team will contact you shortly with RFQ opportunities."
            await self.whatsapp_service.send_message(user_phone, message)
            logger.info(f"Sent seller flow placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending seller flow placeholder: {e}")

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

    async def _handle_rfq_status_inquiry(self, user: User, message: str, session: ConversationSession = None) -> Dict[str, Any]:
        # Help 1 : how to handle session here, like what data needs to be save in db and how to do it
        """Handle RFQ status inquiry requests."""
        return await self.rfq_status_service.handle_rfq_status_inquiry(user, message, session)

    async def _handle_seller_flow(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """
        Enhanced handler for seller flow with complete workflow state management.

        Handles:
        - Initial seller flow (RFQ display)
        - RFQ selection with credit checks
        - Plan upgrade requests
        - Payment processing
        - General seller queries
        """
        try:
            # Check if we're already in a seller workflow
            workflow_state = session.workflow_state or {}
            current_seller_state = workflow_state.get("seller_workflow_state")

            logger.info(f"ChatService: Handling seller flow - Current state: {current_seller_state}")

            # Handle different seller workflow states
            if session.workflow_type and hasattr(session.workflow_type, 'value'):
                workflow_type = session.workflow_type.value
            else:
                workflow_type = str(session.workflow_type) if session.workflow_type else None

            if workflow_type == "seller_rfq_view":
                # We're in an active seller workflow - delegate to seller service
                result = await self.seller_service.handle_seller_workflow(user, session, message)

                # The seller service handles session updates internally
                # Only send message if not already sent
                response_message = result.get("message")
                if response_message and not result.get("message_already_sent"):
                    await self.whatsapp_service.send_message(user.phone_number, response_message)
                    self.session_manager.add_message_to_history(session, "assistant", response_message)

                return {
                    "status": "seller_workflow_handled",
                    **result
                }
            else:
                # Initial seller flow - starting new workflow
                result = await self.seller_service.handle_seller_workflow(user, session, message)

                # Handle workflow initialization
                if result.get("success"):
                    workflow_step = result.get("workflow_step")

                    # Update session workflow type based on result
                    if workflow_step in ["display_rfqs_to_seller", "show_subscription_plans"]:
                        WorkflowManager.set_workflow_type(session, WorkflowType.seller_rfq_view, caller='seller_flow_init')
                        await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

                # Send response message if provided and not already sent
                response_message = result.get("message")
                if response_message and not result.get("message_already_sent"):
                    await self.whatsapp_service.send_message(user.phone_number, response_message)
                    self.session_manager.add_message_to_history(session, "assistant", response_message)

                return {
                    "status": "seller_flow_initiated",
                    **result
                }

        except Exception as e:
            logger.error(f"Error in seller flow handler: {e}")

            # Generate error response using AI
            error_context = {
                "workflow_state": "seller_flow_error",
                "error_message": str(e)
            }

            try:
                error_response = await self.response_helpers.generate_seller_contextual_response(error_context)
                await self.whatsapp_service.send_message(user.phone_number, error_response)
            except Exception as response_error:
                logger.error(f"Error generating seller error response: {response_error}")
                await self.whatsapp_service.send_message(
                    user.phone_number,
                    "I encountered an issue processing your request. Please contact support@procurev.com"
                )

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
        # COMMENTED OUT: Disable automatic reference detection to enforce session timeout behavior
        # When sessions timeout, users should lose context and start fresh
        return False
        
        # try:
        #     # Use OpenAI service for intelligent reference detection
        #     reference_analysis = self.openai_service.analyze_reference_context(message)

        #     has_references = reference_analysis.get("has_references", False)
        #     confidence = reference_analysis.get("confidence", 0)
        #     reference_types = reference_analysis.get("reference_types", [])

        #     # Use summary-aware extraction if we have high confidence references
        #     should_use_summary = has_references and confidence >= 70

        #     print(f"ChatService: Reference analysis for '{message}':")
        #     print(f"  - Has references: {has_references}")
        #     print(f"  - Confidence: {confidence}%")
        #     print(f"  - Reference types: {reference_types}")
        #     print(f"  - Use summary-aware extraction: {should_use_summary}")

        #     return should_use_summary

        # except Exception as e:
        #     print(f"ChatService: Error in reference analysis: {e}")
        #     # Fallback: if analysis fails, don't use summary-aware extraction
        #     return False

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
            await self.session_manager.save_session(session, WorkflowType.general_inquiry)  # seller_flow not in enum

            # Acknowledge selection
            ack = f"Thanks! Noted RFQ ID(s): {', '.join(selected_ids)}. We will proceed accordingly."
            await self.whatsapp_service.send_message(user.phone_number, ack)

            return {"status": "seller_rfq_ids_captured", "rfq_ids": selected_ids}
        except Exception as e:
            logger.error(f"Error handling seller RFQ selection: {e}")
            await self.whatsapp_service.send_message(user.phone_number,
                                                     "Sorry, I couldn't process the RFQ IDs. Please try again with the RFQ numbers from the list.")
            return {"status": "error", "error": str(e)}

    async def _generate_session_summary(self, session: ConversationSession) -> str:
        """
        Generate a user-friendly summary of collected information for display.
        
        This creates a simple summary showing what products/information has been collected
        so far in the conversation, similar to the confirmation message format.
        
        Args:
            session: Current conversation session
            
        Returns:
            String summary of collected information for user display
        """
        try:
            summary_parts = []
            workflow_state = session.workflow_state or {}
            
            # Get all collected product entities from various storage locations
            all_products = []
            
            # Check extracted_entities (main storage)
            extracted_entities = workflow_state.get('extracted_entities', [])
            if extracted_entities:
                all_products.extend(extracted_entities)
            
            # Check incomplete_products 
            incomplete_products = workflow_state.get('incomplete_products', [])
            if incomplete_products:
                # Handle different data structures
                if isinstance(incomplete_products, list):
                    all_products.extend(incomplete_products)
                elif isinstance(incomplete_products, dict) and 'entities' in incomplete_products:
                    # Handle serialized product with entities
                    entities = incomplete_products['entities']
                    if isinstance(entities, list):
                        all_products.extend(entities)
                    else:
                        all_products.append(entities)
                elif isinstance(incomplete_products, dict):
                    # Single product dict
                    all_products.append(incomplete_products)
            
            # Check complete_products
            complete_products = workflow_state.get('complete_products', [])
            if complete_products:
                # Handle different data structures
                if isinstance(complete_products, list):
                    all_products.extend(complete_products)
                elif isinstance(complete_products, dict) and 'entities' in complete_products:
                    # Handle serialized product with entities
                    entities = complete_products['entities']
                    if isinstance(entities, list):
                        all_products.extend(entities)
                    else:
                        all_products.append(entities)
                elif isinstance(complete_products, dict):
                    # Single product dict
                    all_products.append(complete_products)
            
            # Check pending RFQ data
            if workflow_state.get('pending_rfq'):
                pending_data = workflow_state['pending_rfq']
                if 'entities' in pending_data:
                    entities = pending_data['entities']
                    if isinstance(entities, list):
                        all_products.extend(entities)
                    elif isinstance(entities, dict):
                        all_products.append(entities)
            
            # Check pending combined RFQ data
            if workflow_state.get('pending_combined_rfq'):
                combined_data = workflow_state['pending_combined_rfq']
                if 'products' in combined_data:
                    for product in combined_data['products']:
                        if 'entities' in product:
                            entities = product['entities']
                            if isinstance(entities, dict):
                                all_products.append(entities)
            
            # If still no products found, search through all workflow_state for any dict with product-like fields
            if not all_products:
                for key, value in workflow_state.items():
                    if isinstance(value, dict):
                        # Check if this looks like a product (has product_name or similar)
                        if any(field in value for field in ['product_name', 'productName', 'description', 'projectDesc']):
                            all_products.append(value)
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict) and any(field in item for field in ['product_name', 'productName', 'description', 'projectDesc']):
                                all_products.append(item)
            
            # Remove duplicates based on product_name
            seen_products = set()
            unique_products = []
            for product in all_products:
                if isinstance(product, dict):
                    # Try different possible product name fields
                    product_name = product.get('product_name') or product.get('productName') or product.get('description') or product.get('projectDesc') or 'Unknown Product'
                    product_key = f"{product_name}_{product.get('quantity', '')}"
                    if product_key not in seen_products:
                        seen_products.add(product_key)
                        unique_products.append(product)
            
            all_products = unique_products
            
            if all_products:
                summary_parts.append("**Collected Information:**")
                summary_parts.append("")
                
                for product in all_products:
                    # Handle nested entities structure
                    if 'entities' in product and isinstance(product['entities'], dict):
                        entities = product['entities']
                        product_name = entities.get('description') or entities.get('projectDesc') or 'Product'
                        quantity = entities.get('quantity') or ''
                        brand = entities.get('brand') or ''
                        delivery_date = entities.get('deliveryDate') or ''
                        missing_fields = product.get('missing_fields', [])
                    else:
                        product_name = product.get('description') or product.get('product_name') or 'Product'
                        quantity = product.get('quantity') or ''
                        brand = product.get('brand') or ''
                        delivery_date = product.get('delivery_date') or ''
                        missing_fields = []
                    
                    # Product header
                    summary_parts.append(f"**{product_name.title()}:**")
                    
                    # Add available details
                    if quantity:
                        summary_parts.append(f"Quantity: {quantity}")
                    if brand:
                        summary_parts.append(f"Brand: {brand}")
                    if delivery_date:
                        summary_parts.append(f"Delivery Date: {delivery_date}")
                    
                    # Add missing fields if any
                    if missing_fields:
                        summary_parts.append("")
                        summary_parts.append(f"**Still Required:** {', '.join(missing_fields).replace('_', ' ').title()}")
                    
                    summary_parts.append("")  # Empty line between products
                
                # Remove last empty line
                if summary_parts and summary_parts[-1] == "":
                    summary_parts.pop()
            else:
                summary_parts.append("**Collected Information:**")
                summary_parts.append("")
                summary_parts.append("No products discussed yet.")
            
            return '\n'.join(summary_parts)
            
        except Exception as e:
            logger.error(f"Error generating session summary: {e}")
            return "I don't have any product information collected yet. What would you like to procure?"

    async def _handle_contextual_interaction(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle contextual interactions with SAFE session management.

        This method processes contextual intents like session_inquiry, contextual_reference,
        workflow_rejection, and alternative_request. CRITICAL: Uses WorkflowManager to prevent
        accidental data loss.

        Args:
            user: User making the request
            session: Current conversation session
            message: User's contextual message
            intent_result: Result from intent classification with contextual data

        Returns:
            Dict with status and result information
        """
        try:
            # Get contextual response and actions from intent result
            contextual_response = intent_result.get('contextual_response', 'I understand your request.')
            contextual_actions = intent_result.get('contextual_actions', [])
            context_understanding = intent_result.get('context_understanding', {})

            logger.info(f"[CONTEXTUAL_INTERACTION] Intent: {context_understanding.get('user_intent', 'unknown')}, "
                       f"Actions: {len(contextual_actions)}, "
                       f"Current workflow: {WorkflowManager.get_workflow_type(session)}")

            # Check if user has active RFQ data that would be lost
            has_active_data = bool(
                session.workflow_state and (
                    session.workflow_state.get('extracted_entities') or
                    WorkflowManager.has_any_pending_confirmation(session)
                )
            )

            # Process each contextual action WITH SAFEGUARDS
            session_updated = False
            destructive_action_blocked = False

            for action in contextual_actions:
                action_type = action.get('type')
                action_description = action.get('description', '')

                logger.info(f"[CONTEXTUAL_ACTION] Type: {action_type} - {action_description}")

                # SAFE ACTIONS (No data loss risk)
                if action_type == 'show_session_summary':
                    # Generate session summary and replace the contextual response entirely
                    session_summary = await self._generate_session_summary(session)
                    if session_summary:
                        contextual_response = session_summary  # Replace, don't append

                elif action_type == 'suggest_alternatives':
                    # Add common alternatives to response
                    alt_text = "\n\n**Search BFS Inventory** - Check immediate availability\n**Product Information** - Get details about our services\n**General Inquiry** - Ask questions about the process"
                    contextual_response += alt_text

                elif action_type == 'update_entities' or action_type == 'modify_existing_data':
                    # This would need more complex parsing from the original message
                    # For now, just acknowledge that we understand they want to modify something
                    contextual_response += "\n\nI understand you want to modify the information. Please let me know specifically what you'd like to change."

                # DESTRUCTIVE ACTIONS (Require validation)
                elif action_type in ['change_workflow_state', 'change_workflow_type', 'rollback_to_previous',
                                    'clear_session_data', 'restart_workflow']:

                    # CRITICAL: Block destructive actions if user has active data
                    if has_active_data:
                        logger.warning(f"[DESTRUCTIVE_ACTION_BLOCKED] Blocked '{action_type}' - User has active RFQ data. "
                                     f"This prevents data loss bug. User should explicitly confirm if they want to reset.")
                        destructive_action_blocked = True

                        # Instead of destroying data, inform user
                        contextual_response = ("I see you have an active RFQ in progress. " +
                                             contextual_response +
                                             "\n\nWould you like to:\n1. Continue with your current request\n2. Start a new request (this will clear your current data)")
                    else:
                        # Safe to execute - no data to lose
                        session_updated = True

                        if action_type == 'change_workflow_state':
                            # Use WorkflowManager for safe state changes
                            WorkflowManager.set_stage(session, WorkflowStage.COLLECTING, caller='contextual_interaction')
                            WorkflowManager.clear_pending(session, PendingFlag.COMBINED_RFQ, PendingFlag.RFQ,
                                                         caller='contextual_interaction')
                            logger.info(f"[SAFE_STATE_CHANGE] Changed workflow state to: collecting")

                        elif action_type == 'change_workflow_type':
                            # Use WorkflowManager with validation
                            WorkflowManager.transition_workflow(
                                session,
                                WorkflowType.general_inquiry,
                                validate=True,
                                caller='contextual_interaction'
                            )
                            logger.info(f"[SAFE_TRANSITION] Changed workflow type to: general_inquiry")

                        elif action_type == 'rollback_to_previous':
                            await self._rollback_to_stage(session, 'collecting')

                        elif action_type == 'clear_session_data':
                            # Use safe clearing that preserves critical data
                            WorkflowManager.clear_all_rfq_pending(session, caller='contextual_interaction')

                        elif action_type == 'restart_workflow':
                            # Use safe reset that preserves conversation history
                            WorkflowManager.safe_reset_workflow_state(
                                session,
                                preserve_fields=['conversation_history'],
                                caller='contextual_interaction'
                            )

                else:
                    logger.info(f"[UNKNOWN_ACTION] Processed contextual action: {action_type}")

            # Log if destructive action was blocked
            if destructive_action_blocked:
                logger.warning(f"[DATA_LOSS_PREVENTED] Blocked AI-triggered destructive action for session {session.session_id}. "
                             f"This prevents the 'fields asked repeatedly' bug.")

            # Send the contextual response to user
            await self.session_manager.send_and_track_message(user.phone_number, contextual_response, session)

            # Save session if any updates were made
            if session_updated:
                current_workflow = WorkflowManager.get_workflow_type(session)
                await self.session_manager.save_session(session, current_workflow)
                logger.info("[CONTEXTUAL_INTERACTION] Session updated and saved")

            return {
                "status": "contextual_interaction_handled",
                "user_intent": context_understanding.get('user_intent', 'unknown'),
                "actions_performed": len(contextual_actions),
                "destructive_blocked": destructive_action_blocked,
                "confidence": context_understanding.get('confidence', 0)
            }
            
        except Exception as e:
            logger.error(f"Error handling contextual interaction: {e}")
            
            # Send fallback response
            fallback_response = "I had trouble processing your contextual request. Could you please try rephrasing what you'd like me to do?"
            await self.session_manager.send_and_track_message(user.phone_number, fallback_response, session)
            
            return {
                "status": "contextual_interaction_error", 
                "error": str(e)
            }
    
    async def _process_entity_updates(self, session: ConversationSession, entity_updates: List[Dict]) -> None:
        """Process entity updates from contextual interactions."""
        try:
            workflow_state = session.workflow_state or {}
            extracted_entities = workflow_state.get('extracted_entities', [])
            
            for entity_update in entity_updates:
                action = entity_update.get('action', 'add')
                
                if action == 'add':
                    # Add new entity to the list
                    new_entity = {
                        'product_name': entity_update.get('product_name', ''),
                        'quantity': entity_update.get('quantity', ''),
                        'specifications': entity_update.get('specifications', ''),
                        'preferred_brand': entity_update.get('preferred_brand', ''),
                        'delivery_date': entity_update.get('delivery_date', '')
                    }
                    # Remove empty values
                    new_entity = {k: v for k, v in new_entity.items() if v}
                    if new_entity:
                        extracted_entities.append(new_entity)
                        logger.info(f"Added new entity: {new_entity.get('product_name', 'Unknown')}")
                        
                elif action == 'update':
                    # Update existing entities that match product name
                    product_name = entity_update.get('product_name', '')
                    for entity in extracted_entities:
                        if entity.get('product_name', '').lower() == product_name.lower():
                            # Update fields that are provided
                            for field in ['quantity', 'specifications', 'preferred_brand', 'delivery_date']:
                                if entity_update.get(field):
                                    entity[field] = entity_update[field]
                            logger.info(f"Updated entity: {product_name}")
                            break
                            
                elif action == 'remove':
                    # Remove entities that match product name
                    product_name = entity_update.get('product_name', '')
                    extracted_entities = [e for e in extracted_entities if e.get('product_name', '').lower() != product_name.lower()]
                    logger.info(f"Removed entity: {product_name}")
            
            # Update session with modified entities
            workflow_state['extracted_entities'] = extracted_entities
            session.workflow_state = workflow_state
            
        except Exception as e:
            logger.error(f"Error processing entity updates: {e}")
    
    async def _rollback_to_stage(self, session: ConversationSession, target_stage: str) -> None:
        """Rollback session to a previous stage."""
        try:
            workflow_state = session.workflow_state or {}
            
            # Clear stage-specific data based on target
            if target_stage == 'collecting':
                # Clear confirmation states
                workflow_state.pop('pending_combined_rfq', None)
                workflow_state.pop('pending_rfq', None)
                workflow_state.pop('pending_optional_rfq', None)
                workflow_state['stage'] = 'collecting'
                
            elif target_stage == 'entity_collection':
                # Clear all entities and start over
                workflow_state['extracted_entities'] = []
                workflow_state['stage'] = 'collecting'
                
            session.workflow_state = workflow_state
            logger.info(f"Rolled back session to stage: {target_stage}")
            
        except Exception as e:
            logger.error(f"Error rolling back to stage {target_stage}: {e}")
    
    async def _clear_session_fields(self, session: ConversationSession, fields_to_clear: List[str]) -> None:
        """Clear specific fields from session."""
        try:
            workflow_state = session.workflow_state or {}
            
            for field in fields_to_clear:
                if field in workflow_state:
                    del workflow_state[field]
                    logger.info(f"Cleared session field: {field}")
                    
            session.workflow_state = workflow_state
            
        except Exception as e:
            logger.error(f"Error clearing session fields {fields_to_clear}: {e}")
    
    async def _restart_workflow(self, session: ConversationSession) -> None:
        """Completely restart the workflow by clearing session data."""
        try:
            # Keep basic session info but clear workflow data
            session.workflow_state = {
                'extracted_entities': [],
                'stage': 'collecting',
                'last_activity_at': utc_now().isoformat()
            }
            WorkflowManager.set_workflow_type(session, WorkflowType.general_inquiry, caller='restart_workflow')
            logger.info("Restarted workflow - cleared session data")
            
        except Exception as e:
            logger.error(f"Error restarting workflow: {e}")

    def _update_last_user_message_with_intent(self, session: ConversationSession, intent: str, confidence: float) -> None:
        """Update the last user message in conversation history with intent classification results."""
        try:
            if not session.conversation_history or not isinstance(session.conversation_history, dict):
                return

            messages = session.conversation_history.get('messages', [])
            if not messages:
                return

            # Find the last user message and update it with intent data
            for i in range(len(messages) - 1, -1, -1):  # Iterate backwards
                message = messages[i]
                if message.get("sender") == "user":
                    message["intent"] = intent
                    message["confidence"] = confidence
                    logger.info(f"Updated user message with intent: {intent} (confidence: {confidence})")
                    break

        except Exception as e:
            logger.error(f"Error updating last user message with intent: {e}")

    def _track_meaningful_message_during_auth_flow(self, session: ConversationSession, message_content: str, intent_result: Dict[str, Any]) -> None:
        """Track the last meaningful message for processing after auth/registration completes."""
        try:
            if not intent_result:
                return

            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)

            # Check if this is a meaningful message that should be processed after auth/registration
            meaningful_intents = [
                "buy_something", "sell_something", "general_inquiry",
                "modification_request", "reference_request", "rfq_status_check"
            ]

            # Skip OTP-like messages and auth/registration flow responses
            if self._is_auth_flow_response(message_content, intent):
                logger.info(f"Skipping auth/registration flow response: '{message_content[:50]}...' with intent: {intent}")
                return

            # Skip account selection responses during role switch
            if session.workflow_state and session.workflow_state.get("pending_role_switch"):
                logger.info(f"Skipping account selection response during role switch: '{message_content[:50]}...'")
                return

            # Always replace with the most recent meaningful message
            if intent in meaningful_intents and confidence > 50:
                session.workflow_state = session.workflow_state or {}
                session.workflow_state["last_meaningful_message"] = message_content
                session.workflow_state["last_meaningful_intent_result"] = intent_result
                logger.info(f"Tracked meaningful message: '{message_content[:50]}...' with intent: {intent} (confidence: {confidence}%)")

        except Exception as e:
            logger.error(f"Error tracking meaningful message: {e}")

    def _is_auth_flow_response(self, message_content: str, intent: str) -> bool:
        """Check if this message is an auth/registration flow response that shouldn't be processed as business intent."""
        try:
            message_lower = message_content.lower().strip()

            # OTP patterns (4-6 digits, possibly with spaces)
            import re
            if re.match(r'^\s*\d{4,6}\s*$', message_content.strip()):
                return True

            # Confirmation responses
            if message_lower in ["yes", "y", "no", "n", "confirm", "correct", "ok", "restart", "wrong", "incorrect"]:
                return True

            # Email addresses (during email confirmation)
            if "@" in message_content and "." in message_content:
                return True

            # Resend requests
            if message_lower in ["resend", "send again", "retry"]:
                return True

            # If classified as register_account but looks like OTP, it's probably misclassified
            if intent == "register_account" and re.search(r'\d{4,6}', message_content):
                return True

            return False

        except Exception as e:
            logger.error(f"Error checking auth flow response: {e}")
            return False

    def _get_meaningful_message_after_auth(self, session: ConversationSession, current_message: str, current_intent_result: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Get the most meaningful message to process after auth/registration completes."""
        try:
            # Check if we have a tracked meaningful message from during the auth/registration flow
            workflow_state = session.workflow_state or {}
            tracked_message = workflow_state.get("last_meaningful_message")
            tracked_intent_result = workflow_state.get("last_meaningful_intent_result")

            if tracked_message and tracked_intent_result:
                logger.info(f"Using tracked meaningful message: '{tracked_message[:50]}...' with intent: {tracked_intent_result.get('intent')}")

                # Clean up the tracked message since we're using it now
                workflow_state.pop("last_meaningful_message", None)
                workflow_state.pop("last_meaningful_intent_result", None)

                return tracked_message, tracked_intent_result
            else:
                # No tracked message - check if current message is an auth flow response
                current_intent = current_intent_result.get('intent', '')
                if self._is_auth_flow_response(current_message, current_intent):
                    logger.info(f"No meaningful message tracked and current message is auth flow response. Creating default general inquiry.")
                    # Return a default general inquiry since user completed auth/registration without meaningful business request
                    default_message = "Hello, how can I help you today?"
                    default_intent_result = {
                        "intent": "general_inquiry",
                        "confidence": 75,
                        "context_analysis": {"conversation_stage": "post_auth_default"}
                    }
                    return default_message, default_intent_result
                else:
                    # Current message is meaningful, use it
                    logger.info(f"No tracked meaningful message found, using current message: '{current_message[:50]}...'")
                    return current_message, current_intent_result

        except Exception as e:
            logger.error(f"Error getting meaningful message after auth: {e}")
            # Fallback to current message
            return current_message, current_intent_result