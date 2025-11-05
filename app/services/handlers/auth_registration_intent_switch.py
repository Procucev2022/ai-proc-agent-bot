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
        """Handle role switch confirmation - simplified to show only 2 options."""
        try:
            current_role = _get_role_string(user)

            # Store role switch state
            session.workflow_state["pending_role_switch"] = {
                "target_role": target_role,
                "current_role": current_role,
                "original_message": message,
                "timestamp": utc_now().isoformat()
            }

            # Simple 2-option confirmation message
            confirmation_message = (
                f"You're currently logged in as a {current_role.title()}. "
                f"What would you like to do?\n\n"
                f"1. Switch to {target_role.title()} account\n"
                f"2. Continue with current {current_role.title()} account\n\n"
                "Please reply with 1 or 2:"
            )

            await self.whatsapp_service.send_message(
                user.phone_number,
                confirmation_message
            )

            return {"status": "role_switch_confirmation_requested"}

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
        """Handle user's response to simplified 2-option role switch confirmation."""
        try:
            pending_switch = session.workflow_state.get("pending_role_switch")
            if not pending_switch:
                return {"status": "no_pending_role_switch"}

            target_role = pending_switch["target_role"]
            current_role = pending_switch["current_role"]
            original_message = pending_switch["original_message"]

            # Parse user response - looking for "1" (switch) or "2" (continue)
            message_lower = message.strip().lower()

            # Check for switch confirmation (option 1)
            if message_lower in ["1", "switch", "yes", "switch account"]:
                logger.info(f"User chose to switch from {current_role} to {target_role}")

                # Clear pending switch state before exit
                if "pending_role_switch" in session.workflow_state:
                    del session.workflow_state["pending_role_switch"]

                # Exit and trigger profile selection
                return await self._handle_switch_to_existing_account(
                    user, session, target_role, original_message, authentication_service
                )

            # Check for continue with current account (option 2)
            elif message_lower in ["2", "continue", "no", "stay", "current"]:
                logger.info(f"User chose to continue with current {current_role} account - ignoring role switch request")

                # Clear pending switch state
                if "pending_role_switch" in session.workflow_state:
                    del session.workflow_state["pending_role_switch"]

                # Send acknowledgment and let them continue with existing workflow
                continue_message = f"Alright, continuing with your {current_role.title()} account."
                await self.whatsapp_service.send_message(user.phone_number, continue_message)

                # Return status indicating we should just ignore the role switch message
                # and continue with whatever workflow they were in before
                return {
                    "status": "role_switch_declined",
                    "ignore_message": True  # Signal to ignore the original message
                }

            else:
                # Unclear response - ask for clarification
                clarification_message = (
                    f"Please choose one of the options:\n\n"
                    f"1. Switch to {target_role.title()} account\n"
                    f"2. Continue with current {current_role.title()} account\n\n"
                    "Reply with 1 or 2:"
                )
                await self.whatsapp_service.send_message(user.phone_number, clarification_message)

                return {"status": "role_switch_clarification_requested"}

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
                        "What can I assist you with today?"
                    )
                    
                    buttons_config = [
                        {"id": "new_rfq", "title": "Raise a new RFQ"},
                        {"id": "rfq_status", "title": "Check your previous RFQs"},
                        {"id": "contact_support", "title": "Any other support you need"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )
                else:
                    continue_message = (
                        f"Alright, you're staying with your {current_role.title()} account.\n"
                        "What would you like to do today?"
                    )
                    
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQ status"},
                        {"id": "contact_support", "title": "Get other support"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )

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
                        "What can I assist you with today?"
                    )
                    
                    buttons_config = [
                        {"id": "new_rfq", "title": "Raise a new RFQ"},
                        {"id": "rfq_status", "title": "Check your previous RFQs"},
                        {"id": "contact_support", "title": "Any other support you need"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )
                else:
                    continue_message = (
                        f"Alright, you're staying with your {current_role.title()} account.\n"
                        "What would you like to do today?"
                    )
                    
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQ status"},
                        {"id": "contact_support", "title": "Get other support"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )

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
                        "What can I assist you with today?"
                    )
                    
                    buttons_config = [
                        {"id": "new_rfq", "title": "Raise a new RFQ"},
                        {"id": "rfq_status", "title": "Check your previous RFQs"},
                        {"id": "contact_support", "title": "Any other support you need"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )
                else:
                    continue_message = (
                        f"Alright, you're staying with your {current_role.title()} account.\n"
                        "What would you like to do today?"
                    )
                    
                    buttons_config = [
                        {"id": "rfq_status", "title": "Check RFQ status"},
                        {"id": "contact_support", "title": "Get other support"}
                    ]
                    
                    await self.whatsapp_service.send_configurable_buttons(
                        user.phone_number,
                        continue_message,
                        buttons_config,
                        "Choose an option"
                    )

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
        """Handle switching to existing account by completely exiting and triggering profile selection."""
        try:
            from app.services.exit_service import ExitService

            logger.info(f"Switching to different {target_role} account - initiating complete exit and profile selection")

            # Use ExitService to completely exit the user (without goodbye message)
            exit_service = ExitService(
                whatsapp_service=self.whatsapp_service,
                authentication_service=authentication_service,
                session_manager=authentication_service.session_manager if hasattr(authentication_service, 'session_manager') else None
            )

            # Perform complete exit without showing goodbye message
            exit_result = await exit_service.handle_exit_intent(
                user.phone_number,
                session,
                show_message=False  # No goodbye message, we're switching accounts
            )

            logger.info(f"Exit result for account switch: {exit_result}")

            # Send account switch message
            switch_message = f"Switching to a different {target_role} account. Please select your profile to continue."
            await self.whatsapp_service.send_message(user.phone_number, switch_message)

            # Return status to trigger profile selection in chat service
            return {
                "status": "exit_and_trigger_profile_selection",
                "target_role": target_role,
                "original_message": original_message,
                "exit_result": exit_result
            }

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
        """Handle switching to a specific selected account by completely exiting and triggering profile selection."""
        try:
            from app.services.exit_service import ExitService

            logger.info(f"Switching to specific {target_role} account ({selected_email}) - initiating complete exit and profile selection")

            # Clear pending switch state before exit
            if "pending_role_switch" in session.workflow_state:
                del session.workflow_state["pending_role_switch"]

            # Use ExitService to completely exit the user (without goodbye message)
            exit_service = ExitService(
                whatsapp_service=self.whatsapp_service,
                authentication_service=authentication_service,
                session_manager=authentication_service.session_manager if hasattr(authentication_service, 'session_manager') else None
            )

            # Perform complete exit without showing goodbye message
            exit_result = await exit_service.handle_exit_intent(
                user.phone_number,
                session,
                show_message=False  # No goodbye message, we're switching accounts
            )

            logger.info(f"Exit result for specific account switch: {exit_result}")

            # Send account switch message
            switch_message = f"Switching to {target_role} account. Please select your profile to continue."
            await self.whatsapp_service.send_message(user.phone_number, switch_message)

            # Return status to trigger profile selection in chat service
            return {
                "status": "exit_and_trigger_profile_selection",
                "target_role": target_role,
                "selected_email": selected_email,
                "original_message": original_message,
                "exit_result": exit_result
            }

        except Exception as e:
            logger.error(f"Error handling switch to specific account: {e}")
            return {"status": "error", "error": str(e)}