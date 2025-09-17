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
from app.models import ConversationSession, UserType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.schemas.user import User
from app.procucev_apis.auth_apis import AuthAPIService
from app.procucev_apis.register_apis import RegisterAPIService
from app.services.support_notification_service import SupportNotificationService

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
        self.session_manager = session_manager  # Will be injected from ChatService
    
    async def validate_token(self, user_phone: str) -> Optional[User]:
        """Validate user token from Redis auth storage and refresh on activity."""
        try:
            logger.info("user token validation called")
            user_data = await self.auth_redis_service.retrieve(user_phone)
            if user_data:
                # Token automatically refreshed in retrieve method
                logger.info(f"Token validated and refreshed for user {user_phone}")
                return user_data
            
            # Token expired or not found - send welcome message
            logger.info(f"Token expired for user {user_phone}, sending welcome message")
            # await self.whatsapp_service.send_message(user_phone, "Welcome to QUA!")
            return False
        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return False
    
    async def store_user_session(self, user_phone: str, user_details: User) -> bool:
        """Store user session data in Redis."""
        try:
            session_data = user_details.dict()
            session_data["authenticated_at"] = datetime.now().isoformat()
            # Token expires after 1 hour of inactivity
            success = await self.auth_redis_service.store(user_phone, session_data, expiry_seconds=3600)  # 1 hour
            
            if success:
                logger.info(f"Session stored successfully for user {user_phone} (ID: {user_details.id})")
            else:
                logger.error(f"Failed to store session in Redis for user {user_phone}")
            
            return success
        except Exception as e:
            logger.error(f"Session storage error for {user_phone}: {e}")
            return False
    
    async def clear_user_token(self, user_phone: str) -> bool:
        """Clear user token/session from Redis."""
        try:
            success = await self.auth_redis_service.delete_auth(user_phone)
            if success:
                logger.info(f"User token cleared successfully for {user_phone}")
            else:
                logger.warning(f"Failed to clear token for {user_phone} or token not found")
            return success
        except Exception as e:
            logger.error(f"Token clearing error for {user_phone}: {e}")
            return False
    
    async def user_authenticate(self, user_phone: str, message: str, 
                              session: ConversationSession, intent: str = None) -> Dict[str, Any]:
        """Handles token validation failure and routes to user authentication flow."""
        try:
            logger.info(f"Authenticating user {user_phone} with intent: {intent}")
            auth_response = await self.auth_api_service.authenticate_user(user_phone)
            logger.info(f"Auth API service response: {auth_response}")

            if auth_response.get("success"):
                raw_response = auth_response.get("data", [])                
                if raw_response:
                    return {
                        "success": True, 
                        "response": raw_response, 
                        "is_registered": auth_response.get("is_registered", True),
                        "detected_intent": intent
                    }
                return {"success": False, "message": "User details not found"}

            return auth_response

        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return {"success": False, "message": str(e)}
    
    def filter_users_by_intent(self, raw_users: List[Dict], intent: str) -> Dict[str, Any]:
        """Filter users based on intent and return structured data using schemas."""
        try:
            from app.schemas.user import APIUserSchema
            
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
                if user.get("email") == selected_email:
                    selected_user = user
                    break
            
            if not selected_user:
                # If no exact match, use first user with same email pattern
                for user in filtered_users:
                    if selected_email in str(user.get("email", "")):
                        selected_user = user
                        break
            
            if not selected_user:
                logger.warning(f"No user found for email {selected_email}")
                return None
            
            return User(
                id=selected_user.get("id", ""),
                name=selected_user.get("name", ""),
                email=selected_email,
                phone_number=selected_user.get("phone_number", ""),
                self_client=selected_user.get("self_client", False),
                role=selected_user.get("role", "unknown"),
                is_registered=True,
                company_name=selected_user.get("company_name", ""),
                unique_id=selected_user.get("unique_id", ""),
                org_id=selected_user.get("org_id", "")
            )
            
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
            
            return User(
                id=user_data.get("id", ""),
                email=user_data.get("username", ""),
                name=user_data.get("fullName", ""),
                phone_number=user_data.get("phone", ""),
                self_client=user_data.get("selfClient", False),
                role="buyer" if user_data.get("selfClient") else "seller",
                is_registered=True,
                company_name=user_data.get("companyName", "")
            )
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
                                      session: ConversationSession) -> Dict[str, Any]:
        """Handle email confirmation response with AI-first approach."""
        try:
            email_options = session.workflow_state.get("email_options", [])
            filtered_users = session.workflow_state.get("filtered_users", [])
            confirmation_stage = session.workflow_state.get("confirmation_stage", "selection")
            
            if not email_options or not filtered_users:
                return {"status": "restart_authentication"}
            
            # Check if this is a button response
            # Removed button handling - using text-based selection
            
            if confirmation_stage in ["selection", "intent_filtered_selection"]:
                # AI-first email rejection detection
                is_rejection = await self._ai_detect_email_rejection(message)
                if is_rejection:
                    await self.whatsapp_service.send_message(user_phone, "I understand these emails don't match yours. Let me help you register with your correct information.")
                    return {"status": "redirect_to_registration"}
                
                # AI-first email selection parsing
                selected_email = await self._ai_parse_email_selection(message, email_options)
                
                # if not selected_email:
                #     # Fallback to pattern matching
                #     selected_email = await self._parse_email_selection(message, email_options)
                
                if selected_email:
                    return await self._process_selected_email(user_phone, session, selected_email, filtered_users)
                else:
                    # Send retry message
                    return await self._request_email_selection_with_text(user_phone, session, email_options, filtered_users)
            
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
                    email_text = f"Hi {username}! Welcome to QUA,\nIs this your {user_type.lower()} email: {email}?"
                else:
                    email_text = f"Hi {username}! Welcome to QUA,\nIs this your email: {email}?"
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
                    email_text = f"Hi {username}! Welcome to QUA,\nWould you like to buy or sell today?\n\n{email_list}\n\nReply with the number of your email address."
                else:
                    email_text = f"Hi {username}! Welcome to QUA,\nPlease select your email address:\n\n{email_list}\n\nReply with the number of your email address."
            
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
                return f"Hi {username}! Welcome to QUA,\nCould you please confirm your email address to proceed: {emails[0]}?"
            else:
                email_list = "\n".join([f"{i+1}. {email}" for i, email in enumerate(emails)])
                return f"Hi {username}! Welcome to QUA,\nPlease select your email address:\n\n{email_list}\n\nReply with the number of your email address."
    
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
                if user.get("email") == selected_email:
                    selected_user = user
                    break
            
            if not selected_user:
                return {"status": "redirect_to_support", "reason": "user_not_found"}
            
            user_type = "buyer" if selected_user.get("self_client") else "seller"
            logger.info(f"Processing email {selected_email} for user_type: {user_type}")
            
            if user_type == "buyer":
                # Buyers: Store token session and redirect to main flow
                session_stored = await self.store_user_session_with_email(user_phone, filtered_users, selected_email)
                
                if session_stored:
                    logger.info(f"User session stored successfully for buyer {user_phone}")
                else:
                    logger.error(f"Failed to store user session for buyer {user_phone}")
                
                username = selected_user.get("name", "User")
                message = f"Hi {username}!"
                await self.whatsapp_service.send_message(user_phone, message)
                
                # Preserve original message from workflow state for processing after authentication
                original_message = session.workflow_state.get("original_message") if session.workflow_state else None
                
                return {
                    "status": "authentication_completed",
                    "user_type": "buyer",
                    "redirect_to_main_flow": True,
                    "original_message": original_message
                }
            elif user_type == "seller":
                # Sellers: Redirect to email OTP validation
                session.workflow_state["authentication_stage"] = "email_otp"
                session.workflow_state["otp_email"] = selected_email
                session.workflow_state["otp_retry_count"] = 0
                session.workflow_state["selected_user"] = selected_user
                
                # Send OTP immediately
                return await self._send_otp(user_phone, session, selected_email)
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
            otp_email = session.workflow_state.get("otp_email")
            filtered_users = session.workflow_state.get("filtered_users", [])
            retry_count = session.workflow_state.get("otp_retry_count", 0)
            
            if not otp_email or not filtered_users:
                return {"status": "restart_authentication"}
            
            # Check for resend request
            if message.strip().upper() == "RESEND" and retry_count < 3:
                return await self._send_otp(user_phone, session, otp_email)
            
            # Extract and validate OTP
            otp = self._extract_otp_from_message(message)
            
            if not otp:
                return await self._handle_invalid_otp_format(user_phone, session)
            
            return await self._validate_otp(user_phone, session, otp, otp_email, filtered_users)
            
        except Exception as e:
            logger.error(f"Email OTP validation error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _send_otp(self, user_phone: str, session: ConversationSession, email: str) -> Dict[str, Any]:
        """Send OTP to user's email."""
        try:
            logger.info(f"Sending OTP to email: {email} for phone: {user_phone}")
            otp_response = await self.register_api_service.send_otp(email, user_phone)
            
            if otp_response.get("statusCode") in ["1001", "200"] or otp_response.get("status") == "Success":
                # Don't increment retry count for successful OTP send - only for failed validations
                message = f"OTP sent to your email: {email}\n\nPlease enter the OTP you received, or reply 'RESEND' to get a new OTP:"
                await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "otp_sent",
                    "stage": "email_otp",
                    "email": email
                }
            else:
                error_msg = otp_response.get("message", "Failed to send OTP")
                logger.error(f"OTP send failed: {error_msg}")
                
                # Send email notification to support team
                await self.support_notification_service.notify_otp_validation_failed(
                    "User", email, user_phone
                )
                
                if "email not exists" in error_msg.lower():
                    await self.whatsapp_service.send_message(user_phone, "Email address not found in our system. Please contact support.")
                    return {"status": "redirect_to_support", "reason": "email_not_exists"}
                else:
                    await self.whatsapp_service.send_message(user_phone, "Failed to send OTP. Please contact support.")
                    return {"status": "redirect_to_support", "reason": "otp_send_failed"}
                    
        except Exception as e:
            logger.error(f"OTP send error: {e}")
            
            # Send email notification to support team
            await self.support_notification_service.notify_api_service_failure(
                f"OTP send error for {user_phone}: {str(e)}"
            )
            
            await self.whatsapp_service.send_message(user_phone, "Error sending OTP. Please contact support.")
            return {"status": "redirect_to_support", "reason": "otp_send_error"}
    
    def _extract_otp_from_message(self, message: str) -> str:
        """Extract OTP from user message."""
        try:
            import re
            digits = re.findall(r'\d+', message.strip())
            
            if digits:
                otp = digits[0]
                if AuthenticationHelpers.validate_otp_format(otp):
                    return otp
            
            return ""
            
        except Exception as e:
            logger.error(f"OTP extraction error: {e}")
            return ""
    
    async def _validate_otp(self, user_phone: str, session: ConversationSession,
                          otp: str, email: str, filtered_users: List[Dict]) -> Dict[str, Any]:
        """Validate OTP with API."""
        try:
            logger.info(f"Validating OTP for email: {email}, phone: {user_phone}")
            validation_response = await self.register_api_service.validate_otp(email, otp, user_phone)
            
            if validation_response.get("statusCode") == "1001" or validation_response.get("status") == "Success":
                # OTP valid - store session and complete authentication
                session_stored = await self.store_user_session_with_email(user_phone, filtered_users, email)
                
                if session_stored:
                    logger.info(f"User session stored successfully for seller {user_phone} after OTP validation")
                else:
                    logger.error(f"Failed to store user session for seller {user_phone} after OTP validation")
                
                message = "Email verified successfully! You can now proceed with your requests."
                await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "authentication_completed",
                    "user_type": "seller",
                    "redirect_to_main_flow": True
                }
            else:
                # OTP invalid - increment retry count and handle retry logic
                session.workflow_state["otp_retry_count"] = session.workflow_state.get("otp_retry_count", 0) + 1
                
                # Send email notification to support team for OTP validation failure
                await self.support_notification_service.notify_otp_validation_failed(
                    "User", email, user_phone
                )
                
                return await self._handle_invalid_otp(user_phone, session, email)
                
        except Exception as e:
            logger.error(f"OTP validation error: {e}")
            
            # Send email notification to support team
            await self.support_notification_service.notify_api_service_failure(
                f"OTP validation error for {user_phone}: {str(e)}"
            )
            
            await self.whatsapp_service.send_message(user_phone, "Error validating OTP. Please contact support.")
            return {"status": "redirect_to_support", "reason": "otp_validation_error"}
    
    async def _handle_invalid_otp_format(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle invalid OTP format."""
        try:
            # Increment retry count for invalid format
            session.workflow_state["otp_retry_count"] = session.workflow_state.get("otp_retry_count", 0) + 1
            retry_count = session.workflow_state.get("otp_retry_count", 0)
            
            if retry_count >= 3:
                await self.whatsapp_service.send_message(user_phone, "Maximum OTP attempts exceeded. Please contact support.")
                return {"status": "redirect_to_support", "reason": "max_otp_retries_exceeded"}
            
            remaining_attempts = 3 - retry_count
            message = f"Please enter a valid OTP . You have {remaining_attempts} attempts remaining, or reply 'RESEND' to get a new OTP:"
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "otp_format_invalid",
                "stage": "email_otp",
                "retry_count": retry_count
            }
            
        except Exception as e:
            logger.error(f"Invalid OTP format handling error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _handle_invalid_otp(self, user_phone: str, session: ConversationSession, email: str) -> Dict[str, Any]:
        """Handle invalid OTP with retry mechanism."""
        try:
            retry_count = session.workflow_state.get("otp_retry_count", 0)
            
            if retry_count >= 3:
                # Send email notification to support team for max retries exceeded
                await self.support_notification_service.notify_otp_validation_failed(
                    "User", email, user_phone
                )
                
                await self.whatsapp_service.send_message(user_phone, "Maximum OTP attempts exceeded. Please contact support.")
                return {"status": "redirect_to_support", "reason": "max_otp_retries_exceeded"}
            
            remaining_attempts = 3 - retry_count
            message = (
                f"Invalid OTP. You have {remaining_attempts} attempts remaining.\n\n"
                "Please enter the correct OTP or reply 'RESEND' to get a new OTP:"
            )
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "otp_invalid",
                "stage": "email_otp",
                "retry_count": retry_count,
                "remaining_attempts": remaining_attempts
            }
            
        except Exception as e:
            logger.error(f"Invalid OTP handling error: {e}")
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
            
            domain_result = await self._check_domain_approval(selected_email, user_details)
            
            if domain_result.get("approved"):
                await self._update_approval_status(user_details.get("id"), True)
                return await self._complete_buyer_authentication(user_phone, user_details, selected_email)
            else:
                await self._update_approval_status(user_details.get("id"), False)
                return await self._handle_domain_mismatch(user_phone, user_details)
                
        except Exception as e:
            logger.error(f"Domain matching error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _check_domain_approval(self, email: str, user_details: Dict) -> Dict[str, Any]:
        """Check if email domain matches company for approval."""
        try:
            domain = email.split('@')[1] if '@' in email else ""
            company_name = user_details.get("companyName", "").lower()
            
            domain_parts = domain.lower().split('.')
            company_parts = company_name.replace(" ", "").replace("-", "").replace("_", "")
            
            approved = any(part in company_parts for part in domain_parts if len(part) > 2)
            
            return {
                "approved": approved,
                "domain": domain,
                "company_name": company_name,
                "reason": "Domain match" if approved else "Domain mismatch"
            }
            
        except Exception as e:
            logger.error(f"Domain approval check error: {e}")
            
            # Send email notification to support team for domain matching failure
            await self.support_notification_service.notify_api_service_failure(
                f"Domain approval check error for {email}: {str(e)}"
            )
            
            return {"approved": False, "error": str(e)}
    
    async def _update_approval_status(self, user_id: str, approved: bool) -> Dict[str, Any]:
        """Update user approval status via API."""
        try:
            if approved:
                approval_response = await self.register_api_service.user_approval(user_id)
                return approval_response
            else:
                return {"success": True, "approved": False}
                
        except Exception as e:
            logger.error(f"Approval status update error: {e}")
            
            # Send email notification to support team for approval status update failure
            await self.support_notification_service.notify_api_service_failure(
                f"Approval status update error for user {user_id}: {str(e)}"
            )
            
            return {"success": False, "error": str(e)}
    
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
            message = f"Welcome to QUA, We found the following email address associated with your phone number. Could you please help us verify it?\n\n{selected_email}\n\nPlease reply 'Yes' to confirm or 'No' if this is incorrect."
            
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
        """Request email selection using text-based selection."""
        try:
            username = self._get_username_from_users(filtered_users)
            
            # Check if we have mixed user types for better messaging
            buyer_emails = [email for email in emails if self._get_user_type_for_email(email, filtered_users) == "Buyer"]
            seller_emails = [email for email in emails if self._get_user_type_for_email(email, filtered_users) == "Seller"]
            
            if buyer_emails and seller_emails:
                message = f"Hi there!, Welcome to QUA,\nAre you looking to buy or sell today?\n\nPlease select your email address:\n\n"
                for i, email in enumerate(emails, 1):
                    user_type = self._get_user_type_for_email(email, filtered_users)
                    type_label = f" - {user_type}" if user_type else ""
                    message += f"{i}. {email}{type_label}\n"
                message += "\nReply with the number of your email address."
            else:
                message = f"Hi there!, Welcome to QUA,\nPlease select your email address:\n\n"
                for i, email in enumerate(emails, 1):
                    user_type = self._get_user_type_for_email(email, filtered_users)
                    type_label = f" - {user_type}" if user_type else ""
                    message += f"{i}. {email}{type_label}\n"
                message += "\nReply with the number of your email address."
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_selection_requested",
                "stage": "email_confirmation",
                "email_options": emails,
                "using_buttons": False
            }
            
        except Exception as e:
            logger.error(f"Text email selection error: {e}")
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
