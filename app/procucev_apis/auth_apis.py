"""
Authentication API Service.

This service handles user authentication, and related operations
for both buyers and sellers in the system, integrating with the GMT Procucev backend.
"""

import logging
from typing import Dict, Any, List
from datetime import datetime

from app.procucev_apis.procucev_api_client import get_procucev_api_client
from app.schemas.user import APIUserSchema

logger = logging.getLogger(__name__)

class AuthAPIService:
    """
    Service for handling authentication operations.
    Provides methods for user authentication and approval status management for both buyers and sellers.
    """

    def __init__(self):
        self.api_client = get_procucev_api_client()

    
    async def authenticate_user(self, phone_number: str) -> Dict[str, Any]:
        """
        Authenticate user by phone number.
        Handles API responses with statusCode: 200 (success), 204 (no user found), 500 (error)
        """
        try:
            logger.info(f"Making API call to authenticate user: {phone_number}")
            endpoint = f"/partialvendor/getUsersByPhoneNumber/{phone_number}"
            logger.info(f"API endpoint: {endpoint}")
            response_data = await self.api_client.get(endpoint, api_title="authenticate_user")
            logger.debug(f"Raw API response for phone {phone_number}: {response_data}")

            # Check if response has success field (new format)
            if response_data.get("success") and isinstance(response_data.get("data"), dict):
                raw_users = response_data["data"].get("users", [])
                logger.debug(f"Found {len(raw_users)} users in new format response for {phone_number}")

                if not raw_users:
                    logger.debug(f"No users found in new format response for {phone_number}")
                    return {"success": False, "error": "User not found", "is_registered": False}

                # Normalize using schema
                users: List[APIUserSchema] = [APIUserSchema(**user) for user in raw_users]
                logger.debug(f"Successfully normalized {len(users)} users for {phone_number}")

                return {
                    "success": True,
                    "message": response_data.get("message", "Users fetched successfully"),
                    "status_code": 200,
                    "data": [u.dict() for u in users],
                }
            
            # Handle legacy format with statusCode
            status_code = response_data.get("statusCode")
            status = response_data.get("status")
            
            if status_code == "200" and status == "Success":
                users_data = response_data.get("data", {}).get("users", [])
                logger.debug(f"Found {len(users_data)} users in legacy format response for {phone_number}")
                
                if not users_data:
                    logger.debug(f"No users found in legacy format response for {phone_number}")
                    return {"success": False, "error": "User not found", "is_registered": False}
                    
                users = [APIUserSchema(**user) for user in users_data]
                logger.debug(f"Successfully normalized {len(users)} users from legacy format for {phone_number}")
                
                return {
                    "success": True,
                    "message": response_data.get("message", "Users fetched successfully"),
                    "status_code": 200,
                    "data": [u.dict() for u in users],
                }
            
            elif status_code == "204":
                logger.debug(f"API returned 204 (No Content) for {phone_number}")
                return {
                    "success": False,
                    "message": response_data.get("message", f"No user found with phone number: {phone_number}"),
                    "status_code": 204,
                }
            else: # status_code == "500" or any other unexpected status code
                logger.warning(f"API returned unexpected status {status_code} for {phone_number}: {response_data}")
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
