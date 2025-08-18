#!/usr/bin/env python3
"""
Interactive RFQ to Seller Matching Test Script

This script allows you to test the complete RFQ recommendation flow
with different scenarios and see detailed results.
"""

import asyncio
import sys
import json
from datetime import datetime, date, timedelta
from typing import Dict, Any

# Add the app directory to the path
sys.path.insert(0, 'app')

from app.services.seller_recommendation_service import SellerRecommendationService
from app.services.rfq_background_service import RFQBackgroundService
from app.database import get_db_session
from app.models import Seller, MockRFQ, RFQStatus

class RFQMatchingTester:
    """Interactive tester for RFQ to seller matching."""
    
    def __init__(self):
        self.service = SellerRecommendationService()
        self.background_service = RFQBackgroundService()
        self.db = get_db_session()
    
    async def test_predefined_scenarios(self):
        """Test predefined RFQ scenarios."""
        print("🚀 Testing Predefined RFQ Scenarios\n")
        
        scenarios = [
            {
                "name": "🏥 Healthcare Equipment - Bangalore",
                "rfq": {
                    "rfq_id": "HEALTH_BLR_001",
                    "rfq_title": "Hospital Medical Equipment",
                    "categories": ["Medical Equipment", "Healthcare Supplies"],
                    "delivery_location": {
                        "state": "Karnataka",
                        "city": "Bangalore", 
                        "pincode": "560001"
                    },
                    "quantity_info": "Various medical devices",
                    "division": "Healthcare"
                }
            },
            {
                "name": "💻 IT Hardware - Mumbai",  
                "rfq": {
                    "rfq_id": "IT_MUM_001",
                    "rfq_title": "Office IT Infrastructure",
                    "categories": ["IT Hardware", "Electronics", "Office Equipment"],
                    "delivery_location": {
                        "state": "Maharashtra",
                        "city": "Mumbai",
                        "pincode": "400001"  
                    },
                    "quantity_info": "100 laptops, servers, networking",
                    "division": "IT"
                }
            },
            {
                "name": "🏭 Industrial Equipment - Chennai",
                "rfq": {
                    "rfq_id": "IND_CHE_001", 
                    "rfq_title": "Manufacturing Equipment",
                    "categories": ["Industrial Equipment", "Manufacturing", "Heavy Machinery"],
                    "delivery_location": {
                        "state": "Tamil Nadu",
                        "city": "Chennai",
                        "pincode": "600001"
                    },
                    "quantity_info": "Production line equipment",
                    "division": "Manufacturing"
                }
            },
            {
                "name": "🏢 Office Supplies - Delhi",
                "rfq": {
                    "rfq_id": "OFF_DEL_001",
                    "rfq_title": "Corporate Office Setup", 
                    "categories": ["Office Equipment", "Furniture", "Stationery"],
                    "delivery_location": {
                        "state": "Delhi",
                        "city": "Delhi",
                        "pincode": "110001"
                    },
                    "quantity_info": "Complete office setup",
                    "division": "Administration"
                }
            }
        ]
        
        for i, scenario in enumerate(scenarios, 1):
            print(f"{i}. {scenario['name']}")
            print("=" * 60)
            
            result = await self.service.select_sellers_for_rfq(scenario['rfq'])
            
            print(f"📊 Results for {scenario['rfq']['rfq_id']}:")
            print(f"   Total Selected: {result['total_selected']} sellers")
            print(f"   Subscribed: {len(result['subscribed_sellers'])} sellers")  
            print(f"   Unsubscribed: {len(result['unsubscribed_sellers'])} sellers")
            
            # Show selection metadata
            metadata = result['selection_metadata']
            print(f"   Categories: {', '.join(metadata['categories_searched'])}")
            print(f"   Location: {metadata['delivery_location']['city']}, {metadata['delivery_location']['state']}")
            print(f"   Geographic filtering: {metadata['initial_category_matches']} → {metadata['geo_filtered_count']} sellers")
            
            # Show top sellers
            all_sellers = result['subscribed_sellers'] + result['unsubscribed_sellers']
            if all_sellers:
                print(f"\\n   🏆 Top Sellers:")
                for j, seller in enumerate(all_sellers[:5], 1):
                    credits_info = f"({seller['subscription_credits']} credits)" if seller['subscription_credits'] > 0 else "(no credits)"
                    distance_info = f"{seller.get('distance_km', '?'):.1f}km" if seller.get('distance_km') else "distance unknown"
                    print(f"      {j}. {seller['seller_name']} - {seller['ranking']} {credits_info} - {distance_info}")
            else:
                print("   ❌ No sellers found for this RFQ")
                
            print("\\n")
    
    async def test_custom_rfq(self):
        """Test a custom RFQ scenario."""
        print("🎯 Custom RFQ Testing")
        print("=" * 40)
        
        # Custom RFQ - you can modify this
        custom_rfq = {
            "rfq_id": "CUSTOM_TEST_001",
            "rfq_title": "Custom Test RFQ",
            "categories": ["Medical Equipment"],  # Modify categories here
            "delivery_location": {
                "state": "Karnataka",      # Modify location here
                "city": "Bangalore", 
                "pincode": "560001"
            },
            "quantity_info": "Custom quantity",
            "division": "Custom Division"
        }
        
        print(f"Testing Custom RFQ: {custom_rfq['rfq_title']}")
        print(f"Categories: {', '.join(custom_rfq['categories'])}")
        print(f"Location: {custom_rfq['delivery_location']['city']}, {custom_rfq['delivery_location']['state']}")
        print()
        
        result = await self.service.select_sellers_for_rfq(custom_rfq)
        
        print(f"📊 Custom RFQ Results:")
        print(f"   Selected: {result['total_selected']} sellers")
        
        # Detailed breakdown
        for seller_type, sellers in [("Subscribed", result['subscribed_sellers']), ("Unsubscribed", result['unsubscribed_sellers'])]:
            if sellers:
                print(f"\\n   {seller_type} Sellers ({len(sellers)}):")
                for seller in sellers:
                    print(f"      • {seller['seller_name']} ({seller['ranking']}) - {seller.get('distance_km', '?'):.1f}km")
    
    async def test_background_processing(self):
        """Test the complete background RFQ processing."""
        print("⚙️ Testing Background RFQ Processing")
        print("=" * 45)
        
        # Create a mock RFQ in the database
        test_rfq = MockRFQ(
            rfq_id="BG_TEST_001",
            rfq_title="Background Processing Test",
            rfq_description="Testing complete background processing",
            categories=["Medical Equipment", "Healthcare Supplies"],
            delivery_location={
                "state": "Karnataka",
                "city": "Bangalore",
                "pincode": "560001"
            },
            quantity_info="Test quantities",
            deadline=date.today() + timedelta(days=30),
            division="Healthcare",
            status=RFQStatus.ready
        )
        
        # Check if already exists
        existing = self.db.query(MockRFQ).filter(MockRFQ.rfq_id == "BG_TEST_001").first()
        if existing:
            self.db.delete(existing)
            self.db.commit()
        
        self.db.add(test_rfq)
        self.db.commit()
        
        print(f"Created RFQ: {test_rfq.rfq_id}")
        
        # Process it through background service
        result = await self.background_service.process_approved_rfq("BG_TEST_001")
        
        print(f"\\n📊 Background Processing Results:")
        print(f"   Success: {result['success']}")
        print(f"   Sellers Selected: {result.get('sellers_selected', 0)}")
        print(f"   Notifications Sent: {result.get('notifications_sent', 0)}")
        print(f"   Processing Time: {result.get('processing_time_seconds', 0):.2f}s")
        
        # Cleanup
        self.db.delete(test_rfq)
        self.db.commit()
    
    async def show_database_stats(self):
        """Show current database statistics."""
        print("📊 Database Statistics")
        print("=" * 30)
        
        seller_count = self.db.query(Seller).count()
        subscribed_count = self.db.query(Seller).filter(Seller.subscription_credits > 0).count()
        rfq_count = self.db.query(MockRFQ).count()
        
        print(f"   Total Sellers: {seller_count}")
        print(f"   Subscribed Sellers: {subscribed_count}")
        print(f"   Unsubscribed Sellers: {seller_count - subscribed_count}")
        print(f"   Mock RFQs: {rfq_count}")
        
        # City distribution
        from sqlalchemy import func
        city_stats = self.db.query(
            func.json_extract(Seller.location, '$.city').label('city'),
            func.count().label('count')
        ).group_by(func.json_extract(Seller.location, '$.city')).all()
        
        print(f"\\n   🏙️ Sellers by City:")
        for city, count in sorted(city_stats, key=lambda x: x[1], reverse=True)[:10]:
            print(f"      {city}: {count} sellers")
    
    async def run_all_tests(self):
        """Run all tests."""
        print("🧪 COMPLETE RFQ TO SELLER MATCHING TEST")
        print("=" * 50)
        print()
        
        await self.show_database_stats()
        print()
        
        await self.test_predefined_scenarios()
        
        await self.test_custom_rfq()
        print()
        
        await self.test_background_processing()
        print()
        
        print("✅ All tests completed!")

async def main():
    """Main function."""
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("""
RFQ Matching Test Script

Usage:
    python test_rfq_matching.py [options]
    
Options:
    --scenarios     Run predefined scenarios only
    --custom        Run custom RFQ test only  
    --background    Run background processing test only
    --stats         Show database stats only
    --help          Show this help
    
Default: Run all tests
        """)
        return
    
    tester = RFQMatchingTester()
    
    if len(sys.argv) > 1:
        option = sys.argv[1]
        if option == "--scenarios":
            await tester.test_predefined_scenarios()
        elif option == "--custom":
            await tester.test_custom_rfq()
        elif option == "--background":
            await tester.test_background_processing()
        elif option == "--stats":
            await tester.show_database_stats()
    else:
        await tester.run_all_tests()

if __name__ == "__main__":
    asyncio.run(main())