"""
Authentication API Service.

This service handles user authentication, and related operations
for both buyers and sellers in the system, integrating with the GMT Procucev backend.
"""

import logging
from typing import Dict, Any
from datetime import datetime

from app.procucev_apis.procucev_api_client import ProcucevAPIClient

logger = logging.getLogger(__name__)

class AuthAPIService:
    """
    Service for handling authentication operations.
    Provides methods for user authentication and approval status management for both buyers and sellers.
    """

    def __init__(self):
        self.api_client = ProcucevAPIClient()

    
    async def authenticate_user(self, phone_number: str) -> Dict[str, Any]:
        """Get User Details by phone number both used same API."""
        try:
            # Call the API to get user details by phone number
            
            endpoint = f"/partialvendor/getUsersByPhoneNumber/{phone_number}"
            response_data = await self.api_client.get(endpoint)

            # Check if the response indicates success
            if response_data.get("success") and isinstance(response_data.get("data"), list) and len(response_data["data"]) > 0:
                user_data = response_data["data"][0]  # Get first user from the list
                return {
                    "success": True,
                    "user_data": {
                        "id": str(user_data.get("id")),
                        "username": user_data.get("username", ""),
                        "fullName": user_data.get("fullName", ""),
                        "selfClient": user_data.get("selfClient", False),
                        "role": user_data.get("role", ""),
                        "is_registered": True,  # Explicitly set is_registered to True for authenticated users
                        "phone": user_data.get("phone"),
                        "companyName": user_data.get("companyName"),
                        "orgId": user_data.get("orgId"),
                        "active": user_data.get("active", False),
                        "approved": user_data.get("approved", False)
                    },
                    "response": response_data["data"],
                }
            elif response_data.get("status_code") == 404:
                logger.warning(f"API endpoint not found (404) for phone number {phone_number}")
                return {
                    "success": False,
                    "error": "User authentication service unavailable",
                    "status_code": 404,
                    "is_registered": False
                }
            else:
                logger.warning("No user found with given phone number")
                return {
                    "success": False,
                    "error": "User not found",
                    "is_registered": False
                }
        except Exception as e:
            logger.error(f"Error fetching user details: {e}")
            return {"success": False, "error": str(e)}