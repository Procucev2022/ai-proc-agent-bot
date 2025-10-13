"""
Profile Selection Service for WhatsApp Bot.

Handles all profile selection scenarios including:
- Neutral/Greeting messages with profile selection
- Buyer intent detection with profile filtering
- Seller intent detection with profile filtering
- RFQ status check with role-based selection
- Invalid/Ambiguous messages with clarification
- Profile switching and account management

Key responsibilities:
- Detect user intent and filter profiles accordingly
- Present appropriate profile selection interfaces
- Handle profile selection responses
- Manage session context and role switching
- Integrate with existing authentication flow
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from app.models import ConversationSession, WorkflowType
from app.schemas.user import User
from app.services.whatsapp_service import WhatsAppService
from app.services.authentication_service import AuthenticationService
from app.services.user_cache_service import get_user_cache_service
from app.services.workflow_manager import WorkflowManager
from app.services.openai_service import OpenAIService
from app.tools.user_selection_tool import UserSelectionTool

logger = logging.getLogger(__name__)


class ProfileSelectionService:
    """Handles intelligent profile selection based on user intent and context."""
    
    def __init__(self, whatsapp_service: WhatsAppService, authentication_service: AuthenticationService, openai_service: OpenAIService = None):
        self.whatsapp_service = whatsapp_service
        self.authentication_service = authentication_service
        self.user_cache_service = get_user_cache_service()
        # Only initialize user selection tool if OpenAI service is available
        self.user_selection_tool = UserSelectionTool(openai_service) if openai_service else None
    
    async def handle_profile_selection(self, user_phone: str, message: str, session: ConversationSession, 
                                     intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main entry point for profile selection handling.
        
        Analyzes user intent and presents appropriate profile selection interface.
        """
        try:
            intent = intent_result.get('intent', 'general_inquiry')
            confidence = intent_result.get('confidence', 0)
            
            logger.info(f"Profile selection for {user_phone}: intent={intent}, confidence={confidence}")
            
            # Get user profiles from cache or API
            profiles_result = await self._get_user_profiles(user_phone, message, session)
            
            if not profiles_result.get('success'):
                return await self._handle_no_profiles_found(user_phone, intent, session)
            
            profiles = profiles_result.get('profiles', [])
            
            # Case 1: Neutral/Greeting Start
            if intent in ['general_inquiry', 'ambiguous'] or confidence < 50:
                return await self._handle_neutral_greeting(user_phone, profiles, session)
            
            # Case 2: Buyer Intent Detected
            elif intent == 'buy_something' and confidence > 70:
                return await self._handle_buyer_intent(user_phone, profiles, message, session, intent_result)

            # Case 3: Ambiguous buying intent (verb but no object)
            elif intent == 'buy_something' and 50 <= confidence <= 70:
                return await self._handle_ambiguous_buying_intent(user_phone, profiles, message, session, intent_result)

            # Case 3: Seller Intent Detected
            elif intent == 'sell_something' and confidence > 70:
                return await self._handle_seller_intent(user_phone, profiles, message, session, intent_result)

            # Case 4: RFQ Status Check
            elif intent == 'rfq_status_check' and confidence > 70:
                return await self._handle_rfq_status_check(user_phone, profiles, session)

            # Case 5: Invalid or Ambiguous
            else:
                return await self._handle_invalid_ambiguous(user_phone, profiles, session)

        except Exception as e:
            logger.error(f"Profile selection error for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def handle_profile_selection_response(self, user_phone: str, message: str,
                                              session: ConversationSession) -> Dict[str, Any]:
        """Handle user response to profile selection."""
        try:
            # Get stored profile options from session
            profile_options = session.workflow_state.get('profile_options', [])

            if not profile_options:
                logger.warning(f"No profile options found in session for {user_phone}")
                return {"status": "restart_profile_selection"}

            # Parse user selection
            selected_profile = await self._parse_profile_selection(message, profile_options)

            if selected_profile:
                return await self._process_selected_profile(user_phone, selected_profile, session)
            else:
                # Invalid selection - show options again
                return await self._show_profile_selection_retry(user_phone, profile_options, session)

        except Exception as e:
            logger.error(f"Profile selection response error for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _get_user_profiles(self, user_phone: str, message: str,
                               session: ConversationSession) -> Dict[str, Any]:
        """Get user profiles from cache or API."""
        try:
            # First check cache
            cached_data = await self.user_cache_service.get_user_data(user_phone)

            if cached_data:
                profiles = self._convert_api_data_to_profiles(cached_data)
                return {"success": True, "profiles": profiles, "from_cache": True}

            # If no cache, make API call
            auth_response = await self.authentication_service.user_authenticate(
                user_phone, message, session
            )

            if auth_response.get("success"):
                raw_response = auth_response.get("response", [])
                profiles = self._convert_api_data_to_profiles(raw_response)

                # Don't cache here - already cached in authentication_service

                return {"success": True, "profiles": profiles, "from_cache": False}

            return {"success": False, "message": "No profiles found"}

        except Exception as e:
            logger.error(f"Error getting user profiles for {user_phone}: {e}")
            return {"success": False, "error": str(e)}

    def _convert_api_data_to_profiles(self, api_data: List[Dict]) -> List[Dict]:
        """Convert API response to standardized profile format."""
        profiles = []

        for user_data in api_data:
            try:
                user = User.from_api_response(user_data)
                profile = {
                    "email": user.email,
                    "role": user.role.value,
                    "name": user.name,
                    "company": user.company_name,
                    "user_data": user_data  # Store original for authentication
                }
                profiles.append(profile)
            except Exception as e:
                logger.warning(f"Failed to convert user data to profile: {e}")
                continue

        return profiles

    async def _handle_neutral_greeting(self, user_phone: str, profiles: List[Dict],
                                     session: ConversationSession) -> Dict[str, Any]:
        """Handle Case 1: Neutral/Greeting Start."""
        try:
            # Group profiles by role
            buyer_profiles = [p for p in profiles if p['role'] == 'buyer']
            seller_profiles = [p for p in profiles if p['role'] == 'seller']

            # Build profile selection message
            message_parts = [
                "👋 Hi there! I can help you with both Buying (creating or checking RFQs) and Selling (responding to buyer requests).",
                "",
                "Please select your profile to continue:"
            ]

            profile_options = []
            option_num = 1

            # Add buyer profiles
            for profile in buyer_profiles:
                message_parts.append(f" {option_num}️⃣ {profile['email']} — Buyer")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{profile['email']} — Buyer"
                })
                option_num += 1

            # Add seller profiles
            for profile in seller_profiles:
                message_parts.append(f" {option_num}️⃣ {profile['email']} — Seller")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{profile['email']} — Seller"
                })
                option_num += 1



            message_parts.append("")
            message_parts.append("Reply with the number corresponding to your account to continue.")

            # Store options in session
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_selection_stage'] = 'neutral_greeting'
            session.workflow_state['profile_options'] = profile_options

            # Send message
            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)

            return {
                "status": "profile_selection_presented",
                "selection_type": "neutral_greeting",
                "options_count": len(profile_options)
            }

        except Exception as e:
            logger.error(f"Error handling neutral greeting for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_buyer_intent(self, user_phone: str, profiles: List[Dict], message: str,
                                 session: ConversationSession, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Case 2: Buyer Intent Detected."""
        try:
            buyer_profiles = [p for p in profiles if p['role'] == 'buyer']

            if not buyer_profiles:
                # No buyer profiles - redirect to registration
                return await self._redirect_to_buyer_registration(user_phone, session, message)

            if len(buyer_profiles) == 1:
                # Single buyer profile - auto-select and proceed
                profile = buyer_profiles[0]

                message_parts = [
                    f"Got it! You want to create an RFQ to buy items.",
                    "",
                    f"Let's continue with your Buyer profile ({profile['email']})."
                ]

                await self.whatsapp_service.send_message(user_phone, "\n".join(message_parts))

                # Set active profile and proceed to RFQ creation
                return await self._set_active_profile_and_proceed(
                    user_phone, profile, session, message, "buy_something"
                )

            else:
                # Multiple buyer profiles - show selection
                message_parts = [
                    "I understand you want to buy items. Please choose which Buyer profile you'd like to continue with:"
                ]

                profile_options = []
                option_num = 1

                for profile in buyer_profiles:
                    message_parts.append(f" {option_num}️⃣ {profile['email']} — Buyer")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Buyer"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}️⃣ Register a new Buyer account")
                profile_options.append({
                    "number": option_num,
                    "action": "register_buyer",
                    "display": "Register a new Buyer account"
                })

                # Store context in session
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['profile_selection_stage'] = 'buyer_intent'
                session.workflow_state['profile_options'] = profile_options
                session.workflow_state['original_message'] = message
                session.workflow_state['original_intent'] = intent_result

                full_message = "\n".join(message_parts)
                await self.whatsapp_service.send_message(user_phone, full_message)

                return {
                    "status": "buyer_profile_selection_presented",
                    "options_count": len(profile_options)
                }

        except Exception as e:
            logger.error(f"Error handling buyer intent for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_seller_intent(self, user_phone: str, profiles: List[Dict], message: str,
                                  session: ConversationSession, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Case 3: Seller Intent Detected."""
        try:
            seller_profiles = [p for p in profiles if p['role'] == 'seller']

            if not seller_profiles:
                # No seller profiles - redirect to registration
                return await self._redirect_to_seller_registration(user_phone, session, message)

            if len(seller_profiles) == 1:
                # Single seller profile - auto-select and proceed
                profile = seller_profiles[0]

                message_parts = [
                    f"You’d like to sell items — great!",
                    f"Continuing with your Seller profile ({profile['email']}).",
                ]

                await self.whatsapp_service.send_message(user_phone, "\n".join(message_parts))

                # Set active profile and proceed to seller flow
                return await self._set_active_profile_and_proceed(
                    user_phone, profile, session, message, "sell_something"
                )

            else:
                # Multiple seller profiles - show selection
                message_parts = [
                    "I understand you want to sell items. Please choose which Seller profile you'd like to continue with:"
                ]

                profile_options = []
                option_num = 1

                for profile in seller_profiles:
                    message_parts.append(f" {option_num}️⃣ {profile['email']} — Seller")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Seller"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}️⃣ Register a new Seller account")
                profile_options.append({
                    "number": option_num,
                    "action": "register_seller",
                    "display": "Register a new Seller account"
                })

                # Store context in session
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['profile_selection_stage'] = 'seller_intent'
                session.workflow_state['profile_options'] = profile_options
                session.workflow_state['original_message'] = message
                session.workflow_state['original_intent'] = intent_result

                full_message = "\n".join(message_parts)
                await self.whatsapp_service.send_message(user_phone, full_message)

                return {
                    "status": "seller_profile_selection_presented",
                    "options_count": len(profile_options)
                }

        except Exception as e:
            logger.error(f"Error handling seller intent for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_rfq_status_check(self, user_phone: str, profiles: List[Dict],
                                     session: ConversationSession) -> Dict[str, Any]:
        """Handle Case 4: RFQ Status Check."""
        try:
            # Both buyers and sellers can check RFQ status
            message_parts = [
                "I understand you'd like to check the RFQ status.",
                "Please choose the profile you want to use for this request:"
            ]

            profile_options = []
            option_num = 1

            for profile in profiles:
                role_display = "Buyer" if profile['role'] == 'buyer' else "Seller"
                message_parts.append(f" {option_num}️⃣ {profile['email']} — {role_display}")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{profile['email']} — {role_display}"
                })
                option_num += 1

            # Support multiple profiles, not just 1 or 2

            # Store context in session
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_selection_stage'] = 'rfq_status_check'
            session.workflow_state['profile_options'] = profile_options

            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)

            return {
                "status": "rfq_status_profile_selection_presented",
                "options_count": len(profile_options)
            }

        except Exception as e:
            logger.error(f"Error handling RFQ status check for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_invalid_ambiguous(self, user_phone: str, profiles: List[Dict],
                                      session: ConversationSession) -> Dict[str, Any]:
        """Handle Case 5: Invalid or Ambiguous Start Message."""
        try:
            message_parts = [
                "Hi there! I can help you with:",
                "• 🛒 Buying items (Raise or Check RFQs)",
                "• 💼 Selling items (Respond to RFQs)",
                "",
                "Please tell me if you'd like to continue as a Buyer or Seller, or choose from your saved profiles below:"
            ]

            profile_options = []
            option_num = 1

            for profile in profiles:
                role_display = "Buyer" if profile['role'] == 'buyer' else "Seller"
                message_parts.append(f" {option_num}️⃣ {role_display} — {profile['email']}")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{role_display} — {profile['email']}"
                })
                option_num += 1

            # Store context in session
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_selection_stage'] = 'invalid_ambiguous'
            session.workflow_state['profile_options'] = profile_options

            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)

            return {
                "status": "ambiguous_profile_selection_presented",
                "options_count": len(profile_options)
            }

        except Exception as e:
            logger.error(f"Error handling invalid/ambiguous for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_no_profiles_found(self, user_phone: str, intent: str,
                                      session: ConversationSession) -> Dict[str, Any]:
        """Handle when no profiles are found for the user."""
        try:
            if intent == 'buy_something':
                return await self._redirect_to_buyer_registration(user_phone, session, "")
            elif intent == 'sell_something':
                return await self._redirect_to_seller_registration(user_phone, session, "")
            else:
                # General case - ask what they want to do
                message = (
                    "Welcome! I can help you with procurement needs.\n\n"
                    "Would you like to:\n"
                    "• Register as a Buyer (to create RFQs)\n"
                    "• Register as a Seller (to respond to RFQs)\n\n"
                    "Please let me know how you'd like to proceed."
                )

                await self.whatsapp_service.send_message(user_phone, message)

                return {
                    "status": "registration_choice_presented",
                    "reason": "no_profiles_found"
                }

        except Exception as e:
            logger.error(f"Error handling no profiles found for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _parse_profile_selection(self, message: str, profile_options: List[Dict]) -> Optional[Dict]:
        """Parse user's profile selection from message using intelligent analysis."""
        try:
            # Use the user selection tool for comprehensive analysis if available
            if self.user_selection_tool:
                analysis_result = await self.user_selection_tool.analyze_user_selection(message, profile_options)

                logger.info(f"Selection analysis: {analysis_result}")

                # If analysis found a clear selection
                if analysis_result.get('selected_option') and not analysis_result.get('requires_clarification'):
                    selected_num = analysis_result['selected_option']

                    # Find the matching option
                    for option in profile_options:
                        if option.get('number') == selected_num:
                            return option

                # If analysis suggests clarification is needed or no clear match
                return None
            else:
                # Fallback to simple parsing if user selection tool is not available
                return await self._simple_parse_profile_selection(message, profile_options)

        except Exception as e:
            logger.error(f"Error parsing profile selection: {e}")
            # Fallback to simple parsing on error
            return await self._simple_parse_profile_selection(message, profile_options)

    def _fuzzy_email_match(self, user_input: str, target_email: str) -> Dict[str, Any]:
        """Fuzzy match user input against target email for typos/incomplete entries."""
        try:
            user_input = user_input.strip().lower()
            target_email = target_email.lower()

            if len(user_input) < 3:
                return {'confidence': 0.0}

            # Check if user input is substring of email (incomplete email)
            if user_input in target_email:
                confidence = min(0.8, len(user_input) / len(target_email) + 0.3)
                return {'confidence': confidence}

            # Check username part similarity
            if '@' in target_email:
                username = target_email.split('@')[0]
                domain = target_email.split('@')[1]

                # Username fuzzy match
                username_similarity = self._string_similarity(user_input, username)
                if username_similarity > 0.7:
                    return {'confidence': username_similarity * 0.8}

                # Domain fuzzy match
                domain_similarity = self._string_similarity(user_input, domain)
                if domain_similarity > 0.7:
                    return {'confidence': domain_similarity * 0.7}

                # Full email fuzzy match
                email_similarity = self._string_similarity(user_input, target_email)
                if email_similarity > 0.6:
                    return {'confidence': email_similarity * 0.6}

            return {'confidence': 0.0}

        except Exception:
            return {'confidence': 0.0}

    def _string_similarity(self, s1: str, s2: str) -> float:
        """Calculate string similarity using simple character-based approach."""
        try:
            if not s1 or not s2:
                return 0.0

            longer = s2 if len(s2) > len(s1) else s1
            shorter = s1 if len(s1) < len(s2) else s2

            if len(longer) == 0:
                return 1.0

            # Count matching characters in order
            matches = 0
            j = 0
            for char in shorter:
                while j < len(longer) and longer[j] != char:
                    j += 1
                if j < len(longer):
                    matches += 1
                    j += 1

            return matches / len(longer)

        except Exception:
            return 0.0

    async def _simple_parse_profile_selection(self, message: str, profile_options: List[Dict]) -> Optional[Dict]:
        """Simple fallback parsing for profile selection."""
        try:
            message = message.strip()

            # Try to parse as number
            try:
                selection_num = int(message)
                for option in profile_options:
                    if option.get('number') == selection_num:
                        return option
            except ValueError:
                pass

            # Try to match email or role keywords with fuzzy matching
            message_lower = message.lower()

            # First try exact matches
            for option in profile_options:
                if 'profile' in option:
                    profile = option['profile']
                    email = profile.get('email', '').lower()
                    role = profile.get('role', '').lower()

                    if email in message_lower or role in message_lower:
                        return option
                elif 'action' in option:
                    action = option['action'].lower()
                    if 'register' in message_lower and ('buyer' in action or 'seller' in action):
                        return option

            # Try fuzzy email matching if no exact match
            best_match = None
            best_confidence = 0.0

            for option in profile_options:
                if 'profile' in option:
                    profile = option['profile']
                    email = profile.get('email', '')

                    fuzzy_result = self._fuzzy_email_match(message_lower, email)
                    if fuzzy_result['confidence'] > best_confidence and fuzzy_result['confidence'] > 0.6:
                        best_match = option
                        best_confidence = fuzzy_result['confidence']

            return best_match

        except Exception as e:
            logger.error(f"Error in simple profile selection parsing: {e}")
            return None

    async def _process_selected_profile(self, user_phone: str, selected_option: Dict,
                                      session: ConversationSession) -> Dict[str, Any]:
        """Process the selected profile option."""
        try:
            # Handle registration actions
            if 'action' in selected_option:
                action = selected_option['action']

                if action == 'register_new':
                    return await self._handle_new_registration_choice(user_phone, session)
                elif action == 'register_buyer':
                    return await self._redirect_to_buyer_registration(user_phone, session, "")
                elif action == 'register_seller':
                    return await self._redirect_to_seller_registration(user_phone, session, "")

            # Handle profile selection
            elif 'profile' in selected_option:
                profile = selected_option['profile']

                # Get original context
                original_message = session.workflow_state.get('original_message', '')
                original_intent = session.workflow_state.get('original_intent', {})
                selection_stage = session.workflow_state.get('profile_selection_stage', '')

                # Determine next action based on selection stage
                if selection_stage == 'buyer_intent':
                    return await self._set_active_profile_and_proceed(
                        user_phone, profile, session, original_message, "buy_something"
                    )
                elif selection_stage == 'seller_intent':
                    return await self._set_active_profile_and_proceed(
                        user_phone, profile, session, original_message, "sell_something"
                    )
                elif selection_stage == 'rfq_status_check':
                    return await self._set_active_profile_and_proceed(
                        user_phone, profile, session, "check RFQ status", "rfq_status_check"
                    )
                else:
                    # Neutral selection - show role-based menu
                    return await self._show_role_based_menu(user_phone, profile, session)

            return {"status": "error", "error": "Invalid selection option"}

        except Exception as e:
            logger.error(f"Error processing selected profile for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _set_active_profile_and_proceed(self, user_phone: str, profile: Dict,
                                            session: ConversationSession, message: str,
                                            intent: str) -> Dict[str, Any]:
        """Set the active profile and proceed with the intended action."""
        try:
            # Store user session using the profile data
            user_data = profile['user_data']
            user_details = User.from_api_response(user_data)

            # Store session
            session_stored = await self.authentication_service.store_user_session(
                user_phone, user_details
            )

            if not session_stored:
                logger.error(f"Failed to store session for {user_phone}")
                return {"status": "error", "error": "Failed to store session"}

            # Clear profile selection state
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.pop('profile_selection_stage', None)
            session.workflow_state.pop('profile_options', None)

            # Profile selection confirmed - no message needed

            return {
                "status": "profile_selected_and_authenticated",
                "user_type": profile['role'],
                "email": profile['email'],
                "original_message": message,
                "original_intent": intent,
                "redirect_to_main_flow": True
            }

        except Exception as e:
            logger.error(f"Error setting active profile for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _show_role_based_menu(self, user_phone: str, profile: Dict,
                                  session: ConversationSession) -> Dict[str, Any]:
        """Show role-based menu after profile selection."""
        try:
            role = profile['role']
            email = profile['email']

            # Set active profile first
            result = await self._set_active_profile_and_proceed(
                user_phone, profile, session, "", "general_inquiry"
            )

            if result.get('status') != 'profile_selected_and_authenticated':
                return result

            # Show role-specific menu with buttons
            if role == 'buyer':
                menu_message = f"You're now using your Buyer account ({email})."
                header = "What can I assist you with today?"
                buttons_config = [
                    {"id": "new_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQs Status"},
                    {"id": "contact_support", "title": "Contact Support"}
                ]
            else:  # seller
                menu_message = f"You're now using your Seller account ({email})."
                header = "What would you like to do today?"
                buttons_config = [
                    {"id": "rfq_status", "title": "Check RFQs Status"},
                    {"id": "contact_support", "title": "Contact Support"}
                ]

            # Send interactive buttons
            await self.whatsapp_service.send_configurable_buttons(
                user_phone,
                menu_message,
                buttons_config,
                header
            )

            return {
                "status": "role_menu_presented",
                "user_type": role,
                "email": email
            }

        except Exception as e:
            logger.error(f"Error showing role-based menu for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_new_registration_choice(self, user_phone: str, 
                                            session: ConversationSession) -> Dict[str, Any]:
        """Handle choice to register a new profile."""
        try:
            message = (
                "Would you like to register as:\n"
                "• 🛒 Buyer (to create RFQs and purchase items)\n"
                "• 💼 Seller (to respond to RFQs and sell items)\n\n"
                "Please let me know which type of account you'd like to create."
            )
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            # Set session state to handle registration type selection
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['awaiting_registration_type'] = True
            
            return {
                "status": "registration_type_choice_presented"
            }
            
        except Exception as e:
            logger.error(f"Error handling new registration choice for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _redirect_to_buyer_registration(self, user_phone: str, session: ConversationSession, 
                                            message: str) -> Dict[str, Any]:
        """Redirect to buyer registration flow."""
        try:
            # Set workflow type to registration
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="profile_selection")
            
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update({
                "registration_stage": "data_collection",
                "user_type": "buyer",
                "original_message": message
            })
            
            welcome_message = (
                "I'll help you register as a Buyer so you can create RFQs and purchase items.\n\n"
                "Let's start with some basic information..."
            )
            
            await self.whatsapp_service.send_message(user_phone, welcome_message)
            
            return {
                "status": "redirected_to_registration",
                "workflow_type": "registration",
                "user_type": "buyer"
            }
            
        except Exception as e:
            logger.error(f"Error redirecting to buyer registration for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _redirect_to_seller_registration(self, user_phone: str, session: ConversationSession, 
                                             message: str) -> Dict[str, Any]:
        """Redirect to seller registration flow."""
        try:
            # Set workflow type to registration
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="profile_selection")
            
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update({
                "registration_stage": "data_collection",
                "user_type": "seller",
                "original_message": message
            })
            
            welcome_message = (
                "I'll help you register as a Seller so you can respond to RFQs and sell items.\n\n"
                "Let's start with some basic information..."
            )
            
            await self.whatsapp_service.send_message(user_phone, welcome_message)
            
            return {
                "status": "redirected_to_registration",
                "workflow_type": "registration",
                "user_type": "seller"
            }
            
        except Exception as e:
            logger.error(f"Error redirecting to seller registration for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _show_profile_selection_retry(self, user_phone: str, profile_options: List[Dict], 
                                          session: ConversationSession) -> Dict[str, Any]:
        """Show profile selection options again after invalid selection."""
        try:
            message_parts = [
                "I didn't understand your selection. Please choose from the options below:"
            ]
            
            for option in profile_options:
                if 'profile' in option:
                    message_parts.append(f" {option['number']}️⃣ {option['display']}")
                elif 'action' in option:
                    message_parts.append(f" {option['number']}️⃣ {option['display']}")
            
            message_parts.append("")
            message_parts.append("Reply with the number corresponding to your choice.")
            
            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)
            
            return {
                "status": "profile_selection_retry_presented",
                "options_count": len(profile_options)
            }
            
        except Exception as e:
            logger.error(f"Error showing profile selection retry for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}