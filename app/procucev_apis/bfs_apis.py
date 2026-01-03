"""
BFS (Buy From Stock) API Service.

This service handles BFS operations for searching available stock items
with the Procucev backend.
"""

import logging
from typing import Dict, Any, List

from app.procucev_apis.procucev_api_client import get_procucev_api_client

logger = logging.getLogger(__name__)

# BFS API endpoint
BFS_API_URL = "https://p2pv1servicesdev-etfrcte5fhdvfrd4.centralindia-01.azurewebsites.net/rest/bfs/getBfsItemsByCategory"


class BFSAPIService:
    """
    Service for handling BFS (Buy From Stock) operations.
    Provides methods for searching stock items by category/description.
    """

    def __init__(self):
        self.api_client = get_procucev_api_client()

    async def search_bfs_items(self, products: List[Dict[str, List[str]]]) -> Dict[str, Any]:
        """
        Search BFS items by product descriptions.

        Args:
            products: List of products with description arrays.
                Example: [
                    {"description": ["laptop", "notebook"]},
                    {"description": ["phone", "mobile"]}
                ]

        Returns:
            API response with available stock items.
        """
        try:
            logger.info(f"[BFS API] Searching BFS items with payload: {products}")

            # ============ MOCK DATA FOR TESTING ============
            # TODO: Remove this mock data and uncomment actual API call below
            mock_data = [
                {
                    "id": "BFS001",
                    "description": "Dell XPS 13",
                    "specification": "Intel i7, 16GB RAM, 512GB SSD",
                    "availableQuantity": 5,
                    "sellPrice": 85000,
                    "ageOfAsset": "1"
                },
                {
                    "id": "BFS002",
                    "description": "HP Pavilion 15",
                    "specification": "AMD Ryzen 5, 8GB RAM, 256GB SSD",
                    "availableQuantity": 3,
                    "sellPrice": 55000,
                    "ageOfAsset": "2"
                },
                {
                    "id": "BFS003",
                    "description": "Lenovo ThinkPad E14",
                    "specification": "Intel i5, 8GB RAM, 512GB SSD",
                    "availableQuantity": 8,
                    "sellPrice": 62000,
                    "ageOfAsset": "1"
                },
                {
                    "id": "BFS004",
                    "description": "MacBook Air M1",
                    "specification": "Apple M1, 8GB RAM, 256GB SSD",
                    "availableQuantity": 2,
                    "sellPrice": 92000,
                    "ageOfAsset": "1"
                },
                {
                    "id": "BFS005",
                    "description": "ASUS VivoBook",
                    "specification": "Intel i3, 4GB RAM, 1TB HDD",
                    "availableQuantity": 10,
                    "sellPrice": 35000,
                    "ageOfAsset": "3"
                }
            ]
            logger.info(f"[BFS API] Returning MOCK data for testing: {len(mock_data)} items")
            return {
                "success": True,
                "data": mock_data,
                "raw_response": {"mock": True}
            }
            # ============ END MOCK DATA ============

            # ACTUAL API CALL (commented out for testing)
            # response = await self.api_client.post(
            #     endpoint=BFS_API_URL,
            #     json_data=products,
            #     require_auth=True,
            #     api_title="BFS Search Items API"
            # )
            #
            # if response.get('success'):
            #     logger.info(f"[BFS API] Search successful, found items: {response.get('data')}")
            #     return {
            #         "success": True,
            #         "data": response.get('data'),
            #         "raw_response": response
            #     }
            # else:
            #     logger.error(f"[BFS API] Search failed: {response}")
            #     return {
            #         "success": False,
            #         "error": response.get('message', 'BFS search failed'),
            #         "raw_response": response
            #     }

        except Exception as e:
            logger.error(f"[BFS API] Error searching BFS items: {e}")
            return {
                "success": False,
                "error": str(e)
            }


# Singleton instance
_bfs_api_service: BFSAPIService = None


def get_bfs_api_service() -> BFSAPIService:
    """Get the BFS API service singleton instance."""
    global _bfs_api_service
    if _bfs_api_service is None:
        _bfs_api_service = BFSAPIService()
    return _bfs_api_service
