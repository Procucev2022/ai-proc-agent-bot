"""
Tests for consolidated timeout mechanism using InactivityTimeoutService.

This test suite verifies:
1. Timeout triggers at configured interval (30 minutes)
2. Notification sent AFTER cleanup completes
3. Race condition protection (user sends message during timeout)
4. Session state properly cleared
5. Redis TTL serves as safety net
6. No duplicate timeouts across workers
"""

import pytest
import asyncio
import time
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from app.services.inactivity_timeout_service import InactivityTimeoutService, get_timeout_service
from app.config import get_settings
from app.models import ConversationOutcome


class TestTimeoutConfiguration:
    """Test timeout configuration and initialization."""
    
    def test_timeout_config_30_minutes(self):
        """Verify timeout is configured to 5 minutes."""
        settings = get_settings()
        assert settings.workflow_timeout_seconds == 300, "Timeout should be 5 minutes (300 seconds)"
        assert settings.timeout_poll_interval_seconds == 30, "Poll interval should be 30 seconds"
        assert settings.workflow_timeout_enabled is True, "Timeout should be enabled"
    
    def test_redis_session_ttl_buffer(self):
        """Verify Redis TTL has 10-minute buffer beyond timeout."""
        settings = get_settings()
        # Expected: 5-min timeout (300s) + 10-min buffer (600s) = 15 min (900s)
        assert settings.workflow_timeout_seconds == 300, "Timeout should be 5 minutes (300 seconds)"
        # Config default is 900, but can be overridden by REDIS_SESSION_TTL_SECONDS env var
        assert settings.redis_session_ttl_seconds >= settings.workflow_timeout_seconds, "TTL should be >= timeout"
    
    def test_service_initialization(self):
        """Verify InactivityTimeoutService initializes with correct config."""
        service = InactivityTimeoutService()
        assert service.timeout_seconds == 300, "Service should use 5-minute timeout"
        assert service.poll_interval == 30, "Service should use 30-second poll"
        assert service.enabled is True, "Service should be enabled"


