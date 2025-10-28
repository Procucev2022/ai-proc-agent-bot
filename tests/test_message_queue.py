"""
Test script for MessageQueueService rewrite.

Tests basic functionality:
1. Message enqueueing
2. Batch creation
3. Queue status
4. Cleanup

Usage:
    python test_message_queue.py
"""

import asyncio
import time
from typing import Dict

# Mock dependencies for testing
class MockSettings:
    redis_url = "redis://localhost:6379"
    batch_window_seconds = 3
    please_wait_threshold_seconds = 15

class MockWhatsAppService:
    """Mock WhatsApp service for testing"""
    
    async def send_message(self, recipient_id: str, message: str):
        print(f"[MOCK_WHATSAPP] Sending to {recipient_id}: {message[:50]}...")
        
        class MockResult:
            success = True
            message_id = f"mock_{int(time.time())}"
        
        return MockResult()
    
    async def send_configurable_buttons(self, *args, **kwargs):
        print(f"[MOCK_WHATSAPP] Sending buttons...")
        class MockResult:
            success = True
            message_id = f"mock_{int(time.time())}"
        return MockResult()

# Mock config
def get_settings():
    return MockSettings()

# Replace imports
import sys
from unittest.mock import MagicMock

sys.modules['app.config'] = MagicMock(get_settings=get_settings)
sys.modules['app.services.whatsapp_service'] = MagicMock(WhatsAppService=MockWhatsAppService)

# Now import the service
from message_queue_service import MessageQueueService


async def test_basic_flow():
    """Test basic enqueueing and batch creation"""
    print("\n" + "="*60)
    print("TEST: Basic Flow")
    print("="*60)
    
    service = MessageQueueService()
    user_phone = "1234567890"
    
    try:
        # Clean up any existing state
        await service.cleanup_user_state(user_phone)
        
        # Test 1: Enqueue single message
        print("\n[TEST] Enqueueing single message...")
        webhook_data = {
            "from": user_phone,
            "message_id": "msg1",
            "type": "text",
            "content": "Hello, I need help",
            "timestamp": time.time()
        }
        
        await service.enqueue_message(webhook_data)
        
        # Check status
        status = await service.get_queue_status(user_phone)
        print(f"[TEST] Status after enqueue: incoming={status['incoming_queue_size']}, "
              f"timer_active={status['timer_active']}")
        
        assert status['incoming_queue_size'] == 1, "Should have 1 message in incoming queue"
        assert status['timer_active'], "Timer should be active"
        
        # Test 2: Wait for batch creation
        print(f"\n[TEST] Waiting {service.batch_window + 1}s for batch creation...")
        await asyncio.sleep(service.batch_window + 1)
        
        # Give poller time to run
        await asyncio.sleep(2)
        
        status = await service.get_queue_status(user_phone)
        print(f"[TEST] Status after timer: incoming={status['incoming_queue_size']}, "
              f"outgoing={status['outgoing_queue_size']}")
        
        assert status['incoming_queue_size'] == 0, "Incoming queue should be empty"
        assert status['outgoing_queue_size'] == 1, "Should have 1 batch in outgoing queue"
        
        print("\n✅ Basic flow test passed!")
    
    finally:
        await service.cleanup_user_state(user_phone)
        await service.shutdown()


async def test_multiple_messages():
    """Test multiple messages in quick succession"""
    print("\n" + "="*60)
    print("TEST: Multiple Messages")
    print("="*60)
    
    service = MessageQueueService()
    user_phone = "1234567891"
    
    try:
        await service.cleanup_user_state(user_phone)
        
        # Enqueue 3 messages quickly
        print("\n[TEST] Enqueueing 3 messages...")
        for i in range(3):
            webhook_data = {
                "from": user_phone,
                "message_id": f"msg{i}",
                "type": "text",
                "content": f"Message {i}",
                "timestamp": time.time()
            }
            await service.enqueue_message(webhook_data)
            await asyncio.sleep(0.5)  # Small delay
        
        status = await service.get_queue_status(user_phone)
        print(f"[TEST] Status after 3 messages: incoming={status['incoming_queue_size']}")
        
        assert status['incoming_queue_size'] == 3, "Should have 3 messages in queue"
        
        # Wait for batch
        print(f"\n[TEST] Waiting for batch creation...")
        await asyncio.sleep(service.batch_window + 2)
        
        status = await service.get_queue_status(user_phone)
        print(f"[TEST] Status after batch: incoming={status['incoming_queue_size']}, "
              f"outgoing={status['outgoing_queue_size']}")
        
        assert status['incoming_queue_size'] == 0, "Incoming should be empty"
        assert status['outgoing_queue_size'] == 1, "Should have 1 batch"
        
        print("\n✅ Multiple messages test passed!")
    
    finally:
        await service.cleanup_user_state(user_phone)
        await service.shutdown()


