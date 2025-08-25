"""
Authentication API Service.

This service handles user authentication, and related operations
for both buyers and sellers in the system, integrating with the GMT Procucev backend.
"""

import logging
from typing import Dict, Any
from datetime import datetime

from app.procucev_apis.procucev_api_client import ProcucevAPIClient
from app.schemas.user import APIUserSchema 

logger = logging.getLogger(__name__)

class AuthAPIService:
    """
    Service for handling authentication operations.
    Provides methods for user authentication and approval status management for both buyers and sellers.
    """

    def __init__(self):
        self.api_client = ProcucevAPIClient()

    
    async def authenticate_user(self, phone_number: str) -> Dict[str, Any]:
        """Get User Details by phone number, normalized into APIUserSchema list."""
        try:
            endpoint = f"/partialvendor/getUsersByPhoneNumber/{phone_number}"
            response_data = await self.api_client.get(endpoint)

            if response_data.get("success") and isinstance(response_data.get("data"), list):
                raw_users = response_data["data"]

                if not raw_users:
                    return {"success": False, "error": "User not found", "is_registered": False}

                # Normalize using schema
                users: List[APIUserSchema] = [APIUserSchema(**user) for user in raw_users]

                return {
                    "success": True,
                    "users": [u.dict() for u in users],  # Return clean list of dicts
                    "count": len(users),
                    "is_registered": True,
                }

            elif response_data.get("status_code") == 404:
                logger.warning(f"API endpoint not found (404) for phone number {phone_number}")
                return {
                    "success": False,
                    "error": "User authentication service unavailable",
                    "status_code": 404,
                    "is_registered": False,
                }

            else:
                logger.warning(f"No user found for phone number {phone_number}")
                return {"success": False, "error": "User not found", "is_registered": False}

        except Exception as e:
            logger.error(f"Error fetching user details: {e}")
            return {"success": False, "error": str(e)}