"""BFS (Buy From Stock) Search Service."""

import logging
from typing import Dict, Any, Optional
from .auto_categorization_service import get_auto_categorization_service_async

logger = logging.getLogger(__name__)


class BFSSearchService:
    """Service for BFS stock search operations."""

    def __init__(self):
        # Resolved lazily. Building it loads a Sentence Transformer model, which must
        # not happen in __init__ on the event loop.
        self.auto_categorization_service = None

    async def get_product_category(
        self,
        product_description: str,
        user_id: str,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get category for a product using AutoCategorization.

        Args:
            product_description: User's product description
            user_id: User ID for audit trail
            session_id: Optional session ID

        Returns:
            {
                "success": bool,
                "category": str (if success),
                "confidence": float,
                "error": str (if failed)
            }
        """
        try:
            logger.info(f"Getting category for product: {product_description[:50]}...")

            if self.auto_categorization_service is None:
                self.auto_categorization_service = await get_auto_categorization_service_async()

            result = await self.auto_categorization_service.categorize_item(
                item_description=product_description,
                user_id=user_id,
                session_id=session_id
            )

            if result.get("success"):
                return {
                    "success": True,
                    "category": result["category"],
                    "confidence": result.get("confidence_score", 0.0),
                    "method": result.get("method", "unknown")
                }
            else:
                return {
                    "success": False,
                    "error": result.get("reason", "Categorization failed")
                }

        except Exception as e:
            logger.error(f"Error getting product category: {e}")
            return {
                "success": False,
                "error": str(e)
            }
