"""
Registration Service for WhatsApp Bot.

Handles complete user registration flow including:
- Entity-based data collection for buyers and sellers
- Registration API integration
- Email OTP verification
- Domain approval for buyers
- Complementary RFQ offers for sellers
"""

import logging
from typing import Dict, Any, List
from app.models import User, ConversationSession, UserType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.entity_service import EntityService
from app.services.helpers.response_helpers import ResponseHelpers
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class RegistrationService:
    """Handles user registration workflow with entity-based data collection."""
    
    def __init__(self, whatsapp_service: WhatsAppService = None,
                 openai_service: OpenAIService = None,
                 entity_service: EntityService = None,
                 response_helpers: ResponseHelpers = None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.openai_service = openai_service or OpenAIService()
        self.entity_service = entity_service or EntityService()
        self.response_helpers = response_helpers or ResponseHelpers(self.openai_service)
    
    async def initiate_registration(self, user_phone: str, session: ConversationSession,
                                  user_intent: str, message: str = "") -> Dict[str, Any]:
        """
        Initiate registration flow for new users.
        
        Args:
            user_phone: User's phone number
            session: Current conversation session
            user_intent: "buy" or "sell"
            message: Optional initial message from user
        """
        try:
            # Set registration workflow state
            session.workflow_type = "registration"
            session.workflow_state["registration_stage"] = "introduction"
            session.workflow_state["registration_intent"] = user_intent
            session.workflow_state["extracted_entities"] = {}
            
            # Send introduction message based on user intent
            if user_intent == "buy":
                intro_message = await self._get_buyer_introduction_message()
            else:  # sell
                intro_message = await self._get_seller_introduction_message()
            
            await self.whatsapp_service.send_message(user_phone, intro_message)
            
            # Move to data collection stage
            session.workflow_state["registration_stage"] = "data_collection"
            
            return {
                "status": "registration_initiated",
                "user_intent": user_intent,
                "stage": "data_collection",
                "message": "Registration flow started"
            }
            
        except Exception as e:
            logger.error(f"Registration initiation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "registration_initiation_error", str(e))
    
    async def handle_registration_data_collection(self, user_phone: str, message: str,
                                                session: ConversationSession) -> Dict[str, Any]:
        """Handle data collection during registration."""
        try:
            user_intent = session.workflow_state.get("registration_intent")
            current_entities = session.workflow_state.get("extracted_entities", {})
            
            # Extract entities from user message
            entity_result = await self._extract_registration_entities(message, current_entities, user_intent)
            
            # Merge with existing entities
            updated_entities = self._merge_registration_entities(current_entities, entity_result)
            session.workflow_state["extracted_entities"] = updated_entities
            
            # Check completeness
            missing_fields = self._get_missing_registration_fields(updated_entities, user_intent)
            
            if missing_fields:
                # Still missing data, ask for missing fields
                return await self._request_missing_registration_data(
                    user_phone, session, missing_fields, updated_entities, user_intent
                )
            else:
                # All data collected, proceed to confirmation
                return await self._request_registration_confirmation(
                    user_phone, session, updated_entities, user_intent
                )
                
        except Exception as e:
            logger.error(f"Registration data collection error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "registration_data_error", str(e))
    
    async def handle_registration_confirmation(self, user_phone: str, message: str,
                                             session: ConversationSession) -> Dict[str, Any]:
        """Handle registration confirmation response."""
        try:
            confirmation_result = await self._parse_confirmation_response(message)
            
            if confirmation_result["confirmed"]:
                # User confirmed, proceed with registration API call
                entities = session.workflow_state.get("extracted_entities", {})
                user_intent = session.workflow_state.get("registration_intent")
                
                return await self._call_registration_api(user_phone, session, entities, user_intent)
                
            else:
                # User wants to make corrections
                session.workflow_state["registration_stage"] = "data_collection"
                
                await self.whatsapp_service.send_message(
                    user_phone,
                    "Please provide the correct information:"
                )
                
                return {
                    "status": "registration_correction_requested",
                    "stage": "data_collection",
                    "message": "User requested corrections"
                }
                
        except Exception as e:
            logger.error(f"Registration confirmation error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "registration_confirmation_error", str(e))
    
    async def handle_registration_otp_verification(self, user_phone: str, otp: str,
                                                 session: ConversationSession) -> Dict[str, Any]:
        """Handle OTP verification during registration."""
        try:
            registration_data = session.workflow_state.get("registration_data", {})
            user_intent = session.workflow_state.get("registration_intent")
            
            # Verify OTP
            verification_result = await self._verify_registration_otp(
                otp, registration_data.get("email"), registration_data.get("unique_id")
            )
            
            if verification_result["success"]:
                # OTP verified, complete registration
                return await self._complete_registration(user_phone, session, user_intent, registration_data)
                
            else:
                # OTP verification failed
                retry_count = session.workflow_state.get("otp_retry_count", 0) + 1
                session.workflow_state["otp_retry_count"] = retry_count
                
                if retry_count >= 2:
                    return await self._redirect_to_support(
                        user_phone, "registration_otp_failed", "Maximum OTP retry attempts exceeded"
                    )
                else:
                    await self.whatsapp_service.send_message(
                        user_phone,
                        f"Invalid OTP. Please try again ({retry_count}/2 attempts used):"
                    )
                    return {"status": "registration_otp_retry", "retry_count": retry_count}
                    
        except Exception as e:
            logger.error(f"Registration OTP verification error for {user_phone}: {e}")
            return await self._redirect_to_support(user_phone, "registration_otp_error", str(e))
    
    # Private helper methods
    
    async def _get_buyer_introduction_message(self) -> str:
        """Get introduction message for buyer registration."""
        return (
            "Hello, it looks like you're not registered yet. Let's get started—"
            "please share the details below to complete your registration:\n\n"
            "• Full Name\n"
            "• Company Name\n"
            "• Organization Email\n"
            "• Pincode"
        )
    
    async def _get_seller_introduction_message(self) -> str:
        """Get introduction message for seller registration."""
        return (
            "Hello, it looks like you're not registered yet. Let's get started—"
            "please share the details below to complete your registration. "
            "You will also get complementary RFQs after registration:\n\n"
            "• Full Name\n"
            "• Company Name\n"
            "• Email\n"
            "• Location & Pin Code\n"
            "• GSTIN\n"
            "• Products/Services (Categories will be identified from here)"
        )
    
    async def _extract_registration_entities(self, message: str, current_entities: Dict[str, Any],
                                           user_intent: str) -> Dict[str, Any]:
        """Extract registration entities from user message using OpenAI function calling."""
        try:
            # Use OpenAI function calling for structured entity extraction
            result = self.openai_service.extract_registration_entities(message, current_entities, user_intent)
            
            if result.get("success"):
                return result.get("entities", {})
            else:
                # Fallback to simple parsing
                return self._simple_entity_extraction(message, user_intent)
                
        except Exception as e:
            logger.error(f"Registration entity extraction error: {e}")
            return self._simple_entity_extraction(message, user_intent)
    
    def _simple_entity_extraction(self, message: str, user_intent: str) -> Dict[str, Any]:
        """Simple fallback entity extraction."""
        entities = {}
        message_lower = message.lower()
        
        # Simple email detection
        import re
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, message)
        if emails:
            entities["email"] = emails[0]
        
        # Simple pincode detection (6 digits)
        pincode_pattern = r'\b\d{6}\b'
        pincodes = re.findall(pincode_pattern, message)
        if pincodes:
            entities["pincode"] = pincodes[0]
        
        return entities
    
    def _merge_registration_entities(self, current: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """Merge new entities with current entities."""
        merged = current.copy()
        
        for key, value in new.items():
            if value is not None and value != "":
                merged[key] = value
        
        return merged
    
    def _get_missing_registration_fields(self, entities: Dict[str, Any], user_intent: str) -> List[str]:
        """Get list of missing required registration fields."""
        if user_intent == "buy":
            required_fields = ["name", "companyName", "email", "pincode"]
        else:  # sell
            required_fields = ["name", "companyName", "email", "pincode", "gstin", "products_services"]
        
        missing = []
        for field in required_fields:
            if not entities.get(field):
                missing.append(field)
        
        return missing
    
    async def _request_missing_registration_data(self, user_phone: str, session: ConversationSession,
                                               missing_fields: List[str], current_entities: Dict[str, Any],
                                               user_intent: str) -> Dict[str, Any]:
        """Request missing registration data from user."""
        try:
            # Generate natural questions for missing fields
            questions = []
            field_mapping = {
                "name": "What's your full name?",
                "companyName": "What's your company name?",
                "email": "What's your organization email address?",
                "pincode": "What's your pincode?",
                "gstin": "What's your GSTIN number?",
                "products_services": "What products or services do you offer?"
            }
            
            for field in missing_fields:
                if field in field_mapping:
                    questions.append(field_mapping[field])
            
            if questions:
                message = "I still need a few more details:\n\n" + "\n".join(f"• {q}" for q in questions)
            else:
                message = "Please provide the remaining registration details."
            
            await self.whatsapp_service.send_message(user_phone, message)
            
            return {
                "status": "registration_data_requested",
                "stage": "data_collection",
                "missing_fields": missing_fields,
                "completeness": len(current_entities) / (len(current_entities) + len(missing_fields)) * 100
            }
            
        except Exception as e:
            logger.error(f"Missing data request error: {e}")
            return await self._redirect_to_support(user_phone, "missing_data_request_error", str(e))
    
    async def _request_registration_confirmation(self, user_phone: str, session: ConversationSession,
                                               entities: Dict[str, Any], user_intent: str) -> Dict[str, Any]:
        """Request confirmation of registration data."""
        try:
            # Format confirmation message
            confirmation_lines = []
            field_labels = {
                "name": "Name",
                "companyName": "Company",
                "email": "Email",
                "pincode": "Pincode",
                "gstin": "GSTIN",
                "products_services": "Products/Services"
            }
            
            for field, value in entities.items():
                if value and field in field_labels:
                    confirmation_lines.append(f"• {field_labels[field]}: {value}")
            
            confirmation_message = (
                "Please confirm your registration details:\n\n" +
                "\n".join(confirmation_lines) +
                "\n\nIs this information correct? Reply 'Yes' to confirm or provide corrections."
            )
            
            await self.whatsapp_service.send_message(user_phone, confirmation_message)
            
            session.workflow_state["registration_stage"] = "confirmation"
            
            return {
                "status": "registration_confirmation_requested",
                "stage": "confirmation",
                "entities": entities
            }
            
        except Exception as e:
            logger.error(f"Registration confirmation request error: {e}")
            return await self._redirect_to_support(user_phone, "confirmation_request_error", str(e))
    
    async def _parse_confirmation_response(self, message: str) -> Dict[str, Any]:
        """Parse user confirmation response."""
        message_lower = message.lower().strip()
        
        # Check for positive confirmation
        if any(word in message_lower for word in ["yes", "confirm", "correct", "ok", "right"]):
            return {"confirmed": True}
        
        # Check for negative response
        if any(word in message_lower for word in ["no", "wrong", "incorrect", "change"]):
            return {"confirmed": False}
        
        # Default to not confirmed for unclear responses
        return {"confirmed": False}
    
    async def _call_registration_api(self, user_phone: str, session: ConversationSession,
                                   entities: Dict[str, Any], user_intent: str) -> Dict[str, Any]:
        """Call Registration API (API 2)."""
        try:
            # Prepare registration data
            registration_data = {
                "phone_number": user_phone,
                "name": entities.get("name"),
                "company_name": entities.get("companyName"),
                "email": entities.get("email"),
                "pincode": entities.get("pincode"),
                "role": user_intent,
                "gstin": entities.get("gstin") if user_intent == "sell" else None,
                "products_services": entities.get("products_services") if user_intent == "sell" else None
            }
            
            # Mock API call - replace with actual implementation
            api_result = await self._mock_registration_api_call(registration_data)
            
            if api_result.get("success"):
                # Registration successful, initiate email OTP
                session.workflow_state["registration_data"] = {
                    "email": entities.get("email"),
                    "unique_id": api_result.get("unique_id"),
                    "client_id": api_result.get("client_id")
                }
                session.workflow_state["registration_stage"] = "email_otp"
                
                return await self._initiate_registration_email_otp(user_phone, session, entities.get("email"))
                
            else:
                # Registration failed
                error_message = (
                    "We have faced some challenges during your registration, "
                    "our support team shall get in touch with you shortly"
                )
                await self.whatsapp_service.send_message(user_phone, error_message)
                
                return await self._redirect_to_support(
                    user_phone, "registration_api_failed", api_result.get("error", "Unknown error")
                )
                
        except Exception as e:
            logger.error(f"Registration API call error: {e}")
            return await self._redirect_to_support(user_phone, "registration_api_error", str(e))
    
    async def _mock_registration_api_call(self, registration_data: Dict[str, Any]) -> Dict[str, Any]:
        """Mock registration API call - replace with actual implementation."""
        try:
            # Mock successful registration
            return {
                "success": True,
                "unique_id": f"user_{registration_data['phone_number'][-4:]}",
                "client_id": f"client_{registration_data['phone_number'][-4:]}",
                "email": registration_data["email"],
                "message": "Registration successful"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def _initiate_registration_email_otp(self, user_phone: str, session: ConversationSession,
                                             email: str) -> Dict[str, Any]:
        """Initiate email OTP for registration."""
        try:
            # Send OTP via API
            otp_result = await self._send_registration_otp(email, session.workflow_state["registration_data"]["unique_id"])
            
            if otp_result.get("success"):
                await self.whatsapp_service.send_message(
                    user_phone,
                    f"Registration initiated! OTP sent to your email: {email}. Please enter the OTP:"
                )
                
                session.workflow_state["otp_sent_at"] = utc_now().isoformat()
                session.workflow_state["otp_retry_count"] = 0
                
                return {
                    "status": "registration_otp_sent",
                    "stage": "email_otp",
                    "email": email
                }
            else:
                return await self._redirect_to_support(
                    user_phone, "registration_otp_send_failed", "Failed to send registration OTP"
                )
                
        except Exception as e:
            logger.error(f"Registration OTP initiation error: {e}")
            return await self._redirect_to_support(user_phone, "registration_otp_initiation_error", str(e))
    
    async def _send_registration_otp(self, email: str, unique_id: str) -> Dict[str, Any]:
        """Send registration OTP via API."""
        try:
            # Mock API call - replace with actual implementation
            return {
                "success": True,
                "otp_sent": "123456",
                "message": "Registration OTP sent successfully"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _verify_registration_otp(self, otp: str, email: str, unique_id: str) -> Dict[str, Any]:
        """Verify registration OTP."""
        try:
            # Mock verification - replace with actual API call
            if otp == "123456":
                return {
                    "success": True,
                    "message": "Registration OTP verified successfully"
                }
            else:
                return {
                    "success": False,
                    "message": "Invalid registration OTP"
                }
                
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _complete_registration(self, user_phone: str, session: ConversationSession,
                                   user_intent: str, registration_data: Dict[str, Any]) -> Dict[str, Any]:
        """Complete registration after successful OTP verification."""
        try:
            if user_intent == "buy":
                # Buyer registration completion
                email = registration_data.get("email")
                domain_check = await self._check_buyer_domain_approval(email)
                
                if domain_check.get("approved"):
                    # Update approval status
                    await self._update_buyer_approval_status(registration_data.get("unique_id"), True)
                    
                    message = (
                        "Registration successful—thank you! How can I help you today? "
                        "Want to raise an RFQ or any other support?"
                    )
                    ready_for_main_flow = True
                else:
                    # Domain mismatch
                    await self._update_buyer_approval_status(registration_data.get("unique_id"), False)
                    
                    message = (
                        "Registration successful—thank you! Our team will get in touch with you "
                        "shortly to complete your onboarding so that you can raise RFQs. "
                        "In the meantime please let us know if you want us to support you with anything else?"
                    )
                    ready_for_main_flow = False
                
                await self.whatsapp_service.send_message(user_phone, message)
                
                # Set session as completed
                session.workflow_type = "registration_completed"
                session.user_type = UserType.buyer
                
                return {
                    "status": "buyer_registration_completed",
                    "approved": domain_check.get("approved", False),
                    "ready_for_main_flow": ready_for_main_flow
                }
                
            else:  # seller
                # Seller registration completion
                message = (
                    "Registration successful—thank you! Congratulations you have "
                    "received a complementary RFQ. You may please download the same."
                )
                
                await self.whatsapp_service.send_message(user_phone, message)
                
                # Trigger category mapping (internal step)
                await self._trigger_seller_category_mapping(registration_data.get("unique_id"))
                
                # Set session as completed
                session.workflow_type = "registration_completed"
                session.user_type = UserType.seller
                
                return {
                    "status": "seller_registration_completed",
                    "ready_for_main_flow": True,
                    "complementary_rfq": True
                }
                
        except Exception as e:
            logger.error(f"Registration completion error: {e}")
            return await self._redirect_to_support(user_phone, "registration_completion_error", str(e))
    
    async def _check_buyer_domain_approval(self, email: str) -> Dict[str, Any]:
        """Check buyer domain for approval."""
        try:
            # Extract domain and check against company name
            domain = email.split('@')[1] if '@' in email else ""
            
            # Mock approval logic - replace with actual implementation
            approved_domains = ["company.com", "procucev.com", "example.com"]
            approved = domain in approved_domains
            
            return {
                "approved": approved,
                "domain": domain,
                "reason": "Domain match" if approved else "Domain mismatch"
            }
            
        except Exception as e:
            return {"approved": False, "error": str(e)}
    
    async def _update_buyer_approval_status(self, unique_id: str, approved: bool) -> Dict[str, Any]:
        """Update buyer approval status via API 4."""
        try:
            # Mock API call - replace with actual implementation
            return {
                "success": True,
                "unique_id": unique_id,
                "approved": approved,
                "message": "Buyer approval status updated"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _trigger_seller_category_mapping(self, unique_id: str) -> Dict[str, Any]:
        """Trigger seller category mapping (internal step)."""
        try:
            # Mock category mapping - replace with actual implementation
            logger.info(f"Triggering category mapping for seller {unique_id}")
            
            return {
                "success": True,
                "message": "Category mapping initiated"
            }
            
        except Exception as e:
            logger.error(f"Category mapping error: {e}")
            return {"success": False, "error": str(e)}
    
    async def _redirect_to_support(self, user_phone: str, error_reason: str,
                                 error_details: str = "") -> Dict[str, Any]:
        """Redirect user to support with error context."""
        try:
            support_message = "Our support team will contact you shortly."
            await self.whatsapp_service.send_message(user_phone, support_message)
            
            logger.error(f"Registration support redirect for {user_phone}: {error_reason} - {error_details}")
            
            return {
                "status": "redirected_to_support",
                "error_reason": error_reason,
                "error_details": error_details,
                "message": "User redirected to support"
            }
            
        except Exception as e:
            logger.error(f"Support redirect error: {e}")
            return {"status": "error", "error": "Failed to redirect to support"}