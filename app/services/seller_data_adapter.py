"""
Seller Data Adapter for Real Database Integration.

This adapter transforms data from the remote organization/user tables
into the existing Seller model format, enabling seamless integration
with real data while preserving all existing business logic.

Key transformations:
- organization.uuid → seller_id
- organization.vendor_class → ranking (Diamond/Platinum/Gold/Titanium)
- user.activity_ts → last_active_at (for 24-hour filtering)
- organization.opt_out → opted_out_notifications
- Smart defaults for missing fields (categories, location, credits)
"""

import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.database import execute_remote_query
from app.models import Seller, SellerRanking

logger = logging.getLogger(__name__)


class SellerDataAdapter:
    """
    Adapter to convert remote organization/user data to Seller model format.

    This adapter enables switching from mock data to real database data
    with minimal changes to existing business logic.
    """

    def __init__(self):
        self.default_location = {
            "lat": 12.9716,
            "lng": 77.5946,
            "city": "Bangalore",
            "state": "Karnataka"
        }

        # Mapping for vendor_class to SellerRanking enum
        self.ranking_map = {
            'Diamond': SellerRanking.Diamond,
            'Platinum': SellerRanking.Platinum,
            'Gold': SellerRanking.Gold,
            'Titanium': SellerRanking.Titanium
        }

    def get_sellers_from_remote(self, limit: Optional[int] = None) -> List[Seller]:
        """
        Fetch sellers from remote organization/user tables.

        Args:
            limit: Optional limit on number of sellers to fetch

        Returns:
            List of Seller objects transformed from remote data
        """
        try:
            logger.info("Fetching sellers from remote database...")

            query = self._build_seller_query()
            params = {}

            if limit:
                query += " LIMIT :limit"
                params['limit'] = limit

            remote_data = execute_remote_query(query, params)
            logger.info(f"Fetched {len(remote_data)} seller records from remote database")

            # Transform remote data to Seller objects
            sellers = []
            for row in remote_data:
                try:
                    seller = self._transform_to_seller(row)
                    sellers.append(seller)
                except Exception as e:
                    logger.warning(f"Failed to transform seller {row.get('seller_id', 'unknown')}: {e}")
                    continue

            logger.info(f"Successfully transformed {len(sellers)} sellers")
            return sellers

        except Exception as e:
            logger.error(f"Error fetching sellers from remote database: {e}")
            logger.error(f"This will result in no sellers being available for RFQ matching")
            raise

    def _build_seller_query(self) -> str:
        """Build the SQL query to fetch seller data from remote tables."""
        return """
        SELECT
            o.uuid as seller_id,
            o.organization_name as seller_name,
            o.vendor_class as ranking,
            COALESCE(o.opt_out, 0) as opted_out_notifications,
            o.opt_out_modified_date,
            o.organization_phonenumber as phone_number,
            o.email,
            o.rfq_credits as subscription_credits,

            -- Get last activity from user table (critical for 24h filtering)
            u.activity_ts as last_active_at,
            u.uuid as user_uuid,

            -- Smart category defaults based on vendor class
            CASE COALESCE(o.vendor_class, 'Gold')
                WHEN 'Diamond' THEN '["Premium Equipment", "Industrial Solutions", "High-Value Products"]'
                WHEN 'Platinum' THEN '["Electronics", "Medical Equipment", "IT Equipment"]'
                WHEN 'Gold' THEN '["General Trading", "Office Supplies", "Consumer Goods"]'
                WHEN 'Titanium' THEN '["Basic Supplies", "General Trading"]'
                ELSE '["General Trading"]'
            END as categories,

            -- Build location from actual address data
            CONCAT(
                '{"lat": 12.9716, "lng": 77.5946, "city": "',
                COALESCE(o.city, 'Unknown'),
                '", "state": "',
                COALESCE(o.state, 'Unknown'),
                '", "address": "',
                COALESCE(CONCAT(o.address1, ' ', o.address2), ''),
                '"}'
            ) as location,

            -- Additional fields for debugging
            o.created_ts as org_created_at,
            u.last_modified_ts as user_last_modified,
            o.vendorcategory as org_vendor_category

        FROM organization o
        LEFT JOIN user u ON o.uuid = u.org_uuid
        WHERE COALESCE(o.opt_out, 0) != 1  -- Exclude opted-out sellers
          AND o.organization_name IS NOT NULL  -- Must have organization name
          AND o.organization_name != ''  -- Not empty
        ORDER BY u.activity_ts IS NULL, u.activity_ts DESC  -- Most recently active first, nulls last
        """

    def _transform_to_seller(self, row: Dict[str, Any]) -> Seller:
        """
        Transform a single remote database row to Seller object.

        Args:
            row: Dictionary from remote database query

        Returns:
            Seller object with all fields properly mapped
        """
        try:
            # Parse JSON fields with error handling
            try:
                categories = json.loads(row['categories']) if row['categories'] else ["General Trading"]
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Invalid categories JSON for seller {row['seller_id']}: {e}")
                categories = ["General Trading"]  # Default fallback

            try:
                location = json.loads(row['location']) if row['location'] else self.default_location
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Invalid location JSON for seller {row['seller_id']}: {e}")
                location = self.default_location  # Default fallback

            # Map vendor_class to ranking enum
            ranking = self.ranking_map.get(row['ranking'], SellerRanking.Gold)

            # Handle phone number
            phone_number = row['phone_number']
            if not phone_number:
                # Generate fallback phone number from seller_id
                phone_number = f"91{str(abs(hash(row['seller_id'])))[-10:]}"

            # Handle email
            email = row['email']
            if not email and row['seller_name']:
                # Generate fallback email
                company_slug = row['seller_name'].lower().replace(' ', '').replace('&', '')[:20]
                email = f"contact@{company_slug}.com"

            # Create Seller object
            seller = Seller(
                seller_id=row['seller_id'],
                seller_name=row['seller_name'],
                phone_number=phone_number,
                email=email,
                categories=categories,
                location=location,
                ranking=ranking,

                # Critical: Map activity_ts to last_active_at for 24h filtering
                last_active_at=row['last_active_at'],

                opted_out_notifications=bool(row['opted_out_notifications']),
                subscription_credits=row.get('subscription_credits', 0) or 0,
                geographic_coverage_km=self._get_coverage_km(ranking)
            )

            return seller

        except Exception as e:
            logger.error(f"Error transforming seller row: {e}, row: {row}")
            raise

    def _get_subscription_credits(self, seller_id: str, ranking: SellerRanking) -> int:
        """
        Get seller subscription credits.

        For now, uses ranking-based defaults. Can be enhanced later
        to integrate with actual billing/payment system.

        Args:
            seller_id: Seller identifier
            ranking: Seller ranking

        Returns:
            Number of subscription credits
        """
        # Default credits based on ranking
        credit_defaults = {
            SellerRanking.Diamond: 10,   # Premium sellers get more credits
            SellerRanking.Platinum: 5,
            SellerRanking.Gold: 0,       # Most sellers start with 0
            SellerRanking.Titanium: 0
        }

        # TODO: Replace with actual billing system integration
        # Could query a separate subscriptions table or payment API

        return credit_defaults.get(ranking, 0)

    def _get_coverage_km(self, ranking: SellerRanking) -> int:
        """
        Get geographic coverage based on seller ranking.

        Args:
            ranking: Seller ranking

        Returns:
            Coverage radius in kilometers
        """
        coverage_defaults = {
            SellerRanking.Diamond: 500,   # Premium sellers cover more area
            SellerRanking.Platinum: 300,
            SellerRanking.Gold: 200,      # Standard coverage
            SellerRanking.Titanium: 150
        }

        return coverage_defaults.get(ranking, 200)

    def get_seller_by_id(self, seller_id: str) -> Optional[Seller]:
        """
        Get a specific seller by ID from remote database.

        Args:
            seller_id: Seller identifier

        Returns:
            Seller object if found, None otherwise
        """
        try:
            query = self._build_seller_query() + " AND o.uuid = :seller_id"
            result = execute_remote_query(query, {'seller_id': seller_id})

            if result:
                return self._transform_to_seller(result[0])
            return None

        except Exception as e:
            logger.error(f"Error fetching seller {seller_id}: {e}")
            return None

    def get_sellers_by_categories(self, categories: List[str]) -> List[Seller]:
        """
        Get sellers filtered by categories (used for optimization).

        For now, gets all sellers and filters in Python.
        Can be optimized later with database-level category filtering.

        Args:
            categories: List of categories to match

        Returns:
            List of sellers matching any of the categories
        """
        all_sellers = self.get_sellers_from_remote()

        # Filter sellers by categories
        matching_sellers = []
        for seller in all_sellers:
            if any(cat.lower() in [sc.lower() for sc in seller.categories] for cat in categories):
                matching_sellers.append(seller)

        logger.info(f"Filtered to {len(matching_sellers)} sellers matching categories: {categories}")
        return matching_sellers

    def test_connection(self) -> bool:
        """
        Test remote database connection and data availability.

        Returns:
            True if connection and data are available
        """
        try:
            query = """
            SELECT COUNT(*) as seller_count
            FROM organization o
            WHERE COALESCE(o.opt_out, 0) != 1
              AND o.organization_name IS NOT NULL
              AND o.organization_name != ''
            """
            result = execute_remote_query(query)

            if result and result[0]['seller_count'] > 0:
                logger.info(f"Remote seller data available: {result[0]['seller_count']} organizations found")
                return True
            else:
                logger.warning("No organizations found in remote database")
                return False

        except Exception as e:
            logger.error(f"Failed to test remote seller connection: {e}")
            return False