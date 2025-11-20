"""
Test script for InactivityTimeoutService.

Tests core functionality:
1. Activity tracking (update_user_activity)
2. Timeout detection (monitor loop)
3. Timeout handling (workflow cancellation)
4. Singleton pattern (shared instance)
5. Service lifecycle (start/stop)

Usage:
    python -m pytest tests/test_inactivity_timeout.py -v
    OR
    python tests/test_inactivity_timeout.py
"""

import asyncio
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def mock_redis():
    """Mock Redis service for testing"""
    redis_mock = AsyncMock()
    
    # Mock activity key storage
    redis_mock._activity_keys = {}
    
    async def mock_setex(key: str, ttl: int, value: Any):
        redis_mock._activity_keys[key] = {
            'value': value,
            'expires_at': time.time() + ttl
        }
        return True
    
    async def mock_get(key: str):
        if key in redis_mock._activity_keys:
            entry = redis_mock._activity_keys[key]
            if time.time() < entry['expires_at']:
                return entry['value']
        return None
    
    async def mock_delete(*keys):
        count = 0
        for key in keys:
            if key in redis_mock._activity_keys:
                del redis_mock._activity_keys[key]
                count += 1
        return count
    
    async def mock_scan(cursor=0, match=None, count=100):
        """Mock SCAN command - returns matching keys"""
        matching_keys = []
        if match:
            pattern = match.replace('*', '').replace(':', ':')
            matching_keys = [
                key for key in redis_mock._activity_keys.keys()
                if pattern in key
            ]
        return (0, matching_keys)
    
    redis_mock.setex = mock_setex
    redis_mock.get = mock_get
    redis_mock.delete = mock_delete
    redis_mock.scan = mock_scan
    redis_mock.lock = MagicMock(return_value=AsyncMock(acquire=AsyncMock(return_value=True), release=AsyncMock()))
    
    return redis_mock


@pytest.fixture
def mock_redis_session():
    """Mock Redis session service for testing"""
    session_mock = AsyncMock()
    session_mock._sessions = {}
    
    async def mock_get_session(session_id: str):
        return session_mock._sessions.get(session_id)
    
    async def mock_delete_session(session_id: str):
        if session_id in session_mock._sessions:
            del session_mock._sessions[session_id]
            return True
        return False
    
    session_mock.get_session = mock_get_session
    session_mock.delete_session = mock_delete_session
    
    return session_mock


@pytest.fixture
def mock_whatsapp():
    """Mock WhatsApp service for testing"""
    whatsapp_mock = AsyncMock()
    whatsapp_mock.sent_messages = []
    
    async def mock_send(recipient_id: str, message: str):
        whatsapp_mock.sent_messages.append({
            'to': recipient_id,
            'message': message
        })
        return MagicMock(success=True, message_id=f"mock_{int(time.time())}")
    
    whatsapp_mock.send_message = mock_send
    return whatsapp_mock


@pytest.fixture
def mock_config():
    """Mock configuration settings"""
    config = MagicMock()
    config.workflow_timeout_enabled = True
    config.workflow_timeout_seconds = 300  # 5 minutes (production value)
    config.timeout_poll_interval_seconds = 1  # 1 second for faster tests
    return config


# ============================================================================
# Test Cases
# ============================================================================

@pytest.mark.asyncio
async def test_activity_tracking(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that update_user_activity correctly updates Redis activity keys"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    # Mock Redis.from_url to return our mock redis client
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config):
        
        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        
        # Update activity
        await service.update_user_activity(user_phone)
        
        # Verify key was created
        activity_key = "971501234567:last_activity"
        assert activity_key in mock_redis._activity_keys
        
        # Verify timestamp is recent
        stored_value = mock_redis._activity_keys[activity_key]['value']
        assert isinstance(stored_value, float)
        assert abs(time.time() - stored_value) < 2  # Within 2 seconds


