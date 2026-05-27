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
    
    async def mock_send(recipient_id: str = None, message: str = None, **kwargs):
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
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url'), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service'), \
         patch('app.services.inactivity_timeout_service.WhatsAppService'), \
         patch('app.services.inactivity_timeout_service.get_settings'):
        
        service = InactivityTimeoutService()
        
        # Build a buyer user_details object (self_client=True → buyer)
        buyer_user = MagicMock()
        buyer_user.self_client = True
        
        message = await service._generate_timeout_message([buyer_user], None)
        
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
        
        # Build a seller user_details object (self_client=False → seller).
        # Pass session_data=None so the SellerService path is skipped and the
        # base seller message is returned directly.
        seller_user = MagicMock()
        seller_user.self_client = False
        
        message = await service._generate_timeout_message([seller_user], None)
        
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
        
        # No user_details → generic fallback message
        message = await service._generate_timeout_message(None, None)
        
        assert "QUA AI" in message
        assert "Hi" in message


@pytest.mark.asyncio
async def test_timeout_handling_with_buyer_user_type(mock_redis, mock_redis_session, mock_whatsapp, mock_config):
    """Test that buyer users receive buyer-specific message during timeout"""
    from app.services.inactivity_timeout_service import InactivityTimeoutService
    
    # Mock auth_redis to return a buyer user (self_client=True)
    buyer_user = MagicMock()
    buyer_user.self_client = True
    auth_redis_mock = AsyncMock()
    auth_redis_mock.retrieve = AsyncMock(return_value=buyer_user)
    
    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config), \
         patch('app.database.DatabaseManager') as mock_db_class, \
         patch('app.redis_db.get_auth_redis_service', return_value=auth_redis_mock):
        
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
                'user_type': 'buyer',
                'step': 'collecting_items'
            },
            'conversation_history': {'messages': [], 'openai_messages': [], 'metadata': []},
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
    
    # Mock auth_redis to return a seller user (self_client=False).
    # The SellerService path may fail (no real DB/Redis in tests), but the
    # exception fallback still returns the base seller message with RFQ/bid.
    seller_user = MagicMock()
    seller_user.self_client = False
    auth_redis_mock = AsyncMock()
    auth_redis_mock.retrieve = AsyncMock(return_value=seller_user)
    
    mock_seller_instance = AsyncMock()
    mock_seller_instance.handle_seller_flow_completion = AsyncMock(return_value={"success": False, "message": None})

    with patch('app.services.inactivity_timeout_service.Redis.from_url', return_value=mock_redis), \
         patch('app.services.inactivity_timeout_service.get_session_redis_service', return_value=mock_redis_session), \
         patch('app.services.inactivity_timeout_service.WhatsAppService', return_value=mock_whatsapp), \
         patch('app.services.inactivity_timeout_service.get_settings', return_value=mock_config), \
         patch('app.database.DatabaseManager') as mock_db_class, \
         patch('app.redis_db.get_auth_redis_service', return_value=auth_redis_mock), \
         patch('app.services.seller_service.SellerService', return_value=mock_seller_instance):
        
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
                'user_type': 'seller',
                'seller_workflow_state': 'viewing_rfqs'
            },
            'conversation_history': {'messages': [], 'openai_messages': [], 'metadata': []},
            'extracted_entities': {}
        }
        
        # Create OLD activity
        old_timestamp = time.time() - 400
        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, old_timestamp)
        
        # Handle timeout
        await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify seller-specific message was sent (base seller message contains RFQ and bid)
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


# ============================================================================
# Tests for conversation history persistence (new behaviour)
# ============================================================================

def _make_full_mock_config():
    """Return a config mock with all required attributes."""
    cfg = MagicMock()
    cfg.workflow_timeout_enabled = True
    cfg.workflow_timeout_seconds = 300
    cfg.timeout_poll_interval_seconds = 1
    cfg.activity_key_ttl_seconds = 420
    cfg.worker_timeout_threshold_seconds = 135
    cfg.pending_reply_ttl_seconds = 180
    cfg.redis_url = "redis://localhost:6379/0"
    return cfg


def _auth_redis_returning_none():
    """Return a mock auth-redis service whose retrieve() returns None."""
    auth_redis = AsyncMock()
    auth_redis.retrieve = AsyncMock(return_value=None)
    return auth_redis


def _mock_session_helpers_id(user_phone: str) -> str:
    """Replicate the 'daily' session-id used in the service."""
    from app.utils.datetime_utils import utc_now
    return f"whatsapp_{user_phone}_{utc_now().strftime('%Y%m%d')}"


