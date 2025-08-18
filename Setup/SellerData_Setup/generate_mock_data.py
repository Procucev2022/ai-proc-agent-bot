"""
Mock data generation script for the seller recommendation system.

This script generates realistic mock data for testing the complete seller
recommendation and RFQ intimation flow including:
- Seller profiles with categories, locations, rankings
- System configurations
- Mock RFQs for testing
- Seller subscriptions and credit history

The data is distributed across Indian cities with realistic business scenarios
to provide comprehensive testing coverage.

Usage:
    python generate_mock_data.py [--clear] [--sellers N] [--rfqs N]
"""

import sys
import os
import random
import uuid
from datetime import datetime, timedelta, date
from typing import List, Dict, Any

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.database import get_db_session
from app.models import (
    Seller, SellerRanking, SystemConfiguration, MockRFQ, 
    SellerSubscription, SubscriptionPlan, RFQStatus
)

class MockDataGenerator:
    """Generator for realistic mock data for the seller recommendation system."""
    
    def __init__(self):
        self.db_session = get_db_session()
        
        # Indian cities with coordinates
        self.cities = [
            {"city": "Bangalore", "state": "Karnataka", "lat": 12.9716, "lng": 77.5946},
            {"city": "Mumbai", "state": "Maharashtra", "lat": 19.0760, "lng": 72.8777},
            {"city": "Delhi", "state": "Delhi", "lat": 28.6139, "lng": 77.2090},
            {"city": "Chennai", "state": "Tamil Nadu", "lat": 13.0827, "lng": 80.2707},
            {"city": "Hyderabad", "state": "Telangana", "lat": 17.3850, "lng": 78.4867},
            {"city": "Pune", "state": "Maharashtra", "lat": 18.5204, "lng": 73.8567},
            {"city": "Kolkata", "state": "West Bengal", "lat": 22.5726, "lng": 88.3639},
            {"city": "Ahmedabad", "state": "Gujarat", "lat": 23.0225, "lng": 72.5714},
            {"city": "Jaipur", "state": "Rajasthan", "lat": 26.9124, "lng": 75.7873},
            {"city": "Surat", "state": "Gujarat", "lat": 21.1702, "lng": 72.8311},
            {"city": "Lucknow", "state": "Uttar Pradesh", "lat": 26.8467, "lng": 80.9462},
            {"city": "Kanpur", "state": "Uttar Pradesh", "lat": 26.4499, "lng": 80.3319},
            {"city": "Nagpur", "state": "Maharashtra", "lat": 21.1458, "lng": 79.0882},
            {"city": "Indore", "state": "Madhya Pradesh", "lat": 22.7196, "lng": 75.8577},
            {"city": "Visakhapatnam", "state": "Andhra Pradesh", "lat": 17.6868, "lng": 83.2185}
        ]
        
        # Business categories
        self.categories = [
            "Electronics", "Medical Equipment", "IT Equipment", "Furniture", 
            "Office Supplies", "Industrial Equipment", "Automotive Parts",
            "Construction Materials", "Healthcare Supplies", "Laboratory Equipment",
            "Security Systems", "Telecommunications", "Power & Energy",
            "Food & Beverages", "Textiles", "Chemicals", "Packaging Materials",
            "HVAC Systems", "Printing Equipment", "Educational Supplies"
        ]
        
        # Company name templates
        self.company_templates = [
            "{} Solutions", "{} Enterprises", "{} Industries", "{} Systems",
            "{} Technologies", "{} Corp", "{} Trading", "{} Suppliers",
            "{} Equipment", "{} Services", "{} International", "{} Group"
        ]
        
        self.company_prefixes = [
            "Tech", "Global", "Prime", "Elite", "Advanced", "Modern", "United",
            "Supreme", "Perfect", "Dynamic", "Smart", "Digital", "Premium",
            "Reliable", "Expert", "Professional", "Quality", "Excellence",
            "Innovation", "Future", "Strategic", "Pioneer", "Leading"
        ]
    
    def generate_all_data(self, num_sellers: int = 60, num_rfqs: int = 20, clear_existing: bool = False) -> Dict[str, Any]:
        """
        Generate complete mock dataset.
        
        Args:
            num_sellers: Number of sellers to create
            num_rfqs: Number of RFQs to create
            clear_existing: Whether to clear existing data first
            
        Returns:
            Dictionary with generation results
        """
        try:
            print(f"🚀 Starting mock data generation...")
            print(f"   Sellers: {num_sellers}")
            print(f"   RFQs: {num_rfqs}")
            print(f"   Clear existing: {clear_existing}")
            
            generation_start = datetime.utcnow()
            
            if clear_existing:
                print("🧹 Clearing existing data...")
                self._clear_existing_data()
            
            # Generate system configurations
            print("⚙️ Creating system configurations...")
            self._generate_system_config()
            
            # Generate sellers
            print(f"👥 Generating {num_sellers} sellers...")
            sellers_created = self._generate_sellers(num_sellers)
            
            # Generate seller subscriptions
            print("💳 Generating seller subscriptions...")
            subscriptions_created = self._generate_seller_subscriptions(sellers_created)
            
            # Generate RFQs
            print(f"📋 Generating {num_rfqs} RFQs...")
            rfqs_created = self._generate_mock_rfqs(num_rfqs)
            
            generation_time = (datetime.utcnow() - generation_start).total_seconds()
            
            result = {
                "success": True,
                "generation_time_seconds": generation_time,
                "data_created": {
                    "sellers": len(sellers_created),
                    "subscriptions": len(subscriptions_created),
                    "rfqs": len(rfqs_created),
                    "system_configs": 6  # Number of config entries
                },
                "seller_distribution": self._get_seller_distribution(sellers_created),
                "rfq_distribution": self._get_rfq_distribution(rfqs_created)
            }
            
            print(f"✅ Mock data generation completed in {generation_time:.2f} seconds!")
            print(f"   Created: {len(sellers_created)} sellers, {len(rfqs_created)} RFQs")
            
            return result
            
        except Exception as e:
            print(f"❌ Error generating mock data: {str(e)}")
            self.db_session.rollback()
            return {
                "success": False,
                "error": str(e)
            }
        finally:
            self.db_session.close()
    
    def _clear_existing_data(self) -> None:
        """Clear existing mock data from database."""
        try:
            # Delete in correct order to respect foreign key constraints
            self.db_session.query(SellerSubscription).delete()
            self.db_session.query(MockRFQ).delete()
            self.db_session.query(Seller).delete()
            self.db_session.query(SystemConfiguration).delete()
            
            self.db_session.commit()
            print("   Existing data cleared successfully")
            
        except Exception as e:
            print(f"   Error clearing existing data: {str(e)}")
            self.db_session.rollback()
            raise
    
    def _generate_system_config(self) -> None:
        """Generate system configuration entries."""
        configs = [
            {
                "config_key": "MAX_SUBSCRIBED_SELLERS_PER_RFQ",
                "config_value": 10,
                "description": "Maximum subscribed sellers to notify per RFQ"
            },
            {
                "config_key": "MAX_UNSUBSCRIBED_SELLERS_PER_RFQ",
                "config_value": 25,
                "description": "Maximum unsubscribed sellers to notify per RFQ"
            },
            {
                "config_key": "MAX_TIME_SINCE_LAST_MESSAGE_HOURS",
                "config_value": 24,
                "description": "Max time since last message sent to seller"
            },
            {
                "config_key": "MAX_TIME_SINCE_LAST_ACTIVE_HOURS",
                "config_value": 24,
                "description": "Max time since seller was last active"
            },
            {
                "config_key": "GEO_DISTANCE_RADIUS_KM",
                "config_value": 200,
                "description": "Geographic distance radius for seller selection"
            },
            {
                "config_key": "SUBSCRIPTION_PLANS",
                "config_value": {
                    "basic": {"price": 499, "credits": 5},
                    "pro": {"price": 999, "credits": 15}
                },
                "description": "Available subscription plans"
            }
        ]
        
        for config_data in configs:
            config = SystemConfiguration(**config_data)
            self.db_session.add(config)
        
        self.db_session.commit()
    
    def _generate_sellers(self, num_sellers: int) -> List[Seller]:
        """Generate mock seller data with realistic distribution."""
        sellers = []
        
        # Distribution targets
        city_weights = [15, 12, 10, 8, 5, 5, 3, 2, 2, 2, 1, 1, 1, 1, 1]  # Matches cities list
        ranking_weights = [0.1, 0.2, 0.5, 0.2]  # Diamond, Platinum, Gold, Titanium
        
        for i in range(num_sellers):
            # Select city based on weights
            city = random.choices(self.cities, weights=city_weights[:len(self.cities)])[0]
            
            # Select ranking based on weights
            ranking = random.choices(
                list(SellerRanking), 
                weights=ranking_weights
            )[0]
            
            # Generate company name
            prefix = random.choice(self.company_prefixes)
            template = random.choice(self.company_templates)
            company_name = template.format(prefix)
            
            # Select 1-3 categories
            seller_categories = random.sample(self.categories, random.randint(1, 3))
            
            # Generate phone number (Indian format)
            phone_number = f"91{random.randint(7000000000, 9999999999)}"
            
            # Generate email
            email = f"contact@{company_name.lower().replace(' ', '')}.com"
            
            # Last active time (mix of recent and older)
            if random.random() < 0.3:  # 30% recently active
                last_active = datetime.utcnow() - timedelta(hours=random.randint(1, 23))
            else:  # 70% inactive for 24+ hours
                last_active = datetime.utcnow() - timedelta(hours=random.randint(25, 168))  # 1-7 days
            
            # Geographic coverage
            coverage_km = random.randint(150, 300)
            
            seller = Seller(
                seller_name=company_name,
                phone_number=phone_number,
                email=email,
                categories=seller_categories,
                location={
                    "lat": city["lat"],
                    "lng": city["lng"],
                    "city": city["city"],
                    "state": city["state"]
                },
                geographic_coverage_km=coverage_km,
                subscription_credits=0,  # Will be set by subscription generation
                ranking=ranking,
                last_active_at=last_active,
                opted_out_notifications=random.random() < 0.05  # 5% opted out
            )
            
            sellers.append(seller)
            self.db_session.add(seller)
        
        self.db_session.commit()
        
        # Refresh sellers to get generated IDs
        for seller in sellers:
            self.db_session.refresh(seller)
        
        return sellers
    
    def _generate_seller_subscriptions(self, sellers: List[Seller]) -> List[SellerSubscription]:
        """Generate realistic seller subscription data."""
        subscriptions = []
        
        # 40% of sellers should have active subscriptions
        subscribed_sellers = random.sample(sellers, int(len(sellers) * 0.4))
        
        for seller in subscribed_sellers:
            # Choose plan type (70% basic, 30% pro)
            plan_type = SubscriptionPlan.basic if random.random() < 0.7 else SubscriptionPlan.pro
            
            # Plan details
            if plan_type == SubscriptionPlan.basic:
                credits_purchased = 5
                credits_remaining = random.randint(0, 5)
            else:
                credits_purchased = 15
                credits_remaining = random.randint(0, 15)
            
            # Purchase date (within last 3 months)
            purchased_at = datetime.utcnow() - timedelta(days=random.randint(1, 90))
            expires_at = purchased_at + timedelta(days=90)  # 3 month validity
            
            subscription = SellerSubscription(
                seller_id=seller.seller_id,
                plan_type=plan_type,
                credits_purchased=credits_purchased,
                credits_remaining=credits_remaining,
                purchased_at=purchased_at,
                expires_at=expires_at
            )
            
            # Update seller's credit count
            seller.subscription_credits = credits_remaining
            
            subscriptions.append(subscription)
            self.db_session.add(subscription)
        
        self.db_session.commit()
        return subscriptions
    
    def _generate_mock_rfqs(self, num_rfqs: int) -> List[MockRFQ]:
        """Generate mock RFQ data for testing."""
        rfqs = []
        
        # RFQ templates
        rfq_templates = [
            {
                "title": "Medical Equipment for Hospital Setup",
                "description": "Required: X-Ray machines, hospital beds, monitoring equipment, ventilators",
                "categories": ["Medical Equipment", "Healthcare Supplies"],
                "quantity": "Multiple items - see description",
                "division": "Healthcare"
            },
            {
                "title": "Office IT Equipment Procurement",
                "description": "Required: Laptops, printers, networking equipment, UPS systems",
                "categories": ["Electronics", "IT Equipment"],
                "quantity": "70 units total",
                "division": "Admin & IT"
            },
            {
                "title": "Industrial Machinery Purchase",
                "description": "Required: CNC machines, conveyor systems, quality control equipment",
                "categories": ["Industrial Equipment"],
                "quantity": "5 machines",
                "division": "Manufacturing"
            },
            {
                "title": "Laboratory Equipment Setup",
                "description": "Required: Microscopes, centrifuges, analytical instruments, safety equipment",
                "categories": ["Laboratory Equipment", "Medical Equipment"],
                "quantity": "Complete lab setup",
                "division": "Research"
            },
            {
                "title": "Office Furniture Procurement",
                "description": "Required: Desks, chairs, meeting tables, storage cabinets",
                "categories": ["Furniture", "Office Supplies"],
                "quantity": "200 units",
                "division": "Admin & IT"
            },
            {
                "title": "Security Systems Installation",
                "description": "Required: CCTV cameras, access control systems, alarm systems",
                "categories": ["Security Systems", "Electronics"],
                "quantity": "Complete security setup",
                "division": "Security"
            },
            {
                "title": "HVAC Systems for Building",
                "description": "Required: Air conditioning units, ventilation systems, temperature controls",
                "categories": ["HVAC Systems"],
                "quantity": "Building-wide installation",
                "division": "Infrastructure"
            },
            {
                "title": "Educational Supplies for Institute",
                "description": "Required: Projectors, whiteboards, audio systems, computers",
                "categories": ["Educational Supplies", "Electronics"],
                "quantity": "50 classrooms",
                "division": "Education"
            }
        ]
        
        for i in range(num_rfqs):
            # Select random template or create variation
            template = random.choice(rfq_templates)
            
            # Select random delivery city
            delivery_city = random.choice(self.cities)
            
            # Generate deadline (1-60 days from now)
            deadline = date.today() + timedelta(days=random.randint(7, 60))
            
            # Add some variation to title
            title_variations = [
                template["title"],
                f"Urgent: {template['title']}",
                f"Bulk {template['title']}",
                f"Premium {template['title']}"
            ]
            
            rfq = MockRFQ(
                rfq_title=random.choice(title_variations),
                rfq_description=template["description"],
                categories=template["categories"],
                delivery_location=delivery_city,
                quantity_info=template["quantity"],
                deadline=deadline,
                division=template["division"],
                status=RFQStatus.ready  # Ready for processing
            )
            
            rfqs.append(rfq)
            self.db_session.add(rfq)
        
        self.db_session.commit()
        
        # Refresh RFQs to get generated IDs
        for rfq in rfqs:
            self.db_session.refresh(rfq)
        
        return rfqs
    
    def _get_seller_distribution(self, sellers: List[Seller]) -> Dict[str, Any]:
        """Get seller distribution statistics."""
        city_count = {}
        ranking_count = {}
        category_count = {}
        
        for seller in sellers:
            # City distribution
            city = seller.location["city"]
            city_count[city] = city_count.get(city, 0) + 1
            
            # Ranking distribution
            ranking = seller.ranking.value
            ranking_count[ranking] = ranking_count.get(ranking, 0) + 1
            
            # Category distribution
            for category in seller.categories:
                category_count[category] = category_count.get(category, 0) + 1
        
        return {
            "cities": city_count,
            "rankings": ranking_count,
            "categories": category_count,
            "with_credits": len([s for s in sellers if s.subscription_credits > 0]),
            "without_credits": len([s for s in sellers if s.subscription_credits == 0])
        }
    
    def _get_rfq_distribution(self, rfqs: List[MockRFQ]) -> Dict[str, Any]:
        """Get RFQ distribution statistics."""
        city_count = {}
        category_count = {}
        division_count = {}
        
        for rfq in rfqs:
            # City distribution
            city = rfq.delivery_location["city"]
            city_count[city] = city_count.get(city, 0) + 1
            
            # Category distribution
            for category in rfq.categories:
                category_count[category] = category_count.get(category, 0) + 1
            
            # Division distribution
            division = rfq.division
            division_count[division] = division_count.get(division, 0) + 1
        
        return {
            "cities": city_count,
            "categories": category_count,
            "divisions": division_count
        }

