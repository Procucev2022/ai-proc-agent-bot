"""
Registration Service for WhatsApp Bot.

Handles complete user registration flow including:
- Entity-based data collection for buyers and sellers
- Registration API integration
- Email OTP verification
- Domain approval for buyers
"""

import logging
from typing import Dict, Any, List, Optional
from app.schemas.user import User
from app.models import ConversationSession, UserType, WorkflowType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.entity_service import EntityService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.confirmation_service import ConfirmationService
from app.services.workflow_manager import WorkflowManager
from app.procucev_apis.register_apis import RegisterAPIService
from app.schemas.user import User, BuyerRegistrationSchema, SellerRegistrationSchema, normalize_phone_number
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.services.support_notification_service import SupportNotificationService
from app.services.domain_check_service import DomainCheckService
from app.services.otp_service import OTPService

logger = logging.getLogger(__name__)


class RegistrationService:
    """Handles user registration workflow with entity-based data collection."""
    
    def __init__(self, whatsapp_service: WhatsAppService = None,
                 openai_service: OpenAIService = None,
                 entity_service: EntityService = None,
                 response_helpers: ResponseHelpers = None,
                 confirmation_service: ConfirmationService = None,
                 session_manager=None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.openai_service = openai_service or OpenAIService()
        self.entity_service = entity_service or EntityService()
        self.response_helpers = response_helpers or ResponseHelpers(self.openai_service)
        self.confirmation_service = confirmation_service
        self.register_api_service = RegisterAPIService()
        self.auth_redis_service = get_auth_redis_service()
        self.support_notification_service = SupportNotificationService()
        self.session_manager = session_manager  # Set this first
        self.domain_check_service = DomainCheckService(self.openai_service, self.whatsapp_service, self.session_manager)
        self.authentication_helpers = AuthenticationHelpers()
        self.otp_service = OTPService(self.register_api_service, self.whatsapp_service, self.support_notification_service)
    
    async def initiate_registration(self, user_phone: str, session: ConversationSession,
                                  user_type: str, message: str = "") -> Dict[str, Any]:
        """
        Initiate registration flow for new users.
        
        Args:
            user_phone: User's phone number
            session: Current conversation session
            user_type: "buyer" or "seller"
            message: Optional initial message from user
        """
        try:
            logger.info(f"RegistrationService: Initiating registration for user_type: {user_type}")
            logger.info(f"RegistrationService: Session workflow_state: {session.workflow_state}")
            
            session.user_type = UserType.buyer if user_type == "buyer" else UserType.seller
            logger.info(f"RegistrationService: Set session.user_type to: {session.user_type}")
            
            if user_type == "buyer":
                intro_message = self.authentication_helpers.generate_registration_message(BuyerRegistrationSchema, "Buyer", False)
            else:  # seller
                intro_message = self.authentication_helpers.generate_registration_message(SellerRegistrationSchema, "Seller", False)

            logger.info(f"RegistrationService: Sending intro message: {intro_message}")
            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, intro_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, intro_message)
            
            result = {
                "status": "registration_initiated",
                "user_type": user_type,
                "stage": "data_collection",
                "message": "Registration flow started"
            }
            
            logger.info(f"RegistrationService: Registration initiated successfully: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Registration initiation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "registration_initiation_error", str(e), session)
    
    async def handle_registration_data_collection(self, user_phone: str, message_content: str,
                                                session: ConversationSession) -> Dict[str, Any]:
        """Handle registration data collection using entity extraction with context awareness."""
        try:
            user_type = session.workflow_state.get("user_type", "buyer")
            logger.info(f"Starting registration data collection for {user_phone}, user_type: {user_type}")
            
            # Check for exit commands first
            if await self._check_exit_command(message_content):
                return await self._handle_registration_exit(user_phone, session)
            
            # Build context from conversation history for better entity extraction
            conversation_context = self._build_registration_context(session, message_content)
            
            # Extract entities from user message with full context 
            workflow_type = f"registration_{user_type}"  # Fix naming: registration_buyer or registration_seller
            entity_result = await self.entity_service.extract_entities(
                message_content, 
                context={
                    "workflow_type": workflow_type,
                    "conversation_history": conversation_context,
                    "registration_stage": "data_collection"
                },
                workflow_type=workflow_type
            )
            logger.info(f"Complete: Entity extraction for workflow_type: {workflow_type} & result: {entity_result}")
            
            # Step 1: Session pre-context - Initialize registration_entities if not exists
            if "registration_entities" not in session.workflow_state:
                session.workflow_state["registration_entities"] = {}
            
            # Step 2: Merging - Get existing entities from session
            existing_entities = session.workflow_state.get("registration_entities", {})
            logger.info(f"Step 2: Existing entities before merge: {existing_entities}")

            
            # Step 2 Continue: Merging extracted entities with existing ones
            if entity_result.get("entities"):
                logger.info(f"Step 2: Processing extracted entities: {entity_result['entities']}")
                # Smart merge - prioritize new data but preserve existing
                for key, value in entity_result["entities"].items():
                    if value and str(value).strip():  # Only update if new value is meaningful
                        existing_entities[key] = str(value).strip()
                        logger.info(f"Step 2: Merged entity {key}: {value}")
                session.workflow_state["registration_entities"] = existing_entities
                session.workflow_state["last_activity_at"] = utc_now().isoformat()
                logger.info(f"Step 2 Complete: Updated registration entities: {existing_entities}")
            else:
                logger.warning("Step 3: No entities extracted from message")
            
            # Step 4: Dynamic schema-based field validation - flexible to add/remove fields based on schemas
            user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
            missing_fields = AuthenticationHelpers.get_missing_fields(user_schema, existing_entities)
            
            logger.info(f"Step 4: Schema-based missing fields for {user_type}: {missing_fields}")
            
            if missing_fields:
                # Step 5: Response generation for missing fields
                logger.info(f"Step 5: Generating response for missing fields")
                questions = await self._generate_contextual_registration_questions(
                    missing_fields, user_type, existing_entities, message_content
                )
                logger.info(f"Step 5: Generated questions: {questions}")
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, questions, session)
                else:
                    await self.whatsapp_service.send_message(user_phone, questions)
                
                # Step 6: Session update - ensure workflow_type stays as registration
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                session.workflow_state["registration_stage"] = "data_collection"
                session.workflow_state["last_activity_at"] = utc_now().isoformat()
                
                logger.info(f"Step 6: Session update - data collection in progress")
                return {
                    "status": "data_collection_in_progress",
                    "missing_fields": missing_fields,
                    "collected_entities": existing_entities
                }
            else:
                # Step 5: All data collected, show confirmation with buttons
                logger.info(f"Step 5: All data collected, requesting confirmation")
                await self._send_confirmation_with_buttons(user_phone, existing_entities, user_type, session)
                
                # Step 6: Session update - mark as awaiting confirmation
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                session.workflow_state["registration_entities"] = existing_entities
                session.workflow_state["registration_stage"] = "confirmation"
                session.workflow_state["last_activity_at"] = utc_now().isoformat()
                logger.info(f"Step 6: Awaiting registration confirmation: {existing_entities}")
                
                return {
                    "status": "awaiting_confirmation",
                    "collected_entities": existing_entities
                }
                
        except Exception as e:
            logger.error(f"Registration data collection error: {e}", exc_info=True)
            return await self._redirect_to_support(user_phone, "registration_data_error", str(e), session)

    def _build_registration_context(self, session: ConversationSession, current_message: str,
                                    history_limit: int = 20 ) -> str:
        """Build context from conversation history for better entity extraction."""
        try:
            messages = session.conversation_history.get("messages", [])
            context_messages = []

            # Include last few messages from both user and assistant
            for msg in messages[-history_limit:]:
                content = msg.get("content", "").strip()
                if content:
                    context_messages.append(content)

            # Add current message if not empty
            current_message = current_message.strip()
            if current_message:
                context_messages.append(current_message)

            return " ".join(context_messages)
        except Exception as e:
            logger.error(f"Error building registration context: {e}")
            return current_message

    async def _generate_confirmation_message(self, entities: Dict, user_type: str) -> str:
        """Generate confirmation message showing all collected details dynamically."""
        user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
        return AuthenticationHelpers.generate_confirmation_message_dynamic(user_schema, entities)
    
    async def _send_confirmation_with_buttons(self, user_phone: str, entities: Dict, user_type: str, session: ConversationSession) -> None:
        """Send confirmation message with interactive buttons."""
        message = await self._generate_confirmation_message(entities, user_type)
        
        buttons = [
            {"id": "confirm_registration", "title": "Confirm"},
            {"id": "restart_registration", "title": "Restart"}
        ]
        
        # Try to send with buttons first
        button_response = await self.whatsapp_service.send_configurable_buttons(
            user_phone, message, buttons
        )
        
        if not button_response.success:
            # Fallback to text message if buttons fail
            fallback_message = message + "\n\nReply 'YES' to confirm or 'NO' to restart registration."
            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, fallback_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, fallback_message)
    
    async def handle_registration_confirmation(self, user_phone: str, message_content: str,
                                             session: ConversationSession) -> Dict[str, Any]:
        """Handle user confirmation response from buttons or keywords."""
        try:
            # Check for exit keywords first before processing confirmation
            if message_content.lower().strip() in ["exit", "quit", "stop", "cancel"]:
                logger.info(f"Exit keyword detected during registration confirmation: '{message_content}'")
                from app.services.exit_service import ExitService
                exit_service = ExitService(self.whatsapp_service, self.authentication_service, self.session_manager)
                return await exit_service.handle_exit_intent(user_phone, session)
            
            user_type = session.workflow_state.get("user_type", "buyer")
            entities = session.workflow_state.get("registration_entities", {})
            
            # Check for exit commands first
            if await self._check_exit_command(message_content):
                return await self._handle_registration_exit(user_phone, session)
            
            # Check for button responses first
            button_response = self._parse_button_response(message_content)
            if button_response:
                confirmation = button_response
                logger.info(f"Button confirmation: {confirmation}")
            else:
                # Fallback to keyword/AI parsing
                confirmation = await self.confirmation_service.parse_confirmation(message_content) if self.confirmation_service else None
                logger.info(f"Keyword/AI confirmation: {confirmation}")
            
            if confirmation == "yes":
                # User confirmed, proceed based on user type
                logger.info(f"User confirmed registration details")
                
                # Submit registration first, then OTP verification
                logger.info(f"Submitting {user_type} registration to API first")
                result = await self._submit_registration(user_phone, session, entities, user_type)
                
                if result.get("status") == "registration_completed":
                    # Registration successful, now start OTP verification
                    email = entities.get("email")
                    session.workflow_state["registration_stage"] = "email_otp"
                    session.workflow_state["pending_registration_data"] = entities
                    session.workflow_state["otp_email"] = email
                    session.workflow_state["otp_retry_count"] = 0
                    
                    WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                    
                    # Send OTP for email verification
                    return await self.otp_service.send_otp(user_phone, email, session)
                else:
                    return result
                    
            elif confirmation == "no":
                # User wants to restart
                logger.info(f"User requested registration restart")
                session.workflow_state = {
                    "registration_stage": "data_collection",
                    "user_type": user_type,
                    "registration_entities": {},
                    "last_activity_at": utc_now().isoformat()
                }
                
                if user_type == "buyer":
                    restart_message = self.authentication_helpers.generate_registration_message(BuyerRegistrationSchema, "Buyer", False)
                else:
                    restart_message = self.authentication_helpers.generate_registration_message(SellerRegistrationSchema, "Seller", False)
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, restart_message, session)
                else:
                    await self.whatsapp_service.send_message(user_phone, restart_message)
                
                return {
                    "status": "registration_restarted",
                    "user_type": user_type
                }
            else:
                # Unclear response, ask again with buttons
                await self._send_clarification_with_buttons(user_phone, session)
                
                return {
                    "status": "awaiting_confirmation",
                    "message": "clarification_requested"
                }
                
        except Exception as e:
            logger.error(f"Registration confirmation error: {e}")
            return await self._redirect_to_support(user_phone, "confirmation_error", str(e), session)
    
    def _parse_button_response(self, message_content) -> Optional[str]:
        """Parse button response from WhatsApp interactive message."""
        # Handle both string and dictionary inputs
        if isinstance(message_content, dict):
            # Extract button ID from interactive message structure
            button_reply = message_content.get("button_reply", {})
            button_id = button_reply.get("id", "")
            if button_id:
                message_lower = button_id.lower().strip()
            else:
                return None
        elif isinstance(message_content, str):
            message_lower = message_content.lower().strip()
        else:
            return None
        
        # Check for button IDs or titles
        if message_lower in ["confirm_registration", "confirm"]:
            return "yes"
        elif message_lower in ["restart_registration", "restart"]:
            return "no"
        
        return None
    
    async def _send_clarification_with_buttons(self, user_phone: str, session: ConversationSession) -> None:
        """Send clarification message with buttons."""
        message = "Please confirm your registration details."
        
        buttons = [
            {"id": "confirm_registration", "title": "Confirm"},
            {"id": "restart_registration", "title": "Restart"}
        ]
        
        # Try to send with buttons first
        button_response = await self.whatsapp_service.send_configurable_buttons(
            user_phone, message, buttons
        )
        
        if not button_response.success:
            # Fallback to text message if buttons fail
            fallback_message = "Please reply 'yes' to confirm your details or 'no' to restart registration."
            if self.session_manager:
                await self.session_manager.send_and_track_message(user_phone, fallback_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, fallback_message)
    
    async def _generate_contextual_registration_questions(self, missing_fields: List[str], 
                                                        user_type: str, existing_entities: Dict,
                                                        current_message: str) -> str:
        """Generate contextual questions dynamically based on schema."""
        user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
        return AuthenticationHelpers.generate_registration_questions_dynamic(
            user_schema, existing_entities, missing_fields
        )
    
    async def _submit_registration(self, user_phone: str, session: ConversationSession,
                                 entities: Dict, user_type: str) -> Dict[str, Any]:
        """Submit registration to API using dynamic schema-based payload generation."""
        try:
            # Get schema dynamically based on user type
            user_schema = BuyerRegistrationSchema if user_type == "buyer" else SellerRegistrationSchema
            
            # Build payload dynamically from schema
            registration_data = AuthenticationHelpers.build_registration_payload_dynamic(
                user_schema, entities, user_phone
            )
            
            # Call appropriate API based on user type
            if user_type == "buyer":
                result = await self.register_api_service.register_buyer(registration_data)
            else:
                result = await self.register_api_service.register_seller(registration_data)
            
            if result.get("statusCode") in ["1001", "200"] or result.get("status") == "Success":
                # Registration successful - extract user_id from response and store it
                logger.info(f"{user_type.title()} registration API successful, continuing to OTP flow")
                
                # Extract user_id from API response if available
                user_id = None
                if result.get("data") and isinstance(result["data"], dict):
                    user_id = result["data"].get("userId") or result["data"].get("id")
                elif result.get("type") and isinstance(result["type"], dict):
                    user_id = result["type"].get("userId") or result["type"].get("id")
                
                # Store user_id in entities for domain check
                if user_id:
                    entities["user_id"] = user_id
                    session.workflow_state["registration_entities"] = entities
                    logger.info(f"Stored user_id {user_id} for domain check")
                else:
                    logger.warning(f"No user_id found in registration response: {result}")
                
                return {
                    "status": "registration_completed",
                    "user_type": user_type,
                    "continue_to_otp": True,
                    "user_id": user_id
                }
            else:
                # Registration failed
                error_msg = result.get("message", "Registration failed")
                
                # Send email notification to support team for registration failure
                await self.support_notification_service.notify_registration_failed(
                    entities.get("name") or entities.get("full_name", "User"),
                    entities.get("email", "unknown"),
                    user_phone,
                    user_type
                )
                
                if "already exists" in error_msg.lower():
                    return await self._redirect_to_support(user_phone, "user_already_exists", error_msg, session)
                else:
                    return await self._redirect_to_support(user_phone, "registration_failed", error_msg, session)
                    
        except Exception as e:
            logger.error(f"Registration submission error: {e}")
            return await self._redirect_to_support(user_phone, "registration_submission_error", str(e), session)
    


    async def handle_registration_otp_validation(self, user_phone: str, message_content: str,
                                               session: ConversationSession) -> Dict[str, Any]:
        """Handle OTP validation for registration."""
        try:
            logger.info(f"REGISTRATION_SERVICE: Starting OTP validation for {user_phone}")
            
            # Check for exit commands first
            if await self._check_exit_command(message_content):
                return await self._handle_registration_exit(user_phone, session)
            
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
            
            # Use OTP service for validation
            logger.info(f"REGISTRATION_SERVICE: Calling OTP service for {user_phone}")
            otp_result = await self.otp_service.handle_user_message(user_phone, message_content, session)
            logger.info(f"REGISTRATION_SERVICE: OTP service result: {otp_result}")
            
            # If OTP is valid, complete registration
            if otp_result.get("status") == "otp_valid":
                entities = session.workflow_state.get("pending_registration_data", {})
                user_type = session.workflow_state.get("user_type", "buyer")
                
                logger.info(f"REGISTRATION_SERVICE: OTP valid for {user_type} {user_phone}, proceeding with registration completion")
                
                # Store user session token after successful registration
                session_stored = await self._store_user_session_after_registration(user_phone, entities, user_type)
                
                if session_stored:
                    logger.info(f"REGISTRATION_SERVICE: User session stored successfully for {user_type} {user_phone}")
                else:
                    logger.error(f"REGISTRATION_SERVICE: Failed to store user session for {user_type} {user_phone}")
                
                if user_type == "buyer":
                    logger.info(f"REGISTRATION_SERVICE: Processing buyer registration completion for {user_phone}")
                    
                    # Check domain approval after email verification
                    user_id = entities.get("user_id")
                    if user_id:
                        logger.info(f"REGISTRATION_SERVICE: Checking domain approval for user_id: {user_id}")
                        from app.services.verification_check_service import VerificationCheckService
                        verification_service = VerificationCheckService(None, self.otp_service, self.whatsapp_service)
                        domain_result = await verification_service._check_domain_approval(user_id)
                        logger.info(f"REGISTRATION_SERVICE: Domain check result: {domain_result}")
                        
                        if domain_result.get("approved"):
                            logger.info(f"REGISTRATION_SERVICE: ✅ OTP SUCCESS + DOMAIN APPROVED for {user_phone} -> Registration successful")
                            # Domain approved - redirect to main flow without messages
                            return {
                                "status": "registration_completed",
                                "user_type": "buyer",
                                "redirect_to_main_flow": True,
                                "approved": True,
                                "registration_flow_complete": False
                            }
                        else:
                            logger.info(f"REGISTRATION_SERVICE: ❌ OTP SUCCESS + DOMAIN FAILED for {user_phone} -> Registration success contact support")
                            # Domain not approved - send pending message
                            pending_message = "Registration successful—thank you! Our team will get in touch with you shortly to complete your onboarding so that you can raise RFQs. In the meantime please let us know if you want us to support you with anything else?"
                            
                            if self.session_manager:
                                await self.session_manager.send_and_track_message(user_phone, pending_message, session)
                            else:
                                await self.whatsapp_service.send_message(user_phone, pending_message)
                            
                            # Exit the flow
                            from app.services.exit_service import ExitService
                            exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
                            await exit_service.handle_exit_intent(user_phone, session)
                            
                            return {
                                "status": "redirect_to_support",
                                "reason": "domain_not_approved",
                                "exit_completed": True
                            }
                    else:
                        logger.warning(f"REGISTRATION_SERVICE: ❌ OTP SUCCESS + NO USER_ID for {user_phone} -> Registration success contact support")
                        # No user_id found - redirect to support
                        pending_message = "Registration successful—thank you! Our team will get in touch with you shortly to complete your onboarding so that you can raise RFQs. In the meantime please let us know if you want us to support you with anything else?"
                        
                        if self.session_manager:
                            await self.session_manager.send_and_track_message(user_phone, pending_message, session)
                        else:
                            await self.whatsapp_service.send_message(user_phone, pending_message)
                        
                        # Exit the flow
                        from app.services.exit_service import ExitService
                        exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
                        await exit_service.handle_exit_intent(user_phone, session)
                        
                        return {
                            "status": "redirect_to_support",
                            "reason": "missing_user_id",
                            "exit_completed": True
                        }
                else:
                    logger.info(f"REGISTRATION_SERVICE: ✅ OTP SUCCESS for SELLER {user_phone} -> Registration successful")
                    # Sellers: Email verified successfully, proceed to main flow
                    
                    # Send success message only
                    final_success_message = "Registration successful! You can proceed."
                    
                    if self.session_manager:
                        await self.session_manager.send_and_track_message(user_phone, final_success_message, session)
                    else:
                        await self.whatsapp_service.send_message(user_phone, final_success_message)
                    
                    # Sellers don't need domain approval - redirect to main flow
                    return {
                        "status": "registration_completed",
                        "user_type": "seller",
                        "redirect_to_main_flow": True,
                        "approved": True,
                        "registration_flow_complete": False
                    }
            
            # Handle OTP service redirect to support
            elif otp_result.get("status") == "redirect_to_support":
                logger.warning(f"REGISTRATION_SERVICE: OTP service redirected to support for {user_phone}: {otp_result.get('reason')}")
                return await self._redirect_to_support(user_phone, otp_result.get("reason", "otp_error"), "OTP validation failed", session)
            
            logger.info(f"REGISTRATION_SERVICE: Returning OTP result: {otp_result}")
            return otp_result
                
        except Exception as e:
            logger.error(f"REGISTRATION_SERVICE: Registration OTP validation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "otp_validation_error", str(e), session)

    async def _store_user_session_after_registration(self, user_phone: str, entities: Dict, user_type: str) -> bool:
        """Store user session token after successful registration."""
        try:
            # Create User from registration data
            user_details = User(
                id=f"reg_{user_phone}_{int(utc_now().timestamp())}",  # Generate unique ID
                name=entities.get("name") or entities.get("full_name", ""),
                email=entities.get("email", ""),
                phone_number=user_phone,
                self_client=user_type == "buyer",
                role=user_type,
                is_registered=True,
                company_name=entities.get("company_name", ""),
                unique_id=f"reg_{user_phone}"
            )
            
            # Store session data in Redis
            session_data = user_details.dict()
            session_data["authenticated_at"] = utc_now().isoformat()
            session_data["registration_source"] = "whatsapp_bot"
            
            success = await self.auth_redis_service.store(user_phone, session_data, expiry_seconds=86400)  # 24 hours
            
            if success:
                logger.info(f"User session stored successfully for {user_phone} after registration")
            else:
                logger.error(f"Failed to store user session for {user_phone} after registration")
            
            return success
            
        except Exception as e:
            logger.error(f"Error storing user session after registration: {e}")
            return False
    


    async def _redirect_to_support(self, user_phone: str, issue_type: str, error_details: str, session: ConversationSession = None) -> Dict[str, Any]:
        """Redirect user to support team."""
        try:
            support_message = "Our support team will contact you shortly to assist with your registration."
            if self.session_manager and session:
                await self.session_manager.send_and_track_message(user_phone, support_message, session)
            else:
                await self.whatsapp_service.send_message(user_phone, support_message)
            
            logger.error(f"Support redirect for {user_phone}: {issue_type} - {error_details}")
            
            return {
                "status": "redirected_to_support",
                "issue_type": issue_type,
                "support_ticket_created": True
            }
            
        except Exception as e:
            logger.error(f"Support redirect error: {e}")
            return {"status": "error", "error": "Failed to redirect to support"}
    
    async def _check_exit_command(self, message_content: str) -> bool:
        """Check if user wants to exit registration."""
        return message_content.lower().strip() in ["exit", "quit", "stop", "cancel"]
    
    async def _handle_registration_exit(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle exit during registration."""
        from app.services.exit_service import ExitService
        exit_service = ExitService(self.whatsapp_service, None, self.session_manager, None)
        return await exit_service.handle_exit_intent(user_phone, session)