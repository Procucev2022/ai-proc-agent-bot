"""
Authentication Service for WhatsApp Bot.

Handles complete user authentication flow including:
- Token validation
- User lookup via API
- Intent clarification (Buy/Sell)
- Session state management
- Integration with registration flow
"""

import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from app.models import User, ConversationSession, UserType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.schemas.user import UserDetailsSchema
from app.procucev_apis.auth_apis import AuthAPIService

logger = logging.getLogger(__name__)


class AuthenticationService:
    """Handles user authentication and intent clarification."""
    
    def __init__(self, whatsapp_service: WhatsAppService = None, 
                 openai_service: OpenAIService = None,
                 response_helpers: ResponseHelpers = None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.openai_service = openai_service or OpenAIService()
        self.response_helpers = response_helpers or ResponseHelpers(self.openai_service)
        self.auth_redis_service = get_auth_redis_service()
        self.auth_api_service = AuthAPIService()
    
    async def validate_token(self, phone_number: str) -> Optional[UserDetailsSchema]:
        """
        Validate user token from Redis auth storage.
        
        Returns UserDetailsSchema if valid token exists, None otherwise.
        """
        try:
            user_data = await self.auth_redis_service.retrieve(phone_number)
            if user_data:
                # user_data is already a UserDetailsSchema
                return user_data
            return None
        except Exception as e:
            logger.error(f"Authentication error for {phone_number}: {e}")
            return None
    
    async def store_user_session(self, phone_number: str, user_details: UserDetailsSchema) -> bool:
        """
        Store user session data in Redis.
        """
        try:
            session_data = {**user_details.dict(), "authenticated_at": datetime.now().isoformat()}
            await self.auth_redis_service.store(phone_number, session_data)
            logger.info(f"Session stored for user {phone_number} (ID: {user_details.id})")
            return True
        except Exception as e:
            logger.error(f"Session storage error for {phone_number}: {e}")
            return False
    
    async def validate_token_and_authenticate(self, user_phone: str, message: str, 
                                            session: ConversationSession) -> Dict[str, Any]:
        """
        Main authentication entry point.
        
        Handles token validation failure and routes to appropriate authentication flow.
        """
        try:
            # Step 1: Check if user intent is clear from message
            intent_result = await self._classify_user_intent(message)
            user_intent = intent_result.get("intent")  # "buy", "sell", or "unclear"
            
            # Step 2: If intent unclear, ask for clarification
            if user_intent == "unclear":
                return await self._request_intent_clarification(user_phone, session, message)
            
            # Step 3: Store intent and proceed with user lookup
            session.workflow_state["user_intent"] = user_intent
            session.workflow_state["authentication_stage"] = "user_lookup"
            
            # Step 4: Call Authentication API to lookup user
            api_result = await self._call_authentication_api(user_phone)
            
            # Step 5: Handle API response based on user intent
            return await self._handle_authentication_response(
                user_phone, session, api_result, user_intent, message
            )
            
        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "authentication_error", str(e))
    
    async def handle_intent_clarification_response(self, user_phone: str, message: str,
                                                 session: ConversationSession) -> Dict[str, Any]:
        """Handle user response to intent clarification."""
        try:
            # Re-classify intent from user response
            intent_result = await self._classify_user_intent(message)
            user_intent = intent_result.get("intent")
            
            if user_intent == "unclear":
                # Still unclear, increment count and check limit
                clarification_count = session.workflow_state.get("intent_clarification_count", 0)
                if clarification_count >= 2:
                    # Max attempts reached, redirect to support
                    session.workflow_state["workflow_type"] = "general_inquiry"
                    await self.whatsapp_service.send_message(
                        user_phone,
                        "I understand you have a general inquiry. Our support team will assist you shortly."
                    )
                    return {
                        "status": "redirected_to_support",
                        "reason": "max_intent_clarification_attempts"
                    }
                
                # Ask again with more specific options
                session.workflow_state["intent_clarification_count"] = clarification_count + 1
                await self.whatsapp_service.send_message(
                    user_phone,
                    "Please choose one:\n1. I want to BUY products\n2. I want to SELL products"
                )
                return {"status": "intent_clarification_retry", "stage": "intent_clarification"}
            
            # Intent is now clear, proceed with authentication
            session.workflow_state["user_intent"] = user_intent
            session.workflow_state["authentication_stage"] = "user_lookup"
            
            # Call Authentication API
            api_result = await self._call_authentication_api(user_phone)
            
            return await self._handle_authentication_response(
                user_phone, session, api_result, user_intent, message
            )
            
        except Exception as e:
            logger.error(f"Intent clarification error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "intent_clarification_error", str(e))
    
    async def handle_email_confirmation_response(self, user_phone: str, message: str,
                                               session: ConversationSession) -> Dict[str, Any]:
        """Handle user response to email confirmation."""
        try:
            auth_data = session.workflow_state.get("authentication_data", {})
            user_intent = session.workflow_state.get("user_intent")
            
            # Parse user response first
            confirmation_result = await self._parse_email_confirmation(message, auth_data)
            
            if confirmation_result["action"] == "confirmed":
                # User confirmed email, proceed to email OTP
                selected_email = confirmation_result["email"]
                unique_id = confirmation_result["unique_id"]
                
                session.workflow_state["selected_email"] = selected_email
                session.workflow_state["unique_id"] = unique_id
                session.workflow_state["user_unique_id"] = unique_id
                session.workflow_state["authentication_stage"] = "email_otp"
                
                return await self._initiate_email_otp(user_phone, session, selected_email, unique_id)
                
            elif confirmation_result["action"] == "declined":
                # User declined, redirect to registration
                session.workflow_state["workflow_type"] = "registration"
                session.workflow_state["registration_intent"] = user_intent
                return {
                    "status": "redirect_to_registration",
                    "user_intent": user_intent,
                    "message": "User declined email, redirecting to registration"
                }
                
            else:
                # Check for intent change if response is invalid
                intent_change_result = await self._check_intent_change(message, user_intent)
                if intent_change_result["changed"]:
                    return await self._handle_intent_change(user_phone, session, intent_change_result["new_intent"])
                
                # Invalid response, ask for clarification
                return await self._request_email_confirmation_clarification(user_phone, auth_data)
                
        except Exception as e:
            logger.error(f"Email confirmation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "email_confirmation_error", str(e))
    
    async def handle_otp_verification(self, user_phone: str, otp: str,
                                    session: ConversationSession) -> Dict[str, Any]:
        """Handle OTP verification."""
        try:
            selected_email = session.workflow_state.get("selected_email")
            unique_id = session.workflow_state.get("unique_id")
            user_intent = session.workflow_state.get("user_intent")
            
            # Verify OTP via API
            verification_result = await self._verify_otp_api(otp, selected_email, unique_id)
            
            if verification_result["success"]:
                # OTP verified successfully
                session.workflow_state["authentication_stage"] = "completed"
                session.workflow_state["authenticated"] = True
                session.workflow_state["verified_email"] = selected_email
                session.workflow_state["user_unique_id"] = unique_id
                
                # Handle post-authentication flow based on user type
                return await self._handle_successful_authentication(
                    user_phone, session, user_intent, selected_email
                )
                
            else:
                # OTP verification failed
                retry_count = session.workflow_state.get("otp_retry_count", 0) + 1
                session.workflow_state["otp_retry_count"] = retry_count
                
                if retry_count >= 2:
                    # Max retries reached, redirect to support
                    return await self._redirect_to_support(
                        user_phone, "otp_verification_failed", "Maximum OTP retry attempts exceeded"
                    )
                else:
                    # Allow one more retry
                    await self.whatsapp_service.send_message(
                        user_phone,
                        f"Invalid OTP. Please try again ({retry_count}/2 attempts used):"
                    )
                    return {"status": "otp_retry", "stage": "email_otp", "retry_count": retry_count}
                    
        except Exception as e:
            logger.error(f"OTP verification error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "otp_verification_error", str(e))
    
    # Private helper methods
    
    async def _classify_user_intent(self, message: str) -> Dict[str, Any]:
        """Classify user intent as buy, sell, or unclear using OpenAI function calling."""
        try:
            # Use OpenAI function calling for structured intent classification
            result = self.openai_service.classify_auth_intent(message)
            
            if result.get("success"):
                return {
                    "intent": result.get("intent", "unclear"),
                    "confidence": result.get("confidence", 0.0)
                }
            else:
                # Fallback to simple rule-based classification
                return self._simple_intent_classification(message)
                
        except Exception as e:
            logger.error(f"Intent classification error: {e}")
            return self._simple_intent_classification(message)
    
    def _simple_intent_classification(self, message: str) -> Dict[str, Any]:
        """Simple rule-based intent classification fallback."""
        message_lower = message.lower().strip()
        
        # Strong buy indicators
        if any(word in message_lower for word in ["buy", "buying", "purchase", "need", "looking for", "want to buy", "procure"]):
            return {"intent": "buy", "confidence": 0.8}
        
        # Strong sell indicators  
        elif any(word in message_lower for word in ["sell", "selling", "offer", "provide", "want to sell", "supply"]):
            return {"intent": "sell", "confidence": 0.8}
        
        # Check for simple yes/no responses in context
        elif message_lower in ["yes", "y", "no", "n"]:
            return {"intent": "unclear", "confidence": 0.2}
        
        else:
            return {"intent": "unclear", "confidence": 0.3}
    
    async def _request_intent_clarification(self, user_phone: str, session: ConversationSession, 
                                          message: str) -> Dict[str, Any]:
        """Request intent clarification from user."""
        # Check if already asked for clarification to prevent loops
        clarification_count = session.workflow_state.get("intent_clarification_count", 0)
        
        if clarification_count >= 2:
            # Too many clarification attempts, redirect to general inquiry
            session.workflow_state["workflow_type"] = "general_inquiry"
            await self.whatsapp_service.send_message(
                user_phone,
                "I understand you have a general inquiry. Our support team will assist you shortly."
            )
            return {
                "status": "redirected_to_support",
                "reason": "max_intent_clarification_attempts"
            }
        
        session.workflow_state["authentication_stage"] = "intent_clarification"
        session.workflow_state["intent_clarification_count"] = clarification_count + 1
        
        clarification_message = (
            "Welcome, and thank you for reaching out to QUA!\n"
            "How can I assist you today — are you looking to Buy or Sell?"
        )
        
        await self.whatsapp_service.send_message(user_phone, clarification_message)
        
        return {
            "status": "intent_clarification_requested",
            "stage": "intent_clarification",
            "message": "Requested user intent clarification"
        }
    
    async def _call_authentication_api(self, user_phone: str) -> Dict[str, Any]:
        """Call Authentication API (API 1) to lookup user."""
        try:
            # Use actual Procucev API service
            api_result = await self.auth_api_service.authenticate_user(user_phone)
            
            if api_result.get("success"):
                # API returns list of users in response field
                users_list = api_result.get("response", [])
                if users_list:
                    # Convert all users to standard format
                    formatted_users = []
                    for user_data in users_list:
                        formatted_users.append({
                            "name": user_data.get("fullName", ""),
                            "email": user_data.get("username", ""),
                            "unique_id": user_data.get("uniqueId", ""),
                            "client_id": user_data.get("orgId", ""),
                            "role": "buyer" if user_data.get("selfClient") else "seller",
                            "self_client": user_data.get("selfClient", False),
                            "company_name": user_data.get("companyName", ""),
                            "approved": user_data.get("approved", False),
                            "active": user_data.get("active", False)
                        })
                    
                    return {"found": True, "users": formatted_users}
                else:
                    return {"found": False, "error": "No users found"}
            else:
                return {"found": False, "error": api_result.get("error", "User not found")}
            
        except Exception as e:
            logger.error(f"Authentication API error for {user_phone}: {e}")
            return {"found": False, "error": str(e)}
    
    async def _handle_authentication_response(self, user_phone: str, session: ConversationSession,
                                            api_result: Dict[str, Any], user_intent: str,
                                            message: str) -> Dict[str, Any]:
        """Handle Authentication API response."""
        try:
            if not api_result.get("found"):
                # User not found, redirect to registration
                return await self._redirect_to_registration(user_phone, session, user_intent)
            
            users = api_result.get("users", [])
            
            # Filter users by intent and self_client flag
            if user_intent == "buy":
                matching_users = [user for user in users if user.get("self_client") == True]
            else:  # sell
                matching_users = [user for user in users if user.get("self_client") == False]
            
            # If multiple matching users, take first one for now
            if len(matching_users) > 1:
                matching_users = [matching_users[0]]
            
            if matching_users:
                # Found matching user(s)
                session.workflow_state["authentication_data"] = {
                    "matching_users": matching_users,
                    "user_intent": user_intent
                }
                
                if user_intent == "buy":
                    # Buyers: Direct authentication without email confirmation
                    selected_user = matching_users[0]
                    session.workflow_state["selected_email"] = selected_user["email"]
                    session.workflow_state["unique_id"] = selected_user["unique_id"]
                    session.workflow_state["user_unique_id"] = selected_user["unique_id"]
                    session.workflow_state["authentication_stage"] = "completed"
                    session.workflow_state["authenticated"] = True
                    
                    return await self._handle_successful_authentication(
                        user_phone, session, user_intent, selected_user["email"]
                    )
                else:
                    # Sellers: Email confirmation required
                    session.workflow_state["authentication_stage"] = "email_confirmation"
                    return await self._request_email_confirmation(user_phone, matching_users)
                
            else:
                # No matching users, check for intent mismatch
                if user_intent == "buy":
                    other_role_users = [user for user in users if user.get("self_client") == False]
                else:
                    other_role_users = [user for user in users if user.get("self_client") == True]
                
                if other_role_users:
                    # Intent mismatch, confirm intent
                    return await self._handle_intent_mismatch(user_phone, session, user_intent, other_role_users)
                else:
                    # No users found, redirect to registration
                    session.workflow_state["workflow_type"] = "registration"
                    session.workflow_state["registration_intent"] = user_intent
                    return {
                        "status": "redirect_to_registration",
                        "user_intent": user_intent,
                        "message": "User not found, redirecting to registration"
                    }
                    
        except Exception as e:
            logger.error(f"Authentication response handling error: {e}")
            return await self._redirect_to_support(user_phone, "authentication_response_error", str(e))
    
    async def _request_email_confirmation(self, user_phone: str, matching_users: list) -> Dict[str, Any]:
        """Request email confirmation from user."""
        try:
            if len(matching_users) == 1:
                # Single email
                user = matching_users[0]
                message = (
                    f"Welcome, {user['name']}. Before we begin, could you please "
                    f"confirm your email address: {user['email']}?\n\n"
                    f"For confirmation type 'confirm' else 'NO'"
                )
            else:
                # Multiple emails
                message = "We found multiple records. Please choose the correct email to start:\n"
                for i, user in enumerate(matching_users, 1):
                    message += f"{i}. {user['email']} ({user.get('company_name', 'Company')})\n"
                message += "\nFor confirmation type 'confirm' else 'NO'"
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_confirmation_requested",
                "stage": "email_confirmation",
                "user_count": len(matching_users)
            }
            
        except Exception as e:
            logger.error(f"Email confirmation request error: {e}")
            return await self._redirect_to_support(user_phone, "email_confirmation_request_error", str(e))
    
    async def _parse_email_confirmation(self, message: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse user response to email confirmation using OpenAI."""
        try:
            matching_users = auth_data.get("matching_users", [])
            
            # Use OpenAI to parse email confirmation response
            result = await self.openai_service.parse_email_confirmation(
                message, 
                [user["email"] for user in matching_users]
            )
            
            if result.get("success"):
                action = result.get("action")  # "confirmed", "declined", "invalid"
                
                if action == "confirmed":
                    if len(matching_users) == 1:
                        return {
                            "action": "confirmed",
                            "email": matching_users[0]["email"],
                            "unique_id": matching_users[0]["unique_id"]
                        }
                    else:
                        # Handle multiple emails with selection
                        selection = result.get("selection", 1)
                        if 1 <= selection <= len(matching_users):
                            selected_user = matching_users[selection - 1]
                            return {
                                "action": "confirmed",
                                "email": selected_user["email"],
                                "unique_id": selected_user["unique_id"]
                            }
                
                return {"action": action}
            
            # Fallback to simple parsing if OpenAI fails
            return self._simple_email_confirmation_parsing(message, matching_users)
            
        except Exception as e:
            logger.error(f"Email confirmation parsing error: {e}")
            return self._simple_email_confirmation_parsing(message, matching_users)
    
    def _simple_email_confirmation_parsing(self, message: str, matching_users: list) -> Dict[str, Any]:
        """Simple fallback parsing for email confirmation."""
        message_lower = message.lower().strip()
        
        # Simple positive confirmation
        if any(word in message_lower for word in ["confirm", "yes", "correct", "ok"]):
            if len(matching_users) == 1:
                return {
                    "action": "confirmed",
                    "email": matching_users[0]["email"],
                    "unique_id": matching_users[0]["unique_id"]
                }
        
        # Simple negative response
        if any(word in message_lower for word in ["no", "wrong", "not"]):
            return {"action": "declined"}
        
        # Numeric selection
        if len(matching_users) > 1:
            try:
                selection = int(message_lower)
                if 1 <= selection <= len(matching_users):
                    selected_user = matching_users[selection - 1]
                    return {
                        "action": "confirmed",
                        "email": selected_user["email"],
                        "unique_id": selected_user["unique_id"]
                    }
            except ValueError:
                pass
        
        return {"action": "invalid"}
    
    async def _initiate_email_otp(self, user_phone: str, session: ConversationSession,
                                email: str, unique_id: str) -> Dict[str, Any]:
        """Initiate email OTP process."""
        try:
            # Call Send OTP API (API 5)
            otp_result = await self._send_otp_api(email, unique_id, user_phone)
            
            if otp_result.get("success"):
                await self.whatsapp_service.send_message(
                    user_phone,
                    f"OTP sent to your email: {email}. Please enter the OTP:"
                )
                
                session.workflow_state["otp_sent_at"] = utc_now().isoformat()
                session.workflow_state["otp_retry_count"] = 0
                
                return {
                    "status": "otp_sent",
                    "stage": "email_otp",
                    "email": email
                }
            else:
                return await self._redirect_to_support(
                    user_phone, "otp_send_failed", "Failed to send OTP"
                )
                
        except Exception as e:
            logger.error(f"OTP initiation error: {e}")
            return await self._redirect_to_support(user_phone, "otp_initiation_error", str(e))
    
    async def _send_otp_api(self, email: str, unique_id: str, phone_number: str) -> Dict[str, Any]:
        """Call Send OTP API (API 5)."""
        try:
            # Mock API call - replace with actual implementation
            # API 5: Input: Email, unique identifier, phone number
            # Response: OTP that is sent
            
            return {
                "success": True,
                "otp_sent": "123456",  # Mock OTP
                "message": "OTP sent successfully"
            }
            
        except Exception as e:
            logger.error(f"Send OTP API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def _verify_otp_api(self, otp: str, email: str, unique_id: str) -> Dict[str, Any]:
        """Call OTP Verification API (API 3)."""
        try:
            # Mock API call - replace with actual implementation
            # API 3: OTP entered by user shall be sent for verification
            
            # Mock verification - in real implementation, call actual API
            if otp == "123456":  # Mock valid OTP
                return {
                    "success": True,
                    "message": "OTP verified successfully"
                }
            else:
                return {
                    "success": False,
                    "message": "Invalid OTP"
                }
                
        except Exception as e:
            logger.error(f"OTP verification API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def _handle_successful_authentication(self, user_phone: str, session: ConversationSession,
                                              user_intent: str, email: str) -> Dict[str, Any]:
        """Handle successful authentication completion."""
        try:
            # Set user type in session
            session.user_type = UserType.buyer if user_intent == "buy" else UserType.seller
            
            if user_intent == "buy":
                # Buyer authentication success
                # Check if company/email domain match for approval status
                approval_result = await self._check_domain_approval(email)
                
                if approval_result.get("approved"):
                    # Update approval status via API 4
                    await self._update_approval_status_api(session.workflow_state["user_unique_id"], True)
                    
                    # Store authenticated user session in Redis
                    user_details = UserDetailsSchema(
                        id=session.workflow_state["user_unique_id"],
                        name=session.workflow_state.get("authentication_data", {}).get("matching_users", [{}])[0].get("name", ""),
                        email=email,
                        self_client=True,
                        role="buyer",
                        is_registered=True,
                        phone_number=user_phone,
                        company_name=session.workflow_state.get("authentication_data", {}).get("matching_users", [{}])[0].get("company_name", "")
                    )
                    await self.store_user_session(user_phone, user_details)
                    
                    message = (
                        "Authentication successful! How can I help you today? "
                        "Want to raise an RFQ or any other support?"
                    )
                else:
                    # Domain mismatch, mark as not approved
                    await self._update_approval_status_api(session.workflow_state["user_unique_id"], False)
                    
                    message = (
                        "Registration successful—thank you! Our team will get in touch with you "
                        "shortly to complete your onboarding so that you can raise RFQs. "
                        "In the meantime please let us know if you want us to support you with anything else?"
                    )
                
                await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "authentication_completed",
                    "user_type": "buyer",
                    "approved": approval_result.get("approved", False),
                    "ready_for_main_flow": approval_result.get("approved", False)
                }
                
            else:  # seller
                # Seller authentication success
                # Store authenticated user session in Redis
                user_details = UserDetailsSchema(
                    id=session.workflow_state["user_unique_id"],
                    name=session.workflow_state.get("authentication_data", {}).get("matching_users", [{}])[0].get("name", ""),
                    email=email,
                    self_client=False,
                    role="seller",
                    is_registered=True,
                    phone_number=user_phone,
                    company_name=session.workflow_state.get("authentication_data", {}).get("matching_users", [{}])[0].get("company_name", "")
                )
                await self.store_user_session(user_phone, user_details)
                
                message = (
                    f"Thank you for the confirmation. "
                    "How can I assist you with RFQ opportunities today?"
                )
                
                await self.whatsapp_service.send_message(user_phone, message)
                
                return {
                    "status": "authentication_completed",
                    "user_type": "seller",
                    "ready_for_main_flow": True
                }
                
        except Exception as e:
            logger.error(f"Successful authentication handling error: {e}")
            return await self._redirect_to_support(user_phone, "post_authentication_error", str(e))
    
    async def _check_domain_approval(self, email: str) -> Dict[str, Any]:
        """Check if company name and email domain match for approval."""
        try:
            # Mock implementation - replace with actual domain matching logic
            # This would typically involve:
            # 1. Extract domain from email
            # 2. Compare with company name from registration
            # 3. Return approval status
            
            domain = email.split('@')[1] if '@' in email else ""
            
            # Mock approval logic
            approved_domains = ["company.com", "procucev.com", "example.com"]
            approved = domain in approved_domains
            
            return {
                "approved": approved,
                "domain": domain,
                "reason": "Domain match" if approved else "Domain mismatch"
            }
            
        except Exception as e:
            logger.error(f"Domain approval check error: {e}")
            return {"approved": False, "error": str(e)}
    
    async def _update_approval_status_api(self, unique_id: str, approved: bool) -> Dict[str, Any]:
        """Update approval status via API 4."""
        try:
            # Mock API call - replace with actual implementation
            # API 4: Updating the Approved Flag
            
            return {
                "success": True,
                "unique_id": unique_id,
                "approved": approved,
                "message": "Approval status updated"
            }
            
        except Exception as e:
            logger.error(f"Update approval status API error: {e}")
            return {"success": False, "error": str(e)}
    
    async def _handle_intent_mismatch(self, user_phone: str, session: ConversationSession,
                                    user_intent: str, other_users: list) -> Dict[str, Any]:
        """Handle intent mismatch scenario."""
        try:
            opposite_intent = "sell" if user_intent == "buy" else "buy"
            
            message = (
                f"I see you mentioned wanting to {user_intent}, but our records show you as a "
                f"{opposite_intent}er. Could you please confirm your intent?"
            )
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            session.workflow_state["authentication_stage"] = "intent_mismatch_clarification"
            session.workflow_state["other_users"] = other_users
            
            return {
                "status": "intent_mismatch",
                "stage": "intent_mismatch_clarification",
                "user_intent": user_intent,
                "found_intent": opposite_intent
            }
            
        except Exception as e:
            logger.error(f"Intent mismatch handling error: {e}")
            return await self._redirect_to_support(user_phone, "intent_mismatch_error", str(e))
    
    async def _redirect_to_registration(self, user_phone: str, session: ConversationSession,
                                      user_intent: str) -> Dict[str, Any]:
        """Redirect user to registration flow."""
        try:
            session.workflow_state["authentication_stage"] = "redirect_to_registration"
            session.workflow_state["registration_intent"] = user_intent
            
            return {
                "status": "redirect_to_registration",
                "user_intent": user_intent,
                "message": "User not found, redirecting to registration"
            }
            
        except Exception as e:
            logger.error(f"Registration redirect error: {e}")
            return await self._redirect_to_support(user_phone, "registration_redirect_error", str(e))
    
    async def _request_email_confirmation_clarification(self, user_phone: str, 
                                                      auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Request clarification for email confirmation."""
        try:
            matching_users = auth_data.get("matching_users", [])
            
            if len(matching_users) == 1:
                message = "Please type 'confirm' if this is your email or 'NO' if it's not."
            else:
                message = "Please select the number corresponding to your email or type 'NO' if none match."
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "email_confirmation_clarification",
                "stage": "email_confirmation"
            }
            
        except Exception as e:
            logger.error(f"Email confirmation clarification error: {e}")
            return await self._redirect_to_support(user_phone, "email_clarification_error", str(e))
    
    async def _check_intent_change(self, message: str, current_intent: str) -> Dict[str, Any]:
        """Check if user is changing their intent."""
        try:
            intent_result = await self._classify_user_intent(message)
            new_intent = intent_result.get("intent")
            
            # If new intent is clear and different from current
            if new_intent != "unclear" and new_intent != current_intent:
                return {"changed": True, "new_intent": new_intent}
            
            return {"changed": False}
            
        except Exception as e:
            logger.error(f"Intent change check error: {e}")
            return {"changed": False}
    
    async def _handle_intent_change(self, user_phone: str, session: ConversationSession, 
                                  new_intent: str) -> Dict[str, Any]:
        """Handle user intent change during authentication."""
        try:
            current_intent = session.workflow_state.get("user_intent")
            
            # Confirm intent change with user
            await self.whatsapp_service.send_message(
                user_phone,
                f"I notice you mentioned wanting to {new_intent}, but we were processing you as a {current_intent}er. "
                f"Would you like to switch to {new_intent}ing instead?"
            )
            
            session.workflow_state["authentication_stage"] = "intent_change_confirmation"
            session.workflow_state["pending_intent_change"] = new_intent
            
            return {
                "status": "intent_change_confirmation",
                "current_intent": current_intent,
                "new_intent": new_intent
            }
            
        except Exception as e:
            logger.error(f"Intent change handling error: {e}")
            return await self._redirect_to_support(user_phone, "intent_change_error", str(e))
    
    async def handle_intent_change_confirmation(self, user_phone: str, message: str,
                                              session: ConversationSession) -> Dict[str, Any]:
        """Handle user confirmation of intent change."""
        try:
            message_lower = message.lower().strip()
            pending_intent = session.workflow_state.get("pending_intent_change")
            
            if any(word in message_lower for word in ["yes", "y", "switch", "change"]):
                # User confirmed intent change, restart authentication with new intent
                session.workflow_state["user_intent"] = pending_intent
                session.workflow_state["authentication_stage"] = "user_lookup"
                session.workflow_state.pop("pending_intent_change", None)
                session.workflow_state.pop("authentication_data", None)
                
                # Call Authentication API with new intent
                api_result = await self._call_authentication_api(user_phone)
                
                return await self._handle_authentication_response(
                    user_phone, session, api_result, pending_intent, message
                )
                
            else:
                # User declined intent change, continue with original intent
                current_intent = session.workflow_state.get("user_intent")
                session.workflow_state["authentication_stage"] = "email_confirmation"
                session.workflow_state.pop("pending_intent_change", None)
                
                await self.whatsapp_service.send_message(
                    user_phone,
                    f"Continuing with your original {current_intent}ing request. Please confirm your email."
                )
                
                return {
                    "status": "intent_change_declined",
                    "stage": "email_confirmation"
                }
                
        except Exception as e:
            logger.error(f"Intent change confirmation error: {e}")
            return await self._redirect_to_support(user_phone, "intent_change_confirmation_error", str(e))
    
    async def _redirect_to_support(self, user_phone: str, error_reason: str, 
                                 error_details: str = "") -> Dict[str, Any]:
        """Redirect user to support with error context."""
        try:
            support_message = "Our support team will contact you shortly."
            await self.whatsapp_service.send_message(user_phone, support_message)
            
            # Log support request for internal tracking
            logger.error(f"Support redirect for {user_phone}: {error_reason} - {error_details}")
            
            return {
                "status": "redirected_to_support",
                "error_reason": error_reason,
                "error_details": error_details,
                "message": "User redirected to support"
            }
            
        except Exception as e:
            logger.error(f"Support redirect error: {e}")
            return {
                "status": "error",
                "error": "Failed to redirect to support"
            }