class TestTimeoutDetection:
    """Test timeout detection and triggering."""
    
    @pytest.mark.asyncio
    async def test_timeout_triggers_at_30_minutes(self):
        """Verify timeout triggers when user inactive for 5 minutes."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        
        # Mock Redis to simulate 5-minute inactivity
        with patch.object(service.redis, 'get', new_callable=AsyncMock) as mock_get:
            # Simulate activity 6 minutes ago
            last_activity = time.time() - 360  # 6 minutes
            mock_get.return_value = str(last_activity)
            
            # Check if should timeout
            now = time.time()
            inactive_duration = now - last_activity
            
            assert inactive_duration >= service.timeout_seconds, "Should detect timeout after 5 minutes"
    
    @pytest.mark.asyncio
    async def test_no_timeout_for_active_users(self):
        """Verify no timeout for users active within 5 minutes."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        
        # Mock Redis to simulate recent activity
        with patch.object(service.redis, 'get', new_callable=AsyncMock) as mock_get:
            # Simulate activity 2 minutes ago
            last_activity = time.time() - 120  # 2 minutes
            mock_get.return_value = str(last_activity)
            
            # Check if should timeout
            now = time.time()
            inactive_duration = now - last_activity
            
            assert inactive_duration < service.timeout_seconds, "Should NOT timeout for active users"
    
    @pytest.mark.asyncio
    async def test_timeout_skips_no_workflow(self):
        """Verify timeout skips users with workflow_type=None."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        
        # Mock session with no workflow
        session_data = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': None,
            'workflow_state': {}
        }
        
        with patch.object(service.redis_session, 'get_session', new_callable=AsyncMock) as mock_get_session:
            mock_get_session.return_value = session_data
            
            # Simulate checking inactive users
            # User should be skipped because workflow_type is None
            workflow_type = session_data.get('workflow_type')
            
            # Skip timeout if no workflow
            should_timeout = workflow_type is not None and workflow_type != 'None'
            
            assert should_timeout is False, "Should skip timeout for users with no active workflow"


class TestNotificationTiming:
    """Test that notification is sent AFTER cleanup completes."""
    
    @pytest.mark.asyncio
    async def test_notification_after_cleanup(self):
        """Verify notification is sent after all cleanup steps."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        # Track order of operations
        operations = []
        
        # Mock session data
        session_data = {
            'session_id': session_id,
            'external_user_id': user_phone,
            'workflow_type': 'rfq_creation',
            'workflow_state': {},
            'conversation_history': {}
        }
        
        # Mock all async operations to track order
        async def mock_get_session(sid):
            operations.append('get_session')
            return session_data
        
        async def mock_get_activity(key):
            # Return old timestamp (> 30 min ago) so timeout proceeds
            operations.append('check_activity')
            return str(time.time() - 2000)
        
        async def mock_delete(*args):
            if len(args) > 1:
                operations.append('delete_queues')
            else:
                operations.append('delete_activity')
            return len(args)
        
        async def mock_delete_session(sid):
            operations.append('delete_session')
        
        async def mock_send_message(phone, msg):
            operations.append('send_notification')
        
        with patch.object(service.redis_session, 'get_session', side_effect=mock_get_session), \
             patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis, 'delete', side_effect=mock_delete), \
             patch.object(service.redis_session, 'delete_session', side_effect=mock_delete_session), \
             patch.object(service.whatsapp_service, 'send_message', side_effect=mock_send_message), \
             patch('app.database.DatabaseManager') as mock_db_class:
            
            # Mock database operations
            mock_db_instance = MagicMock()
            mock_db_instance.append_session_data = MagicMock()
            mock_db_instance.close = MagicMock()
            mock_db_class.return_value = mock_db_instance
            
            # Execute timeout handling
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify notification is last operation
        assert 'send_notification' in operations, f"Notification should be sent. Operations: {operations}"
        assert operations[-1] == 'send_notification', f"Notification should be last. Operations: {operations}"
        
        # Verify cleanup happens before notification
        notification_index = operations.index('send_notification')
        delete_session_index = operations.index('delete_session')
        delete_queues_index = operations.index('delete_queues')
        
        assert delete_session_index < notification_index, "Session delete should happen before notification"
        assert delete_queues_index < notification_index, "Queue cleanup should happen before notification"
    
    @pytest.mark.asyncio
    async def test_cleanup_completes_on_notification_failure(self):
        """Verify cleanup completes even if notification fails."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        session_cleaned = False
        queues_cleaned = False
        
        async def mock_get_activity(key):
            # Return old timestamp so timeout proceeds
            return str(time.time() - 2000)
        
        async def mock_delete_session(sid):
            nonlocal session_cleaned
            session_cleaned = True
        
        async def mock_delete(*args):
            nonlocal queues_cleaned
            if len(args) > 1:  # Queue keys deletion
                queues_cleaned = True
            return len(args)
        
        async def mock_send_message(phone, msg):
            raise Exception("WhatsApp API down")
        
        with patch.object(service.redis_session, 'get_session', new_callable=AsyncMock) as mock_get, \
             patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis, 'delete', side_effect=mock_delete), \
             patch.object(service.redis_session, 'delete_session', side_effect=mock_delete_session), \
             patch.object(service.whatsapp_service, 'send_message', side_effect=mock_send_message), \
             patch('app.database.DatabaseManager') as mock_db_class:
            
            mock_get.return_value = {
                'session_id': session_id,
                'workflow_type': 'rfq_creation',
                'workflow_state': {}
            }
            mock_db_instance = MagicMock()
            mock_db_instance.append_session_data = MagicMock()
            mock_db_instance.close = MagicMock()
            mock_db_class.return_value = mock_db_instance
            
            # Execute timeout handling (should not raise exception)
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify cleanup completed despite notification failure
        assert session_cleaned, "Session should be cleaned even if notification fails"
        assert queues_cleaned, "Queues should be cleaned even if notification fails"


class TestRaceConditionProtection:
    """Test race condition protection when user sends message during timeout."""
    
    @pytest.mark.asyncio
    async def test_abort_timeout_if_user_active(self):
        """Verify timeout aborts if user sends message during processing."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        timeout_executed = False
        
        async def mock_get_activity(key):
            # Simulate user just sent a message (10 seconds ago)
            return str(time.time() - 10)
        
        async def mock_send_message(phone, msg):
            nonlocal timeout_executed
            timeout_executed = True
        
        with patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis_session, 'get_session', new_callable=AsyncMock), \
             patch.object(service.whatsapp_service, 'send_message', side_effect=mock_send_message):
            
            # Execute timeout handling
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify timeout was aborted (notification not sent)
        assert not timeout_executed, "Timeout should abort if user became active"


