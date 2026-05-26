#!/usr/bin/env python3
"""
Test script to verify date formatting for validation errors.
"""

import sys
import os

# Add the project root to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from app.utils.datetime_utils import format_date_for_validation_error

def test_date_formatting():
    """Test the date formatting function."""
    print("Testing Date Formatting for Validation Errors...")
    
    test_cases = [
        "2025-09-12",  # Should become "12 Sept 2025"
        "2024-12-25",  # Should become "25 Dec 2024"
        "2025-01-01",  # Should become "1 Jan 2025"
        "2023-03-05",  # Should become "5 Mar 2023"
        "invalid-date",  # Should return as-is
        "",  # Should return "N/A"
        None  # Should return "N/A"
    ]
    
    expected_results = [
        "12 Sept 2025",
        "25 Dec 2024", 
        "1 Jan 2025",
        "5 Mar 2023",
        "invalid-date",
        "N/A",
        "N/A"
    ]
    
    print("-" * 50)
    
    for i, test_case in enumerate(test_cases):
        result = format_date_for_validation_error(test_case)
        expected = expected_results[i]
        
        print(f"Input: {test_case}")
        print(f"Output: {result}")
        print(f"Expected: {expected}")
        
        if result == expected:
            print("✅ PASS")
        else:
            print("❌ FAIL")
        
        print("-" * 30)
    
    print("Date formatting test completed!")

if __name__ == "__main__":
    test_date_formatting()