@pytest.mark.asyncio
async def test_timeout_message_persisted_in_conversation_history(
    mock_redis, mock_redis_session, mock_whatsapp, mock_config
):
    """
    The timeout notification message must be appended to conversation_history
    inside timeout_session_data BEFORE it reaches db_manager.append_session_data.
    """
    from app.services.inactivity_timeout_service import InactivityTimeoutService

    mock_config = _make_full_mock_config()

    # WhatsApp mock that accepts skip_concatenation kwarg
    sent_messages = []

    async def mock_send(recipient_id=None, message=None, **kwargs):
        sent_messages.append({"to": recipient_id, "message": message})
        return MagicMock(success=True, message_id="mock_id")

    mock_whatsapp.send_message = mock_send

    # Capture the dict passed to append_session_data
    captured_session_data = {}

    def capture_append(data):
        captured_session_data.update(data)

    mock_db_instance = MagicMock()
    mock_db_instance.append_session_data = MagicMock(side_effect=capture_append)
    mock_db_instance.close = MagicMock()

    auth_redis_mock = _auth_redis_returning_none()

    with patch("app.services.inactivity_timeout_service.Redis.from_url", return_value=mock_redis), \
         patch("app.services.inactivity_timeout_service.get_session_redis_service", return_value=mock_redis_session), \
         patch("app.services.inactivity_timeout_service.WhatsAppService", return_value=mock_whatsapp), \
         patch("app.services.inactivity_timeout_service.get_settings", return_value=mock_config), \
         patch("app.database.DatabaseManager", return_value=mock_db_instance), \
         patch("app.redis_db.get_auth_redis_service", return_value=auth_redis_mock):

        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"

        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            "session_id": session_id,
            "external_user_id": user_phone,
            "workflow_type": "rfq_creation",
            "workflow_state": {"step": "collecting_items"},
            "conversation_history": {"messages": [], "openai_messages": [], "metadata": []},
            "extracted_entities": {},
        }

        activity_key = f"{normalized_phone}:last_activity"
        await mock_redis.setex(activity_key, 420, time.time() - 400)

        await service._handle_timeout(user_phone, session_id, activity_key)

        # DB must have been called
        assert mock_db_instance.append_session_data.called, "DB persist was not called"

        # The persisted conversation_history must contain the timeout message
        persisted_history = captured_session_data.get("conversation_history", {})
        messages = persisted_history.get("messages", [])
        openai_msgs = persisted_history.get("openai_messages", [])
        metadata = persisted_history.get("metadata", [])

        assert len(messages) >= 1, "No message in persisted conversation_history.messages"
        assert len(openai_msgs) >= 1, "No message in persisted conversation_history.openai_messages"
        assert len(metadata) >= 1, "No entry in persisted conversation_history.metadata"

        last_msg = messages[-1]
        assert last_msg["role"] == "assistant"
        assert "QUA AI" in last_msg["content"], "Timeout message not found in persisted history"

        last_oai = openai_msgs[-1]
        assert last_oai["role"] == "assistant"
        assert last_oai["content"] == last_msg["content"]


@pytest.mark.asyncio
async def test_worker_timeout_message_persisted_in_conversation_history(
    mock_redis, mock_redis_session, mock_whatsapp, mock_config
):
    """
    The worker-timeout notification must be appended to conversation_history BEFORE
    db_manager.save_conversation_session is called.
    """
    from app.services.inactivity_timeout_service import InactivityTimeoutService

    mock_config = _make_full_mock_config()

    async def mock_send(recipient_id=None, message=None, **kwargs):
        return MagicMock(success=True, message_id="mock_id")

    mock_whatsapp.send_message = mock_send

    # Capture what is passed to save_conversation_session
    captured_session_obj = {}

    def capture_save(session_obj):
        captured_session_obj["obj"] = session_obj

    mock_db_instance = MagicMock()
    mock_db_instance.save_conversation_session = MagicMock(side_effect=capture_save)
    mock_db_instance.close = MagicMock()

    with patch("app.services.inactivity_timeout_service.Redis.from_url", return_value=mock_redis), \
         patch("app.services.inactivity_timeout_service.get_session_redis_service", return_value=mock_redis_session), \
         patch("app.services.inactivity_timeout_service.WhatsAppService", return_value=mock_whatsapp), \
         patch("app.services.inactivity_timeout_service.get_settings", return_value=mock_config), \
         patch("app.database.DatabaseManager", return_value=mock_db_instance):

        service = InactivityTimeoutService()
        user_phone = "+971501234567"
        normalized_phone = "971501234567"

        session_id = f"{normalized_phone}_20240101"
        mock_redis_session._sessions[session_id] = {
            "session_id": session_id,
            "external_user_id": user_phone,
            "workflow_type": "rfq_creation",
            "workflow_state": {},
            "conversation_history": {"messages": [], "openai_messages": [], "metadata": []},
            "extracted_entities": {},
        }

        activity_key = f"{normalized_phone}:last_activity"
        pending_reply_key = f"{normalized_phone}:pending_reply"

        await service._handle_worker_timeout(user_phone, session_id, activity_key, pending_reply_key)

        # DB must have been called
        assert mock_db_instance.save_conversation_session.called, "DB persist was not called"

        # The persisted ConversationSession object must contain the worker timeout message in history
        saved_obj = captured_session_obj.get("obj")
        assert saved_obj is not None

        conv_history = saved_obj.conversation_history
        assert isinstance(conv_history, dict), "conversation_history is not a dict"

        messages = conv_history.get("messages", [])
        openai_msgs = conv_history.get("openai_messages", [])

        assert len(messages) >= 1, "No message in persisted conversation_history.messages"
        assert len(openai_msgs) >= 1, "No message in persisted conversation_history.openai_messages"

        worker_timeout_text = "Sorry, your request is taking longer than expected"
        last_msg = messages[-1]
        assert last_msg["role"] == "assistant"
        assert worker_timeout_text in last_msg["content"], (
            f"Worker timeout message not found in persisted history: {last_msg['content']}"
        )

        last_oai = openai_msgs[-1]
        assert last_oai["role"] == "assistant"
        assert worker_timeout_text in last_oai["content"]


