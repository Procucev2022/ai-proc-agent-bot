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
        Handles API responses with statusCode: 200 (success), 204 (no user found), 500 (error)
        """
        try:
            endpoint = f"/partialvendor/getUsersByPhoneNumber/{phone_number}"
            response_data = await self.api_client.get(endpoint)

            status_code = response_data.get("statusCode")
            
            if status_code == "200":
                users_data = response_data.get("data", {}).get("users", [])
                users = [APIUserSchema(**user) for user in users_data]
                
                return {
                    "success": True,
                    "message": response_data.get("message", "Users fetched successfully"),
                    "status_code": 200,
                    "data": [u.dict() for u in users],
                }
            
            elif status_code == "204":
                return {
                    "success": False,
                    "message": response_data.get("message", f"No user found with phone number: {phone_number}"),
                    "status_code": 204,
                }
            else : # status_code == "500" or any other unexpected status code
                return {
                    "success": False,
                    "message": response_data.get("message", "Error fetching user details"),
                    "status_code": 500,
                }
        except Exception as e:
            logger.exception(f"Exception during user authentication for {phone_number}: {e}")
            return {
                "success": False,
                "message": "Internal server error while fetching user details",
                "status_code": 500,
                "error": str(e),
            }
