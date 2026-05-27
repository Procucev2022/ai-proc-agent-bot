#!/usr/bin/env python3
"""
Script to create sample category mapping data for auto-categorization testing.

This script populates the category_mappings table with sample data across all divisions
and generates embeddings in ChromaDB for vector similarity search.
"""

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.auto_categorization_service import AutoCategorizationService
from app.database import get_db_session
from app.models import CategoryMapping

def create_sample_data():
    """Create sample category mapping data."""
    
    # Simplified category-item mappings (2-level structure)
    sample_categories = [
        # Admin & IT items
        {"category": "Admin & IT", "item": "pens"},
        {"category": "Admin & IT", "item": "pencils"},
        {"category": "Admin & IT", "item": "markers"},
        {"category": "Admin & IT", "item": "highlighters"},
        {"category": "Admin & IT", "item": "erasers"},
        {"category": "Admin & IT", "item": "notebooks"},
        {"category": "Admin & IT", "item": "sticky notes"},
        {"category": "Admin & IT", "item": "laptops"},
        {"category": "Admin & IT", "item": "desktop computers"},
        {"category": "Admin & IT", "item": "tablets"},
        {"category": "Admin & IT", "item": "monitors"},
        {"category": "Admin & IT", "item": "keyboards"},
        {"category": "Admin & IT", "item": "mouse"},
        {"category": "Admin & IT", "item": "software licenses"},
        {"category": "Admin & IT", "item": "antivirus"},
        {"category": "Admin & IT", "item": "MS Office"},
        {"category": "Admin & IT", "item": "Windows license"},
        {"category": "Admin & IT", "item": "cloud subscriptions"},
        
        # CAPEX Equipment items
        {"category": "CAPEX Equipment", "item": "CNC lathe"},
        {"category": "CAPEX Equipment", "item": "CNC milling machine"},
        {"category": "CAPEX Equipment", "item": "CNC router"},
        {"category": "CAPEX Equipment", "item": "machining center"},
        {"category": "CAPEX Equipment", "item": "drill machine"},
        {"category": "CAPEX Equipment", "item": "angle grinder"},
        {"category": "CAPEX Equipment", "item": "welding machine"},
        {"category": "CAPEX Equipment", "item": "cutting machine"},
        {"category": "CAPEX Equipment", "item": "compressor"},
        
        # Civil Works items
        {"category": "Civil Works", "item": "cement"},
        {"category": "Civil Works", "item": "steel bars"},
        {"category": "Civil Works", "item": "bricks"},
        {"category": "Civil Works", "item": "tiles"},
        {"category": "Civil Works", "item": "paint"},
        {"category": "Civil Works", "item": "concrete blocks"},
        
        # Electrical items
        {"category": "Electrical", "item": "LED lights"},
        {"category": "Electrical", "item": "ceiling fans"},
        {"category": "Electrical", "item": "switches"},
        {"category": "Electrical", "item": "electrical panels"},
        {"category": "Electrical", "item": "junction boxes"},
        {"category": "Electrical", "item": "electrical cables"},
        {"category": "Electrical", "item": "copper wire"},
        {"category": "Electrical", "item": "fiber optic cable"},
        {"category": "Electrical", "item": "power cables"},
        {"category": "Electrical", "item": "network cables"},
        {"category": "Electrical", "item": "temperature sensor"},
        {"category": "Electrical", "item": "pressure sensor"},
        {"category": "Electrical", "item": "flow meter"},
        {"category": "Electrical", "item": "level indicator"},
        {"category": "Electrical", "item": "control valve"},
        
        # Logistics items
        {"category": "Logistics", "item": "truck tires"},
        {"category": "Logistics", "item": "engine oil"},
        {"category": "Logistics", "item": "brake pads"},
        {"category": "Logistics", "item": "batteries"},
        {"category": "Logistics", "item": "spare parts"},
        {"category": "Logistics", "item": "storage racks"},
        {"category": "Logistics", "item": "pallets"},
        {"category": "Logistics", "item": "forklifts"},
        {"category": "Logistics", "item": "warehouse shelving"},
        {"category": "Logistics", "item": "containers"},
        
        # Mechanical items
        {"category": "Mechanical", "item": "ball bearings"},
        {"category": "Mechanical", "item": "roller bearings"},
        {"category": "Mechanical", "item": "bolts"},
        {"category": "Mechanical", "item": "nuts"},
        {"category": "Mechanical", "item": "screws"},
        {"category": "Mechanical", "item": "washers"},
        {"category": "Mechanical", "item": "centrifugal pump"},
        {"category": "Mechanical", "item": "gear pump"},
        {"category": "Mechanical", "item": "diaphragm pump"},
        {"category": "Mechanical", "item": "water pump"},
        {"category": "Mechanical", "item": "hydraulic pump"},
        
        # Safety items
        {"category": "Safety", "item": "safety helmets"},
        {"category": "Safety", "item": "safety shoes"},
        {"category": "Safety", "item": "gloves"},
        {"category": "Safety", "item": "safety goggles"},
        {"category": "Safety", "item": "reflective vests"},
        {"category": "Safety", "item": "ear plugs"},
        {"category": "Safety", "item": "fire extinguisher"},
        {"category": "Safety", "item": "fire alarm"},
        {"category": "Safety", "item": "smoke detector"},
        {"category": "Safety", "item": "fire hose"},
        {"category": "Safety", "item": "emergency exit signs"},
        
        # Packaging items
        {"category": "Packaging", "item": "cardboard boxes"},
        {"category": "Packaging", "item": "plastic containers"},
        {"category": "Packaging", "item": "bubble wrap"},
        {"category": "Packaging", "item": "packing tape"},
        {"category": "Packaging", "item": "foam padding"},
        
        # Services items
        {"category": "Services", "item": "engineering consultation"},
        {"category": "Services", "item": "technical advisory"},
        {"category": "Services", "item": "project management"},
        {"category": "Services", "item": "design services"},
        
        # Raw Materials items
        {"category": "Raw Materials", "item": "steel sheets"},
        {"category": "Raw Materials", "item": "iron rods"},
        {"category": "Raw Materials", "item": "aluminum plates"},
        {"category": "Raw Materials", "item": "copper sheets"},
        {"category": "Raw Materials", "item": "stainless steel"},
        {"category": "Raw Materials", "item": "sulfuric acid"},
        {"category": "Raw Materials", "item": "sodium hydroxide"},
        {"category": "Raw Materials", "item": "industrial solvents"},
        {"category": "Raw Materials", "item": "cleaning chemicals"}
    ]
    
    # Initialize auto-categorization service
    auto_cat_service = AutoCategorizationService()
    
    # Add sample data
    print("Creating sample category mapping data...")
    
    db = get_db_session()
    try:
        # Clear existing data (optional)
        existing_count = db.query(CategoryMapping).count()
        print(f"Found {existing_count} existing category mappings")
        
        created_count = 0
        for category_data in sample_categories:
            try:
                mapping_id = auto_cat_service.add_category_mapping(
                    category=category_data['category'],
                    item=category_data['item']
                )
                created_count += 1
                print(f"✅ Created: {category_data['category']} → {category_data['item']}")
            except Exception as e:
                print(f"❌ Failed to create {category_data['category']} → {category_data['item']}: {e}")
        
        print(f"\n📊 Summary:")
        print(f"- Created {created_count} category mappings")
        print(f"- Total embeddings generated: {created_count}")
        
        # Get collection stats
        stats = auto_cat_service.get_collection_stats()
        print(f"- ChromaDB collection: {stats}")
        
    finally:
        db.close()

