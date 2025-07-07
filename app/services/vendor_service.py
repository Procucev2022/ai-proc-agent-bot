"""
Vendor matching and search service for procurement queries.
"""

from typing import Dict, List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..database import get_db_session
from ..models import ProductCategory, Vendor


class VendorService:
    """Vendor search service with direct SQL queries."""

    def __init__(self, db_session: Optional[Session] = None):
        self.db_session = db_session or get_db_session()

    def search_vendors(self, entities: dict) -> List[Dict]:
        """Search vendors based on extracted entities."""
        if not entities:
            return []

        entity_data = entities.get("entities", {})
        if not entity_data:
            return []

        category = entity_data.get("category")
        location = entity_data.get("location")

        if not category and not location:
            return []

        # Build query
        query = self.db_session.query(Vendor)

        # Filter by category if provided
        if category:
            query = query.filter(Vendor.vendor_services.any(category))

        # Filter by location if provided
        if location:
            query = query.filter(Vendor.geographic_coverage.any(location))

        # Execute query
        vendors = query.all()

        # Convert to dict format and rank results
        results = []
        for vendor in vendors:
            vendor_dict = {
                "vendor_id": str(vendor.vendor_id),
                "vendor_name": vendor.vendor_name,
                "geographic_coverage": vendor.geographic_coverage,
                "vendor_services": vendor.vendor_services,
                "relevance_score": self._calculate_relevance(vendor, entity_data),
            }
            results.append(vendor_dict)

        # Sort by relevance score
        results.sort(key=lambda x: x["relevance_score"], reverse=True)

        return results

    def search_bfs_inventory(self, entities: dict) -> List[Dict]:
        """Search BFS inventory - placeholder for now since we don't have BFS table."""
        # This would query a BFS/Product table when implemented
        # For now, return vendors that match the criteria
        return self.search_vendors(entities)

    def get_vendor_recommendations(self, entities: dict) -> List[Dict]:
        """Get vendor recommendations for RFQ creation."""
        # Same as search_vendors but with RFQ-specific ranking
        vendors = self.search_vendors(entities)

        # Add RFQ-specific scoring here if needed
        return vendors[:5]  # Return top 5 recommendations

    def update_vendor_learning(
        self, rfq_id: str, vendor_id: str, learned_categories: list
    ):
        """Update vendor profile with learned associations from CM assignments."""
        pass  # TODO: Implement when learning system is needed

    def _calculate_relevance(self, vendor: Vendor, entities: dict) -> float:
        """Calculate relevance score for vendor based on entities."""
        score = 0.0

        category = entities.get("category")
        location = entities.get("location")

        # Category match scoring
        if category and category in vendor.vendor_services:
            score += 50.0

        # Location match scoring
        if location:
            if location in vendor.geographic_coverage:
                score += 30.0
            # Partial location matches (city in coverage area)
            else:
                for coverage_area in vendor.geographic_coverage:
                    if (
                        location.lower() in coverage_area.lower()
                        or coverage_area.lower() in location.lower()
                    ):
                        score += 15.0
                        break

        # Service breadth bonus (more services = higher score)
        score += len(vendor.vendor_services) * 2.0

        # Coverage area bonus (more locations = higher score)
        score += len(vendor.geographic_coverage) * 1.0

        return score


# TODO: Changes needed when integrating with actual client database:
#
# 1. BFS Inventory Search (search_bfs_inventory method):
#    - Create actual BFS/Product table queries instead of placeholder
#    - Query fields: description, specification, category, location, ageOfAsset,
#      totalQuantity, availableQuantity, sellPrice, askPrice, itemNumber
#    - Add real availability checks and inventory filtering
#
# 2. Database Schema Updates:
#    - Update Vendor model to match actual client database schema
#    - May need different field names, additional relationships
#    - Add proper foreign key relationships to product/category tables
#
# 3. Query Optimization:
#    - Add database indexes on frequently queried fields (category, location)
#    - Optimize for <200ms response time target
#    - Consider using raw SQL for complex queries if needed
#
# 4. Category/Location Matching:
#    - Implement fuzzy matching for location names
#    - Handle category hierarchies if they exist in client DB
#    - Add standardized location codes/mapping
#
# 5. Performance Metrics Integration:
#    - Add vendor performance scoring based on historical data
#    - Include delivery time, quality ratings in relevance calculation
#    - Query vendor transaction history for scoring
#
# 6. Real API Integration:
#    - Replace mock responses with actual BFS API calls using client endpoints
#    - Handle API authentication and rate limiting
#    - Add error handling for external API failures
