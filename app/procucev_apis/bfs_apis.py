"""
BFS (Buy From Stock) API Service.

This service handles BFS operations for searching available stock items
with the Procucev backend.
"""

import logging
from typing import Dict, Any, List

from app.procucev_apis.procucev_api_client import get_procucev_api_client
from app.config import get_settings
logger = logging.getLogger(__name__)

# BFS API endpoints
BFS_SEARCH_URL = f"{get_settings().gmt_base_url}/rest/bfs/getBfsItemsByCategory"
BFS_REQUEST_ITEM_URL = f"{get_settings().gmt_base_url}/rest/bfs/requestBfsItem"
BFS_ACCEPT_BID_URL = f"{get_settings().gmt_base_url}/rest/bfs/acceptBfsItemBySeller"
BFS_REJECT_BID_URL = f"{get_settings().gmt_base_url}/rest/bfs/rejectBfsItemBySeller"


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
            products: List of products with category and description arrays.
                Example: [
                    {"category": ["steels"], "description": ["Alloy Steel plate"]},
                    {"category": ["electronics"], "description": ["laptop"]}
                ]

        Returns:
            API response with available stock items.
        """
        try:
            logger.info(f"[BFS API] Searching BFS items with payload: {products}")

            response = await self.api_client.post(
                endpoint=BFS_SEARCH_URL,
                json_data=products,
                require_auth=True,
                api_title="BFS Search Items API"
            )

            if response.get('success'):
                logger.info(f"[BFS API] Search successful, found items: {response.get('data')}")
                return {
                    "success": True,
                    "data": response.get('data'),
                    "raw_response": response
                }
            else:
                logger.error(f"[BFS API] Search failed: {response}")
                return {
                    "success": False,
                    "error": response.get('message', 'BFS search failed'),
                    "raw_response": response
                }

        except Exception as e:
            logger.error(f"[BFS API] Error searching BFS items: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def request_bfs_item(
        self,
        item_id: str,
        buy_price: float,
        ask_price: float,
        quantity: int,
        org_id: str,
        user_id: str,
        buyer_phone: str
    ) -> Dict[str, Any]:
        """
        Request/bid on a BFS item.

        Args:
            item_id: The ID of the BFS item to request (from search results).
            buy_price: The seller's listed price for the item.
            ask_price: The buyer's bid/offer amount.
            quantity: Number of items to request.
            org_id: The buyer's organization ID.
            user_id: The buyer's user ID.
            buyer_phone: The buyer's phone number.

        Returns:
            API response with request confirmation or error.
        """
        try:
            payload = {
                "buyPrice": buy_price,
                "quantity": quantity,
                "askPrice": ask_price,
                "org": {
                    "id": org_id
                },
                "items": {
                    "id": item_id
                },
                "user": {
                    "id": user_id
                },
                "buyerPhone": buyer_phone
            }

            logger.info(f"[BFS API] Requesting BFS item with payload: {payload}")

            response = await self.api_client.post(
                endpoint=BFS_REQUEST_ITEM_URL,
                json_data=payload,
                require_auth=True,
                api_title="BFS Request Item API"
            )

            if response.get('success'):
                logger.info(f"[BFS API] BFS item request successful: {response.get('data')}")
                return {
                    "success": True,
                    "data": response.get('data'),
                    "raw_response": response
                }
            else:
                logger.error(f"[BFS API] BFS item request failed: {response}")
                return {
                    "success": False,
                    "error": response.get('message', 'BFS item request failed'),
                    "raw_response": response
                }

        except Exception as e:
            logger.error(f"[BFS API] Error requesting BFS item: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def accept_bid_by_seller(self, bfs_user_id: str) -> Dict[str, Any]:
        """
        Accept a BFS bid as a seller.

        Called when seller clicks "Accept Bid" button on the notification.
        This confirms the seller agrees to the buyer's offered price.

        Args:
            bfs_user_id: The bfs_users record UUID (from button callback)

        Returns:
            API response with acceptance confirmation or error
        """
        try:
            payload = {"id": bfs_user_id}

            logger.info(f"[BFS API] Seller accepting bid: {bfs_user_id}")

            response = await self.api_client.post(
                endpoint=BFS_ACCEPT_BID_URL,
                json_data=payload,
                require_auth=True,
                api_title="BFS Accept Bid API"
            )

            if response.get('success'):
                logger.info(f"[BFS API] Bid accepted successfully: {bfs_user_id}")
                return {
                    "success": True,
                    "bfs_user_id": bfs_user_id,
                    "data": response.get('data'),
                    "raw_response": response
                }
            else:
                logger.error(f"[BFS API] Bid acceptance failed: {response}")
                return {
                    "success": False,
                    "bfs_user_id": bfs_user_id,
                    "error": response.get('message', 'Bid acceptance failed'),
                    "raw_response": response
                }

        except Exception as e:
            logger.error(f"[BFS API] Error accepting bid {bfs_user_id}: {e}")
            return {
                "success": False,
                "bfs_user_id": bfs_user_id,
                "error": str(e)
            }

    async def reject_bid_by_seller(self, bfs_user_id: str) -> Dict[str, Any]:
        """
        Reject a BFS bid as a seller.

        Called when seller clicks "Reject Bid" button on the notification.
        This declines the buyer's offered price.

        Args:
            bfs_user_id: The bfs_users record UUID (from button callback)

        Returns:
            API response with rejection confirmation or error
        """
        try:
            payload = {"id": bfs_user_id}

            logger.info(f"[BFS API] Seller rejecting bid: {bfs_user_id}")

            response = await self.api_client.post(
                endpoint=BFS_REJECT_BID_URL,
                json_data=payload,
                require_auth=True,
                api_title="BFS Reject Bid API"
            )

            if response.get('success'):
                logger.info(f"[BFS API] Bid rejected successfully: {bfs_user_id}")
                return {
                    "success": True,
                    "bfs_user_id": bfs_user_id,
                    "data": response.get('data'),
                    "raw_response": response
                }
            else:
                logger.error(f"[BFS API] Bid rejection failed: {response}")
                return {
                    "success": False,
                    "bfs_user_id": bfs_user_id,
                    "error": response.get('message', 'Bid rejection failed'),
                    "raw_response": response
                }

        except Exception as e:
            logger.error(f"[BFS API] Error rejecting bid {bfs_user_id}: {e}")
            return {
                "success": False,
                "bfs_user_id": bfs_user_id,
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
