"""
Verification Check Service - Fixed implementation with automatic OTP triggering.

Checks verificationStatus and approved flags to control main flow access.
FIXED: Now automatically triggers OTP when email verification is required.
"""

import logging
from typing import Dict, Any
from app.schemas.user import User
from app.services.support_notification_service import SupportNotificationService

logger = logging.getLogger(__name__)


class VerificationCheckService:
    """Service to check verification status and enforce verification flow.
    
    Implements the verification logic:
    - Blocks access if verificationStatus is PENDING_EMAIL_VERIFICATION or EMAIL_VERIFICATION_FAILED
    - For EMAIL_VERIFIED buyers: requires approved=True for main flow access
    - For EMAIL_VERIFIED sellers: allows main flow access regardless of approved flag
    - FIXED: Automatically triggers OTP when email verification is required
    """
    
    def __init__(self, auth_api_service, otp_service, whatsapp_service):
        self.auth_api_service = auth_api_service
        self.otp_service = otp_service
        self.whatsapp_service = whatsapp_service
    
    async def check_and_enforce_verification(self, user_phone: str, user_data,session) -> Dict[str, Any]:
        """
        Check verification status and enforce verification flow if needed.
        
        Logic:
        - PENDING_EMAIL_VERIFICATION or EMAIL_VERIFICATION_FAILED: Redirect to OTP/email verification
        - EMAIL_VERIFIED + Buyer: Requires approved=True for main flow access
        - EMAIL_VERIFIED + Seller: Can access main flow regardless of approved flag
        - FIXED: Automatically triggers OTP when email verification is required
        
        Returns:
            - {"access_granted": True, "user_data": user_data} if access allowed
            - {"verification_required": True, "redirect_info": {...}} if verification needed
        """
        try:
            # Handle both User objects and dict data
            if isinstance(user_data, dict):
                # Already a dict
                user_dict = user_data
            elif hasattr(user_data, 'dict'):
                # User object - convert to dict
                user_dict = user_data.dict()
            elif hasattr(user_data, '__dict__'):
                # Object with attributes
                user_dict = user_data.__dict__
            else:
                # Try to convert to dict
                try:
                    user_dict = dict(user_data)
                except (TypeError, ValueError):
                    raise ValueError(f"Cannot convert user_data to dict: {type(user_data)}")
            
            # Extract verification data
            verification_status = user_dict.get("verificationStatus") or user_dict.get("verification_status") or "PENDING_EMAIL_VERIFICATION"
            # CRITICAL FIX: Handle selfClient=False correctly (sellers)
            self_client = user_dict.get("selfClient")
            if self_client is None:
                self_client = user_dict.get("self_client", True)
            user_type = "buyer" if self_client else "seller"
            approved = user_dict.get("approved")
            email = user_dict.get("username") or user_dict.get("email")
            
            logger.info(f"Verification check: user_type={user_type}, status={verification_status}, approved={approved}, email={email}")
            
            # CRITICAL FIX: Domain matching should ONLY apply to buyers, not sellers
            # Sellers should be allowed access with EMAIL_VERIFIED status regardless of approved flag
            
            # Step 1: Check verification status - block if not EMAIL_VERIFIED
            if verification_status in ["PENDING_EMAIL_VERIFICATION", "EMAIL_VERIFICATION_FAILED"]:
                logger.info(f"Blocking access - verification status: {verification_status}")

                # First refresh user data to check if status was recently updated
                # logger.info(f"Refreshing user data to check for updated verification status")
                # refresh_result = await self.refresh_user_verification_status(user_phone, max_retries=1)
                logger.warning(f"Failed to refresh user data - requiring verification")
                # FIXED: Automatically send OTP when email verification is required
                print("user", user_phone,"email",email)
                otp_result = await self._send_verification_otp(user_phone, email,session)
                return {
                    "verification_required": True,
                    "otp_sent": otp_result.get("status") == "otp_sent",
                    "redirect_info": {
                        "flow": "email_verification",
                        "reason": verification_status,
                        "message": f"Email verification required for {email}.",
                        "email": email
                    }
                }
            
            # Step 2: If EMAIL_VERIFIED, apply user type specific rules
            if verification_status == "EMAIL_VERIFIED":
                if user_type == "buyer":
                    # Buyers need EMAIL_VERIFIED AND approved=True
                    if approved is True:
                        # User is already approved - grant access immediately
                        logger.info(f"Access granted - buyer with EMAIL_VERIFIED status and approved=True")
                        return {"access_granted": True, "user_data": user_dict}
                    else:
                        # Missing user_id - show pending message
                        logger.info(f"Blocking access - buyer missing user_id")
                        return {
                            "verification_required": True,
                            "redirect_to_support": True,
                            "redirect_info": {
                                "flow": "pending_approval",
                                "reason": "missing_user_id",
                                "message": (
                                    "*Registration under review — thank you!*\n\n"
                                    "Your registration details are being verified to ensure a smooth onboarding experience. "
                                    "Our team will reach out shortly to complete the setup. Once verified, you’ll be ready to access your account and raise RFQs.\n\n"
                                    "Feel free to return to this chat anytime to continue your journey with *Procucev* — simply type *“Hi”* to start the conversation again."
                                )

                            }
                        }
                else:
                    # Sellers with EMAIL_VERIFIED status - still require OTP for authentication
                    logger.info(f"Seller with EMAIL_VERIFIED status - sending OTP for authentication")
                    otp_result = await self._send_verification_otp(user_phone, email,session=session, is_daily_verification= True)
                    return {
                        "verification_required": True,
                        "otp_sent": otp_result.get("status") == "otp_sent",
                        "redirect_info": {
                            "flow": "email_verification",
                            "reason": "seller_authentication",
                            "message": f"Please verify your email ({email}) to continue with seller menu.",
                            "email": email
                        }
                    }
            
            # Step 3: Unknown/invalid verification status - require verification
            logger.warning(f"Unknown verification status: {verification_status} - requiring verification")
            # FIXED: Automatically send OTP for unknown verification status
            otp_result = await self._send_verification_otp(user_phone, email or 'your email',session)
            return {
                "verification_required": True,
                "otp_sent": otp_result.get("status") == "otp_sent",
                "redirect_info": {
                    "flow": "email_verification",
                    "reason": "unknown_status",
                    "message": f"{user_dict.get('fullName', 'Hi')}, please verify your email ({email or 'your email'}) to complete your registration process."
                }
            }
            
        except Exception as e:
            logger.error(f"Verification check error: {e}")
            # CRITICAL: Do not fail open for verification errors - this is a security issue
            # Instead, require verification to be safe
            
            # Try to extract email for error message
            try:
                if isinstance(user_data, dict):
                    user_dict = user_data
                elif hasattr(user_data, 'dict'):
                    user_dict = user_data.dict()
                elif hasattr(user_data, '__dict__'):
                    user_dict = user_data.__dict__
                else:
                    user_dict = {}
                    
                email = user_dict.get('username') or user_dict.get('email', 'your email')
                full_name = user_dict.get('fullName', 'Hi')
            except:
                email = 'your email'
                full_name = 'Hi'
            
            # FIXED: Automatically send OTP for verification check errors
            otp_result = await self._send_verification_otp(user_phone, email,session)
            return {
                "verification_required": True,
                "otp_sent": otp_result.get("status") == "otp_sent",
                "redirect_info": {
                    "flow": "email_verification",
                    "reason": "verification_check_error",
                    "message": f"{full_name}, please verify your email ({email}) to complete your registration process."
                }
            }
    
    async def refresh_user_verification_status(self, user_phone: str, max_retries: int = 1) -> Dict[str, Any]:
        """
        Refresh user data from API to get updated verification status.
        
        Implements retry logic with max retry limit. If max retries exceeded,
        redirects to support and exits the flow.
        """
        try:
            retry_count = 0
            while retry_count < max_retries:
                auth_response = await self.auth_api_service.authenticate_user(user_phone)
                if auth_response.get("success"):
                    logger.info(f"Successfully refreshed user data for {user_phone} on attempt {retry_count + 1}")
                    return {"success": True, "data": auth_response.get("data", [])}
                
                retry_count += 1
                logger.warning(f"Failed to refresh user data for {user_phone}, attempt {retry_count}/{max_retries}")
                
                if retry_count < max_retries:
                    # Wait before retry with increasing delay
                    import asyncio
                    await asyncio.sleep(0.5 * retry_count)
            
            # Max retries exceeded - return failure
            logger.error(f"Max retries ({max_retries}) exceeded for user {user_phone}")
            return {
                "success": False, 
                "message": "Max retries exceeded"
            }
            
        except Exception as e:
            logger.error(f"Error refreshing user data: {e}")
            return {"success": False, "message": str(e)}
    
    async def _send_verification_otp(self, user_phone: str, email: str,is_daily_verification: bool = False,session=None) -> Dict[str, Any]:
        """Send OTP for email verification."""
        try:
            if not email or email == 'your email':
                logger.warning(f"Cannot send OTP - invalid email: {email}")
                return {"status": "otp_send_failed", "reason": "invalid_email"}
            
            logger.info(f"Sending verification OTP to {email} for user {user_phone}")
            # Create a minimal session for OTP sending
            from app.models import ConversationSession
            temp_session = ConversationSession()
            temp_session.workflow_state = {}
            
            otp_result = await self.otp_service.send_otp(user_phone, email,session,is_daily_verification)
            logger.info(f"OTP send result: {otp_result}")
            return otp_result
            
        except Exception as e:
            logger.error(f"Error sending verification OTP: {e}")
            return {"status": "otp_send_failed", "reason": str(e)}
    
    async def _check_domain_approval(self, user_id: str, current_approved_status: bool = None, user_data: Dict = None) -> Dict[str, Any]:
        """Check user domain approval using AI-based domain matching first, then call API only if AI approves."""
        try:
            # If user is already approved, don't call the API again
            if current_approved_status is True:
                logger.info(f"User {user_id} is already approved, skipping domain check")
                return {
                    "approved": True,
                    "status": "already_approved",
                    "message": "User already approved"
                }
            
            from app.services.domain_check_service import DomainCheckService
            domain_check_service = DomainCheckService()
            
            # Extract email and company name for AI domain matching
            if user_data:
                email = user_data.get("username") or user_data.get("email")
                company_name = user_data.get("companyName") or user_data.get("company_name")

                logger.info(f"email:{email}, company_name:{company_name}")
                
                if email and company_name:
                    logger.info(f"Performing AI domain matching for user {user_id}: email={email}, company={company_name}")
                    
                    # Step 1: AI-based domain matching
                    domain_check_result = await domain_check_service.check_domain_match(email, company_name)
                    logger.info(f"AI domain check result: {domain_check_result}")
                    
                    # Step 2: Only call API if AI says approved=True and method=ai
                    if domain_check_result.get("approved") and domain_check_result.get("method") == "ai":
                        logger.info(f"AI approved domain match, calling /rest/gmt/acceptSelfRegisterClient API")
                        api_result = await domain_check_service.user_approval_api_call(user_id)
                        return api_result
                    else:
                        logger.info(f"AI rejected domain match or used fallback method, not calling API")
                        return {
                            "approved": False,
                            "status": "ai_domain_rejected",
                            "message": f"Domain matching failed: {domain_check_result.get('reasoning', 'AI domain check failed')}",
                            "domain_check_result": domain_check_result
                        }
                else:
                    logger.warning(f"Missing email or company name for domain check: email={email}, company={company_name}")
                    return {
                        "approved": False,
                        "status": "missing_domain_data",
                        "message": "Missing email or company name for domain verification"
                    }
            else:
                logger.warning(f"No user data provided for domain check")
                return {
                    "approved": False,
                    "status": "missing_user_data",
                    "message": "User data required for domain verification"
                }
            
        except Exception as e:
            logger.error(f"Domain approval check error: {e}")
            return {"approved": False, "error": str(e)}
