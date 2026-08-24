"""Deterministic residual branch tests for timeout, queue, and Excel services."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.excel_validation_service as excel_mod
import app.services.inactivity_timeout_service as timeout_mod
import app.services.message_queue_service as queue_mod


class FakeLock:
    def __init__(self, acquired=True, release_error=None):
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock(side_effect=release_error)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class AsyncResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class DownloadSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, _url):
        if self.error:
            return AsyncResponseContext(self.error)
        return AsyncResponseContext(self.response)


class DownloadResponse:
    def __init__(self, status, body=b"excel"):
        self.status = status
        self.body = body

    async def read(self):
        return self.body


class DbContext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


def timeout_service(redis=None, redis_session=None):
    service = timeout_mod.InactivityTimeoutService.__new__(timeout_mod.InactivityTimeoutService)
    service.redis = redis or MagicMock()
    service.redis_session = redis_session or MagicMock()
    service.timeout_seconds = 10
    service.poll_interval = 1
    service.activity_key_ttl = 30
    service.enabled = True
    service.worker_timeout_threshold = 5
    service.pending_reply_ttl = 20
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service._monitor_task = None
    return service


def queue_service(redis=None):
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = redis or MagicMock()
    service.redis.set = AsyncMock(return_value=True)
    service.redis.zadd = AsyncMock()
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.setex = AsyncMock()
    service.redis.zrange = AsyncMock(return_value=[])
    service.redis.zrem = AsyncMock()
    service.redis.rpush = AsyncMock()
    service.redis.lpop = AsyncMock(return_value=None)
    service.redis.lpush = AsyncMock()
    service.redis.scan = AsyncMock(return_value=(0, []))
    service.redis.get = AsyncMock(return_value=None)
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service.redis.delete = AsyncMock()
    service.redis.close = AsyncMock()
    service.batch_window = 3
    service.please_wait_threshold = 10
    service.max_please_wait_count = 2
    service.monitoring_poll_interval = 1
    service.response_ready_ttl = 60
    service.monitor_lock_ttl = 30
    service.please_wait_interval_ttl = 60
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    service._background_tasks = []
    service._running = True
    return service


@pytest.mark.asyncio
async def test_timeout_activity_lock_success_busy_and_double_failure():
    service = timeout_service()
    lock = FakeLock(acquired=True)
    service.redis.lock.return_value = lock
    service.redis.setex = AsyncMock()

    await service.update_user_activity("+1555")
    service.redis.setex.assert_awaited_once_with("1555:last_activity", 30, pytest.approx(service.redis.setex.call_args.args[2]))
    lock.release.assert_awaited_once()

    lock.acquire.return_value = False
    await service.update_user_activity("+1555")
    assert service.redis.setex.await_count == 2

    lock.acquire.side_effect = RuntimeError("lock unavailable")
    service.redis.setex.side_effect = [RuntimeError("first write"), RuntimeError("fallback write")]
    await service.update_user_activity("+1555")
    assert service.redis.setex.await_count == 3


@pytest.mark.asyncio
async def test_timeout_lifecycle_startup_availability_stop_and_monitor_lock(monkeypatch):
    service = timeout_service()
    service.enabled = False
    await service.start_monitoring()
    assert service._monitor_task is None
    assert await service.try_start_monitoring_if_available() is False

    service.enabled = True
    service._monitor_task = SimpleNamespace(done=lambda: False)
    assert await service.start_monitoring() is None
    assert await service.try_start_monitoring_if_available() is True

    # Replace the created task with a harmless captured coroutine for the remaining branches.
    service._monitor_task = None
    lock = FakeLock(acquired=False)
    service.redis.lock.return_value = lock
    created = []

    def fake_create_task(coro):
        coro.close()
        task = SimpleNamespace(done=lambda: False)
        created.append(task)
        return task

    monkeypatch.setattr(timeout_mod.asyncio, "create_task", fake_create_task)
    assert await service.try_start_monitoring_if_available() is False
    lock.acquire.return_value = True
    assert await service.try_start_monitoring_if_available() is True
    lock.acquire.side_effect = RuntimeError("redis lock error")
    service._monitor_task = None
    assert await service.try_start_monitoring_if_available() is True
    assert len(created) == 2
    service._monitor_task = None

    class CancelledTask:
        def __init__(self):
            self.was_cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.was_cancelled = True

        def __await__(self):
            async def wait():
                raise asyncio.CancelledError()
            return wait().__await__()

    task = CancelledTask()
    service._monitor_task = task
    await service.stop_monitoring()
    assert task.was_cancelled

    # Exercise both the skipped-lock cycle and cancellation exit without Redis calls.
    service._monitor_task = None
    service.redis.lock.return_value = FakeLock(acquired=False, release_error=RuntimeError("release"))
    sleeps = iter([None, asyncio.CancelledError()])

    async def cancel_sleep(_seconds):
        value = next(sleeps)
        if isinstance(value, BaseException):
            raise value

    monkeypatch.setattr(timeout_mod.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await service._run_monitor_loop()


@pytest.mark.asyncio
async def test_timeout_monitor_cycle_releases_lock_and_handles_check_errors(monkeypatch):
    service = timeout_service()
    lock = FakeLock(acquired=True, release_error=RuntimeError("release failed"))
    service.redis.lock.return_value = lock
    service._check_inactive_users = AsyncMock(side_effect=RuntimeError("check failed"))
    sleeps = iter([None, asyncio.CancelledError()])

    async def cancel_after_cycle(_seconds):
        value = next(sleeps)
        if isinstance(value, BaseException):
            raise value

    monkeypatch.setattr(timeout_mod.asyncio, "sleep", cancel_after_cycle)
    with pytest.raises(asyncio.CancelledError):
        await service._run_monitor_loop()
    service._check_inactive_users.assert_awaited_once()
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_scan_worker_priority_and_normal_session_branches(monkeypatch):
    service = timeout_service()
    service.redis.scan = AsyncMock(side_effect=[(1, ["1:last_activity"]), (0, ["2:last_activity"])])
    service.redis.get = AsyncMock(side_effect=["0", "0"])
    service.redis.exists = AsyncMock(side_effect=[1, 0])
    service.redis.delete = AsyncMock()
    service.redis_session.get_session = AsyncMock(return_value={"workflow_type": "rfq_creation", "outcome": None})
    service._handle_worker_timeout = AsyncMock()
    service._handle_timeout = AsyncMock()
    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100.0)

    await service._check_inactive_users()
    service._handle_worker_timeout.assert_awaited_once_with("1", "whatsapp_1_" + datetime.now().strftime("%Y%m%d"), "1:last_activity", "1:pending_reply")
    service._handle_timeout.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_scan_db_fallback_completed_and_error_paths(monkeypatch):
    service = timeout_service()
    service.redis.scan = AsyncMock(return_value=(0, ["1:last_activity"]))
    service.redis.get = AsyncMock(return_value="0")
    service.redis.exists = AsyncMock(return_value=0)
    service.redis.delete = AsyncMock()
    service.redis_session.get_session = AsyncMock(return_value=None)
    service._handle_timeout = AsyncMock()
    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100.0)
    monkeypatch.setattr(timeout_mod.SessionHelpers, "generate_session_id", lambda *_args: "daily-session")

    no_row_db = SimpleNamespace(get_conversation_session=MagicMock(return_value=None), close=MagicMock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: no_row_db)
    await service._check_inactive_users()
    service.redis.delete.assert_awaited_once_with("1:last_activity")
    no_row_db.close.assert_called_once()

    completed_db = SimpleNamespace(
        session_id="daily-session", external_user_id="1", workflow_type=None, outcome=None,
        workflow_state={}, conversation_history={}, extracted_entities={}, retention_date=None,
        created_at=None, last_activity_at=None, completed_at=None,
    )
    db = SimpleNamespace(get_conversation_session=MagicMock(return_value=completed_db), close=MagicMock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)
    monkeypatch.setattr("app.services.workflow_manager.WorkflowManager.get_workflow_type", lambda _session: SimpleNamespace(value="rfq_creation"))
    await service._check_inactive_users()
    service._handle_timeout.assert_awaited_once()

    db.get_conversation_session.side_effect = RuntimeError("database unavailable")
    await service._check_inactive_users()
    assert service.redis.delete.await_count >= 2


@pytest.mark.asyncio
async def test_timeout_worker_timeout_persists_resets_cleans_and_tolerates_failures(monkeypatch):
    session = {
        "session_id": "S", "external_user_id": "1", "workflow_type": "rfq_creation",
        "outcome": None, "workflow_state": {}, "conversation_history": "bad",
        "extracted_entities": [], "retention_date": None, "created_at": None,
        "last_activity_at": None, "completed_at": None,
    }
    redis_session = MagicMock()
    redis_session.get_session = AsyncMock(return_value=session)
    redis_session.save_session = AsyncMock()
    service = timeout_service(redis_session=redis_session)
    service.redis.delete = AsyncMock(return_value=7)
    persisted = {}
    db = SimpleNamespace(
        save_conversation_session=MagicMock(side_effect=lambda value: persisted.update(value)),
        close=MagicMock(),
    )
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)

    await service._handle_worker_timeout("+1", "S", "1:last_activity", "1:pending_reply")
    saved = persisted
    assert saved["outcome"] == "abandoned"
    assert len(saved["conversation_history"]["messages"]) == 1
    redis_session.save_session.assert_awaited_once()
    assert service.redis.delete.await_count >= 3
    service.whatsapp_service.send_message.assert_awaited_once()

    redis_session.get_session.side_effect = RuntimeError("read")
    redis_session.save_session.side_effect = RuntimeError("reset")
    service.redis.delete.side_effect = RuntimeError("delete")
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    await service._handle_worker_timeout("1", "S", "activity", "pending")


@pytest.mark.asyncio
async def test_timeout_message_types_seller_fallback_and_generic(monkeypatch):
    service = timeout_service()
    buyer = SimpleNamespace(self_client=True)
    assert "resume creating RFQs" in await service._generate_timeout_message([buyer], {})

    seller = SimpleNamespace(self_client=False, org_id="org", phone_number="1")
    seller_service = SimpleNamespace(handle_seller_flow_completion=AsyncMock(return_value={"success": True, "message": "seller remainder"}))
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda: seller_service)
    assert await service._generate_timeout_message([seller], {"session_id": "S"}) == "seller remainder"
    seller_service.handle_seller_flow_completion.side_effect = RuntimeError("seller")
    assert "away" in await service._generate_timeout_message([seller], {"session_id": "S"})
    assert "resume anytime" in await service._generate_timeout_message(None, None)


@pytest.mark.asyncio
async def test_timeout_handle_timeout_race_fallback_and_notification_failure(monkeypatch):
    service = timeout_service()
    service.redis.delete = AsyncMock()
    service.redis.get = AsyncMock(return_value="95")
    service.redis_session.get_session = AsyncMock(return_value={"workflow_type": "rfq", "conversation_history": {}})
    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100.0)
    await service._handle_timeout("+1", "S", "1:last_activity")
    service.redis.delete.assert_not_awaited()

    service.redis.get.return_value = "0"
    service.redis_session.get_session.return_value = None
    auth = SimpleNamespace(retrieve=AsyncMock(side_effect=RuntimeError("auth")))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    service.whatsapp_service.send_message.side_effect = RuntimeError("WhatsApp")
    await service._handle_timeout("+1", "S", "1:last_activity")
    service.whatsapp_service.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_enqueue_polling_and_batch_retry_lock_paths(monkeypatch):
    service = queue_service()
    service.redis.zadd = AsyncMock()
    service.redis.exists = AsyncMock(return_value=False)
    service._create_batch = AsyncMock()
    await service.enqueue_message({"from": "+1", "timestamp": "2024-01-01 00:00:00", "text": {"body": "hello"}})
    service._create_batch.assert_not_awaited()
    service.redis.exists.return_value = True
    service._refresh_batch_timer = AsyncMock()
    await service.enqueue_message({"from": "1", "timestamp": 2, "type": "text", "text": {"body": "again"}})
    service._refresh_batch_timer.assert_awaited_once_with("1")
    # An unreadable gateway timestamp must not cost the message. This used to
    # raise ValueError out of enqueue_message, which logged an error, re-raised,
    # and left the user with no reply at all.
    service.redis.zadd.reset_mock()
    await service.enqueue_message({"from": "1", "timestamp": "not-a-date", "type": "text", "text": {"body": "still me"}})
    service.redis.zadd.assert_awaited()

    poll_lock = FakeLock(acquired=True, release_error=RuntimeError("release"))
    service = queue_service()
    service.redis.lock.return_value = poll_lock
    service.redis.scan = AsyncMock(return_value=(0, ["1:incoming"]))
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.zcard = AsyncMock(return_value=2)
    service._create_batch = AsyncMock()
    sleeps = {"count": 0}

    async def stop_poller(_seconds):
        sleeps["count"] += 1
        if sleeps["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_poller)
    await service.run_batch_poller()
    service._create_batch.assert_awaited_once_with("1")

    service = queue_service()
    service.redis.lock.return_value = FakeLock(acquired=True)
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.zrange = AsyncMock(return_value=["not-json"])
    service.redis.zrem = AsyncMock()
    service.redis.delete = AsyncMock()
    service._try_start_processing = AsyncMock()
    await service._create_batch("1")
    service.redis.delete.assert_awaited_once_with("1:incoming")

    valid = queue_mod.Message("m", "1", "hello", "text", 1, {}).to_dict()
    service.redis.zrange.return_value = [json.dumps(valid)]
    service.redis.rpush = AsyncMock()
    await service._create_batch("1")
    service._try_start_processing.assert_awaited_once_with("1")

    service = queue_service()
    service.redis.lock.return_value = FakeLock(acquired=True)
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.lpop = AsyncMock(return_value="bad-json")
    service.redis.lpush = AsyncMock()
    await service._try_start_processing("1")
    service.redis.lpush.assert_awaited_once_with("1:outgoing", "bad-json")


@pytest.mark.asyncio
async def test_queue_monitor_response_races_claim_retry_and_session_removal(monkeypatch):
    def session_json(count=0):
        return queue_mod.ProcessingSession("b", 0, please_wait_sent_count=count).to_json()

    async def one_cycle(service, monkeypatch, get_values, set_values, send=None, exists_value=True):
        service.redis.scan = AsyncMock(return_value=(0, ["1:session"]))
        service.redis.get = AsyncMock(side_effect=get_values)
        service.redis.set = AsyncMock(side_effect=set_values)
        service.redis.exists = AsyncMock(return_value=exists_value)
        service.redis.setex = AsyncMock()
        service.redis.delete = AsyncMock()
        service._send_please_wait = AsyncMock(side_effect=send)
        ticks = {"count": 0}

        async def sleep(_seconds):
            ticks["count"] += 1
            if ticks["count"] > 1:
                service._running = False

        monkeypatch.setattr(queue_mod.asyncio, "sleep", sleep)
        monkeypatch.setattr(queue_mod.time, "time", lambda: 20.0)
        await service.run_monitoring_loop()

    # Response ready before lock: no lock claim and no send.
    service = queue_service()
    await one_cycle(service, monkeypatch, [session_json(), "ready"], [True])
    service.redis.set.assert_not_awaited()
    service._send_please_wait.assert_not_awaited()

    # Response becomes ready after lock acquisition: double-check skips the send.
    service = queue_service()
    await one_cycle(service, monkeypatch, [session_json(), None, "ready"], [True])
    assert service.redis.set.await_count == 1
    service._send_please_wait.assert_not_awaited()

    # Interval already claimed: no send.
    service = queue_service()
    await one_cycle(service, monkeypatch, [session_json(), None, None, session_json()], [True, False])
    service._send_please_wait.assert_not_awaited()

    # Send failure releases the interval claim so a later monitor cycle can retry.
    service = queue_service()
    await one_cycle(service, monkeypatch, [session_json(), None, None, session_json()], [True, True], RuntimeError("send"))
    service.redis.delete.assert_awaited_once_with("1:please_wait:interval:2")

    # A session removed during a successful send is not rewritten.
    service = queue_service()
    service.redis.exists = AsyncMock(return_value=False)
    await one_cycle(service, monkeypatch, [session_json(), None, None, session_json()], [True, True], exists_value=False)
    service._send_please_wait.assert_awaited_once_with("1")
    service.redis.setex.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_processing_delegation_suppression_and_cleanup(monkeypatch):
    service = queue_service()
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.lpop = AsyncMock(return_value=json.dumps(queue_mod.Batch("b", "1", "body", "text", 1, 1).to_dict()))
    service.redis.setex = AsyncMock()
    created = []
    monkeypatch.setattr(queue_mod.asyncio, "create_task", lambda coro: created.append(coro))
    await service._try_start_processing("1")
    assert service.redis.setex.await_count == 2
    assert len(created) == 1
    created[0].close()

    service = queue_service()
    service.redis.get = AsyncMock(return_value=None)
    service.whatsapp_service.send_message = AsyncMock(return_value=SimpleNamespace(success=True))
    result = await service.send_message("+1", "direct")
    assert result.success
    service.whatsapp_service.send_message.assert_awaited_once_with("+1", "direct")

    service.redis.get.return_value = queue_mod.ProcessingSession("b", 1).to_json()
    service.redis.setex = AsyncMock()
    service._cleanup_and_next = AsyncMock()
    service._should_suppress_response = AsyncMock(return_value=True)
    suppressed = await service.send_message("+1", "newer")
    assert suppressed.success and suppressed.message_id is None
    service._cleanup_and_next.assert_awaited_once_with("b", "1", success=True)

    service.redis.get.return_value = queue_mod.ProcessingSession("b", 1).to_json()
    service._should_suppress_response.return_value = False
    service.whatsapp_service.send_message = AsyncMock(return_value=SimpleNamespace(success=False, error="remote"))
    result = await service.send_message("+1", "real")
    assert result.error == "remote"
    assert service._cleanup_and_next.await_count == 2

    service.whatsapp_service.send_message.side_effect = RuntimeError("remote down")
    with pytest.raises(RuntimeError):
        await service.send_message("+1", "fails")
    with pytest.raises(ValueError):
        await service.send_message("")

    service.redis.get.return_value = "not-json"
    with pytest.raises(json.JSONDecodeError):
        await service.send_message("1", "bad session")

    service.whatsapp_service.plain_value = "value"
    assert service.plain_value == "value"
    with pytest.raises(AttributeError):
        _ = service.not_present


@pytest.mark.asyncio
async def test_queue_process_batch_chat_success_failure_and_cleanup_branches(monkeypatch):
    service = queue_service()
    service.redis.exists = AsyncMock(return_value=True)
    service._cleanup_and_next = AsyncMock()
    chat = SimpleNamespace(process_message=AsyncMock(), cleanup=AsyncMock())

    class ChatFactory:
        def __init__(self, **_kwargs):
            pass

        async def process_message(self, **_kwargs):
            await chat.process_message(**_kwargs)

        async def cleanup(self):
            await chat.cleanup()

    monkeypatch.setattr("app.services.chat_service.ChatService", ChatFactory)
    monkeypatch.setattr("app.database.get_db_session_context", lambda: DbContext("db"))
    batch = queue_mod.Batch("b", "1", "body", "text", 1, 1)
    await service._process_batch(batch)
    chat.process_message.assert_awaited_once()
    service._cleanup_and_next.assert_awaited_once_with("b", "1", success=True)

    chat.process_message.side_effect = RuntimeError("chat failed")
    await service._process_batch(batch)
    assert service._cleanup_and_next.await_count == 2

    # Cleanup with empty queues clears acknowledgement; pending incoming merges instead.
    service = queue_service()
    service.redis.delete = AsyncMock()
    service.redis.scan = AsyncMock(return_value=(0, ["1:please_wait:interval:1"]))
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service._try_start_processing = AsyncMock()
    await service._cleanup_and_next("b", "1", True)
    assert service.redis.delete.await_count >= 4
    service.redis.zcard.return_value = 1
    service._create_batch = AsyncMock()
    await service._cleanup_and_next("b", "1", False)
    service._create_batch.assert_awaited_once_with("1")


@pytest.mark.asyncio
async def test_queue_status_health_shutdown_and_error_metrics():
    service = queue_service()
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service.redis.get = AsyncMock(return_value="bad-session")
    service.redis.exists = AsyncMock(return_value=False)
    status = await service.get_queue_status("1")
    assert status["session"] is None

    service.redis.scan = AsyncMock(side_effect=[
        (0, ["1:processing"]), (0, ["1:incoming"]), (0, ["1:outgoing"]), (0, ["1:session"]),
    ])
    service.redis.zcard.return_value = 2
    service.redis.llen.return_value = 1
    service.redis.get.return_value = queue_mod.ProcessingSession("b", 0).to_json()
    metrics = await service.get_health_metrics()
    assert metrics["processing_count"] == 1 and metrics["active_users"] == 1

    service.redis.scan.side_effect = RuntimeError("metrics unavailable")
    assert "error" in await service.get_health_metrics()
    service.redis.close = AsyncMock(side_effect=RuntimeError("close"))
    service._background_tasks = []
    await service.shutdown()
    assert service._running is False


@pytest.mark.asyncio
async def test_excel_download_retry_extension_integrity_and_content(monkeypatch):
    service = excel_mod.ExcelValidationService()
    responses = iter([DownloadResponse(500), DownloadResponse(200, b"payload")])
    monkeypatch.setattr(excel_mod.aiohttp, "ClientSession", lambda **_: DownloadSession(next(responses)))
    assert await service._download_file_with_retry("url") == b"payload"

    monkeypatch.setattr(excel_mod.aiohttp, "ClientSession", lambda **_: DownloadSession(DownloadResponse(404)))
    assert await service._download_file_with_retry("url") is None

    attempts = {"count": 0}

    def failing_session(**_kwargs):
        attempts["count"] += 1
        raise RuntimeError("network")

    monkeypatch.setattr(excel_mod.aiohttp, "ClientSession", failing_session)
    assert await service._download_file_with_retry("url", max_retries=2) is None
    assert attempts["count"] == 2
    assert service._validate_file_extension("x.XLSX")
    assert not service._validate_file_extension("x.csv")
    assert not service._validate_file_integrity(b"short")["valid"]
    assert not service._validate_file_integrity(b"\x00" * 8)["valid"]
    assert service._validate_excel_content(b"PK\x03\x04payload")["format"] == "xlsx"
    assert service._validate_excel_content(b"\xd0\xcf\x11\xe0payload")["format"] == "xls"
    assert service._validate_excel_content(b"a,b\n1,2")["error_type"] == "csv_format_detected"


@pytest.mark.asyncio
async def test_excel_readability_fallback_readers_and_security(monkeypatch):
    service = excel_mod.ExcelValidationService()
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame()))
    assert (await service._validate_excel_readability(b"data", "x.xlsx"))["valid"]

    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(side_effect=ValueError("protected workbook")))
    result = await service._validate_excel_readability(b"data", "x.xlsx")
    assert result["error_type"] == "password_protected"

    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(side_effect=ValueError("bad engine")))
    workbook = SimpleNamespace(close=MagicMock())
    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(return_value=workbook))
    assert (await service._validate_excel_readability(b"data", "x.xlsx"))["valid"]
    workbook.close.assert_called_once()

    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(side_effect=excel_mod.InvalidFileException("bad")))
    monkeypatch.setattr(excel_mod.xlrd, "open_workbook", MagicMock(return_value=object()))
    assert (await service._validate_excel_readability(b"data", "x.xls"))["valid"]
    monkeypatch.setattr(excel_mod.xlrd, "open_workbook", MagicMock(side_effect=RuntimeError("corrupt")))
    assert (await service._validate_excel_readability(b"data", "x.xls"))["error_type"] == "unreadable_file"

    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(side_effect=RuntimeError("unexpected")))
    assert (await service._validate_excel_readability(b"data", "x.xlsx"))["error_type"] == "readability_error"


@pytest.mark.asyncio
async def test_excel_structure_row_merge_pivot_hidden_and_pandas_fallback(monkeypatch):
    service = excel_mod.ExcelValidationService()
    worksheet = SimpleNamespace(
        iter_rows=lambda: [[SimpleNamespace(value="header"), SimpleNamespace(value="x")]],
        merged_cells=SimpleNamespace(ranges=[]), row_dimensions={}, column_dimensions={}, _pivots=[],
    )
    workbook = SimpleNamespace(worksheets=[worksheet], active=worksheet, close=MagicMock())
    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(return_value=workbook))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["valid"]

    hidden = SimpleNamespace(
        iter_rows=lambda: [[SimpleNamespace(value="h")]],
        merged_cells=SimpleNamespace(ranges=[]),
        row_dimensions={1: SimpleNamespace(hidden=True)}, column_dimensions={"A": SimpleNamespace(hidden=True)}, _pivots=[],
    )
    hidden_book = SimpleNamespace(worksheets=[hidden], active=hidden, close=MagicMock())
    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(return_value=hidden_book))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["valid"]

    multi = SimpleNamespace(worksheets=[worksheet, worksheet], active=worksheet, close=MagicMock())
    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(return_value=multi))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["error_type"] == "multiple_worksheets"

    pivot = SimpleNamespace(
        iter_rows=lambda: [[SimpleNamespace(value="h")]], merged_cells=SimpleNamespace(ranges=[]),
        row_dimensions={}, column_dimensions={}, _pivots=[object()],
    )
    pivot_book = SimpleNamespace(worksheets=[pivot], active=pivot, close=MagicMock())
    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(return_value=pivot_book))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["error_type"] == "pivot_tables_found"

    merged = SimpleNamespace(
        iter_rows=lambda: [[SimpleNamespace(value="h")]], merged_cells=SimpleNamespace(ranges=["A1:B1"]),
        row_dimensions={}, column_dimensions={}, _pivots=[],
    )
    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(return_value=SimpleNamespace(worksheets=[merged], active=merged, close=MagicMock())))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["error_type"] == "merged_cells_found"

    monkeypatch.setattr(excel_mod, "load_workbook", MagicMock(side_effect=RuntimeError("openpyxl")))
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A": range(51)})))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["error_type"] == "too_many_rows"
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(side_effect=RuntimeError("pandas")))
    assert (await service._validate_excel_structure_comprehensive(b"data"))["error_type"] == "structure_validation_error"


@pytest.mark.asyncio
async def test_excel_headers_quantity_and_data_quality_cases(monkeypatch):
    service = excel_mod.ExcelValidationService()
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame()))
    assert (await service._validate_data_quality(b"data"))["error_type"] == "no_data_found"

    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A": [1], "B": [2]})))
    assert (await service._validate_data_quality(b"data"))["error_type"] == "no_headers_detected"

    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"Item@": ["x"], "Qty": ["y"]})))
    assert (await service._validate_data_quality(b"data"))["error_type"] == "invalid_header_characters"

    monkeypatch.setattr(excel_mod.ExcelValidationService, "SPECIAL_CHARS_PATTERN", r"$^")
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"数量": ["x"], "項目": ["y"]})))
    assert (await service._validate_data_quality(b"data"))["error_type"] == "non_english_headers"

    monkeypatch.setattr(excel_mod.ExcelValidationService, "SPECIAL_CHARS_PATTERN", r"[^a-zA-Z0-9\s\-\._()&]")
    unnamed = pd.DataFrame({"Unnamed: 0": ["Bad@", "value"], "Unnamed: 1": ["Other", "x"]})
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=unnamed))
    assert (await service._validate_data_quality(b"data"))["error_type"] == "invalid_header_characters"

    mixed = pd.DataFrame({"ItemDescription": ["one", "two"], "Quantity": ["bad", "3"]})
    monkeypatch.setattr(excel_mod.pd, "read_excel", MagicMock(return_value=mixed))
    assert service._validate_data_types(mixed)
    assert (await service._validate_data_quality(b"data"))["error_type"] == "invalid_quantity_values"
    assert any("decimal" in issue for issue in service._validate_data_types(pd.DataFrame({"Quantity": [30.0]})))
    assert not service._validate_data_types(pd.DataFrame({"Unnamed: 0": [1, "bad"], "Unnamed: 1": ["x", "y"]}))


@pytest.mark.asyncio
async def test_excel_validation_pipeline_success_skip_and_error_boundaries(monkeypatch):
    service = excel_mod.ExcelValidationService()
    service._download_file_with_retry = AsyncMock(return_value=b"PK\x03\x04payload")
    service._validate_file_integrity = MagicMock(return_value={"valid": True})
    service._validate_excel_readability = AsyncMock(return_value={"valid": True})
    service._validate_excel_structure_comprehensive = AsyncMock(return_value={"valid": True})
    service._validate_data_quality = AsyncMock(return_value={"valid": True})
    result = await service.validate_excel_file_from_url("url", "upload.xlsx")
    assert result["valid"] and result["validation_summary"]["structure_check"] == "passed"
    skipped = await service.validate_excel_file_from_url("url", "upload.xlsx", skip_content_validation=True)
    assert skipped["validation_summary"]["data_quality_check"] == "skipped"

    assert (await service.validate_excel_file_from_url("url", "upload.txt"))["error_type"] == "invalid_extension"
    service._download_file_with_retry.return_value = None
    assert (await service.validate_excel_file_from_url("url", "upload.xlsx"))["error_type"] == "download_failed"
    service._download_file_with_retry.return_value = b"bytes"
    service._validate_file_integrity.return_value = {"valid": False, "error_type": "corrupted_file"}
    assert (await service.validate_excel_file_from_url("url", "upload.xlsx"))["error_type"] == "corrupted_file"
    service._validate_file_integrity.side_effect = RuntimeError("unexpected")
    assert (await service.validate_excel_file_from_url("url", "upload.xlsx"))["error_type"] == "validation_error"
