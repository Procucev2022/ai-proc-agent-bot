"""
Authentication Service for WhatsApp Bot.

Handles complete user authentication flow including:
- Token validation
- User lookup via API
- Intent clarification (Buy/Sell)
- Session state management
- Integration with registration flow
"""

import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from app.models import User, ConversationSession, UserType
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.utils.datetime_utils import utc_now
from app.redis_db import get_auth_redis_service
from app.schemas.user import UserDetailsSchema
from app.procucev_apis.auth_apis import AuthAPIService

logger = logging.getLogger(__name__)


class AuthenticationService:
    """Handles user authentication and intent clarification."""
    
    def __init__(self, whatsapp_service: WhatsAppService = None, 
                 openai_service: OpenAIService = None,
                 response_helpers: ResponseHelpers = None):
        self.whatsapp_service = whatsapp_service or WhatsAppService()
        self.openai_service = openai_service or OpenAIService()
        self.response_helpers = response_helpers or ResponseHelpers(self.openai_service)
        self.auth_redis_service = get_auth_redis_service()
        self.auth_api_service = AuthAPIService()
    
    async def validate_token(self, user_phone: str) -> Optional[UserDetailsSchema]:
        """
        Validate user token from Redis auth storage.
        
        Returns UserDetailsSchema if valid token exists, None otherwise.
        """
        try:
            user_data = await self.auth_redis_service.retrieve(user_phone)
            if user_data:
                # user_data is already a UserDetailsSchema
                return user_data
            return None
        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return None
    
    async def store_user_session(self, user_phone: str, user_details: UserDetailsSchema) -> bool:
        """
        Store user session data in Redis.
        """
        try:
            session_data = user_details.dict()
            session_data["authenticated_at"] = datetime.now().isoformat()
            await self.auth_redis_service.store(user_phone, session_data)
            logger.info(f"Session stored for user {user_phone} (ID: {user_details.id})")
            return True
        except Exception as e:
            logger.error(f"Session storage error for {user_phone}: {e}")
            return False
    
    async def user_authenticate(self, user_phone: str, message: str, 
                                            session: ConversationSession) -> Dict[str, Any]:
        """       
        Handles token validation failure and routes to user authentication flow.
        """
        try:
            logger.info(f"Authenticating user {user_phone}")
            auth_response = await self.auth_api_service.authenticate_user(user_phone)

            if auth_response.get("success"):
                user_details = self._extract_user_details(auth_response.get("response", []))
                if user_details:
                    logger.info(f"User {user_phone} authenticated successfully.")
                    logger.info(f"User details: {user_details}")
                    
                    await self.store_user_session(user_phone, user_details)
                    logger.info(f"Session stored for user {user_phone} ")
                    return {"success": True, "user_details": user_details, "raw_response": auth_response.get("response")}
                return {"success": False, "message": "No valid user details found"}

            return auth_response

        except Exception as e:
            logger.error(f"Authentication error for {user_phone}: {e}")
            return {"success": False, "message": str(e)}

    def _extract_user_details(self, api_response: list) -> Optional[UserDetailsSchema]:
        """
        Parse external API response and map into UserDetailsSchema.
        Filter for selfClient=true users first.
        """
        try:
            if not api_response:
                return None
            
            # Filter for selfClient=true users (buyers)
            self_client_users = [user for user in api_response if user.get("selfClient") == True]
            
            if self_client_users:
                user_data = self_client_users[0]  # Take first selfClient user
            else:
                user_data = api_response[0]  # Fallback to first user
            
            return UserDetailsSchema(
                id=user_data.get("id", ""),
                username=user_data.get("username", ""),
                name=user_data.get("fullName", ""),
                phone_number=user_data.get("phone", ""),
                self_client=user_data.get("selfClient", False),
                role="buyer" if user_data.get("selfClient") else "seller",
                is_registered=True,
                company_name=user_data.get("companyName", ""),
                approved=user_data.get("approved", False)
            )
        except Exception as e:
            logger.error(f"Error extracting user details: {e}")
            return None
    
    