@pytest.mark.asyncio
async def test_ack_message_appended_to_redis_session_history():
    """
    The ACK ('Got it. Please wait...') message sent by _send_acknowledgment must be
    appended to the user's Redis session conversation history.
    """
    from app.services.message_queue_service import MessageQueueService, ProcessingSession
    import json

    appended_calls = []
    
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.setex = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock(return_value=1)
    mock_lock = MagicMock()
    mock_lock.__aenter__ = AsyncMock(return_value=None)
    mock_lock.__aexit__ = AsyncMock(return_value=None)
    mock_redis.lock = MagicMock(return_value=mock_lock)

    async def mock_wa_send(recipient_id=None, message=None, **kwargs):
        return MagicMock(success=True, message_id="mock_id")

    mock_wa = AsyncMock()
    mock_wa.send_message = mock_wa_send

    mock_redis_session = AsyncMock()

    async def mock_append(session_id, role, content, message_type="text"):
        appended_calls.append({"session_id": session_id, "role": role, "content": content})
        return True

    mock_redis_session.append_message_to_history = mock_append

    with patch("app.services.message_queue_service.Redis.from_url", return_value=mock_redis), \
         patch("app.services.message_queue_service.MessageQueueService.__init__",
               lambda s: None):

        service = MessageQueueService.__new__(MessageQueueService)
        service.redis = mock_redis
        service.whatsapp_service = mock_wa

        settings = MagicMock()
        settings.batch_window_seconds = 3
        settings.please_wait_threshold_seconds = 15
        settings.max_please_wait_count = 3
        settings.monitoring_poll_interval_seconds = 5
        settings.redis_url = "redis://localhost:6379"
        settings.retry_initial_delay = 1
        service.please_wait_threshold = 15
        service.max_please_wait_count = 3
        service.monitoring_poll_interval = 5
        service.response_ready_ttl = 60
        service.monitor_lock_ttl = 180
        service.please_wait_interval_ttl = 600
        service._background_tasks = []

        user_phone = "971501234567"

        with patch("app.redis_db.get_session_redis_service",
                   return_value=mock_redis_session), \
             patch("app.services.helpers.session_helpers.SessionHelpers.generate_session_id",
                   return_value=f"whatsapp_{user_phone}_20240101"):

            await service._send_acknowledgment(user_phone)

    # append_message_to_history must have been called with the ack text
    assert len(appended_calls) >= 1, "_send_acknowledgment did not append to Redis session history"
    call = appended_calls[0]
    assert call["role"] == "assistant"
    assert "Please wait" in call["content"]


@pytest.mark.asyncio
async def test_please_wait_message_appended_to_redis_session_history():
    """
    The 'Please wait...' message sent by _send_please_wait must be appended to
    the user's Redis session conversation history.
    """
    from app.services.message_queue_service import MessageQueueService

    appended_calls = []

    async def mock_wa_send(recipient_id=None, message=None, **kwargs):
        return MagicMock(success=True, message_id="mock_id")

    mock_wa = AsyncMock()
    mock_wa.send_message = mock_wa_send

    mock_redis_session = AsyncMock()

    async def mock_append(session_id, role, content, message_type="text"):
        appended_calls.append({"session_id": session_id, "role": role, "content": content})
        return True

    mock_redis_session.append_message_to_history = mock_append

    mock_redis = MagicMock()

    with patch("app.services.message_queue_service.MessageQueueService.__init__",
               lambda s: None):

        service = MessageQueueService.__new__(MessageQueueService)
        service.redis = mock_redis
        service.whatsapp_service = mock_wa
        service._background_tasks = []

        user_phone = "971501234567"

        with patch("app.redis_db.get_session_redis_service",
                   return_value=mock_redis_session), \
             patch("app.services.helpers.session_helpers.SessionHelpers.generate_session_id",
                   return_value=f"whatsapp_{user_phone}_20240101"):

            await service._send_please_wait(user_phone)

    assert len(appended_calls) >= 1, "_send_please_wait did not append to Redis session history"
    call = appended_calls[0]
    assert call["role"] == "assistant"
    assert "working on your request" in call["content"].lower() or "please wait" in call["content"].lower()