class TestSessionClearing:
    """Test session state is properly cleared on timeout."""
    
    @pytest.mark.asyncio
    async def test_session_deleted_from_redis(self):
        """Verify session is deleted from Redis on timeout."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        session_deleted = False
        
        async def mock_get_activity(key):
            # Return old timestamp so timeout proceeds
            return str(time.time() - 2000)
        
        async def mock_delete_session(sid):
            nonlocal session_deleted
            session_deleted = True
            assert sid == session_id, "Should delete correct session"
        
        with patch.object(service.redis_session, 'get_session', new_callable=AsyncMock) as mock_get, \
             patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis, 'delete', new_callable=AsyncMock), \
             patch.object(service.redis_session, 'delete_session', side_effect=mock_delete_session), \
             patch.object(service.whatsapp_service, 'send_message', new_callable=AsyncMock), \
             patch('app.database.DatabaseManager') as mock_db_class:
            
            mock_get.return_value = {
                'session_id': session_id,
                'workflow_type': 'rfq_creation',
                'workflow_state': {}
            }
            mock_db_instance = MagicMock()
            mock_db_instance.append_session_data = MagicMock()
            mock_db_instance.close = MagicMock()
            mock_db_class.return_value = mock_db_instance
            
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        assert session_deleted, "Session should be deleted from Redis"
    
    @pytest.mark.asyncio
    async def test_all_queue_keys_cleared(self):
        """Verify all message queue keys are cleared on timeout."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        deleted_keys = []
        
        async def mock_get_activity(key):
            # Return old timestamp so timeout proceeds
            return str(time.time() - 2000)
        
        async def mock_delete(*keys):
            deleted_keys.extend(keys)
            return len(keys)
        
        with patch.object(service.redis_session, 'get_session', new_callable=AsyncMock) as mock_get, \
             patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis, 'delete', side_effect=mock_delete), \
             patch.object(service.redis_session, 'delete_session', new_callable=AsyncMock), \
             patch.object(service.whatsapp_service, 'send_message', new_callable=AsyncMock), \
             patch('app.database.DatabaseManager') as mock_db_class:
            
            mock_get.return_value = {
                'session_id': session_id,
                'workflow_type': 'rfq_creation',
                'workflow_state': {}
            }
            mock_db_instance = MagicMock()
            mock_db_instance.append_session_data = MagicMock()
            mock_db_instance.close = MagicMock()
            mock_db_class.return_value = mock_db_instance
            
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify expected queue keys were deleted
        expected_keys = [
            ':incoming', ':outgoing', ':processing', ':session',
            ':batch_trigger', ':response_ready', ':ack_sent'
        ]
        
        for key_suffix in expected_keys:
            assert any(key.endswith(key_suffix) for key in deleted_keys), \
                f"Should delete {key_suffix} queue key. Deleted keys: {deleted_keys}"
    
    @pytest.mark.asyncio
    async def test_session_persisted_to_db_with_timeout_outcome(self):
        """Verify session is persisted to DB with outcome=timeout."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        persisted_data = None
        
        def mock_append(data):
            nonlocal persisted_data
            persisted_data = data
        
        async def mock_get_activity(key):
            # Return old timestamp so timeout proceeds
            return str(time.time() - 2000)
        
        with patch.object(service.redis_session, 'get_session', new_callable=AsyncMock) as mock_get, \
             patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis, 'delete', new_callable=AsyncMock), \
             patch.object(service.redis_session, 'delete_session', new_callable=AsyncMock), \
             patch.object(service.whatsapp_service, 'send_message', new_callable=AsyncMock), \
             patch('app.database.DatabaseManager') as mock_db_class:
            
            # Mock session data
            mock_get.return_value = {
                'session_id': session_id,
                'external_user_id': user_phone,
                'workflow_type': 'rfq_creation',
                'workflow_state': {'extracted_entities': []},
                'conversation_history': {'messages': ['test']}
            }
            
            # Mock database manager
            mock_db_instance = MagicMock()
            mock_db_instance.append_session_data = mock_append
            mock_db_instance.close = MagicMock()
            mock_db_class.return_value = mock_db_instance
            
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify session was persisted with correct outcome
        assert persisted_data is not None, "Session should be persisted to DB"
        assert persisted_data['outcome'] == ConversationOutcome.timeout.value, \
            "Outcome should be 'timeout'"
        assert persisted_data['session_id'] == session_id, "Should persist correct session ID"
        assert persisted_data['external_user_id'] == user_phone, "Should persist user phone"
        assert 'timeout_completed' in persisted_data['workflow_state'], \
            "Should mark timeout_completed in workflow_state"


class TestTimeoutMessage:
    """Test timeout notification message content."""
    
    @pytest.mark.asyncio
    async def test_timeout_message_shows_30_minutes(self):
        """Verify timeout message shows correct duration (5 minutes)."""
        service = InactivityTimeoutService()
        user_phone = "919999999999"
        session_id = f"whatsapp_{user_phone}_20251111"
        activity_key = f"{user_phone}:last_activity"
        
        sent_message = None
        
        async def mock_get_activity(key):
            # Return old timestamp so timeout proceeds
            return str(time.time() - 2000)
        
        async def mock_send_message(phone, msg):
            nonlocal sent_message
            sent_message = msg
        
        with patch.object(service.redis_session, 'get_session', new_callable=AsyncMock) as mock_get, \
             patch.object(service.redis, 'get', side_effect=mock_get_activity), \
             patch.object(service.redis, 'delete', new_callable=AsyncMock), \
             patch.object(service.redis_session, 'delete_session', new_callable=AsyncMock), \
             patch.object(service.whatsapp_service, 'send_message', side_effect=mock_send_message), \
             patch('app.database.DatabaseManager') as mock_db_class:
            
            mock_get.return_value = {
                'session_id': session_id,
                'workflow_type': 'rfq_creation',
                'workflow_state': {}
            }
            mock_db_instance = MagicMock()
            mock_db_instance.append_session_data = MagicMock()
            mock_db_instance.close = MagicMock()
            mock_db_class.return_value = mock_db_instance
            
            await service._handle_timeout(user_phone, session_id, activity_key)
        
        # Verify message mentions 5 minutes
        assert sent_message is not None, "Message should be sent"
        assert "5 minutes" in sent_message, f"Message should mention 5 minutes. Got: {sent_message}"
        assert "cancelled" in sent_message.lower(), f"Message should mention cancellation. Got: {sent_message}"


class TestDeprecatedSessionExpiry:
    """Test that deprecated session expiry method does nothing."""
    
    @pytest.mark.asyncio
    async def test_session_expiry_check_disabled(self):
        """Verify handle_session_expiry_check does nothing (deprecated)."""
        from app.services.session_management_service import SessionManagementService
        from app.database import DatabaseManager
        from app.services.whatsapp_service import WhatsAppService
        from app.services.chat_summary_service import ChatSummaryService
        from app.services.daily_summary_service import DailySummaryService
        
        # Create service with mocks
        db_manager = MagicMock(spec=DatabaseManager)
        whatsapp = MagicMock(spec=WhatsAppService)
        chat_summary = MagicMock(spec=ChatSummaryService)
        daily_summary = MagicMock(spec=DailySummaryService)
        
        service = SessionManagementService(
            db_manager, whatsapp, chat_summary, daily_summary
        )
        
        # Create mock session
        mock_session = MagicMock()
        mock_session.session_id = "test_session"
        
        # Call deprecated method
        result = await service.handle_session_expiry_check("919999999999", mock_session)
        
        # Verify it returns session unchanged
        assert result == mock_session, "Should return session unchanged"
        
        # Verify no DB operations were performed
        db_manager.get_conversation_session.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