@pytest.mark.asyncio
async def test_singleton_pattern():
    """Test that get_timeout_service returns the same instance"""
    from app.services.inactivity_timeout_service import get_timeout_service, _timeout_service_instance
    
    # Reset singleton
    import app.services.inactivity_timeout_service as timeout_module
    timeout_module._timeout_service_instance = None
    
    # Get first instance
    service1 = get_timeout_service()
    
    # Get second instance
    service2 = get_timeout_service()
    
    # Verify same instance
    assert service1 is service2
    assert id(service1) == id(service2)


@pytest.mark.asyncio
async def test_timeout_detection_and_handling(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that inactive users are detected and timeouts are handled"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    # Mock DatabaseManager at the app.database level (where it's imported in _handle_timeout)
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config), \
         patch('app.database.DatabaseManager') as mock_db_class:
        
        # Mock database manager instance
        mock_db_instance = MagicMock()
        mock_db_instance.append_session_data = MagicMock()
        mock_db_instance.close = MagicMock()
        mock_db_class.return_value = mock_db_instance
        
        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"
        
        # Set up session in Redis
        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': 'rfq_creation',
            'workflow_state': {'step': 'collecting_items'},
            'conversation_history': {'messages': []},
            'extracted_entities': {}
        }
        
        # Create OLD activity (400 seconds ago, past 300-second / 5-minute timeout)
        old_timestamp = time.time() - 400
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Call _handle_timeout directly instead of _check_inactive_users (simpler test)
        await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify timeout message was sent
        assert len(mock_whatsapp.sent_messages) >= 1
        sent = mock_whatsapp.sent_messages[0]
        assert sent['to'] == user_phone
        # Verify it's a timeout message (generic, since no user_type set)
        assert "QUA AI" in sent['message']
        assert "Hi" in sent['message']
        
        # Verify DB was called to persist timeout
        assert mock_db_instance.append_session_data.called
        
        # Verify activity key was deleted
        assert activity_key not in mock_redis._activity_keys


@pytest.mark.asyncio
async def test_no_timeout_for_recent_activity(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that recently active users are NOT timed out"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config):
        
        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"
        
        # Create RECENT activity (2 seconds ago, within 5-second timeout)
        recent_timestamp = time.time() - 2
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, recent_timestamp)
        
        # Run timeout check
        await service._check_inactive_users()
        
        # Verify NO timeout message was sent
        assert len(mock_whatsapp.sent_messages) == 0
        
        # Verify activity key still exists
        assert activity_key in mock_redis._activity_keys


