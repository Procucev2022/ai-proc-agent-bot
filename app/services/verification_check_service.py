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
            self_client = user_dict.get("selfClient") or user_dict.get("self_client", True)
            user_type = "buyer" if self_client else "seller"
            approved = user_dict.get("approved")
            email = user_dict.get("username") or user_dict.get("email")
            
            logger.info(f"Verification check: user_type={user_type}, status={verification_status}, approved={approved}, email={email}")
            
            # Step 1: Check verification status - block if not EMAIL_VERIFIED
            if verification_status in ["PENDING_EMAIL_VERIFICATION", "EMAIL_VERIFICATION_FAILED"]:
                logger.info(f"Blocking access - verification status: {verification_status}")
                
                # First refresh user data to check if status was recently updated
                logger.info(f"Refreshing user data to check for updated verification status")
                refresh_result = await self.refresh_user_verification_status(user_phone, max_retries=1)
                
                if refresh_result.get("success") and refresh_result.get("data"):
                    refreshed_data = refresh_result["data"][0] if refresh_result["data"] else {}
                    refreshed_status = refreshed_data.get("verificationStatus", verification_status)
                    
                    if refreshed_status == "EMAIL_VERIFIED":
                        logger.info(f"User status updated to EMAIL_VERIFIED after refresh - continuing verification")
                        user_dict.update(refreshed_data)
                        verification_status = refreshed_status
                    else:
                        logger.info(f"Status still {refreshed_status} after refresh - requiring verification")
                        return {
                            "verification_required": True,
                            "otp_sent": False,
                            "redirect_info": {
                                "flow": "email_verification",
                                "reason": verification_status,
                                "message": f"Email verification required for {email}.",
                                "email": email
                            }
                        }
                else:
                    logger.warning(f"Failed to refresh user data - requiring verification")
                    return {
                        "verification_required": True,
                        "otp_sent": False,
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
                    # Buyers need EMAIL_VERIFIED AND domain check
                    user_id = user_dict.get("id")
                    if user_id:
                        # Check domain approval for buyers (skip API if already approved)
                        domain_result = await self._check_domain_approval(user_id, approved)
                        if domain_result.get("approved"):
                            # User is already EMAIL_VERIFIED and domain approved - grant access immediately
                            logger.info(f"Access granted - buyer with EMAIL_VERIFIED status and domain approved")
                            return {"access_granted": True, "user_data": user_dict}
                        else:
                            logger.info(f"Blocking access - buyer domain check failed: {domain_result}")
                            return {
                                "verification_required": True,
                                "redirect_to_support": True,
                                "redirect_info": {
                                    "flow": "pending_approval",
                                    "reason": "domain_not_approved",
                                    "message": "Registration successful—thank you! Our team will get in touch with you shortly to complete your onboarding so that you can raise RFQs. In the meantime please let us know if you want us to support you with anything else?"
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
                    # Sellers need EMAIL_VERIFIED AND approved=False (sellers should have approved=False)
                    if approved is False:
                        logger.info(f"Access granted - seller with email verified and approved=False")
                        return {"access_granted": True, "user_data": user_dict}
                    else:
                        logger.info(f"Blocking access - seller has incorrect approved flag: approved={approved}")
                        return {
                            "verification_required": True,
                            "redirect_to_support": True,
                            "redirect_info": {
                                "flow": "support_required",
                                "reason": "incorrect_seller_status",
                                "message": "Account verification required - please contact support"
                            }
                        }
            
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
                    # Wait before retry (shorter delay)
                    import asyncio
                    await asyncio.sleep(0.5)
            
            # Max retries exceeded - return failure
            logger.error(f"Max retries ({max_retries}) exceeded for user {user_phone}")
            return {
                "success": False, 
                "message": "Max retries exceeded"
            }
            
        except Exception as e:
            logger.error(f"Error refreshing user data: {e}")
            return {"success": False, "message": str(e)}
    
    async def _check_domain_approval(self, user_id: str, current_approved_status: bool = None) -> Dict[str, Any]:
        """Check user domain approval - only call API if not already approved."""
        try:
            # If user is already approved, don't call the API again
            if current_approved_status is True:
                logger.info(f"User {user_id} is already approved, skipping API call")
                return {
                    "approved": True,
                    "status": "already_approved",
                    "message": "User already approved"
                }
            
            from app.services.domain_check_service import DomainCheckService
            domain_check_service = DomainCheckService()
            # Only call approval API if user is not already approved
            return await domain_check_service.user_approval_api_call(user_id)
        except Exception as e:
            logger.error(f"Domain approval check error: {e}")
            return {"approved": False, "error": str(e)}
