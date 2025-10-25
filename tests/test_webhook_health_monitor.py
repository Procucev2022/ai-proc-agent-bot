"""
Test suite for Webhook Health Monitor Service.

Tests the complete health monitoring system including:
- API health checks with different response scenarios
- State machine transitions
- Alert triggering logic
- Email template rendering
- Redis state persistence
- Graceful degradation
"""

import pytest
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import aiohttp

from app.services.webhook_health_monitor_service import (
    WebhookHealthMonitorService,
    HealthStatus,
    MonitorState
)
from app.config import get_settings


class TestWebhookHealthMonitor:
    """Test suite for webhook health monitoring."""
    
    @pytest.fixture
    def settings(self):
        """Get test settings."""
        return get_settings()
    
    @pytest.fixture
    def monitor(self):
        """Create health monitor instance for testing."""
        monitor = WebhookHealthMonitorService()
        # Set alert recipients for testing (to enable email sending in tests)
        monitor.alert_recipients = ["test@example.com"]
        return monitor
    
    @pytest.fixture
    def mock_redis(self):
        """Mock Redis service."""
        redis_mock = AsyncMock()
        redis_mock.init_client = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.set = AsyncMock(return_value=True)
        return redis_mock
    
    @pytest.fixture
    def mock_email(self):
        """Mock email service."""
        email_mock = AsyncMock()
        email_mock.send_email_by_template = AsyncMock(
            return_value={"status": "Success"}
        )
        return email_mock
    
    # ===== API Health Check Tests =====
    
    @pytest.mark.asyncio
    async def test_api_health_check_ok(self, monitor):
        """Test successful API health check."""
        with patch.object(monitor, '_get_session') as mock_session:
            # Mock successful response
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock()
            
            mock_session_obj = AsyncMock()
            mock_session_obj.post = Mock(return_value=mock_response)
            mock_session.return_value = mock_session_obj
            
            status, latency, error = await monitor._check_api_health()
            
            assert status == HealthStatus.OK
            assert latency is not None
            assert latency > 0
            assert error is None
    
    @pytest.mark.asyncio
    async def test_api_health_check_slow_response(self, monitor):
        """Test API health check with slow response (WARNING)."""
        with patch.object(monitor, '_get_session') as mock_session:
            # Mock slow response
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock()
            
            mock_session_obj = AsyncMock()
            mock_session_obj.post = Mock(return_value=mock_response)
            mock_session.return_value = mock_session_obj
            
            # Simulate slow response by patching datetime
            with patch('app.services.webhook_health_monitor_service.datetime') as mock_dt:
                start_time = datetime(2025, 10, 24, 12, 0, 0)
                end_time = start_time + timedelta(seconds=6)  # 6 seconds > threshold
                
                mock_dt.utcnow.side_effect = [start_time, end_time]
                
                status, latency, error = await monitor._check_api_health()
                
                assert status == HealthStatus.WARNING
                assert "Slow response" in error
    
    @pytest.mark.asyncio
    async def test_api_health_check_timeout(self, monitor):
        """Test API health check with timeout (CRITICAL)."""
        with patch.object(monitor, '_get_session') as mock_session:
            # Mock timeout
            mock_session_obj = AsyncMock()
            mock_session_obj.post = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_session.return_value = mock_session_obj
            
            status, latency, error = await monitor._check_api_health()
            
            assert status == HealthStatus.CRITICAL
            assert "Timeout" in error
    
    @pytest.mark.asyncio
    async def test_api_health_check_server_error(self, monitor):
        """Test API health check with 5xx error (CRITICAL)."""
        with patch.object(monitor, '_get_session') as mock_session:
            # Mock 500 error
            mock_response = AsyncMock()
            mock_response.status = 500
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock()
            
            mock_session_obj = AsyncMock()
            mock_session_obj.post = Mock(return_value=mock_response)
            mock_session.return_value = mock_session_obj
            
            status, latency, error = await monitor._check_api_health()
            
            assert status == HealthStatus.CRITICAL
            assert "Server error: 500" in error
    
    # ===== State Machine Tests =====
    
    @pytest.mark.asyncio
    async def test_state_transition_healthy_to_failing(self, monitor, mock_redis):
        """Test transition from HEALTHY to FAILING on first failure."""
        monitor.redis = mock_redis
        
        state = {
            "current_state": MonitorState.HEALTHY.value,
            "is_alerting": False,
            "consecutive_failures": 0,
            "consecutive_successes": 0,
            "consecutive_warnings": 0,
            "failure_start_time": None
        }
        
        await monitor._handle_critical_status(state, MonitorState.HEALTHY)
        
        assert state["current_state"] == MonitorState.FAILING.value
        assert state["consecutive_failures"] == 1
        assert state["failure_start_time"] is not None
        assert not state["is_alerting"]
    
    @pytest.mark.asyncio
    async def test_state_transition_failing_to_alerting(self, monitor, mock_redis, mock_email):
        """Test transition from FAILING to ALERTING after grace period."""
        monitor.redis = mock_redis
        monitor.email_service = mock_email
        monitor.grace_period = 1  # 1 second grace period for testing
        
        # State has been failing for more than grace period
        failure_start = datetime.utcnow() - timedelta(seconds=70)
        state = {
            "current_state": MonitorState.FAILING.value,
            "is_alerting": False,
            "consecutive_failures": 3,
            "consecutive_successes": 0,
            "consecutive_warnings": 0,
            "failure_start_time": failure_start.isoformat(),
            "last_error": "Connection timeout"
        }
        
        await monitor._handle_critical_status(state, MonitorState.FAILING)
        
        assert state["current_state"] == MonitorState.ALERTING.value
        assert state["is_alerting"]
        # Verify alert was sent
        mock_email.send_email_by_template.assert_called_once()
        call_args = mock_email.send_email_by_template.call_args
        assert call_args[0][0] == "webhook_api_critical"
    
    @pytest.mark.asyncio
    async def test_state_transition_alerting_to_recovered(self, monitor, mock_redis):
        """Test transition from ALERTING to RECOVERED on first success."""
        monitor.redis = mock_redis
        
        state = {
            "current_state": MonitorState.ALERTING.value,
            "is_alerting": True,
            "consecutive_failures": 0,
            "consecutive_successes": 0,
            "consecutive_warnings": 0
        }
        
        await monitor._handle_ok_status(state, MonitorState.ALERTING)
        
        assert state["current_state"] == MonitorState.RECOVERED.value
        assert state["consecutive_successes"] == 1
    
    @pytest.mark.asyncio
    async def test_state_transition_recovered_to_healthy(self, monitor, mock_redis, mock_email):
        """Test transition from RECOVERED to HEALTHY after N confirmations."""
        monitor.redis = mock_redis
        monitor.email_service = mock_email
        monitor.recovery_confirmations = 3
        
        state = {
            "current_state": MonitorState.RECOVERED.value,
            "is_alerting": True,
            "consecutive_failures": 0,
            "consecutive_successes": 2,  # One more needed
            "consecutive_warnings": 0,
            "failure_start_time": datetime.utcnow().isoformat()
        }
        
        await monitor._handle_ok_status(state, MonitorState.RECOVERED)
        
        assert state["current_state"] == MonitorState.HEALTHY.value
        assert not state["is_alerting"]
        assert state["failure_start_time"] is None
        # Verify recovery notification was sent
        mock_email.send_email_by_template.assert_called_once()
        call_args = mock_email.send_email_by_template.call_args
        assert call_args[0][0] == "webhook_api_recovery"
    
    @pytest.mark.asyncio
    async def test_state_transition_recovered_to_alerting_relapse(self, monitor, mock_redis, mock_email):
        """Test relapse: RECOVERED → ALERTING when failure occurs during recovery."""
        monitor.redis = mock_redis
        monitor.email_service = mock_email
        
        state = {
            "current_state": MonitorState.RECOVERED.value,
            "is_alerting": False,
            "consecutive_failures": 0,
            "consecutive_successes": 1,  # Was recovering
            "consecutive_warnings": 0,
            "last_error": "Connection lost again"
        }
        
        await monitor._handle_critical_status(state, MonitorState.RECOVERED)
        
        assert state["current_state"] == MonitorState.ALERTING.value
        assert state["consecutive_successes"] == 0
        # Verify relapse alert was sent
        mock_email.send_email_by_template.assert_called_once()
        call_args = mock_email.send_email_by_template.call_args
        assert call_args[0][0] == "webhook_api_relapse"
    
    @pytest.mark.asyncio
    async def test_strict_recovery_policy_warning_resets_counter(self, monitor, mock_redis, mock_email):
        """Test that WARNING during recovery resets success counter (strict policy)."""
        monitor.redis = mock_redis
        monitor.email_service = mock_email
        
        state = {
            "current_state": MonitorState.RECOVERED.value,
            "is_alerting": False,
            "consecutive_failures": 0,
            "consecutive_successes": 2,  # Had 2 successes
            "consecutive_warnings": 0
        }
        
        await monitor._handle_warning_status(state, MonitorState.RECOVERED)
        
        assert state["current_state"] == MonitorState.ALERTING.value
        assert state["consecutive_successes"] == 0  # Reset
        # Verify relapse alert
        mock_email.send_email_by_template.assert_called_once()
    
    # ===== Alert Tests =====
    
    @pytest.mark.asyncio
    async def test_warning_alert_after_consecutive_slow_checks(self, monitor, mock_redis, mock_email):
        """Test that WARNING alert is sent after N consecutive slow responses."""
        monitor.redis = mock_redis
        monitor.email_service = mock_email
        monitor.grace_period = 1
        monitor.warning_threshold = 3
        
        # State has been warning for 3 consecutive checks after grace period
        failure_start = datetime.utcnow() - timedelta(seconds=70)
        state = {
            "current_state": MonitorState.FAILING.value,
            "is_alerting": False,
            "consecutive_failures": 3,
            "consecutive_successes": 0,
            "consecutive_warnings": 3,
            "failure_start_time": failure_start.isoformat(),
            "last_latency_ms": 6500
        }
        
        await monitor._handle_warning_status(state, MonitorState.FAILING)
        
        assert state["current_state"] == MonitorState.ALERTING.value
        assert state["is_alerting"]
        # Verify warning alert was sent
        mock_email.send_email_by_template.assert_called_once()
        call_args = mock_email.send_email_by_template.call_args
        assert call_args[0][0] == "webhook_api_warning"
    
    @pytest.mark.asyncio
    async def test_no_duplicate_alerts(self, monitor, mock_redis, mock_email):
        """Test that no duplicate alerts are sent when already alerting."""
        monitor.redis = mock_redis
        monitor.email_service = mock_email
        
        state = {
            "current_state": MonitorState.ALERTING.value,
            "is_alerting": True,  # Already alerting
            "consecutive_failures": 5,
            "consecutive_successes": 0,
            "consecutive_warnings": 0,
            "failure_start_time": datetime.utcnow().isoformat()
        }
        
        await monitor._handle_critical_status(state, MonitorState.ALERTING)
        
        # Should not send another alert
        mock_email.send_email_by_template.assert_not_called()
    
    # ===== Graceful Degradation Tests =====
    
    @pytest.mark.asyncio
    async def test_continues_monitoring_when_redis_fails(self, monitor):
        """Test that monitoring continues even if Redis fails."""
        # Mock Redis to fail
        monitor.redis = AsyncMock()
        monitor.redis.init_client = AsyncMock()
        monitor.redis.get = AsyncMock(side_effect=Exception("Redis connection error"))
        monitor.redis.set = AsyncMock(side_effect=Exception("Redis connection error"))
        
        # Should not raise exception, should return default state
        state = await monitor._get_state()
        
        assert state["current_state"] == MonitorState.HEALTHY.value
        assert isinstance(state, dict)
    
    @pytest.mark.asyncio
    async def test_continues_monitoring_when_email_fails(self, monitor, mock_redis):
        """Test that monitoring continues even if email sending fails."""
        monitor.redis = mock_redis
        monitor.email_service = AsyncMock()
        monitor.email_service.send_email_by_template = AsyncMock(
            side_effect=Exception("Email service down")
        )
        
        state = {
            "current_state": MonitorState.FAILING.value,
            "is_alerting": False,
            "consecutive_failures": 3,
            "failure_start_time": (datetime.utcnow() - timedelta(seconds=70)).isoformat()
        }
        
        # Should not raise exception
        try:
            await monitor._send_critical_alert(state)
        except Exception as e:
            pytest.fail(f"Should not raise exception: {e}")
    
    # ===== History Buffer Tests =====
    
    @pytest.mark.asyncio
    async def test_history_buffer_stores_checks(self, monitor, mock_redis):
        """Test that health check results are stored in history buffer."""
        monitor.redis = mock_redis
        mock_redis.get.return_value = json.dumps([])
        
        await monitor._add_to_history(HealthStatus.OK, 1200, None)
        
        # Verify set was called
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "webhook:health:history"
        
        # Verify history entry structure
        history_json = call_args[0][1]
        history = json.loads(history_json)
        assert len(history) == 1
        assert history[0]["status"] == "OK"
        assert history[0]["latency_ms"] == 1200
    
    @pytest.mark.asyncio
    async def test_history_buffer_circular_limit(self, monitor, mock_redis):
        """Test that history buffer keeps only last 100 entries."""
        monitor.redis = mock_redis
        
        # Create 105 existing entries
        existing_history = [
            {
                "timestamp": datetime.utcnow().isoformat(),
                "status": "OK",
                "latency_ms": 1000,
                "error": None
            }
            for _ in range(105)
        ]
        mock_redis.get.return_value = json.dumps(existing_history)
        
        await monitor._add_to_history(HealthStatus.OK, 1200, None)
        
        # Verify only last 100 entries are kept
        call_args = mock_redis.set.call_args
        history_json = call_args[0][1]
        history = json.loads(history_json)
        assert len(history) == 100
    
    # ===== Configuration Validation Tests =====
    
    def test_config_validation_check_interval_greater_than_timeout(self):
        """Test that check interval must be greater than timeout."""
        with patch.dict('os.environ', {
            'WEBHOOK_HEALTH_MONITORING_ENABLED': 'true',
            'WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS': '5',
            'WEBHOOK_API_TIMEOUT_SECONDS': '10'
        }):
            with pytest.raises(ValueError, match="WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS must be greater than"):
                from app.config import Settings
                Settings()
    
    def test_config_validation_check_interval_less_than_grace_period(self):
        """Test that check interval must be less than grace period."""
        with patch.dict('os.environ', {
            'WEBHOOK_HEALTH_MONITORING_ENABLED': 'true',
            'WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS': '70',
            'WEBHOOK_FAILURE_GRACE_PERIOD_SECONDS': '60'
        }):
            with pytest.raises(ValueError, match="WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS must be less than"):
                from app.config import Settings
                Settings()
    
    # ===== Helper Method Tests =====
    
    def test_format_duration(self, monitor):
        """Test duration formatting."""
        # 1 hour 23 minutes 45 seconds
        delta = timedelta(hours=1, minutes=23, seconds=45)
        formatted = monitor._format_duration(delta)
        assert formatted == "1h 23m 45s"
        
        # Just seconds
        delta = timedelta(seconds=30)
        formatted = monitor._format_duration(delta)
        assert formatted == "30s"
        
        # Hours and seconds (no minutes)
        delta = timedelta(hours=2, seconds=15)
        formatted = monitor._format_duration(delta)
        assert formatted == "2h 15s"
    
    # ===== Leader Election Tests =====
    
    @pytest.mark.asyncio
    async def test_leader_lock_acquisition(self, monitor, mock_redis):
        """Test that worker can acquire leader lock."""
        monitor.redis = mock_redis
        
        # Mock successful lock acquisition
        mock_redis.client = AsyncMock()
        mock_redis.client.set = AsyncMock(return_value=True)
        
        result = await monitor._try_acquire_leader_lock()
        
        assert result is True
        mock_redis.client.set.assert_called_once_with(
            "webhook:health:leader_lock",
            monitor.worker_id,
            nx=True,
            ex=60
        )
    
    @pytest.mark.asyncio
    async def test_leader_lock_already_held_by_another(self, monitor, mock_redis):
        """Test that worker cannot acquire lock held by another worker."""
        monitor.redis = mock_redis
        
        # Mock lock acquisition failure
        mock_redis.client = AsyncMock()
        mock_redis.client.set = AsyncMock(return_value=False)
        mock_redis.get = AsyncMock(return_value="worker_9999")
        
        result = await monitor._try_acquire_leader_lock()
        
        assert result is False
        assert monitor.worker_id != "worker_9999"
    
    @pytest.mark.asyncio
    async def test_leader_lock_already_held_by_self(self, monitor, mock_redis):
        """Test that worker recognizes it already holds the lock."""
        monitor.redis = mock_redis
        
        # Mock lock acquisition failure but worker already holds it
        mock_redis.client = AsyncMock()
        mock_redis.client.set = AsyncMock(return_value=False)
        mock_redis.get = AsyncMock(return_value=monitor.worker_id)
        
        result = await monitor._try_acquire_leader_lock()
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_leader_lock_renewal(self, monitor, mock_redis):
        """Test that leader can renew its lock."""
        monitor.redis = mock_redis
        monitor.is_leader = True
        
        # Mock successful renewal
        mock_redis.get = AsyncMock(return_value=monitor.worker_id)
        mock_redis.expire = AsyncMock(return_value=True)
        
        result = await monitor._renew_leader_lock()
        
        assert result is True
        mock_redis.expire.assert_called_once_with(
            "webhook:health:leader_lock",
            60
        )
    
    @pytest.mark.asyncio
    async def test_leader_lock_renewal_fails_if_lost(self, monitor, mock_redis):
        """Test that renewal fails if another worker took the lock."""
        monitor.redis = mock_redis
        monitor.is_leader = True
        
        # Mock lock now held by another worker
        mock_redis.get = AsyncMock(return_value="worker_9999")
        mock_redis.expire = AsyncMock()
        
        result = await monitor._renew_leader_lock()
        
        assert result is False
        mock_redis.expire.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_leader_lock_release(self, monitor, mock_redis):
        """Test that leader can release its lock."""
        monitor.redis = mock_redis
        monitor.is_leader = True
        
        # Mock successful release
        mock_redis.get = AsyncMock(return_value=monitor.worker_id)
        mock_redis.delete = AsyncMock(return_value=True)
        
        await monitor._release_leader_lock()
        
        mock_redis.delete.assert_called_once_with("webhook:health:leader_lock")
    
    @pytest.mark.asyncio
    async def test_leader_lock_not_released_by_non_leader(self, monitor, mock_redis):
        """Test that non-leader worker doesn't release another worker's lock."""
        monitor.redis = mock_redis
        monitor.is_leader = False
        
        # Mock lock held by another worker
        mock_redis.get = AsyncMock(return_value="worker_9999")
        mock_redis.delete = AsyncMock()
        
        await monitor._release_leader_lock()
        
        mock_redis.delete.assert_not_called()


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
