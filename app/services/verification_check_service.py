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
    
    async def check_and_enforce_verification(self, user_phone: str, user_data) -> Dict[str, Any]:
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
            if hasattr(user_data, 'dict'):
                # User object - convert to dict
                user_dict = user_data.dict()
            elif hasattr(user_data, '__dict__'):
                # Object with attributes
                user_dict = user_data.__dict__
            else:
                # Already a dict
                user_dict = user_data
            
            # Extract verification data
            verification_status = user_dict.get("verificationStatus") or user_dict.get("verification_status") or "PENDING_EMAIL_VERIFICATION"
            self_client = user_dict.get("selfClient")
            if self_client is None:
                self_client = user_dict.get("self_client", True)
            user_type = "buyer" if self_client else "seller"
            approved = user_dict.get("approved")
            email = user_dict.get("username") or user_dict.get("email")
            
            logger.info(f"Verification check: user_type={user_type}, status={verification_status}, approved={approved}, email={email}")
            
            # Step 1: Check verification status - block if not EMAIL_VERIFIED
            if verification_status in ["PENDING_EMAIL_VERIFICATION", "EMAIL_VERIFICATION_FAILED"]:
                logger.info(f"Blocking access - verification status: {verification_status}")
                
                # FIXED: Automatically trigger OTP sending when email verification is required
                if email:
                    logger.info(f"Automatically triggering OTP for email verification: {email}")
                    
                    # Create a mock session for OTP service
                    class MockSession:
                        def __init__(self):
                            self.workflow_state = {}
                    
                    mock_session = MockSession()
                    
                    try:
                        # Send OTP automatically (OTP service handles the WhatsApp message)
                        otp_result = await self.otp_service.send_otp(user_phone, email, mock_session)
                        
                        if otp_result.get("status") == "otp_sent":
                            logger.info(f"OTP sent successfully to {email}")
                            return {
                                "verification_required": True,
                                "otp_sent": True,
                                "redirect_info": {
                                    "flow": "email_verification",
                                    "reason": verification_status,
                                    "email": email
                                }
                            }
                        else:
                            logger.error(f"Failed to send OTP: {otp_result}")
                            # Import support notification service for OTP failures
                            
                            support_service = SupportNotificationService()
                            await support_service.notify_otp_validation_failure(email)
                            
                            return {
                                "verification_required": True,
                                "otp_sent": False,
                                "redirect_info": {
                                    "flow": "email_verification",
                                    "reason": verification_status,
                                    "message": f"Email verification required for {email}. Please contact support if you don't receive the OTP.",
                                    "email": email
                                }
                            }
                    except Exception as e:
                        logger.error(f"Error sending OTP: {e}")
                        return {
                            "verification_required": True,
                            "otp_sent": False,
                            "redirect_info": {
                                "flow": "email_verification",
                                "reason": verification_status,
                                "message": f"Email verification required. There was an issue sending the OTP. Please contact support.",
                                "email": email
                            }
                        }
                else:
                    logger.warning(f"No email found for user - cannot send OTP")
                    return {
                        "verification_required": True,
                        "otp_sent": False,
                        "redirect_info": {
                            "flow": "email_verification",
                            "reason": verification_status,
                            "message": "Email verification required but no email address found. Please contact support."
                        }
                    }
            
            # Step 2: If EMAIL_VERIFIED, apply user type specific rules
            if verification_status == "EMAIL_VERIFIED":
                if user_type == "buyer":
                    # Buyers need EMAIL_VERIFIED AND approved=True (which gets set after domain check)
                    approved = user_dict.get("approved")
                    
                    if approved is True:
                        logger.info(f"Access granted - buyer with email verified and approved=True")
                        return {"access_granted": True, "user_data": user_dict}
                    else:
                        # Check if domain check is needed (approved=False could mean domain check not done yet)
                        user_id = user_dict.get("id")
                        if user_id:
                            logger.info(f"Buyer approved=False, checking domain approval for user {user_id}")
                            domain_result = await self._check_domain_approval(user_id)
                            
                            if domain_result.get("approved"):
                                # Domain approved - refresh user data to get updated approved flag
                                logger.info(f"Domain approved for buyer {user_id}, refreshing user data")
                                refresh_result = await self.refresh_user_verification_status(user_phone)
                                
                                if refresh_result.get("success"):
                                    fresh_data = refresh_result.get("data", [])
                                    if fresh_data:
                                        fresh_user_data = fresh_data[0] if isinstance(fresh_data, list) else fresh_data
                                        fresh_approved = fresh_user_data.get("approved")
                                        logger.info(f"Fresh user data after domain check - approved: {fresh_approved}")
                                        
                                        if fresh_approved is True:
                                            logger.info(f"Access granted - buyer with domain approved and fresh approved=True")
                                            return {"access_granted": True, "user_data": fresh_user_data}
                                        else:
                                            logger.info(f"Domain approved but approved flag still False, allowing access anyway")
                                            return {"access_granted": True, "user_data": fresh_user_data}
                                
                                # If refresh fails but domain is approved, allow access with current data
                                logger.info(f"Access granted - buyer with domain approved (refresh failed)")
                                return {"access_granted": True, "user_data": user_dict}
                            else:
                                logger.info(f"Blocking access - buyer domain check failed: {domain_result}")
                                return {
                                    "verification_required": True,
                                    "redirect_to_support": True,
                                    "redirect_info": {
                                        "flow": "pending_approval",
                                        "reason": "domain_not_approved",
                                        "message": "Domain verification pending - our team will contact you shortly"
                                    }
                                }
                        else:
                            logger.warning(f"No user ID found for domain check")
                            return {
                                "verification_required": True,
                                "redirect_to_support": True,
                                "redirect_info": {
                                    "flow": "pending_approval",
                                    "reason": "missing_user_id",
                                    "message": "Account verification required - please contact support"
                                }
                            }
                else:
                    # Sellers need EMAIL_VERIFIED (approved flag doesn't matter for sellers)
                    logger.info(f"Access granted - seller with email verified (approved={approved})")
                    return {"access_granted": True, "user_data": user_dict}
            
            # Step 3: Unknown/invalid verification status - require verification
            logger.warning(f"Unknown verification status: {verification_status} - requiring verification")
            return {
                "verification_required": True,
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
                if hasattr(user_data, 'dict'):
                    user_dict = user_data.dict()
                elif hasattr(user_data, '__dict__'):
                    user_dict = user_data.__dict__
                else:
                    user_dict = user_data or {}
                    
                email = user_dict.get('username') or user_dict.get('email', 'your email')
                full_name = user_dict.get('fullName', 'Hi')
            except:
                email = 'your email'
                full_name = 'Hi'
            
            return {
                "verification_required": True,
                "redirect_info": {
                    "flow": "email_verification",
                    "reason": "verification_check_error",
                    "message": f"{full_name}, please verify your email ({email}) to complete your registration process."
                }
            }
    
    async def refresh_user_verification_status(self, user_phone: str, max_retries: int = 3) -> Dict[str, Any]:
        """
        Refresh user data from API to get updated verification status.
        
        Implements retry logic with max retry limit. If max retries exceeded,
        redirects to support and exits the flow.
        """
        try:
            retry_count = 0
            while retry_count < max_retries:
                # Add a small delay before API call to allow backend to process domain check
                if retry_count > 0:
                    import asyncio
                    await asyncio.sleep(2 ** retry_count)
                
                auth_response = await self.auth_api_service.authenticate_user(user_phone)
                if auth_response.get("success"):
                    logger.info(f"Successfully refreshed user data for {user_phone} on attempt {retry_count + 1}")
                    return {"success": True, "data": auth_response.get("data", [])}
                
                retry_count += 1
                logger.warning(f"Failed to refresh user data for {user_phone}, attempt {retry_count}/{max_retries}")
            
            # Max retries exceeded - redirect to support
            logger.error(f"Max retries ({max_retries}) exceeded for user {user_phone} - redirecting to support")
            await self.whatsapp_service.send_message(
                user_phone, 
                "We're experiencing technical difficulties. Please contact our support team for assistance."
            )
            return {
                "success": False, 
                "message": "Max retries exceeded", 
                "redirect_to_support": True,
                "exit_flow": True
            }
            
        except Exception as e:
            logger.error(f"Error refreshing user data: {e}")
            return {"success": False, "message": str(e)}
    
    async def _check_domain_approval(self, user_id: str) -> Dict[str, Any]:
        """Check user domain approval using auth_reg_service."""
        try:
            from app.services.auth_reg_service import AuthRegService
            auth_reg_service = AuthRegService()
            return await auth_reg_service.user_domain_check(user_id)
        except Exception as e:
            logger.error(f"Domain approval check error: {e}")
            return {"approved": False, "error": str(e)}