async def test_batch_concatenation():
    """Test that batch properly concatenates messages"""
    print("\n" + "="*60)
    print("TEST: Batch Concatenation")
    print("="*60)
    
    service = MessageQueueService()
    user_phone = "1234567892"
    
    try:
        await service.cleanup_user_state(user_phone)
        
        # Enqueue messages with known content
        messages = ["First message", "Second message", "Third message"]
        print(f"\n[TEST] Enqueueing {len(messages)} messages...")
        
        for i, content in enumerate(messages):
            webhook_data = {
                "from": user_phone,
                "message_id": f"msg{i}",
                "type": "text",
                "content": content,
                "timestamp": time.time() + i  # Ensure ordering
            }
            await service.enqueue_message(webhook_data)
        
        # Wait for batch
        await asyncio.sleep(service.batch_window + 2)
        
        # Get batch from outgoing queue
        outgoing_key = service._key_outgoing(user_phone)
        batch_json = await service.redis.lindex(outgoing_key, 0)
        
        if batch_json:
            import json
            batch_dict = json.loads(batch_json)
            content = batch_dict['concatenated_content']
            expected = "\n".join(messages)
            
            print(f"[TEST] Batch content: '{content}'")
            print(f"[TEST] Expected: '{expected}'")
            
            assert content == expected, "Batch should concatenate messages with newlines"
            assert batch_dict['message_count'] == 3, "Batch should have 3 messages"
            
            print("\n✅ Batch concatenation test passed!")
        else:
            raise AssertionError("No batch found in outgoing queue")
    
    finally:
        await service.cleanup_user_state(user_phone)
        await service.shutdown()


async def test_health_metrics():
    """Test health metrics endpoint"""
    print("\n" + "="*60)
    print("TEST: Health Metrics")
    print("="*60)
    
    service = MessageQueueService()
    user_phone = "1234567893"
    
    try:
        await service.cleanup_user_state(user_phone)
        
        # Enqueue some messages
        print("\n[TEST] Setting up test data...")
        for i in range(2):
            webhook_data = {
                "from": user_phone,
                "message_id": f"msg{i}",
                "type": "text",
                "content": f"Message {i}",
                "timestamp": time.time()
            }
            await service.enqueue_message(webhook_data)
        
        # Get metrics
        metrics = await service.get_health_metrics()
        
        print(f"\n[TEST] Health metrics:")
        print(f"  - Active users: {metrics['active_users']}")
        print(f"  - Processing count: {metrics['processing_count']}")
        print(f"  - Total incoming: {metrics['total_incoming']}")
        print(f"  - Total outgoing: {metrics['total_outgoing']}")
        
        assert metrics['total_incoming'] >= 2, "Should have at least 2 messages"
        assert 'timestamp' in metrics, "Should have timestamp"
        
        print("\n✅ Health metrics test passed!")
    
    finally:
        await service.cleanup_user_state(user_phone)
        await service.shutdown()


async def run_all_tests():
    """Run all tests"""
    print("\n" + "="*60)
    print("MESSAGE QUEUE SERVICE - TEST SUITE")
    print("="*60)
    
    tests = [
        ("Basic Flow", test_basic_flow),
        ("Multiple Messages", test_multiple_messages),
        ("Batch Concatenation", test_batch_concatenation),
        ("Health Metrics", test_health_metrics),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            await test_func()
            passed += 1
        except Exception as e:
            print(f"\n❌ {name} test FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        
        # Small delay between tests
        await asyncio.sleep(1)
    
    print("\n" + "="*60)
    print(f"TEST RESULTS: {passed} passed, {failed} failed")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(run_all_tests())
