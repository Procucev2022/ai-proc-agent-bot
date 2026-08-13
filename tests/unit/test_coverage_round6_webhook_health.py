import asyncio
import json
from datetime import datetime as RealDateTime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest

from app.services import webhook_health_monitor_service as health_module


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, closed=False):
        self.closed = closed
        self.close = AsyncMock(side_effect=self._mark_closed)

    async def _mark_closed(self):
        self.closed = True


def health_service(recipients=None):
    redis = MagicMock()
    redis.init_client = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.expire = AsyncMock()
    redis.delete = AsyncMock()
    redis.client = SimpleNamespace(set=AsyncMock(return_value=True))

    email_service = SimpleNamespace(
        send_email_by_template=AsyncMock(return_value={"status": "Success"})
    )
    service = health_module.WebhookHealthMonitorService.__new__(
        health_module.WebhookHealthMonitorService
    )
    service.settings = SimpleNamespace(
        WHATSAPP_BASE_URL="https://wa.invalid",
        WHATSAPP_USERNAME="username",
        WHATSAPP_PASSWORD="password",
        webhook_alert_state_ttl_seconds=90,
        webhook_health_monitoring_enabled=True,
    )
    service.redis = redis
    service.email_service = email_service
    service.check_interval = 5
    service.response_threshold = 1.0
    service.api_timeout = 3.0
    service.grace_period = 10
    service.recovery_confirmations = 2
    service.warning_threshold = 2
    service.alert_recipients = [] if recipients is None else recipients
    service.worker_id = "worker-test"
    service.is_leader = False
    service._running = False
    service._task = None
    service._session = None
    return service


def health_state(current="HEALTHY", **overrides):
    state = {
        "current_state": current,
        "is_alerting": False,
        "last_alert_time": None,
        "failure_start_time": None,
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "consecutive_warnings": 0,
        "last_severity": "OK",
        "last_check_time": None,
        "last_latency_ms": None,
        "last_error": None,
    }
    state.update(overrides)
    return state


def patch_utcnow(monkeypatch, *values):
    clock = Mock(side_effect=list(values))
    monkeypatch.setattr(
        health_module,
        "datetime",
        SimpleNamespace(utcnow=clock, fromisoformat=RealDateTime.fromisoformat),
    )
    return clock


def patch_fixed_utcnow(monkeypatch, value=None):
    fixed = value or RealDateTime(2025, 1, 2, 3, 4, 5)
    clock = Mock(return_value=fixed)
    monkeypatch.setattr(
        health_module,
        "datetime",
        SimpleNamespace(utcnow=clock, fromisoformat=RealDateTime.fromisoformat),
    )
    return clock


class Response:
    def __init__(self, status):
        self.status = status


def session_for(response=None, error=None):
    session = MagicMock()
    session.post.return_value = AsyncContext(response, error=error)
    return session


def test_constructor_uses_mocked_config_and_logs_empty_recipients(monkeypatch):
    settings = SimpleNamespace(
        webhook_health_check_interval_seconds=11,
        webhook_api_response_threshold_seconds=1.5,
        webhook_api_timeout_seconds=3.0,
        webhook_failure_grace_period_seconds=20,
        webhook_recovery_confirmations=2,
        webhook_warning_consecutive_threshold=3,
        webhook_alert_recipients=[],
        webhook_health_monitoring_enabled=False,
    )
    redis = MagicMock()
    email_service = MagicMock()
    monkeypatch.setattr(health_module, "get_settings", Mock(return_value=settings))
    monkeypatch.setattr(health_module, "get_redis_service", Mock(return_value=redis))
    monkeypatch.setattr(health_module, "EmailService", Mock(return_value=email_service))
    monkeypatch.setattr(health_module.os, "getpid", Mock(return_value=1234))

    service = health_module.WebhookHealthMonitorService()

    assert service.settings is settings
    assert service.redis is redis
    assert service.email_service is email_service
    assert service.worker_id == "worker_1234"
    assert service.alert_recipients == []
    assert service.check_interval == 11
    assert service.response_threshold == 1.5
    assert service.api_timeout == 3.0
    assert service.grace_period == 20


