"""
B2B WhatsApp Daily Aggregation Service - Redesigned for actual business requirements.

Based on the B2B WhatsApp Insights Report, this service calculates the correct
business metrics for buyers, sellers, and categories.
"""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, date, timedelta
from collections import defaultdict, Counter
from sqlalchemy import func
from decimal import Decimal

from app.database import get_db_session
from app.models import ConversationSession, DailyAggregatedMetrics, RollingWindowMetrics, UserType
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class DailyAggregationService:
    """Service for B2B WhatsApp business metrics aggregation matching the insights report."""
    
    def __init__(self):
        self.settings = get_settings()
    
    @log_service_method("daily_aggregation_service")
    async def run_daily_aggregation(self, target_date: date = None) -> bool:
        """Run daily aggregation for B2B WhatsApp metrics."""
        if target_date is None:
            target_date = date.today() - timedelta(days=1)
        
        try:
            logger.info(f"Starting B2B WhatsApp daily aggregation for {target_date}")
            
            with get_db_session() as db:
                # Get all sessions for the day
                sessions = db.query(ConversationSession)\
                    .filter(func.date(ConversationSession.created_at) == target_date)\
                    .all()
                
                if not sessions:
                    logger.info(f"No sessions found for {target_date}")
                    return True
                
                # Calculate metrics matching the report format
                buyer_metrics = await self._calculate_buyer_summary_metrics(sessions, target_date)
                seller_metrics = await self._calculate_seller_summary_metrics(sessions, target_date)
                category_metrics = await self._calculate_category_summary_metrics(sessions, target_date)
                
                # Store daily metrics
                self._store_metrics(db, target_date, 'buyer_summary', buyer_metrics)
                self._store_metrics(db, target_date, 'seller_summary', seller_metrics)
                self._store_metrics(db, target_date, 'category_summary', category_metrics)
                
                db.commit()
                
                # Calculate and store rolling window metrics
                await self._update_rolling_windows(target_date)
                
                logger.info(f"B2B WhatsApp daily aggregation completed for {target_date}")
                return True
                
        except Exception as e:
            logger.error(f"Daily aggregation failed for {target_date}: {e}")
            return False
    
    async def _calculate_buyer_summary_metrics(self, sessions: List, target_date: date) -> Dict[str, Any]:
        """
        Calculate buyer summary metrics matching the B2B WhatsApp Insights Report.
        
        Metrics (refined based on Excel requirements):
        - Number of Chats Initiated By Buyers
        - Unique Buyers
        - Total RFQs Submitted
        - Unique Buyers Submitted RFQ (NEW)
        - Average Products per RFQ
        - Average Categories per RFQ
        - RFQs with At Least One Response
        - Total RFQ Responses (NEW)
        - No of Products Searched by Buyers (NEW)
        - BFS-related metrics
        """
        buyer_sessions = [s for s in sessions if s.user_type == UserType.buyer]
        
        # 1. Number of Chats Initiated By Buyers
        chats_initiated = len(buyer_sessions)
        
        # 2. Unique Buyers
        unique_buyers = len(set(s.external_user_id for s in buyer_sessions))
        
        # 3. Total RFQs Submitted
        total_rfqs = 0
        
        # 4. Unique Buyers Submitted RFQ (NEW)
        buyers_with_rfqs = set()
        
        # Additional tracking variables
        all_products = []
        all_categories = []
        bfs_searches = 0
        products_searched_total = 0  # NEW
        total_rfq_responses = 0  # NEW
        rfqs_with_at_least_one_response = 0  # RENAMED
        products_bid_for = 0
        bids_accepted = 0
        
        for session in buyer_sessions:
            # Count RFQs
            session_rfqs = 0
            if session.rfq_ids:
                session_rfqs = len([rfq for rfq in session.rfq_ids if rfq])
            elif session.rfq_id:
                session_rfqs = 1
            
            total_rfqs += session_rfqs
            
            # Track unique buyers who submitted RFQs
            if session_rfqs > 0:
                buyers_with_rfqs.add(session.external_user_id)
            
            # Count products and categories per session
            if session.product_items:
                products = session.product_items
                if isinstance(products, list):
                    all_products.extend(products)
                    # Extract categories from products
                    for product in products:
                        if isinstance(product, dict) and product.get('category'):
                            all_categories.append(product['category'])
            
            # Products searched count (NEW)
            if session.products_searched_count:
                products_searched_total += session.products_searched_count
            
            # Total RFQ responses received (NEW)
            if session.total_rfq_responses_received:
                total_rfq_responses += session.total_rfq_responses_received
            
            # BFS searches
            if session.bfs_search_count:
                bfs_searches += session.bfs_search_count
            
            # Products bid for
            if session.products_bid_for:
                if isinstance(session.products_bid_for, list):
                    products_bid_for += len(session.products_bid_for)
                elif isinstance(session.products_bid_for, dict):
                    products_bid_for += len(session.products_bid_for.get('products', []))
            
            # Bids accepted
            if session.bids_accepted:
                if isinstance(session.bids_accepted, list):
                    bids_accepted += len(session.bids_accepted)
                elif isinstance(session.bids_accepted, dict):
                    bids_accepted += session.bids_accepted.get('count', 0)
            
            # RFQs with at least one response (count unique RFQs, not total responses)
            if session.rfqs_with_response:
                if isinstance(session.rfqs_with_response, list):
                    rfqs_with_at_least_one_response += len(set(session.rfqs_with_response))  # Count unique RFQs
                elif isinstance(session.rfqs_with_response, dict):
                    rfqs_with_at_least_one_response += session.rfqs_with_response.get('unique_rfqs', 0)
        
        # 5. Average Products per RFQ
        avg_products_per_rfq = len(all_products) / total_rfqs if total_rfqs > 0 else 0
        
        # 6. Average Categories per RFQ  
        unique_categories = len(set(all_categories))
        avg_categories_per_rfq = unique_categories / total_rfqs if total_rfqs > 0 else 0
        
        return {
            'chats_initiated_by_buyers': chats_initiated,
            'unique_buyers': unique_buyers,
            'total_rfqs_submitted': total_rfqs,
            'unique_buyers_submitted_rfq': len(buyers_with_rfqs),  # Can be calculated from existing data
            'avg_products_per_rfq': round(avg_products_per_rfq, 2),
            'avg_categories_per_rfq': round(avg_categories_per_rfq, 2),
            'rfqs_with_at_least_one_response': rfqs_with_at_least_one_response,  # Renamed from existing
            'total_rfq_responses': 0,  # TODO: Implement total RFQ responses tracking in chat service
            'products_searched_by_buyers': 0,  # TODO: Implement products searched tracking in chat service
            'bfs_products_searched': bfs_searches,
            'products_bid_for': products_bid_for,
            'bids_accepted_by_buyers': bids_accepted
        }
    
    async def _calculate_seller_summary_metrics(self, sessions: List, target_date: date) -> Dict[str, Any]:
        """
        Calculate seller summary metrics matching the B2B WhatsApp Insights Report.
        
        Metrics (refined based on Excel requirements):
        - Seller Chats Initiated
        - Unique Sellers
        - RFQs Requested
        - Subscription Plans Requested (NEW)
        - Counter Offers Accepted
        """
        seller_sessions = [s for s in sessions if s.user_type == UserType.seller]
        
        # 1. Seller Chats Initiated
        seller_chats = len(seller_sessions)
        
        # 2. Unique Sellers
        unique_sellers = len(set(s.external_user_id for s in seller_sessions))
        
        # 3. RFQs Requested
        rfqs_requested = 0
        
        # 4. Subscription Plans Requested (NEW - placeholder)
        subscription_plans_requested = 0
        
        # 5. Counter Offers Accepted
        counter_offers_accepted = 0
        
        for session in seller_sessions:
            # RFQs requested (RFQs that sellers asked for or engaged with)
            if session.rfq_ids:
                rfqs_requested += len([rfq for rfq in session.rfq_ids if rfq])
            elif session.rfq_id:
                rfqs_requested += 1
            
            # RFQs responded (RFQs sellers actually responded to)
            if session.seller_responses:
                if isinstance(session.seller_responses, list):
                    rfqs_responded += len(session.seller_responses)
                elif isinstance(session.seller_responses, dict):
                    rfqs_responded += session.seller_responses.get('count', 0)
            
            # Bids accepted
            if session.bids_accepted:
                if isinstance(session.bids_accepted, list):
                    bids_accepted += len(session.bids_accepted)
                elif isinstance(session.bids_accepted, dict):
                    bids_accepted += session.bids_accepted.get('count', 0)
            
            # Counter offers accepted
            if session.counter_offers_accepted:
                if isinstance(session.counter_offers_accepted, list):
                    counter_offers_accepted += len(session.counter_offers_accepted)
                elif isinstance(session.counter_offers_accepted, dict):
                    counter_offers_accepted += session.counter_offers_accepted.get('count', 0)
        
        return {
            'seller_chats_initiated': seller_chats,
            'unique_sellers': unique_sellers,
            'rfqs_requested': rfqs_requested,
            'subscription_plans_requested': 0,  # TODO: Implement subscription plans tracking in chat service
            'counter_offers_accepted': counter_offers_accepted
        }
    
    async def _calculate_category_summary_metrics(self, sessions: List, target_date: date) -> Dict[str, Any]:
        """
        Calculate category summary metrics matching the B2B WhatsApp Insights Report.
        
        Metrics per category (updated based on Excel requirements):
        - RFQs Uploaded
        - RFQ Requested (kept for existing functionality)
        - RFQs w/ Response
        - BFS Products Searched
        - BFS Price Accepted by Buyers
        - BFS Counter Offer By Buyer (NEW)
        - Counter Offers Accepted By Sellers
        - New Counter Offer By Seller
        - BFS Products Searched by Unregistered Users (NEW)
        """
        category_data = defaultdict(lambda: {
            'rfqs_uploaded': 0,
            'rfq_requested': 0,  # Keep existing for backward compatibility
            'rfqs_with_response': 0,
            'bfs_products_searched': 0,
            'bfs_price_accepted': 0,
            'bfs_counter_offer_by_buyer': 0,  # TODO: Implement BFS counter offers by buyer tracking
            'counter_offers_accepted_by_sellers': 0,
            'new_counter_offer_by_seller': 0,
            'bfs_products_searched_by_unregistered_users': 0  # TODO: Implement unregistered user BFS tracking
        })
        
        # TODO: Implement Multi Category tracking - sessions with len(categories) > 1 should be 
        # tracked separately in a "Multi Category" bucket instead of individual categories
        
        for session in sessions:
            # Extract categories from session
            categories = self._extract_categories_from_session(session)
            
            for category in categories:
                # RFQs uploaded
                if session.user_type == UserType.buyer:
                    rfq_count = 0
                    if session.rfq_ids:
                        rfq_count = len([rfq for rfq in session.rfq_ids if rfq])
                    elif session.rfq_id:
                        rfq_count = 1
                    category_data[category]['rfqs_uploaded'] += rfq_count
                
                # RFQs with response
                if session.rfqs_with_response:
                    if isinstance(session.rfqs_with_response, list):
                        category_data[category]['rfqs_with_response'] += len(session.rfqs_with_response)
                    elif isinstance(session.rfqs_with_response, dict):
                        category_data[category]['rfqs_with_response'] += session.rfqs_with_response.get('count', 0)
                
                # BFS products searched
                if session.bfs_search_count:
                    category_data[category]['bfs_products_searched'] += session.bfs_search_count
                
                # BFS price accepted by buyers
                if session.bfs_price_accepted:
                    if isinstance(session.bfs_price_accepted, list):
                        category_data[category]['bfs_price_accepted'] += len(session.bfs_price_accepted)
                    elif isinstance(session.bfs_price_accepted, dict):
                        category_data[category]['bfs_price_accepted'] += session.bfs_price_accepted.get('count', 0)
                
                # Counter offers accepted by sellers
                if session.counter_offers_accepted and session.user_type == UserType.seller:
                    if isinstance(session.counter_offers_accepted, list):
                        category_data[category]['counter_offers_accepted_by_sellers'] += len(session.counter_offers_accepted)
                    elif isinstance(session.counter_offers_accepted, dict):
                        category_data[category]['counter_offers_accepted_by_sellers'] += session.counter_offers_accepted.get('count', 0)
                
                # New counter offers by seller
                if session.counter_offers_made and session.user_type == UserType.seller:
                    if isinstance(session.counter_offers_made, list):
                        category_data[category]['new_counter_offer_by_seller'] += len(session.counter_offers_made)
                    elif isinstance(session.counter_offers_made, dict):
                        category_data[category]['new_counter_offer_by_seller'] += session.counter_offers_made.get('count', 0)
        
        return dict(category_data)
    
    def _extract_categories_from_session(self, session) -> List[str]:
        """Extract all categories mentioned in a session."""
        categories = set()
        
        # From product items
        if session.product_items:
            if isinstance(session.product_items, list):
                for product in session.product_items:
                    if isinstance(product, dict) and product.get('category'):
                        categories.add(product['category'])
        
        # From extracted entities
        if session.extracted_entities:
            entities = session.extracted_entities
            if isinstance(entities, dict):
                if entities.get('category'):
                    categories.add(entities['category'])
                if entities.get('description'):
                    categories.add(entities['description'])
            elif isinstance(entities, list):
                for entity in entities:
                    if isinstance(entity, dict):
                        if entity.get('category'):
                            categories.add(entity['category'])
                        if entity.get('description'):
                            categories.add(entity['description'])
        
        return list(categories)
    
    def _store_metrics(self, db, target_date: date, metric_type: str, metric_data: Dict) -> None:
        """Store or update metrics in database."""
        existing = db.query(DailyAggregatedMetrics)\
            .filter(DailyAggregatedMetrics.date == target_date)\
            .filter(DailyAggregatedMetrics.metric_type == metric_type)\
            .first()
        
        if existing:
            existing.metric_data = metric_data
            existing.is_complete = True
        else:
            new_metric = DailyAggregatedMetrics(
                date=target_date,
                metric_type=metric_type,
                metric_data=metric_data,
                is_complete=True
            )
            db.add(new_metric)
    
    async def _update_rolling_windows(self, end_date: date) -> None:
        """Calculate and store 7/30/90 day rolling windows."""
        try:
            await self._calculate_rolling_window(end_date, "7day", 7)
            await self._calculate_rolling_window(end_date, "30day", 30)
            await self._calculate_rolling_window(end_date, "90day", 90)
            
        except Exception as e:
            logger.error(f"Error updating rolling windows: {e}")
            raise
    
    async def _calculate_rolling_window(self, end_date: date, window_type: str, days: int) -> None:
        """Calculate rolling window aggregation matching the report format."""
        try:
            start_date = end_date - timedelta(days=days-1)
            
            with get_db_session() as db:
                # Load daily metrics for the period
                daily_metrics = db.query(DailyAggregatedMetrics)\
                    .filter(DailyAggregatedMetrics.date.between(start_date, end_date))\
                    .filter(DailyAggregatedMetrics.is_complete == True)\
                    .all()
                
                # Separate by metric type
                buyer_daily_data = []
                seller_daily_data = []
                category_daily_data = []
                
                for metric in daily_metrics:
                    if metric.metric_type == 'buyer_summary':
                        buyer_daily_data.append(metric.metric_data)
                    elif metric.metric_type == 'seller_summary':
                        seller_daily_data.append(metric.metric_data)
                    elif metric.metric_type == 'category_summary':
                        category_daily_data.append(metric.metric_data)
                
                # Aggregate metrics
                buyer_totals = self._aggregate_buyer_rolling_metrics(buyer_daily_data)
                seller_totals = self._aggregate_seller_rolling_metrics(seller_daily_data)
                category_totals = self._aggregate_category_rolling_metrics(category_daily_data)
                
                # Store rolling window metric
                existing = db.query(RollingWindowMetrics)\
                    .filter(RollingWindowMetrics.window_type == window_type)\
                    .filter(RollingWindowMetrics.end_date == end_date)\
                    .first()
                
                if existing:
                    existing.buyer_metrics = buyer_totals
                    existing.seller_metrics = seller_totals
                    existing.category_metrics = category_totals
                    existing.last_updated = func.current_timestamp()
                else:
                    new_window = RollingWindowMetrics(
                        window_type=window_type,
                        end_date=end_date,
                        buyer_metrics=buyer_totals,
                        seller_metrics=seller_totals,
                        category_metrics=category_totals
                    )
                    db.add(new_window)
                
                db.commit()
                logger.info(f"Updated {window_type} rolling window for {end_date}")
                
        except Exception as e:
            logger.error(f"Error calculating {window_type} rolling window: {e}")
            raise
    
    def _aggregate_buyer_rolling_metrics(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """Aggregate buyer metrics across multiple days for rolling windows."""
        if not daily_data:
            return {}
        
        totals = {
            'chats_initiated_by_buyers': 0,
            'unique_buyers': 0,  # This should be recalculated, not summed
            'total_rfqs_submitted': 0,
            'unique_buyers_submitted_rfq': 0,  # This should be recalculated, not summed
            'rfqs_with_at_least_one_response': 0,  # Renamed
            'total_rfq_responses': 0,  # New placeholder
            'products_searched_by_buyers': 0,  # New placeholder
            'bfs_products_searched': 0,
            'products_bid_for': 0,
            'bids_accepted_by_buyers': 0
        }
        
        total_products = 0
        total_categories = 0
        total_rfqs = 0
        
        for day_data in daily_data:
            for key in totals.keys():
                if key in day_data:
                    totals[key] += day_data[key]
            
            # Accumulate for averages
            if 'avg_products_per_rfq' in day_data and 'total_rfqs_submitted' in day_data:
                total_products += day_data['avg_products_per_rfq'] * day_data['total_rfqs_submitted']
            
            if 'avg_categories_per_rfq' in day_data and 'total_rfqs_submitted' in day_data:
                total_categories += day_data['avg_categories_per_rfq'] * day_data['total_rfqs_submitted']
                
            if 'total_rfqs_submitted' in day_data:
                total_rfqs += day_data['total_rfqs_submitted']
        
        # Calculate averages
        totals['avg_products_per_rfq'] = round(total_products / total_rfqs, 2) if total_rfqs > 0 else 0
        totals['avg_categories_per_rfq'] = round(total_categories / total_rfqs, 2) if total_rfqs > 0 else 0
        
        return totals
    
    def _aggregate_seller_rolling_metrics(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """Aggregate seller metrics across multiple days for rolling windows."""
        if not daily_data:
            return {}
        
        totals = {
            'seller_chats_initiated': 0,
            'unique_sellers': 0,  # This should be recalculated, not summed
            'rfqs_requested': 0,
            'subscription_plans_requested': 0,  # New placeholder
            'counter_offers_accepted': 0
        }
        
        for day_data in daily_data:
            for key in totals.keys():
                if key in day_data:
                    totals[key] += day_data[key]
        
        return totals
    
    def _aggregate_category_rolling_metrics(self, daily_data: List[Dict]) -> Dict[str, Any]:
        """Aggregate category metrics across multiple days for rolling windows."""
        if not daily_data:
            return {}
        
        # Merge all categories across days
        merged_categories = defaultdict(lambda: {
            'rfqs_uploaded': 0,
            'rfqs_with_response': 0,
            'bfs_products_searched': 0,
            'bfs_price_accepted': 0,
            'counter_offers_accepted_by_sellers': 0,
            'new_counter_offer_by_seller': 0
        })
        
        for day_data in daily_data:
            for category, metrics in day_data.items():
                for metric_name, value in metrics.items():
                    merged_categories[category][metric_name] += value
        
        return dict(merged_categories)