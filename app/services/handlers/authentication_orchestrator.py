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
from app.models import User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.authentication_service import AuthenticationService
from app.services.registration_service import RegistrationService
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.chat_service import ChatService
from app.services.handlers.supportService_hanlder import SupportHelpers
from app.services.chat_service import ChatService
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch

from app.schemas.user import UserDetailsSchema

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

    async def authentication_orchestrator_flow(self, user_phone: str, message_content: str, 
                                             session: ConversationSession) -> Dict[str, Any]:
        """Main authentication orchestrator function following the specified flow."""
        try:
            logger.info(f"Starting authentication flow for {user_phone}")

            # Step 1: Token validation
            user_details = await self.authentication_service.validate_token(user_phone)
            if user_details and user_details.is_registered:
                logger.info(f"Token valid - User authenticated: {user_details.id}")
                return user_details
            
            logger.info(f"Token validation failed for {user_phone}")
                      
            # Step 3: Check for existing auth/registration workflows
            workflow_type_str = str(session.workflow_type).lower() if session.workflow_type else None
            logger.info(f"Current workflow_type: {workflow_type_str}")
            
            if workflow_type_str in ["workflowtype.authentication", "authentication"]:
                logger.info("Routing to existing authentication workflow")
                return await self._handle_authentication_workflow(user_phone, message_content, session, {})
            elif workflow_type_str in ["workflowtype.registration", "registration"]:
                return await self._handle_registration_workflow(user_phone, message_content, session, {})
            
            # Step 4: Classify intent for new workflows
            from app.services.helpers.chat_service_helpers import ChatServiceHelpers
            conversation_context = ChatServiceHelpers.build_conversation_context(session, message_content)
            intent_result = self.intent_service.classify_intent(message_content, conversation_context)
           
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)
            
            # Step 5: For ambiguous or low confidence intents, try authentication first
            # If user exists, show emails with buyer/seller labels instead of asking for clarification
            if intent == "ambiguous" or confidence < 50:
                # Try to authenticate first - if user exists, they can choose email type
                auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
                
                if auth_response.get("success"):
                    # User found - show all emails with buyer/seller labels (no intent filtering)
                    raw_response = auth_response.get("response", [])
                    filter_result = self.authentication_service.filter_users_by_intent(raw_response, "general_inquiry")  # This shows all emails
                    
                    if filter_result.get("success"):
                        # Store the ambiguous message as original message
                        return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content)
                    else:
                        # No emails found - redirect to registration
                        return await self._redirect_to_registration_flow(user_phone, session, "buyer")
                else:
                    # User not found - redirect directly to registration
                    return await self._redirect_to_registration_flow(user_phone, session, "buyer")
            
            # Step 6: Handle confirmed intents  
            if intent in ["buy_something", "sell_something"]:
                # Proceed with authentication/registration flow
                pass
            elif intent == "general_inquiry":
                return await self._handle_auth_general_inquiry(user_phone, message_content)
            else:
                return await self._handle_auth_fallback(user_phone, message_content)
            
            # Step 7: Start new authentication flow with validated intent
            return await self._start_authentication_flow(user_phone, message_content, session, intent_result)
                
        except Exception as e:
            logger.error(f"Authentication orchestrator error for {user_phone}: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "authentication_orchestrator_error", str(e)
            )
    
    async def _start_authentication_flow(self, user_phone: str, message_content: str,
                                        session: ConversationSession, intent_result: Dict) -> Dict[str, Any]:
        """Start new authentication flow based on validated intent."""
        try:
            intent = intent_result.get('intent')
            confidence = intent_result.get('confidence', 0)
            logger.info(f"Starting authentication flow for intent: {intent} (confidence: {confidence}%)")
            
            # Ensure we have a valid intent before proceeding
            valid_intents = ["buy_something", "sell_something", "general_inquiry", "modification_request", 
                           "confirmation_response", "rfq_status_check", "reference_request", "ambiguous"]
            
            if intent not in valid_intents or (confidence < 50 and intent != "ambiguous"):
                logger.warning(f"Invalid or low confidence intent in auth flow: {intent} ({confidence}%)")
                return await self._handle_auth_clarification_request(user_phone, message_content)
            
            # Handle ambiguous intent - show all available emails with buyer/seller labels
            if intent == "ambiguous":
                logger.info("Processing ambiguous intent - showing all available emails")
                auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
                
                if not auth_response.get("success"):
                    logger.info("User not found for ambiguous intent - redirecting to registration")
                    return await self._redirect_to_registration_flow(user_phone, session, "buyer")
                
                # User found - show all emails with buyer/seller labels (no intent filtering)
                raw_response = auth_response.get("response", [])
                filter_result = self.authentication_service.filter_users_by_intent(raw_response, "general_inquiry")
                
                if not filter_result.get("success"):
                    return await self._redirect_to_registration_flow(user_phone, session, "buyer")
                
                return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content)
            
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
                    return await self._redirect_to_registration_flow(user_phone, session, "seller")
                
                return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content)
            
            # Step 1: API call to check if user exists
            auth_response = await self.authentication_service.user_authenticate(user_phone, message_content, session)
            
            if not auth_response.get("success"):
                # User not found - redirect to registration flow
                # Default to buyer unless explicitly sell_something intent
                user_type = "seller" if intent == "sell_something" else "buyer"
                return await self._redirect_to_registration_flow(user_phone, session, user_type)
            
            # Step 2: User found - filter based on intent (buy/sell)
            raw_response = auth_response.get("response", [])
            filter_result = self.authentication_service.filter_users_by_intent(raw_response, intent)
            
            if not filter_result.get("success"):
                # Default to buyer unless explicitly sell_something intent
                user_type = "seller" if intent == "sell_something" else "buyer"
                return await self._redirect_to_registration_flow(user_phone, session, user_type)
            
            # Step 3: User selection and email confirmation
            return await self._handle_user_selection(user_phone, session, filter_result, intent_result, message_content)
                
        except Exception as e:
            logger.error(f"Authentication flow start error: {e}")
            return await self.support_service.redirect_to_support(
                user_phone, "authentication_flow_error", str(e)
            )
    

    

    
    async def _handle_user_selection(self, user_phone: str, session: ConversationSession,
                                   filter_result: Dict, intent_result: Dict, message_content: str) -> Dict[str, Any]:
        """Handle user selection from filtered users."""
        try:
            filtered_users = filter_result.get("filtered_users", [])
            unique_emails = filter_result.get("unique_emails", [])
            
            if not unique_emails:
                # Default to buyer unless explicitly sell_something intent
                user_type = "seller" if intent_result.get("intent") == "sell_something" else "buyer"
                return await self._redirect_to_registration_flow(user_phone, session, user_type)
            
            # Store user data for email confirmation
            session.workflow_type = "authentication"
            session.workflow_state = {
                "authentication_stage": "email_confirmation",
                "filtered_users": filtered_users,
                "available_emails": unique_emails,
                "intent_result": intent_result,
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
            # Check for switch response first
            switch_result = await self._check_switch_response(user_phone, session, message_content, intent_result)
            if switch_result:
                return switch_result
            
            auth_stage = session.workflow_state.get("authentication_stage")
            logger.info(f"Handling authentication workflow stage: {auth_stage}")
            
            if auth_stage == "email_confirmation":
                logger.info(f"Processing email confirmation with message: {message_content}")
                return await self.authentication_service.handle_email_confirmation(
                    user_phone, message_content, session
                )
            elif auth_stage == "email_otp":
                return await self.authentication_service.handle_email_otp_validation(
                    user_phone, message_content, session
                )
            else:
                # No valid auth stage - classify intent and start new flow
                logger.info(f"No valid auth stage, classifying intent for new flow")
                from app.services.helpers.chat_service_helpers import ChatServiceHelpers
                conversation_context = ChatServiceHelpers.build_conversation_context(session, message_content)
                new_intent_result = self.intent_service.classify_intent(message_content, conversation_context)
                return await self._start_authentication_flow(user_phone, message_content, session, new_intent_result)
                
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
            
            registration_stage = session.workflow_state.get("registration_stage")
            current_user_type = session.workflow_state.get("user_type", "buyer")
            
            logger.info(f"AuthOrchestrator: Handling registration workflow, stage: {registration_stage}")
            logger.info(f"AuthOrchestrator: Current user_type: {current_user_type}")
            
            # ALWAYS process registration data collection for data_collection stage
            if registration_stage == "data_collection":
                logger.info(f"AuthOrchestrator: Processing registration data collection")
                
                # Ensure workflow_type stays as registration
                session.workflow_type = "registration"
                
                result = await self.registration_service.handle_registration_data_collection(
                    user_phone, message_content, session
                )
                
                logger.info(f"AuthOrchestrator: Registration result: {result}")
                return result
            elif registration_stage == "email_confirmation":
                return await self.authentication_service.handle_email_confirmation(
                    user_phone, message_content, session
                )
            elif registration_stage == "email_otp":
                return await self.authentication_service.handle_email_otp_validation(
                    user_phone, message_content, session
                )
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
        switch_intents = ["buy_something", "registration_request", "general_inquiry", "cancel", "stop"]
        return intent in switch_intents and current_stage not in ["email_otp", "confirmation"]
    
    async def _handle_intent_switch_during_auth(self, user_phone: str, session: ConversationSession,
                                              intent_result: Dict) -> Dict[str, Any]:
        """Handle intent switch during authentication."""
        try:
            intent = intent_result.get('intent')
            
            if intent == "registration_request":
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
    
    async def _handle_intent_switch_during_registration(self, user_phone: str, session: ConversationSession,
                                                      intent_result: Dict) -> Dict[str, Any]:
        """Handle intent switch during registration."""
        try:
            intent = intent_result.get('intent')
            
            if intent == "buy_something":
                # Switch to authentication
                return await self._initiate_authentication_flow(user_phone, "", session, intent_result)
            elif intent in ["cancel", "stop"]:
                # Cancel registration
                session.workflow_type = None
                session.workflow_state = {}
                await self.whatsapp_service.send_message(
                    user_phone, "Registration cancelled. How can I help you?"
                )
                return {"status": "registration_cancelled"}
            else:
                # Continue with current registration
                return {"status": "continue_registration"}
                
        except Exception as e:
            logger.error(f"Intent switch during registration error: {e}")
            return {"status": "continue_registration"}
    

    
    async def _redirect_to_registration_flow(self, user_phone: str, session: ConversationSession, user_type: str = "buyer") -> Dict[str, Any]:
        """Redirect to registration flow."""
        try:
            logger.info(f"AuthOrchestrator: Redirecting to registration flow for user_type: {user_type}")
            logger.info(f"AuthOrchestrator: Session workflow_state before redirect: {session.workflow_state}")
            
            # Preserve existing registration entities if switching within registration
            existing_entities = session.workflow_state.get("registration_entities", {})
            existing_last_activity = session.workflow_state.get("last_activity_at")
            
            # Ensure workflow_type is consistently set
            session.workflow_type = "registration"
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
            session.workflow_type = "registration"
            
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
                    target_workflow = result["target_workflow"]
                    
                    if target_workflow == "authentication":
                        return await self._start_authentication_flow(
                            user_phone, result["new_message"], session, 
                            {"intent": new_intent}
                        )
                    else:  # registration
                        return await self._redirect_to_registration_flow(
                            user_phone, session, new_user_type
                        )
                
                return result
            
            return None
            
        except Exception as e:
            logger.error(f"Switch response check error: {e}")
            return None
    
    async def _handle_auth_clarification_request(self, user_phone: str, message_content: str) -> Dict[str, Any]:
        """Handle ambiguous messages requiring clarification before proceeding to auth flow."""
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
            logger.error(f"Clarification request error: {e}")
            await self.whatsapp_service.send_message(
                user_phone, "Could you be more specific about your procurement needs?"
            )
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