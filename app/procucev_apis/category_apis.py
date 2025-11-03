"""
Category API Service.

This service handles category and division management operations
with the GMT Procucev backend.
"""

import logging
from typing import Dict, Any

from app.procucev_apis.procucev_api_client import get_procucev_api_client

logger = logging.getLogger(__name__)

class CategoryAPIService:
    """
    Service for handling category and division operations.
    Provides methods for fetching divisions and categories.
    """

    def __init__(self):
        self.api_client = get_procucev_api_client()
        
    async def get_divisions(self) -> Dict[str, Any]:
        """Get all available divisions."""
        try:
            endpoint = "/rest/categoryManager/getAllDivisions"

            response = await self.api_client.get(
                endpoint=endpoint,
                require_auth=True,
                api_title="get_divisions"
            )
            
            if response.get('success'):
                return {"success": True, "divisions": response.get('data')}
            else:
                return {"success": False, "error": response.get('message', 'Failed to get divisions')}
                        
        except Exception as e:
            logger.error(f"Error getting divisions: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_categories(self) -> Dict[str, Any]:
        """Get all available categories."""
        try:
            endpoint = "/rest/categoryManager/getAllCategories"

            response = await self.api_client.get(
                endpoint=endpoint,
                require_auth=True,
                api_title="get_categories"
            )
            
            if response.get('success'):
                return {"success": True, "categories": response.get('data')}
            else:
                return {"success": False, "error": response.get('message', 'Failed to get categories')}
                        
        except Exception as e:
            logger.error(f"Error getting categories: {e}")
            return {"success": False, "error": str(e)}