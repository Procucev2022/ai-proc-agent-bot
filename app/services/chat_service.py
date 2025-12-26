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

Performance Notes:
- ChatService is instantiated per request (by design) to avoid DB connection leaks
- Heavy services use singleton patterns (InteractionLogger, LocationService)
- SessionManagementService is instantiated per request but uses Redis for state
"""

import logging
from typing import Dict, Any, List
import json
import asyncio

from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService
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
from app.services.faq_service import FAQService
from app.tools.confirmation_tool import ConfirmationTool
from app.services.confirmation_service import ConfirmationService
from app.services.workflow_manager import WorkflowManager, WorkflowStage, PendingFlag
from app.services.message_queue_service import MessageQueueService
from app.redis_db import get_redis_service
from app.database import SessionLocal, DatabaseManager, get_db_session, get_db_session_context
from app.models import ConversationSession, WorkflowType, ConversationOutcome
from app.schemas.user import User
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ChatService:
    """
    Central orchestrator for all user message processing.

    Coordinates authentication, intent classification, workflow routing,
    and response generation for the complete chat experience.
    """

    def __init__(self, db_session: Session = None, message_queue_service: MessageQueueService = None):
        """
        Initialize ChatService with a shared database session.

        Args:
            db_session: Database session (optional for now, will be required in future).
                       When provided, prevents connection leaks by sharing session across services.
                       Should be provided by the caller using get_db_session_context().
            message_queue_service: Optional MessageQueueService for batching.

        Note:
            For production use, always provide db_session to prevent connection leaks.
            Backward compatibility: If not provided, services will create their own sessions
            (may cause connection leaks at high traffic).
        """
        self.db_session = db_session
        self.message_queue_service = message_queue_service

        # Use message_queue_service if provided, otherwise use WhatsAppService directly
        if message_queue_service:
            # When message queue is available, it wraps WhatsApp functionality
            self.whatsapp_service = message_queue_service
        else:
            # Fallback to direct WhatsApp service (for non-queued scenarios)
            from app.services.whatsapp_service import WhatsAppService
            self.whatsapp_service = WhatsAppService()

        # Lazy initialization for services - only create when first accessed
        self._intent_service = None
        self._entity_service = None
        self._openai_service = None
        self._response_helpers = None

        # Lazy initialization for handlers
        self._confirmation_service = None
        self._authentication_service = None
        self._registration_service = None
        self._exit_service = None
        self._cancel_service = None
        self._faq_service = None
        self._confirmation_handler = None
        self._intent_switch_handler = None
        self._products_array_handler = None
        self._purchase_intent_handler = None
        self._attachment_decision_handler = None
        self._image_processor = None
        self._seller_service = None
        self._rfq_status_service = None
        self._format_modification_handler = None

        # Initialize only lightweight services that need database sessions
        self.chat_summary_service = ChatSummaryService(db_session=db_session)
        self.daily_summary_service = DailySummaryService()
        self.db_manager = DatabaseManager(session=db_session)
        self.vendor_service = VendorService(db_session=db_session)
        self.rfq_service = RFQService()
        self.rfq_background_service = RFQBackgroundService(db_session=db_session)

        # Initialize session manager (lightweight, needed early)
        self.session_manager = SessionManagementService(
            self.db_manager, self.whatsapp_service,
            self.chat_summary_service, self.daily_summary_service
        )

        self.settings = get_settings()


    # Lazy-loaded service properties
    @property
    def intent_service(self):
        """Lazy-load IntentService only when needed."""
        if self._intent_service is None:
            self._intent_service = IntentService()
        return self._intent_service

    @property
    def entity_service(self):
        """Lazy-load EntityService only when needed."""
        if self._entity_service is None:
            self._entity_service = EntityService()
        return self._entity_service

    @property
    def openai_service(self):
        """Lazy-load OpenAIService only when needed."""
        if self._openai_service is None:
            self._openai_service = OpenAIService()
        return self._openai_service

    @property
    def response_helpers(self):
        """Lazy-load ResponseHelpers only when needed."""
        if self._response_helpers is None:
            self._response_helpers = ResponseHelpers(self.openai_service)
        return self._response_helpers

    @property
    def confirmation_service(self):
        """Lazy-load ConfirmationService only when needed."""
        if self._confirmation_service is None:
            from app.tools.confirmation_tool import ConfirmationTool
            from app.services.confirmation_service import ConfirmationService
            confirmation_tool = ConfirmationTool(self.openai_service)
            self._confirmation_service = ConfirmationService(confirmation_tool)
        return self._confirmation_service

    @property
    def authentication_service(self):
        """Lazy-load AuthenticationService only when needed."""
        if self._authentication_service is None:
            from app.services.authentication_service import AuthenticationService
            self._authentication_service = AuthenticationService(
                self.whatsapp_service, self.openai_service, self.response_helpers, self.session_manager
            )
        return self._authentication_service

    @property
    def registration_service(self):
        """Lazy-load RegistrationService only when needed."""
        if self._registration_service is None:
            from app.services.registration_service import RegistrationService
            self._registration_service = RegistrationService(
                self.whatsapp_service, self.openai_service, self.entity_service,
                self.response_helpers, self.confirmation_service, self.session_manager
            )
        return self._registration_service

    @property
    def exit_service(self):
        """Lazy-load ExitService only when needed."""
        if self._exit_service is None:
            from app.services.exit_service import ExitService
            self._exit_service = ExitService(
                self.whatsapp_service, self.authentication_service, self.session_manager, self.db_manager
            )
        return self._exit_service

    @property
    def cancel_service(self):
        """Lazy-load CancelService only when needed."""
        if self._cancel_service is None:
            from app.services.cancel_service import CancelService
            self._cancel_service = CancelService(
                self.whatsapp_service, self.session_manager, self.db_manager
            )
        return self._cancel_service

    @property
    def faq_service(self):
        """Lazy-load FAQService only when needed."""
        if self._faq_service is None:
            from app.services.faq_service import FAQService
            self._faq_service = FAQService()
        return self._faq_service

    @property
    def confirmation_handler(self):
        """Lazy-load ConfirmationHandler only when needed."""
        if self._confirmation_handler is None:
            from app.services.handlers.confirmation_handler import ConfirmationHandler
            self._confirmation_handler = ConfirmationHandler(
                self.whatsapp_service, self.response_helpers
            )
        return self._confirmation_handler

    @property
    def intent_switch_handler(self):
        """Lazy-load IntentSwitchHandler only when needed."""
        if self._intent_switch_handler is None:
            from app.services.handlers.intent_switch_handler import IntentSwitchHandler
            self._intent_switch_handler = IntentSwitchHandler(
                self.whatsapp_service, self.response_helpers
            )
        return self._intent_switch_handler

    @property
    def products_array_handler(self):
        """Lazy-load ProductsArrayHandler only when needed."""
        if self._products_array_handler is None:
            from app.services.handlers.products_array_handler import ProductsArrayHandler
            self._products_array_handler = ProductsArrayHandler(
                self.whatsapp_service, self.openai_service,
                self.response_helpers, self.session_manager
            )
        return self._products_array_handler

    @property
    def format_modification_handler(self):
        """Lazy-load FormatModificationHandler only when needed."""
        if self._format_modification_handler is None:
            from app.services.handlers.format_modification_handler import FormatModificationHandler
            self._format_modification_handler = FormatModificationHandler(
                self.whatsapp_service
            )
        return self._format_modification_handler

    @property
    def purchase_intent_handler(self):
        """Lazy-load PurchaseIntentHandler only when needed."""
        if self._purchase_intent_handler is None:
            from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
            # Initialize WITHOUT circular dependencies first
            self._purchase_intent_handler = PurchaseIntentHandler(
                self.whatsapp_service, self.response_helpers, self.entity_service,
                self.chat_summary_service, self.products_array_handler, self.session_manager,
                None, None  # Pass None to avoid circular dependency
            )
            # Set handlers AFTER initialization to break circular dependency
            self._purchase_intent_handler.confirmation_handler = self.confirmation_handler
            self._purchase_intent_handler.attachment_decision_handler = self.attachment_decision_handler
        return self._purchase_intent_handler

    @property
    def attachment_decision_handler(self):
        """Lazy-load AttachmentDecisionHandler only when needed."""
        if self._attachment_decision_handler is None:
            from app.services.handlers.attachment_decision_handler import AttachmentDecisionHandler
            # Initialize WITHOUT circular dependency first
            self._attachment_decision_handler = AttachmentDecisionHandler(
                self.whatsapp_service, self.response_helpers,
                None, self.session_manager  # Pass None to avoid circular dependency
            )
            # Set handler AFTER initialization to break circular dependency
            self._attachment_decision_handler.purchase_intent_handler = self.purchase_intent_handler
        return self._attachment_decision_handler

    @property
    def image_processor(self):
        """Lazy-load ImageMessageProcessor only when needed."""
        if self._image_processor is None:
            from app.services.processors.image_message_processor import ImageMessageProcessor
            self._image_processor = ImageMessageProcessor(self.whatsapp_service, self.response_helpers)
        return self._image_processor

    @property
    def seller_service(self):
        """Lazy-load SellerService only when needed."""
        if self._seller_service is None:
            from app.services.seller_service import SellerService
            self._seller_service = SellerService(
                whatsapp_service=self.whatsapp_service,
                session_manager=self.session_manager,
                db_session=self.db_session
            )
        return self._seller_service

    @property
    def rfq_status_service(self):
        """Lazy-load RFQStatusService only when needed."""
        if self._rfq_status_service is None:
            from app.services.rfq_status_service import RFQStatusService
            self._rfq_status_service = RFQStatusService(
                whatsapp_service=self.whatsapp_service,
                session_manager=self.session_manager,
                db_session=self.db_session
            )
        return self._rfq_status_service

    async def cleanup(self):
        """Cleanup resources - close OpenAI client to prevent connection leaks."""
        try:
            if self._openai_service is not None:
                await self._openai_service.close()
                logger.debug("ChatService: OpenAI service closed")
        except Exception as e:
            logger.warning(f"ChatService: Error during cleanup: {e}")

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

    async def _activate_sectioned_rfq(self, user: User, session: ConversationSession,
                                     message: str = "") -> Dict[str, Any]:
        """
        Helper method to activate sectioned RFQ workflow.

        This can be called manually for testing or triggered by configuration.
        Once activated, the sectioned RFQ handler will take over the RFQ creation process.

        Args:
            user: User object
            session: Conversation session
            message: Initial message (if any)

        Returns:
            Dict with response data
        """
        logger.info(f"[SECTIONED_RFQ] Activating sectioned RFQ workflow for user {user.phone_number}")

        # Initialize sectioned RFQ workflow
        WorkflowManager.initialize_sectioned_rfq(session)
        WorkflowManager.set_sectioned_rfq_section(session, "date_location")

        # Mark as active
        if "sectioned_rfq" in session.workflow_state:
            session.workflow_state["sectioned_rfq"]["active"] = True

        # Set workflow type
        WorkflowManager.set_workflow_type(session, WorkflowType.rfq_creation, caller='activate_sectioned_rfq')

        await self.session_manager.save_session(session, persist_to_db=False)

        # Send initial prompt for date/location section
        initial_msg = ("Let's start with the Delivery Date and Delivery Pincode.\n\n"
                      "Note: If you are expecting the delivery at different locations or on different dates, "
                      "we request you create separate RFQs.")

        await self.whatsapp_service.send_message(user.phone_number, initial_msg, session_id=session)

        return {"status": "sectioned_rfq_activated"}

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
            session = await self.session_manager.get_conversation_context(user_phone)

            # DISABLED: Session expiry now handled by InactivityTimeoutService (30-minute proactive timeout)
            # The timeout service actively monitors user activity and cleans up timed-out sessions
            # Redis TTL (40 min) serves as a safety net for any sessions that bypass timeout monitoring
            # session = await self.session_manager.handle_session_expiry_check(user_phone, session)

            # Track user message in conversation history using extracted service
            # Classify intent for all user messages to enable proper message routing after auth
            message_intent_result = None

            # CRITICAL: Handle RFQ notification buttons BEFORE intent classification
            # These buttons (rfq_interested) should skip intent classification entirely
            if message_type == "interactive" and isinstance(message_content, dict):
                button_id = message_content.get("button_reply", {}).get("id", "")
                if button_id.startswith("rfq_interested"):
                    logger.info(f"RFQ notification button detected: {button_id} - skipping intent classification")
                    from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler

                    # Parse button_id to extract rfq_id and seller_id
                    # Format: rfq_interested_{rfq_id}_{seller_id}
                    # Note: RFQ ID may contain underscores (e.g., RFQ_TEST_001), seller_id is UUID with dashes
                    # Split from right with maxsplit=1 to get seller_id, rest is rfq_id
                    prefix = "rfq_interested_"
                    suffix = button_id[len(prefix):]
                    parts = suffix.rsplit("_", 1)  # Split from right, max 1 split
                    rfq_id = parts[0] if parts else None
                    seller_id = parts[1] if len(parts) > 1 else None

                    # Track button click in conversation history before handling
                    self.session_manager.add_message_to_history(
                        session, "user", f"[Button: I'm Interested] RFQ: {rfq_id} SellerID:{seller_id}", "interactive"
                    )

                    # Handle I'm Interested - use the handler directly
                    handler = SellerRFQInterestHandler(
                        whatsapp_service=self.whatsapp_service,
                        authentication_service=self.authentication_service,
                        session_manager=self.session_manager,
                        otp_service=self.authentication_service.otp_service if self.authentication_service else None
                    )
                    return await handler.handle_rfq_interest_click(user_phone, rfq_id, seller_id, session)

                # Handle "Check Details" button - intermediate step after authentication
                if button_id.startswith("rfq_check_details"):
                    logger.info(f"RFQ Check Details button detected: {button_id}")
                    from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler

                    # Parse button_id: rfq_check_details_{rfq_id}_{seller_id}
                    prefix = "rfq_check_details_"
                    suffix = button_id[len(prefix):]
                    parts = suffix.rsplit("_", 1)
                    rfq_id = parts[0] if parts else None
                    seller_id = parts[1] if len(parts) > 1 else None

                    # Track button click in conversation history before handling
                    self.session_manager.add_message_to_history(
                        session, "user", f"[Button: Check Details] RFQ: {rfq_id}", "interactive"
                    )

                    handler = SellerRFQInterestHandler(
                        whatsapp_service=self.whatsapp_service,
                        authentication_service=self.authentication_service,
                        session_manager=self.session_manager,
                        otp_service=self.authentication_service.otp_service if self.authentication_service else None
                    )
                    return await handler.handle_check_details_click(user_phone, rfq_id, seller_id, session)

                # Handle "Request RFQ" button - intermediate step after authentication
                if button_id.startswith("rfq_request"):
                    logger.info(f"RFQ Request button detected: {button_id}")
                    from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler

                    # Parse button_id: rfq_request_{rfq_id}_{seller_id}
                    prefix = "rfq_request_"
                    suffix = button_id[len(prefix):]
                    parts = suffix.rsplit("_", 1)
                    rfq_id = parts[0] if parts else None
                    seller_id = parts[1] if len(parts) > 1 else None

                    # Track button click in conversation history before handling
                    self.session_manager.add_message_to_history(
                        session, "user", f"[Button: Request RFQ] RFQ: {rfq_id}", "interactive"
                    )

                    handler = SellerRFQInterestHandler(
                        whatsapp_service=self.whatsapp_service,
                        authentication_service=self.authentication_service,
                        session_manager=self.session_manager,
                        otp_service=self.authentication_service.otp_service if self.authentication_service else None
                    )
                    return await handler.handle_request_rfq_click(user_phone, rfq_id, seller_id, session)

                if button_id.startswith("confirm_exit") or button_id.startswith("decline_exit"):
                    logger.info(f"Exit buttons clicked in auth workflow  - handling immediately to prevent loop")
                    exit_result = await self.exit_service.handle_exit_intent(user_phone, session,message=message_content)
                    return exit_result
                if button_id.startswith("confirm_cancel") or button_id.startswith("'decline_cancel") or button_id.startswith("cancel_no_credits"):
                    logger.info(f"Cancel buttons clicked in auth workflow  - handling immediately to prevent loop")
                    cancel_result = await self.cancel_service.handle_cancel_intent(user_phone, session, message_content)
                    await self.session_manager.save_session(session, session.workflow_type)
                    return cancel_result

            # CRITICAL: Handle seller_rfq_intimation workflow BEFORE intent classification
            # This workflow has stages: switch_prompt, otp - both need dedicated handling
            workflow_type_value = session.workflow_type.value if hasattr(session.workflow_type, 'value') else str(session.workflow_type) if session.workflow_type else None
            if workflow_type_value == "seller_rfq_intimation":
                auth_stage = session.workflow_state.get("auth_stage") if session.workflow_state else None
                logger.debug(f"seller_rfq_intimation workflow detected, auth_stage={auth_stage}")

                from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler

                handler = SellerRFQInterestHandler(
                    whatsapp_service=self.whatsapp_service,
                    authentication_service=self.authentication_service,
                    session_manager=self.session_manager,
                    otp_service=self.authentication_service.otp_service if self.authentication_service else None
                )

                if auth_stage == "switch_prompt":
                    # Handle user's response to account switch prompt
                    logger.info(f"Handling switch response for seller_rfq_intimation")
                    return await handler.handle_switch_response(user_phone, session, message_content)

                elif auth_stage == "otp":
                    # Handle OTP input for seller authentication
                    logger.info(f"Handling OTP for seller_rfq_intimation, OTP input: {message_content}")
                    # Use the OTP service to validate
                    if self.authentication_service and self.authentication_service.otp_service:
                        otp_result = await self.authentication_service.otp_service.validate_otp(
                            user_phone, session, message_content
                        )
                        logger.info(f"OTP verification result: {otp_result}")

                        if otp_result.get("status") == "otp_valid":
                            # OTP verified - now authenticate as the seller and send portal link
                            target_seller_email = session.workflow_state.get("target_seller_email")
                            target_seller_id = session.workflow_state.get("target_seller_id")
                            target_seller_user = session.workflow_state.get("target_seller_user")
                            logger.info(f"OTP verified, authenticating as seller: {target_seller_email}, seller_id: {target_seller_id}")

                            # Authenticate the user as the seller account
                            if self.authentication_service and target_seller_user:
                                auth_result = await self.authentication_service.store_user_session_with_email(
                                    user_phone, [target_seller_user], target_seller_email
                                )
                                logger.debug(f"Seller authentication result: {auth_result}")

                            # Send portal link after successful auth
                            return await handler.handle_otp_validated(user_phone, session)
                        elif otp_result.get("status") == "max_otp_exceeded":
                            # Max OTP attempts exceeded - clear workflow
                            session.workflow_type = None
                            session.workflow_state = {}
                            await self.session_manager.save_session(session)
                            return {"status": "max_otp_exceeded", "message": "Maximum OTP attempts exceeded"}
                        else:
                            # OTP verification failed - keep in OTP stage for retry
                            return {"status": "otp_invalid", "retry": True}
                    else:
                        logger.error("No OTP service available for seller_rfq_intimation OTP verification")
                        await self.whatsapp_service.send_message(user_phone, "Sorry, there was an error verifying your code. Please try again.")
                        return {"status": "error", "message": "OTP service not available"}

            # Classify intent once for all message routing and tracking
            try:
                # Sanitize message content for intent classification - strip base64 data to avoid token limits
                classification_content = message_content
                if message_type == "excel_upload" and isinstance(message_content, dict):
                    # Create sanitized copy without base64 data for classification
                    classification_content = f"Excel file upload: {message_content.get('document', {}).get('filename', 'unknown')}"

                conversation_context = await ChatServiceHelpers.build_conversation_context(session, classification_content)
                # Now using async OpenAI service
                message_intent_result = await self.intent_service.classify_intent(classification_content, conversation_context)
                intent = message_intent_result.get('intent')
                confidence = message_intent_result.get('confidence', 0)

                # For history, also use sanitized content for excel uploads
                history_content = classification_content if message_type == "excel_upload" else message_content
                self.session_manager.add_message_to_history(session, "user", history_content, message_type, intent, confidence)
            except Exception as e:
                # If intent classification fails, still track the message without intent
                logger.warning(f"Intent classification failed during message tracking: {e}")
                self.session_manager.add_message_to_history(session, "user", message_content, message_type)
                message_intent_result = {"intent": "greeting", "confidence": 0}

            # Track meaningful messages during auth/registration flows for later processing
            self._track_meaningful_message_during_auth_flow(session, message_intent_result.get('relevant_message') or message_content, message_intent_result)

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



            # CRITICAL: Handle exit and cancel intents BEFORE any workflow routing
            # This ensures users can always exit regardless of workflow state (especially auth loops)
            # Only applies when user is NOT authenticated (auth flow)
            # For authenticated users, exit is handled later with optional field logic
            workflow_type_str = str(session.workflow_type).lower() if session.workflow_type else None
            is_in_auth_workflow = workflow_type_str in ["workflowtype.authentication", "authentication"]

            if intent == "exit_system" and confidence > 50 and is_in_auth_workflow:
                logger.info(f"Exit intent detected in auth workflow with {confidence}% confidence - handling immediately to prevent loop")
                exit_result = await self.exit_service.handle_exit_intent(user_phone, session, message=message_content)
                return exit_result

            if intent == "cancel_workflow" and confidence > 50 and is_in_auth_workflow:
                logger.info(f"Cancel workflow intent detected in auth workflow with {confidence}% confidence - handling immediately to prevent loop")
                cancel_result = await self.cancel_service.handle_cancel_intent(user_phone, session, message_content)
                await self.session_manager.save_session(session, session.workflow_type)
                return cancel_result

            # Handle irrelevant messages using reusable function
            if intent in ('general_inquiry','greeting','support') or message_intent_result.get("irrelevant_message"):
                await self.handle_irrelevant_message_flow(user_phone, message_intent_result, session)


            auth_result = await self.authentication_orchestrator_flow(user_phone,message_intent_result.get('relevant_message') or message_content,session, message_intent_result)


            # Check if authentication is still in progress
            if isinstance(auth_result, dict):
                auth_status = auth_result.get("status")
                
                # Authentication/registration flow statuses - stay in auth loop
                auth_in_progress_statuses = [
                    "clarification_sent", "general_inquiry_handled", "greeting_handled", "fallback_handled","filtered_seller_profiles_shown",
                    "redirected_to_registration", "redirected_to_buyer_registration", "redirected_to_seller_registration",
                    "redirected_to_email_confirmation", "otp_sent","dual_intent_clarification_sent","general_inquiry_already_handled",
                    "email_selection_requested", "registration_initiated", "data_collection_in_progress",
                    "awaiting_confirmation", "registration_restarted", "otp_validated", "otp_invalid", "otp_format_invalid",
                    "domain_approved", "domain_approval_required", "email_confirmation_requested",
                    "auth_reg_switch_choice_presented", "exit_completed", "switch_authentication_started",
                    "role_switch_clarification_requested", "profile_selection_presented",
                    "buyer_profile_selection_presented", "seller_profile_selection_presented",
                    "rfq_status_profile_selection_presented", "ambiguous_profile_selection_presented",
                    "registration_choice_presented", "registration_type_choice_presented",
                    "profile_selection_retry_presented", "role_menu_presented",
                    "redirected_to_buyer_registration", "redirected_to_seller_registration",
                    "intent_mismatch_handled", "intent_mismatch_retry_sent", "new_user_registration_presented",
                    "buyer_options_presented", "seller_options_presented", "single_buyer_profile_selection_presented", "profile_selection_sent",
                    "registration_type_clarification_sent", "verification_failed","filtered_buyer_profiles_shown","max_otp_exceeded", "buyer_no_accounts_message_sent","user_already_exists",
                    "exit_confirmation_pending","no_workflow_to_exit"
                ]
                
                if auth_status in auth_in_progress_statuses:
                    # Special handling for buyer/seller_options_presented: preserve meaningful message
                    if auth_status in ["buyer_options_presented", "seller_options_presented"]:
                        # Preserve meaningful message before saving - user will respond to options next
                        last_meaningful = session.workflow_state.get("last_meaningful_message")
                        last_meaningful_intent = session.workflow_state.get("last_meaningful_intent_result")

                        if last_meaningful and last_meaningful_intent:
                            # Clear workflow_type to indicate auth is complete (just waiting for button response)
                            session.workflow_type = None
                            # Preserve only the meaningful message fields for next interaction
                            session.workflow_state = {
                                "last_meaningful_message": last_meaningful,
                                "last_meaningful_intent_result": last_meaningful_intent
                            }
                            logger.debug(f"Preserved meaningful message for post-auth interaction: '{str(last_meaningful)[:50]}...'")
                            await self.session_manager.save_session(session, None)
                        else:
                            # No meaningful message to preserve, save as normal
                            current_workflow = WorkflowManager.get_workflow_type(session)
                            workflow_type = WorkflowType.registration if current_workflow == WorkflowType.registration else WorkflowType.authentication
                            await self.session_manager.save_session(session, workflow_type)
                    else:

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
                        
                    return auth_result
                elif auth_status in ["redirected_to_support", "redirect_to_support", "user_exited"]:
                    # Max OTP retries exceeded, user exited, or other support-requiring scenario
                    # Check if exit has already been completed to avoid duplicate calls
                    if auth_result.get("exit_completed"):
                        # Exit already completed - session already cleaned up, no need to save
                        return auth_result
                    else:
                        # Exit service handles all session persistence (DB + Redis cleanup)
                        exit_result = await self.exit_service.handle_exit_intent(user_phone, session)
                        return exit_result

                elif auth_status == "registration_completed" and auth_result.get("user_type") == "buyer":
                    # Registration completed - check if this is truly complete or needs further processing
                    registration_flow_complete = auth_result.get("registration_flow_complete", False)
                    user_type = auth_result.get("user_type", "unknown")
                    approved = auth_result.get("approved", False)

                    if registration_flow_complete:
                        # Registration is completely done - no further processing needed
                        # Clear workflow completely to prevent any further processing
                        session.workflow_type = None
                        session.workflow_state = {}
                        await self.session_manager.save_session(session, None)
                        return {"status": "registration_completed", "message": "Registration successful", "flow_terminated": True}
                    
                    # Legacy handling for cases where registration_flow_complete is not set
                    user = await self.authentication_service.validate_token(user_phone)
                    if user:
                        # Refresh user cache after successful registration to include the new account
                        try:
                            # Clear existing cache first to force fresh API call
                            from app.services.user_cache_service import get_user_cache_service
                            user_cache_service = get_user_cache_service()
                            await user_cache_service.clear_user_data(user_phone)

                            # Make fresh API call to get updated user data including new account
                            auth_response = await self.authentication_service.user_authenticate(
                                user_phone, "refresh_cache_post_registration", session, intent="greeting"
                            )
                            if not auth_response.get("success"):
                                logger.warning(f"Failed to refresh user cache after registration for {user_phone}")
                        except Exception as e:
                            logger.error(f"Error refreshing user cache after registration for {user_phone}: {e}")
                            # Continue with flow even if cache refresh fails
                        user_type = auth_result.get("user_type", "buyer")

                        if user_type == "seller":
                            # Sellers: Registration is complete, don't process the OTP message further
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

                                    # Clear from cache since we've restored it
                                    await user_cache_service.clear_meaningful_message(user_phone)
                                elif not cached_meaningful:
                                    logger.warning(f"No cached meaningful message to restore after registration")

                                message_to_process, intent_to_process = self._get_meaningful_message_after_auth(
                                    session, message_content, message_intent_result
                                )
                                return await self._process_text_message(user, session, message_to_process, intent_to_process)
                            else:
                                # Domain NOT approved buyers: Registration complete, no further processing
                                return {"status": "registration_completed", "message": "Buyer registration successful - awaiting approval"}
                    else:
                        return {"status": "error", "error": "Session not found after registration"}
                elif auth_status in ["authentication_completed", "profile_selected_and_authenticated", "profile_selection_sent","registration_completed"]:
                    # Authentication completed - check user type before processing
                    user_type = auth_result.get("user_type")
                    original_message = auth_result.get("original_message", message_content)
                    original_intent = auth_result.get("original_intent", "greeting")

                    if user_type == "seller":
                        # For sellers, process the meaningful message after authentication
                        # Use original message from profile selection if available
                        message_to_process = original_message if original_message else message_content
                        intent_to_process = {"intent": original_intent, "confidence": 90} if isinstance(original_intent, str) else original_intent
                        
                        # Restore meaningful message from cache if workflow_state was cleared
                        cached_meaningful = await user_cache_service.get_meaningful_message(user_phone)

                        if cached_meaningful and not session.workflow_state.get("last_meaningful_message"):
                            session.workflow_state["last_meaningful_message"] = cached_meaningful["message"]
                            session.workflow_state["last_meaningful_intent_result"] = cached_meaningful["intent_result"]

                            # Clear from cache since we've restored it
                            await user_cache_service.clear_meaningful_message(user_phone)

                            # Use cached message if no original message from profile selection
                            if not original_message:
                                message_to_process, intent_to_process = self._get_meaningful_message_after_auth(
                                    session, message_content, message_intent_result
                                )

                        user = await self.authentication_service.validate_token(user_phone)
                        if user:
                            return await self._process_text_message(user, session, message_to_process, intent_to_process)
                        else:
                            return {"status": "error", "error": "Session not found after seller authentication"}

                    # For buyers, process the meaningful message after authentication
                    # Use original message from profile selection if available
                    message_to_process = original_message if original_message else message_content
                    intent_to_process = {"intent": original_intent, "confidence": 90} if isinstance(original_intent, str) else original_intent
                    
                    # Restore meaningful message from cache if workflow_state was cleared
                    cached_meaningful = await user_cache_service.get_meaningful_message(user_phone)

                    if cached_meaningful and not session.workflow_state.get("last_meaningful_message"):
                        session.workflow_state["last_meaningful_message"] = cached_meaningful["message"]
                        session.workflow_state["last_meaningful_intent_result"] = cached_meaningful["intent_result"]

                        # Clear from cache since we've restored it
                        await user_cache_service.clear_meaningful_message(user_phone)

                        # Use cached message if no original message from profile selection
                        if not original_message:
                            message_to_process, intent_to_process = self._get_meaningful_message_after_auth(
                                session, message_content, message_intent_result
                            )

                    user = await self.authentication_service.validate_token(user_phone)
                    if user:
                        # Ensure user cache is populated after authentication
                        try:
                            from app.services.user_cache_service import get_user_cache_service
                            user_cache_service = get_user_cache_service()
                            cached_data = await user_cache_service.get_user_data(user_phone)

                            if not cached_data:
                                # No cache exists, make API call to populate it
                                auth_response = await self.authentication_service.user_authenticate(
                                    user_phone, "refresh_cache_post_auth", session, intent="greeting"
                                )
                                if not auth_response.get("success"):
                                    logger.warning(f"Failed to populate user cache after authentication for {user_phone}")
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
                    pass
                else:
                    # Invalid user but not registered, handle as general inquiry
                    return await self._process_text_message(auth_result, session, message_content, message_intent_result)
            elif isinstance(auth_result, dict):
                # Handle dict responses that weren't caught above
                auth_status = auth_result.get("status")
                if auth_status == "redirected_to_support" or auth_status == "redirect_to_support" :
                    # Check if exit has already been completed to avoid duplicate calls
                    if auth_result.get("exit_completed"):
                        logger.info(f"Exit already completed in auth flow for {user_phone}, skipping duplicate exit call")
                        # Exit already completed - session already cleaned up, no need to save
                        return auth_result
                    else:
                        logger.info(f"Final redirect to support - calling exit service for {user_phone}")
                        # Exit service handles all session persistence (DB + Redis cleanup)
                        exit_result = await self.exit_service.handle_exit_intent(user_phone, session)
                        return exit_result
                elif auth_status == "verification_required":
                    # Handle verification required status
                    logger.info(f"Verification required for {user_phone}")
                    
                    # Send verification message to user
                    redirect_info = auth_result.get("redirect_info", {})
                    verification_message = redirect_info.get("message", "Email verification is required to continue.")
                    
                    # await self.whatsapp_service.send_message(user_phone, verification_message)
                    
                    await self.session_manager.save_session(session, WorkflowType.authentication)
                    return auth_result
                elif auth_status == "verification_failed":
                    # Handle verification failed status
                    logger.debug(f"Verification failed for {user_phone}")
                    
                    # Send verification failed message to user
                    redirect_info = auth_result.get("redirect_info", {})
                    pending_message = (
                        "*Registration received—thank you!*\n\n"
                        "We’re reviewing your details to ensure everything is set up perfectly for your onboarding. "
                        "Our team will get in touch shortly to complete the process, and once verified, "
                        "you’ll be able to access your account and start raising RFQs.\n\n"
                        "Thank you for choosing Procucev!"
                    )

                    verification_message = redirect_info.get("message", pending_message)

                    await self.whatsapp_service.send_message(user_phone, verification_message, session_id=session)

                    await self.session_manager.save_session(session, WorkflowType.authentication)

                    await self.exit_service.handle_exit_intent(user_phone, session,show_message=False)

                    return auth_result
                else:
                    logger.error(f"Unexpected auth_result dict with status: {auth_status}")
                    return {"status": "error", "error": "Authentication failed"}
            else:
                logger.error(f"Unexpected auth_result type: {type(auth_result)}")
                return {"status": "error", "error": "Authentication failed"}

            # Only proceed to main flow if user is properly authenticated
            user = auth_result
            if message_type == "text" and (message_intent_result.get('relevant_message') or message_content):
                result = await self._process_text_message(user, session, message_intent_result.get('relevant_message') or message_content, message_intent_result)
            elif message_type == "interactive":
                result = await self._process_interactive_message(user, session, message_content)
            elif message_type == "excel_upload":
                result = await self._process_excel_upload(user, session, message_content)
                # Save session immediately after Excel processing to persist the excel_file_processed flag
                await self.session_manager.save_session(session, WorkflowType.rfq_creation)
            elif message_type == "image" or message_type == "document":
                result = await self.image_processor.process_image_message(user, session, message_content)
                await self.session_manager.save_session(session, WorkflowType.rfq_creation)
            else:
                result = {"status": "error", "error": f"Unknown message type: {message_type}"}

            # # Log OpenAI call summary for performance monitoring
            # call_summary = self.openai_service.get_call_summary(user_phone)
            # if call_summary:
            #     total_calls = sum(call_summary.values())
            #     call_breakdown = ", ".join([f"{call_type}: {count}" for call_type, count in call_summary.items()])
            #     logger.info(f"OpenAI calls for {user_phone}: {total_calls} total ({call_breakdown})")

            return result

        except Exception as e:
            logger.error(f"Critical error in process_message for {user_phone}: {e}")
            
            # Use technical failure handler for critical errors
            from app.utils.technical_failure_handler import handle_technical_failure
            await handle_technical_failure(
                user_phone=user_phone,
                error_message=f"Critical processing error: {str(e)}",
                error_type="Critical System Error"
            )
            
            return {"status": "technical_failure", "error": str(e)}

    async def handle_irrelevant_message_flow(self, user_phone: str, message_intent_result: Dict[str, Any],
                                             session: ConversationSession) -> None:
        """Handle irrelevant message flow and cache response."""
        try:
            intent = message_intent_result.get('intent')
            relevant_msg = message_intent_result.get('relevant_message')
            irrelevant_msg = message_intent_result.get('irrelevant_message')
            conversation_history = session.conversation_history or {"openai_messages": [], "metadata": []}

            # Handle greeting intent - response generation only, no FAQ search
            if intent == 'greeting':
                query_message = irrelevant_msg or relevant_msg

                if query_message:
                    logger.debug(f"Processing greeting: {query_message}")

                    context_data = {
                        'intent': intent,
                        'relevant_message': relevant_msg or '',
                        'irrelevant_message': irrelevant_msg or query_message,
                        'workflow_type': str(session.workflow_type) if session.workflow_type else 'unknown',
                        'workflow_state': session.workflow_state or {},
                        'available_workflow_types': [wf.value for wf in WorkflowType],
                        'conversation_history': conversation_history
                    }

                    # Generate response directly without FAQ search
                    response = await self._generate_llm_response(user_phone, query_message, context_data)
                    logger.info(f"Generated greeting response: {response}")

                    if response:
                        await self._cache_irrelevant_response(user_phone, response)

            # Handle general inquiry intent (both relevant and irrelevant)
            elif intent in ( 'general_inquiry', 'support'):
                # When both messages exist, prioritize irrelevant message for general inquiry
                query_message = irrelevant_msg if irrelevant_msg else relevant_msg

                if query_message:
                    logger.debug(f"Processing general inquiry: {query_message}")
                    if relevant_msg and irrelevant_msg:
                        logger.debug(f"Both messages present - using irrelevant message for general inquiry")

                    # Search FAQ first, then fallback to LLM
                    context_data = {
                        'intent':intent,
                        'relevant_message': relevant_msg or '',
                        'irrelevant_message': irrelevant_msg or query_message,
                        'workflow_type': str(session.workflow_type) if session.workflow_type else 'unknown',
                        'workflow_state': session.workflow_state or {},
                        'available_workflow_types': [wf.value for wf in WorkflowType],
                        'conversation_history': conversation_history
                    }


                    response = await self._handle_irrelevant_message(user_phone, query_message, context_data)
                    logger.info(f"Generated response: {response}")

                    # Cache response
                    if response:
                        await self._cache_irrelevant_response(user_phone, response)


            # Handle other irrelevant messages (non-general inquiry)
            elif irrelevant_msg:
                logger.debug(f"Processing irrelevant message: {irrelevant_msg}")
                context_data = {
                    'intent':intent,
                    'relevant_message': relevant_msg or '',
                    'irrelevant_message': irrelevant_msg,
                    'workflow_type': str(session.workflow_type) if session.workflow_type else 'unknown',
                    'workflow_state': session.workflow_state or {},
                    'available_workflow_types': [wf.value for wf in WorkflowType],
                    'conversation_history': conversation_history
                }

                response = await self._handle_irrelevant_message(user_phone, irrelevant_msg, context_data)
                logger.debug(f"Generated irrelevant response: {response}")


                # Cache response
                if response:
                    await self._cache_irrelevant_response(user_phone, response)





        except Exception as e:
            logger.error(f"Error in handle_irrelevant_message_flow: {e}")

    async def _handle_irrelevant_message(self, user_phone: str, message: str, context) -> str:
        """Handle irrelevant messages by searching FAQ first, then generating LLM response."""
        try:
            # First search in FAQ with conversation history
            logger.debug(f"Searching FAQ for: {message}")
            conversation_history = context.get('conversation_history')
            faq_answer = await self.faq_service.get_faq_answer(message, conversation_history)

            if faq_answer and not faq_answer.startswith("I don't have specific information"):
                logger.debug(f"FAQ answer found: {faq_answer}")
                return faq_answer

            logger.debug("No FAQ match found - generating LLM response")
            return await self._generate_llm_response(user_phone, message, context)

        except Exception as e:
            logger.error(f"Error handling irrelevant message: {e}")
            return "I'm having trouble processing that request right now. Please try again or contact support."

    async def _generate_llm_response(self, user_phone: str, message: str, context) -> str:
        """Generate LLM response without FAQ search."""
        try:
            # Extract conversation history for context
            conversation_context = ""
            conversation_history = context.get('conversation_history')
            if conversation_history:
                messages = conversation_history.get('messages', [])
                # Get last 10 messages (5 user + 5 bot pairs)
                recent_messages = messages[-10:] if len(messages) > 10 else messages
                
                if recent_messages:
                    conversation_context = "\n\nRecent conversation context:\n"
                    for msg in recent_messages:
                        role = msg.get('role', 'unknown')
                        content = msg.get('content', '')
                        if role in ['user', 'assistant']:
                            role_label = 'User' if role == 'user' else 'Bot'
                            conversation_context += f"{role_label}: {content}\n"

            prompt_context = {
                'workflow_type': context.get('workflow_type', 'unknown'),
                'workflow_state': str(context.get('workflow_state', {})),
                'relevant_message': context.get('relevant_message', ''),
                'irrelevant_message': context.get('irrelevant_message', message),
                'available_workflow_types': ', '.join(context.get('available_workflow_types', [])),
                'conversation_context': conversation_context
            }

            llm_response = await self.openai_service.generate_response(
                prompt_context,
                None,
                "response_generation/_get_irrelevant_message_response_prompt"
            )

            logger.info(f"LLM response generated: {llm_response}")
            return llm_response

        except Exception as e:
            logger.error(f"Error generating LLM response: {e}")
            return "I'm having trouble processing that request right now. Please try again or contact support."
    async def _cache_irrelevant_response(self, user_phone: str, response: str) -> None:
        """Cache irrelevant response in Redis."""
        try:
            redis_service = get_redis_service()
            cache_key = f"user_cache:{user_phone}"
            cache_data = await redis_service.get(cache_key, as_json=True) or {}
            cache_data["irrelevant_response"] = {
                "user_message": response,
                "timestamp": utc_now().isoformat()
            }
            await redis_service.set(cache_key, cache_data, ex=43200)
            logger.debug(f"Cached response for {user_phone}")
        except Exception as e:
            logger.error(f"Error caching irrelevant response: {e}")

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
            # Check if user needs registration - handle both User object and dict
            if isinstance(user, dict):
                # User is a dict (from validate_token returning dict with verification_required)
                if user.get("verification_required"):
                    return {"status": "verification_required", "verification_info": user.get("verification_info", {})}
                # Convert dict to User object if possible
                try:
                    from app.schemas.user import User as UserSchema
                    user = UserSchema.from_mixed_data(user)
                except Exception as e:
                    logger.error(f"Failed to convert user dict to User object: {e}")
                    return {"status": "error", "error": "Invalid user data"}
            
            # Now check registration status
            if hasattr(user, 'is_registered') and not user.is_registered:
                return await self._handle_registration_workflow(user, message)


            
            # Handle pending role switch confirmation FIRST
            if session.workflow_state.get("pending_role_switch"):
                from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
                auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
                result = await auth_reg_switch.handle_role_switch_response(user, session, message, self.authentication_service)
                await self.session_manager.save_session(session, self._get_workflow_or_default(session))

                # If role switch completed with original message, process it
                if result.get("status") == "authentication_completed" and result.get("original_message"):
                    original_msg = result["original_message"]
                    original_intent = result.get("original_intent_result")

                    # Get fresh user object after switch
                    user = await self.authentication_service.validate_token(user.phone_number)
                    if user:
                        return await self._process_text_message(user, session, original_msg, original_intent)

                return result

            # Handle pending account switch confirmation (same-role switches)
            if session.workflow_state.get("pending_account_switch"):
                from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
                auth_reg_switch = AuthRegistrationIntentSwitch(self.whatsapp_service)
                result = await auth_reg_switch.handle_account_switch_response(user, session, message, self.authentication_service)
                await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                return result

            # Handle seller RFQ intimation workflow (from "I'm Interested" button)
            if session.workflow_type == WorkflowType.seller_rfq_intimation:
                result = await self._handle_seller_rfq_intimation_flow(user, session, message)
                if result:
                    return result

            # Use already-classified intent from message tracking or fallback to classification
            intent_result = message_intent_result
            if not intent_result:
                # Fallback: classify intent if not provided (shouldn't happen with our optimization)
                conversation_context = await ChatServiceHelpers.build_conversation_context(session, message)
                intent_result = await self.intent_service.classify_intent(message, conversation_context)
                logger.warning(f"Had to fallback to intent classification - this shouldn't happen")

            logger.info(f"Intent classification result: {intent_result}")

            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)

            # Update the last user message in conversation history with intent data
            self._update_last_user_message_with_intent(session, intent, confidence)

            # Check workflow state flags early for use in intent handling
            has_excel_confirmation_pending = bool(session.workflow_state.get("awaiting_excel_confirmation"))


            # Handle cancel workflow intent - highest priority after FAQ and exit
            if intent == "cancel_workflow" and confidence > 50:
                logger.info(f"Cancel workflow intent detected with {confidence}% confidence")
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

                # Check if user is in Excel confirmation - handle directly
                if has_excel_confirmation_pending:
                    logger.info("Cancel workflow during Excel confirmation - treating as Excel cancellation")
                    return await self._handle_excel_confirmation_response(user, session, "cancel", intent_result)

                # Trigger cancel confirmation flow (will send buttons or handle confirmation)
                cancel_result = await self.cancel_service.handle_cancel_intent(user_phone, session, message, user)
                await self.session_manager.save_session(session, session.workflow_type)
                return cancel_result

            # Handle exit intent - but check for pending optional fields and Excel confirmation first
            if intent == "exit_system" and confidence > 50:
                # Check if user is in Excel confirmation - handle directly
                if has_excel_confirmation_pending:
                    logger.info("Exit intent during Excel confirmation - treating as Excel cancellation")
                    return await self._handle_excel_confirmation_response(user, session, "exit", intent_result)

                # Check if user has pending optional fields - they might mean "skip" instead of "exit"
                has_pending_optional = bool(
                    session.workflow_state.get("pending_optional_rfq") or
                    session.workflow_state.get("pending_optional_combined_rfq")
                )

                if has_pending_optional:
                    # User said "exit" but has pending optional fields
                    # Interpret as "skip optional fields and proceed to confirmation"
                    logger.info(f"Exit intent detected but user has pending optional fields - treating as 'skip optional' instead")
                    # Route to optional fields handler which will skip and proceed to confirmation
                    result = await self.confirmation_handler.handle_optional_fields_response(user, session, message)
                    await self.session_manager.save_session(session, WorkflowType.rfq_creation)
                    return result
                else:
                    # No pending optional fields - treat as genuine exit with confirmation
                    logger.info(f"Exit intent detected with {confidence}% confidence - requesting exit confirmation")
                    # Use the same phone format as used in authentication flow
                    user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')
                    # Exit service now shows confirmation before exiting
                    exit_result = await self.exit_service.handle_exit_intent(user_phone, session)
                    await self.session_manager.save_session(session, session.workflow_type)
                    return exit_result

            if intent == "support" and confidence > 0.7:
                logger.info(f"Support intent detected with {confidence}% confidence - handling immediately")
                result = await self._handle_support_request(user, message,session)
                return result

            # Handle pending intent switch choices (user responding to "1. Continue or 2. Switch")
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
                        return await self._handle_seller_flow(user, session, message, intent_result)
                    elif new_intent == "general_inquiry":
                        logger.debug("calling from process text message inside new intent")
                        await self.handle_irrelevant_message_flow(user.phone_number, message_intent_result, session)
                    elif new_intent == "greeting":
                        return await self._handle_greeting_inquiry(user, new_message,session ,intent_result)
                    else:
                        return await self._handle_fallback(user, new_message,session)
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

            # Check if sectioned RFQ workflow is active
            has_sectioned_rfq_active = WorkflowManager.is_sectioned_rfq_active(session)

            # has_excel_confirmation_pending already defined above for early use in intent handling
            print(
                f"ChatService: has_existing_data={has_existing_data}, has_incomplete_products={has_incomplete_products}, has_pending_confirmations={has_pending_confirmations}, has_pending_optional={has_pending_optional}, has_pending_attachment_decision={has_pending_attachment_decision}, has_excel_confirmation_pending={has_excel_confirmation_pending}, has_sectioned_rfq_active={has_sectioned_rfq_active}")

            # Debug logging for optional fields state

           



            # Handle cancel confirmation response (when cancel_pending is true)
            cancel_pending = session.workflow_state and session.workflow_state.get("cancel_pending", False)
            if cancel_pending:
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

                # Detect confirmation from the message using confirmation service
                confirmation_result = await self.cancel_service.confirmation_service.parse_confirmation(message)
                is_confirmed = confirmation_result == "yes"
                cancel_result = await self.cancel_service.handle_cancel_confirmation(user_phone, session, is_confirmed,user)

                if cancel_result.get("status") == "cancelled":
                    # Workflow was cancelled, save session and return
                    await self.session_manager.save_session(session, session.workflow_type)
                    return cancel_result
                elif cancel_result.get("status") == "cancelled_aborted":
                    # User declined, save session and continue with normal flow
                    await self.session_manager.save_session(session, session.workflow_type)
                    # Don't return - let the flow continue below to re-ask pending questions
                else:
                    # Any other status, return the result
                    return cancel_result

            # Handle exit confirmation response (when exit_pending is true)
            exit_pending = session.workflow_state and session.workflow_state.get("exit_pending", False)
            if exit_pending:
                user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

                # Detect confirmation from the message using confirmation service
                confirmation_result = await self.cancel_service.confirmation_service.parse_confirmation(message)
                is_confirmed = confirmation_result == "yes"
                exit_result = await self.exit_service.handle_exit_confirmation(user_phone, session, is_confirmed, user)

                return exit_result


            # Handle support requests immediately - even during active workflows
            if intent == "support" and confidence > 0.7:
                result = await self._handle_support_request(user, message,session)
                return result

            # Handle contextual intents with direct response capability
            if intent in ['contextual_reference', 'session_inquiry', 'alternative_request'] and confidence > 60:
                if intent_result.get('should_handle_directly'):
                    logger.debug(f"Contextual intent detected: {intent} with {confidence}% confidence - handling directly")
                    return await self._handle_contextual_interaction(user, session, message, intent_result)
            
            # Handle modification requests immediately if detected with sufficient confidence
            if intent == "modification_request" and confidence > 0.7:
                logger.debug(f"Modification intent detected with {confidence}% confidence - handling immediately")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)

            # Handle pending attachment decisions
            if has_pending_attachment_decision:
                return await self.attachment_decision_handler.handle_attachment_decision(user, session, message,
                                                                                         self._should_use_summary_aware_extraction)

            # Check for intent switch during pending optional/confirmation states BEFORE handling them
            # Note: Excel confirmation should not be interrupted by intent switches
            if (
                    has_pending_optional or has_pending_confirmations) and not has_excel_confirmation_pending and await self.intent_switch_handler.should_handle_intent_switch(
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

            # Handle Excel confirmation responses BEFORE pending confirmations
            if session.workflow_state.get("awaiting_excel_confirmation"):
                result = await self._handle_excel_confirmation_response(user, session, message, intent_result)
                # Excel confirmation handler returns specific statuses, don't continue to normal flow
                return result

            # Handle pending confirmations (user responding to "Would you like to proceed?")
            if has_pending_confirmations:
                result = await self.confirmation_handler.handle_pending_confirmations(user, session, message,
                                                                                      intent_result)

                # Handle session completion if RFQs were created
                if result.get("status") == "multiple_rfqs_created":
                    # APPEND session to database (preserves history from previous RFQs)
                    from app.database import DatabaseManager
                    db_manager = DatabaseManager()
                    try:
                        db_manager.append_session_data({
                            'session_id': session.session_id,
                            'external_user_id': session.external_user_id,
                            'workflow_type': WorkflowType.rfq_submitted.value,
                            'outcome': session.outcome.value if session.outcome else None,
                            'workflow_state': session.workflow_state,
                            'conversation_history': session.conversation_history,
                            'extracted_entities': session.extracted_entities,
                            'rfq_ids': session.rfq_ids if hasattr(session, 'rfq_ids') else None,
                            'product_items': session.product_items if hasattr(session, 'product_items') else None,
                            'retention_date': session.retention_date,
                            'last_activity_at': session.last_activity_at,
                            'completed_at': session.completed_at
                        })
                        logger.debug(f"Appended completed RFQ session {session.session_id} to database")
                    finally:
                        db_manager.close()

                    # Clear Redis (session complete)
                    from app.redis_db import get_session_redis_service
                    from app.config import get_settings
                    settings = get_settings()
                    if settings.redis_session_storage_enabled:
                        redis_session = get_session_redis_service()
                        await redis_session.delete_session(session.session_id)
                        logger.debug(f"Deleted completed session {session.session_id} from Redis")
                elif result.get("continue_with_purchase_intent"):
                    # Continue with purchase intent flow for modifications
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                     self._should_use_summary_aware_extraction)

                else:
                    await self.session_manager.save_session(session, WorkflowType.rfq_creation)

                return result

            # If sectioned RFQ is active, always route to it regardless of intent
            if has_sectioned_rfq_active:
                logger.info("Sectioned RFQ workflow active - routing to sectioned RFQ handler")
                # Clear any preserved meaningful message - we want the actual current message
                if session.workflow_state:
                    session.workflow_state.pop("last_meaningful_message", None)
                    session.workflow_state.pop("last_meaningful_intent_result", None)
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                 self._should_use_summary_aware_extraction)

            if has_existing_data or has_incomplete_products:
                # Check for intent switch during active workflow BEFORE continuing
                # Note: Excel confirmation should not be interrupted by intent switches
                if not has_excel_confirmation_pending and await self.intent_switch_handler.should_handle_intent_switch(session, intent, confidence, intent_result.get('context_analysis')):
                    result = await self.intent_switch_handler.handle_intent_switch_choice(user, session, message,
                                                                                          intent, intent_result)
                    await self.session_manager.save_session(session, self._get_workflow_or_default(session))
                    return result

                # Already in RFQ workflow, continue collecting
                logger.info("Continuing existing RFQ workflow")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, None,
                                                                                 self._should_use_summary_aware_extraction)

            # Redirect to Seller Flow and its Orchestrator
            if (user.role.value if hasattr(user.role, "value") else user.role) == "seller" and intent != 'account_switch':

                # If workflow already in RFQ view → continue that flow first (highest priority)
                if session.workflow_type and hasattr(session.workflow_type, "value") and session.workflow_type.value == "seller_rfq_view" :
                    workflow_state = session.workflow_state or {}
                    current_seller_state = workflow_state.get("seller_workflow_state")

                    # Continue seller RFQ view flow immediately
                    return await self._handle_seller_flow(
                        user, session, message, message_intent_result
                    )

                # Otherwise → fresh seller flow routing based on intent clarity
                if intent == "rfq_status_check" and confidence > 0.7:
                    # Clear intent → RFQ Status inquiry
                    return await self._handle_rfq_status_inquiry(user, message, session)

                elif intent == "support" and confidence > 0.7:
                    # Support inquiry routing
                    return await self._handle_support_request(user, message,session)

                else:
                    # Intent unclear or general → default seller flow (Active RFQs)
                    return await self._handle_seller_flow(
                        user, session, message, intent_result
                    )

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
                        "Would you like to register as a buyer or seller?",
                        session_id=session
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
                # Check if we have a meaningful message preserved from auth/registration flow
                message_to_process = message
                intent_to_process = intent_result

                if session.workflow_state:
                    tracked_message = session.workflow_state.get("last_meaningful_message")
                    tracked_intent = session.workflow_state.get("last_meaningful_intent_result")

                    # Use meaningful message if it exists AND current message is post-auth (no active auth workflow)
                    if tracked_message and tracked_intent and session.workflow_type not in [WorkflowType.authentication, WorkflowType.registration]:
                        logger.debug(f"Using preserved meaningful message '{str(tracked_message)[:50]}...' instead of current message '{str(message)[:50]}...'")
                        message_to_process = tracked_message

                        # DEFENSIVE: Sanitize tracked_intent to prevent recursion from old stored data
                        # This handles cases where the session has unsafe intent_result from before the fix
                        try:
                            if isinstance(tracked_intent, dict):
                                # Safe copy with explicit type conversion to prevent circular refs
                                safe_all_intent_scores = {}
                                if isinstance(tracked_intent.get("all_intent_scores"), dict):
                                    for k, v in tracked_intent.get("all_intent_scores", {}).items():
                                        if isinstance(v, (int, float, str, bool, type(None))):
                                            safe_all_intent_scores[k] = v

                                safe_context_analysis = {}
                                if isinstance(tracked_intent.get("context_analysis"), dict):
                                    for k, v in tracked_intent.get("context_analysis", {}).items():
                                        if isinstance(v, (int, float, str, bool, type(None))):
                                            safe_context_analysis[k] = v
                                        elif isinstance(v, dict):
                                            # Shallow copy only primitives
                                            safe_context_analysis[k] = {k2: v2 for k2, v2 in v.items() if isinstance(v2, (int, float, str, bool, type(None)))}

                                intent_to_process = {
                                    "intent": str(tracked_intent.get("intent")) if tracked_intent.get("intent") else None,
                                    "confidence": int(tracked_intent.get("confidence", 0)),
                                    "all_intent_scores": safe_all_intent_scores,
                                    "context_analysis": safe_context_analysis,
                                    "reasoning": str(tracked_intent.get("reasoning")) if tracked_intent.get("reasoning") else None,
                                    "suggested_clarification": str(tracked_intent.get("suggested_clarification")) if tracked_intent.get("suggested_clarification") else None,
                                    "success": bool(tracked_intent.get("success", True))
                                }
                                logger.debug(f"[SANITIZE] Successfully sanitized tracked_intent")
                            else:
                                # If not a dict, use current intent_result as fallback
                                logger.warning(f"[SANITIZE] tracked_intent is not a dict, using current intent_result")
                                intent_to_process = intent_result
                        except Exception as e:
                            # If sanitization fails, use current intent_result as fallback
                            logger.error(f"[SANITIZE_ERROR] Failed to sanitize tracked_intent: {e}, using current intent_result")
                            intent_to_process = intent_result

                        # Clear the tracked message now that we're using it
                        session.workflow_state.pop("last_meaningful_message", None)
                        session.workflow_state.pop("last_meaningful_intent_result", None)
                        # Set flag to prevent re-tracking this message (prevents recursion loop)
                        session.workflow_state["meaningful_message_used"] = True

                # Normal buy_something flow - user wants to buy with current account
                logger.debug(f"[DEBUG] About to call handle_purchase_intent with message_to_process type: {type(message_to_process)}, intent_to_process type: {type(intent_to_process)}")
                try:
                    return await self.purchase_intent_handler.handle_purchase_intent(user, session, message_to_process, intent_to_process,
                                                                                     self._should_use_summary_aware_extraction)
                except RecursionError as e:
                    import traceback
                    logger.error(f"[RECURSION_ERROR] Traceback: {traceback.format_exc()}")
                    raise
            elif intent == "format_modification":
                # Handle format modification (Track 2)
                logger.debug(f"Format modification intent detected - routing to handler")
                return await self._handle_format_modification(user, session, message)
            elif intent == "confirmation_response" and confidence > 0.7:
                # Handle confirmation responses - these should already be handled by pending confirmations check above
                # But if we reach here, treat as continuation of existing workflow
                logger.debug(f"Handling confirmation response with context: {intent_result.get('context_analysis', {})}")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)
            elif intent == "reference_request" and confidence > 0.7:
                # Handle reference requests by routing to purchase intent flow
                # The EntityService will detect and handle the reference extraction
                logger.debug(f"Handling reference request with context: {intent_result.get('context_analysis', {})}")
                return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result,
                                                                                 self._should_use_summary_aware_extraction)
            elif intent == "bfs_search" and confidence > 0.7:
                # Handle BFS search intent with profile selection message
                user_role = user.role.value if hasattr(user.role, 'value') else user.role
                user_email = getattr(user, 'email', 'your profile')
                
                # Create the profile selection message
                profile_message = f"Got it, you're looking to check if items are available in stock.\nLet's continue with your {user_role.title()} profile ({user_email}).\n\nBFS Search coming soon!\nPlease confirm what you'd like to do next:"
                
                if user_role == "buyer":
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "get_support", "title": "Get Support Info"}
                    ]
                else:  # seller or other roles
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "get_support", "title": "Get Support Info"}
                    ]
                
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    profile_message,
                    buttons_config,
                    session_id=session
                )
                
                return {"status": "bfs_search_handled"}
            elif intent == "sell_something" and confidence > 0.7:
                # Normal sell_something flow - user wants to sell with current account
                return await self._handle_seller_flow(user, session, message, intent_result)
            elif intent == "rfq_status_check" and confidence > 0.7:
                return await self._handle_rfq_status_inquiry(user, message, session)
            elif intent == "account_switch" and confidence > 0.7:
                return await self._handle_account_switch_intent(user, session, message, intent_result)
            elif intent == "general_inquiry":
                # Handle FAQ requests
                logger.debug(f"general inquiry intent detected with {confidence}% confidence in main routing")
                await self.handle_irrelevant_message_flow(user.phone_number, message_intent_result, session)
            elif intent == "greeting":
                return await self._handle_greeting_inquiry(user, message,session, intent_result)

            elif confidence < 0.5:
                return await self._handle_clarification_request(user, message,session)
            else:
                return await self._handle_fallback(user, message,session)

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
        
        # Track upload lock for cleanup
        upload_lock = None
        
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

            # Check if user is in optional phase - treat Excel as attachment instead of bulk upload
            has_pending_optional = bool(
                session.workflow_state.get("pending_optional_rfq") or
                session.workflow_state.get("pending_optional_combined_rfq")
            )
            if has_pending_optional:
                logger.info(f"[EXCEL-UPLOAD] User {user.phone_number} is in optional phase - treating Excel as attachment")
                # Delegate to image processor to handle as attachment
                return await self.image_processor.process_image_message(user, session, content)

            # Check if an Excel file has already been processed in this workflow
            if session.workflow_state and session.workflow_state.get('excel_file_processed'):
                processed_filename = session.workflow_state.get('excel_filename', 'a file')
                logger.info(f"[EXCEL-UPLOAD] Excel file already processed: {processed_filename} for user {user.phone_number}")
                await self.whatsapp_service.send_message(
                    user.phone_number,
                    f"An Excel file ('{processed_filename}') has already been processed for this RFQ. "
                    f"If you would like to upload a different excel file, please create a new RFQ by completing the current one, or by cancelling it."
                )
                return {"status": "handled", "response": "excel_already_processed"}

            # Extract document information - handle both formats
            if not isinstance(content, dict):
                raise ValueError("Invalid Excel upload content format")

            if "document" in content:
                # Standard WhatsApp format
                document_info = content["document"]
                file_url = document_info.get("link")
                filename = document_info.get("filename", "")
            else:
                # ICS format - direct content structure
                media_id = content.get("id")
                filename = content.get("filename", "")
                
                if media_id:
                    # Construct download URL from media ID
                    file_url = f"https://download.sendmsg.in/whatsapp-mediadownloader/{media_id}"
                else:
                    file_url = None

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

            # Acquire upload lock to prevent simultaneous uploads
            from redis.asyncio import Redis
            from app.utils.datetime_utils import utc_now
            from app.config import get_settings
            settings = get_settings()
            redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
            normalized_phone = user.phone_number.lstrip('+')
            lock_key = f"{normalized_phone}:excel_upload_lock"
            upload_lock = redis_client.lock(lock_key, timeout=120)  # 2 minutes for processing
            
            acquired = await upload_lock.acquire(blocking=False)
            if not acquired:
                logger.info(f"[EXCEL-UPLOAD] Upload already in progress for {user.phone_number}")
                await self.whatsapp_service.send_message(
                    user.phone_number,
                    "Currently, we can only process one excel file per RFQ. To upload another file, please create a new RFQ by completing the current one, or by cancelling it."
                )
                return {"status": "handled", "response": "upload_in_progress"}
            
            logger.info(f"[EXCEL-UPLOAD] Upload lock acquired for {user.phone_number}")

            # Set excel_file_processed flag IMMEDIATELY after successful validation and lock acquisition
            # This prevents race conditions with simultaneous uploads
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['excel_file_processed'] = True
            session.workflow_state['excel_processed_at'] = utc_now().isoformat()
            session.workflow_state['excel_filename'] = filename
            logger.info(f"[EXCEL-UPLOAD] Set excel_file_processed flag for {user.phone_number}, file: {filename}")

            # Send processing message after validation checks pass
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Please wait, the file is processing…"
            )
            logger.info(f"[EXCEL-UPLOAD] Sent 'Please wait' message to {user.phone_number}")

            # Process Excel file
            processing_service = ExcelProcessingService(self.openai_service)
            processing_result = await processing_service.process_excel_file(
                content=validation_result['content'],
                filename=filename
            )


            # Prepare context using helpers
            excel_context = ExcelHelpers.prepare_excel_context(processing_result, user.phone_number)

            # intiliased sectioned rfw
            # Update session
            WorkflowManager.set_workflow_type(session, WorkflowType.rfq_creation, caller='excel_upload_handler')
            WorkflowManager.initialize_workflow_state(session)
            session.workflow_state.update(excel_context)

            # Determine flow based on completeness
            completeness = excel_context['completeness']
            items = processing_result.get('items', [])

            # Check if processing was successful
            if processing_result.get('success') and items and len(items) > 0:
                # Set workflow type for RFQ creation
                WorkflowManager.set_workflow_type(session, WorkflowType.rfq_creation, caller='excel_upload_complete')
                result = await self._handle_complete_excel(user, session, processing_result)
                # Release lock after successful processing
                if upload_lock:
                    await upload_lock.release()
                    logger.info(f"[EXCEL-UPLOAD] Lock released after successful processing for {user.phone_number}")
                return result
            else:
                # Processing failed or no items extracted
                result = await self._handle_incomplete_excel(user, session, excel_context)
                # Release lock after incomplete handling
                if upload_lock:
                    await upload_lock.release()
                    logger.info(f"[EXCEL-UPLOAD] Lock released after incomplete handling for {user.phone_number}")
                return result

        except Exception as e:
            logger.error(f"Error processing Excel upload: {e}")
            
            # Clear the excel_file_processed flag on critical error
            if session.workflow_state and 'excel_file_processed' in session.workflow_state:
                del session.workflow_state['excel_file_processed']
                del session.workflow_state['excel_processed_at']
                del session.workflow_state['excel_filename']
                logger.info(f"[EXCEL-UPLOAD] Cleared excel_file_processed flag due to critical error for {user.phone_number}")
            
            # Release lock on critical error
            if upload_lock:
                try:
                    await upload_lock.release()
                    logger.info(f"[EXCEL-UPLOAD] Lock released after critical error for {user.phone_number}")
                except Exception as lock_error:
                    logger.error(f"[EXCEL-UPLOAD] Failed to release lock: {lock_error}")
            
            error_message = "Sorry, I encountered an error processing your Excel file. Please try uploading again or provide the details through text."
            await self.session_manager.send_and_track_message(user.phone_number, error_message, session)
            return {"status": "error", "response": str(e)}

    async def _handle_complete_excel(self, user: User, session: ConversationSession, processing_result: Dict) -> Dict[
        str, Any]:
        """Handle complete Excel files with confirmation step before proceeding to multiple RFQ flow."""
        try:
            # Convert Excel items to products array format
            products = self._convert_excel_items_to_products_array(processing_result['items'])
            
            logger.info(f"[EXCEL-CONFIRMATION] Converted {len(processing_result['items'])} Excel items to {len(products)} products")
            
            # Validation: Check if all items were successfully extracted
            total_rows = processing_result.get('total_items', len(processing_result['items']))
            extracted_rows = len(products)
            
            if extracted_rows :
                # All items successfully extracted - proceed with confirmation flow
                
                # Save extracted data to session for confirmation flow
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['excel_confirmation_data'] = {
                    'products': products,
                    'processing_result': processing_result,
                    'filename': processing_result.get('filename', 'Excel file')
                }
                session.workflow_state['awaiting_excel_confirmation'] = True
                
                # Clear any existing workflow state to prepare for fresh flow
                session.workflow_state.pop('incomplete_products', None)
                session.workflow_state.pop('complete_products', None)
                session.workflow_state.pop('excel_data', None)
                
                # Send confirmation message with processing summary
                processing_summary = processing_result.get('processing_summary', {})
                skipped_rows = processing_summary.get('skipped_rows', 0)
                skipped_items_summary = processing_result.get('skipped_items_summary', '')
                
                # Generate item list for display (show first 3 items, then +X more)
                item_names = []
                for i, product in enumerate(products[:3]):
                    desc = product.get('description', f'Item{i+1}')
                    item_names.append(desc)
                
                if len(products) > 3:
                    remaining = len(products) - 3
                    items_display = f"{', '.join(item_names)}, +{remaining} Items"
                else:
                    items_display = ', '.join(item_names)
                
                if skipped_rows > 0:
                    confirmation_message = f"Successfully identified {items_display}.\nPlease click on Confirm to proceed for the RFQ creation\n\n(Type 'Exit' to anytime to end the chat)\n\n📝"
                else:
                    confirmation_message = f"Successfully identified {items_display}.\nPlease click on Confirm to proceed for the RFQ creation\n\n(Type 'Exit' to anytime to end the chat)"
                
                # Send confirmation message with buttons
                buttons_config = [
                    {"id": "confirm_excel", "title": "Confirm"},
                    {"id": "cancel_excel", "title": "Cancel"}
                ]
                
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    confirmation_message,
                    buttons_config,
                    "Please choose:"
                )
                await self.session_manager.save_session(session, WorkflowType.rfq_creation)
                
                return {"status": "excel_confirmation_sent", "awaiting_confirmation": True}
                
            else:
                # Not all items extracted - handle as incomplete
                logger.info(f"[EXCEL-INCOMPLETE] Only {extracted_rows}/{total_rows} items extracted - handling as incomplete")
                excel_context = {
                    'excel_data': processing_result,
                    'completeness': (extracted_rows / total_rows) * 100 if total_rows > 0 else 0,
                    'missing_fields': ['Some items could not be processed']
                }
                return await self._handle_incomplete_excel(user, session, excel_context)

        except Exception as e:
            logger.error(f"[EXCEL-COMPLETE-ERROR] Error handling complete Excel: {e}")
            logger.error(f"[EXCEL-COMPLETE-ERROR] Error type: {type(e)}")
            logger.error(f"[EXCEL-COMPLETE-ERROR] Processing result at error: {processing_result}")
            import traceback
            logger.error(f"[EXCEL-COMPLETE-ERROR] Full traceback: {traceback.format_exc()}")
            
            error_context = {'error': str(e), 'workflow_type': 'excel_rfq_upload'}
            error_response = await self.response_helpers.generate_contextual_response(
                error_context,
                ["There was an issue processing your Excel file. Let me help you through conversation."],
                "error_recovery"
            )
            await self.session_manager.send_and_track_message(user.phone_number, error_response, session)
            return await self._handle_incomplete_excel(user, session, {"excel_data": processing_result})
    
    async def _handle_excel_confirmation_response(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Handle user response to Excel confirmation message with AI-based confirmation and keyword fallback."""
        try:
            # First try AI-based confirmation parsing
            ai_confirmation_result = None
            try:
                ai_confirmation_result = await self.openai_service.parse_confirmation_response(message)
            except Exception as e:
                logger.warning(f"[EXCEL-CONFIRMATION-AI] AI parsing failed: {e}, falling back to keywords")
            
            # Determine confirmation status using AI result or keyword fallback
            is_confirmed = None
            is_cancelled = None
            
            if ai_confirmation_result == "yes":
                is_confirmed = True
            elif ai_confirmation_result == "no":
                is_cancelled = True
            else:
                # Fallback to keyword matching
                message_lower = message.lower().strip()
                confirm_responses = ['confirm', 'yes', 'y', 'proceed', 'continue', 'ok']
                cancel_responses = ['clear', 'cancel', 'exit', 'reupload', 'no', 'n', 'restart']
                
                if any(response in message_lower for response in confirm_responses):
                    is_confirmed = True
                    logger.debug(f"[EXCEL-CONFIRMATION] Keyword detected confirmation: '{message}'")
                elif any(response in message_lower for response in cancel_responses):
                    is_cancelled = True
                    logger.debug(f"[EXCEL-CONFIRMATION] Keyword detected cancellation: '{message}'")
            
            if is_confirmed:
                # User confirmed - proceed to multiple RFQ creation
                logger.debug(f"[EXCEL-CONFIRMED] User confirmed Excel processing - proceeding to RFQ creation")
                
                # Retrieve saved data
                excel_data = session.workflow_state.get('excel_confirmation_data', {})
                products = excel_data.get('products', [])
                processing_result = excel_data.get('processing_result', {})
                filename = excel_data.get('filename', 'Excel file')
                
                if not products:
                    error_message = "Sorry, I couldn't find the Excel data. Please upload your file again."
                    await self.session_manager.send_and_track_message(user.phone_number, error_message, session)
                    return {"status": "excel_data_missing"}

                # Check for sectioned RFQ configuration
                from app.config import get_settings
                settings = get_settings()
                
                if settings.use_sectioned_rfq:
                    # Handle sectioned RFQ workflow
                    result = await self._handle_excel_sectioned_rfq(user, session, products)
                    if result.get('status') == 'validation_failed':
                        # Validation failed - file rejected, return immediately
                        logger.info(f"[EXCEL-CONFIRMED] File rejected due to validation failure")
                        return result
                    elif result.get('status') == 'excel_sectioned_rfq_initialized':
                        # Continue with sectioned RFQ flow using PurchaseIntentHandler
                        # update flag in session to hide modify btn 
                        session.workflow_state['excel_source'] = True
                        return await self.purchase_intent_handler.handle_purchase_intent(user, session, message, intent_result, self._should_use_summary_aware_extraction)
                    else:
                        return result
                else:
                    # Clear session data for non-sectioned flow
                    fields_to_clear = [
                        'extracted_entities', 'completeness', 'missing_fields', 'conversation_stage',
                        'user_phone', 'upload_timestamp', 'filename', 'total_items', 'delivery_date',
                        'pincode', 'state', 'city', 'current_intent_result', 'original_message',
                        'original_intent','excel_confirmation_data','awaiting_excel_confirmation'
                    ]
                    for field in fields_to_clear:
                        session.workflow_state.pop(field, None)
                    
                    # Use existing products array handler for multiple RFQ creation
                    return await self.products_array_handler.handle_products_array(
                        user, session, f"Excel upload: {filename}", products
                    )
                
            elif is_cancelled:
                logger.debug(f"[EXCEL-CANCELLED] User cancelled Excel processing - clearing session")
                # User cancelled - clear session and redirect to initial greeting stage
                logger.debug(f"[EXCEL-CANCELLED] User cancelled Excel processing - clearing session and redirecting to greeting")
                
                # Clear all Excel-related data and reset session completely
                session.workflow_state = {}
                session.workflow_type = None
                
                # Get user details for personalized greeting
                user_role = user.role.value if hasattr(user.role, 'value') else user.role
                first_name = user.name.split()[0].capitalize() if user.name else "there"
                
                # Send the specified greeting message with buttons
                greeting_message = (
                    f"Hi {first_name}! Let's continue with your buyer profile ({user.email})\n"
                    "What can I assist you with today?\n\n"
                    "If you’d like to create an RFQ using Excel, please attach your file here.\n\n"
                )

                # Role-based button configuration
                if user_role == "buyer":
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "search_bfs", "title": "Search Stocks"}
                    ]
                else:
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "get_support", "title": "Get Support Info"}
                    ]
                
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    greeting_message,
                    buttons_config
                )
                
                await self.session_manager.save_session(session, None)
                
                return {"status": "excel_cancelled_redirected_to_greeting"}
                
            else:
                # Unclear response - send clarification with buttons
                clarification_message = "🤔 I didn't quite understand your response.\n\nPlease choose what you'd like to do:"
                
                buttons_config = [
                    {"id": "confirm_excel", "title": "✅ Confirm"},
                    {"id": "cancel_excel", "title": "❌ Cancel"}
                ]
                
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    clarification_message,
                    buttons_config,
                    "Excel Processing:"
                )
                return {"status": "excel_clarification_requested"}
                
        except Exception as e:
            logger.error(f"[EXCEL-CONFIRMATION-ERROR] Error handling Excel confirmation response: {e}")
            error_message = "⚠️ Sorry, there was an error processing your response.\n\nPlease choose what you'd like to do:"
            
            buttons_config = [
                {"id": "confirm_excel", "title": "✅ Confirm"},
                {"id": "cancel_excel", "title": "❌ Cancel"}
            ]
            
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_message,
                buttons_config,
                "Excel Processing:"
            )
            return {"status": "error", "error": str(e)}
        
    async def _handle_excel_sectioned_rfq(self, user: User, session: ConversationSession, products: List[Dict]) -> Dict[str, Any]:
        """Handle Excel data in sectioned RFQ workflow."""
        from app.services.workflow_manager import WorkflowManager
        
        # Initialize sectioned RFQ workflow
        if not WorkflowManager.is_sectioned_rfq_active(session):
            logger.debug(f"[EXCEL-SECTIONED] Initializing sectioned RFQ workflow")
            WorkflowManager.initialize_sectioned_rfq(session)
            WorkflowManager.set_sectioned_rfq_section(session, "date_location")
            if "sectioned_rfq" in session.workflow_state:
                session.workflow_state["sectioned_rfq"]["active"] = True

        try:
            products, delivery_details = await self.transform_rfq_to_section_rfq_format(session, products)
        except ValueError as ve:
            # Validation failed - send error message and reject file
            error_msg = str(ve)
            logger.error(f"[EXCEL-SECTIONED] Validation failed: {error_msg}")
            await self.whatsapp_service.send_message(user.phone_number, error_msg, session_id=session)
            # Clear workflow state and return validation failure
            session.workflow_state = {}
            session.workflow_type = None
            await self.session_manager.save_session(session, None)
            return {"status": "validation_failed", "error": error_msg}
        except Exception as e:
            logger.error(f"[EXCEL-SECTIONED-ERROR] Error transforming RFQ format: {e}")
            from app.utils.excel_error_formatter import format_excel_error
            error_msg = format_excel_error('validation_error', {'message': 'Error processing file. Please check your data and try again.'})
            await self.whatsapp_service.send_message(user.phone_number, error_msg, session_id=session)
            session.workflow_state = {}
            session.workflow_type = None
            await self.session_manager.save_session(session, None)
            return {"status": "error", "error": str(e)}
        
        # Mark this sectioned RFQ as originating from Excel (allows >5 items)
        WorkflowManager.set_sectioned_rfq_from_excel(session, True, caller="excel_sectioned_rfq")

        # Store Excel products in items section
        WorkflowManager.update_section_data(session, "items", products)
        logger.debug(f"[EXCEL-SECTIONED] Stored {len(products)} products in items section")
        
        # Store delivery details in date_location section using proper field names
        if delivery_details:
            WorkflowManager.update_section_data(session, "date_location", delivery_details)
            logger.debug(f"[EXCEL-SECTIONED] Stored delivery details: {delivery_details}")

        # Clear existing excel session data
        fields_to_clear = [
            'completeness', 'missing_fields', 'conversation_stage',
            'user_phone', 'upload_timestamp', 'filename', 'total_items', 'delivery_date',
            'pincode', 'state', 'city', 'current_intent_result', 'original_message',
            'original_intent','excel_confirmation_data','awaiting_excel_confirmation'
        ]
        
        # Set extracted_entities to empty list
        session.workflow_state['extracted_entities'] = []
        
        # Add external_user_id field
        if session.workflow_state.get('user_phone'):
            session.workflow_state['external_user_id'] = session.workflow_state['user_phone']
        
        # Clear other fields
        for field in fields_to_clear:
            session.workflow_state.pop(field, None)
        
       
        await self.session_manager.save_session(session, WorkflowType.rfq_creation)
        
        return {"status": "excel_sectioned_rfq_initialized", "message": "Sectioned RFQ workflow started"}


    async def transform_rfq_to_section_rfq_format(self, session: ConversationSession, products: List[Dict]) -> tuple[List[Dict], Dict]:
        """Transform Excel RFQ format to sectioned RFQ format."""
        try:
            transformed_products = []
            delivery_details = {}
            
            # Check if date_location section is already confirmed in sectioned RFQ
            sectioned_rfq = session.workflow_state.get('sectioned_rfq', {})
            date_location_confirmed = (
                sectioned_rfq.get('sections', {})
                .get('date_location', {})
                .get('confirmed', False)
            )
            
            if date_location_confirmed:
                # Use confirmed date_location data, ignore Excel date/location
                confirmed_data = (
                    sectioned_rfq.get('sections', {})
                    .get('date_location', {})
                    .get('data', {})
                )
                delivery_details = confirmed_data.copy()
                logger.debug(f"[TRANSFORM-RFQ] Using confirmed date_location data, ignoring Excel: {delivery_details}")
            else:
                # Extract delivery details from session - map to sectioned RFQ format
                if session.workflow_state.get('delivery_date'):
                    delivery_details['deliveryDate'] = session.workflow_state['delivery_date']
                if session.workflow_state.get('pincode'):
                    delivery_details['pincode'] = session.workflow_state['pincode']
                if session.workflow_state.get('city'):
                    delivery_details['city'] = session.workflow_state['city']
                if session.workflow_state.get('state'):
                    delivery_details['state'] = session.workflow_state['state']
            
            # Only validate if date_location is not already confirmed
            if not date_location_confirmed:
                # Validate and normalize delivery date if provided using sectioned RFQ handler
                if delivery_details.get('deliveryDate'):
                    from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
                    sectioned_handler = SectionedRFQCreationHandler(
                        entity_service=self.entity_service,
                        whatsapp_service=self.whatsapp_service,
                        cancel_service=None,
                        session_manager=self.session_manager
                    )
                    date_validation = await sectioned_handler._validate_delivery_date(delivery_details['deliveryDate'])
                    if date_validation.get('is_valid'):
                        delivery_details['deliveryDate'] = date_validation.get('normalized_date', delivery_details['deliveryDate'])
                        logger.debug(f"[TRANSFORM-RFQ] Delivery date validated: {delivery_details['deliveryDate']}")
                    else:
                        # Date validation failed - raise exception to reject file
                        from app.utils.excel_error_formatter import format_excel_error
                        error_msg = date_validation.get('error', 'Invalid delivery date')
                        logger.error(f"[TRANSFORM-RFQ] Date validation failed: {error_msg}")
                        raise ValueError(format_excel_error('date_validation', {'message': error_msg}))
                
                # Validate pincode if provided
                if delivery_details.get('pincode'):
                    from app.utils.pincode_lookup import get_location_from_pincode_async
                    pincode = delivery_details['pincode'].strip()
                    
                    # Validate pincode format
                    if not pincode.isdigit() or len(pincode) != 6:
                        from app.utils.excel_error_formatter import format_excel_error
                        error_msg = f"Invalid pincode '{pincode}'. Pincode must be a 6-digit number."
                        logger.error(f"[TRANSFORM-RFQ] Pincode format validation failed: {error_msg}")
                        raise ValueError(format_excel_error('pincode_validation', {'message': error_msg}))
                    
                    # Validate pincode existence
                    location = await get_location_from_pincode_async(pincode)
                    if not location:
                        from app.utils.excel_error_formatter import format_excel_error
                        error_msg = f"Pincode '{pincode}' not found. Please provide a valid Indian pincode."
                        logger.error(f"[TRANSFORM-RFQ] Pincode existence validation failed: {error_msg}")
                        raise ValueError(format_excel_error('pincode_validation', {'message': error_msg}))
                    
                    # Auto-fill city/state from validated pincode
                    if not delivery_details.get('city') or not delivery_details.get('state'):
                        logger.debug(f"[TRANSFORM-RFQ] Auto-filling location from validated pincode: {pincode}")
                        if location.get('city'):
                            delivery_details['city'] = location['city']
                        if location.get('state'):
                            delivery_details['state'] = location['state']
            
            # Transform each product to match sectioned RFQ items format exactly
            for product in products:
                transformed_product = {
                    'description': product.get('description', ''),
                    'quantity': int(product.get('quantity', 0)) if product.get('quantity') and str(product.get('quantity')).isdigit() else 0,
                    'unitofMeasures': product.get('uom', 'unit(s)'),
                    'brand': product.get('projectDesc', ''),
                    'remarks': product.get('remarks', ''),
                    'deliveryDate': delivery_details.get('deliveryDate', ''),
                    'pincode': delivery_details.get('pincode', ''),
                    'city': delivery_details.get('city', ''),
                    'state': delivery_details.get('state', '')
                }
                transformed_products.append(transformed_product)
            
            return transformed_products, delivery_details
            
        except ValueError as ve:
            # Re-raise ValueError for validation failures
            raise
        except Exception as e:
            from app.utils.excel_error_formatter import format_excel_error
            logger.error(f"[TRANSFORM-RFQ-ERROR] Error transforming RFQ format: {e}")
            raise ValueError(format_excel_error('validation_error', {'message': 'Error processing file. Please check your data and try again.'}))
    

            
            
    def _convert_excel_items_to_products_array(self, excel_items: List[Dict]) -> List[Dict]:
        """Convert Excel items to products array format for existing multiple RFQ flow."""
        products = []
        
        for i, item in enumerate(excel_items, 1):
           
            # Map Excel columns to entity format expected by products array handler
            product_entity = {
                'description': item.get('ItemDescription', ''),
                'projectDesc': item.get('Specification', ''),
                'quantity': item.get('Quantity', ''),
                'uom': item.get('Uom', 'pcs'),
                'remarks': item.get('Remarks', ''),
                # Add default values for required fields that Excel doesn't have
                'deliveryDate': None,
                'state': None,
                'city': None,
                'pincode': None,
                'division': None
            }
                        
            # Clean up empty values but keep structure for validation
            cleaned_entity = {}
            for k, v in product_entity.items():
                
                try:
                    if v is not None:
                        # Convert to string first
                        v_str = str(v)                        
                        # Check if it has content (str() already gives clean representation)
                        if v_str and v_str.strip():
                            # Keep quantity as string but ensure it's valid
                            if k == 'quantity':
                                try:
                                    # Validate it's a valid number but keep as string
                                    float(v_str)
                                    cleaned_entity[k] = v_str
                                except ValueError as ve:
                                    cleaned_entity[k] = None
                            else:
                                # Only strip if it's actually a string that needs stripping
                                if isinstance(v, str):
                                    cleaned_entity[k] = v_str.strip()
                                else:
                                    cleaned_entity[k] = v_str
                        else:
                            cleaned_entity[k] = None  # Keep None for empty strings
                    else:
                        cleaned_entity[k] = None  # Keep None for missing required fields
                        
                except Exception as field_error:
                    logger.error(f"[EXCEL-CONVERSION-ITEM-{i}] ERROR processing field '{k}': {field_error}")
                    logger.error(f"[EXCEL-CONVERSION-ITEM-{i}] Field details - value: {v}, type: {type(v)}")
                    raise field_error
            
            products.append(cleaned_entity)
            
        logger.debug(f"[EXCEL-CONVERSION-SUCCESS] Successfully converted {len(excel_items)} Excel items to products array")
        logger.debug(f"[EXCEL-CONVERSION-RESULT] Final products: {products}")
        return products

    async def _handle_incomplete_excel(self, user: User, session: ConversationSession, excel_context: Dict) -> Dict[
        str, Any]:
        """Handle incomplete Excel files that need conversation completion."""
        try:
            processing_result = excel_context['excel_data']
            missing_fields = excel_context['missing_fields']
            
            # Check if this is a rejection due to skipped rows
            if processing_result.get('should_skip_rfq_creation') and processing_result.get('processing_summary'):
                processing_summary = processing_result['processing_summary']
                skipped_items_summary = processing_summary.get('skipped_items_summary', '')
                
                # Show rejection message with skipped items details
                rejection_message = processing_result.get('error', 'File processing incomplete')
                await self.session_manager.send_and_track_message(user.phone_number, rejection_message, session)
                
                # Update session state - waiting for excel reupload
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['stage'] = 'excel_reupload_required'
                session.workflow_state['pending_excel_reupload'] = True
                session.workflow_state['last_excel_issues'] = [processing_result.get('error', 'File processing incomplete')]
                await self.session_manager.save_session(session, WorkflowType.rfq_creation)
                
                return {"status": "excel_rejected", "response": "excel_rejection_sent"}

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
            clarification_response = await self.openai_service.generate_clarification_response(
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
            # Store issues in session state for retry
            session.workflow_state['last_excel_issues'] = missing_fields
            await self.session_manager.save_session(session, WorkflowType.rfq_creation)
            
            return {"status": "excel_reupload_required", "response": "excel_reupload_instructions_sent"}

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

    async def _handle_greeting_inquiry(
            self, user: User, message: str, session: ConversationSession,intent_result: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Handle general inquiries using OpenAI."""
        try:
            context = ChatServiceHelpers.build_context("greeting", message)
            logger.debug(f"intent result in handle general inquiry :{intent_result}")

            # Determine user role
            user_role = user.role.value if hasattr(user.role, 'value') else user.role

            logger.debug(f"continue with user profile:{user.email}, name:{user.name}, user role:{user_role}")

            # Role-based button configuration
            if user_role == "buyer":
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]
                profile_message = (
                    f"Perfect! We’ll continue with your Buyer profile (*{user.email}*).\n\n"
                    "If you’d like to create an RFQ using Excel, please attach your file here.\n\n"
                )
                # Extract first name and capitalize first letter
                first_name = user.name.split()[0].capitalize() if user.name else "there"
                header = f"Hi {first_name}! What can I assist you with today?"

            elif user_role == "seller":
                buttons_config = [
                    {"id": "rfq_status", "title": "Check RFQ status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
                profile_message = f"Let's continue with your seller account ({user.email})"
                # Extract first name and capitalize first letter
                first_name = user.name.split()[0].capitalize() if user.name else "there"
                header = f"Hi {first_name}! What would you like to do today?"

            else:
                # Unknown role → check if we can determine role from user object
                if hasattr(user, 'role') and user.role:
                    actual_role = user.role.value if hasattr(user.role, 'value') else user.role
                    if actual_role == "buyer":
                        buttons_config = [
                            {"id": "create_rfq", "title": "Create new RFQ"},
                            {"id": "rfq_status", "title": "Check RFQ Status"},
                            {"id": "search_bfs", "title": "Search Stocks"}
                        ]
                        profile_message = (
                            f"We’ll continue with your Buyer profile (*{user.email}*).\n\n"
                            "If you’d like to create an RFQ using Excel, please attach your file here.\n\n"
                        )
                        # Extract first name and capitalize first letter
                        first_name = user.name.split()[0].capitalize() if user.name else "there"
                        header = f"Hi {first_name}! What can I assist you with today?"
                    elif actual_role == "seller":
                        buttons_config = [
                            {"id": "rfq_status", "title": "Check RFQs Status"},
                            {"id": "contact_support", "title": "Contact Support"}
                        ]
                        profile_message = f"Let's continue with your seller account ({user.email})"
                        # Extract first name and capitalize first letter
                        first_name = user.name.split()[0].capitalize() if user.name else "there"
                        header = f"Hi {first_name}! What would you like to do today?"
                    else:
                        buttons_config = [
                            {"id": "create_rfq", "title": "Create new RFQ"},
                            {"id": "rfq_status", "title": "Check RFQ Status"},
                            {"id": "search_bfs", "title": "Search Stocks"}
                        ]
                        profile_message = "How can I help you with your procurement needs today?"
                        header = "Please choose an option:"
                else:
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "search_bfs", "title": "Search Stocks"}
                    ]
                    profile_message = "How can I help you with your procurement needs today?"
                    header = "Please choose an option:"
            # ✅ Send interactive buttons
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                profile_message,
                buttons_config,
                header,
                session_id=session
            )


            return {"status": "greeting_handled"}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "general_inquiry",
                                                     "How can I assist you today?")

    async def _handle_format_modification(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """
        Handle format modification intent (Track 2).

        Delegates to FormatModificationHandler which orchestrates:
        - Parsing/validation (via Track 1)
        - State management (retry counter, flags)
        - Error handling with retry limits
        """
        try:
            logger.debug(f"Processing format modification for user {user.phone_number}")

            # Delegate to format modification handler
            result = await self.format_modification_handler.handle_format_modification(
                message, session, user
            )

            # Save session after modification
            await self.session_manager.save_session(session, WorkflowType.rfq_creation)

            return result

        except Exception as e:
            logger.error(f"Error in format modification handler: {e}", exc_info=True)
            return await self._handle_error_response(
                e, user.phone_number, "format_modification",
                "An error occurred while processing your modification. Please try again."
            )

    async def _handle_faq_request(self, user: User, message: str) -> Dict[str, Any]:
        """Handle FAQ requests by providing answers from FAQ service."""
        try:
            logger.debug(f"Processing FAQ request for user {user.phone_number}: '{message[:50]}...'")

            # Get FAQ answer from FAQ service
            faq_answer = await self.faq_service.get_faq_answer(message)

            if faq_answer:
                # Send FAQ answer
                full_response = f"{faq_answer}\n\nWhat can I assist you with next?"
                await self.whatsapp_service.send_message(user.phone_number, full_response)
                return {"status": "faq_handled", "answer_provided": True}
            else:
                # No FAQ answer found, provide fallback
                fallback_message = "I don't have specific information about that. For detailed assistance, please contact our support team."
                await self.whatsapp_service.send_message(user.phone_number, fallback_message)
                return {"status": "faq_no_answer", "answer_provided": False}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "faq_request",
                                                     "I'm having trouble accessing FAQ information. Please try again or contact support.")

    async def _handle_support_request(self, user: User, message: str,session) -> Dict[str, Any]:
        """Handle support requests by providing contact information and menu options."""
        try:
            settings = get_settings()
            support_contact = settings.support_contact_info
            
            # Get user role for appropriate menu
            user_role = user.role.value if hasattr(user.role, 'value') else user.role
            
            # Create support message with contact info
            support_message = ""
            
            # Role-based button configuration
            if user_role == "buyer":
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]
                header = "What else can I help you with?"
            elif user_role == "seller":
                buttons_config = [
                    {"id": "view_rfqs", "title": "Request Active RFQs"},
                    {"id": "rfq_status", "title": "Show RFQ status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
                header = "What else would you like to do?"
            else:
                buttons_config = [
                    {"id": "contact_support", "title": "Contact Support"},
                    {"id": "exit", "title": "Exit"}
                ]
                header = "How can I help you?"
            
            # Send interactive buttons
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                support_message,
                buttons_config,
                header,
                session_id=session
            )
            
            return {"status": "support_handled"}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "support_request",
                                                     "For support, please contact info.support.com")

    async def _handle_clarification_request(self, user: User, message: str,session) -> Dict[str, Any]:
        """Handle ambiguous messages requiring clarification."""
        try:
            # Check user role to provide appropriate menu
            user_role = user.role.value if hasattr(user.role, 'value') else user.role
            
            if user_role == "buyer":
                # Buyer fallback with buttons
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    "What can I assist you with today?",
                    buttons_config,
                    "Please choose an option:",
                    session_id=session
                )
            elif user_role == "seller":
                # Seller fallback with buttons
                buttons_config = [
                    {"id": "rfq_status", "title": "Check RFQ status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    f"Hi {user.name}! What would you like to do today?",
                    buttons_config,
                    "Please choose an option:",
                    session_id=session
                )
            else:
                # Fallback based on user role
                user_role = user.role.value if hasattr(user.role, 'value') else user.role
                if user_role == "buyer":
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "search_bfs", "title": "Search Stocks"}
                    ]
                elif user_role == "seller":
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQs Status"},
                        {"id": "contact_support", "title": "Contact Support"}
                    ]
                else:
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "search_bfs", "title": "Search Stocks"}
                    ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    "How can I help you with your procurement needs today?",
                    buttons_config,
                    "Please choose an option:",
                    session_id=session
                )
            
            return {"status": "clarification_sent"}

        except Exception as e:
            return await self._handle_error_response(e, user.phone_number, "clarification_request",
                                                     "Could you be more specific about your procurement needs?")

    async def _handle_fallback(self, user: User, message: str,session) -> Dict[str, Any]:
        """Handle messages that don't fit other categories."""
        try:
            # Check user role to provide appropriate menu
            user_role = user.role.value if hasattr(user.role, 'value') else user.role
            
            if user_role == "buyer":
                # Buyer fallback with buttons
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    "What can I assist you with today?",
                    buttons_config,
                    "Please choose an option:",
                    session_id=session
                )
            elif user_role == "seller":
                # Seller fallback with buttons
                buttons_config = [
                    {"id": "rfq_status", "title": "🔍 Check RFQ status"},
                    {"id": "get_support", "title": "💬 Get Support Info"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    "What would you like to do today?",
                    buttons_config,
                    "Please choose an option:",
                    session_id=session
                )
            else:
                # Fallback based on user role
                user_role = user.role.value if hasattr(user.role, 'value') else user.role
                if user_role == "buyer":
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "search_bfs", "title": "Search Stocks"}
                    ]
                elif user_role == "seller":
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQs Status"},
                        {"id": "contact_support", "title": "Contact Support"}
                    ]
                else:
                    buttons_config = [
                        {"id": "create_rfq", "title": "Create new RFQ"},
                        {"id": "rfq_status", "title": "Check RFQ Status"},
                        {"id": "search_bfs", "title": "Search Stocks"}
                    ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    "How can I help you with your procurement needs today?",
                    buttons_config,
                    "Please choose an option:",
                    session_id=session
                )
            
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

            logger.debug(f"Enhanced account selection check: current_role={current_role}, target_role={target_role}, target_intent={target_intent}")

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
        # Handle sectioned RFQ buttons (Track 3)
        # IMPORTANT: Exclude confirm_rfq from sectioned RFQ handling - it should be handled by confirmation_handler
        # Only handle section-specific buttons like confirm_date_location, confirm_items, etc.
        is_sectioned_rfq_button = button_id.startswith(("confirm_", "modify_", "final_", "attachments_", "restart_"))

        # Exclude generic confirm_rfq (final confirmation), continue_rfq, and Excel buttons from sectioned routing
        if is_sectioned_rfq_button and button_id not in ["confirm_rfq", "continue_rfq", "confirm_excel", "cancel_excel"]:
            logger.info(f"[SECTIONED_RFQ] Button click detected: {button_id}")
            if WorkflowManager.is_sectioned_rfq_active(session):
                # Get or create sectioned RFQ handler
                if not hasattr(self.purchase_intent_handler, 'sectioned_rfq_handler'):
                    from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
                    from app.services.cancel_service import CancelService
                    cancel_service = CancelService(self.whatsapp_service, self.session_manager)
                    self.purchase_intent_handler.sectioned_rfq_handler = SectionedRFQCreationHandler(
                        entity_service=self.entity_service,
                        whatsapp_service=self.whatsapp_service,
                        cancel_service=cancel_service,
                        session_manager=self.session_manager,
                        confirmation_handler=self.confirmation_handler,
                        attachment_decision_handler=self.attachment_decision_handler
                    )

                # Route to sectioned RFQ button handler
                return await self.purchase_intent_handler.sectioned_rfq_handler.handle_section_button_click(
                    user, session, button_id
                )

        # Handle new menu buttons
        if button_id == "new_rfq" or button_id == "raise_rfq" or button_id == "create_rfq":
            # Check if we have a tracked meaningful message from auth/registration flow
            workflow_state = session.workflow_state or {}
            tracked_message = workflow_state.get("last_meaningful_message")
            tracked_intent_result = workflow_state.get("last_meaningful_intent_result")

            # CRITICAL: Only use tracked message if session has NO existing RFQ data
            # This prevents using stale messages after RFQ creation/exit
            has_existing_rfq_data = bool(
                workflow_state.get("pending_rfq") or
                workflow_state.get("pending_combined_rfq") or
                workflow_state.get("extracted_entities") or
                workflow_state.get("incomplete_products") or
                workflow_state.get("complete_products")
            )

            if tracked_message and tracked_intent_result and not has_existing_rfq_data:
                logger.debug(f"Using tracked meaningful message instead of button synthetic message: '{str(tracked_message)[:50]}...'")
                # Clear the tracked message since we're using it
                workflow_state.pop("last_meaningful_message", None)
                workflow_state.pop("last_meaningful_intent_result", None)
                # Set flag to prevent re-tracking this message (prevents recursion loop)
                workflow_state["meaningful_message_used"] = True

                message_to_process = tracked_message
                intent_result = tracked_intent_result

                # Trigger RFQ creation flow with the tracked message
                return await self.purchase_intent_handler.handle_purchase_intent(
                    user, session, message_to_process, intent_result,
                    self._should_use_summary_aware_extraction
                )
            else:
                if has_existing_rfq_data:
                    logger.debug(f"Ignoring tracked message - session has existing RFQ data, starting fresh")
                else:
                    logger.debug(f"No tracked meaningful message found - directly activating sectioned RFQ")

                # Clear any stale meaningful message
                workflow_state.pop("last_meaningful_message", None)
                workflow_state.pop("last_meaningful_intent_result", None)

                # Directly activate sectioned RFQ workflow without entity extraction
                # This avoids wasting an API call on a synthetic message
                return await self._activate_sectioned_rfq(user, session)
        
        elif button_id == "search_bfs":
            # Handle BFS search coming soon with profile selection message
            user_role = user.role.value if hasattr(user.role, 'value') else user.role
            user_email = getattr(user, 'email', 'your profile')
            
            # Create the profile selection message
            profile_message = (
                "Got it! You’re looking to check if items are available in stock.\n\n"
                "🔍 *BFS Search coming soon!*"
            )

            if user_role == "buyer":
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
            else:  # seller or other roles
                buttons_config = [
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
            
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                profile_message,
                buttons_config,
                session_id=session
            )
            
            return {"status": "bfs_coming_soon_handled"}
        
        elif button_id == "rfq_status" or button_id == "check_rfqs":
            # Trigger RFQ status check flow
            return await self._handle_rfq_status_inquiry(user, "Check my RFQ status", session)
        
        elif button_id == "view_rfqs":
            # Trigger seller RFQ view flow
            return await self._handle_seller_flow(user, session, "View available RFQs")
        
        elif button_id == "check_submissions":
            # Trigger seller submission check flow
            return await self._handle_seller_flow(user, session, "Check my previous submissions")
        
        elif button_id == "contact_support" or button_id == "other_support" or button_id == "get_support":
            # Trigger support flow
            return await self._handle_support_request(user, "I need support",session)
        
        elif button_id == "exit":
            # Trigger exit flow
            user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')
            # Exit service handles all session persistence (DB + Redis cleanup)
            exit_result = await self.exit_service.handle_exit_intent(user_phone, session)
            return exit_result

        # Handle Excel confirmation buttons
        elif button_id in ["confirm_excel", "cancel_excel"]:
            return await self._handle_excel_confirmation_button(user, session, button_id)
        
        # Handle cancel workflow confirmation buttons
        elif button_id in ["confirm_cancel", "decline_cancel"]:
            return await self._handle_cancel_confirmation_button(user, session, button_id)
        
        # Handle exit confirmation buttons
        elif button_id in ["confirm_exit", "decline_exit"]:
            return await self._handle_exit_confirmation_button(user, session, button_id)

        # Handle modify button by simulating "modify" message
        elif button_id == "no_rfq":
            return await self._process_text_message(user, session, "modify")
        
        # Handle continue button from optional fields
        elif button_id == "continue_rfq":
            result = await self.confirmation_handler.handle_confirmation_button(user, session, button_id)

            # CRITICAL: Save session after continue button to persist pending_rfq/pending_combined_rfq
            # The confirmation handler moves from pending_optional_* to pending_* but doesn't save
            # Without this save, the next "Confirm" click will fail because pending_rfq won't exist
            await self.session_manager.save_session(session)
            logger.debug(f"Session saved after continue button handling for {user.phone_number}")

        # Handle continue button from modification clarification (confirms no changes needed)
        elif button_id == "confirm_no_changes":
            result = await self.confirmation_handler.handle_confirmation_button(user, session, button_id)
            # Session will be cleared by confirmation handler after RFQ creation
            # CRITICAL: Save session to persist the cleared workflow_state to Redis
            await self.session_manager.save_session(session)
            logger.debug(f"Handled confirm_no_changes button for {user.phone_number}")

            return result

        # Check if this is a confirmation button response
        elif button_id == "confirm_rfq":
            # Route to confirmation handler
            result = await self.confirmation_handler.handle_confirmation_button(user, session, button_id)

            # Handle session completion if RFQs were created
            if result.get("status") == "multiple_rfqs_created":
                # APPEND session to database (preserves history from previous RFQs)
                from app.database import DatabaseManager
                db_manager = DatabaseManager()
                try:
                    db_manager.append_session_data({
                        'session_id': session.session_id,
                        'external_user_id': session.external_user_id,
                        'workflow_type': WorkflowType.rfq_submitted.value,
                        'outcome': session.outcome.value if session.outcome else None,
                        'workflow_state': session.workflow_state,
                        'conversation_history': session.conversation_history,
                        'extracted_entities': session.extracted_entities,
                        'rfq_ids': session.rfq_ids if hasattr(session, 'rfq_ids') else None,
                        'product_items': session.product_items if hasattr(session, 'product_items') else None,
                        'retention_date': session.retention_date,
                        'last_activity_at': session.last_activity_at,
                        'completed_at': session.completed_at
                    })
                    logger.debug(f"Appended completed RFQ session {session.session_id} to database (button handler)")
                finally:
                    db_manager.close()

                # Clear Redis (session complete) - CRITICAL: Delete old session to prevent stale data leak
                from app.redis_db import get_session_redis_service
                from app.config import get_settings
                settings = get_settings()
                if settings.redis_session_storage_enabled:
                    redis_session = get_session_redis_service()
                    await redis_session.delete_session(session.session_id)
                    logger.debug(f"Deleted completed session {session.session_id} from Redis (button handler)")
            else:
                # Save session if RFQ was not created (e.g., error occurred)
                await self.session_manager.save_session(session)
                logger.debug(f"Session saved after confirmation button handling for {user.phone_number}")

            return result

        # Check if this is an email confirmation button response during authentication
        elif button_id in ["confirm_email", "reject_email"]:
            # Route to authentication email confirmation handler
            return await self._handle_authentication_email_button(user, session, button_id)

        # Default button handling
        return {"status": "button_handled", "button_id": button_id}

    async def _handle_authentication_email_button(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle email confirmation button responses during authentication."""
        logger.debug(f"Authentication email button response from {user.phone_number}: {button_id}")
        
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

    async def _handle_excel_confirmation_button(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle Excel confirmation button responses."""
        logger.debug(f"Excel confirmation button response from {user.phone_number}: {button_id}")
        
        try:
            if button_id == "confirm_excel":
                # User confirmed - simulate "confirm" response
                return await self._handle_excel_confirmation_response(user, session, "confirm", None)
            elif button_id == "cancel_excel":
                # User cancelled - simulate "cancel" response
                return await self._handle_excel_confirmation_response(user, session, "cancel", None)
            else:
                return {"status": "unknown_excel_button", "button_id": button_id}
                
        except Exception as e:
            logger.error(f"Error handling Excel confirmation button: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_cancel_confirmation_button(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle cancel workflow confirmation button responses."""
        logger.debug(f"Cancel confirmation button response from {user.phone_number}: {button_id}")

        try:
            user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

            # Map button ID to confirmation result
            # confirm_cancel -> Yes, decline_cancel -> No
            is_confirmed = button_id == "confirm_cancel"

            cancel_result = await self.cancel_service.handle_cancel_confirmation(user_phone, session, is_confirmed,user)

            if cancel_result.get("status") == "cancelled":
                # Workflow was cancelled, save session and return
                await self.session_manager.save_session(session, session.workflow_type)
                return cancel_result
            elif cancel_result.get("status") == "cancelled_aborted":
                # User declined, save session and continue with normal flow
                await self.session_manager.save_session(session, session.workflow_type)
                # Don't return - let the message processing continue
                logger.debug("User declined cancel via button - would need to re-process as normal message")
                # Since this is a button response, we can't continue the flow here
                # We need to return a special status to trigger the workflow to continue
                return cancel_result

        except Exception as e:
            logger.error(f"Error handling cancel confirmation button: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_exit_confirmation_button(self, user: User, session: ConversationSession, button_id: str) -> Dict[str, Any]:
        """Handle exit confirmation button responses."""
        logger.debug(f"Exit confirmation button response from {user.phone_number}: {button_id}")

        try:
            user_phone = session.external_user_id if session.external_user_id else user.phone_number.lstrip('+')

            # Map button ID to confirmation result
            # confirm_exit -> Yes, decline_exit -> No
            is_confirmed = button_id == "confirm_exit"

            exit_result = await self.exit_service.handle_exit_confirmation(user_phone, session, is_confirmed, user)

            return exit_result

        except Exception as e:
            logger.error(f"Error handling exit confirmation button: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_list_response(self, user: User, session: ConversationSession, list_id: str) -> Dict[
        str, Any]:  # noqa: ARG002
        """Handle list selection responses."""
        # Implementation for list responses
        logger.debug(f"List response from {user.phone_number}: {list_id}")
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
        
        # Use technical failure handler for clean exit
        from app.utils.technical_failure_handler import handle_technical_failure
        await handle_technical_failure(
            user_phone=user_phone,
            error_message=f"{error_type}: {str(error)}",
            error_type=error_type
        )
        
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

            logger.debug(f"Started background summarization for session {session.session_id}")

        except Exception as e:
            logger.error(f"Error starting enhanced session completion for {session.session_id}: {e}")
            # Fallback to original method
            await self._handle_session_completion_fallback(session)

    async def _handle_session_completion_fallback(self, session: ConversationSession) -> None:
        """Fallback session completion method (original logic)."""
        try:
            await self.chat_summary_service.generate_session_summary(session)
            await self.daily_summary_service.generate_daily_summary(session.external_user_id)
            logger.debug(f"Generated summaries for completed session {session.session_id}")
        except Exception as e:
            logger.error(f"Error generating summaries for session {session.session_id}: {e}")

    async def _show_auth_placeholder(self, user_phone: str) -> None:
        """Show authentication placeholder message for new sessions."""
        try:
            message = "Registration system is in progress, continuing with your request..."
            await self.whatsapp_service.send_message(user_phone, message)
            logger.debug(f"Sent authentication placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending authentication placeholder: {e}")

    async def _show_seller_flow_placeholder(self, user_phone: str) -> None:
        """Show seller flow placeholder message."""
        try:
            message = "Seller flow is in progress. Our team will contact you shortly with RFQ opportunities."
            await self.whatsapp_service.send_message(user_phone, message)
            logger.debug(f"Sent seller flow placeholder to {user_phone}")
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

            logger.debug(f"Sent BFS availability placeholder to {user_phone}")
        except Exception as e:
            logger.error(f"Error sending BFS availability placeholder: {e}")

    async def _handle_rfq_status_inquiry(self, user: User, message: str, session: ConversationSession = None) -> Dict[str, Any]:
        # Help 1 : how to handle session here, like what data needs to be save in db and how to do it
        """Handle RFQ status inquiry requests."""
        return await self.rfq_status_service.handle_rfq_status_inquiry(user, message, session)

    async def _handle_seller_flow(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
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
            logger.info(f"message is:{message}")
            workflow_state = session.workflow_state or {}
            current_seller_state = workflow_state.get("seller_workflow_state")

            logger.debug(f"ChatService: Handling seller flow - Current state: {current_seller_state}")

            # Normalize workflow type
            workflow_type = (
                session.workflow_type.value
                if getattr(session.workflow_type, "value", None)
                else str(session.workflow_type) if session.workflow_type
                else None
            )


            # Check if user is already inside seller workflow
            is_existing = workflow_type == "seller_rfq_view"


            # --- Helper: send message once only ---
            async def send_response(result , session):
                msg = result.get("message")
                if msg and not result.get("message_already_sent"):
                    await self.whatsapp_service.send_message(user.phone_number, msg , session_id = session)
                    self.session_manager.add_message_to_history(session, "assistant", msg)

            # --- Single call to seller workflow handler ---
            result = await self.seller_service.handle_seller_workflow(
                user, session, message, intent_result
            )

            logger.info(f"seller rsult:{result}")


            # --- Workflow init logic (only for new flows) ---
            if not is_existing and result.get("success"):
                if result.get("workflow_step") in [
                    "display_rfqs_to_seller",
                    "show_subscription_plans"
                ]:
                    WorkflowManager.set_workflow_type(
                        session, WorkflowType.seller_rfq_view, caller="seller_flow_init"
                    )
                    await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            # Handle display_rfqs_to_seller workflow step with buttons
            if result.get("workflow_step") in ["display_rfqs_to_seller" , "general_seller_response","awaiting_plan_selection"] :
                buttons_config = [
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]
                
                msg = result.get("message")
                logger.info(f"mg to be send:{msg}")
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    msg,
                    buttons_config
                )
                # Save assistant response in history
                if msg:
                    self.session_manager.add_message_to_history(session, "assistant", msg)
                    await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            elif result.get("workflow_step") in ["general_seller_response","awaiting_plan_selection"]:

                buttons_config = [
                    {"id": "view_rfqs", "title": "Request Active RFQs"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]

                msg = result.get("message")
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    msg,
                    buttons_config
                )
                # Save assistant response in history
                if msg:
                    self.session_manager.add_message_to_history(session, "assistant", msg)
                    await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            elif result.get("workflow_step") in ["payment_link_generated","rfq_emails_processed"]:

                buttons_config = [
                    {"id": "view_rfqs", "title": "Request Active RFQs"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "get_support", "title": "Get Support Info"}
                ]

                msg = result.get("message")
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    msg,
                    buttons_config,
                )
                # Save assistant response in history
                if msg:
                    self.session_manager.add_message_to_history(session, "assistant", msg)
                    await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)
                    logger.info("after saving now clearing ")
                    # Clear workflow state without confirmation (automatic cancellation)
                    await self.cancel_service._clear_workflow_state(session)

            elif result.get("workflow_step") in ["no_credits_available","invalid_plan_selection","show_subscription_plans"]:
                buttons_config = [
                    {"id": "cancel_no_credits", "title": "Cancel"}
                ]
                msg = result.get("message")
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    msg,
                    buttons_config
                )
                # Save assistant response in history
                if msg:
                    self.session_manager.add_message_to_history(session, "assistant", msg)
                    await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)
            else:
                # Send message normally
                await send_response(result , session)



            return {"status": "seller_flow_processed", **result}

        except Exception as e:
            logger.error(f"Error in seller flow handler: {e}")

            # AI-generated contextual response fallback
            try:
                err_msg = await self.response_helpers.generate_seller_contextual_response({
                    "workflow_state": "seller_flow_error",
                    "error_message": str(e),
                })
                await self.whatsapp_service.send_message(user.phone_number, err_msg)
            except Exception as resp_err:
                logger.error(f"Error generating seller error response: {resp_err}")
                await self.whatsapp_service.send_message(
                    user.phone_number,
                    f"I encountered an issue processing your request. Please contact {self.settings.support_email}"
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
            extraction = await self.openai_service.extract_entities(message=message, workflow_type="rfq_status_check")
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

    async def _handle_seller_rfq_intimation_flow(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """
        Handle seller RFQ intimation workflow (from "I'm Interested" button).

        This workflow handles:
        - switch_prompt: User is responding to account switch confirmation
        - otp: User is entering OTP for seller authentication

        Args:
            user: User making the request
            session: Current conversation session
            message: User's message

        Returns:
            Dict with status and result, or None if workflow should not be handled
        """
        try:
            from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler

            auth_stage = session.workflow_state.get("auth_stage")
            logger.debug(f"Handling seller RFQ intimation flow for {user.phone_number}, stage={auth_stage}")

            if auth_stage == "switch_prompt":
                # User is responding to account switch prompt
                handler = SellerRFQInterestHandler(
                    whatsapp_service=self.whatsapp_service,
                    authentication_service=self.authentication_service,
                    session_manager=self.session_manager,
                    otp_service=self.authentication_service.otp_service
                )
                result = await handler.handle_switch_response(user.phone_number, session, message)
                await self.session_manager.save_session(session)
                return result

            elif auth_stage == "otp":
                # User is entering OTP for seller authentication
                otp_result = await self.authentication_service.handle_email_otp_validation(
                    user.phone_number, message, session
                )

                if otp_result.get("status") == "otp_valid":
                    # OTP validated - send portal link
                    handler = SellerRFQInterestHandler(
                        whatsapp_service=self.whatsapp_service,
                        authentication_service=self.authentication_service,
                        session_manager=self.session_manager,
                        otp_service=self.authentication_service.otp_service
                    )
                    result = await handler.handle_otp_validated(user.phone_number, session)
                    await self.session_manager.save_session(session)
                    return result
                else:
                    # OTP not yet valid - authentication service handles retry messages
                    await self.session_manager.save_session(session)
                    return otp_result

            else:
                # Unknown stage or workflow just started - let it continue
                logger.warning(f"Unknown auth_stage in seller_rfq_intimation: {auth_stage}")
                return None

        except Exception as e:
            logger.error(f"Error handling seller RFQ intimation flow: {e}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Sorry, there was an error processing your request. Please try again."
            )
            # Clear workflow on error
            session.workflow_type = None
            session.workflow_state = {}
            await self.session_manager.save_session(session)
            return {"status": "error", "error": str(e)}

    async def _handle_contextual_interaction(self, user: User, session: ConversationSession, message: str, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle contextual interactions with SAFE session management.

        This method processes contextual intents like session_inquiry, contextual_reference,
        and alternative_request. CRITICAL: Uses WorkflowManager to prevent
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

            logger.debug(f"[CONTEXTUAL_INTERACTION] Intent: {context_understanding.get('user_intent', 'unknown')}, "
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

                logger.debug(f"[CONTEXTUAL_ACTION] Type: {action_type} - {action_description}")

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
                            logger.debug(f"[SAFE_STATE_CHANGE] Changed workflow state to: collecting")

                        elif action_type == 'change_workflow_type':
                            # Use WorkflowManager with validation
                            WorkflowManager.transition_workflow(
                                session,
                                WorkflowType.general_inquiry,
                                validate=True,
                                caller='contextual_interaction'
                            )
                            logger.debug(f"[SAFE_TRANSITION] Changed workflow type to: general_inquiry")

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
                    logger.debug(f"[UNKNOWN_ACTION] Processed contextual action: {action_type}")

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
                logger.debug("[CONTEXTUAL_INTERACTION] Session updated and saved")

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
                        logger.debug(f"Added new entity: {new_entity.get('product_name', 'Unknown')}")
                        
                elif action == 'update':
                    # Update existing entities that match product name
                    product_name = entity_update.get('product_name', '')
                    for entity in extracted_entities:
                        if entity.get('product_name', '').lower() == product_name.lower():
                            # Update fields that are provided
                            for field in ['quantity', 'specifications', 'preferred_brand', 'delivery_date']:
                                if entity_update.get(field):
                                    entity[field] = entity_update[field]
                            logger.debug(f"Updated entity: {product_name}")
                            break
                            
                elif action == 'remove':
                    # Remove entities that match product name
                    product_name = entity_update.get('product_name', '')
                    extracted_entities = [e for e in extracted_entities if e.get('product_name', '').lower() != product_name.lower()]
                    logger.debug(f"Removed entity: {product_name}")
            
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
            logger.debug(f"Rolled back session to stage: {target_stage}")
            
        except Exception as e:
            logger.error(f"Error rolling back to stage {target_stage}: {e}")
    
    async def _clear_session_fields(self, session: ConversationSession, fields_to_clear: List[str]) -> None:
        """Clear specific fields from session."""
        try:
            workflow_state = session.workflow_state or {}
            
            for field in fields_to_clear:
                if field in workflow_state:
                    del workflow_state[field]
                    logger.debug(f"Cleared session field: {field}")
                    
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
            logger.debug("Restarted workflow - cleared session data")
            
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
                    logger.debug(f"Updated user message with intent: {intent} (confidence: {confidence})")
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
                "buy_something", "sell_something", "greeting",
                "modification_request", "reference_request", "rfq_status_check"
            ]

            # Skip OTP-like messages and auth/registration flow responses
            if self._is_auth_flow_response(message_content, intent, session):
                logger.debug(f"Skipping auth/registration flow response: '{str(message_content)[:50]}...' with intent: {intent}")
                return

            # Skip account selection responses during role switch
            if session.workflow_state and session.workflow_state.get("pending_role_switch"):
                logger.debug(f"Skipping account selection response during role switch: '{str(message_content)[:50]}...'")
                return

            # Skip profile selection responses (e.g., "1", "2") during authentication
            if session.workflow_state and session.workflow_state.get("profile_selection_stage"):
                logger.debug(f"Skipping profile selection response during auth: '{str(message_content)[:50]}...'")
                return

            # CRITICAL: Check if this message was already used/consumed to prevent recursion
            # If meaningful_message_used flag is set, don't re-track the same message
            if session.workflow_state and session.workflow_state.get("meaningful_message_used"):
                logger.debug(f"Skipping re-tracking of already used meaningful message: '{str(message_content)[:50]}...'")
                # Clear the flag for next time
                session.workflow_state.pop("meaningful_message_used", None)
                return

            # Check if we already have a meaningful message preserved (e.g., after options presented)
            existing_meaningful = session.workflow_state.get("last_meaningful_message") if session.workflow_state else None

            # Track meaningful messages, but preserve existing ones in post-auth state
            if intent in meaningful_intents and confidence > 50:
                session.workflow_state = session.workflow_state or {}

                # REMOVED: Button response check that was causing issues when users type button text manually
                # The button handler itself will set the meaningful_message_used flag to prevent re-tracking
                # For all other cases, we should track the new meaningful message

                # Always track/update the meaningful message (button handler will use the flag to prevent re-tracking)
                session.workflow_state["last_meaningful_message"] = message_content

                # Create a safe copy of intent_result without circular references
                # This prevents maximum recursion depth errors during workflow_state processing
                safe_intent_result = {
                    "intent": intent_result.get("intent"),
                    "confidence": intent_result.get("confidence", 0),
                    "all_intent_scores": intent_result.get("all_intent_scores", {}),
                    "context_analysis": intent_result.get("context_analysis", {}),
                    "reasoning": intent_result.get("reasoning"),
                    "suggested_clarification": intent_result.get("suggested_clarification"),
                    "success": intent_result.get("success", True)
                }

                session.workflow_state["last_meaningful_intent_result"] = safe_intent_result
                logger.debug(f"Tracked meaningful message: '{str(message_content)[:50]}...' with intent: {intent} (confidence: {confidence}%)")

        except Exception as e:
            logger.error(f"Error tracking meaningful message: {e}")

    def _is_auth_flow_response(self, message_content: str, intent: str, session: ConversationSession = None) -> bool:
        """Check if this message is an auth/registration flow response that shouldn't be processed as business intent."""
        try:
            # Handle non-string message content (like interactive button responses)
            if not isinstance(message_content, str):
                return False

            message_lower = message_content.lower().strip()

            # OTP patterns (4-6 digits, possibly with spaces)
            import re
            if re.match(r'^\s*\d{4,6}\s*$', message_content.strip()):
                return True

            # Confirmation responses - BUT NOT if we have pending optional fields or confirmations
            # These responses might be answers to optional field questions or RFQ confirmations
            if message_lower in ["yes", "y", "no", "n", "confirm", "correct", "ok", "restart", "wrong", "incorrect", "skip", "exit"]:
                # Check if user has active workflow with pending optional fields or confirmations
                if session and session.workflow_state:
                    has_pending_optional = bool(
                        session.workflow_state.get("pending_optional_rfq") or
                        session.workflow_state.get("pending_optional_combined_rfq")
                    )
                    has_pending_confirmations = bool(
                        session.workflow_state.get("pending_combined_rfq") or
                        session.workflow_state.get("pending_rfq")
                    )

                    # If there are pending optional fields or confirmations, this is NOT an auth flow response
                    if has_pending_optional or has_pending_confirmations:
                        logger.debug(f"Message '{message_lower}' detected with pending optional/confirmation - NOT treating as auth flow response")
                        return False

                # Otherwise, treat as auth flow response
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
                logger.debug(f"Using tracked meaningful message: '{str(tracked_message)[:50]}...' with intent: {tracked_intent_result.get('intent')}")

                # Clean up the tracked message since we're using it now
                workflow_state.pop("last_meaningful_message", None)
                workflow_state.pop("last_meaningful_intent_result", None)

                return tracked_message, tracked_intent_result
            else:
                # No tracked message - check if current message is an auth flow response
                current_intent = current_intent_result.get('intent', '')
                if self._is_auth_flow_response(current_message, current_intent, session):
                    logger.debug(f"No meaningful message tracked and current message is auth flow response. Creating default general inquiry.")
                    # Return a default general inquiry since user completed auth/registration without meaningful business request
                    default_message = "What can I assist you with today?"
                    default_intent_result = {
                        "intent": "greeting",
                        "confidence": 75,
                        "context_analysis": {"conversation_stage": "post_auth_default", "show_buttons": True}
                    }
                    return default_message, default_intent_result
                else:
                    # Current message is meaningful, use it
                    logger.debug(f"No tracked meaningful message found, using current message: '{str(current_message)[:50]}...'")
                    return current_message, current_intent_result

        except Exception as e:
            logger.error(f"Error getting meaningful message after auth: {e}")
            # Fallback to current message
            return current_message, current_intent_result
