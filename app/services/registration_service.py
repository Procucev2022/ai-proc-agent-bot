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
from app.schemas.user import BuyerRegistrationSchema, SellerRegistrationSchema, normalize_phone_number
from app.schemas.user import User
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.services.support_notification_service import SupportNotificationService
from app.services.auth_reg_service import AuthRegService

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
        self.auth_reg_service = AuthRegService()
        self.session_manager = session_manager  # Will be injected from ChatService
    
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
                intro_message = await self._get_buyer_introduction_message()
            else:  # seller
                intro_message = await self._get_seller_introduction_message()
            
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
            logger.info(f"Current workflow_state: {session.workflow_state}")
            logger.info(f"Message content: {message_content}")
            
            # Build context from conversation history for better entity extraction
            conversation_context = self._build_registration_context(session, message_content)
            logger.info(f"Built conversation context: {conversation_context}")
            
            # Extract entities from user message with full context
            workflow_type = f"registration_{user_type}"  # Fix naming: registration_buyer, registration_seller
            logger.info(f"Calling entity extraction with workflow_type: {workflow_type}")
            entity_result = self.entity_service.extract_entities(
                message_content, 
                context={
                    "workflow_type": workflow_type,
                    "conversation_history": conversation_context,
                    "registration_stage": "data_collection"
                },
                workflow_type=workflow_type
            )
            logger.info(f"Step 3 Complete: Entity extraction result: {entity_result}")
            
            # Step 1: Session pre-context - Initialize registration_entities if not exists
            if "registration_entities" not in session.workflow_state:
                session.workflow_state["registration_entities"] = {}
                logger.info("Step 1: Initialized registration_entities in workflow_state")
            
            # Step 2: Merging - Get existing entities from session
            existing_entities = session.workflow_state.get("registration_entities", {})
            logger.info(f"Step 2: Existing entities before merge: {existing_entities}")
            
            # Step 3: Entity extraction from user message
            logger.info(f"Step 3: Calling entity extraction with workflow_type: {user_type}_registration")
            
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
            
            # Step 4: Mapping completeness
            if user_type == "buyer":
                required_fields = ["name", "company_name", "email", "pincode"]
            else:  # seller - all buyer fields + additional seller fields
                required_fields = ["full_name", "company_name", "email", "pincode", "location", "gstin", "products_services"]
            
            logger.info(f"Step 4: Required fields for {user_type}: {required_fields}")
            missing_fields = [field for field in required_fields if not existing_entities.get(field)]
            logger.info(f"Step 4: Missing fields: {missing_fields}")
            
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
    
    async def _get_buyer_introduction_message(self) -> str:
        """Get buyer registration introduction message."""
        return (
            "Hello Buyer \n To get started, please share\n\n 1. Full name,\n 2. Company name,\n 3. Business email,\n 4. Company pincode.\n\n We’ll have you registered right away."
        )
    
    async def _get_seller_introduction_message(self) -> str:
        """Get seller registration introduction message."""
        return (
            "Hello Seller\n To get started, please share your\n 1. Full name,\n 2. Company name, \n 3. Business email, \n 4. Location with Pincode,\n 5. GSTIN number,\n 6. The products or services you offer. \n\nWe’ll have you registered right away."
        )
    
    def _build_registration_context(self, session: ConversationSession, current_message: str) -> str:
        """Build context from conversation history for better entity extraction."""
        try:
            messages = session.conversation_history.get("messages", [])
            context_messages = []
            
            # Include last few messages for context
            for msg in messages[-3:]:
                if msg.get("sender") == "user":
                    context_messages.append(msg.get("content", ""))
            
            # Add current message
            context_messages.append(current_message)
            
            return " ".join(context_messages)
        except Exception as e:
            logger.error(f"Error building registration context: {e}")
            return current_message
    
    async def _generate_confirmation_message(self, entities: Dict, user_type: str) -> str:
        """Generate confirmation message showing all collected details."""
        if user_type == "buyer":
            message = "Please confirm your registration details:\n\n"
            message += f"• Name: {entities.get('name', 'N/A')}\n"
            message += f"• Company: {entities.get('company_name', 'N/A')}\n"
            message += f"• Email: {entities.get('email', 'N/A')}\n"
            message += f"• Pincode: {entities.get('pincode', 'N/A')}\n\n"
        else:
            message = "Please confirm your registration details:\n\n"
            message += f"• Name: {entities.get('full_name', 'N/A')}\n"
            message += f"• Company: {entities.get('company_name', 'N/A')}\n"
            message += f"• Email: {entities.get('email', 'N/A')}\n"
            message += f"• Location: {entities.get('location', 'N/A')}\n"
            message += f"• Pincode: {entities.get('pincode', 'N/A')}\n"
            message += f"• GSTIN: {entities.get('gstin', 'N/A')}\n"
            message += f"• Products/Services: {entities.get('products_services', 'N/A')}\n\n"
        
        message += "📩 Please re-check your email, as an OTP will be sent to complete the registration process."
        return message
    
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
                    return await self._send_registration_otp(user_phone, session, email)
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
                
                restart_message = await self._get_buyer_introduction_message() if user_type == "buyer" else await self._get_seller_introduction_message()
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
        """Generate contextual questions based on what's already collected."""
        field_questions = {
            "name": "What's your full name?",
            "full_name": "What's your full name?",
            "company_name": "What's your company name?",
            "email": "What's your business email address?",
            "pincode": "What's your company's pincode?",
            "location": "What's your company location (city, state)?",
            "gstin": "What's your GSTIN number?",
            "products_services": "What products or services do you offer?"
        }
        
        # Acknowledge what we have
        acknowledgment = ""
        if existing_entities:
            collected = []
            if existing_entities.get("name") or existing_entities.get("full_name"):
                name = existing_entities.get("name") or existing_entities.get("full_name")
                collected.append(f"Name: {name}")
            if existing_entities.get("company_name"):
                collected.append(f"Company: {existing_entities['company_name']}")
            if existing_entities.get("email"):
                collected.append(f"Email: {existing_entities['email']}")
            if existing_entities.get("pincode"):
                collected.append(f"Pincode: {existing_entities['pincode']}")
            
            if collected:
                acknowledgment = f"Great! I have: {', '.join(collected)}\n\n"
        
        # Ask for ALL missing fields at once
        questions = []
        for field in missing_fields:  # Ask all missing fields
            if field in field_questions:
                questions.append(f"• {field_questions[field]}")
        
        if questions:
            return acknowledgment + "I still need:\n\n" + "\n".join(questions)
        else:
            return acknowledgment + "Please provide the remaining registration details."
    
    async def _submit_registration(self, user_phone: str, session: ConversationSession,
                                 entities: Dict, user_type: str) -> Dict[str, Any]:
        """Submit registration to API."""
        try:
            # Prepare registration data
            registration_data = {
                "organizationPhonenumber": normalize_phone_number(user_phone),
                "source_type": "W",
                "whatsApp": True
            }
            
            if user_type == "buyer":
                registration_data.update({
                    "name": entities.get("name"),
                    "companyName": entities.get("company_name"),
                    "email": entities.get("email"),
                    "zipCode": entities.get("pincode"),
                    "details": f"Registered via WhatsApp bot"
                })
                
                result = await self.register_api_service.register_buyer(registration_data)
            else:
                registration_data.update({
                    "companyName": entities.get("company_name"),
                    "email": entities.get("email"),
                    "gstin": entities.get("gstin"),
                    "address1": entities.get("location"),
                    "details": entities.get("products_services"),
                    "pan": "",  # Optional
                    "crn": "",  # Optional
                    "india": "true"
                })
                
                result = await self.register_api_service.register_seller(registration_data)
            
            if result.get("statusCode") in ["1001", "200"] or result.get("status") == "Success":
                # Registration successful - send confirmation message and continue to OTP
                logger.info(f"{user_type.title()} registration API successful, continuing to OTP flow")
                return {
                    "status": "registration_completed",
                    "user_type": user_type,
                    "continue_to_otp": True
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
    
    async def _send_registration_otp(self, user_phone: str, session: ConversationSession, email: str) -> Dict[str, Any]:
        """Send OTP for registration flow using real API."""
        try:
            logger.info(f"Sending OTP to email: {email} for phone: {user_phone}")
            
            # Call the actual OTP send API
            otp_response = await self.register_api_service.send_otp(email, user_phone)
            logger.info(f"OTP API response: {otp_response}")
            
            if otp_response.get("statusCode") in ["1001", "200"] or otp_response.get("status") == "Success":
                session.workflow_state["otp_retry_count"] = session.workflow_state.get("otp_retry_count", 0) + 1
                
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
                
                message = f"An OTP has been sent to your email: {email}.\nPlease enter this OTP to complete your registration."
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, message, session)
                else:
                    await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "otp_sent",
                    "stage": "email_otp",
                    "email": email
                }
            else:
                error_msg = otp_response.get("message", "Failed to send OTP")
                logger.error(f"OTP send failed: {error_msg}")
                
                # Send email notification to support team for OTP send failure
                await self.support_notification_service.notify_otp_validation_failed(
                    "User", email, user_phone
                )
                
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, f"Failed to send OTP: {error_msg}. Please contact support.", session)
                else:
                    await self.whatsapp_service.send_message(user_phone, f"Failed to send OTP: {error_msg}. Please contact support.")
                return await self._redirect_to_support(user_phone, "otp_send_failed", error_msg, session)
            
        except Exception as e:
            logger.error(f"Registration OTP send error: {e}")
            return await self._redirect_to_support(user_phone, "otp_send_error", str(e), session)

    async def handle_registration_otp_validation(self, user_phone: str, message_content: str,
                                               session: ConversationSession) -> Dict[str, Any]:
        """Handle OTP validation for registration."""
        try:
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="registration_service")
            
            otp_email = session.workflow_state.get("otp_email")
            retry_count = session.workflow_state.get("otp_retry_count", 0)
            
            # Check for resend request
            if message_content.strip().upper() == "RESEND" and retry_count < 3:
                return await self._send_registration_otp(user_phone, session, otp_email)
            
            # Extract and validate OTP using real API
            import re
            digits = re.findall(r'\d+', message_content.strip())
            
            if digits and len(digits[0]) >= 4:
                otp = digits[0]
                logger.info(f"Validating OTP: {otp} for email: {otp_email}")
                
                # Call real OTP validation API
                validation_response = await self.register_api_service.validate_otp(otp_email, otp, user_phone)
                logger.info(f"OTP validation response: {validation_response}")
                
                if validation_response.get("statusCode") in ["1001", "200"] or validation_response.get("status") == "Success":
                    # OTP valid - complete registration based on user type
                    entities = session.workflow_state.get("pending_registration_data", {})
                    user_type = session.workflow_state.get("user_type", "buyer")
                    
                    # Store user session token after successful registration
                    session_stored = await self._store_user_session_after_registration(user_phone, entities, user_type)
                    
                    if session_stored:
                        logger.info(f"User session stored successfully for {user_type} {user_phone} after registration")
                    else:
                        logger.error(f"Failed to store user session for {user_type} {user_phone} after registration")
                    
                    if user_type == "buyer":
                        # Buyers: Domain check using API
                        user_id = entities.get("user_id")  # Assuming user_id is available from registration response
                        if user_id:
                            domain_result = await self.auth_reg_service.user_domain_check(user_id)
                            
                            if domain_result.get("approved"):
                                success_message = (
                                    "Registration successful—thank you!"
                                )
                            else:
                                success_message = (
                                    "Registration successful—thank you! Our team will get in touch with you "
                                    "shortly to complete your onboarding so that you can raise RFQs. "
                                    "In the meantime please let us know if you want us to support you with anything else?"
                                )
                        else:
                            # Fallback if no user_id available
                            domain_result = {"approved": False}
                            success_message = (
                                "Registration successful—thank you! Our team will get in touch with you "
                                "shortly to complete your onboarding so that you can raise RFQs. "
                                "In the meantime please let us know if you want us to support you with anything else?"
                            )
                        
                        if self.session_manager:
                            await self.session_manager.send_and_track_message(user_phone, success_message, session)
                        else:
                            await self.whatsapp_service.send_message(user_phone, success_message)
                        
                        session.workflow_type = None
                        session.workflow_state = {
                            "recently_completed_registration": True,
                            "registration_completion_time": utc_now().isoformat(),
                            "user_type": "buyer",
                            "approved": domain_result.get("approved", False)
                        }
                        
                        return {
                            "status": "registration_completed",
                            "user_type": "buyer",
                            "approved": domain_result.get("approved", False),
                            "redirect_to_main_flow": domain_result.get("approved", False)
                        }
                    else:
                        # Sellers: Complete registration
                        success_message = (
                            "Registration successful! Our team will contact you shortly to complete "
                            "your onboarding. How can I help you in the meantime?"
                        )
                        
                        if self.session_manager:
                            await self.session_manager.send_and_track_message(user_phone, success_message, session)
                        else:
                            await self.whatsapp_service.send_message(user_phone, success_message)
                        
                        session.workflow_type = None
                        session.workflow_state = {
                            "recently_completed_registration": True,
                            "registration_completion_time": utc_now().isoformat(),
                            "user_type": "seller"
                        }
                        
                        return {
                            "status": "registration_completed",
                            "user_type": "seller",
                            "redirect_to_main_flow": True
                        }
                else:
                    # OTP invalid
                    if retry_count >= 3:
                        # Send email notification to support team for max OTP retries
                        await self.support_notification_service.notify_otp_validation_failed(
                            "User", otp_email, user_phone
                        )
                        return await self._redirect_to_support(user_phone, "max_otp_retries", "Maximum OTP attempts exceeded", session)
                    
                    message = "Invalid OTP. Please enter the correct OTP or reply 'RESEND' to get a new OTP:"
                    if self.session_manager:
                        await self.session_manager.send_and_track_message(user_phone, message, session)
                    else:
                        await self.whatsapp_service.send_message(user_phone, message)
                    
                    return {
                        "status": "otp_invalid",
                        "stage": "email_otp",
                        "retry_count": retry_count
                    }
            else:
                # Invalid OTP format
                if retry_count >= 3:
                    # Send email notification to support team for max OTP retries
                    await self.support_notification_service.notify_otp_validation_failed(
                        "User", otp_email, user_phone
                    )
                    return await self._redirect_to_support(user_phone, "max_otp_retries", "Maximum OTP attempts exceeded", session)
                
                message = "Please enter a valid OTP  or reply 'RESEND' to get a new OTP:"
                if self.session_manager:
                    await self.session_manager.send_and_track_message(user_phone, message, session)
                else:
                    await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "otp_invalid",
                    "stage": "email_otp",
                    "retry_count": retry_count
                }
                
        except Exception as e:
            logger.error(f"Registration OTP validation error: {e}")
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