def main():
    """Main function for command line usage."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate mock data for seller recommendation system")
    parser.add_argument("--clear", action="store_true", help="Clear existing data before generating")
    parser.add_argument("--sellers", type=int, default=60, help="Number of sellers to generate")
    parser.add_argument("--rfqs", type=int, default=20, help="Number of RFQs to generate")
    
    args = parser.parse_args()
    
    generator = MockDataGenerator()
    result = generator.generate_all_data(
        num_sellers=args.sellers,
        num_rfqs=args.rfqs,
        clear_existing=args.clear
    )
    
    if result["success"]:
        print("\n📊 Generation Summary:")
        print(f"   Time taken: {result['generation_time_seconds']:.2f} seconds")
        print(f"   Sellers created: {result['data_created']['sellers']}")
        print(f"   RFQs created: {result['data_created']['rfqs']}")
        print(f"   Subscriptions created: {result['data_created']['subscriptions']}")
        
        print("\n🏙️ Seller Distribution by City:")
        for city, count in sorted(result['seller_distribution']['cities'].items()):
            print(f"   {city}: {count}")
        
        print("\n🏆 Seller Distribution by Ranking:")
        for ranking, count in sorted(result['seller_distribution']['rankings'].items()):
            print(f"   {ranking}: {count}")
            
        print("\n💳 Subscription Status:")
        print(f"   With credits: {result['seller_distribution']['with_credits']}")
        print(f"   Without credits: {result['seller_distribution']['without_credits']}")
        
    else:
        print(f"\n❌ Generation failed: {result['error']}")
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())