def test_categorization():
    """Test the categorization with sample items."""
    
    auto_cat_service = AutoCategorizationService()
    
    test_items = [
        "blue ballpoint pens for office use",
        "Dell laptop for programming",
        "CNC lathe machine", 
        "safety helmet for construction workers",
        "fire extinguisher for office",
        "cardboard shipping boxes",
        "steel sheets for fabrication"
    ]
    
    print("\n🧪 Testing categorization...")
    
    for item in test_items:
        try:
            result = auto_cat_service.categorize_item(
                item_description=item,
                user_id="test_user",
                session_id="test_session"
            )
            
            if result["success"]:
                print(f"✅ '{item}' → {result['category']} (confidence: {result['confidence_score']:.2f})")
                print(f"   Reasoning: {result['reasoning'][:100]}...")
            else:
                print(f"❌ '{item}' → Failed: {result.get('reason', 'Unknown error')}")
                
        except Exception as e:
            print(f"❌ '{item}' → Error: {e}")
    
if __name__ == "__main__":
    print("🚀 Setting up auto-categorization sample data...\n")
    
    # Create sample data and embeddings
    create_sample_data()
    
    # Test categorization
    test_categorization()
    
    print("\n✅ Sample data setup complete!")
    print("\nNext steps:")
    print("1. Run the application")
    print("2. Test auto-categorization in RFQ creation flow")
    print("3. Check auto_categorization_log table for results")