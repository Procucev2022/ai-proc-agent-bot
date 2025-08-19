"""
Authentication service for user verification 

This service handles user authentication and user type detection
for both buyers and sellers. It integrates with external APIs to verify user credentials
and determine user roles.
"""

import logging
from datetime import datetime
from typing import Dict, Optional
from app.procucev_apis.auth_apis import AuthAPIService
from app.redis_db import get_auth_redis_service
from app.schemas.user import UserDetailsSchema

logger = logging.getLogger(__name__)

class AuthenticationService:
    """
    Service for handling user authentication and session management.
    """
    def __init__(self):
        self.auth_api_service = AuthAPIService()
        self.auth_redis_service = get_auth_redis_service()

    async def validate_token(self, phone_number: str) -> UserDetailsSchema:
        try:
            user_data = await self.auth_redis_service.retrieve(phone_number)
            if user_data:
                # user_data is already a UserDetailsSchema
                return user_data
            return None
        except Exception as e:
            logger.error(f"Authentication error for {phone_number}: {e}")
            return None




    async def authenticate_user(self, phone_number: str) -> Dict:
        """
        Call external API to authenticate a user by phone number.
        """
        try:
            logger.info(f"Authenticating user {phone_number}")
            auth_response = await self.auth_api_service.authenticate_user(phone_number)

            if auth_response.get("success"):
                user_details = self._extract_user_details(auth_response.get("response", []))
                if user_details:
                    logger.info(f"User {phone_number} authenticated successfully.")
                    return {"success": True, "user_details": user_details, "raw_response": auth_response.get("response")}
                return {"success": False, "message": "No valid user details found"}

            return auth_response

        except Exception as e:
            logger.error(f"Authentication error for {phone_number}: {e}")
            return {"success": False, "message": str(e)}

    async def store_user_session(self, phone_number: str, user_details: UserDetailsSchema) -> bool:
        """
        Store user session data in Redis.
        """
        try:
            session_data = {**user_details.dict(), "authenticated_at": datetime.now().isoformat()}
            await self.auth_redis_service.store(phone_number, session_data)
            logger.info(f"Session stored for user {phone_number} (ID: {user_details.id})")
            return True
        except Exception as e:
            logger.error(f"Error storing session for {phone_number}: {e}")
            return False

    def _extract_user_details(self, api_response: list) -> Optional[UserDetailsSchema]:
        """
        Parse external API response and map into UserDetailsSchema.
        """
        try:
            if not api_response:
                return None
            user_data = api_response[0]
            return UserDetailsSchema.from_api_response(user_data)
        except Exception as e:
            logger.error(f"Error extracting user details: {e}")
            return None
