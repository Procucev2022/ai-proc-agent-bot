#!/usr/bin/env python3
"""
Test script to verify pincode lookup integration in RFQ creation.
"""

import asyncio
import sys
import os

# Add the app directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app.services.entity_service import EntityService
from app.utils.pincode_lookup import get_location_from_pincode, get_location_from_pincode_async


async def test_pincode_lookup():
    """Test the pincode lookup functionality."""
    print("Testing Pincode Lookup Integration")
    print("=" * 50)
    
    # Test 1: Direct pincode lookup
    print("\n1. Testing direct pincode lookup:")
    try:
        result = get_location_from_pincode("110001")
        print(f"   Pincode 110001: {result}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Test 2: Async pincode lookup
    print("\n2. Testing async pincode lookup:")
    try:
        result = await get_location_from_pincode_async("400001")
        print(f"   Pincode 400001: {result}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Test 3: EntityService integration
    print("\n3. Testing EntityService integration:")
    try:
        entity_service = EntityService()
        
        # Test products with pincode
        test_products = [
            {
                "description": "Office chairs",
                "quantity": "50",
                "pincode": "560001",
                "deliveryDate": "2024-02-15"
            }
        ]
        
        print(f"   Before auto-fill: {test_products[0]}")
        
        # Test the auto-fill functionality
        updated_products = await entity_service._auto_fill_location_from_pincode(test_products)
        
        print(f"   After auto-fill: {updated_products[0]}")
        
    except Exception as e:
        print(f"   Error: {e}")
    
    # Test 4: Invalid pincode handling
    print("\n4. Testing invalid pincode handling:")
    try:
        result = await get_location_from_pincode_async("999999")
        print(f"   Invalid pincode result: {result}")
    except Exception as e:
        print(f"   Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_pincode_lookup())