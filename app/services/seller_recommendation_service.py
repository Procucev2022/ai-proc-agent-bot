"""
Seller recommendation service for RFQ notification system.

This service implements the core seller selection logic as per the requirements:
- Category matching based on RFQ categories
- Geographic filtering within 200km radius  
- Subscription status filtering (10 subscribed + 25 unsubscribed)
- 24-hour activity filtering for unsubscribed sellers
- Ranking-based prioritization (Diamond > Platinum > Gold > Titanium)
- Cyclic selection logic for unsubscribed sellers
- Rate limiting based on message history

Key responsibilities:
- Select qualifying sellers for approved RFQs
- Apply business rules and constraints
- Implement ranking and prioritization logic
- Manage cyclic selection for fairness
- Track notification history for rate limiting
"""

import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, or_

from app.database import get_db_session
from app.models import (
    Seller, SellerRanking, RFQSellerNotification, SystemConfiguration,
    MockRFQ, SellerSubscription
)
from app.config import get_settings
from app.utils.logging_utils import log_service_method
from app.services.location_service import LocationService
from app.services.seller_data_adapter import SellerDataAdapter
from app.utils import pincode_distance

logger = logging.getLogger(__name__)

class SellerRecommendationService:
    """
    Core service for selecting and recommending sellers for RFQ notifications.
    
    Implements the complete seller selection algorithm following the business
    requirements including category matching, geographic filtering, subscription
    management, and cyclic selection logic.
    """
    
    _instance = None
    _initialized = False
    
    def __new__(cls, db_session: Optional[Session] = None):
        if cls._instance is None:
            cls._instance = super(SellerRecommendationService, cls).__new__(cls)
        return cls._instance
    
    def __init__(self, db_session: Optional[Session] = None):
        if self._initialized:
            return
        self._initialized = True
        self.db_session = db_session or get_db_session()
        self.settings = get_settings()
        self.location_service = LocationService()
        
        # Default configuration values (can be overridden by database config)
        self.default_config = {
            "MAX_SUBSCRIBED_SELLERS_PER_RFQ": 10,
            "MAX_UNSUBSCRIBED_SELLERS_PER_RFQ": 15,
            "MAX_TIME_SINCE_LAST_MESSAGE_HOURS": 0.25,  # 24 hours, TODO: Update to 24hr, Set to 15 min for Testing
            "MAX_TIME_SINCE_LAST_ACTIVE_HOURS": 24    # 24 hours
        }
    
    @log_service_method("seller_recommendation")
    async def select_sellers_for_rfq(
        self,
        rfq_data: Dict[str, Any],
        candidate_seller_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Main seller selection method implementing the complete algorithm.

        Args:
            rfq_data: Dictionary containing RFQ information including:
                - rfq_id: RFQ identifier
                - categories: List of category strings
                - delivery_location: Location dict with lat/lng
                - quantity_info: Quantity information
            candidate_seller_ids: Optional list of seller IDs to pre-filter to.
                If provided, only these sellers will be considered (useful for
                hybrid approach where enhanced service pre-filters candidates).
                If None, all sellers matching categories will be considered.

        Returns:
            Dictionary with selected sellers categorized by subscription status:
            {
                "subscribed_sellers": [...],
                "unsubscribed_sellers": [...],
                "total_selected": int,
                "selection_metadata": {...}
            }
        """
        try:
            logger.info(f"Starting seller selection for RFQ {rfq_data.get('rfq_id')}")

            # Log if using pre-filtered candidates
            if candidate_seller_ids:
                logger.info(f"Using {len(candidate_seller_ids)} pre-filtered candidate sellers from enhanced service")

            # Load system configuration
            config = await self._load_system_config()

            # Step 1: Get all potential sellers based on categories
            category_matched_sellers = await self._filter_sellers_by_category(
                rfq_data.get("categories", []),
                candidate_seller_ids=candidate_seller_ids
            )
            
            if not category_matched_sellers:
                logger.warning(f"No sellers found for categories: {rfq_data.get('categories')}")
                return self._empty_selection_result("Unable to retrieve sellers from remote database or no sellers match the required categories")
            
            # Step 2: Calculate distances and sort by location
            geo_sorted_sellers = await self._filter_sellers_by_location(
                category_matched_sellers,
                rfq_data.get("delivery_location", {})
            )

            if not geo_sorted_sellers:
                logger.warning("No sellers remaining after location processing")
                return self._empty_selection_result("No sellers available for delivery location")
            
            # Step 3: Apply opt-out filtering
            active_sellers = [s for s in geo_sorted_sellers if not s.opted_out_notifications]
            
            # Step 4: Separate by subscription status
            subscribed_sellers = []
            unsubscribed_sellers = []
            
            for seller in active_sellers:
                if seller.subscription_credits > 0:
                    subscribed_sellers.append(seller)
                else:
                    unsubscribed_sellers.append(seller)
            
            # Step 5: Apply activity filtering for unsubscribed sellers (24hr inactive)
            unsubscribed_sellers = await self._filter_inactive_sellers(
                unsubscribed_sellers, 
                config["MAX_TIME_SINCE_LAST_ACTIVE_HOURS"]
            )
            
            # Step 6: Apply message rate limiting
            subscribed_sellers = await self._filter_by_message_history(
                subscribed_sellers,
                config["MAX_TIME_SINCE_LAST_MESSAGE_HOURS"]
            )
            unsubscribed_sellers = await self._filter_by_message_history(
                unsubscribed_sellers, 
                config["MAX_TIME_SINCE_LAST_MESSAGE_HOURS"]
            )
            
            # Step 7: Apply ranking and sort sellers
            subscribed_sellers = await self._rank_sellers_by_criteria(
                subscribed_sellers, rfq_data.get("delivery_location", {})
            )
            unsubscribed_sellers = await self._rank_sellers_by_criteria(
                unsubscribed_sellers, rfq_data.get("delivery_location", {})
            )
            
            # Step 8: Apply cyclic selection for unsubscribed sellers
            unsubscribed_sellers = await self._apply_cyclic_selection(
                unsubscribed_sellers, rfq_data.get("categories", [])
            )
            
            # Step 9: Select final counts
            selected_subscribed = subscribed_sellers[:config["MAX_SUBSCRIBED_SELLERS_PER_RFQ"]]
            selected_unsubscribed = unsubscribed_sellers[:config["MAX_UNSUBSCRIBED_SELLERS_PER_RFQ"]]
            
            # Step 10: Deduplicate across categories (ensure no seller appears twice)
            all_selected = selected_subscribed + selected_unsubscribed
            deduplicated_sellers = await self._deduplicate_sellers(all_selected)
            
            # Separate back into subscribed/unsubscribed after deduplication
            final_subscribed = [s for s in deduplicated_sellers if s.subscription_credits > 0]
            final_unsubscribed = [s for s in deduplicated_sellers if s.subscription_credits == 0]
            
            result = {
                "subscribed_sellers": [self._seller_to_dict(s) for s in final_subscribed],
                "unsubscribed_sellers": [self._seller_to_dict(s) for s in final_unsubscribed],
                "total_selected": len(deduplicated_sellers),
                "selection_metadata": {
                    "rfq_id": rfq_data.get("rfq_id"),
                    "categories_searched": rfq_data.get("categories", []),
                    "delivery_location": rfq_data.get("delivery_location", {}),
                    "initial_category_matches": len(category_matched_sellers),
                    "distance_sorted_count": len(geo_sorted_sellers),
                    "subscribed_available": len(subscribed_sellers),
                    "unsubscribed_available": len(unsubscribed_sellers),
                    "config_used": config,
                    "selection_timestamp": datetime.utcnow().isoformat()
                }
            }
            
            logger.info(f"Selected {len(final_subscribed)} subscribed and {len(final_unsubscribed)} unsubscribed sellers for RFQ {rfq_data.get('rfq_id')}")
            return result
            
        except Exception as e:
            logger.error(f"Error in seller selection: {str(e)}")
            return self._empty_selection_result(f"Selection failed: {str(e)}")
    
    async def _filter_sellers_by_category(
        self,
        categories: List[str],
        candidate_seller_ids: Optional[List[str]] = None
    ) -> List[Seller]:
        """
        Filter sellers who serve any of the RFQ categories.

        Args:
            categories: List of category strings to match
            candidate_seller_ids: Optional list of seller IDs to pre-filter to.
                If provided, only these sellers will be considered.

        Returns:
            List of sellers matching the categories (and optionally in candidate list)
        """
        if not categories:
            return []

        try:
            # Use SellerDataAdapter to get real seller data from remote database
            adapter = SellerDataAdapter()
            all_sellers = adapter.get_sellers_from_remote()

            # If candidate IDs provided, filter to those first (hybrid approach)
            if candidate_seller_ids:
                candidate_set = set(candidate_seller_ids)
                all_sellers = [s for s in all_sellers if s.seller_id in candidate_set]
                logger.info(f"Pre-filtered to {len(all_sellers)} sellers from {len(candidate_seller_ids)} candidates")

            # Filter sellers by categories (same logic as before)
            matching_sellers = []
            seen = set()

            for seller in all_sellers:
                # Check if seller serves any of the required categories
                seller_categories_lower = [cat.lower() for cat in seller.categories]
                if any(cat.lower() in seller_categories_lower for cat in categories):
                    if seller.seller_id not in seen:
                        seen.add(seller.seller_id)
                        matching_sellers.append(seller)

            logger.info(f"Found {len(matching_sellers)} sellers matching categories: {categories}")
            logger.debug(f"Categories searched: {categories}")
            logger.debug(f"Total sellers available: {len(all_sellers)}")

            return matching_sellers

        except Exception as e:
            logger.error(f"Error filtering sellers by category: {str(e)}")
            logger.error(f"Unable to retrieve seller data from remote database. Categories: {categories}")
            return []
    
    async def _filter_sellers_by_location(self, sellers: List[Seller], delivery_location: Dict) -> List[Seller]:
        """
        Calculate distances and sort sellers by proximity to delivery location.

        Uses pincode-based distance calculation for accuracy.
        No longer filters by radius - returns all sellers sorted by distance.
        """
        if not delivery_location:
            logger.warning("No delivery location provided, returning sellers unsorted")
            return sellers

        # Get delivery pincode
        delivery_pincode = delivery_location.get('pincode')

        if not delivery_pincode:
            logger.warning("No delivery pincode provided, returning sellers unsorted")
            return sellers

        sellers_with_distance = []
        sellers_without_distance = []

        for seller in sellers:
            try:
                seller_location = seller.location
                seller_pincode = seller_location.get('pincode') if seller_location else None

                if not seller_pincode:
                    logger.debug(f"Seller {seller.seller_id} ({seller.seller_name}) missing pincode")
                    sellers_without_distance.append(seller)
                    continue

                # Calculate distance using pincode helper
                distance_km = pincode_distance.calculate_distance_between_pincodes(
                    delivery_pincode,
                    seller_pincode
                )

                if distance_km is not None:
                    # Add distance info to seller for ranking
                    seller._calculated_distance = distance_km
                    sellers_with_distance.append(seller)
                    logger.debug(f"Seller {seller.seller_name} at pincode {seller_pincode}: {distance_km:.2f} km away")
                else:
                    logger.debug(f"Could not calculate distance for seller {seller.seller_name} (pincode: {seller_pincode})")
                    sellers_without_distance.append(seller)

            except Exception as e:
                logger.warning(f"Error calculating distance for seller {seller.seller_id}: {str(e)}")
                sellers_without_distance.append(seller)
                continue

        # Sort sellers with distance by proximity (closest first)
        sellers_with_distance.sort(key=lambda s: s._calculated_distance)

        # Return sellers with distance first, then others
        result = sellers_with_distance + sellers_without_distance

        logger.info(f"Calculated distances for {len(sellers_with_distance)}/{len(sellers)} sellers")
        if sellers_with_distance:
            closest = sellers_with_distance[0]
            farthest = sellers_with_distance[-1]
            logger.info(f"Distance range: {closest._calculated_distance:.2f} km (closest) to {farthest._calculated_distance:.2f} km (farthest)")

        return result
    
    async def _filter_inactive_sellers(self, sellers: List[Seller], max_inactive_hours: int) -> List[Seller]:
        """
        Filter to keep ACTIVE sellers (active within last N hours).

        For unsubscribed sellers, we only want those who have been active recently
        to ensure they're engaged and likely to respond.
        """
        if not sellers:
            return []

        cutoff_time = datetime.utcnow() - timedelta(hours=max_inactive_hours)
        active_sellers = []

        for seller in sellers:
            # Keep seller if they have been active within the time window
            if seller.last_active_at and seller.last_active_at >= cutoff_time:
                active_sellers.append(seller)

        logger.info(f"Filtered to {len(active_sellers)} sellers active within last {max_inactive_hours}h")
        return active_sellers
    
    async def _filter_by_message_history(self, sellers: List[Seller], max_hours_since_message: int) -> List[Seller]:
        """Filter out sellers who received messages in the last N hours."""
        if not sellers:
            return []
        
        cutoff_time = datetime.utcnow() - timedelta(hours=max_hours_since_message)
        filtered_sellers = []
        
        for seller in sellers:
            # Check if seller received any notification in the last N hours
            recent_notification = self.db_session.query(RFQSellerNotification).filter(
                and_(
                    RFQSellerNotification.seller_id == seller.seller_id,
                    RFQSellerNotification.sent_at > cutoff_time
                )
            ).first()
            
            if not recent_notification:
                filtered_sellers.append(seller)
        
        logger.info(f"Filtered to {len(filtered_sellers)} sellers with no recent messages")
        return filtered_sellers
    
    async def _rank_sellers_by_criteria(self, sellers: List[Seller], delivery_location: Dict) -> List[Seller]:
        """
        Rank sellers by business criteria: DISTANCE first, then ranking tier.

        Priority: Distance >> Ranking
        Example: Gold seller at 0 km beats Diamond seller at 10 km
        """
        if not sellers:
            return []

        # Define ranking order (higher score = higher priority)
        ranking_scores = {
            SellerRanking.Diamond: 3,
            SellerRanking.Gold: 2,
            SellerRanking.Titanium: 1
        }

        def seller_sort_key(seller):
            distance = getattr(seller, '_calculated_distance', 999999)  # Sellers without distance go last
            rank_score = ranking_scores.get(seller.ranking, 1)
            # Sort by: 1) Distance (ascending), 2) Ranking (descending)
            return (distance, -rank_score)

        sorted_sellers = sorted(sellers, key=seller_sort_key)
        logger.info(f"Ranked {len(sorted_sellers)} sellers by distance (primary) and ranking (secondary)")
        return sorted_sellers
    
    async def _apply_cyclic_selection(self, sellers: List[Seller], categories: List[str]) -> List[Seller]:
        """
        Apply cyclic selection logic for unsubscribed sellers.
        
        Ensures fair distribution by tracking which sellers were recently selected
        and prioritizing those who haven't been selected recently.
        """
        if not sellers:
            return []
        
        # For now, implement simple round-robin based on recent notification history
        # In production, this would use a more sophisticated state tracking system
        
        # Get sellers sorted by last notification time (least recently notified first)
        sellers_with_last_notification = []
        
        for seller in sellers:
            last_notification = self.db_session.query(RFQSellerNotification)\
                .filter(RFQSellerNotification.seller_id == seller.seller_id)\
                .order_by(RFQSellerNotification.sent_at.desc())\
                .first()
            
            last_notified = last_notification.sent_at if last_notification else datetime.min
            sellers_with_last_notification.append((seller, last_notified))
        
        # Sort by last notification time (oldest first for fairness)
        sorted_by_fairness = sorted(sellers_with_last_notification, key=lambda x: x[1])
        cyclic_sellers = [seller for seller, _ in sorted_by_fairness]
        
        logger.info(f"Applied cyclic selection to {len(cyclic_sellers)} unsubscribed sellers")
        return cyclic_sellers
    
    async def _deduplicate_sellers(self, sellers: List[Seller]) -> List[Seller]:
        """Remove duplicate sellers from the final selection."""
        seen_ids = set()
        deduplicated = []
        
        for seller in sellers:
            if seller.seller_id not in seen_ids:
                seen_ids.add(seller.seller_id)
                deduplicated.append(seller)
        
        logger.info(f"Deduplicated {len(sellers)} to {len(deduplicated)} unique sellers")
        return deduplicated
    
    async def _load_system_config(self) -> Dict[str, Any]:
        """Load system configuration from database or use defaults."""
        try:
            config = {}
            
            # Load each configuration key
            for key, default_value in self.default_config.items():
                db_config = self.db_session.query(SystemConfiguration)\
                    .filter(SystemConfiguration.config_key == key).first()
                
                if db_config:
                    config[key] = db_config.config_value
                else:
                    config[key] = default_value
            
            return config
            
        except Exception as e:
            logger.warning(f"Error loading system config, using defaults: {str(e)}")
            return self.default_config
    
    def _seller_to_dict(self, seller: Seller) -> Dict[str, Any]:
        """Convert Seller model to dictionary for API response."""
        return {
            "seller_id": seller.seller_id,
            "seller_name": seller.seller_name,
            "phone_number": seller.phone_number,
            "email": seller.email,
            "categories": seller.categories,
            "location": seller.location,
            "subscription_credits": seller.subscription_credits,
            "ranking": seller.ranking.value if seller.ranking else "Gold",
            "last_active_at": seller.last_active_at.isoformat() if seller.last_active_at else None,
            "distance_km": getattr(seller, '_calculated_distance', None)
        }
    
    def _empty_selection_result(self, reason: str) -> Dict[str, Any]:
        """Return empty selection result with reason."""
        return {
            "subscribed_sellers": [],
            "unsubscribed_sellers": [],
            "total_selected": 0,
            "selection_metadata": {
                "reason": reason,
                "selection_timestamp": datetime.utcnow().isoformat()
            }
        }
    
    @log_service_method("seller_recommendation")
    async def get_seller_details(self, seller_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific seller."""
        try:
            seller = self.db_session.query(Seller)\
                .filter(Seller.seller_id == seller_id).first()
            
            if not seller:
                return None
            
            # Get subscription details
            active_subscription = self.db_session.query(SellerSubscription)\
                .filter(
                    and_(
                        SellerSubscription.seller_id == seller_id,
                        SellerSubscription.credits_remaining > 0
                    )
                ).first()
            
            # Get recent notification count
            recent_notifications = self.db_session.query(func.count(RFQSellerNotification.notification_id))\
                .filter(
                    and_(
                        RFQSellerNotification.seller_id == seller_id,
                        RFQSellerNotification.sent_at > datetime.utcnow() - timedelta(days=30)
                    )
                ).scalar()
            
            return {
                **self._seller_to_dict(seller),
                "subscription_details": {
                    "plan_type": active_subscription.plan_type.value if active_subscription else None,
                    "credits_purchased": active_subscription.credits_purchased if active_subscription else 0,
                    "purchased_at": active_subscription.purchased_at.isoformat() if active_subscription else None,
                    "expires_at": active_subscription.expires_at.isoformat() if active_subscription else None
                },
                "notification_stats": {
                    "recent_notifications_30d": recent_notifications
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting seller details for {seller_id}: {str(e)}")
            return None