@pytest.mark.asyncio
async def test_session_creation_reuse_and_cleanup_are_fully_mocked(monkeypatch):
    service = health_service()
    first = FakeSession()
    second = FakeSession()
    timeout = object()
    timeout_factory = Mock(return_value=timeout)
    session_factory = Mock(side_effect=[first, second])
    monkeypatch.setattr(health_module.aiohttp, "ClientTimeout", timeout_factory)
    monkeypatch.setattr(health_module.aiohttp, "ClientSession", session_factory)

    assert await service._get_session() is first
    timeout_factory.assert_called_once_with(total=service.api_timeout)
    session_factory.assert_called_once_with(timeout=timeout)

    assert await service._get_session() is first
    first.closed = True
    assert await service._get_session() is second
    assert session_factory.call_count == 2

    await service._close_session()
    second.close.assert_awaited_once()
    assert service._session is None
    service._session = FakeSession(closed=True)
    closed_session = service._session
    await service._close_session()
    closed_session.close.assert_not_awaited()
    service._session = None
    await service._close_session()


@pytest.mark.asyncio
async def test_interruptible_sleep_covers_deadline_and_shutdown(monkeypatch):
    service = health_service()
    loop = SimpleNamespace(time=Mock(side_effect=[10.0, 10.0]))
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "get_running_loop", Mock(return_value=loop))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    service._running = True

    await service._interruptible_sleep(0)
    sleep.assert_not_awaited()

    service._running = True
    loop.time.side_effect = [0.0, 0.25]

    async def stop_after_sleep(_seconds):
        service._running = False

    sleep.side_effect = stop_after_sleep
    await service._interruptible_sleep(1.0)
    sleep.assert_awaited_once()
    assert sleep.await_args.args[0] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_start_monitoring_handles_standby_leader_and_cleanup_exception():
    service = health_service()
    service._try_acquire_leader_lock = AsyncMock(side_effect=[False, True])
    service._interruptible_sleep = AsyncMock()

    async def finish_leader_cycle():
        service._running = False

    service._run_as_leader = AsyncMock(side_effect=finish_leader_cycle)
    service._release_leader_lock = AsyncMock()
    service._close_session = AsyncMock()

    await service.start_monitoring()

    assert service._try_acquire_leader_lock.await_count == 2
    service._interruptible_sleep.assert_awaited_once_with(service.check_interval)
    service._run_as_leader.assert_awaited_once()
    service._release_leader_lock.assert_awaited_once()
    service._close_session.assert_awaited_once()
    assert service.is_leader is True

    service = health_service()
    service._try_acquire_leader_lock = AsyncMock(return_value=False)
    service._interruptible_sleep = AsyncMock(
        side_effect=lambda _seconds: setattr(service, "_running", False)
    )
    service._release_leader_lock = AsyncMock(side_effect=RuntimeError("release"))
    service._close_session = AsyncMock()

    await service.start_monitoring()

    service._release_leader_lock.assert_awaited_once()
    service._close_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_monitoring_returns_when_running_or_disabled():
    service = health_service()
    service._running = True
    service._try_acquire_leader_lock = AsyncMock()
    await service.start_monitoring()
    service._try_acquire_leader_lock.assert_not_awaited()

    service._running = False
    service.settings.webhook_health_monitoring_enabled = False
    await service.start_monitoring()
    service._try_acquire_leader_lock.assert_not_awaited()


@pytest.mark.asyncio
async def test_leader_lock_acquisition_renewal_and_release_error_paths():
    service = health_service()
    service.redis.client.set.return_value = False
    service.redis.get.return_value = service.worker_id
    assert await service._try_acquire_leader_lock() is True
    service.redis.get.return_value = "another-worker"
    assert await service._try_acquire_leader_lock() is False
    service.redis.init_client.side_effect = RuntimeError("redis unavailable")
    assert await service._try_acquire_leader_lock() is False

    service.redis.init_client.side_effect = None
    service.redis.get.side_effect = None
    service.redis.get.return_value = service.worker_id
    service.redis.expire.side_effect = RuntimeError("expire failed")
    assert await service._renew_leader_lock() is False
    service.redis.expire.side_effect = None
    service.redis.get.return_value = "another-worker"
    assert await service._renew_leader_lock() is False

    service.redis.get.return_value = service.worker_id
    service.redis.delete = AsyncMock()
    await service._release_leader_lock()
    service.redis.delete.assert_awaited_once_with(service.LEADER_LOCK_KEY)

    service.redis.init_client.side_effect = RuntimeError("release failed")
    service._running = True
    await service._release_leader_lock()
    service._running = False
    await service._release_leader_lock()


