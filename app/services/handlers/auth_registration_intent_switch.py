"""
Authentication/Registration Intent Switch Handler.

Handles intent switching between 4 combinations:
1. Buyer Authentication
2. Seller Authentication  
3. Buyer Registration
4. Seller Registration
"""

import logging
from typing import Dict, Any, List, Optional
from app.models import WorkflowType, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.services.openai_service import OpenAIService
from app.utils.datetime_utils import utc_now
from app.services.user_cache_service import get_user_cache_service

logger = logging.getLogger(__name__)


def _get_role_string(user) -> str:
    """Helper function to extract role as string from user object, handling both enum and string values."""
    role = getattr(user, 'role', 'buyer')
    if hasattr(role, 'value'):
        # Role is an enum (UserRole.BUYER -> 'buyer')
        return role.value
    return role


class AuthRegistrationIntentSwitch:
    """Handles intent switching for authentication/registration flows."""
    
    def __init__(self, whatsapp_service: WhatsAppService):
        self.whatsapp_service = whatsapp_service
        self.openai_service = OpenAIService()
        self.user_cache_service = get_user_cache_service()
    
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
        
        # Generate dynamic message based on intent
        intent_action = "sell items" if new_intent == "sell_something" else "buy items"
        current_user_type = session.workflow_state.get("user_type", "buyer")
        
        choice_message = (
            f"I see you want to {intent_action}, but you're currently logged in as a {current_user_type.title()}. To {intent_action.split()[0]}, you'll need to switch to a {user_type.title()} profile.\n"
            f"1️⃣ Switch to existing {user_type.title()} account\n"
            f"2️⃣ Register a new {user_type.title()} account\n"
            f"3️⃣ Stay as {current_user_type.title()}\n"
            "Reply with 1, 2, or 3 to proceed."
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

        if choice == "exit":
            # Clear switch state and trigger exit flow
            del session.workflow_state["pending_auth_reg_switch"]
            # Clear entire workflow
            self._clear_current_workflow(session)
            await self.whatsapp_service.send_message(
                user_phone,
                "Thank you for using QUA. Have a great day!"
            )
            return {"status": "exit_requested"}

        elif choice == "continue_current":
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

        # Check for exit commands first
        if any(word in message_lower for word in ["exit", "quit", "stop", "cancel", "bye", "goodbye"]):
            return "exit"

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
            current_role = _get_role_string(user) if user and hasattr(user, 'role') else None
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
            current_role = _get_role_string(user)

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
        """Handle role switch confirmation with dynamic account options."""
        try:
            current_role = _get_role_string(user)
            current_email = getattr(user, 'email', None) or getattr(user, 'username', None)

            # Store role switch state
            session.workflow_state["pending_role_switch"] = {
                "target_role": target_role,
                "current_role": current_role,
                "original_message": message,
                "current_email": current_email,
                "timestamp": utc_now().isoformat()
            }

            # Get dynamic account options from cache
            target_intent = "sell_something" if target_role == "seller" else "buy_something"
            account_options = await self.user_cache_service.get_account_options_for_intent_switch(
                user.phone_number, target_intent, current_email
            )

            if not account_options:
                # Fallback to generic message if no cached data
                return await self._handle_role_switch_fallback(user, session, target_role, current_role)

            # Generate dynamic confirmation message
            role_descriptions = {
                "buyer": "create RFQs and purchase products",
                "seller": "view and respond to RFQs"
            }

            if account_options["has_target_accounts"]:
                # Show actual account options
                confirmation_message = (
                    f"You're currently logged in as a {current_role}. "
                    f"I found these {target_role} accounts for you:\n\n"
                )

                for option in account_options["formatted_options"]:
                    confirmation_message += f"{option['text']}\n"

                confirmation_message += f"\nPlease reply with the number of your choice:"
            else:
                # No target accounts found
                confirmation_message = (
                    f"You're currently logged in as a {current_role}. "
                    f"I didn't find any {target_role} accounts for your number.\n\n"
                )

                for option in account_options["formatted_options"]:
                    confirmation_message += f"{option['text']}\n"

                confirmation_message += f"\nPlease reply with the number of your choice:"

            # Store account options for response parsing
            session.workflow_state["pending_role_switch"]["account_options"] = account_options

            await self.whatsapp_service.send_message(
                user.phone_number,
                confirmation_message
            )

            return {"status": "enhanced_role_switch_confirmation_requested"}

        except Exception as e:
            logger.error(f"Error handling role switch confirmation: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_role_switch_fallback(self, user, session, target_role: str, current_role: str) -> Dict[str, Any]:
        """Fallback to generic role switch message when no cached data available."""
        try:
            role_descriptions = {
                "buyer": "create RFQs and purchase products",
                "seller": "view and respond to RFQs"
            }

            confirmation_message = (
                f"You're currently logged in as a {current_role}. "
                f"Would you like to switch to {target_role} mode to {role_descriptions[target_role]}?"
            )

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
            logger.error(f"Error in role switch fallback: {e}")
            return {"status": "error", "error": str(e)}
    
    async def handle_role_switch_response(self, user, session, message: str, authentication_service) -> Dict[str, Any]:
        """Handle user's response to enhanced role switch confirmation."""
        try:
            pending_switch = session.workflow_state.get("pending_role_switch")
            if not pending_switch:
                return {"status": "no_pending_role_switch"}

            target_role = pending_switch["target_role"]
            current_role = pending_switch["current_role"]
            original_message = pending_switch["original_message"]

            # Check if this is enhanced account selection or fallback
            account_options = pending_switch.get("account_options")

            if account_options:
                # Enhanced account selection - parse user choice
                return await self._handle_enhanced_account_selection_response(
                    user, session, message, authentication_service, pending_switch, account_options
                )
            else:
                # Fallback to original three-option response handling
                return await self._handle_traditional_role_switch_response(
                    user, session, message, authentication_service, pending_switch
                )

        except Exception as e:
            logger.error(f"Error handling role switch response: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_enhanced_account_selection_response(self, user, session, message: str,
                                                        authentication_service, pending_switch: Dict,
                                                        account_options: Dict) -> Dict[str, Any]:
        """Handle response for enhanced account selection with actual account options."""
        try:
            target_role = pending_switch["target_role"]
            current_role = pending_switch["current_role"]
            original_message = pending_switch["original_message"]

            # Parse user selection
            selected_option = await self._parse_account_selection(message, account_options["formatted_options"])

            if not selected_option:
                # Invalid selection - show options again
                return await self._show_account_selection_clarification(user, account_options, target_role)

            # Handle the selected option
            if selected_option.get("action") == "register_new":
                # User wants to register new account
                logger.info(f"User chose to register new {target_role} account")
                return await self._handle_register_new_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif selected_option.get("action") == "continue_current":
                # User wants to continue with current account
                logger.info(f"User chose to continue with current {current_role} account")

                # Clear pending switch state
                del session.workflow_state["pending_role_switch"]

                if current_role.lower() == "buyer":
                    continue_message = (
                        f"Alright, you're staying with your {current_role.title()} account.\n"
                        f"What can I assist you with today?"
                    )
                    
                    # Send message with interactive buttons
                    buttons_config = [
                        {"id": "new_rfq", "title": "📄 New RFQ"},
                        {"id": "rfq_status", "title": "🔍 RFQs Status Check"},
                        {"id": "exit", "title": "❌ Exit"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )
                else:
                    continue_message = f"Continuing with your current {current_role} account. How can I help you today?"
                    await self.whatsapp_service.send_message(user.phone_number, continue_message)

                return {
                    "status": "role_switch_declined",
                    "original_message": original_message,
                    "continue_with_original_intent": True
                }

            elif "account_data" in selected_option:
                # User selected a specific account
                selected_account = selected_option["account_data"]
                selected_email = selected_option["email"]

                logger.info(f"User chose to switch to {target_role} account: {selected_email}")

                return await self._handle_switch_to_specific_account(
                    user, session, selected_account, selected_email, target_role,
                    original_message, authentication_service
                )

            else:
                return {"status": "invalid_account_selection"}

        except Exception as e:
            logger.error(f"Error handling enhanced account selection: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_traditional_role_switch_response(self, user, session, message: str,
                                                     authentication_service, pending_switch: Dict) -> Dict[str, Any]:
        """Handle traditional three-option role switch response."""
        try:
            target_role = pending_switch["target_role"]
            current_role = pending_switch["current_role"]
            original_message = pending_switch["original_message"]

            # Use AI to validate three-option response
            confirmation_result = await self._ai_validate_three_option_response(
                message, current_role, target_role, "role_switch"
            )

            if confirmation_result == "switch_existing":
                logger.info(f"User chose to switch to existing {target_role} account")
                return await self._handle_switch_to_existing_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif confirmation_result == "register_new":
                logger.info(f"User chose to register new {target_role} account")
                return await self._handle_register_new_account(
                    user, session, target_role, original_message, authentication_service
                )

            elif confirmation_result == "continue_current":
                logger.info(f"User chose to continue with current {current_role} account")

                # Clear pending switch state
                del session.workflow_state["pending_role_switch"]

                if current_role.lower() == "buyer":
                    continue_message = (
                        f"Alright, you're staying with your {current_role.title()} account.\n"
                        f"What can I assist you with today?"
                    )
                    
                    # Send message with interactive buttons
                    buttons_config = [
                        {"id": "new_rfq", "title": "📄 New RFQ"},
                        {"id": "rfq_status", "title": "🔍 RFQs Status Check"},
                        {"id": "exit", "title": "❌ Exit"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )
                else:
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
            logger.error(f"Error handling traditional role switch response: {e}")
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

                if current_role.lower() == "buyer":
                    continue_message = (
                        f"Alright, you're staying with your {current_role.title()} account.\n"
                        f"What can I assist you with today?"
                    )
                    
                    # Send message with interactive buttons
                    buttons_config = [
                        {"id": "new_rfq", "title": "📄 New RFQ"},
                        {"id": "rfq_status", "title": "🔍 RFQs Status Check"},
                        {"id": "exit", "title": "❌ Exit"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )
                else:
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
            logger.info(f"AI validation response for message '{message}': '{response}'")

            if "switch_existing" in response:
                logger.info("AI validation: Detected switch_existing")
                return "switch_existing"
            elif "register_new" in response:
                logger.info("AI validation: Detected register_new")
                return "register_new"
            elif "continue_current" in response:
                logger.info("AI validation: Detected continue_current")
                return "continue_current"
            else:
                logger.info(f"AI validation: Response unclear - '{response}', trying fallback pattern matching")
                # Trigger fallback by raising an exception
                raise ValueError("AI validation returned unclear response")

        except Exception as e:
            logger.error(f"AI three-option validation error: {e}")
            # Fallback to simple pattern matching
            message_lower = message.lower().strip()
            logger.info(f"AI validation failed, using fallback pattern matching for: '{message_lower}'")

            if message_lower == "1" or any(word in message_lower for word in ["switch", "existing", "authenticate", "login"]):
                logger.info("Fallback: Detected switch_existing intent")
                return "switch_existing"
            elif message_lower == "2" or any(word in message_lower for word in ["register", "new", "create", "signup"]):
                logger.info("Fallback: Detected register_new intent")
                return "register_new"
            elif message_lower == "3" or any(word in message_lower for word in ["continue", "current", "stay", "keep"]):
                logger.info("Fallback: Detected continue_current intent")
                return "continue_current"
            else:
                logger.info("Fallback: Response unclear")
                return "unclear"

    async def _handle_switch_to_existing_account(self, user, session, target_role: str, original_message: str, authentication_service) -> Dict[str, Any]:
        """Handle switching to existing account authentication flow."""
        try:
            # Clear user token/session
            # Normalize phone number by removing '+' prefix for token clearing
            normalized_phone = user.phone_number.lstrip('+')
            await authentication_service.clear_user_token(normalized_phone)

            # Clear current session and create new one for reauthentication
            if hasattr(authentication_service, 'session_manager') and authentication_service.session_manager:
                # Create new session for authentication with target role
                new_session = await authentication_service.session_manager.create_session(
                    user.phone_number,
                    workflow_type="authentication",
                    user_type=target_role
                )

                # Update current session to match new session
                WorkflowManager.set_workflow_type(session, WorkflowType.authentication, caller="handler")
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

            # Check if we have cached user data to avoid API call
            cached_filtered_result = await self.user_cache_service.get_filtered_user_data(
                user.phone_number, intent
            )

            if cached_filtered_result:
                logger.info(f"Using cached filtered data for intent switch to {target_role}")
                filtered_result = cached_filtered_result
            else:
                # Fallback to authentication service
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
                else:
                    filtered_result = {"success": False}

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

        except Exception as e:
            logger.error(f"Error handling switch to existing account: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_register_new_account(self, user, session, target_role: str, original_message: str, authentication_service) -> Dict[str, Any]:
        """Handle registration flow for new account."""
        try:
            from app.services.registration_service import RegistrationService

            # Clear user token/session for registration
            # Normalize phone number by removing '+' prefix for token clearing
            normalized_phone = user.phone_number.lstrip('+')
            await authentication_service.clear_user_token(normalized_phone)

            # Clear current session and create new one for registration
            if hasattr(authentication_service, 'session_manager') and authentication_service.session_manager:
                new_session = await authentication_service.session_manager.create_session(
                    user.phone_number,
                    workflow_type="registration",
                    user_type=target_role
                )

                # Update current session to match new session
                WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="handler")
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

    async def _parse_account_selection(self, message: str, formatted_options: List[Dict]) -> Optional[Dict]:
        """Parse user's account selection from formatted options."""
        try:
            message = message.strip()

            # Try to parse as number first
            try:
                selection_num = int(message)
                for option in formatted_options:
                    if option.get("number") == selection_num:
                        logger.info(f"Selected option {selection_num}: {option.get('text', 'Unknown')}")
                        return option
            except ValueError:
                pass

            # Try to match by email
            message_lower = message.lower()
            for option in formatted_options:
                if "email" in option:
                    if option["email"].lower() in message_lower:
                        logger.info(f"Selected option by email match: {option['email']}")
                        return option

            logger.warning(f"Could not parse account selection: '{message}'")
            return None

        except Exception as e:
            logger.error(f"Error parsing account selection: {e}")
            return None

    async def _show_account_selection_clarification(self, user, account_options: Dict, target_role: str) -> Dict[str, Any]:
        """Show clarification message for account selection."""
        try:
            clarification_message = f"Please choose one of the {target_role} account options:\n\n"

            for option in account_options["formatted_options"]:
                clarification_message += f"{option['text']}\n"

            clarification_message += "\nReply with the number of your choice:"

            await self.whatsapp_service.send_message(user.phone_number, clarification_message)

            return {"status": "account_selection_clarification_requested"}

        except Exception as e:
            logger.error(f"Error showing account selection clarification: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_switch_to_specific_account(self, user, session, selected_account: Dict, selected_email: str,
                                               target_role: str, original_message: str, authentication_service) -> Dict[str, Any]:
        """Handle switching to a specific selected account using traditional auth flow."""
        try:
            # Clear user token/session
            normalized_phone = user.phone_number.lstrip('+')
            await authentication_service.clear_user_token(normalized_phone)

            # Clear pending switch state
            if "pending_role_switch" in session.workflow_state:
                del session.workflow_state["pending_role_switch"]

            # Make fresh authentication call to get all accounts for this phone number
            logger.info(f"Making authentication call to get fresh account data for selected email: {selected_email}")

            # Use existing user_authenticate method to get fresh data and cache it
            auth_response = await authentication_service.user_authenticate(
                user.phone_number, "account_switch", session, intent="general_inquiry"
            )

            if auth_response.get("success"):
                raw_response = auth_response.get("response", [])
                if raw_response:
                    # Find the specific user account from the response
                    selected_user_data = None
                    for account in raw_response:
                        account_email = account.get("username") or account.get("email")
                        if account_email == selected_email:
                            selected_user_data = account
                            break

                    if selected_user_data:
                        # Use traditional authentication flow via _process_selected_email
                        # This ensures buyers get "Hi {username}!" and sellers get OTP validation
                        logger.info(f"Processing selected email through traditional auth flow: {selected_email}")

                        # Create filtered_users list with just the selected account
                        filtered_users = [selected_user_data]

                        # Call the traditional email processing method
                        result = await authentication_service._process_selected_email(
                            user.phone_number,
                            session,
                            selected_email,
                            filtered_users
                        )

                        logger.info(f"Traditional auth flow result for {target_role}: {result.get('status')}")

                        # Retrieve meaningful message from cache if available
                        cached_meaningful = await self.user_cache_service.get_meaningful_message(normalized_phone)
                        if cached_meaningful:
                            result["original_message"] = cached_meaningful["message"]
                            result["original_intent_result"] = cached_meaningful["intent_result"]
                            logger.info(f"Retrieved and attached meaningful message to result: {cached_meaningful['message'][:50]}...")
                            # Clear from cache after retrieval
                            await self.user_cache_service.clear_meaningful_message(normalized_phone)

                        return result
                    else:
                        logger.error(f"Selected email {selected_email} not found in fresh API response")
                else:
                    logger.error("Empty API response when switching accounts")
            else:
                logger.error(f"Authentication API call failed for {selected_email}: {auth_response.get('message')}")

            # If we reach here, something went wrong
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Error switching accounts. Please try again or contact support."
            )
            return {"status": "account_switch_failed"}

        except Exception as e:
            logger.error(f"Error handling switch to specific account: {e}")
            return {"status": "error", "error": str(e)}