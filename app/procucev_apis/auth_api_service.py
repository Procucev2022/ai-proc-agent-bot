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
            logger.info(f" User Auth Api service is Called - authenticate User")
            endpoint = f"/partialvendor/getUsersByPhoneNumber/{phone_number}"
            response_data = await self.api_client.get(endpoint)
            print("api response" ,response_data)

            if isinstance(response_data.get("data"), list) and len(response_data["data"]) > 0:
                logger.info("User details retrieved successfully")
                return {
                    "success": True,
                    "response": response_data["data"],
                }
            else:
                logger.warning("No user found with given phone number")
                return {
                    "success": False,
                    "error": "No user data found"
                }
        except Exception as e:
            logger.error(f"Error fetching buyer details: {e}")
            return {"success": False, "error": str(e)}


    