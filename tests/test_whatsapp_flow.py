#!/usr/bin/env python3
"""
WhatsApp Flow Test Script - Simple Orchestrator
Tests the complete flow: Intent → Entity → RFQ → WhatsApp Response
"""

import sys
import asyncio
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.services.chat_service import ChatService


def load_test_inputs():
    """Load test queries from input file."""
    inputs_file = project_root / "tests" / "inputs" / "whatsapp_flow_test_queries.txt"
    queries = []
    
    with open(inputs_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split('|')
                if len(parts) == 3:
                    phone, message, expected = parts
                    queries.append((phone, message, expected))
    
    return queries


async def test_message_flow(chat_service, phone, message, expected):
    """Test a single message through the flow."""
    print(f"\nTesting: {phone} -> {message}")
    print(f"Expected: {expected}")
    
    try:
        result = await chat_service.process_message(
            user_phone=phone,
            message_content=message,
            message_type="text"
        )
        
        print(f"Result: {result.get('status', 'unknown')}")
        if 'completeness' in result:
            print(f"RFQ Completeness: {result['completeness']}%")
        
        return True, result
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False, {"error": str(e)}


async def main():
    """Run WhatsApp flow tests."""
    print("WhatsApp Flow Test")
    print("==================")
    
    # Initialize chat service
    chat_service = ChatService()
    print("Chat service initialized successfully")
    
    # Load test inputs
    queries = load_test_inputs()
    print(f"Loaded {len(queries)} test queries")
    
    # Test just the first query for debugging
    phone, message, expected = queries[0]
    print(f"\nTesting first query: {phone} -> {message}")
    
    try:
        result = await chat_service.process_message(
            user_phone=phone,
            message_content=message,
            message_type="text"
        )
        print(f"Result: {result.get('status', 'unknown')}")
        if 'completeness' in result:
            print(f"RFQ Completeness: {result['completeness']}%")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())