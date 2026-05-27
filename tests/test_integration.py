#!/usr/bin/env python3
"""
Quick integration test for Message Queue Service v2.0

Tests the integration with main.py startup/shutdown.
Run this AFTER deploying to verify everything works.

Usage:
    python tests/test_integration.py
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def test_service_import():
    """Test that service can be imported"""
    print("="*60)
    print("TEST 1: Service Import")
    print("="*60)
    
    try:
        from app.services.message_queue_service import MessageQueueService
        print("✅ MessageQueueService imported successfully")
        return True
    except Exception as e:
        print(f"❌ Failed to import: {e}")
        return False


async def test_service_instantiation():
    """Test that service can be instantiated"""
    print("\n" + "="*60)
    print("TEST 2: Service Instantiation")
    print("="*60)
    
    try:
        from app.services.message_queue_service import MessageQueueService
        service = MessageQueueService()
        print("✅ MessageQueueService instantiated successfully")
        
        # Check required methods exist
        assert hasattr(service, 'enqueue_message'), "Missing enqueue_message method"
        assert hasattr(service, 'run_batch_poller'), "Missing run_batch_poller method"
        assert hasattr(service, 'run_monitoring_loop'), "Missing run_monitoring_loop method"
        assert hasattr(service, 'shutdown'), "Missing shutdown method"
        assert hasattr(service, 'get_health_metrics'), "Missing get_health_metrics method"
        assert hasattr(service, 'get_queue_status'), "Missing get_queue_status method"
        
        print("✅ All required methods present")
        
        await service.shutdown()
        return True
    except Exception as e:
        print(f"❌ Failed to instantiate: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_background_tasks():
    """Test that background tasks can start"""
    print("\n" + "="*60)
    print("TEST 3: Background Tasks")
    print("="*60)
    
    try:
        from app.services.message_queue_service import MessageQueueService
        service = MessageQueueService()
        
        # Start background tasks
        poller_task = asyncio.create_task(service.run_batch_poller())
        monitor_task = asyncio.create_task(service.run_monitoring_loop())
        
        print("✅ Background tasks started")
        
        # Let them run briefly
        await asyncio.sleep(2)
        
        # Check they're still running
        assert not poller_task.done(), "Poller task stopped unexpectedly"
        assert not monitor_task.done(), "Monitor task stopped unexpectedly"
        
        print("✅ Background tasks running correctly")
        
        # Shutdown
        await service.shutdown()
        
        # Give tasks time to cancel
        try:
            await asyncio.wait_for(asyncio.gather(poller_task, monitor_task, return_exceptions=True), timeout=5)
        except:
            pass
        
        print("✅ Background tasks shut down successfully")
        return True
        
    except Exception as e:
        print(f"❌ Failed background tasks test: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_redis_connection():
    """Test Redis connection"""
    print("\n" + "="*60)
    print("TEST 4: Redis Connection")
    print("="*60)
    
    try:
        from app.services.message_queue_service import MessageQueueService
        service = MessageQueueService()
        
        # Try a simple Redis operation
        test_key = "test:integration:ping"
        await service.redis.set(test_key, "pong", ex=10)
        value = await service.redis.get(test_key)
        
        assert value == "pong", f"Redis get/set failed: got {value}"
        
        print("✅ Redis connection working")
        
        # Cleanup
        await service.redis.delete(test_key)
        await service.shutdown()
        return True
        
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_basic_enqueue():
    """Test basic message enqueueing"""
    print("\n" + "="*60)
    print("TEST 5: Basic Message Enqueue")
    print("="*60)
    
    try:
        from app.services.message_queue_service import MessageQueueService
        import time
        
        service = MessageQueueService()
        test_user = "9999999999"
        
        # Clean state
        await service.cleanup_user_state(test_user)
        
        # Enqueue a message
        webhook_data = {
            "from": test_user,
            "message_id": "test_msg_1",
            "type": "text",
            "content": "Integration test message",
            "timestamp": time.time()
        }
        
        await service.enqueue_message(webhook_data)
        print("✅ Message enqueued")
        
        # Check queue status
        status = await service.get_queue_status(test_user)
        
        assert status['incoming_queue_size'] == 1, f"Expected 1 message, got {status['incoming_queue_size']}"
        assert status['timer_active'], "Timer should be active"
        
        print(f"✅ Queue status correct: incoming={status['incoming_queue_size']}, timer={status['timer_active']}")
        
        # Cleanup
        await service.cleanup_user_state(test_user)
        await service.shutdown()
        return True
        
    except Exception as e:
        print(f"❌ Enqueue test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_health_metrics():
    """Test health metrics endpoint"""
    print("\n" + "="*60)
    print("TEST 6: Health Metrics")
    print("="*60)
    
    try:
        from app.services.message_queue_service import MessageQueueService
        service = MessageQueueService()
        
        # Get health metrics
        metrics = await service.get_health_metrics()
        
        # Check required fields
        required_fields = ['timestamp', 'active_users', 'processing_count', 'total_incoming', 'total_outgoing']
        for field in required_fields:
            assert field in metrics, f"Missing field: {field}"
        
        print("✅ Health metrics returned successfully")
        print(f"   - Active users: {metrics['active_users']}")
        print(f"   - Processing: {metrics['processing_count']}")
        print(f"   - Incoming: {metrics['total_incoming']}")
        print(f"   - Outgoing: {metrics['total_outgoing']}")
        
        await service.shutdown()
        return True
        
    except Exception as e:
        print(f"❌ Health metrics test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def run_all_tests():
    """Run all integration tests"""
    print("\n" + "="*60)
    print("MESSAGE QUEUE SERVICE - INTEGRATION TESTS")
    print("="*60 + "\n")
    
    tests = [
        test_service_import,
        test_service_instantiation,
        test_redis_connection,
        test_basic_enqueue,
        test_health_metrics,
        test_background_tasks,  # Run last as it's slower
    ]
    
    results = []
    
    for test in tests:
        try:
            result = await test()
            results.append((test.__name__, result))
        except Exception as e:
            print(f"\n❌ Test {test.__name__} crashed: {e}")
            results.append((test.__name__, False))
        
        # Small delay between tests
        await asyncio.sleep(0.5)
    
    # Summary
    print("\n" + "="*60)
    print("INTEGRATION TEST RESULTS")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    failed = len(results) - passed
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print("="*60)
    print(f"Total: {passed} passed, {failed} failed")
    print("="*60)
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
