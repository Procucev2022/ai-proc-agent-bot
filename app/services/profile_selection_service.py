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

    def __init__(self, whatsapp_service: WhatsAppService, authentication_service: AuthenticationService,
                 openai_service: OpenAIService = None):
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

            # Handle case where message might be a dict (button reply)
            if isinstance(message, dict):
                if 'button_reply' in message:
                    message = message['button_reply'].get('title', str(message))
                else:
                    message = str(message)

            # Check for explicit registration intent first
            registration_intent = await self._detect_registration_intent(message)
            logger.info(f"Registration intent detection for '{message}': {registration_intent}")
            if registration_intent:
                if registration_intent == 'buyer':
                    return await self._redirect_to_buyer_registration(user_phone, session, message)
                elif registration_intent == 'seller':
                    return await self._redirect_to_seller_registration(user_phone, session, message)

            # Also check if intent is register_account and handle accordingly
            if intent == 'register_account':
                # Try to detect specific registration type from message
                detected_type = await self._detect_registration_intent(message)
                if detected_type == 'buyer':
                    return await self._redirect_to_buyer_registration(user_phone, session, message)
                elif detected_type == 'seller':
                    return await self._redirect_to_seller_registration(user_phone, session, message)
                else:
                    # If no specific type detected, show registration options
                    return await self._handle_no_profiles_found(user_phone, intent, session)

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
                return await self._handle_buyer_intent(user_phone, profiles, message, session, intent_result)

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
            # Check if awaiting registration type choice
            if session.workflow_state.get('awaiting_registration_type'):
                return await self._handle_registration_type_response(user_phone, message, session)

            # Check if handling intent mismatch response
            if session.workflow_state.get('profile_selection_stage') == 'intent_mismatch':
                return await self._handle_intent_mismatch_response(user_phone, message, session)

            # CRITICAL FIX: Check if we're in new_user_registration stage and handle directly
            if session.workflow_state.get('profile_selection_stage') == 'new_user_registration':
                return await self._handle_new_user_registration_response(user_phone, message, session)

            # Check if user is requesting specific role filter (buyer/seller)
            role_filter_result = await self._check_role_filter_request(user_phone, message, session)
            if role_filter_result:
                return role_filter_result

            # Get stored profile options from session
            profile_options = session.workflow_state.get('profile_options', [])

            if not profile_options:
                logger.warning(f"No profile options found in session for {user_phone}")
                return {"status": "restart_profile_selection"}

            # Parse user selection
            logger.info(f"Parsing profile selection for message: '{message}' with options: {profile_options}")
            selected_profile = await self._parse_profile_selection(message, profile_options)
            logger.info(f"Parsed selection result: {selected_profile}")

            if selected_profile:
                logger.info(f"Processing selected profile: {selected_profile}")
                return await self._process_selected_profile(user_phone, selected_profile, session)
            else:
                # Invalid selection - show options again
                logger.warning(f"Invalid selection for message: '{message}', showing retry")
                return await self._show_profile_selection_retry(user_phone, profile_options, session)

        except Exception as e:
            logger.error(f"Profile selection response error for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _get_user_profiles(self, user_phone: str, message: str,
                                 session: ConversationSession) -> Dict[str, Any]:
        """Get user profiles from cache or API."""
        try:
            logger.info(f"Getting user profiles for {user_phone}")
            
            # First check cache
            cached_data = await self.user_cache_service.get_user_data(user_phone)
            logger.info(f"Cache check result for {user_phone}: {bool(cached_data)}")

            if cached_data:
                profiles = self._convert_api_data_to_profiles(cached_data)
                logger.info(f"Found {len(profiles)} profiles in cache for {user_phone}")
                return {"success": True, "profiles": profiles, "from_cache": True}

            # If no cache, make API call
            logger.info(f"No cache found, making API call for {user_phone}")
            auth_response = await self.authentication_service.user_authenticate(
                user_phone, message, session
            )
            logger.info(f"Authentication API response for {user_phone}: {auth_response}")

            if auth_response.get("success"):
                raw_response = auth_response.get("response", [])
                profiles = self._convert_api_data_to_profiles(raw_response)
                logger.info(f"Successfully converted {len(profiles)} profiles from API response for {user_phone}")

                # Don't cache here - already cached in authentication_service

                return {"success": True, "profiles": profiles, "from_cache": False}

            logger.info(f"Authentication failed for {user_phone}: {auth_response.get('message', 'Unknown error')}")
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

    def _extract_user_name(self, profiles: List[Dict]) -> Optional[str]:
        """Extract user's first name from profiles."""
        try:
            for profile in profiles:
                user_data = profile.get('user_data', {})
                full_name = user_data.get('fullName')
                if full_name and full_name.strip():
                    # Extract first name
                    first_name = full_name.strip().split()[0]
                    return first_name.title()
            return None
        except Exception as e:
            logger.warning(f"Error extracting user name: {e}")
            return None

    async def _handle_neutral_greeting(self, user_phone: str, profiles: List[Dict],
                                       session: ConversationSession) -> Dict[str, Any]:
        """Handle Case 1: Neutral/Greeting Start."""
        try:
            # Group profiles by role
            buyer_profiles = [p for p in profiles if p.get('role') == 'buyer']
            seller_profiles = [p for p in profiles if p.get('role') == 'seller']

            logger.info(f"buyer profile:{buyer_profiles}, seller_profile:{seller_profiles}")

            # Get user's name from the first available profile
            user_name = self._extract_user_name(profiles)

            logger.info(f"user_name:{user_name}")
            
            # Determine greeting based on profile count
            if len(profiles) == 1 and user_name:
                greeting = f"👋 Hi {user_name}!"
            else:
                greeting = "👋 Hi there!"

            # Case: Only buyer profiles exist
            if buyer_profiles and not seller_profiles:
                message_parts = [
                    greeting,
                    "I can assist you with both Buying (creating or checking RFQs) and Selling (responding to buyer requests)",
                    "",
                    "Please select your profile to continue:"
                ]

                profile_options = []
                option_num = 1

                # Add all buyer profiles
                for profile in buyer_profiles:
                    message_parts.append(f" {option_num}. {profile['email']} — Buyer")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Buyer"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}. Add or Register a new profile (New Buyer or New Seller)")
                profile_options.append({
                    "number": option_num,
                    "action": "register_new",
                    "display": "Add or Register a new profile "
                })

                message_parts.extend([
                    "",
                    "Reply with the number corresponding to your account to continue."
                ])

            # Case: Only seller profiles exist
            elif seller_profiles and not buyer_profiles:
                message_parts = [
                    greeting,
                    "I can assist you with both Selling (responding to buyer requests) or Buying (creating or checking RFQs)",
                    "",
                    "Please select your profile to continue:"
                ]

                profile_options = []
                option_num = 1

                # Add all seller profiles
                for profile in seller_profiles:
                    message_parts.append(f" {option_num}. {profile['email']} — Seller")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Seller"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}. Add or Register a new profile (New Seller or New Buyer)")
                profile_options.append({
                    "number": option_num,
                    "action": "register_new",
                    "display": "Add or Register a new profile"
                })

                message_parts.extend([
                    "",
                    "Reply with the number corresponding to your account to continue."
                ])

            # Case: Both buyer and seller profiles exist
            else:
                message_parts = [
                    greeting,
                    "I can help you with both Buying (creating/checking RFQs) and Selling (responding to buyer requests).",
                    "",
                    "What would you like to start with today, buying or selling?",
                    "",
                    "Please select your profile to continue:"
                ]

                profile_options = []
                option_num = 1

                # Add buyer profiles
                for profile in buyer_profiles:
                    message_parts.append(f" {option_num}. {profile['email']} — Buyer")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Buyer"
                    })
                    option_num += 1

                # Add seller profiles
                for profile in seller_profiles:
                    message_parts.append(f" {option_num}. {profile['email']} — Seller")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Seller"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}. Add or Register a new profile (Buyer or Seller)")
                profile_options.append({
                    "number": option_num,
                    "action": "register_new",
                    "display": "Add or Register a new profile"
                })

                message_parts.extend([
                    "",
                    "Reply with the number corresponding to your account to continue."
                ])

            # Store in session for later reference
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_options'] = profile_options
            session.workflow_state['profile_selection_stage'] = 'neutral_greeting'

            # Send profile selection message
            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)

            return {
                "status": "profile_selection_sent",
                "profiles_count": len(profiles),
                "selection_type": "neutral_greeting"
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
                # Case 7: Intent Mismatch - User wants to buy but only has Seller account(s)
                return await self._handle_intent_mismatch(user_phone, session, "buyer", profiles)

            if len(buyer_profiles) == 1:
                # Single buyer profile - show selection options
                profile = buyer_profiles[0]

                message_parts = [
                    "👋 Hi there! I understand you want to buy items. Please choose:"
                ]

                profile_options = [
                    {
                        "number": 1,
                        "profile": profile,
                        "display": f"{profile['email']} — Buyer"
                    },
                    {
                        "number": 2,
                        "action": "register_buyer",
                        "display": "Register a new Buyer account"
                    }
                ]

                message_parts.append(f" 1. {profile['email']} — Buyer")
                message_parts.append(f" 2. Register a new Buyer account")

                # Store context in session
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['profile_selection_stage'] = 'buyer_intent'
                session.workflow_state['profile_options'] = profile_options
                session.workflow_state['original_message'] = message
                session.workflow_state['original_intent'] = intent_result

                full_message = "\n".join(message_parts)
                await self.whatsapp_service.send_message(user_phone, full_message)

                return {
                    "status": "single_buyer_profile_selection_presented",
                    "options_count": len(profile_options)
                }

            else:
                # Multiple buyer profiles - show selection
                message_parts = [
                    "👋 Hi there! I understand you want to buy items. Please choose which Buyer profile you'd like to continue with:\n"
                ]

                profile_options = []
                option_num = 1

                for profile in buyer_profiles:
                    message_parts.append(f" {option_num}. {profile['email']} — Buyer")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Buyer"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}. Register a new Buyer account")
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
                # Case 7: Intent Mismatch - User wants to sell but only has Buyer account(s)
                return await self._handle_intent_mismatch(user_phone, session, "seller", profiles)

            if len(seller_profiles) == 1:
                # Single seller profile - auto-select and proceed
                profile = seller_profiles[0]

                message_parts = [
                    f"👋 Hi there! You’d like to sell items — great!",
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
                    "👋 Hi there! I understand you want to sell items. Please choose which Seller profile you'd like to continue with:\n\n"
                ]

                profile_options = []
                option_num = 1

                for profile in seller_profiles:
                    message_parts.append(f" {option_num}. {profile['email']} — Seller")
                    profile_options.append({
                        "number": option_num,
                        "profile": profile,
                        "display": f"{profile['email']} — Seller"
                    })
                    option_num += 1

                # Add registration option
                message_parts.append(f" {option_num}. Register a new Seller account")
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
        """Handle Case 4: RFQ Status Check - Common Intent (Buyer & Seller Applicable)."""
        try:
            # Handle single profile case - proceed directly
            if len(profiles) == 1:
                profile = profiles[0]
                role_display = "Buyer" if profile['role'] == 'buyer' else "Seller"

                # Auto-select single profile and proceed to RFQ status check
                logger.info(f"Single profile found for RFQ status check: {profile['email']} ({role_display})")
                return await self._set_active_profile_and_proceed(
                    user_phone, profile, session, "check RFQ status", "rfq_status_check"
                )

            # Multiple profiles case - ask user to choose
            message_parts = [
                "👋 Hi there! I understand you'd like to check an RFQ status.",
                "",
                "Please choose which profile you'd like to use:\n\n"
            ]

            profile_options = []
            option_num = 1

            for profile in profiles:
                role_display = "Buyer" if profile['role'] == 'buyer' else "Seller"
                message_parts.append(f" {option_num}. {profile['email']} — {role_display}")
                profile_options.append({
                    "number": option_num,
                    "profile": profile,
                    "display": f"{profile['email']} — {role_display}"
                })
                option_num += 1

            message_parts.append("")
            message_parts.append("Reply with the number to continue.")

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
                "👋 Hi there! I can help you with:\n\n",
                "• 🛒 Buying items (Raise or Check RFQs)",
                "• 💼 Selling items (Respond to RFQs)",
                "",
                "Please tell me if you'd like to continue as a Buyer or Seller, or choose from your saved profiles below:"
            ]

            profile_options = []
            option_num = 1

            for profile in profiles:
                role_display = "Buyer" if profile['role'] == 'buyer' else "Seller"
                message_parts.append(f" {option_num}. {role_display} — {profile['email']}")
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
        """Handle Case 6: New User (No Existing Profiles)."""
        try:
            # Case 6: New User Registration Flow
            message = (
                "👋 **Hi there!**\n"
                "It looks like there’s no registered account linked to this number or email.\n\n"
                "Let’s get you started by creating a new profile so you can easily raise or respond to RFQs.\n\n"
                "Please select one of the options below 👇\n"
                "1. **Register as Buyer** – to create and manage RFQs for your requirements\n"
                "2. **Register as Seller** – to receive and respond to buyer RFQs\n"
                "3. **Exit**\n\n"
                "Please reply with the number (1, 2, or 3) or type **Buyer**, **Seller**, or **Exit** to continue."
            )

            # Store registration options in session
            profile_options = [
                {"number": 1, "action": "register_buyer", "display": "Register as Buyer"},
                {"number": 2, "action": "register_seller", "display": "Register as Seller"},
                {"number": 3, "action": "exit", "display": "Exit"}
            ]

            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_selection_stage'] = 'new_user_registration'
            session.workflow_state['profile_options'] = profile_options

            await self.whatsapp_service.send_message(user_phone, message)

            return {
                "status": "new_user_registration_presented",
                "reason": "no_profiles_found"
            }

        except Exception as e:
            logger.error(f"Error handling no profiles found for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _parse_profile_selection(self, message: str, profile_options: List[Dict]) -> Optional[Dict]:
        """Parse user's profile selection from message using intelligent analysis."""
        try:
            message_lower = message.strip().lower()

            # Check for explicit existing account selection phrases
            existing_phrases = [
                'use existing', 'existing account', 'continue with existing',
                'use current', 'current account', 'existing profile',
                'continue with current', 'use my account', 'my existing'
            ]

            # Check for new registration phrases
            new_phrases = [
                'new account', 'register new', 'create new', 'new profile',
                'register a new', 'create a new', 'new buyer', 'new seller',
                'register me', 'sign me up', 'create account'
            ]

            # Use the user selection tool for comprehensive analysis if available
            if self.user_selection_tool:
                analysis_result = await self.user_selection_tool.analyze_user_selection(message, profile_options)

                logger.info(f"Selection analysis: {analysis_result}")

                # Check for registration intent first
                register_info = analysis_result.get('register', {})
                register_type = register_info.get('type')

                if register_type in ['buyer', 'seller']:
                    logger.info(f"Registration intent detected in profile selection: {register_type}")
                    # Create a special option for registration
                    return {
                        "action": f"register_{register_type}",
                        "display": f"Register as {register_type.title()}",
                        "registration_type": register_type
                    }

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
                # Enhanced fallback parsing with phrase detection
                return await self._enhanced_simple_parse_profile_selection(message, profile_options)

        except Exception as e:
            logger.error(f"Error parsing profile selection: {e}")
            # Fallback to simple parsing on error
            return await self._enhanced_simple_parse_profile_selection(message, profile_options)

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

    async def _enhanced_simple_parse_profile_selection(self, message: str, profile_options: List[Dict]) -> Optional[
        Dict]:
        """Enhanced fallback parsing for profile selection with phrase detection."""
        try:
            message = message.strip()
            message_lower = message.lower()

            # Check for explicit existing account selection phrases
            existing_phrases = [
                'use existing', 'existing account', 'continue with existing',
                'use current', 'current account', 'existing profile',
                'continue with current', 'use my account', 'my existing'
            ]

            # Check for new registration phrases
            new_phrases = [
                'new account', 'register new', 'create new', 'new profile',
                'register a new', 'create a new', 'new buyer', 'new seller',
                'register me', 'sign me up', 'create account'
            ]

            # Try number parsing first (most reliable)
            try:
                selection_num = int(message)
                for option in profile_options:
                    if option.get('number') == selection_num:
                        logger.info(f"Enhanced parsing matched number {selection_num} to option: {option}")
                        return option
            except ValueError:
                pass

            # Check for registration intent
            registration_type = await self._detect_registration_intent(message)
            if registration_type:
                logger.info(f"Enhanced parsing detected registration intent: {registration_type}")
                return {
                    "action": f"register_{registration_type}",
                    "display": f"Register as {registration_type.title()}",
                    "registration_type": registration_type
                }

            # Check for new registration phrases
            for phrase in new_phrases:
                if phrase in message_lower:
                    for option in profile_options:
                        if 'action' in option and 'register' in option['action']:
                            return option

            # Check for existing account phrases
            for phrase in existing_phrases:
                if phrase in message_lower:
                    for option in profile_options:
                        if 'profile' in option:
                            return option

            # Use AI analysis if phrases not found
            if hasattr(self, 'user_selection_tool') and self.user_selection_tool:
                try:
                    analysis_result = await self.user_selection_tool.analyze_user_selection(message, profile_options)
                    if analysis_result.get('selected_option') and not analysis_result.get('requires_clarification'):
                        selected_num = analysis_result['selected_option']
                        for option in profile_options:
                            if option.get('number') == selected_num:
                                return option
                except Exception:
                    pass

            return None

        except Exception as e:
            logger.error(f"Error in enhanced profile selection parsing: {e}")
            return None

    async def _simple_parse_profile_selection(self, message: str, profile_options: List[Dict]) -> Optional[Dict]:
        """Simple fallback parsing for profile selection."""
        return await self._enhanced_simple_parse_profile_selection(message, profile_options)

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
                elif action == 'show_all_profiles':
                    return await self._show_all_profiles(user_phone, session)
                elif action == 'exit':
                    return await self._handle_exit_action(user_phone, session)

            # Handle direct registration intent detected in parsing
            if 'registration_type' in selected_option:
                registration_type = selected_option['registration_type']
                logger.info(f"Processing direct registration intent: {registration_type}")

                if registration_type == 'buyer':
                    return await self._redirect_to_buyer_registration(user_phone, session, "")
                elif registration_type == 'seller':
                    return await self._redirect_to_seller_registration(user_phone, session, "")

            # Handle profile selection
            if 'profile' in selected_option:
                profile = selected_option['profile']

                # Get original context
                original_message = session.workflow_state.get('original_message', '')
                original_intent = session.workflow_state.get('original_intent', {})
                selection_stage = session.workflow_state.get('profile_selection_stage', '')

                # Determine next action based on selection stage
                if selection_stage == 'buyer_intent':
                    # Set active profile and show buying options
                    result = await self._set_active_profile_and_proceed(
                        user_phone, profile, session, original_message, "buy_something"
                    )

                    # Check if authentication was successful or if we should show buying options anyway
                    if result.get('status') == 'profile_selected_and_authenticated' or result.get(
                            'redirect_to_main_flow'):
                        # Show buying options with buttons
                        buying_message = (
                            f"Got it, you'd like to buy items!\n"
                            f"Let's continue with your Buyer profile ({profile['email']}).\n"
                            f"What would you like to do?"
                        )
                        buttons_config = [
                            {"id": "create_rfq", "title": "Create new RFQ"},
                            {"id": "search_bfs", "title": "Search Stocks"}
                        ]

                        await self.whatsapp_service.send_configurable_buttons(
                            user_phone,
                            buying_message,
                            buttons_config
                        )

                        return {
                            "status": "buyer_options_presented",
                            "user_type": profile['role'],
                            "email": profile['email']
                        }
                    else:
                        # Authentication failed or requires additional steps
                        return result
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
            # Get user data and perform verification check
            user_data = profile['user_data']

            # CRITICAL: Perform verification check (including domain check) before storing session
            verification_check = await self.authentication_service.verification_check_service.check_and_enforce_verification(
                user_phone, user_data
            )

            if not verification_check.get("access_granted"):
                # User doesn't meet verification requirements
                logger.info(f"Profile selection blocked due to verification requirements: {verification_check}")
                redirect_info = verification_check.get("redirect_info", {})

                if verification_check.get("redirect_to_support"):
                    # Redirect to support
                    support_message = redirect_info.get("message", "Please contact our support team for assistance.")
                    await self.whatsapp_service.send_message(user_phone, support_message)

                    return {
                        "status": "verification_failed",
                        "reason": redirect_info.get("reason"),
                        "redirect_to_support": True
                    }
                else:
                    # OTP verification required - set session stage for OTP handling
                    if verification_check.get("otp_sent") and redirect_info.get("flow") == "email_verification":
                        from app.services.workflow_manager import WorkflowManager
                        from app.models import WorkflowType
                        WorkflowManager.set_workflow_type(session, WorkflowType.authentication,
                                                          caller="profile_selection")
                        session.workflow_state["authentication_stage"] = "email_otp"
                        session.workflow_state["otp_email"] = redirect_info.get("email")
                        session.workflow_state["otp_retry_count"] = 0
                        session.workflow_state["selected_user"] = user_data
                        session.workflow_state["filtered_users"] = [user_data]  # Add filtered_users for OTP validation
                        # Clear profile selection stage to prevent conflicts
                        session.workflow_state.pop("profile_selection_stage", None)
                        session.workflow_state.pop("profile_options", None)
                        logger.info(f"Set authentication stage to email_otp for {user_phone}")

                    return {
                        "status": "verification_required",
                        "redirect_info": redirect_info
                    }

            # Verification passed - proceed with session storage
            user_details = User.from_api_response(user_data)
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

            # Set active profile first with verification check
            result = await self._set_active_profile_and_proceed(
                user_phone, profile, session, "", "general_inquiry"
            )

            if result.get('status') != 'profile_selected_and_authenticated':
                # Handle verification failures
                if result.get('status') == 'verification_failed':
                    return result
                elif result.get('status') == 'verification_required':
                    return result
                else:
                    return result

            # Show role-specific menu with buttons
            if role == 'buyer':
                # Get name from user_data fullName field
                user_data = profile.get('user_data', {})
                name = user_data.get('fullName')
                if name:
                    name = name.title()
                else:
                    name = 'there'
                menu_message = f"Let's continue with your Buyer profile ({email})."
                header = f"Hi {name}! What would you like to do today?"
                buttons_config = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]
            else:  # seller
                # Get name from user_data fullName field
                user_data = profile.get('user_data', {})
                name = user_data.get('fullName')
                if name:
                    name = name.title()
                else:
                    name = 'there'
                menu_message = f" You're now using your Seller profile ({email})."
                header = f"Hi {name} ! What would you like to do today?"
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
                "1. Buyer (to create RFQs and purchase items)\n"
                "2. Seller (to respond to RFQs and sell items)\n\n"
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
        """Redirect to buyer registration flow and create new buyer profile."""
        try:
            logger.info(f"Redirecting {user_phone} to buyer registration with message: '{message}'")

            # Import registration service
            from app.services.registration_service import RegistrationService

            # Initialize registration service
            registration_service = RegistrationService(
                whatsapp_service=self.whatsapp_service,
                openai_service=OpenAIService() if not hasattr(self, 'openai_service') else self.openai_service
            )

            # Set workflow type to registration
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="profile_selection")

            # Initialize session state for registration
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update({
                "registration_stage": "data_collection",
                "user_type": "buyer",
                "original_message": message,
                "registration_entities": {}  # Will be filled during data collection
            })

            # Start the registration process
            result = await registration_service.initiate_registration(
                user_phone=user_phone,
                session=session,
                user_type="buyer",
                message=message
            )

            logger.info(f"Buyer registration initiated: {result}")

            return {
                "status": "redirected_to_buyer_registration",
                "workflow_type": "registration",
                "user_type": "buyer",
                "registration_result": result
            }

        except Exception as e:
            logger.error(f"Error redirecting to buyer registration for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _redirect_to_seller_registration(self, user_phone: str, session: ConversationSession,
                                               message: str) -> Dict[str, Any]:
        """Redirect to seller registration flow and create new seller profile."""
        try:
            logger.info(f"Redirecting {user_phone} to seller registration with message: '{message}'")

            # Import registration service
            from app.services.registration_service import RegistrationService

            # Initialize registration service
            registration_service = RegistrationService(
                whatsapp_service=self.whatsapp_service,
                openai_service=OpenAIService() if not hasattr(self, 'openai_service') else self.openai_service
            )

            # Set workflow type to registration
            WorkflowManager.set_workflow_type(session, WorkflowType.registration, caller="profile_selection")

            # Initialize session state for registration
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update({
                "registration_stage": "data_collection",
                "user_type": "seller",
                "original_message": message,
                "registration_entities": {}  # Will be filled during data collection
            })

            # Start the registration process
            result = await registration_service.initiate_registration(
                user_phone=user_phone,
                session=session,
                user_type="seller",
                message=message
            )

            logger.info(f"Seller registration initiated: {result}")

            return {
                "status": "redirected_to_seller_registration",
                "workflow_type": "registration",
                "user_type": "seller",
                "registration_result": result
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
                    message_parts.append(f" {option['number']}. {option['display']}")
                elif 'action' in option:
                    message_parts.append(f" {option['number']}. {option['display']}")

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

    async def _handle_registration_type_response(self, user_phone: str, message: str,
                                                 session: ConversationSession) -> Dict[str, Any]:
        """Handle user response to registration type choice (buyer/seller)."""
        try:
            message_lower = message.strip().lower()

            # First try to detect registration intent using the user selection tool
            detected_type = await self._detect_registration_intent(message)

            if detected_type == 'buyer':
                session.workflow_state.pop('awaiting_registration_type', None)
                return await self._redirect_to_buyer_registration(user_phone, session, message)
            elif detected_type == 'seller':
                session.workflow_state.pop('awaiting_registration_type', None)
                return await self._redirect_to_seller_registration(user_phone, session, message)

            # Fallback to simple keyword matching
            elif any(keyword in message_lower for keyword in ['buyer', 'buy', '1', 'one', 'first']):
                session.workflow_state.pop('awaiting_registration_type', None)
                return await self._redirect_to_buyer_registration(user_phone, session, message)

            # Check for seller keywords
            elif any(keyword in message_lower for keyword in ['seller', 'sell', '2', 'two', 'second']):
                session.workflow_state.pop('awaiting_registration_type', None)
                return await self._redirect_to_seller_registration(user_phone, session, message)

            else:
                # Use the user selection tool to detect registration intent
                detected_type = await self._detect_registration_intent(message)

                if detected_type == 'buyer':
                    session.workflow_state.pop('awaiting_registration_type', None)
                    return await self._redirect_to_buyer_registration(user_phone, session, message)
                elif detected_type == 'seller':
                    session.workflow_state.pop('awaiting_registration_type', None)
                    return await self._redirect_to_seller_registration(user_phone, session, message)
                else:
                    # Still unclear - ask for clarification
                    clarification_message = (
                        "Please specify whether you'd like to register as:\n"
                        "• Type 'buyer' or '1' for Buyer registration\n"
                        "• Type 'seller' or '2' for Seller registration"
                    )
                    await self.whatsapp_service.send_message(user_phone, clarification_message)

                    return {
                        "status": "registration_type_clarification_sent"
                    }

        except Exception as e:
            logger.error(f"Error handling registration type response for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _detect_registration_intent(self, message: str) -> Optional[str]:
        """Detect explicit registration intent from user message using user selection tool."""
        try:
            # Use the user selection tool to detect registration intent
            if self.user_selection_tool:
                # Create a dummy profile options list for the analysis
                dummy_options = []
                analysis_result = await self.user_selection_tool.analyze_user_selection(message, dummy_options)

                # Extract registration intent from the analysis
                register_info = analysis_result.get('register', {})
                register_type = register_info.get('type')

                if register_type in ['buyer', 'seller']:
                    logger.info(f"User selection tool detected registration intent: {register_type}")
                    return register_type

            # Fallback to simple phrase matching - ONLY for explicit registration phrases
            # Ensure message is a string
            if not isinstance(message, str):
                message = str(message)
            message_lower = message.lower().strip()

            # Only explicit registration phrases - removed generic 'buyer', 'buy', 'seller', 'sell'
            buyer_registration_phrases = [
                'register me as buyer', 'register as buyer', 'register me as a buyer',
                'sign me up as buyer', 'sign up as buyer', 'create buyer account',
                'i want to register as buyer', 'register buyer account', 'add me as buyer',
                'new buyer account', 'create new buyer'
            ]

            seller_registration_phrases = [
                'register me as seller', 'register as seller', 'register me as a seller',
                'sign me up as seller', 'sign up as seller', 'create seller account',
                'i want to register as seller', 'register seller account', 'add me as seller',
                'new seller account', 'create new seller'
            ]

            # Check for explicit registration phrases only
            for phrase in buyer_registration_phrases:
                if phrase in message_lower:
                    logger.info(f"Fallback: Matched buyer registration phrase '{phrase}' in message '{message}'")
                    return 'buyer'

            for phrase in seller_registration_phrases:
                if phrase in message_lower:
                    logger.info(f"Fallback: Matched seller registration phrase '{phrase}' in message '{message}'")
                    return 'seller'

            logger.info(f"No explicit registration intent detected for message: '{message}'")
            return None

        except Exception as e:
            logger.error(f"Error detecting registration intent: {e}")
            return None

    async def _handle_intent_mismatch(self, user_phone: str, session: ConversationSession,
                                      target_role: str, existing_profiles: List[Dict]) -> Dict[str, Any]:
        """Handle Case 7: Intent Mismatch - User wants one role but only has the other."""
        try:
            # Determine the existing role
            existing_role = "seller" if target_role == "buyer" else "buyer"

            # Create appropriate message based on target role
            if target_role == "buyer":
                message_parts = [
                    "It looks like you don't have a Buyer profile linked to your account.",
                    "Would you like to register as a Buyer to continue?",
                    "",
                    "1. Register as Buyer",
                    "2. Exit",
                    "",
                    "Reply with the number corresponding to your choice."
                ]
            else:  # target_role == "seller"
                message_parts = [
                    "It looks like you don't have a Seller profile linked to your account.",
                    "Would you like to register as a Seller to continue?",
                    "",
                    "1. Register as Seller",
                    "2. Exit",
                    "",
                    "Reply with the number corresponding to your choice."
                ]

            # Store intent mismatch options in session
            profile_options = [
                {"number": 1, "action": f"register_{target_role}", "display": f"Register as {target_role.title()}"},
                {"number": 2, "action": "exit", "display": "Exit"}
            ]

            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_selection_stage'] = 'intent_mismatch'
            session.workflow_state['profile_options'] = profile_options
            session.workflow_state['target_role'] = target_role
            session.workflow_state['existing_profiles'] = existing_profiles

            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)

            return {
                "status": "intent_mismatch_handled",
                "target_role": target_role,
                "existing_role": existing_role,
                "options_count": len(profile_options)
            }

        except Exception as e:
            logger.error(f"Error handling intent mismatch for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_intent_mismatch_response(self, user_phone: str, message: str,
                                               session: ConversationSession) -> Dict[str, Any]:
        """Handle user response to intent mismatch options."""
        try:
            target_role = session.workflow_state.get('target_role')
            profile_options = session.workflow_state.get('profile_options', [])

            if not target_role or not profile_options:
                logger.error(f"Missing intent mismatch data for {user_phone}")
                return {"status": "restart_profile_selection"}

            # Parse user selection
            selected_option = await self._parse_profile_selection(message, profile_options)

            if selected_option:
                action = selected_option.get('action')

                if action == f"register_{target_role}":
                    # User chose to register for the target role
                    logger.info(f"User chose to register as {target_role} to resolve intent mismatch")

                    # Clear intent mismatch state
                    session.workflow_state.pop('profile_selection_stage', None)
                    session.workflow_state.pop('profile_options', None)
                    session.workflow_state.pop('target_role', None)
                    session.workflow_state.pop('existing_profiles', None)

                    # Redirect to appropriate registration
                    if target_role == "buyer":
                        return await self._redirect_to_buyer_registration(user_phone, session, message)
                    else:
                        return await self._redirect_to_seller_registration(user_phone, session, message)

                elif action == "exit":
                    # User chose to exit
                    return await self._handle_exit_action(user_phone, session)

            # Invalid selection - show options again
            message_parts = [
                "Please select a valid option:",
                "",
                f"1. Register as {target_role.title()}",
                "2. Exit",
                "",
                "Reply with the number corresponding to your choice."
            ]

            retry_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, retry_message)

            return {
                "status": "intent_mismatch_retry_sent",
                "target_role": target_role
            }

        except Exception as e:
            logger.error(f"Error handling intent mismatch response for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_new_user_registration_response(self, user_phone: str, message: str,
                                                     session: ConversationSession) -> Dict[str, Any]:
        """Handle user response to new user registration options."""
        try:
            profile_options = session.workflow_state.get('profile_options', [])

            if not profile_options:
                logger.error(f"No profile options found for new user registration response from {user_phone}")
                return {"status": "restart_profile_selection"}

            # Parse user selection
            selected_option = await self._parse_profile_selection(message, profile_options)

            if selected_option:
                action = selected_option.get('action')

                if action == 'register_buyer':
                    logger.info(f"User selected register as buyer from new user registration")
                    # Clear profile selection state
                    session.workflow_state.pop('profile_selection_stage', None)
                    session.workflow_state.pop('profile_options', None)
                    return await self._redirect_to_buyer_registration(user_phone, session, message)

                elif action == 'register_seller':
                    logger.info(f"User selected register as seller from new user registration")
                    # Clear profile selection state
                    session.workflow_state.pop('profile_selection_stage', None)
                    session.workflow_state.pop('profile_options', None)
                    return await self._redirect_to_seller_registration(user_phone, session, message)

                elif action == 'exit':
                    return await self._handle_exit_action(user_phone, session)

            # Invalid selection - show options again
            retry_message = (
                "Please select a valid option:\n\n"
                "1. Register as Buyer\n"
                "2. Register as Seller\n"
                "3. Exit\n\n"
                "Reply with the number corresponding to your choice."
            )
            await self.whatsapp_service.send_message(user_phone, retry_message)

            return {
                "status": "new_user_registration_retry_sent"
            }

        except Exception as e:
            logger.error(f"Error handling new user registration response for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _handle_exit_action(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle user selecting exit option."""
        try:
            message = "Thank you for your interest. Feel free to reach out anytime if you need assistance with procurement!"
            await self.whatsapp_service.send_message(user_phone, message)

            # Clear session state
            session.workflow_state = {}
            session.workflow_type = None

            return {
                "status": "user_exited",
                "session_cleared": True
            }

        except Exception as e:
            logger.error(f"Error handling exit action for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def handle_bfs_coming_soon_response(self, user_phone: str, profile: Dict) -> Dict[str, Any]:
        """Handle BFS search coming soon response with follow-up buttons."""
        try:
            # Send coming soon message
            coming_soon_message = "BFS search is coming soon!"
            await self.whatsapp_service.send_message(user_phone, coming_soon_message)

            # Show the three buttons as requested
            buttons_config = [
                {"id": "create_rfq", "title": "Create new RFQ"},
                {"id": "rfq_status", "title": "Check RFQ Status"},
                {"id": "contact_support", "title": "Get Support Info"}
            ]

            await self.whatsapp_service.send_configurable_buttons(
                user_phone,
                "What would you like to do?",
                buttons_config
            )

            return {
                "status": "bfs_coming_soon_handled",
                "user_type": profile['role'],
                "email": profile['email']
            }

        except Exception as e:
            logger.error(f"Error handling BFS coming soon response for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _check_role_filter_request(self, user_phone: str, message: str,
                                         session: ConversationSession) -> Optional[Dict[str, Any]]:
        """Check if user is requesting to filter profiles by role (buyer/seller)."""
        try:
            message_lower = message.strip().lower()

            # Check for buyer role request
            if any(keyword in message_lower for keyword in ['buyer', 'buy']):
                return await self._show_filtered_profiles(user_phone, session, 'buyer')

            # Check for seller role request
            elif any(keyword in message_lower for keyword in ['seller', 'sell']):
                return await self._show_filtered_profiles(user_phone, session, 'seller')

            return None

        except Exception as e:
            logger.error(f"Error checking role filter request for {user_phone}: {e}")
            return None

    async def _show_filtered_profiles(self, user_phone: str, session: ConversationSession,
                                      role_filter: str) -> Dict[str, Any]:
        """Show profiles filtered by specific role."""
        try:
            # Get user profiles from cache or API
            profiles_result = await self._get_user_profiles(user_phone, "", session)

            if not profiles_result.get('success'):
                return await self._handle_no_profiles_found(user_phone, "general_inquiry", session)

            profiles = profiles_result.get('profiles', [])

            # Filter profiles by requested role
            filtered_profiles = [p for p in profiles if p.get('role') == role_filter]

            if not filtered_profiles:
                # No profiles found for this role - offer registration
                message_parts = [
                    f"You don't have any {role_filter.title()} profiles linked to your account.",
                    f"Would you like to register as a {role_filter.title()}?",
                    "",
                    f"1. Register as {role_filter.title()}",
                    "2. Show all profiles",
                    "3. Exit",
                    "",
                    "Reply with the number corresponding to your choice."
                ]

                profile_options = [
                    {"number": 1, "action": f"register_{role_filter}", "display": f"Register as {role_filter.title()}"},
                    {"number": 2, "action": "show_all_profiles", "display": "Show all profiles"},
                    {"number": 3, "action": "exit", "display": "Exit"}
                ]

                session.workflow_state = session.workflow_state or {}
                session.workflow_state['profile_options'] = profile_options
                session.workflow_state['profile_selection_stage'] = f'no_{role_filter}_profiles'

                full_message = "\n".join(message_parts)
                await self.whatsapp_service.send_message(user_phone, full_message)

                return {
                    "status": f"no_{role_filter}_profiles_message_sent",
                    "role_filter": role_filter
                }

            # If single profile, auto-select and show menu
            if len(filtered_profiles) == 1:
                selected_profile = filtered_profiles[0]
                return await self._show_role_based_menu(user_phone, selected_profile, session)

            # Multiple profiles - show selection
            message_parts = [
                f"Select your {role_filter.title()} account:",
                ""
            ]

            profile_options = []
            for i, profile in enumerate(filtered_profiles, 1):
                message_parts.append(f"{i}. {profile['email']}")
                profile_options.append({
                    "number": i,
                    "profile": profile,
                    "display": profile['email']
                })

            message_parts.extend([
                "",
                "Reply with the number corresponding to your choice."
            ])

            session.workflow_state = session.workflow_state or {}
            session.workflow_state['profile_options'] = profile_options
            session.workflow_state['profile_selection_stage'] = f'filtered_{role_filter}_profiles'
            session.workflow_state['role_filter'] = role_filter

            full_message = "\n".join(message_parts)
            await self.whatsapp_service.send_message(user_phone, full_message)

            return {
                "status": f"filtered_{role_filter}_profiles_shown",
                "profiles_count": len(filtered_profiles),
                "role_filter": role_filter
            }

        except Exception as e:
            logger.error(f"Error showing filtered profiles for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}

    async def _show_all_profiles(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Show all available profiles (reset filter)."""
        try:
            # Get user profiles from cache or API
            profiles_result = await self._get_user_profiles(user_phone, "", session)

            if not profiles_result.get('success'):
                return await self._handle_no_profiles_found(user_phone, "general_inquiry", session)

            profiles = profiles_result.get('profiles', [])

            # Use the neutral greeting handler to show all profiles
            return await self._handle_neutral_greeting(user_phone, profiles, session)

        except Exception as e:
            logger.error(f"Error showing all profiles for {user_phone}: {e}")
            return {"status": "error", "error": str(e)}