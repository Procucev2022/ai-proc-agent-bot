"""
Test script for the Global Error Handling System.

This script demonstrates how the global error handler works for different
types of technical errors and shows the notification system in action.
"""

import asyncio
import logging
from app.services.global_error_handler import (
    handle_api_error,
    handle_database_error,
    handle_server_error,
    ErrorContext,
    get_global_error_handler
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_api_error():
    """Test API error handling."""
    print("\n=== Testing API Error Handling ===")
    
    await handle_api_error(
        api_name="Payment Gateway API",
        endpoint="/api/v1/payments/process",
        error_message="500 (Internal Server Error - Timeout)",
        payload={"amount": 1000, "currency": "INR"},
        user_phone="919876543210",
        user_name="Test User",
        user_email="test@example.com",
        current_flow="Payment Processing"
    )
    
    print("API error notification sent!")

async def test_database_error():
    """Test database error handling."""
    print("\n=== Testing Database Error Handling ===")
    
    await handle_database_error(
        error_message="Connection timeout to MySQL server at aiproc.mysql.database.azure.com:3306",
        user_phone="919876543210",
        user_name="Test User",
        current_flow="RFQ Creation"
    )
    
    print("Database error notification sent!")

async def test_server_error():
    """Test server error handling."""
    print("\n=== Testing Server Error Handling ===")
    
    await handle_server_error(
        error_message="Unhandled exception: KeyError('required_field')",
        user_phone="919876543210",
        current_flow="User Registration"
    )
    
    print("Server error notification sent!")

async def test_custom_error_context():
    """Test custom error context."""
    print("\n=== Testing Custom Error Context ===")
    
    error_context = ErrorContext(
        error_type="Third-Party Service Error",
        error_message="WhatsApp Business API rate limit exceeded",
        user_phone="919876543210",
        user_name="Test User",
        current_flow="Message Sending",
        api_name="WhatsApp Business API",
        endpoint="/v1/messages",
        payload={"to": "919876543210", "type": "text"}
    )
    
    handler = get_global_error_handler()
    success = await handler.handle_error(error_context)
    
    print(f"Custom error handling {'succeeded' if success else 'failed'}!")

async def main():
    """Run all error handling tests."""
    print("Starting Global Error Handling Tests...")
    print("Note: This will send notifications to the configured support team.")
    
    try:
        await test_api_error()
        await asyncio.sleep(2)  # Small delay between tests
        
        await test_database_error()
        await asyncio.sleep(2)
        
        await test_server_error()
        await asyncio.sleep(2)
        
        await test_custom_error_context()
        
        print("\n=== All Tests Completed ===")
        print("Check your WhatsApp and email for support notifications!")
        
    except Exception as e:
        logger.error(f"Error during testing: {e}")

if __name__ == "__main__":
    asyncio.run(main())