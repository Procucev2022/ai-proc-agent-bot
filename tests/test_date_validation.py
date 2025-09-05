#!/usr/bin/env python3
"""
Simple test script to verify date validation functionality.
"""

import sys
import os
from datetime import datetime, timedelta

# Add the app directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from services.openai_service import OpenAIService
from services.entity_service import EntityService

def test_date_validation():
    """Test the date validation functionality."""
    print("Testing Date Validation...")
    
    try:
        # Initialize services
        openai_service = OpenAIService()
        entity_service = EntityService(openai_service)
        
        # Test cases
        test_cases = [
            {
                "name": "Partial date - current year assumption",
                "input": "12 sept",
                "expected_valid": True
            },
            {
                "name": "Relative date - tomorrow",
                "input": "tomorrow",
                "expected_valid": True
            },
            {
                "name": "Relative date - today",
                "input": "today",
                "expected_valid": True
            },
            {
                "name": "Past date - yesterday",
                "input": "yesterday",
                "expected_valid": False
            },
            {
                "name": "Past date - last week",
                "input": "last week",
                "expected_valid": False
            }
        ]
        
        print(f"Current date: {datetime.now().strftime('%Y-%m-%d')}")
        print("-" * 50)
        
        for test_case in test_cases:
            print(f"\nTesting: {test_case['name']}")
            print(f"Input: '{test_case['input']}'")
            
            # Test direct date validation
            result = openai_service.validate_delivery_date(
                raw_date_input=test_case['input']
            )
            
            print(f"Valid: {result.get('is_valid')}")
            print(f"Normalized: {result.get('normalized_date')}")
            print(f"Message: {result.get('user_friendly_message')}")
            print(f"Expected valid: {test_case['expected_valid']}")
            
            if result.get('is_valid') == test_case['expected_valid']:
                print("✅ PASS")
            else:
                print("❌ FAIL")
        
        print("\n" + "=" * 50)
        print("Testing entity extraction with date validation...")
        
        # Test entity extraction with date validation
        test_message = "I need 10 chairs delivered tomorrow"
        print(f"\nExtracting entities from: '{test_message}'")
        
        extraction_result = entity_service.extract_entities(test_message)
        print(f"Extraction result: {extraction_result}")
        
        if "products" in extraction_result:
            for i, product in enumerate(extraction_result["products"]):
                print(f"Product {i+1}:")
                print(f"  Description: {product.get('description')}")
                print(f"  Quantity: {product.get('quantity')}")
                print(f"  Delivery Date: {product.get('deliveryDate')}")
                if product.get('date_validation_error'):
                    print(f"  Date Error: {product.get('date_validation_error')}")
        
        print("\nDate validation test completed!")
        
    except Exception as e:
        print(f"Error during testing: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_date_validation()