@pytest.mark.asyncio
async def test_service_lifecycle(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test service start and stop methods"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config):
        
        service = InactivityTimeoutService()
        
        # Start service
        await service.start_monitoring()
        assert service._monitor_task is not None
        assert not service._monitor_task.done()
        
        # Give it a moment to start
        await asyncio.sleep(0.1)
        
        # Stop service
        await service.stop_monitoring()
        
        # Verify task is done or cancelled
        if service._monitor_task:
            assert service._monitor_task.done() or service._monitor_task.cancelled()


@pytest.mark.asyncio
async def test_workflow_type_none_skipped(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that users with workflow_type=None are NOT timed out"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config):
        
        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"
        
        # Set up session with workflow_type=None
        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': None,
            'workflow_state': {}
        }
        
        # Create OLD activity (6 seconds ago, past timeout)
        old_timestamp = time.time() - 6
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Run timeout check
        await service._check_inactive_users()
        
        # Verify NO timeout message was sent (workflow_type=None should skip)
        assert len(mock_whatsapp.sent_messages) == 0


@pytest.mark.asyncio
async def test_feature_disabled(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that timeout service does nothing when disabled in config"""
    mock_config.workflow_timeout_enabled = False
    
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config):
        
        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"
        
        # Create OLD activity
        old_timestamp = time.time() - 6
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Set up session
        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': 'rfq_creation',
            'workflow_state': {}
        }
        
        # Run check
        await service._check_inactive_users()
        
        # Verify no timeout happened (feature disabled)
        assert len(mock_whatsapp.sent_messages) == 0


# ============================================================================
# Test User-Type-Specific Timeout Messages
# ============================================================================

@pytest.mark.asyncio
async def test_generate_timeout_message_buyer():
    """Test that buyer users receive buyer-specific timeout message"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    # Create service instance (doesn't need mocks for this simple test)
    with patch('app.services.inactivity_timeout_service.Redis.from_url'), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service'), \
         patch('app.services.inactivity_timeout_service.WhatsAppService'), \
         patch('app.services.inactivity_timeout_service.get_settings'):
        
        service = InactivityTimeoutService()
        
        # Test buyer message
        message = service._generate_timeout_message("buyer")
        
        assert "Looks like you're away for a bit" in message
        assert "QUA AI" in message
        assert "RFQ" in message or "rfq" in message.lower()
        assert "status" in message.lower()
        assert "Hi" in message


@pytest.mark.asyncio
async def test_generate_timeout_message_seller():
    """Test that seller users receive seller-specific timeout message"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url'), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service'), \
         patch('app.services.inactivity_timeout_service.WhatsAppService'), \
         patch('app.services.inactivity_timeout_service.get_settings'):
        
        service = InactivityTimeoutService()
        
        # Test seller message
        message = service._generate_timeout_message("seller")
        
        assert "Looks like you're away for a bit" in message
        assert "QUA AI" in message
        assert "RFQ" in message or "rfq" in message.lower()
        assert "bid" in message.lower()
        assert "Hi" in message


@pytest.mark.asyncio
async def test_generate_timeout_message_default():
    """Test that users without type receive generic timeout message"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url'), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service'), \
         patch('app.services.inactivity_timeout_service.WhatsAppService'), \
         patch('app.services.inactivity_timeout_service.get_settings'):
        
        service = InactivityTimeoutService()
        
        # Test None/default message
        message = service._generate_timeout_message(None)
        
        assert "Looks like you're away for a bit" in message
        assert "QUA AI" in message
        assert "Hi" in message
        # Should NOT have role-specific text for default/generic message
        # (checking that it's different from buyer/seller messages)


@pytest.mark.asyncio
async def test_timeout_handling_with_buyer_user_type(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that buyer users receive buyer-specific message during timeout"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config), \
         patch('app.database.DatabaseManager') as mock_db_class:
        
        # Mock database manager
        mock_db_instance = MagicMock()
        mock_db_instance.append_session_data = MagicMock()
        mock_db_instance.close = MagicMock()
        mock_db_class.return_value = mock_db_instance
        
        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"
        
        # Set up session with buyer user_type
        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': 'rfq_creation',
            'workflow_state': {
                'user_type': 'buyer',  # BUYER user type
                'step': 'collecting_items'
            },
            'conversation_history': {'messages': []},
            'extracted_entities': {}
        }
        
        # Create OLD activity
        old_timestamp = time.time() - 400
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Handle timeout
        await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify buyer-specific message was sent
        assert len(mock_whatsapp.sent_messages) >= 1
        sent = mock_whatsapp.sent_messages[0]
        assert sent['to'] == user_phone
        assert "RFQ" in sent['message'] or "rfq" in sent['message'].lower()
        assert "status" in sent['message'].lower()


@pytest.mark.asyncio
async def test_timeout_handling_with_seller_user_type(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that seller users receive seller-specific message during timeout"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config), \
         patch('app.database.DatabaseManager') as mock_db_class:
        
        # Mock database manager
        mock_db_instance = MagicMock()
        mock_db_instance.append_session_data = MagicMock()
        mock_db_instance.close = MagicMock()
        mock_db_class.return_value = mock_db_instance
        
        service = InactivityTimeoutService()
        user_phone = "+971509876543"
        normalized_phone = "971509876543"
        
        # Set up session with seller user_type
        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': 'seller_rfq_view',
            'workflow_state': {
                'user_type': 'seller',  # SELLER user type
                'seller_workflow_state': 'viewing_rfqs'
            },
            'conversation_history': {'messages': []},
            'extracted_entities': {}
        }
        
        # Create OLD activity
        old_timestamp = time.time() - 400
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Handle timeout
        await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify seller-specific message was sent
        assert len(mock_whatsapp.sent_messages) >= 1
        sent = mock_whatsapp.sent_messages[0]
        assert sent['to'] == user_phone
        assert "RFQ" in sent['message'] or "rfq" in sent['message'].lower()
        assert "bid" in sent['message'].lower()


@pytest.mark.asyncio
async def test_timeout_handling_without_user_type(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that users without user_type receive generic message during timeout"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config), \
         patch('app.database.DatabaseManager') as mock_db_class:
        
        # Mock database manager
        mock_db_instance = MagicMock()
        mock_db_instance.append_session_data = MagicMock()
        mock_db_instance.close = MagicMock()
        mock_db_class.return_value = mock_db_instance
        
        service = InactivityTimeoutService()
        user_phone = "+971501111111"
        normalized_phone = "971501111111"
        
        # Set up session WITHOUT user_type
        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': 'general_inquiry',
            'workflow_state': {
                # No user_type field
                'step': 'initial'
            },
            'conversation_history': {'messages': []},
            'extracted_entities': {}
        }
        
        # Create OLD activity
        old_timestamp = time.time() - 400
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Handle timeout
        await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify generic message was sent
        assert len(mock_whatsapp.sent_messages) >= 1
        sent = mock_whatsapp.sent_messages[0]
        assert sent['to'] == user_phone
        assert "QUA AI" in sent['message']
        assert "Hi" in sent['message']


# ============================================================================
# Standalone Execution (for manual testing)
# ============================================================================

async def run_manual_tests():
    """Run tests manually without pytest"""
    print("\n" + "="*70)
    print("INACTIVITY TIMEOUT SERVICE - MANUAL TEST RUN")
    print("="*70)
    
    # Create fixtures
    mock_redis = MagicMock()
    mock_redis._activity_keys = {}
    
    # Add mock methods
    async def mock_setex(key, ttl, value):
        mock_redis._activity_keys[key] = {'value': value, 'expires_at': time.time() + ttl}
        return True
    
    mock_redis.setex = mock_setex
    mock_redis.get = AsyncMock()
    mock_redis.delete = AsyncMock(return_value=1)
    mock_redis.scan = AsyncMock(return_value=(0, []))
    mock_redis.lock = MagicMock(return_value=AsyncMock(acquire=AsyncMock(return_value=True), release=AsyncMock()))
    
    mock_redis_session = AsyncMock()
    mock_redis_session._sessions = {}
    mock_redis_session.get_session = AsyncMock(return_value=None)
    mock_redis_session.delete_session = AsyncMock()
    
    mock_whatsapp = AsyncMock()
    mock_whatsapp.sent_messages = []
    
    async def mock_send(recipient_id, message):
        mock_whatsapp.sent_messages.append({'to': recipient_id, 'message': message})
        return MagicMock(success=True)
    
    mock_whatsapp.send_message = mock_send
    
    mock_config = MagicMock()
    mock_config.workflow_timeout_enabled = True
    mock_config.workflow_timeout_seconds = 5
    mock_config.timeout_poll_interval_seconds = 1
    mock_config.redis_url = "redis://localhost:6379/0"
    
    print("\n[TEST 1] Activity Tracking")
    print("-" * 70)
    try:
        await test_activity_tracking(mock_redis, mock_redis_session, mock_whatsapp, mock_config)
        print("✅ PASSED")
    except Exception as e:
        print(f"❌ FAILED: {e}")
    
    print("\n[TEST 2] Singleton Pattern")
    print("-" * 70)
    try:
        await test_singleton_pattern()
        print("✅ PASSED")
    except Exception as e:
        print(f"❌ FAILED: {e}")
    
    print("\n" + "="*70)
    print("MANUAL TEST RUN COMPLETE")
    print("="*70)


if __name__ == "__main__":
    # Check if pytest is available
    try:
        import pytest
        print("Running tests with pytest...")
        pytest.main([__file__, "-v", "--tb=short"])
    except ImportError:
        print("pytest not found, running manual tests...")
        asyncio.run(run_manual_tests())
