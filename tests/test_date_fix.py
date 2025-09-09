#!/usr/bin/env python3
"""
Simple test to verify the date extraction fix for "10th sept" issue.
"""

import sys
import os
from datetime import datetime

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(__file__))

def test_date_extraction_fix():
    """Test that '10th sept' extracts as 2025-09-10, not 2024-09-10."""
    
    print("Testing Date Extraction Fix")
    print("=" * 40)
    print(f"Current date: {datetime.now().strftime('%Y-%m-%d')}")
    current_year = datetime.now().year
    print(f"Current year: {current_year}")
    print(f"Expected extraction: {current_year}-09-10")
    print()
    
    # Test cases
    test_cases = [
        {
            "message": "i want to buy 10 laptops, delivered by 10th sept",
            "description": "Partial date (should use current year)",
            "expected_year": current_year
        },
        {
            "message": "i want to buy 5 chairs on 15 dec 2023",
            "description": "Explicit past year (should be rejected)",
            "expected_year": None  # Should be rejected
        },
        {
            "message": "i want to buy 3 tables by 20 jan 2026",
            "description": "Explicit future year (should be accepted)",
            "expected_year": 2026
        }
    ]
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['description']}")
        print(f"Message: '{test_case['message']}'")
        print()
    
    try:
        from app.services.openai_service import OpenAIService
        
        # Initialize OpenAI service
        openai_service = OpenAIService()
        
        for test_case in test_cases:
            print("Testing entity extraction...")
            result = openai_service.extract_entities(test_case["message"], workflow_type="buy_something")
        
            print("Extraction result:")
            if result.get("success") and "products" in result:
                for j, product in enumerate(result["products"]):
                    print(f"  Product {j+1}:")
                    print(f"    Description: {product.get('description')}")
                    print(f"    Quantity: {product.get('quantity')}")
                    print(f"    Delivery Date: {product.get('deliveryDate')}")
                    
                    # Check date validation error
                    if product.get('date_validation_error'):
                        print(f"    ⚠️  DATE ERROR: {product.get('date_validation_error')}")
                        if test_case["expected_year"] is None:
                            print(f"    ✅ CORRECT: Past date rejected as expected")
                        else:
                            print(f"    ❌ INCORRECT: Should not have date error")
                    else:
                        # Check if the date is correct
                        delivery_date = product.get('deliveryDate')
                        if delivery_date:
                            expected_year = test_case["expected_year"]
                            if expected_year and delivery_date.startswith(str(expected_year)):
                                print(f"    ✅ CORRECT: Date extracted as {delivery_date} ({expected_year})")
                            elif not expected_year:
                                print(f"    ❌ INCORRECT: Should have been rejected but got {delivery_date}")
                            else:
                                print(f"    ❌ INCORRECT: Expected {expected_year} but got {delivery_date}")
                        else:
                            if test_case["expected_year"] is None:
                                print(f"    ✅ CORRECT: No date extracted (past date rejected)")
                            else:
                                print(f"    ❌ INCORRECT: No delivery date extracted")
            else:
                print(f"  ❌ FAILED: {result}")
            
            print("-" * 50)

        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
    
    print()
    print("Test completed!")

if __name__ == "__main__":
    test_date_extraction_fix()