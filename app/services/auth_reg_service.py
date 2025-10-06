"""
Shared Authentication and Registration Service.

Contains common functionality used by both authentication and registration services,
including domain check logic for buyers.
"""

import logging
from typing import Dict, Any
from app.procucev_apis.register_apis import RegisterAPIService

logger = logging.getLogger(__name__)


class AuthRegService:
    """Shared service for authentication and registration common operations."""
    
    def __init__(self):
        self.register_api_service = RegisterAPIService()
    
    async def user_domain_check(self, user_id: str) -> Dict[str, Any]:
        """
        Check user domain approval by calling the user approval API.
        
        Args:
            user_id: The user ID to approve
            
        Returns:
            Dict containing approval status and details
        """
        try:
            logger.info(f"Performing domain check for user ID: {user_id}")
            
            # Call the user approval API
            approval_response = await self.register_api_service.user_approval(user_id)
            
            # Check if approval was successful
            if (approval_response.get("statusCode") == "200" and 
                approval_response.get("status") == "Success"):
                
                logger.info(f"Domain check successful for user {user_id}")
                return {
                    "approved": True,
                    "status": "success",
                    "message": approval_response.get("message", "User approved successfully"),
                    "api_response": approval_response
                }
            else:
                logger.warning(f"Domain check failed for user {user_id}: {approval_response}")
                return {
                    "approved": False,
                    "status": "failed",
                    "message": approval_response.get("message", "Domain approval failed"),
                    "api_response": approval_response
                }
                
        except Exception as e:
            logger.error(f"Domain check error for user {user_id}: {e}")
            return {
                "approved": False,
                "status": "error",
                "message": f"Domain check error: {str(e)}",
                "error": str(e)
            }