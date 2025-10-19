"""
Authentication Orchestrator Handler.

Handles complete authentication flow orchestration including:
- Token validation
- User authentication via API
- Email confirmation workflow
- Email OTP validation
- Domain matching
- Support team redirection
"""

import logging
from typing import Dict, Any, Optional, List
from app.models import WorkflowType, User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService
from typing import TYPE_CHECKING
from app.services.exit_service import ExitService

if TYPE_CHECKING:
    from app.services.chat_service import ChatService
from app.services.helpers.support_helpers import SupportHelpers
from app.services.chat_service import ChatService
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.profile_selection_service import ProfileSelectionService

# UserDetailsSchema replaced with User model

logger = logging.getLogger(__name__)


class AuthenticationOrchestrator:
    """Handles authentication flow orchestration."""
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers,
                 authentication_service: AuthenticationService, registration_service: RegistrationService,
                 intent_service, support_service: SupportHelpers, chat_service: 'ChatService' = None):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.authentication_service = authentication_service
        self.registration_service = registration_service
        self.intent_service = intent_service
        self.support_service = support_service
        self.chat_service = chat_service
        self.auth_reg_switch = AuthRegistrationIntentSwitch(whatsapp_service)
        # Get OpenAI service from chat_service if available
        openai_service = chat_service.openai_service if chat_service else None
        self.profile_selection_service = ProfileSelectionService(whatsapp_service, authentication_service, openai_service)

    async def authentication_orchestrator_flow(self, user_phone: str, message_content: str,
                                             session: ConversationSession, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Main authentication orchestrator function following the specified flow."""
        try:
            logger.info(f"Starting authentication flow for {user_phone}")

            # Check if this is a role switch scenario
            role_switch_in_progress = session.workflow_state.get("role_switch_in_progress", False)
            target_user_type = session.workflow_state.get("user_type")

            if role_switch_in_progress:
                logger.info(f"Role switch in progress - forcing authentication for {target_user_type}")
                # Skip token validation and force authentication for new role
                session.workflow_state.pop("role_switch_in_progress", None)
                
                # For seller role switch, go directly to authentication flow
                if target_user_type == "seller":
                    logger.info(f"Seller role switch - starting seller authentication flow")
                    auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
                    
                    if auth_response.get("success"):
                        # User found - filter by seller role
                        raw_response = auth_response.get("response", [])
                        filter_result = self.authentication_service.filter_users_by_intent(raw_response, "sell_something")

                        if filter_result.get("success"):
                            # Store the original message for processing after authentication
                            return await self._handle_user_selection(user_phone, session, filter_result, {"intent": "sell_something"}, message_content, raw_response)
                        else:
                            # No seller emails found - redirect to registration
                            return await self._redirect_to_registration_flow(user_phone, session, "seller")
                    else:
                        # User not found for seller role - redirect to registration
                        return await self._redirect_to_registration_flow(user_phone, session, "seller")
            else:
                # Step 1: Token validation (normal flow)
                user_details = await self.authentication_service.validate_token(user_phone)
                if user_details:
                    # Check if it's a dict with verification_required or a valid user dict
                    if isinstance(user_details, dict):
                        if user_details.get("verification_required"):
                            # User needs verification - continue to auth flow
                            logger.info(f"Token valid but verification required for {user_phone}")
                        elif user_details.get("is_registered"):
                            # User is authenticated and registered
                            logger.info(f"Token valid - User authenticated: {user_details.get('id')}")
                            # Convert dict to User object for return
                            from app.schemas.user import User
                            return User.from_mixed_data(user_details)
                # If token expired, continue to check for active workflows


            # Step 4: Check for existing auth/registration workflows
            workflow_type_str = str(session.workflow_type).lower() if session.workflow_type else None
            logger.info(f"Current workflow_type: {workflow_type_str}")

            if workflow_type_str in ["workflowtype.authentication", "authentication"]:
                logger.info("Routing to existing authentication workflow")
                return await self._handle_authentication_workflow(user_phone, message_content, session, intent_result or {})
            elif workflow_type_str in ["workflowtype.registration", "registration"]:
                return await self._handle_registration_workflow(user_phone, message_content, session, intent_result or {})
            
            # Step 5: Intent result should always be provided from ChatService
            # If not provided, there's a bug in the calling code
            if not intent_result:
                logger.warning("Intent result not provided to authentication orchestrator - this should not happen")
                # Use a fallback general inquiry intent instead of re-classifying
                intent_result = {"intent": "general_inquiry", "confidence": 50}

            # Store intent result in session for use in authentication handlers
            session.workflow_state = session.workflow_state or {}
            session.workflow_state["current_intent_result"] = intent_result
           
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)

            # Handle exit intent immediately - even for unauthenticated users
            if intent == "exit_system" and confidence > 50:
                logger.info(f"Exit intent detected in auth flow with {confidence}% confidence")
                exit_service = ExitService(self.whatsapp_service, self.authentication_service,
                                         self.chat_service.session_manager if self.chat_service else None,
                                         self.chat_service.db_manager if self.chat_service else None)
                exit_result = await exit_service.handle_exit_intent(user_phone, session)
                return exit_result

            # Step 5: For ambiguous or low confidence intents, use profile selection service
            if intent == "ambiguous" or confidence < 50:
                logger.info(f"Using profile selection service for ambiguous/low confidence intent: {intent} ({confidence}%)")
                return await self.profile_selection_service.handle_profile_selection(
                    user_phone, message_content, session, intent_result
                )
            
            # Step 6: Use profile selection service for clear intents
            if intent in ["buy_something", "sell_something", "rfq_status_check", "general_inquiry", "register_account"]:
                logger.info(f"Using profile selection service for intent: {intent} ({confidence}%)")
                return await self.profile_selection_service.handle_profile_selection(
                    user_phone, message_content, session, intent_result
                )
            
            # Handle role switches with profile selection
            if role_switch_in_progress and target_user_type:
                # Create modified intent result for role switch
                target_intent = "sell_something" if target_user_type == "seller" else "buy_something"
                modified_intent_result = {
                    "intent": target_intent,
                    "confidence": 90,
                    "role_switch": True
                }
                return await self.profile_selection_service.handle_profile_selection(
                    user_phone, message_content, session, modified_intent_result
                )
            
            # Fallback for other intents
            return await self._handle_auth_fallback(user_phone, message_content)
                
        except Exception as e:
            logger.error(f"Authentication orchestrator error for {user_phone}: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "authentication_orchestrator_error", str(e)
            )
    
    async def _start_authentication_flow(self, user_phone: str, message_content: str,
                                        session: ConversationSession, intent_result: Dict) -> Dict[str, Any]:
        """Start new authentication flow based on validated intent."""
        try:
            # Get role switch variables from session state
            role_switch_in_progress = session.workflow_state.get("role_switch_in_progress", False)
            target_user_type = session.workflow_state.get("user_type")
            
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)
            logger.info(f"Starting authentication flow for intent: {intent} (confidence: {confidence}%)")
            
            # Ensure we have a valid intent before proceeding
            valid_intents = ["buy_something", "sell_something", "general_inquiry", "modification_request", 
                           "confirmation_response", "rfq_status_check", "ambiguous"]
            
            # For user-initiated switches (from auth/reg switch choices), accept even low confidence
            user_switch_in_progress = session.workflow_state.get("pending_auth_reg_switch") is not None

            if intent not in valid_intents or (confidence < 50 and intent != "ambiguous"):
                logger.warning(f"Invalid or low confidence intent in auth flow: {intent} ({confidence}%)")
                # Allow user-initiated switches and role switches to proceed
                if not (role_switch_in_progress or user_switch_in_progress):
                    return await self._handle_auth_clarification_request(user_phone, message_content)
            
            # Handle ambiguous intent - show all available emails with buyer/seller labels
            if intent == "ambiguous":
                logger.info("Processing ambiguous intent - showing all available emails")
                auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
                
                if not auth_response.get("success"):
                    logger.info("User not found for ambiguous intent - redirecting to registration")
                    # Use target user type if role switch, otherwise default to buyer
                    user_type = target_user_type if role_switch_in_progress and target_user_type else "buyer"
                    return await self._redirect_to_registration_flow(user_phone, session, user_type)
                
                # User found - show all emails with buyer/seller labels (no intent filtering)
                raw_response = auth_response.get("response", [])
                filter_result = self.authentication_service.filter_users_by_intent(raw_response, "general_inquiry")

                if not filter_result.get("success"):
                    user_type = target_user_type if role_switch_in_progress and target_user_type else "buyer"
                    return await self._redirect_to_registration_flow(user_phone, session, user_type)

                return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content, raw_response)
            
            # Handle sell_something intent - check authentication first
            if intent == "sell_something":
                logger.info("Processing sell_something intent - checking authentication first")
                auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
                
                if not auth_response.get("success"):
                    logger.info("Seller not found - redirecting to seller registration")
                    return await self._redirect_to_registration_flow(user_phone, session, "seller")
                
                # Seller found - filter and proceed with email confirmation
                raw_response = auth_response.get("response", [])
                filter_result = self.authentication_service.filter_users_by_intent(raw_response, intent)

                if not filter_result.get("success"):
                    logger.info("No seller emails found - redirecting to seller registration")
                    return await self._redirect_to_registration_flow(user_phone, session, "seller")

                return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content, raw_response)
            
            # Step 1: API call to check if user exists
            auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
            
            if not auth_response.get("success"):
                # User not found - redirect to registration flow
                # Use target user type if role switch, otherwise infer from intent
                if role_switch_in_progress and target_user_type:
                    user_type = target_user_type
                else:
                    user_type = "seller" if intent == "sell_something" else "buyer"
                return await self._redirect_to_registration_flow(user_phone, session, user_type)
            
            # Step 2: User found - filter based on intent (buy/sell)
            raw_response = auth_response.get("response", [])
            filter_result = self.authentication_service.filter_users_by_intent(raw_response, intent)

            if not filter_result.get("success"):
                # Use target user type if role switch, otherwise infer from intent
                if role_switch_in_progress and target_user_type:
                    user_type = target_user_type
                else:
                    user_type = "seller" if intent == "sell_something" else "buyer"
                return await self._redirect_to_registration_flow(user_phone, session, user_type)

            # Step 3: User selection and email confirmation
            # Store the intent result in session for use during email confirmation
            session.workflow_state = session.workflow_state or {}
            session.workflow_state["current_intent_result"] = intent_result
            return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content, raw_response)
                
        except Exception as e:
            logger.error(f"Authentication flow start error: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "authentication_flow_error", str(e)
            )
    

    

    
    async def _handle_user_selection(self, user_phone: str, session: ConversationSession,
                                   filter_result: Dict, intent_result: Dict, message_content: str,
                                   original_auth_users: List = None) -> Dict[str, Any]:
        """Handle user selection from filtered users."""
        try:
            filtered_users = filter_result.get("filtered_users", [])
            unique_emails = filter_result.get("unique_emails", [])

            logger.info(f"intent passed in handle user selection is {intent_result}")

            if not unique_emails:
                # Default to buyer unless explicitly sell_something intent
                user_type = "seller" if intent_result.get("intent") == "sell_something" else "buyer"
                return await self._redirect_to_registration_flow(user_phone, session, user_type)

            # Store user data for email confirmation
            WorkflowManager.set_workflow_type(session, WorkflowType.authentication, caller="authentication_orchestrator")
            
            # CRITICAL FIX: Preserve existing workflow_state data to prevent context loss
            existing_state = session.workflow_state or {}
            session.workflow_state = {
                **existing_state,  # Preserve all existing data
                "authentication_stage": "email_confirmation",
                "filtered_users": filtered_users,
                "available_emails": unique_emails,
                "original_auth_users": original_auth_users or filtered_users,  # Store original unfiltered users
                "intent_result": intent_result,
                "current_intent_result": intent_result,  # Store for email confirmation handler
                "original_message": message_content
            }
            
            logger.info(f"Starting email confirmation for {len(unique_emails)} emails")
            return await self.authentication_service.initiate_email_confirmation(
                user_phone, session, filtered_users, unique_emails
            )
            
        except Exception as e:
            logger.error(f"User selection error: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "user_selection_error", str(e)
            )
    
    async def _handle_authentication_workflow(self, user_phone: str, message_content: str,
                                            session: ConversationSession, intent_result: Dict) -> Dict[str, Any]:
        """Handle ongoing authentication workflow."""
        try:
            # Check authentication stage first (OTP takes priority over profile selection)
            auth_stage = session.workflow_state.get("authentication_stage")
            logger.info(f"Handling authentication workflow stage: {auth_stage}")
            
            # Handle OTP stage immediately - HIGHEST PRIORITY
            if auth_stage == "email_otp":
                logger.info(f"Processing OTP validation for message: {message_content}")
                return await self.authentication_service.handle_email_otp_validation(
                    user_phone, message_content, session
                )
            
            # Check for profile selection response - HIGHEST PRIORITY after OTP
            if session.workflow_state.get("profile_selection_stage"):
                logger.info(f"Handling profile selection response for stage: {session.workflow_state.get('profile_selection_stage')}")
                return await self.profile_selection_service.handle_profile_selection_response(
                    user_phone, message_content, session
                )
            
            # Check for switch response
            switch_result = await self._check_switch_response(user_phone, session, message_content, intent_result)
            if switch_result:
                return switch_result
            
            # Check for intent switch during authentication stages (but NOT during OTP)
            if auth_stage == "email_confirmation":
                # Use pre-classified intent from ChatService to detect potential switches
                new_intent_result = intent_result
                
                new_intent = new_intent_result.get('intent')
                confidence = new_intent_result.get('confidence', 0)
                
                # Check if user wants to switch intent during authentication
                if await self._should_handle_intent_switch_during_auth(new_intent, confidence, auth_stage, session):
                    logger.info(f"AuthOrchestrator: Intent switch detected during authentication: {new_intent}")
                    return await self._handle_intent_switch_during_auth(user_phone, session, message_content, new_intent_result)
            
            if auth_stage == "email_confirmation":
                logger.info(f"Processing email confirmation with message: {message_content}")
                # Always use the current intent result
                session.workflow_state["current_intent_result"] = intent_result
                stored_intent_result = intent_result
                
                return await self.authentication_service.handle_email_confirmation(
                    user_phone, message_content, session, stored_intent_result
                )
            else:
                # No valid auth stage - use stored intent and start new flow
                logger.info(f"No valid auth stage, starting new flow with stored intent")
                stored_intent_result = session.workflow_state.get("current_intent_result", {"intent": "general_inquiry", "confidence": 50})

                # Handle exit intent immediately before starting new flow
                intent = stored_intent_result.get('intent')
                confidence = stored_intent_result.get('confidence', 0)
                if intent == "exit_system" and confidence > 50:
                    logger.info(f"Exit intent detected in auth workflow with {confidence}% confidence")
                    exit_service = ExitService(self.whatsapp_service, self.authentication_service,
                                             self.chat_service.session_manager if self.chat_service else None,
                                             self.chat_service.db_manager if self.chat_service else None)
                    exit_result = await exit_service.handle_exit_intent(user_phone, session)
                    return exit_result

                return await self._start_authentication_flow(user_phone, message_content, session, stored_intent_result)
                
        except Exception as e:
            logger.error(f"Authentication workflow error: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "authentication_workflow_error", str(e)
            )
    
    async def _handle_registration_workflow(self, user_phone: str, message_content: str,
                                         session: ConversationSession, intent_result: Dict) -> Dict[str, Any]:
        """Handle ongoing registration workflow following the correct flow sequence."""
        try:
            # Check for switch response first
            switch_result = await self._check_switch_response(user_phone, session, message_content, intent_result)
            if switch_result:
                return switch_result
            
            # Use stored intent result or classify only if needed for switch detection
            stored_intent_result = session.workflow_state.get("current_intent_result")
            if stored_intent_result:
                new_intent_result = stored_intent_result
            else:
                # Use the intent result already classified by ChatService
                new_intent_result = intent_result
                logger.info("Using pre-classified intent from ChatService for registration workflow")

            new_intent = new_intent_result.get('intent')
            confidence = new_intent_result.get('confidence', 0)

            # Handle exit intent immediately before processing registration stages
            if new_intent == "exit_system" and confidence > 50:
                logger.info(f"Exit intent detected in registration workflow with {confidence}% confidence")
                exit_service = ExitService(self.whatsapp_service, self.authentication_service,
                                         self.chat_service.session_manager if self.chat_service else None,
                                         self.chat_service.db_manager if self.chat_service else None)
                exit_result = await exit_service.handle_exit_intent(user_phone, session)
                return exit_result
            
            registration_stage = session.workflow_state.get("registration_stage")
            current_user_type = session.workflow_state.get("user_type", "buyer")


            # Check if user wants to switch intent during registration
            if await self._should_handle_intent_switch_during_registration(new_intent, confidence, current_user_type):
                logger.info(f"AuthOrchestrator: Intent switch detected during registration: {new_intent}")
                return await self._handle_intent_switch_during_registration(user_phone, session, message_content, new_intent_result, current_user_type)
            
            logger.info(f"AuthOrchestrator: Handling registration workflow, stage: {registration_stage}")
            logger.info(f"AuthOrchestrator: Current user_type: {current_user_type}")
            
            if registration_stage in ["data_collection", "start"]:
                logger.info(f"AuthOrchestrator: Processing registration data collection (stage: {registration_stage})")

                # Ensure workflow_type stays as registration
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="authentication_orchestrator")

                result = await self.registration_service.handle_registration_data_collection(
                    user_phone, message_content, session
                )

                logger.info(f"AuthOrchestrator: Registration result: {result}")
                return result
            elif registration_stage == "email_confirmation":
                # Get stored intent result from session
                stored_intent_result = session.workflow_state.get("current_intent_result", {})
                return await self.authentication_service.handle_email_confirmation(
                    user_phone, message_content, session, stored_intent_result
                )
            elif registration_stage == "email_otp":
                result = await self.registration_service.handle_registration_otp_validation(
                    user_phone, message_content, session
                )
                
                # Check if registration is completely finished
                if result.get("status") == "registration_completed" and result.get("registration_flow_complete"):
                    logger.info(f"Registration flow completely finished - stopping further processing")
                    # Clear workflow to prevent any further processing
                    session.workflow_type = None
                    session.workflow_state = {}
                
                return result
            elif registration_stage == "domain_matching":
                return await self.authentication_service.handle_domain_matching(
                    user_phone, message_content, session
                )
            elif registration_stage == "confirmation":
                return await self.registration_service.handle_registration_confirmation(
                    user_phone, message_content, session
                )
            else:
                # No valid registration stage - start new registration
                logger.info(f"AuthOrchestrator: No valid stage, starting new registration with user_type: {current_user_type}")
                return await self._redirect_to_registration_flow(user_phone, session, current_user_type)
                
        except Exception as e:
            logger.error(f"Registration workflow error: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "registration_workflow_error", str(e)
            )
    
    async def _should_handle_intent_switch(self, intent: str, current_stage: str) -> bool:
        """Check if intent switch should be handled."""
        # Allow intent switch if user clearly wants to change direction
        switch_intents = ["buy_something", "sell_something", "registration_request", "general_inquiry", "cancel", "stop"]
        return intent in switch_intents and current_stage not in ["email_otp", "confirmation"]
    
    async def _should_handle_intent_switch_during_auth(self, new_intent: str, confidence: float, auth_stage: str, session: ConversationSession = None) -> bool:
        """Check if we should handle intent switch during authentication."""
        # NEVER interrupt OTP validation - OTP codes should always be processed as OTP
        if auth_stage == "email_otp":
            logger.info(f"Blocking intent switch during OTP stage - message should be processed as OTP")
            return False

        # Handle high-confidence switches
        if confidence < 70:
            return False

        # Check if this is actually a conflicting intent switch
        if session and new_intent in ["buy_something", "sell_something"]:
            # Get the intent of the currently selected email/user
            current_intent_result = session.workflow_state.get("intent_result", {})
            current_intent = current_intent_result.get("intent")

            # If the new intent matches the current intent, don't treat as switch
            if current_intent == new_intent:
                return False

            # Also check filtered users to see if they're already buyer/seller focused
            filtered_users = session.workflow_state.get("filtered_users", [])
            if filtered_users:
                # If we have buyer users and new intent is buy_something, don't switch
                user_types = [user.get("selfClient", True) for user in filtered_users if isinstance(user, dict)]
                if new_intent == "buy_something" and any(user_types):  # selfClient=True means buyer
                    return False
                elif new_intent == "sell_something" and not all(user_types):  # selfClient=False means seller
                    return False

        # Allow switches for conflicting intents or cancellation
        if new_intent in ["buy_something", "sell_something", "registration_request", "cancel", "stop"]:
            return True

        return False
    
    async def _handle_intent_switch_during_auth(self, user_phone: str, session: ConversationSession,
                                              message_content: str, intent_result: Dict) -> Dict[str, Any]:
        """Handle intent switch during authentication."""
        try:
            intent = intent_result.get('intent')
            
            logger.info(f"AuthOrchestrator: Handling intent switch during authentication to {intent}")
            
            if intent == "buy_something":
                # User wants to buy - offer switch to buyer authentication
                return await self.auth_reg_switch.handle_auth_reg_switch_choice(
                    user_phone, session, message_content, intent, "buyer"
                )
            elif intent == "sell_something":
                # User wants to sell - offer switch to seller authentication
                return await self.auth_reg_switch.handle_auth_reg_switch_choice(
                    user_phone, session, message_content, intent, "seller"
                )
            elif intent == "registration_request":
                # Switch to registration
                return await self._redirect_to_registration_flow(user_phone, session, "buyer")
            elif intent in ["cancel", "stop"]:
                # Cancel authentication
                session.workflow_type = None
                session.workflow_state = {}
                await self.whatsapp_service.send_message(
                    user_phone, "Authentication cancelled. How can I help you?"
                )
                return {"status": "authentication_cancelled"}
            else:
                # Continue with current authentication
                return {"status": "continue_authentication"}
                
        except Exception as e:
            logger.error(f"Intent switch during auth error: {e}")
            return {"status": "continue_authentication"}
    
    async def _should_handle_intent_switch_during_registration(self, new_intent: str, confidence: float, current_user_type: str) -> bool:
        """Check if we should handle intent switch during registration."""
        # Handle high-confidence intent switches that conflict with current registration type
        if confidence < 70:
            return False
            
        # Detect conflicting intents
        if current_user_type == "seller" and new_intent == "buy_something":
            return True
        elif current_user_type == "buyer" and new_intent == "sell_something":
            return True
        elif new_intent in ["cancel", "stop", "exit_system"]:
            return True
            
        return False
    
    async def _handle_intent_switch_during_registration(self, user_phone: str, session: ConversationSession,
                                                      message_content: str, intent_result: Dict, current_user_type: str) -> Dict[str, Any]:
        """Handle intent switch during registration."""
        try:
            intent = intent_result.get('intent')
            
            logger.info(f"AuthOrchestrator: Handling intent switch from {current_user_type} registration to {intent}")
            
            if intent == "buy_something" and current_user_type == "seller":
                # User was registering as seller but wants to buy - offer switch
                return await self.auth_reg_switch.handle_auth_reg_switch_choice(
                    user_phone, session, message_content, intent, "buyer"
                )
            elif intent == "sell_something" and current_user_type == "buyer":
                # User was registering as buyer but wants to sell - offer switch
                return await self.auth_reg_switch.handle_auth_reg_switch_choice(
                    user_phone, session, message_content, intent, "seller"
                )
            elif intent in ["cancel", "stop"]:
                # Cancel registration
                session.workflow_type = None
                session.workflow_state = {}
                await self.whatsapp_service.send_message(
                    user_phone, "Registration cancelled. How can I help you?"
                )
                return {"status": "registration_cancelled"}
            elif intent == "exit_system":
                # Handle exit during registration
                logger.info(f"Exit system intent detected during registration switch handling")
                exit_service = ExitService(self.whatsapp_service, self.authentication_service,
                                         self.chat_service.session_manager if self.chat_service else None,
                                         self.chat_service.db_manager if self.chat_service else None)
                exit_result = await exit_service.handle_exit_intent(user_phone, session)
                return exit_result
            else:
                # Continue with current registration
                return {"status": "continue_registration"}
                
        except Exception as e:
            logger.error(f"Intent switch during registration error: {e}")
            return {"status": "continue_registration"}
    

    
    async def _redirect_to_registration_flow(self, user_phone: str, session: ConversationSession, user_type: str = "buyer") -> Dict[str, Any]:
        """Redirect to registration flow."""
        try:
            # starting registration flow
            logger.info(f"AuthOrchestrator: Redirecting to registration flow for user_type: {user_type}")
            logger.info(f"AuthOrchestrator: Session workflow_state before redirect: {session.workflow_state}")
            
            # Preserve existing registration entities if switching within registration
            existing_entities = session.workflow_state.get("registration_entities", {})
            existing_last_activity = session.workflow_state.get("last_activity_at")
            
            # Ensure workflow_type is consistently set
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="authentication_orchestrator")
            session.workflow_state = {
                "registration_stage": "data_collection",
                "user_type": user_type,
                "registration_entities": existing_entities,
                "last_activity_at": existing_last_activity
            }
            
            logger.info(f"AuthOrchestrator: Set workflow_type to registration, user_type to {user_type}")
            
            logger.info(f"AuthOrchestrator: Updated session workflow_state: {session.workflow_state}")
            
            # Use registration service to initiate flow
            logger.info(f"AuthOrchestrator: Calling registration service initiate_registration")
            result = await self.registration_service.initiate_registration(
                user_phone, session, user_type
            )
            
            logger.info(f"AuthOrchestrator: Registration initiation result: {result}")
            
            # Ensure workflow_type remains registration
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="authentication_orchestrator")
            
            return {
                "status": "redirected_to_registration",
                "workflow_type": "registration",
                "user_type": user_type,
                "stage": "data_collection"
            }
            
        except Exception as e:
            logger.error(f"Registration redirect error: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "registration_redirect_error", str(e)
            )
    
    async def _check_switch_response(self, user_phone: str, session: ConversationSession,
                                   message_content: str, intent_result: Dict) -> Dict[str, Any]:
        """Check if user is responding to a switch choice."""
        try:
            # Handle auth/registration switch response
            if session.workflow_state.get("pending_auth_reg_switch"):
                result = await self.auth_reg_switch.handle_auth_reg_switch_response(
                    user_phone, session, message_content
                )
                
                if result.get("status") == "switch_to_new_combination":
                    # User chose to switch - start new workflow
                    new_intent = result["new_intent"]
                    new_user_type = result["new_user_type"]
                    new_message = result["new_message"]
                    target_workflow = result["target_workflow"]
                    
                    logger.info(f"AuthOrchestrator: User chose to switch to {target_workflow} for {new_user_type}")
                    
                    if target_workflow == "authentication":
                        return await self._start_authentication_flow(
                            user_phone, new_message, session,
                            {"intent": new_intent, "confidence": 90}
                        )
                    else:  # registration
                        return await self._redirect_to_registration_flow(
                            user_phone, session, new_user_type
                        )
                elif result.get("status") == "continue_current_workflow":
                    # User chose to continue - return to normal flow processing
                    logger.info(f"AuthOrchestrator: User chose to continue current workflow")
                    return None  # Let normal flow continue
                elif result.get("status") == "exit_requested":
                    # User chose to exit - terminate the flow
                    logger.info(f"AuthOrchestrator: User requested exit from switch dialog")
                    return result

                return result
            
            return None
            
        except Exception as e:
            logger.error(f"Switch response check error: {e}")
            return None
    
    async def _handle_auth_clarification_request(self, user_phone: str, message_content: str) -> Dict[str, Any]:
        """ 
            Handle general or ambiguous user messages by sending clarification prompts
            before proceeding with authentication or registration flow.
        """
        try:
            clarification_questions = [
                "Could you be more specific about what you're looking for?",
                "Are you continuing as buy or sell?"
            ]
            
            # Send clarification message
            response = "\n".join(clarification_questions)
            await self.whatsapp_service.send_message(user_phone, response)
            
            return {"status": "clarification_sent"}

        except Exception as e:
            logger.error(f"Clarification error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_auth_general_inquiry(self, user_phone: str, message_content: str) -> Dict[str, Any]:
        """Handle general inquiries."""
        try:
            response = "How can I help you with your procurement needs today?"
            await self.whatsapp_service.send_message(user_phone, response)
            return {"status": "general_inquiry_handled"}
        except Exception as e:
            logger.error(f"General inquiry error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_auth_fallback(self, user_phone: str, message_content: str) -> Dict[str, Any]:
        """Handle fallback cases."""
        try:
            response = "I'm here to help with your procurement needs. How can I assist you today?"
            await self.whatsapp_service.send_message(user_phone, response)
            return {"status": "fallback_handled"}
        except Exception as e:
            logger.error(f"Fallback error: {e}")
            return {"status": "error", "error": str(e)}