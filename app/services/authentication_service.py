"""
Authentication Service for WhatsApp Bot.

Handles complete user authentication flow including:
- Token validation
- User lookup via API
- Email confirmation workflow
- Email OTP validation
- Domain matching
- Session state management
- Integration with registration flow
"""

import logging
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime
from app.schemas.user import User
from app.models import ConversationSession, UserType, WorkflowType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.services.workflow_manager import WorkflowManager
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.schemas.user import User
from app.procucev_apis.auth_apis import AuthAPIService
from app.procucev_apis.register_apis import RegisterAPIService
from app.services.support_notification_service import SupportNotificationService
from app.services.user_cache_service import get_user_cache_service
from app.context import user_context
from app.services.auth_reg_service import AuthRegService
from app.services.otp_service import OTPService
from app.services.verification_check_service import VerificationCheckService

logger = logging.getLogger(__name__)


class AuthenticationService:
    """Handles user authentication and all sub-services."""
    
    def __init__(self, whatsapp_service: WhatsAppService = None, 
                 openai_service: OpenAIService = None,
                 response_helpers: ResponseHelpers = None,
                 session_manager=None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.openai_service = openai_service or OpenAIService()
        self.response_helpers = response_helpers or ResponseHelpers(self.openai_service)
        self.auth_redis_service = get_auth_redis_service()
        self.auth_api_service = AuthAPIService()
        self.register_api_service = RegisterAPIService()
        self.support_notification_service = SupportNotificationService()
        self.user_cache_service = get_user_cache_service()
        self.auth_reg_service = AuthRegService()
        self.otp_service = OTPService(self.register_api_service, self.whatsapp_service, self.support_notification_service)
        self.verification_check_service = VerificationCheckService(self.auth_api_service, self.otp_service, self.whatsapp_service)
        self.session_manager = session_manager  # Will be injected from ChatService
    
    async def validate_token(self, user_phone: str) -> Optional[User]:
        """Validate user token and check verification status before main flow access.
        
        Returns:
        - User object if authenticated and verified
        - Dict with verification_required=True if verification needed
        - False if token expired or max retries exceeded
        """
        try:
            # Normalize phone number (remove + prefix for consistent Redis keys)
            normalized_phone = user_phone.lstrip('+')
            logger.info(f"Token validation called for {normalized_phone}")
            user_data = await self.auth_redis_service.retrieve(normalized_phone)
            if user_data:
                # Check verification status before granting access
                verification_check = await self.verification_check_service.check_and_enforce_verification(user_phone, user_data)
                
                if verification_check.get("access_granted"):
                    # Save user details in global context
                    user_context.set(normalized_phone, {"user_details": user_data})
                    logger.info(f"Token validated and main flow access granted for user {normalized_phone}")
                    return user_data
                else:
                    # Verification required - refresh user data and retry
                    logger.info(f"Verification required for user {normalized_phone}, refreshing user data")
                    refresh_result = await self.verification_check_service.refresh_user_verification_status(user_phone)
                    
                    # Check if max retries exceeded and support redirection needed
                    if refresh_result.get("exit_flow"):
                        logger.info(f"Max retries exceeded for user {normalized_phone} - exiting flow")
                        return False  # Exit the authentication flow
                    
                    if refresh_result.get("success"):
                        # Re-check with fresh data
                        fresh_data = refresh_result.get("data", [])
                        if fresh_data:
                            # Use first user data for verification check
                            fresh_user_data = fresh_data[0] if isinstance(fresh_data, list) else fresh_data
                            fresh_check = await self.verification_check_service.check_and_enforce_verification(user_phone, fresh_user_data)
                            
                            if fresh_check.get("access_granted"):
                                # Update cached data and grant access
                                await self.auth_redis_service.store(normalized_phone, fresh_user_data, expiry_seconds=3600)
                                user_context.set(normalized_phone, {"user_details": fresh_user_data})
                                logger.info(f"Fresh verification check passed for user {normalized_phone}")
                                return fresh_user_data
                    
                    # Still requires verification - return verification info
                    logger.info(f"Token valid but verification still required for user {normalized_phone}")
                    return {"verification_required": True, "verification_info": verification_check.get("redirect_info", {})}

            # Token expired or not found
            logger.info(f"Token expired or not found for user {normalized_phone}")
            return False
        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return False
    
    async def store_user_session(self, user_phone: str, user_details: User) -> bool:
        """Store user session data in Redis."""
        try:
            # Normalize phone number (remove + prefix for consistent Redis keys)
            normalized_phone = user_phone.lstrip('+')
            session_data = user_details.dict()
            session_data["authenticated_at"] = datetime.now().isoformat()

            # Token expires after 1 hour of inactivity
            success = await self.auth_redis_service.store(normalized_phone, session_data, expiry_seconds=3600)  # 1 hour

            if success:
                logger.info(f"Session stored successfully for user {normalized_phone} (ID: {user_details.id})")
                # Refresh cache expiry to match session expiry
                await self.user_cache_service.refresh_cache_expiry(normalized_phone, 3600)
            else:
                logger.error(f"Failed to store session in Redis for user {normalized_phone}")

            return success
        except Exception as e:
            logger.error(f"Session storage error for {user_phone}: {e}")
            return False
    
    async def clear_user_token(self, user_phone: str) -> bool:
        """Clear user token/session and cached user data from Redis."""
        try:
            # Normalize phone number (remove + prefix for consistent Redis keys)
            normalized_phone = user_phone.lstrip('+')

            # Clear auth token
            auth_success = await self.auth_redis_service.delete_auth(normalized_phone)

            # Clear cached user data (user_cache_service normalizes internally)
            cache_success = await self.user_cache_service.clear_user_data(user_phone)

            if auth_success:
                logger.info(f"User token cleared successfully for {normalized_phone}")
            else:
                logger.warning(f"Failed to clear token for {normalized_phone} or token not found")

            if cache_success:
                logger.info(f"User cache cleared successfully for {normalized_phone}")

            return auth_success  # Return auth token success as primary indicator

        except Exception as e:
            logger.error(f"Token clearing error for {user_phone}: {e}")
            return False
    
    async def user_authenticate(self, user_phone: str, message: str,
                              session: ConversationSession, intent: str = None) -> Dict[str, Any]:
        """Handles token validation failure and routes to user authentication flow."""
        try:
            logger.info(f"Authenticating user {user_phone} with intent: {intent}")

            # First check if we have cached user data
            cached_data = await self.user_cache_service.get_user_data(user_phone)
            if cached_data:
                logger.info(f"Using cached user data for {user_phone}")
                return {
                    "success": True,
                    "response": cached_data,
                    "is_registered": True,
                    "detected_intent": intent,
                    "from_cache": True
                }

            # If no cache, make API call
            auth_response = await self.auth_api_service.authenticate_user(user_phone)
            logger.info(f"Auth API service response: {auth_response}")

            if auth_response.get("success"):
                raw_response = auth_response.get("data", [])
                if raw_response:
                    # Cache the raw API response for future use
                    await self.user_cache_service.store_user_data(user_phone, raw_response)

                    return {
                        "success": True,
                        "response": raw_response,
                        "is_registered": auth_response.get("is_registered", True),
                        "detected_intent": intent
                    }
                else:
                    return {"success": False, "message": "User details not found"}

            return auth_response

        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return {"success": False, "message": str(e)}
    
    def filter_users_by_intent(self, raw_users: List[Dict], intent: str) -> Dict[str, Any]:
        """Filter users based on intent and return structured data using schemas."""
        try:
            from app.schemas.user import APIUserSchema

            logger.info(f"filter_users_by_intent called: intent='{intent}', raw_users_count={len(raw_users)}")

            # Normalize using schema
            users: List[APIUserSchema] = [APIUserSchema(**user) for user in raw_users]
            
            # 1. List filtering using intent
            if intent == "buy_something":
                filtered_users = [user for user in users if user.selfClient is True]
            elif intent == "sell_something":
                filtered_users = [user for user in users if user.selfClient is False]
            else:
                filtered_users = users
                        
            if not filtered_users:
                logger.info(f"filter_users_by_intent: No matching users found for intent '{intent}'")
                return {"success": False, "message": "No matching users found"}

            # 2. Aggregate the emails via user.username
            unique_emails = []
            for user in filtered_users:
                if user.username and user.username not in unique_emails:
                    unique_emails.append(user.username)

            # Convert to User for consistent response
            user_details_list = []
            for user in filtered_users:
                user_dict = user.dict()
                user_detail = User.from_api_response(user_dict)
                user_details_list.append(user_detail)

            logger.info(f"filter_users_by_intent success: filtered_users_count={len(filtered_users)}, unique_emails_count={len(unique_emails)}")

            return {
                "success": True,
                "filtered_users": [user.dict() for user in user_details_list],
                "unique_emails": unique_emails,
                "count": len(filtered_users)
            }
            
        except Exception as e:
            logger.error(f"User filtering error: {e}")
            return {"success": False, "message": str(e)}
    
    def create_user_details_from_email(self, filtered_users: List[Dict], selected_email: str) -> Optional[User]:
        """Create User from selected email and filtered users."""
        try:
            # Find user data matching the selected email
            selected_user = None
            for user in filtered_users:
                # Check both 'email' and 'username' fields as API uses 'username' for email
                user_email = user.get("email") or user.get("username")
                if user_email == selected_email:
                    selected_user = user
                    break
            
            if not selected_user:
                # If no exact match, use first user with same email pattern
                for user in filtered_users:
                    user_email = user.get("email") or user.get("username")
                    if selected_email in str(user_email or ""):
                        selected_user = user
                        break
            
            if not selected_user:
                logger.warning(f"No user found for email {selected_email}")
                return None
            
            # Use from_mixed_data to handle both API response and User dict formats
            return User.from_mixed_data(selected_user)
            
        except Exception as e:
            logger.error(f"Error creating user details from email: {e}")
            return None

    def _extract_user_details(self, api_response: list) -> Optional[User]:
        """Parse external API response and map into User."""
        try:
            if not api_response:
                return None
            
            user_data = AuthenticationHelpers.extract_user_details(api_response)
            if not user_data:
                return None
            
            # Use from_mixed_data to handle both API response and User dict formats
            return User.from_mixed_data(user_data)
            
        except Exception as e:
            logger.error(f"Error extracting user details: {e}")
            return None
    
    # 3. Next flow email confirmation
    async def initiate_email_confirmation(self, user_phone: str, session: ConversationSession,
                                        filtered_users: List, available_emails: List) -> Dict[str, Any]:
        """Initiate email confirmation process with improvements."""
        try:
            if not available_emails:
                return {"status": "redirect_to_registration"}
            
            session.workflow_state["email_options"] = available_emails
            session.workflow_state["filtered_users"] = filtered_users
            
            # IMPROVEMENT 1: Skip confirmation for single email - auto-process
            if len(available_emails) == 1:
                selected_email = available_emails[0]
                logger.info(f"Auto-processing single email: {selected_email}")
                return await self._process_selected_email(user_phone, session, selected_email, filtered_users)
            
            # Multiple emails - request selection
            logger.info(f"Requesting email selection from {len(available_emails)} options")
            session.workflow_state["confirmation_stage"] = "selection"
            return await self._request_email_selection_with_text(user_phone, session, available_emails, filtered_users)
                
        except Exception as e:
            logger.error(f"Email confirmation initiation error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def handle_email_confirmation(self, user_phone: str, message: str,
                                      session: ConversationSession, intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """Handle email confirmation response with intent-aware re-filtering."""
        try:
            email_options = session.workflow_state.get("email_options", [])
            filtered_users = session.workflow_state.get("filtered_users", [])
            confirmation_stage = session.workflow_state.get("confirmation_stage", "selection")

            if not email_options or not filtered_users:
                return {"status": "restart_authentication"}

            # NEW: Check for clear intent before processing email selection
            if confirmation_stage == "selection":
                # Use the passed intent result or fallback to stored result
                if not intent_result:
                    intent_result = session.workflow_state.get("current_intent_result", {})

                # If still no intent result, preserve the original intent from session if available
                if not intent_result:
                    # Check if we have a stored intent result from the original message
                    stored_intent = session.workflow_state.get("intent_result", {})
                    if stored_intent and stored_intent.get("intent"):
                        intent_result = stored_intent
                        logger.info(f"Using stored intent result from session: {intent_result}")
                    else:
                        logger.warning("No intent result available during email confirmation - using default")
                        intent_result = {"intent": "general_inquiry", "confidence": 50}


                intent = intent_result.get('intent')
                confidence = intent_result.get('confidence', 0)

                logger.info(f"Intent refinement check: {intent} ({confidence}%)")

                # Skip intent refinement for rfq_status_check and other non-transactional intents
                original_intent = session.workflow_state.get("intent_result", {}).get("intent")
                if original_intent in ["rfq_status_check", "general_inquiry", "reference_request", "session_inquiry"]:
                    logger.info(f"Skipping intent refinement for original intent: {original_intent}")
                elif intent in ["buy_something", "sell_something"] and confidence > 75:
                    # Get original users and re-filter
                    original_users = session.workflow_state.get("original_auth_users", filtered_users)
                    filter_result = self.filter_users_by_intent(original_users, intent)

                    if filter_result.get("success") and set(filter_result["unique_emails"]) != set(email_options):
                        # Intent refinement applied - update session state
                        logger.info(f"Intent refinement applied: {intent} ({confidence}%) - "
                                   f"Emails: {len(email_options)} → {len(filter_result['unique_emails'])}")

                        session.workflow_state["email_options"] = filter_result["unique_emails"]
                        session.workflow_state["filtered_users"] = filter_result["filtered_users"]
                        email_options = filter_result["unique_emails"]
                        filtered_users = filter_result["filtered_users"]

                        # If only one email remains after refinement, auto-process it
                        if len(filter_result["unique_emails"]) == 1:
                            selected_email = filter_result["unique_emails"][0]
                            logger.info(f"Auto-processing single email after intent refinement: {selected_email}")
                            return await self._process_selected_email(user_phone, session, selected_email,
                                                                   filter_result["filtered_users"])

                        # Multiple emails still remain - show refined list
                        logger.info(f"Showing refined email list with {len(filter_result['unique_emails'])} options")
                        return await self._request_email_selection_with_text(
                            user_phone, session,
                            filter_result["unique_emails"],
                            filter_result["filtered_users"]
                        )
                    else:
                        logger.info(f"Intent refinement skipped - no change in email options or filtering failed")

            # Check if this is a button response
            # Removed button handling - using text-based selection

            if confirmation_stage in ["selection", "intent_filtered_selection"]:
                # Use ProfileSelectionService to handle the response
                from app.services.profile_selection_service import ProfileSelectionService
                profile_service = ProfileSelectionService(
                    whatsapp_service=self.whatsapp_service,
                    authentication_service=self,
                    openai_service=self.openai_service
                )
                
                # Handle profile selection response
                result = await profile_service.handle_profile_selection_response(
                    user_phone, message, session
                )
                
                # If profile was selected successfully, process it
                if result.get("status") == "profile_selected_and_authenticated":
                    selected_email = result.get("email")
                    if selected_email:
                        return await self._process_selected_email(user_phone, session, selected_email, filtered_users)
                
                return result
            
            elif confirmation_stage == "intent_clarification":
                # Handle intent clarification response
                return await self._handle_intent_clarification_response(user_phone, message, session)
                
            elif confirmation_stage == "confirmation":
                # AI-first confirmation validation
                selected_email = session.workflow_state.get("selected_email")
                is_confirmed = await self._ai_validate_confirmation_response(message)
                
                # if is_confirmed is None:
                #     # Fallback to pattern matching
                #     is_confirmed = await self._validate_confirmation_response(message)
                
                if is_confirmed:
                    return await self._process_selected_email(user_phone, session, selected_email, filtered_users)
                else:
                    # Reset to selection with text
                    session.workflow_state["confirmation_stage"] = "selection"
                    return await self._request_email_selection_with_text(user_phone, session, email_options, filtered_users)
            
        except Exception as e:
            logger.error(f"Email confirmation handling error: {e}")
            return {"status": "error", "error": str(e)}
    
    def _extract_emails_from_response(self, raw_response: List) -> List[str]:
        """Extract emails from API response."""
        try:
            emails = []
            for user_data in raw_response:
                user_emails = AuthenticationHelpers.extract_emails_from_user_data(user_data)
                emails.extend(user_emails)
            
            unique_emails = []
            for email in emails:
                if email and email not in unique_emails:
                    unique_emails.append(email)
            
            return unique_emails
            
        except Exception as e:
            logger.error(f"Email extraction error: {e}")
            return []
    
    async def _request_email_selection(self, user_phone: str, session: ConversationSession,
                                     emails: List[str]) -> Dict[str, Any]:
        """Request user to select email from list."""
        try:
            # Get user name from filtered users
            filtered_users = session.workflow_state.get("filtered_users", [])
            username = "there"
            if filtered_users:
                username = filtered_users[0].get("fullName", "there")
            
            # Generate email confirmation response using OpenAI with user type labels
            message = await self._generate_email_confirmation_response(username, emails, filtered_users)
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_selection_requested",
                "stage": "email_confirmation",
                "email_options": emails
            }
            
        except Exception as e:
            logger.error(f"Email selection request error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _generate_email_confirmation_response(self, username: str, emails: List[str], filtered_users: List[Dict] = None) -> str:
        """Generate email confirmation response using OpenAI."""
        try:
            # Format email list with buyer/seller labels
            if len(emails) == 1:
                # Single email - check if we have user type info
                email = emails[0]
                user_type = self._get_user_type_for_email(email, filtered_users)
                if user_type:
                    email_text = f"Hi {username}! \nIs this your {user_type.lower()} email: {email}?"
                else:
                    email_text = f"Hi {username}! \nIs this your email: {email}?"
            else:
                # Multiple emails - show with buyer/seller labels and QUA welcome
                email_list_items = []
                buyer_emails = []
                seller_emails = []
                
                for i, email in enumerate(emails):
                    user_type = self._get_user_type_for_email(email, filtered_users)
                    if user_type == "Buyer":
                        buyer_emails.append(email)
                        email_list_items.append(f"{i+1}. {email} ({user_type})")
                    elif user_type == "Seller":
                        seller_emails.append(email)
                        email_list_items.append(f"{i+1}. {email} ({user_type})")
                    else:
                        email_list_items.append(f"{i+1}. {email}")
                
                email_list = "\n".join(email_list_items)
                
                # Check if we have mixed user types for better messaging
                if buyer_emails and seller_emails:
                    email_text = f"Hi {username}! \nWould you like to buy or sell today?\n\n{email_list}\n\nReply with the number of your email address."
                else:
                    email_text = f"Hi {username}! \nPlease select your email address:\n\n{email_list}\n\nReply with the number of your email address."
            
            response = self.openai_service.generate_response(
                context={"username": username, "email_text": email_text},
                query_results=[],
                prompt_file="email_confirmation/email_confirmation_generation"
            )
            
            return response.strip()
            
        except Exception as e:
            logger.error(f"Error generating email confirmation response: {e}")
            # Fallback message
            if len(emails) == 1:
                return f"Hi {username}! \nCould you please confirm your email address to proceed: {emails[0]}?"
            else:
                email_list = "\n".join([f"{i+1}. {email}" for i, email in enumerate(emails)])
                return f"Hi {username}! \nPlease select your email address:\n\n{email_list}\n\nReply with the number of your email address."
    
    async def _parse_email_selection(self, message: str, email_options: List[str]) -> Optional[str]:
        """Parse email selection from user message."""
        try:
            message = message.strip()
            logger.info(f"Parsing email selection: '{message}' from options: {email_options}")
            
            # Try to parse as number first
            try:
                selection_num = int(message)
                if 1 <= selection_num <= len(email_options):
                    selected_email = email_options[selection_num - 1]
                    logger.info(f"Selected email by number {selection_num}: {selected_email}")
                    return selected_email
                else:
                    logger.warning(f"Invalid selection number {selection_num}, valid range: 1-{len(email_options)}")
                    return None
            except ValueError:
                pass
            
            # Try to match email directly
            message_lower = message.lower()
            for email in email_options:
                if email.lower() in message_lower:
                    logger.info(f"Selected email by text match: {email}")
                    return email
            
            logger.warning(f"Could not parse email selection: '{message}'")
            return None
            
        except Exception as e:
            logger.error(f"Email selection parsing error: {e}")
            return None
    
    async def _validate_email_confirmation_with_ai(self, message: str, email_options: List[str]) -> Optional[str]:
        """Validate email confirmation using OpenAI."""
        try:
            prompt = f"""
User message: "{message}"
Email options: {', '.join(email_options)}

Analyze if the user is confirming one of the email addresses.
Look for:
1. "yes" responses (if single email)
2. Number selections (1, 2, etc.)
3. Direct email mentions
4. Positive confirmations

Return only the selected email address or "none" if no clear selection.
"""
            
            response = self.openai_service.generate_response(
                context={"prompt": prompt},
                query_results=[]
            )
            
            response = response.strip().lower()
            
            # Check if response matches any email
            for email in email_options:
                if email.lower() in response:
                    return email
            
            # Check for "yes" response with single email
            if len(email_options) == 1 and ("yes" in response or "confirm" in response):
                return email_options[0]
            
            return None
            
        except Exception as e:
            logger.error(f"AI email validation error: {e}")
            return None
    
    async def _process_selected_email(self, user_phone: str, session: ConversationSession,
                                    selected_email: str, filtered_users: List[Dict]) -> Dict[str, Any]:
        """Process selected email and determine next step based on user type."""
        try:
            session.workflow_state["selected_email"] = selected_email
            
            # Find user details for selected email
            selected_user = None
            for user in filtered_users:
                # Check both 'email' and 'username' fields as API uses 'username' for email
                user_email = user.get("email") or user.get("username")
                if user_email == selected_email:
                    selected_user = user
                    break

            if not selected_user:
                logger.error(f"User not found for email {selected_email} in filtered_users: {[u.get('email') or u.get('username') for u in filtered_users]}")
                return {"status": "redirect_to_support", "reason": "user_not_found"}
            
            # Check both 'self_client' and 'selfClient' field names for compatibility
            is_self_client = selected_user.get("self_client") or selected_user.get("selfClient")
            user_type = "buyer" if is_self_client else "seller"
            logger.info(f"Processing email {selected_email} for user_type: {user_type} (selfClient={is_self_client})")
            
            if user_type == "buyer":
                # CRITICAL: Check verification status before allowing buyer access
                verification_check = await self.verification_check_service.check_and_enforce_verification(user_phone, selected_user)
                
                if not verification_check.get("access_granted"):
                    # User doesn't meet verification requirements
                    logger.info(f"User {user_phone} blocked due to verification requirements: {verification_check}")
                    redirect_info = verification_check.get("redirect_info", {})
                    
                    # Check if redirect to support is required
                    if verification_check.get("redirect_to_support"):
                        # Call exit service and redirect to support
                        from app.services.exit_service import ExitService
                        exit_service = ExitService(self.whatsapp_service, self, 
                                                 self.session_manager, None)
                        
                        # Send support message
                        support_message = redirect_info.get("message", "Please contact our support team for assistance.")
                        await self.whatsapp_service.send_message(user_phone, support_message)
                        
                        # Clear session and exit
                        await exit_service.handle_exit_intent(user_phone, session)
                        
                        return {
                            "status": "redirected_to_support",
                            "user_type": user_type,
                            "reason": redirect_info.get("reason"),
                            "exit_completed": True
                        }
                    else:
                        # Email verification required - set up OTP flow
                        if redirect_info.get("flow") == "email_verification":
                            WorkflowManager.set_workflow_type(session, WorkflowType.authentication, caller="authentication_service")
                            session.workflow_state["authentication_stage"] = "email_otp"
                            session.workflow_state["otp_email"] = selected_email
                            session.workflow_state["otp_retry_count"] = 0
                            session.workflow_state["selected_user"] = selected_user
                            session.workflow_state["filtered_users"] = filtered_users
                            
                            return {
                                "status": "otp_sent",
                                "user_type": "buyer",
                                "workflow_type": "authentication"
                            }
                        else:
                            return {
                                "status": "verification_required",
                                "user_type": "buyer",
                                "redirect_info": redirect_info
                            }
                
                # Buyer meets verification requirements - proceed with authentication
                session_stored = await self.store_user_session_with_email(user_phone, filtered_users, selected_email)

                if session_stored:
                    logger.info(f"User session stored successfully for verified buyer {user_phone}")
                else:
                    logger.error(f"Failed to store user session for verified buyer {user_phone}")

                # Get username from fullName or fallback to firstName or generic "there"
                username = selected_user.get("fullName") or selected_user.get("name") or selected_user.get("firstName") or "there"



                # Preserve original message from workflow state for processing after authentication
                original_message = session.workflow_state.get("original_message") if session.workflow_state else None

                # Clear authentication workflow state after successful authentication
                session.workflow_type = None
                session.workflow_state = {}
                logger.info(f"Cleared authentication workflow state for verified buyer {user_phone}")

                return {
                    "status": "authentication_completed",
                    "user_type": "buyer",
                    "approved": True,
                    "redirect_to_main_flow": True,
                    "original_message": original_message
                }
            elif user_type == "seller":
                # CRITICAL: Check verification status before allowing seller access
                verification_check = await self.verification_check_service.check_and_enforce_verification(user_phone, selected_user)
                
                if not verification_check.get("access_granted"):
                    # Check if redirect to support is required
                    if verification_check.get("redirect_to_support"):
                        # Call exit service and redirect to support
                        from app.services.exit_service import ExitService
                        exit_service = ExitService(self.whatsapp_service, self, 
                                                 self.session_manager, None)
                        
                        redirect_info = verification_check.get("redirect_info", {})
                        support_message = redirect_info.get("message", "Please contact our support team for assistance.")
                        await self.whatsapp_service.send_message(user_phone, support_message)
                        
                        # Clear session and exit
                        await exit_service.handle_exit_intent(user_phone, session)
                        
                        return {
                            "status": "redirected_to_support",
                            "user_type": "seller",
                            "reason": redirect_info.get("reason"),
                            "exit_completed": True
                        }
                    else:
                        # Seller needs email verification - redirect to OTP flow
                        logger.info(f"Seller {user_phone} needs email verification - redirecting to OTP")
                        WorkflowManager.set_workflow_type(session, WorkflowType.authentication, caller="authentication_service")
                        session.workflow_state["authentication_stage"] = "email_otp"
                        session.workflow_state["otp_email"] = selected_email
                        session.workflow_state["otp_retry_count"] = 0
                        session.workflow_state["selected_user"] = selected_user
                        session.workflow_state["filtered_users"] = filtered_users

                        # Send OTP and return status indicating OTP flow started
                        otp_result = await self.otp_service.send_otp(user_phone, selected_email, session)
                        return {
                            "status": "otp_sent",
                            "user_type": "seller",
                            "workflow_type": "authentication"
                        }
                else:
                    # Seller is already verified - complete authentication immediately
                    logger.info(f"Seller {user_phone} already verified - completing authentication")
                    session_stored = await self.store_user_session_with_email(user_phone, filtered_users, selected_email)

                    if session_stored:
                        logger.info(f"User session stored successfully for verified seller {user_phone}")
                    else:
                        logger.error(f"Failed to store user session for verified seller {user_phone}")

                    # Get username from fullName or fallback to firstName or generic "there"
                    username = selected_user.get("fullName") or selected_user.get("name") or selected_user.get("firstName") or "there"
                    message = f"Hi {username}!"
                    await self.whatsapp_service.send_message(user_phone, message)

                    # Clear authentication workflow state after successful authentication
                    session.workflow_type = None
                    session.workflow_state = {}
                    logger.info(f"Cleared authentication workflow state for verified seller {user_phone}")

                    return {
                        "status": "authentication_completed",
                        "user_type": "seller",
                        "redirect_to_main_flow": True
                    }
            else:
                return {"status": "redirect_to_support", "reason": "unknown_user_type"}
                
        except Exception as e:
            logger.error(f"Email processing error: {e}")
            return {"status": "error", "error": str(e)}
    
    def _determine_user_type(self, user_details: Dict) -> str:
        """Determine user type from user details."""
        try:
            if user_details.get("selfClient") is True:
                return "buyer"
            else:
                return "seller"
        except Exception as e:
            logger.error(f"User type determination error: {e}")
            return "unknown"
    
    async def store_user_session_with_email(self, user_phone: str, filtered_users: List[Dict], selected_email: str) -> bool:
        """Store user session with selected email using filtered user data."""
        try:
            user_details = self.create_user_details_from_email(filtered_users, selected_email)

            if not user_details:
                logger.error(f"Could not create user details for email {selected_email}")
                return False

            success = await self.store_user_session(user_phone, user_details)

            if success:
                logger.info(f"Successfully stored session for {user_phone} with email {selected_email}")
            else:
                logger.error(f"Failed to store session for {user_phone} with email {selected_email}")

            return success

        except Exception as e:
            logger.error(f"Session storage error: {e}")
            return False
    

    
    # Email OTP Sub-Service
    async def handle_email_otp_validation(self, user_phone: str, message: str,
                                        session: ConversationSession) -> Dict[str, Any]:
        """Handle email OTP validation process."""
        try:
            filtered_users = session.workflow_state.get("filtered_users", [])
            if not filtered_users:
                return {"status": "restart_authentication"}
            
            # Use OTP service for validation
            otp_result = await self.otp_service.handle_user_message(user_phone, message, session)
            
            # If OTP is valid, refresh user data and complete authentication
            if otp_result.get("status") == "otp_valid":
                selected_email = session.workflow_state.get("otp_email")
                selected_user = session.workflow_state.get("selected_user")
                
                # Clear user cache and force fresh API call to get updated verification status
                logger.info(f"OTP validated successfully, forcing fresh user data refresh for {user_phone}")
                
                # Clear cached data to force fresh API call
                from app.services.user_cache_service import get_user_cache_service
                user_cache_service = get_user_cache_service()
                await user_cache_service.clear_user_data(user_phone)
                
                # CRITICAL: For buyers, trigger domain check after OTP validation
                is_self_client = selected_user.get("selfClient") or selected_user.get("self_client", True)
                user_type = "buyer" if is_self_client else "seller"
                
                if user_type == "buyer":
                    user_id = selected_user.get("id")
                    if user_id:
                        logger.info(f"Triggering domain check for buyer {user_id} after OTP validation")
                        domain_result = await self._check_domain_approval(user_id)
                        logger.info(f"Domain check result: {domain_result}")
                
                # Force fresh API call to get updated user details
                auth_response = await self.user_authenticate(user_phone, "refresh_after_otp", session)
                
                if auth_response.get("success") and auth_response.get("response"):
                    fresh_users = auth_response["response"]
                    
                    # Find the selected user from fresh data
                    fresh_selected_user = None
                    for user in fresh_users:
                        user_email = user.get("email") or user.get("username")
                        if user_email == selected_email:
                            fresh_selected_user = user
                            break
                    
                    if fresh_selected_user:
                        # CRITICAL: Check verification status after OTP validation and domain check
                        verification_check = await self.verification_check_service.check_and_enforce_verification(user_phone, fresh_selected_user)
                        
                        if not verification_check.get("access_granted"):
                            # User still doesn't meet verification requirements (e.g., approved=False for buyers)
                            logger.info(f"User {user_phone} blocked after OTP validation due to verification requirements: {verification_check}")
                            redirect_info = verification_check.get("redirect_info", {})
                            
                            if verification_check.get("redirect_to_support"):
                                # Redirect to support and exit
                                from app.services.exit_service import ExitService
                                exit_service = ExitService(self.whatsapp_service, self, self.session_manager, None)
                                
                                support_message = redirect_info.get("message", "Please contact our support team for assistance.")
                                await self.whatsapp_service.send_message(user_phone, support_message)
                                
                                await exit_service.handle_exit_intent(user_phone, session)
                                
                                return {
                                    "status": "redirect_to_support",
                                    "reason": redirect_info.get("reason"),
                                    "exit_completed": True
                                }
                            else:
                                # Other verification requirements not met
                                return {
                                    "status": "verification_required",
                                    "redirect_info": redirect_info
                                }
                        
                        # Verification passed - proceed with session storage
                        session_stored = await self.store_user_session_with_email(user_phone, fresh_users, selected_email)
                        
                        # CRITICAL: Update auth token with fresh user data that has updated verification status
                        normalized_phone = user_phone.lstrip('+')
                        await self.auth_redis_service.store(normalized_phone, fresh_selected_user, expiry_seconds=3600)
                        logger.info(f"Updated auth token with fresh verification status for {user_phone}")
                        
                        if session_stored:
                            logger.info(f"User session stored successfully after OTP validation for {user_phone}")
                            
                            # Clear authentication workflow state
                            session.workflow_type = None
                            session.workflow_state = {}
                            logger.info(f"Cleared authentication workflow state after OTP validation for {user_phone}")
                            
                            return {
                                "status": "authentication_completed",
                                "user_type": user_type,
                                "redirect_to_main_flow": True
                            }
                        else:
                            logger.error(f"Failed to store user session after OTP validation for {user_phone}")
                
                # If refresh or session storage fails, clear state and redirect to main flow anyway
                # The user has successfully validated their email, so we should let them proceed
                logger.warning(f"OTP validated but session storage failed for {user_phone} - allowing access anyway")
                session.workflow_type = None
                session.workflow_state = {}
                
                return {
                    "status": "authentication_completed",
                    "user_type": "unknown",
                    "redirect_to_main_flow": True,
                    "note": "OTP validated successfully"
                }
            
            return otp_result
            
        except Exception as e:
            logger.error(f"Email OTP validation error: {e}")
            # Clear authentication state on error
            session.workflow_type = None
            session.workflow_state = {}
            return {"status": "error", "error": str(e)}
    

    
    # Domain Matching Sub-Service
    async def handle_domain_matching(self, user_phone: str, message: str,
                                   session: ConversationSession) -> Dict[str, Any]:
        """Handle domain matching process."""
        try:
            user_details = session.workflow_state.get("selected_user_details")
            selected_email = session.workflow_state.get("selected_email")
            
            if not user_details or not selected_email:
                return {"status": "restart_authentication"}
            
            user_id = user_details.get("id")
            if not user_id:
                logger.error("No user ID found for domain approval")
                return {"status": "error", "error": "User ID not found"}
            
            domain_result = await self._check_domain_approval(user_id)
            
            if domain_result.get("approved"):
                return await self._complete_buyer_authentication(user_phone, user_details, selected_email)
            else:
                return await self._handle_domain_mismatch(user_phone, user_details)
                
        except Exception as e:
            logger.error(f"Domain matching error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _check_domain_approval(self, user_id: str) -> Dict[str, Any]:
        """Check user domain approval using shared service."""
        try:
            return await self.auth_reg_service.user_domain_check(user_id)
        except Exception as e:
            logger.error(f"Domain approval check error: {e}")
            await self.support_notification_service.notify_api_service_failure(
                f"Domain approval check error for user {user_id}: {str(e)}"
            )
            return {"approved": False, "error": str(e)}
    

    
    async def _complete_buyer_authentication(self, user_phone: str, user_details: Dict,
                                           selected_email: str) -> Dict[str, Any]:
        """Complete buyer authentication with approval."""
        try:
            session_stored = await self.store_user_session_with_email(user_phone, [user_details], selected_email)
            
            if session_stored:
                logger.info(f"User session stored successfully for approved buyer {user_phone}")
            else:
                logger.error(f"Failed to store user session for approved buyer {user_phone}")
            
            message = (
                "Registration successful—thank you! How can I help you today? "
                "Want to raise an RFQ or any other support?"
            )
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "authentication_completed",
                "user_type": "buyer",
                "approved": True,
                "redirect_to_main_flow": True
            }
            
        except Exception as e:
            logger.error(f"Buyer authentication completion error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_domain_mismatch(self, user_phone: str, user_details: Dict) -> Dict[str, Any]:
        """Handle domain mismatch scenario."""
        try:
            message = (
                "Registration successful—thank you! Our team will get in touch with you "
                "shortly to complete your onboarding so that you can raise RFQs. "
                "In the meantime please let us know if you want us to support you with anything else?"
            )
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "authentication_completed",
                "user_type": "buyer",
                "approved": False,
                "redirect_to_main_flow": False,
                "pending_approval": True
            }
            
        except Exception as e:
            logger.error(f"Domain mismatch handling error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _request_email_confirmation(self, user_phone: str, session: ConversationSession,
                                        selected_email: str, filtered_users: List[Dict]) -> Dict[str, Any]:
        """Request user to confirm selected email."""
        try:
            # Store selected email and update stage
            session.workflow_state["selected_email"] = selected_email
            session.workflow_state["confirmation_stage"] = "confirmation"
            
            # Generate confirmation message
            message = f"We found the following email address associated with your phone number. Could you please help us verify it?\n\n{selected_email}\n\nPlease reply 'Yes' to confirm or 'No' if this is incorrect."
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_confirmation_requested",
                "stage": "email_confirmation",
                "selected_email": selected_email
            }
            
        except Exception as e:
            logger.error(f"Email confirmation request error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _validate_confirmation_response(self, message: str) -> bool:
        """Validate user confirmation response."""
        try:
            message = message.strip().lower()
            
            # Simple validation
            positive_responses = ["yes", "y", "confirm", "correct", "ok", "1"]
            negative_responses = ["no", "n", "wrong", "incorrect", "2"]
            
            if any(word in message for word in positive_responses):
                return True
            elif any(word in message for word in negative_responses):
                return False
            
            # Use AI for complex responses
            # return await self._validate_confirmation_with_ai(message)
            return False
            
        except Exception as e:
            logger.error(f"Confirmation validation error: {e}")
            return False
    
    async def _validate_confirmation_with_ai(self, message: str) -> bool:
        """Validate confirmation using AI."""
        try:
            prompt = f"""
User message: "{message}"

Is this a positive confirmation (yes) or negative (no)?
Look for agreement, confirmation, or disagreement.

Respond only with: "yes" or "no"
"""
            
            response = self.openai_service.generate_response(
                context={"prompt": prompt},
                query_results=[]
            )
            
            return "yes" in response.strip().lower()
            
        except Exception as e:
            logger.error(f"AI confirmation validation error: {e}")
            return False
    
    async def _is_email_rejection(self, message: str) -> bool:
        """Check if user is rejecting all email options."""
        try:
            message_lower = message.lower().strip()
            rejection_phrases = [
                "not my email", "not mine", "wrong email", "incorrect", 
                "none of these", "not me", "different email", "other email"
            ]
            return any(phrase in message_lower for phrase in rejection_phrases)
        except Exception as e:
            logger.error(f"Email rejection check error: {e}")
            return False
    
    def _get_user_type_for_email(self, email: str, filtered_users: List[Dict]) -> Optional[str]:
        """Get user type (Buyer/Seller) for a specific email from filtered users data."""
        try:
            if not filtered_users:
                return None
            
            for user in filtered_users:
                user_email = user.get("email") or user.get("username")
                if user_email == email:
                    is_self_client = user.get("self_client", False)
                    return "Buyer" if is_self_client else "Seller"
            
            return None
        except Exception as e:
            logger.error(f"Error getting user type for email {email}: {e}")
            return None
    
    # ===== TEXT-BASED EMAIL SELECTION =====
    
    async def _request_email_selection_with_text(self, user_phone: str, session: ConversationSession,
                                               emails: List[str], filtered_users: List[Dict]) -> Dict[str, Any]:
        """Request email selection using ProfileSelectionService."""
        try:
            # Use ProfileSelectionService to handle profile selection
            from app.services.profile_selection_service import ProfileSelectionService
            profile_service = ProfileSelectionService(
                whatsapp_service=self.whatsapp_service,
                authentication_service=self,
                openai_service=self.openai_service
            )
            
            # Convert filtered_users to profiles format expected by ProfileSelectionService
            profiles = []
            for user_data in filtered_users:
                try:
                    from app.schemas.user import User
                    user = User.from_api_response(user_data)
                    profile = {
                        "email": user.email,
                        "role": user.role.value,
                        "name": user.name,
                        "company": user.company_name,
                        "user_data": user_data
                    }
                    profiles.append(profile)
                except Exception as e:
                    logger.warning(f"Failed to convert user data to profile: {e}")
                    continue
            
            # Get current intent from session
            current_intent_result = session.workflow_state.get("current_intent_result", {})
            
            # Handle profile selection based on intent
            result = await profile_service.handle_profile_selection(
                user_phone, "", session, current_intent_result
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Profile selection error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _request_email_selection_text_fallback(self, user_phone: str, session: ConversationSession,
                                                   emails: List[str], filtered_users: List[Dict]) -> Dict[str, Any]:
        """Fallback text-based email selection."""
        try:
            username = self._get_username_from_users(filtered_users)
            
            message = f"Hi {username}, please select your email address:\\n\\n"
            for i, email in enumerate(emails, 1):
                user_type = self._get_user_type_for_email(email, filtered_users)
                type_label = f" - {user_type}" if user_type else ""
                message += f"{i}. {email}{type_label}\\n"
            message += "\\nReply with the number of your email."
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_selection_requested",
                "stage": "email_confirmation",
                "email_options": emails,
                "using_buttons": False
            }
            
        except Exception as e:
            logger.error(f"Text email selection fallback error: {e}")
            return {"status": "error", "error": str(e)}
    
    def _get_username_from_users(self, filtered_users: List[Dict]) -> str:
        """Extract username from filtered users data."""
        try:
            if filtered_users and len(filtered_users) > 0:
                name = filtered_users[0].get("fullName")
                # Handle None or empty string cases
                if name and name.strip():
                    return name.strip()
            return "there"
        except Exception:
            return "there"
    
    # ===== AI-FIRST AUTHENTICATION METHODS =====
    
    async def _ai_parse_email_selection(self, message: str, email_options: List[str]) -> Optional[str]:
        """Use AI to parse email selection from user message."""
        try:
            # Use OpenAI to understand the selection
            response = self.openai_service.generate_response(
                context={"message": message, "email_options": email_options},
                query_results=[],
                prompt_file="email_confirmation/email_selection_parsing"
            )
            
            response = response.strip().lower()
            
            # Check if response matches any email
            for email in email_options:
                if email.lower() in response:
                    logger.info(f"AI selected email: {email} from message: '{message}'")
                    return email
            
            return None
            
        except Exception as e:
            logger.error(f"AI email selection parsing error: {e}")
            return None
    
    async def _ai_validate_confirmation_response(self, message: str) -> Optional[bool]:
        """Use AI to validate confirmation response (yes/no)."""
        try:
            response = self.openai_service.generate_response(
                context={"message": message},
                query_results=[],
                prompt_file="email_confirmation/confirmation_validation"
            )
            
            response = response.strip().lower()
            
            if "yes" in response:
                return True
            elif "no" in response:
                return False
            else:
                return None  # Unclear - will fallback to pattern matching
                
        except Exception as e:
            logger.error(f"AI confirmation validation error: {e}")
            return None
    
    async def _ai_detect_email_rejection(self, message: str) -> bool:
        """Use AI to detect if user is rejecting all email options."""
        try:
            response = self.openai_service.generate_response(
                context={"message": message},
                query_results=[],
                prompt_file="email_confirmation/rejection_detection"
            )
            
            return "yes" in response.strip().lower()
            
        except Exception as e:
            logger.error(f"AI email rejection detection error: {e}")
            # Fallback to pattern matching
            message_lower = message.lower().strip()
            rejection_phrases = [
                "not my email", "not mine", "wrong email", "incorrect", 
                "none of these", "not me", "different email", "other email"
            ]
            return any(phrase in message_lower for phrase in rejection_phrases)
