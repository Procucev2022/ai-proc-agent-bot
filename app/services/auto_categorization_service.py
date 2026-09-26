"""
Auto-categorization entry point for RFQ and seller items.

Vector-search (RAG) categorization and its ChromaDB store have been removed. Every
item is now left uncategorized: categorize_item always returns the unsuccessful
result shape, so each caller takes its existing "no category" path (BFS search
continues without a category, seller registration completes without categories).
"""

from typing import Dict, Optional


class UncategorizedCategorizationService:
    """Categorizer that leaves every item uncategorized."""

    async def categorize_item(self,
                              item_description: str,
                              user_id: str,
                              session_id: Optional[str] = None,
                              rfq_id: Optional[str] = None) -> Dict:
        return {
            "success": False,
            "method": "disabled",
            "reason": "categorization_disabled",
            "error": "Item categorization is not available",
            "processing_time_ms": 0
        }


_service = UncategorizedCategorizationService()


def get_auto_categorization_service() -> UncategorizedCategorizationService:
    """Return the shared categorizer."""
    return _service


async def get_auto_categorization_service_async(
    timeout: Optional[float] = None,
) -> UncategorizedCategorizationService:
    """Return the shared categorizer; timeout is accepted for caller compatibility."""
    return _service
