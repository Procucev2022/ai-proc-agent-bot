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
from app.models import User, ConversationSession, UserType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.schemas.user import UserDetailsSchema
from app.procucev_apis.auth_apis import AuthAPIService
from app.procucev_apis.register_apis import RegisterAPIService

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
        self.session_manager = session_manager  # Will be injected from ChatService
    
    async def validate_token(self, user_phone: str) -> Optional[UserDetailsSchema]:
        """Validate user token from Redis auth storage."""
        try:
            logger.info("user token validation called")
            user_data = await self.auth_redis_service.retrieve(user_phone)
            if user_data:
                return user_data
            return False
        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return False
    
    async def store_user_session(self, user_phone: str, user_details: UserDetailsSchema) -> bool:
        """Store user session data in Redis."""
        try:
            session_data = user_details.dict()
            session_data["authenticated_at"] = datetime.now().isoformat()
            
            success = await self.auth_redis_service.store(user_phone, session_data, expiry_seconds=86400)  # 24 hours
            
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
                              session: ConversationSession) -> Dict[str, Any]:
        """Handles token validation failure and routes to user authentication flow."""
        try:
            logger.info(f"Authenticating user {user_phone}")
            auth_response = await self.auth_api_service.authenticate_user(user_phone)

            if auth_response.get("success"):
                raw_response = auth_response.get("users", [])                
                if raw_response:
                    return {"success": True, "response": raw_response}
                return {"success": False, "message": "User details not found"}

            return auth_response

        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return {"success": False, "message": str(e)}
    
    def filter_users_by_intent(self, raw_users: List[Dict], intent: str) -> Dict[str, Any]:
        """Filter users based on intent and return structured data using schemas."""
        try:
            from app.schemas.user import APIUserSchema, UserDetailsSchema
            
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
            
            # Convert to UserDetailsSchema for consistent response
            user_details_list = []
            for user in filtered_users:
                user_dict = user.dict()
                user_detail = UserDetailsSchema.from_api_response(user_dict)
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
    
    def create_user_details_from_email(self, filtered_users: List[Dict], selected_email: str) -> Optional[UserDetailsSchema]:
        """Create UserDetailsSchema from selected email and filtered users."""
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
            
            return UserDetailsSchema(
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

    def _extract_user_details(self, api_response: list) -> Optional[UserDetailsSchema]:
        """Parse external API response and map into UserDetailsSchema."""
        try:
            if not api_response:
                return None
            
            user_data = AuthenticationHelpers.extract_user_details(api_response)
            if not user_data:
                return None
            
            return UserDetailsSchema(
                id=user_data.get("id", ""),
                username=user_data.get("username", ""),
                name=user_data.get("fullName", ""),
                phone_number=user_data.get("phone", ""),
                self_client=user_data.get("selfClient", False),
                role="buyer" if user_data.get("selfClient") else "seller",
                is_registered=True,
                company_name=user_data.get("companyName", ""),
                approved=user_data.get("approved", False)
            )
        except Exception as e:
            logger.error(f"Error extracting user details: {e}")
            return None
    
    # 3. Next flow email confirmation
    async def initiate_email_confirmation(self, user_phone: str, session: ConversationSession,
                                        filtered_users: List, available_emails: List) -> Dict[str, Any]:
        """Initiate email confirmation process."""
        try:
            if not available_emails:
                return {"status": "redirect_to_registration"}
            
            session.workflow_state["email_options"] = available_emails
            session.workflow_state["filtered_users"] = filtered_users
            
            # Single email - request confirmation
            if len(available_emails) == 1:
                selected_email = available_emails[0]
                logger.info(f"Requesting confirmation for single email: {selected_email}")
                session.workflow_state["confirmation_stage"] = "confirmation"
                return await self._request_email_confirmation(user_phone, session, selected_email, filtered_users)
            
            # Multiple emails - request selection
            else:
                logger.info(f"Requesting email selection from {len(available_emails)} options")
                session.workflow_state["confirmation_stage"] = "selection"
                return await self._request_email_selection(user_phone, session, available_emails)
                
        except Exception as e:
            logger.error(f"Email confirmation initiation error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def handle_email_confirmation(self, user_phone: str, message: str,
                                      session: ConversationSession) -> Dict[str, Any]:
        """Handle email confirmation response."""
        try:
            email_options = session.workflow_state.get("email_options", [])
            filtered_users = session.workflow_state.get("filtered_users", [])
            confirmation_stage = session.workflow_state.get("confirmation_stage", "selection")
            
            if not email_options or not filtered_users:
                return {"status": "restart_authentication"}
            
            if confirmation_stage == "selection":
                # Check if user is rejecting all emails
                if await self._is_email_rejection(message):
                    await self.whatsapp_service.send_message(user_phone, "I understand these emails don't match yours. Let me help you register with your correct information.")
                    return {"status": "redirect_to_registration"}
                
                # User is selecting email
                selected_email = await self._parse_email_selection(message, email_options)
                
                if not selected_email:
                    # Send single combined message instead of separate error + list
                    username = filtered_users[0].get("name", "there") if filtered_users else "there"
                    combined_message = f"Hi {username}, Since we have found multiple emails associated with this phone number I request you choose one to start with chat.\n\n"
                    for i, email in enumerate(email_options, 1):
                        combined_message += f"{i}. {email}\n"
                    combined_message += "\nReply with the number of your email."
                    await self.whatsapp_service.send_message(user_phone, combined_message)
                    return {"status": "email_selection_requested", "stage": "email_confirmation"}
                
                # Process selected email directly
                return await self._process_selected_email(user_phone, session, selected_email, filtered_users)
            
            elif confirmation_stage == "confirmation":
                # User is confirming selected email
                selected_email = session.workflow_state.get("selected_email")
                is_confirmed = await self._validate_confirmation_response(message)
                
                if is_confirmed:
                    return await self._process_selected_email(user_phone, session, selected_email, filtered_users)
                else:
                    # Reset to selection
                    session.workflow_state["confirmation_stage"] = "selection"
                    return await self._request_email_selection(user_phone, session, email_options)
            
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
                username = filtered_users[0].get("name", "there")
            
            # Generate email confirmation response using OpenAI
            message = await self._generate_email_confirmation_response(username, emails)
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_selection_requested",
                "stage": "email_confirmation",
                "email_options": emails
            }
            
        except Exception as e:
            logger.error(f"Email selection request error: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _generate_email_confirmation_response(self, username: str, emails: List[str]) -> str:
        """Generate email confirmation response using OpenAI."""
        try:
            # Format email list
            if len(emails) == 1:
                email_text = f"Is this your email: {emails[0]}?"
            else:
                email_list = "\n".join([f"{i+1}. {email}" for i, email in enumerate(emails)])
                email_text = f"Please select your email address:\n\n{email_list}\n\nReply with the number of your email address."
            
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
                return f"Hi {username}! Could you please confirm your email address to proceed: {emails[0]}?"
            else:
                email_list = "\n".join([f"{i+1}. {email}" for i, email in enumerate(emails)])
                return f"Hi {username}! Please select your email address:\n\n{email_list}\n\nReply with the number."
    
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
                message = f"Hi {username}! How can I help you today?"
                await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "authentication_completed",
                    "user_type": "buyer",
                    "redirect_to_main_flow": True
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
            
            if otp_response.get("statusCode") == "1001":
                session.workflow_state["otp_retry_count"] = session.workflow_state.get("otp_retry_count", 0) + 1
                
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
                
                if "email not exists" in error_msg.lower():
                    await self.whatsapp_service.send_message(user_phone, "Email address not found in our system. Please contact support.")
                    return {"status": "redirect_to_support", "reason": "email_not_exists"}
                else:
                    await self.whatsapp_service.send_message(user_phone, "Failed to send OTP. Please contact support.")
                    return {"status": "redirect_to_support", "reason": "otp_send_failed"}
                    
        except Exception as e:
            logger.error(f"OTP send error: {e}")
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
            
            if validation_response.get("statusCode") == "1001":
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
                # OTP invalid - handle retry logic
                return await self._handle_invalid_otp(user_phone, session, email)
                
        except Exception as e:
            logger.error(f"OTP validation error: {e}")
            await self.whatsapp_service.send_message(user_phone, "Error validating OTP. Please contact support.")
            return {"status": "redirect_to_support", "reason": "otp_validation_error"}
    
    async def _handle_invalid_otp_format(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """Handle invalid OTP format."""
        try:
            retry_count = session.workflow_state.get("otp_retry_count", 0)
            
            if retry_count >= 3:
                await self.whatsapp_service.send_message(user_phone, "Maximum OTP attempts exceeded. Please contact support.")
                return {"status": "redirect_to_support", "reason": "max_otp_retries_exceeded"}
            
            message = "Please enter a valid OTP (4-6 digits) or reply 'RESEND' to get a new OTP:"
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
            message = f"We found the following email address associated with your phone number. Could you please help us verify it?\n\n{selected_email}"
            
            # await self.whatsapp_service.send_message(user_phone, message)
            
            # Send Yes/No confirmation buttons
            buttons_config = [
                {"id": "confirm_email", "title": "Yes"},
                {"id": "reject_email", "title": "No"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user_phone,
                "Email Confirmation", 
                message,
                buttons_config
            )
            
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
            return await self._validate_confirmation_with_ai(message)
            
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