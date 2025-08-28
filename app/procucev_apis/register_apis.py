"""
Authentication and Registration API Service.

This service handles user authentication, registration, and related operations
for both buyers and sellers in the system, integrating with the GMT Procucev backend.
"""

import logging
from typing import Dict, Any
from datetime import datetime

from app.procucev_apis.procucev_api_client import ProcucevAPIClient

logger = logging.getLogger(__name__)

class RegisterAPIService:
    """
    Service for handling authentication and registration operations.
    
    Provides methods for user authentication, registration, OTP verification,
    and approval status management for both buyers and sellers.
    """
    
    def __init__(self):
        self.api_client = ProcucevAPIClient()
        
    async def register_seller(self, seller_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Register a new seller with the GMT Procucev system.
    
        """
        try:
            seller_url = "/partialvendor/sellerRegistration"
            # Prepare request payload
            payload = {
                "companyName": seller_data.get("companyName", ""),
                "organizationPhonenumber": seller_data.get("organizationPhonenumber", ""),
                "email": seller_data.get("email", ""),
                "pan": seller_data.get("pan", ""),
                "gstin": seller_data.get("gstin", ""),
                "address1": seller_data.get("address1", ""),
                "details": seller_data.get("details", ""),
                "india": seller_data.get("india", "true"),
                "crn": seller_data.get("crn", "")
            }
            
            response = await self.api_client.post(
                endpoint=seller_url,
                json_data=payload
            )
            
            if response["success"]:
                return {
                    "statusCode": "1001",
                    "message": "Thanks for your interest with procucev, our vendor partner will connect with you",
                    "errorMsg": None,
                    "timestamp": response["timestamp"],
                    "status": "Success",
                    "type": None
                }
            else:
                return {
                    "statusCode": response["status_code"],
                    "message": response.get("data", {}).get("message", "Registration failed"),
                    "errorMsg": response.get("data", {}).get("error", None),
                    "timestamp": response["timestamp"],
                    "status": "Failure",
                    "type": None
                }
                        
        except Exception as e:
            logger.error(f"Seller registration error: {e}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "status": "Failure",
                "type": None
            }
            
    async def register_buyer(self, buyer_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Register a new buyer with the GMT Procucev system.
        
        """
        try:
            buyer_url = "/partialvendor/buyerRegistration"
            # Prepare request payload
            payload = {
                "companyName": buyer_data.get("companyName", ""),
                "organizationPhonenumber": buyer_data.get("organizationPhonenumber", ""),
                "email": buyer_data.get("email", ""),
                "name": buyer_data.get("name", ""),
                "zipCode": buyer_data.get("zipCode", ""),
                "details": buyer_data.get("details", ""),
                "whatsApp": buyer_data.get("whatsApp", True)
            }
            
            response = await self.api_client.post(
                endpoint=buyer_url,
                json_data=payload
            )
            
            if response["success"]:
                return {
                    "statusCode": "1001",
                    "message": "Thanks for your interest with procucev, our client partner will connect with you",
                    "errorMsg": None,
                    "timestamp": response["timestamp"],
                    "status": "Success",
                    "type": None
                }
            else:
                return {
                    "statusCode": response["status_code"],
                    "message": response.get("data", {}).get("message", "Registration failed"),
                    "errorMsg": response.get("data", {}).get("error", None),
                    "timestamp": response["timestamp"],
                    "status": "Failure",
                    "type": None
                }
                        
        except Exception as e:
            logger.error(f"Buyer registration error: {e}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "status": "Failure",
                "type": None
            }
     
    async def send_otp(self, username: str, phone_number: str = None) -> Dict[str, Any]:
        """
        Send OTP to the provided username for verification.
        
        """
        try:
            sendOTP_url = "/rest/users/sendOtp"
            payload = {
                "email": username,
                "organizationPhonenumber": phone_number or ""
            }
            
            response = await self.api_client.post(
                endpoint=sendOTP_url,
                json_data=payload ,
                require_auth=True
            )
            
            if response["success"]:
                return {
                    "statusCode": "1001",
                    "message": "OTP sent successfully",
                    "errorMsg": None,
                    "timestamp": response["timestamp"],
                    "status": "Success",
                    "type": None
                }
            else:
                return {
                    "statusCode": response["status_code"],
                    "message": "Failed to send OTP",
                    "errorMsg": response["message"],
                    "timestamp": response["timestamp"],
                    "status": "Failure",
                    "type": None
                }
                        
        except Exception as e:
            logger.error(f"Error sending OTP: {e}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "status": "Failure",
                "type": None
            }
    
    async def validate_otp(self, username: str, otp: str, phone_number: str = None) -> Dict[str, Any]:
        """
        Validate OTP for the provided username.
        
        """
        try:
            validateOTP_url = "/rest/users/validateOtp"
            payload = {
                "email": username,
                "emailOtp": otp,
                "organizationPhonenumber": phone_number or ""
            }
            
            response = await self.api_client.post(
                endpoint=validateOTP_url,
                json_data=payload,
                require_auth=True
            )
            
            if response["success"]:
                return {
                    "statusCode": "1001",
                    "message": "OTP validated successfully",
                    "errorMsg": None,
                    "timestamp": response["timestamp"],
                    "status": "Success",
                    "type": None
                }
            else:
                return {
                    "statusCode": response["status_code"],
                    "message": "Invalid OTP",
                    "errorMsg": response["message"],
                    "timestamp": response["timestamp"],
                    "status": "Failure",
                    "type": None
                }
                        
        except Exception as e:
            logger.error(f"Error validating OTP: {e}")
            return {
                "statusCode": "500",
                "message": "Internal server error",
                "errorMsg": str(e),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "status": "Failure",
                "type": None
            }

    async def user_approval(self, user_id: str) -> Dict[str, Any]:
        try:
            user_approval_url = "/rest/gmt/acceptSelfRegisterClient"
            payload = {
                "id": user_id
            }
            
            response = await self.api_client.post(
                endpoint=user_approval_url,
                json_data=payload,
                require_auth=True
            )
            
            if response["success"]:
                return {
                    "statusCode": "1001",
                    "message": "User approved successfully",
                    "errorMsg": None,
                    "timestamp": response["timestamp"],
                    "status": "Success",
                    "type": None
                }
            else:
                return {
                    "statusCode": response["status_code"],
                    "message": "Failed to approve user",
                    "errorMsg": response["message"],
                    "timestamp": response["timestamp"],
                    "status": "Failure",
                    "type": None
                }
        
        except Exception as e:
            logger.error(f"Error getting User Approval: {e}")
            return {"success": False, "error": str(e)}