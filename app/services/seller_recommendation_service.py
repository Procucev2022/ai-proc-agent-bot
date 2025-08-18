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

logger = logging.getLogger(__name__)

class SellerRecommendationService:
    """
    Core service for selecting and recommending sellers for RFQ notifications.
    
    Implements the complete seller selection algorithm following the business
    requirements including category matching, geographic filtering, subscription
    management, and cyclic selection logic.
    """
    
    def __init__(self, db_session: Optional[Session] = None):
        self.db_session = db_session or get_db_session()
        self.settings = get_settings()
        self.location_service = LocationService()
        
        # Default configuration values (can be overridden by database config)
        self.default_config = {
            "MAX_SUBSCRIBED_SELLERS_PER_RFQ": 10,
            "MAX_UNSUBSCRIBED_SELLERS_PER_RFQ": 25, 
            "MAX_TIME_SINCE_LAST_MESSAGE_HOURS": 24,
            "MAX_TIME_SINCE_LAST_ACTIVE_HOURS": 24,
            "GEO_DISTANCE_RADIUS_KM": 200
        }
    
    @log_service_method("seller_recommendation")
    async def select_sellers_for_rfq(self, rfq_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main seller selection method implementing the complete algorithm.
        
        Args:
            rfq_data: Dictionary containing RFQ information including:
                - rfq_id: RFQ identifier
                - categories: List of category strings
                - delivery_location: Location dict with lat/lng
                - quantity_info: Quantity information
                
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
            
            # Load system configuration
            config = await self._load_system_config()
            
            # Step 1: Get all potential sellers based on categories
            category_matched_sellers = await self._filter_sellers_by_category(
                rfq_data.get("categories", [])
            )
            
            if not category_matched_sellers:
                logger.warning(f"No sellers found for categories: {rfq_data.get('categories')}")
                return self._empty_selection_result("No sellers match the required categories")
            
            # Step 2: Apply geographic filtering
            geo_filtered_sellers = await self._filter_sellers_by_location(
                category_matched_sellers, 
                rfq_data.get("delivery_location", {}),
                config["GEO_DISTANCE_RADIUS_KM"]
            )
            
            if not geo_filtered_sellers:
                logger.warning(f"No sellers found within {config['GEO_DISTANCE_RADIUS_KM']}km radius")
                return self._empty_selection_result("No sellers found within geographic range")
            
            # Step 3: Apply opt-out filtering
            active_sellers = [s for s in geo_filtered_sellers if not s.opted_out_notifications]
            
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
                    "geo_filtered_count": len(geo_filtered_sellers),
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
    
    async def _filter_sellers_by_category(self, categories: List[str]) -> List[Seller]:
        """Filter sellers who serve any of the RFQ categories."""
        if not categories:
            return []
        
        try:
            # Use JSON_CONTAINS or JSON_OVERLAPS to find sellers with matching categories
            sellers = []
            for category in categories:
                category_sellers = self.db_session.query(Seller).filter(
                    func.json_contains(Seller.categories, f'"{category}"')
                ).all()
                sellers.extend(category_sellers)
            
            # Remove duplicates while preserving order
            seen = set()
            unique_sellers = []
            for seller in sellers:
                if seller.seller_id not in seen:
                    seen.add(seller.seller_id)
                    unique_sellers.append(seller)
            
            logger.info(f"Found {len(unique_sellers)} sellers matching categories: {categories}")
            return unique_sellers
            
        except Exception as e:
            logger.error(f"Error filtering by category: {str(e)}")
            return []
    
    async def _filter_sellers_by_location(self, sellers: List[Seller], delivery_location: Dict, radius_km: int) -> List[Seller]:
        """Filter sellers within geographic radius of delivery location."""
        if not delivery_location:
            logger.warning("No delivery location provided, skipping geo filtering")
            return sellers
        
        # Handle both old format (lat/lng) and new format (state/city/pincode)
        if delivery_location.get('lat') and delivery_location.get('lng'):
            # Legacy format - use directly
            delivery_coords = {
                'lat': delivery_location['lat'],
                'lng': delivery_location['lng']
            }
            logger.debug("Using legacy lat/lng format for delivery location")
        else:
            # New format - convert pincode to coordinates
            delivery_coords = await self.location_service.get_delivery_coordinates(delivery_location)
            logger.debug(f"Converted delivery location {delivery_location} to coordinates {delivery_coords}")
        
        if not delivery_coords.get('lat') or not delivery_coords.get('lng'):
            logger.warning("Could not determine delivery coordinates, skipping geo filtering")
            return sellers
        
        filtered_sellers = []
        
        for seller in sellers:
            try:
                seller_location = seller.location
                if not seller_location or not seller_location.get('lat') or not seller_location.get('lng'):
                    logger.debug(f"Seller {seller.seller_id} missing location data")
                    continue
                
                # Calculate distance using location service
                distance = await self.location_service.calculate_distance(seller_location, delivery_coords)
                
                # Check if seller is within their coverage area or within system radius
                max_distance = min(radius_km, seller.geographic_coverage_km or radius_km)
                
                if distance <= max_distance and distance != float('inf'):
                    # Add distance info to seller for ranking
                    seller._calculated_distance = distance
                    filtered_sellers.append(seller)
                    logger.debug(f"Seller {seller.seller_name} is {distance}km away (within {max_distance}km)")
                else:
                    logger.debug(f"Seller {seller.seller_name} is {distance}km away (outside {max_distance}km)")
                    
            except Exception as e:
                logger.warning(f"Error calculating distance for seller {seller.seller_id}: {str(e)}")
                continue
        
        logger.info(f"Filtered to {len(filtered_sellers)} sellers within {radius_km}km of delivery location")
        return filtered_sellers
    
    async def _filter_inactive_sellers(self, sellers: List[Seller], max_inactive_hours: int) -> List[Seller]:
        """Filter out sellers who have been active in the last N hours (for unsubscribed only)."""
        if not sellers:
            return []
        
        cutoff_time = datetime.utcnow() - timedelta(hours=max_inactive_hours)
        inactive_sellers = []
        
        for seller in sellers:
            if not seller.last_active_at or seller.last_active_at < cutoff_time:
                inactive_sellers.append(seller)
        
        logger.info(f"Filtered to {len(inactive_sellers)} sellers inactive for >{max_inactive_hours}h")
        return inactive_sellers
    
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
        """Rank sellers by business criteria: ranking tier, then distance."""
        if not sellers:
            return []
        
        # Define ranking order (higher score = higher priority)
        ranking_scores = {
            SellerRanking.Diamond: 4,
            SellerRanking.Platinum: 3,
            SellerRanking.Gold: 2,
            SellerRanking.Titanium: 1
        }
        
        def seller_score(seller):
            rank_score = ranking_scores.get(seller.ranking, 1)
            distance_score = 1000 - getattr(seller, '_calculated_distance', 500)  # Closer = higher score
            return (rank_score * 1000) + distance_score  # Ranking weighted heavily
        
        sorted_sellers = sorted(sellers, key=seller_score, reverse=True)
        logger.info(f"Ranked {len(sorted_sellers)} sellers by ranking and distance")
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