@pytest.mark.asyncio
async def test_run_as_leader_success_renewal_failure_and_exception_lifecycle():
    service = health_service()
    service._renew_leader_lock = AsyncMock(return_value=True)
    service._health_check_cycle = AsyncMock()
    service._interruptible_sleep = AsyncMock()
    await service._run_as_leader()
    service._renew_leader_lock.assert_awaited_once()
    service._health_check_cycle.assert_awaited_once()
    service._interruptible_sleep.assert_awaited_once_with(service.check_interval)

    service.is_leader = True
    service._renew_leader_lock = AsyncMock(return_value=False)
    await service._run_as_leader()
    assert service.is_leader is False

    for running in (True, False):
        service._running = running
        service.is_leader = True
        service._renew_leader_lock = AsyncMock(side_effect=RuntimeError("cycle failed"))
        await service._run_as_leader()
        assert service.is_leader is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [health_module.HealthStatus.WARNING, health_module.HealthStatus.CRITICAL])
async def test_health_check_cycle_dispatches_warning_and_critical_logging(status):
    service = health_service()
    state = health_state()
    service._check_api_health = AsyncMock(return_value=(status, 123.4, "degraded"))
    service._get_state = AsyncMock(return_value=state)
    service._process_check_result = AsyncMock()
    service._add_to_history = AsyncMock()

    await service._health_check_cycle()

    service._process_check_result.assert_awaited_once_with(state, status, 123.4, "degraded")
    service._add_to_history.assert_awaited_once_with(status, 123.4, "degraded")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, expected_status, expected_error, elapsed",
    [
        (200, health_module.HealthStatus.OK, None, 0.1),
        (200, health_module.HealthStatus.WARNING, "Slow response", 1.5),
        (400, health_module.HealthStatus.CRITICAL, "Client error: 400", 0.1),
        (499, health_module.HealthStatus.CRITICAL, "Client error: 499", 0.1),
        (500, health_module.HealthStatus.CRITICAL, "Server error: 500", 0.1),
        (503, health_module.HealthStatus.CRITICAL, "Server error: 503", 0.1),
        (201, health_module.HealthStatus.CRITICAL, "Unexpected status: 201", 0.1),
    ],
)
async def test_api_health_status_classification_is_deterministic(
    monkeypatch, status, expected_status, expected_error, elapsed
):
    service = health_service()
    service.response_threshold = 1.0
    start = RealDateTime(2025, 1, 1)
    patch_utcnow(monkeypatch, start, start + timedelta(seconds=elapsed))
    response = Response(status)
    session = session_for(response)
    service._get_session = AsyncMock(return_value=session)

    result = await service._check_api_health()

    assert result[0] is expected_status
    assert result[1] == round(elapsed * 1000, 2)
    if expected_error is None:
        assert result[2] is None
    else:
        assert expected_error in result[2]
    session.post.assert_called_once_with(
        "https://wa.invalid/qualitycheck",
        json={"user": "username", "pass": "password"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception, expected_text",
    [
        (asyncio.TimeoutError(), "Timeout after 3.0s"),
        (aiohttp.ClientError("offline"), "Connection error: offline"),
        (RuntimeError("unexpected"), "Unexpected error: unexpected"),
    ],
)
async def test_api_health_connection_timeout_and_unexpected_errors(
    monkeypatch, exception, expected_text
):
    service = health_service()
    start = RealDateTime(2025, 1, 1)
    patch_utcnow(monkeypatch, start, start + timedelta(seconds=2))
    service._get_session = AsyncMock(return_value=session_for(error=exception))

    status, latency, error = await service._check_api_health()

    assert status is health_module.HealthStatus.CRITICAL
    assert latency == 2000.0
    assert error == expected_text


@pytest.mark.asyncio
async def test_ok_state_edges_include_unconfirmed_recovery():
    service = health_service()
    service._send_recovery_notification = AsyncMock()

    state = health_state("HEALTHY", consecutive_successes=4, consecutive_failures=2, consecutive_warnings=3)
    await service._handle_ok_status(state, health_module.MonitorState.HEALTHY)
    assert state["current_state"] == "HEALTHY"
    assert state["consecutive_successes"] == 5
    assert state["consecutive_failures"] == 0
    assert state["consecutive_warnings"] == 0

    state = health_state("FAILING", failure_start_time="2025-01-01T00:00:00")
    await service._handle_ok_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "HEALTHY"
    assert state["failure_start_time"] is None

    state = health_state("ALERTING", is_alerting=True)
    await service._handle_ok_status(state, health_module.MonitorState.ALERTING)
    assert state["current_state"] == "RECOVERED"
    assert state["consecutive_successes"] == 1

    state = health_state("RECOVERED", consecutive_successes=0, is_alerting=True)
    await service._handle_ok_status(state, health_module.MonitorState.RECOVERED)
    assert state["current_state"] == "RECOVERED"
    service.recovery_confirmations = 2
    state["consecutive_successes"] = 1
    await service._handle_ok_status(state, health_module.MonitorState.RECOVERED)
    assert state["current_state"] == "HEALTHY"
    service._send_recovery_notification.assert_awaited_once_with(state)


@pytest.mark.asyncio
async def test_warning_state_edges_cover_grace_and_threshold_paths(monkeypatch):
    service = health_service()
    service.warning_threshold = 2
    service._send_warning_alert = AsyncMock()

    start = RealDateTime(2025, 1, 1)
    state = health_state(
        "HEALTHY",
        consecutive_warnings=0,
        consecutive_failures=0,
    )
    patch_fixed_utcnow(monkeypatch, start)
    await service._handle_warning_status(state, health_module.MonitorState.HEALTHY)
    assert state["current_state"] == "FAILING"
    assert state["failure_start_time"] == start.isoformat()

    state = health_state(
        "FAILING",
        failure_start_time=start.isoformat(),
        consecutive_warnings=0,
    )
    patch_utcnow(monkeypatch, start + timedelta(seconds=1))
    await service._handle_warning_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "FAILING"
    service._send_warning_alert.assert_not_awaited()

    state = health_state(
        "FAILING",
        failure_start_time=start.isoformat(),
        consecutive_warnings=0,
    )
    patch_utcnow(monkeypatch, start + timedelta(seconds=20))
    await service._handle_warning_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "FAILING"

    state = health_state(
        "FAILING",
        failure_start_time=start.isoformat(),
        consecutive_warnings=1,
        last_latency_ms=1200,
    )
    patch_utcnow(monkeypatch, start + timedelta(seconds=20))
    await service._handle_warning_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "ALERTING"
    assert state["is_alerting"] is True
    service._send_warning_alert.assert_awaited_once_with(state)

    state = health_state("RECOVERED", consecutive_successes=3)
    await service._handle_warning_status(state, health_module.MonitorState.RECOVERED)
    assert state["current_state"] == "ALERTING"
    assert state["consecutive_successes"] == 0
    assert service._send_warning_alert.await_count == 1


@pytest.mark.asyncio
async def test_critical_state_edges_cover_grace_alerting_and_relapse(monkeypatch):
    service = health_service()
    service._send_critical_alert = AsyncMock()
    service._send_relapse_alert = AsyncMock()
    start = RealDateTime(2025, 1, 1)

    state = health_state("HEALTHY")
    patch_fixed_utcnow(monkeypatch, start)
    await service._handle_critical_status(state, health_module.MonitorState.HEALTHY)
    assert state["current_state"] == "FAILING"
    assert state["failure_start_time"] == start.isoformat()

    state = health_state("FAILING", failure_start_time=start.isoformat())
    patch_utcnow(monkeypatch, start + timedelta(seconds=1))
    await service._handle_critical_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "FAILING"

    state = health_state(
        "FAILING", failure_start_time=start.isoformat(), is_alerting=True
    )
    patch_utcnow(monkeypatch, start + timedelta(seconds=20))
    await service._handle_critical_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "FAILING"

    state = health_state("FAILING", failure_start_time=start.isoformat())
    patch_utcnow(monkeypatch, start + timedelta(seconds=20))
    await service._handle_critical_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "ALERTING"
    assert state["is_alerting"] is True
    service._send_critical_alert.assert_awaited_once_with(state)

    state = health_state("RECOVERED", consecutive_successes=2)
    await service._handle_critical_status(state, health_module.MonitorState.RECOVERED)
    assert state["current_state"] == "ALERTING"
    assert state["consecutive_successes"] == 0
    service._send_relapse_alert.assert_awaited_once_with(state)


@pytest.mark.asyncio
async def test_alerts_cover_success_failure_exception_and_no_recipient_paths(monkeypatch):
    now = RealDateTime(2025, 1, 2, 3, 4, 5)
    service = health_service([" ops@example.com ", "backup@example.com"])
    patch_fixed_utcnow(monkeypatch, now)
    state = health_state(
        "ALERTING",
        failure_start_time=(now - timedelta(minutes=2)).isoformat(),
        consecutive_failures=3,
        consecutive_warnings=3,
        last_latency_ms=1500,
        last_error="down",
        last_severity="CRITICAL",
    )

    await service._send_critical_alert(state)
    assert service.email_service.send_email_by_template.await_args.args[0] == "webhook_api_critical"
    assert state["last_alert_time"] == now.isoformat()

    service.email_service.send_email_by_template.return_value = {"status": "Failed"}
    previous_alert_time = state["last_alert_time"]
    await service._send_warning_alert(state)
    assert state["last_alert_time"] == previous_alert_time

    service.email_service.send_email_by_template.side_effect = RuntimeError("mail")
    await service._send_relapse_alert(state)
    await service._send_recovery_notification(state)

    no_recipient = health_service([])
    await no_recipient._send_critical_alert(state)
    await no_recipient._send_warning_alert(state)
    await no_recipient._send_recovery_notification(state)
    await no_recipient._send_relapse_alert(state)
    no_recipient.email_service.send_email_by_template.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_notification_without_failure_start_uses_unknown_downtime(monkeypatch):
    service = health_service(["ops@example.com"])
    now = RealDateTime(2025, 1, 2, 3, 4, 5)
    patch_fixed_utcnow(monkeypatch, now)
    state = health_state("HEALTHY", failure_start_time=None, last_latency_ms=12)

    await service._send_recovery_notification(state)

    service.email_service.send_email_by_template.assert_awaited_once()
    template, variables = service.email_service.send_email_by_template.await_args.args
    assert template == "webhook_api_recovery"
    assert variables["total_downtime"] == "Unknown"
    assert variables["current_response_time"] == "12ms"


@pytest.mark.asyncio
async def test_state_and_history_persistence_covers_empty_existing_invalid_and_errors(
    monkeypatch,
):
    service = health_service()
    patch_fixed_utcnow(monkeypatch, RealDateTime(2025, 1, 3))

    service.redis.get.return_value = None
    default = await service._get_state()
    assert default["current_state"] == "HEALTHY"
    service.redis.get.return_value = json.dumps(health_state())
    assert (await service._get_state())["current_state"] == "HEALTHY"
    service.redis.get.return_value = "not-json"
    assert (await service._get_state())["current_state"] == "HEALTHY"
    service.redis.get.side_effect = RuntimeError("get state")
    assert (await service._get_state())["current_state"] == "HEALTHY"

    service.redis.get.side_effect = None
    service.redis.set = AsyncMock()
    await service._save_state(health_state())
    service.redis.set.assert_awaited_with(
        service.STATE_KEY,
        json.dumps(health_state()),
        ex=service.settings.webhook_alert_state_ttl_seconds,
    )
    service.redis.set.side_effect = RuntimeError("set state")
    await service._save_state(health_state())

    service.redis.set = AsyncMock()
    service.redis.get.return_value = None
    await service._add_to_history(health_module.HealthStatus.OK, 10, None)
    history = json.loads(service.redis.set.await_args.args[1])
    assert len(history) == 1

    service.redis.get.return_value = json.dumps([{"status": "OK"}])
    await service._add_to_history(health_module.HealthStatus.WARNING, 20, "slow")
    assert len(json.loads(service.redis.set.await_args.args[1])) == 2

    service.redis.get.return_value = json.dumps([{"i": i} for i in range(101)])
    await service._add_to_history(health_module.HealthStatus.CRITICAL, None, "down")
    history = json.loads(service.redis.set.await_args.args[1])
    assert len(history) == 100
    assert history[-1]["status"] == "CRITICAL"

    service.redis.get.return_value = "invalid-history"
    await service._add_to_history(health_module.HealthStatus.OK, 1, None)
    service.redis.get.side_effect = RuntimeError("history")
    await service._add_to_history(health_module.HealthStatus.OK, 1, None)


def test_duration_formatting_covers_zero_seconds_with_nonzero_parts():
    formatter = health_module.WebhookHealthMonitorService._format_duration
    assert formatter(timedelta()) == "0s"
    assert formatter(timedelta(seconds=7)) == "7s"
    assert formatter(timedelta(minutes=2)) == "2m"
    assert formatter(timedelta(hours=1)) == "1h"
    assert formatter(timedelta(hours=1, minutes=2)) == "1h 2m"
