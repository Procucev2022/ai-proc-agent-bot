#!/usr/bin/env python3
"""
Simple test script for the learning categorization system.

This script tests the basic functionality of the self-learning 3-level
categorization system by processing a few sample items.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.auto_categorization_service import AutoCategorizationService
from app.services.learning_categorization_service import LearningCategorizationService

def test_learning_system():
    """Test the learning categorization system with sample items."""
    print("🧪 Testing Learning Categorization System")
    print("=" * 50)
    
    # Initialize services
    auto_service = AutoCategorizationService()
    learning_service = LearningCategorizationService()
    
    # Test items
    test_items = [
        "MacBook Pro 16-inch laptop for office work",
        "Industrial safety helmet with chin strap",
        "Wireless mouse and keyboard set",
        "Office printer paper A4 size",
        "LED desk lamp with adjustable brightness"
    ]
    
    print("\n📊 System Status Check:")
    print("-" * 30)
    
    # Check database tables
    try:
        stats = learning_service.get_learning_category_stats()
        print(f"✅ Learning categories: {stats.get('total_learning_categories', 0)}")
        print(f"✅ Learning items: {stats.get('total_learning_items', 0)}")
        print(f"✅ Cross-references: {stats.get('total_cross_references', 0)}")
    except Exception as e:
        print(f"❌ Stats error: {str(e)}")
    
    # Test categorization with learning for each item
    print(f"\n🔄 Testing {len(test_items)} items:")
    print("-" * 30)
    
    for i, item in enumerate(test_items, 1):
        print(f"\n{i}. Testing: '{item}'")
        
        try:
            # Test the enhanced categorization
            result = auto_service.categorize_with_learning(
                item_description=item,
                user_id="test_user",
                session_id=f"test_session_{i}"
            )
            
            if result.get("success"):
                auto_result = result.get("auto_categorization", {})
                learning_result = result.get("learning_categorization", {})
                
                print(f"   📂 Client category: {auto_result.get('category', 'N/A')}")
                
                if learning_result and learning_result.get("success"):
                    learning_cat = learning_result.get("learning_category", {})
                    print(f"   🎯 Learning categories:")
                    print(f"      Level 1: {learning_cat.get('level_1_category', 'N/A')}")
                    print(f"      Level 2: {learning_cat.get('level_2_category', 'N/A')}")
                    print(f"      Level 3: {learning_cat.get('level_3_category', 'N/A')}")
                    print(f"   ✅ Confidence: {learning_cat.get('confidence_score', 0):.2f}")
                    
                    if not learning_result.get("existing", False):
                        print(f"   🆕 Created new learning category")
                    else:
                        print(f"   🔄 Used existing learning category")
                else:
                    print(f"   ❌ Learning categorization failed: {learning_result.get('error', 'Unknown error')}")
            else:
                print(f"   ❌ Categorization failed: {result.get('error', 'Unknown error')}")
                
        except Exception as e:
            print(f"   💥 Error: {str(e)}")
    
    # Final stats
    print(f"\n📈 Final Statistics:")
    print("-" * 30)
    try:
        final_stats = learning_service.get_learning_category_stats()
        print(f"Total learning categories: {final_stats.get('total_learning_categories', 0)}")
        print(f"Total learning items: {final_stats.get('total_learning_items', 0)}")
        print(f"Average confidence: {final_stats.get('average_confidence_score', 0):.2f}")
        
        top_categories = final_stats.get('top_level_1_categories', [])
        if top_categories:
            print(f"Top Level 1 categories:")
            for cat in top_categories[:3]:
                print(f"  - {cat['category']}: {cat['usage_count']} uses")
    except Exception as e:
        print(f"Error getting final stats: {str(e)}")
    
    print(f"\n✅ Test completed!")

if __name__ == "__main__":
    test_learning_system()