"""
Authentication API Service.

This service handles user authentication, and related operations
for both buyers and sellers in the system, integrating with the GMT Procucev backend.
"""

import logging
from typing import Dict, Any, List
from datetime import datetime

from app.procucev_apis.procucev_api_client import ProcucevAPIClient
from app.schemas.user import APIUserSchema
from app.utils.procucev_api_logger import log_procucev_api_call

logger = logging.getLogger(__name__)

class AuthAPIService:
    """
    Service for handling authentication operations.
    Provides methods for user authentication and approval status management for both buyers and sellers.
    """

    def __init__(self):
        self.api_client = ProcucevAPIClient()

    
    @log_procucev_api_call("authenticate_user")
    async def authenticate_user(self, phone_number: str) -> Dict[str, Any]:
        """
        Authenticate user by phone number.
        Handles both scenarios:
          - API returns { "status": "failure", "message": "..."} when no user is found
          - API returns a list of user dicts when user(s) are found
        """

        try:
            endpoint = f"/partialvendor/getUsersByPhoneNumber/{phone_number}"
            response_data = await self.api_client.get(endpoint)

            # Case 1: API explicitly returns failure response
            if isinstance(response_data, dict) and response_data.get("status") == "failure":
                logger.info(f"No user found for phone number {phone_number}")
                return {
                    "success": False,
                    "message": response_data.get("message", f"No user found with phone number: {phone_number}"),
                    "status_code": 404,
                    "is_registered": False,
                }

            # Case 2: API returns a list of users (successful lookup)
            elif isinstance(response_data, list):
                if not response_data:
                    logger.info(f"No user found for phone number {phone_number}")
                    return {
                        "success": False,
                        "message": f"No user found with phone number: {phone_number}",
                        "status_code": 404,
                        "is_registered": False,
                    }

                # Normalize response
                users: List[APIUserSchema] = [APIUserSchema(**user) for user in response_data]

                return {
                    "success": True,
                    "message": "User(s) retrieved successfully",
                    "status_code": 200,
                    "data": [u.dict() for u in users],
                    "count": len(users),
                    "is_registered": True,
                }

            # Unexpected format
            else:
                logger.warning(f"Unexpected response format for {phone_number}: {response_data}")
                return {
                    "success": False,
                    "message": "Unexpected response format from authentication service",
                    "status_code": 502,
                    "is_registered": False,
                }

        except Exception as e:
            logger.exception(f"Exception during user authentication for {phone_number}: {e}")
            return {
                "success": False,
                "message": "Internal server error while fetching user details",
                "status_code": 500,
                "error": str(e),
                "is_registered": False,
            }
