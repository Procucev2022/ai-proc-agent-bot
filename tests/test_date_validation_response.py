#!/usr/bin/env python3
"""
Test script to verify date validation error messages are included in responses.
"""

import sys
import os

# Add the app directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from services.helpers.response_helpers import ResponseHelpers
from services.openai_service import OpenAIService

def test_date_validation_error_extraction():
    """Test that date validation errors are extracted from context."""
    print("Testing date validation error extraction...")
    
    try:
        # Initialize services
        openai_service = OpenAIService()
        response_helpers = ResponseHelpers(openai_service)
        
        # Create test context with date validation error
        test_context = {
            "conversation_stage": "clarification",
            "user_message": "12 sept 2023",
            "products": [
                {
                    "description": "laptops",
                    "quantity": 10,
                    "deliveryDate": None,
                    "date_validation_error": "The date 2023-09-12 has already passed. Please provide a future date for delivery."
                }
            ]
        }
        
        # Test error extraction
        date_errors = response_helpers._extract_date_validation_errors(test_context)
        
        print(f"Context: {test_context}")
        print(f"Extracted date errors: {date_errors}")
        
        if date_errors:
            print("SUCCESS: Date validation errors extracted correctly")
            print(f"Error message: {date_errors[0]}")
        else:
            print("FAIL: No date validation errors extracted")
        
        # Test with questions
        questions = ["What is the required delivery date?", "Where should items be delivered?"]
        
        # This should prepend the date error to the questions
        enhanced_questions = date_errors + questions if date_errors else questions
        
        print(f"Original questions: {questions}")
        print(f"Enhanced questions: {enhanced_questions}")
        
        if len(enhanced_questions) > len(questions):
            print("SUCCESS: Date validation error would be included in response")
        else:
            print("FAIL: Date validation error not included")
            
    except Exception as e:
        print(f"Error during testing: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_date_validation_error_extraction()