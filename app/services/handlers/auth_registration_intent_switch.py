"""
Authentication/Registration Intent Switch Handler.

Handles intent switching between 4 combinations:
1. Buyer Authentication
2. Seller Authentication  
3. Buyer Registration
4. Seller Registration
"""

import logging
from typing import Dict, Any
from app.models import ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class AuthRegistrationIntentSwitch:
    """Handles intent switching for authentication/registration flows."""
    
    def __init__(self, whatsapp_service: WhatsAppService):
        self.whatsapp_service = whatsapp_service
        self.openai_service = OpenAIService()
    
    async def should_handle_auth_reg_switch(self, session: ConversationSession, 
                                          new_intent: str, user_type: str) -> bool:
        """
        Check if should handle switch between auth/registration combinations.
        
        Returns True if:
        1. User has active auth/registration workflow
        2. New combination is different from current
        3. Not already in switch state
        """
        current_workflow = session.workflow_type
        current_user_type = session.workflow_state.get("user_type", "buyer")
        
        # Check if in auth/registration workflow
        if not current_workflow or str(current_workflow).lower() not in ["authentication", "registration"]:
            return False
        
        # Don't handle if already in switch state
        if session.workflow_state.get("pending_auth_reg_switch"):
            return False
        
        # Determine current and new combinations
        current_combo = f"{str(current_workflow).lower()}_{current_user_type}"
        new_combo = self._get_target_combination(new_intent, user_type)
        
        logger.info(f"Intent switch check: {current_combo} -> {new_combo}")
        
        return current_combo != new_combo
    
    def _get_target_combination(self, intent: str, user_type: str) -> str:
        """Get target combination based on intent and user type."""
        if intent == "sell_something":
            return "authentication_seller"
        elif intent == "buy_something":
            return "authentication_buyer"
        elif intent == "register_seller":
            return "registration_seller"
        elif intent == "register_buyer":
            return "registration_buyer"
        else:
            # Infer from user_type and intent context
            if "register" in intent.lower():
                return f"registration_{user_type}"
            else:
                return f"authentication_{user_type}"
    
    async def handle_auth_reg_switch_choice(self, user_phone: str, session: ConversationSession,
                                          message: str, new_intent: str, user_type: str) -> Dict[str, Any]:
        """Present user with choice between current and new combination."""
        
        current_combo = self._get_current_combination_desc(session)
        new_combo = self._get_new_combination_desc(new_intent, user_type)
        
        # Store switch state
        session.workflow_state["pending_auth_reg_switch"] = {
            "new_intent": new_intent,
            "new_user_type": user_type,
            "new_message": message,
            "current_workflow": session.workflow_type,
            "current_user_type": session.workflow_state.get("user_type", "buyer"),
            "timestamp": utc_now().isoformat()
        }
        
        choice_message = (
            f"I see you want to {new_combo}. "
            f"This will stop your current {current_combo}.\n\n"
            "1. Continue current process\n"
            f"2. Switch to {new_combo}\n\n"
            "Please choose 1 or 2:"
        )
        
        await self.whatsapp_service.send_message(user_phone, choice_message)
        
        return {"status": "auth_reg_switch_choice_presented"}
    
    async def handle_auth_reg_switch_response(self, user_phone: str, session: ConversationSession,
                                            message: str) -> Dict[str, Any]:
        """Handle user's response to switch choice."""
        
        pending_switch = session.workflow_state.get("pending_auth_reg_switch")
        if not pending_switch:
            return {"status": "no_pending_switch"}
        
        # Analyze user choice
        choice = await self._analyze_switch_choice(message)
        
        if choice == "continue_current":
            # Clear switch state and continue current
            del session.workflow_state["pending_auth_reg_switch"]
            return {"status": "continue_current_workflow"}
        
        elif choice == "switch_to_new":
            # Extract new combination details
            new_intent = pending_switch["new_intent"]
            new_user_type = pending_switch["new_user_type"]
            new_message = pending_switch["new_message"]
            
            # Clear switch state
            del session.workflow_state["pending_auth_reg_switch"]
            
            # Clear current workflow
            self._clear_current_workflow(session)
            
            return {
                "status": "switch_to_new_combination",
                "new_intent": new_intent,
                "new_user_type": new_user_type,
                "new_message": new_message,
                "target_workflow": self._get_target_workflow(new_intent)
            }
        
        else:
            # Unclear response, ask again
            await self.whatsapp_service.send_message(
                user_phone, 
                "Please choose 1 to continue current process or 2 to switch:"
            )
            return {"status": "clarification_requested"}
    
    async def _analyze_switch_choice(self, message: str) -> str:
        """Analyze user's choice using simple logic first, then AI if needed."""
        message_lower = message.lower().strip()
        
        # Simple keyword matching
        if any(word in message_lower for word in ["1", "continue", "current", "first"]):
            return "continue_current"
        elif any(word in message_lower for word in ["2", "switch", "new", "second"]):
            return "switch_to_new"
        
        # Use AI for complex responses
        try:
            prompt = f"""
User message: "{message}"

The user was asked to choose between:
1. Continue current process
2. Switch to new process

Analyze their response and return only:
- "continue_current" if they want option 1
- "switch_to_new" if they want option 2
- "unclear" if ambiguous
"""
            
            response = self.openai_service.generate_response(
                context={"prompt": prompt},
                query_results=[]
            )
            
            if "continue_current" in response.lower():
                return "continue_current"
            elif "switch_to_new" in response.lower():
                return "switch_to_new"
            else:
                return "unclear"
                
        except Exception as e:
            logger.error(f"AI choice analysis failed: {e}")
            return "unclear"
    
    def _get_current_combination_desc(self, session: ConversationSession) -> str:
        """Get description of current combination."""
        workflow = str(session.workflow_type).lower()
        user_type = session.workflow_state.get("user_type", "buyer")
        
        if workflow == "authentication":
            return f"{user_type} login"
        elif workflow == "registration":
            return f"{user_type} registration"
        else:
            return "current process"
    
    def _get_new_combination_desc(self, intent: str, user_type: str) -> str:
        """Get description of new combination."""
        if intent == "sell_something":
            return "seller login"
        elif intent == "buy_something":
            return "buyer login"
        elif intent == "register_seller":
            return "seller registration"
        elif intent == "register_buyer":
            return "buyer registration"
        else:
            return f"{user_type} process"
    
    def _get_target_workflow(self, intent: str) -> str:
        """Get target workflow type based on intent."""
        if intent in ["sell_something", "buy_something"]:
            return "authentication"
        elif intent in ["register_seller", "register_buyer"]:
            return "registration"
        else:
            return "authentication"
    
    def _clear_current_workflow(self, session: ConversationSession) -> None:
        """Clear current workflow state."""
        session.workflow_type = None
        session.workflow_state = {}
        session.outcome = 'abandoned'
    
    async def handle_account_change_confirmation(self, user, session, message: str, target_role: str, original_intent: str) -> Dict[str, Any]:
        """Handle unified confirmation for account switch OR registration."""
        try:
            current_role = getattr(user, 'role', 'buyer') if user and hasattr(user, 'role') else None
            is_authenticated = user and user.is_registered if user else False

            # Store account change state
            session.workflow_state["pending_account_change"] = {
                "target_role": target_role,
                "current_role": current_role,
                "original_intent": original_intent,
                "original_message": message,
                "is_authenticated": is_authenticated,
                "timestamp": utc_now().isoformat()
            }

            # Generate confirmation message with options
            if is_authenticated:
                confirmation_message = f"You're currently logged in as a {current_role}.\n\n"

                # Show available options based on target role
                options = []

                if target_role != current_role:
                    # Cross-role options
                    options.append(f"1. Switch to existing {target_role} account")
                    options.append(f"2. Register new {target_role} account")
                    options.append("3. Continue with current account")
                else:
                    # Same role options
                    options.append(f"1. Switch to different {target_role} account")
                    options.append(f"2. Register new {target_role} account")
                    options.append("3. Continue with current account")

                confirmation_message += "\n".join(options)
                confirmation_message += "\n\nPlease reply with 1, 2, or 3:"
            else:
                # User not authenticated - direct to registration
                confirmation_message = (
                    f"Would you like to register as a {target_role}?\n\n"
                    "1. Yes, register now\n"
                    "2. No, continue without registration\n\n"
                    "Please reply with 1 or 2:"
                )

            await self.whatsapp_service.send_message(
                user.phone_number,
                confirmation_message
            )

            return {"status": "account_change_confirmation_requested"}

        except Exception as e:
            logger.error(f"Error handling account change confirmation: {e}")
            return {"status": "error", "error": str(e)}

    async def handle_account_switch_confirmation(self, user, session, message: str, target_role: str) -> Dict[str, Any]:
        """Handle account switch confirmation for same-role switches (buyer->different buyer, seller->different seller)."""
        try:
            current_role = getattr(user, 'role', 'buyer')

            # Store account switch state
            session.workflow_state["pending_account_switch"] = {
                "target_role": target_role,
                "current_role": current_role,
                "original_message": message,
                "timestamp": utc_now().isoformat()
            }

            # Generate confirmation message for same-role switch
            confirmation_message = (
                f"You're currently logged in as a {current_role}. "
                f"What would you like to do with your {target_role} accounts?"
            )

            # Send confirmation with text options including registration
            confirmation_message += (
                f"\n\n1. Switch to different {target_role} account"
                f"\n2. Register new {target_role} account"
                "\n3. Continue with current account\n\n"
                "Please reply with 1, 2, or 3:"
            )

            await self.whatsapp_service.send_message(
                user.phone_number,
                confirmation_message
            )

            return {"status": "account_switch_confirmation_requested"}

        except Exception as e:
            logger.error(f"Error handling account switch confirmation: {e}")
            return {"status": "error", "error": str(e)}

    async def handle_role_switch_confirmation(self, user, session, message: str, target_role: str) -> Dict[str, Any]:
        """Handle role switch confirmation for authenticated users."""
        try:
            current_role = getattr(user, 'role', 'buyer')
            
            # Store role switch state
            session.workflow_state["pending_role_switch"] = {
                "target_role": target_role,
                "current_role": current_role,
                "original_message": message,
                "timestamp": utc_now().isoformat()
            }
            
            # Generate confirmation message
            role_descriptions = {
                "buyer": "create RFQs and purchase products",
                "seller": "view and respond to RFQs"
            }

            confirmation_message = (
                f"You're currently logged in as a {current_role}. "
                f"Would you like to switch to {target_role} mode to {role_descriptions[target_role]}?"
            )

            # Send confirmation with text options including registration
            confirmation_message += (
                f"\n\n1. Switch to existing {target_role} account"
                f"\n2. Register new {target_role} account"
                "\n3. Continue with current account\n\n"
                "Please reply with 1, 2, or 3:"
            )
            
            await self.whatsapp_service.send_message(
                user.phone_number,
                confirmation_message
            )
            
            return {"status": "role_switch_confirmation_requested"}
            
        except Exception as e:
            logger.error(f"Error handling role switch confirmation: {e}")
            return {"status": "error", "error": str(e)}
    
    async def handle_role_switch_response(self, user, session, message: str, authentication_service) -> Dict[str, Any]:
        """Handle user's response to role switch confirmation with three options."""
        try:
            pending_switch = session.workflow_state.get("pending_role_switch")
            if not pending_switch:
                return {"status": "no_pending_role_switch"}

            target_role = pending_switch["target_role"]
            current_role = pending_switch["current_role"]
            original_message = pending_switch["original_message"]

            # Use AI to validate three-option response
            confirmation_result = await self._ai_validate_three_option_response(
                message, current_role, target_role, "role_switch"
            )

            if confirmation_result == "switch_existing":
                # Option 1: Switch to existing account - clear token and restart authentication
                logger.info(f"User chose to switch to existing {target_role} account")

                return await self._handle_switch_to_existing_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif confirmation_result == "register_new":
                # Option 2: Register new account - redirect to registration
                logger.info(f"User chose to register new {target_role} account")

                return await self._handle_register_new_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif confirmation_result == "continue_current":
                # Option 3: Continue with current account
                logger.info(f"User chose to continue with current {current_role} account")

                # Clear pending switch state
                del session.workflow_state["pending_role_switch"]

                continue_message = f"Continuing with your current {current_role} account. How can I help you today?"
                await self.whatsapp_service.send_message(user.phone_number, continue_message)

                return {
                    "status": "role_switch_declined",
                    "original_message": original_message,
                    "continue_with_original_intent": True
                }

            else:
                # Unclear response - ask for clarification
                clarification_message = (
                    f"Please choose one of the options:\n\n"
                    f"1. Switch to existing {target_role} account\n"
                    f"2. Register new {target_role} account\n"
                    f"3. Continue with current account\n\n"
                    "Reply with 1, 2, or 3:"
                )
                await self.whatsapp_service.send_message(user.phone_number, clarification_message)

                return {"status": "role_switch_clarification_requested"}

        except Exception as e:
            logger.error(f"Error handling role switch response: {e}")
            return {"status": "error", "error": str(e)}

    async def handle_account_switch_response(self, user, session, message: str, authentication_service) -> Dict[str, Any]:
        """Handle user's response to account switch confirmation with three options."""
        try:
            pending_switch = session.workflow_state.get("pending_account_switch")
            if not pending_switch:
                return {"status": "no_pending_account_switch"}

            target_role = pending_switch["target_role"]
            current_role = pending_switch["current_role"]
            original_message = pending_switch["original_message"]

            # Use AI to validate three-option response
            confirmation_result = await self._ai_validate_three_option_response(
                message, current_role, target_role, "account_switch"
            )

            if confirmation_result == "switch_existing":
                # Option 1: Switch to different account of same role
                logger.info(f"User chose to switch to different {target_role} account")

                return await self._handle_switch_to_existing_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif confirmation_result == "register_new":
                # Option 2: Register new account of same role
                logger.info(f"User chose to register new {target_role} account")

                return await self._handle_register_new_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif confirmation_result == "continue_current":
                # Option 3: Continue with current account
                logger.info(f"User chose to continue with current {current_role} account")

                # Clear pending switch state
                del session.workflow_state["pending_account_switch"]

                continue_message = f"Continuing with your current {current_role} account. How can I help you today?"
                await self.whatsapp_service.send_message(user.phone_number, continue_message)

                return {
                    "status": "account_switch_declined",
                    "original_message": original_message,
                    "continue_with_original_intent": True
                }

            else:
                # Unclear response - ask for clarification
                clarification_message = (
                    f"Please choose one of the options:\n\n"
                    f"1. Switch to different {target_role} account\n"
                    f"2. Register new {target_role} account\n"
                    f"3. Continue with current account\n\n"
                    "Reply with 1, 2, or 3:"
                )
                await self.whatsapp_service.send_message(user.phone_number, clarification_message)

                return {"status": "account_switch_clarification_requested"}

        except Exception as e:
            logger.error(f"Error handling account switch response: {e}")
            return {"status": "error", "error": str(e)}

    async def _ai_validate_role_confirmation_response(self, message: str, current_role: str, target_role: str) -> str:
        """Use AI to validate role confirmation response (yes/no/unclear)."""
        try:
            response = self.openai_service.generate_response(
                context={
                    "message": message,
                    "current_role": current_role,
                    "target_role": target_role
                },
                query_results=[],
                prompt_file="intent_confirmation/role_confirmation_validation"
            )

            response = response.strip().lower()

            if "yes" in response:
                return "yes"
            elif "no" in response:
                return "no"
            else:
                return "unclear"

        except Exception as e:
            logger.error(f"AI role confirmation validation error: {e}")
            # Fallback to simple pattern matching
            message_lower = message.lower().strip()

            if any(word in message_lower for word in ["1", "yes", "y", "switch", "confirm", "ok"]):
                return "yes"
            elif any(word in message_lower for word in ["2", "no", "n", "continue", "stay", "current"]):
                return "no"
            else:
                return "unclear"

    async def _ai_validate_three_option_response(self, message: str, current_role: str, target_role: str, context_type: str) -> str:
        """Use AI to validate three-option response (switch_existing/register_new/continue_current/unclear)."""
        try:
            # Load prompt from file
            prompt_path = "app/prompts/auth_registration_intent_switch.txt"
            with open(prompt_path, 'r', encoding='utf-8') as f:
                prompt_template = f.read()

            # Format the prompt with variables
            prompt = prompt_template.format(
                message=message,
                target_role=target_role,
                current_role=current_role,
                context_type=context_type
            )

            response = self.openai_service.generate_response(
                context={"prompt": prompt},
                query_results=[]
            )

            response = response.strip().lower()

            if "switch_existing" in response:
                return "switch_existing"
            elif "register_new" in response:
                return "register_new"
            elif "continue_current" in response:
                return "continue_current"
            else:
                return "unclear"

        except Exception as e:
            logger.error(f"AI three-option validation error: {e}")
            # Fallback to simple pattern matching
            message_lower = message.lower().strip()

            if any(word in message_lower for word in ["1", "switch", "existing", "authenticate", "login"]):
                return "switch_existing"
            elif any(word in message_lower for word in ["2", "register", "new", "create", "signup"]):
                return "register_new"
            elif any(word in message_lower for word in ["3", "continue", "current", "stay", "keep"]):
                return "continue_current"
            else:
                return "unclear"

    async def _handle_switch_to_existing_account(self, user, session, target_role: str, original_message: str, authentication_service) -> Dict[str, Any]:
        """Handle switching to existing account authentication flow."""
        try:
            # Clear user token/session
            await authentication_service.clear_user_token(user.phone_number)

            # Clear current session and create new one for reauthentication
            if hasattr(authentication_service, 'session_manager') and authentication_service.session_manager:
                # Create new session for authentication with target role
                new_session = await authentication_service.session_manager.create_session(
                    user.phone_number,
                    workflow_type="authentication",
                    user_type=target_role
                )

                # Update current session to match new session
                session.workflow_type = "authentication"
                session.workflow_state = {
                    "target_role": target_role,
                    "authentication_stage": "start",
                    "user_type": target_role
                }

            # Send confirmation message
            switch_message = f"Switched to {target_role} mode. Starting authentication process..."
            await self.whatsapp_service.send_message(user.phone_number, switch_message)

            # Determine intent based on target role
            intent = "sell_something" if target_role == "seller" else "buy_something"

            # Call authentication service with proper intent filtering
            auth_result = await authentication_service.user_authenticate(
                user.phone_number,
                original_message,
                session,
                intent=intent
            )

            if auth_result.get("success") and auth_result.get("response"):
                # Filter users by the new intent
                filtered_result = authentication_service.filter_users_by_intent(
                    auth_result["response"],
                    intent
                )

                if filtered_result.get("success"):
                    # Initiate email confirmation with filtered users
                    email_result = await authentication_service.initiate_email_confirmation(
                        user.phone_number,
                        session,
                        filtered_result["filtered_users"],
                        filtered_result["unique_emails"]
                    )

                    return {
                        "status": "switch_authentication_started",
                        "target_role": target_role,
                        "email_result": email_result
                    }
                else:
                    await self.whatsapp_service.send_message(
                        user.phone_number,
                        f"No {target_role} accounts found for your phone number. Please contact support."
                    )
                    return {"status": "no_matching_accounts", "target_role": target_role}
            else:
                await self.whatsapp_service.send_message(
                    user.phone_number,
                    "Unable to find accounts for authentication. Please contact support."
                )
                return {"status": "authentication_failed", "target_role": target_role}

        except Exception as e:
            logger.error(f"Error handling switch to existing account: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_register_new_account(self, user, session, target_role: str, original_message: str, authentication_service) -> Dict[str, Any]:
        """Handle registration flow for new account."""
        try:
            from app.services.registration_service import RegistrationService

            # Clear user token/session for registration
            await authentication_service.clear_user_token(user.phone_number)

            # Clear current session and create new one for registration
            if hasattr(authentication_service, 'session_manager') and authentication_service.session_manager:
                new_session = await authentication_service.session_manager.create_session(
                    user.phone_number,
                    workflow_type="registration",
                    user_type=target_role
                )

                # Update current session to match new session
                session.workflow_type = "registration"
                session.workflow_state = {
                    "target_role": target_role,
                    "registration_stage": "start",
                    "user_type": target_role
                }

            # Send confirmation message
            register_message = f"Starting {target_role} registration process..."
            await self.whatsapp_service.send_message(user.phone_number, register_message)

            # Initialize registration service
            registration_service = RegistrationService(self.whatsapp_service)

            # Start registration flow for target role
            registration_result = await registration_service.initiate_registration(
                user.phone_number,
                session,
                user_type=target_role
            )

            return {
                "status": "registration_started",
                "target_role": target_role,
                "registration_result": registration_result
            }

        except Exception as e:
            logger.error(f"Error handling register new account: {e}")
            return {"status": "error", "error": str(e)}