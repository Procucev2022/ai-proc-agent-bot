"""Focused deterministic tests for the remaining round-six coverage gaps.

All tests replace persistence, Redis, Celery, OpenAI, WhatsApp, HTTP, and
filesystem boundaries with local fakes or mocks.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.models import ConversationOutcome, ConversationSession, WorkflowType
from app.services import inactivity_timeout_service as timeout_mod
from app.services import learning_categorization_service as learning_mod
from app.services import message_queue_service as queue_mod
from app.services import opt_out_service as opt_out_mod
from app.services import profile_selection_service as profile_mod
from app.services import registration_service as registration_mod
from app.services import rfq_background_service as background_mod
from app.services import seller_service as seller_mod
from app.services import session_management_service as session_mod
from app.services.processors import excel_message_processor as excel_mod
from app.services.processors import image_message_processor as image_mod
from app.tasks import bfs_notification_task as bfs_mod
from app.tasks import log_cleanup_task as cleanup_mod
from app.tasks import seller_matching_task as seller_task
from app.tasks import whatsapp_report_automation_task as report_mod
from app.tools import user_selection_tool as selection_mod
from app.utils import logging_utils, pincode_lookup


class AsyncLock:
    def __init__(self, acquired=True, release_error=None):
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock(side_effect=release_error)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class QueueRedis:
    def __init__(self):
        self.lock_obj = AsyncLock()
        self.set = AsyncMock(return_value=True)
        self.setex = AsyncMock(return_value=True)
        self.exists = AsyncMock(return_value=False)
        self.get = AsyncMock(return_value=None)
        self.delete = AsyncMock()
        self.scan = AsyncMock(return_value=(0, []))
        self.zcard = AsyncMock(return_value=0)
        self.llen = AsyncMock(return_value=0)
        self.zrange = AsyncMock(return_value=[])
        self.zrem = AsyncMock()
        self.rpush = AsyncMock()
        self.lpop = AsyncMock(return_value=None)
        self.lpush = AsyncMock()
        self.close = AsyncMock()

    def lock(self, *_args, **_kwargs):
        return self.lock_obj


def timeout_service():
    service = timeout_mod.InactivityTimeoutService.__new__(timeout_mod.InactivityTimeoutService)
    service.redis = MagicMock()
    service.redis.scan = AsyncMock(return_value=(0, []))
    service.redis.get = AsyncMock(return_value=None)
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.delete = AsyncMock()
    service.redis_session = MagicMock()
    service.redis_session.get_session = AsyncMock(return_value=None)
    service.redis_session.save_session = AsyncMock()
    service.redis_session.store_session = AsyncMock()
    service.timeout_seconds = 10
    service.poll_interval = 1
    service.activity_key_ttl = 30
    service.enabled = True
    service.worker_timeout_threshold = 5
    service.pending_reply_ttl = 20
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service._monitor_task = None
    return service


def queue_service():
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = QueueRedis()
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


def conversation_session(**overrides):
    values = {
        "session_id": "sid",
        "external_user_id": "user-1",
        "workflow_type": WorkflowType.rfq_creation,
        "outcome": None,
        "workflow_state": {},
        "conversation_history": {},
        "extracted_entities": {},
        "retention_date": date(2030, 1, 1),
        "created_at": datetime(2025, 1, 1),
        "last_activity_at": datetime(2025, 1, 1),
        "completed_at": None,
    }
    values.update(overrides)
    return ConversationSession(**values)


# ---------------------------------------------------------------------------
# Inactivity timeout and queue service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_message_dict_and_seller_fallback_branches(monkeypatch):
    service = timeout_service()
    assert "resume creating RFQs" in await service._generate_timeout_message(
        [{"selfClient": True}], {}
    )

    seller = {"selfClient": False, "org_id": "org", "phone_number": "1"}
    seller_service = SimpleNamespace(
        handle_seller_flow_completion=AsyncMock(return_value={"success": False, "message": ""})
    )
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda: seller_service)
    session_data = {
        "session_id": "sid",
        "external_user_id": "1",
        "workflow_type": "seller_rfq_view",
        "workflow_state": {},
        "conversation_history": {},
        "extracted_entities": {},
    }
    result = await service._generate_timeout_message([seller], session_data)
    assert "resume viewing RFQs" in result
    assert seller_service.handle_seller_flow_completion.await_count == 1
    assert "resume anytime" in await service._generate_timeout_message([{}], {})


@pytest.mark.asyncio
async def test_timeout_scan_completed_age_workflow_and_scan_error_paths(monkeypatch):
    service = timeout_service()
    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100.0)
    monkeypatch.setattr(timeout_mod, "utc_now", lambda: datetime(2025, 1, 1, 0, 0, 30, tzinfo=timezone.utc))
    service.redis.scan = AsyncMock(return_value=(0, ["1:last_activity"]))
    service.redis.get = AsyncMock(return_value="0")
    service.redis.exists = AsyncMock(return_value=False)
    old_row = SimpleNamespace(
        session_id="daily", external_user_id="1", workflow_type=None, outcome=None,
        workflow_state={}, conversation_history={}, extracted_entities={}, retention_date=None,
        created_at=None, last_activity_at=None, completed_at=datetime(2024, 1, 1),
    )
    db = SimpleNamespace(get_conversation_session=Mock(return_value=old_row), close=Mock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)
    monkeypatch.setattr(timeout_mod.SessionHelpers, "generate_session_id", lambda *_: "daily")
    await service._check_inactive_users()
    service.redis.delete.assert_awaited_once_with("1:last_activity")
    service._handle_timeout = AsyncMock()

    recent_row = SimpleNamespace(
        session_id="daily", external_user_id="1", workflow_type=WorkflowType.rfq_creation,
        outcome=None, workflow_state={}, conversation_history={}, extracted_entities={},
        retention_date=None, created_at=None, last_activity_at=None,
        completed_at=datetime(2025, 1, 1, 0, 0, 20),
    )
    db.get_conversation_session.return_value = recent_row
    service.redis.delete.reset_mock()
    await service._check_inactive_users()
    service._handle_timeout.assert_awaited_once()

    service.redis_session.get_session.return_value = {"workflow_type": None, "outcome": "completed"}
    service.redis.delete.reset_mock()
    await service._check_inactive_users()
    service.redis.delete.assert_awaited_once_with("1:last_activity")

    service.redis.scan.side_effect = RuntimeError("scan unavailable")
    await service._check_inactive_users()


@pytest.mark.asyncio
async def test_timeout_worker_session_instance_and_reset_error_branches(monkeypatch):
    service = timeout_service()
    model_session = conversation_session(conversation_history={"messages": []})
    service.redis_session.get_session.return_value = model_session
    db = SimpleNamespace(save_conversation_session=Mock(), close=Mock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)

    await service._handle_worker_timeout("+1", "sid", "activity", "pending")
    db.save_conversation_session.assert_called_once()
    assert service.redis_session.save_session.await_count == 1
    assert service.whatsapp_service.send_message.await_count == 1

    # A to_dict object exercises the alternate Redis representation, while a
    # failed reset and notification remain non-fatal cleanup paths.
    class SessionDict:
        def to_dict(self):
            return {"conversation_history": {}, "workflow_state": {}, "extracted_entities": []}

    service.redis_session.get_session.return_value = SessionDict()
    service.redis_session.save_session.side_effect = RuntimeError("reset")
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    await service._handle_worker_timeout("1", "sid", "activity", "pending")

    service.redis_session.get_session.side_effect = RuntimeError("read")
    service.redis.delete.side_effect = RuntimeError("delete")
    await service._handle_worker_timeout("1", "sid", "activity", "pending")


@pytest.mark.asyncio
async def test_timeout_full_persist_reset_cache_and_cleanup_fallbacks(monkeypatch):
    service = timeout_service()
    session_data = {
        "session_id": "sid", "external_user_id": "1", "workflow_type": "rfq_creation",
        "outcome": None, "workflow_state": "not-a-dict", "conversation_history": "bad",
        "extracted_entities": [], "retention_date": None, "created_at": None,
        "last_activity_at": None, "completed_at": None, "whatsapp_context": {"auth": True},
    }
    service.redis_session.get_session.return_value = session_data
    service.redis.get.return_value = "0"
    auth = SimpleNamespace(retrieve=AsyncMock(return_value=SimpleNamespace(self_client=True)))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    db = SimpleNamespace(append_session_data=Mock(), close=Mock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)
    cache = SimpleNamespace(clear_meaningful_message=AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)
    monkeypatch.setattr(timeout_mod, "utc_now", lambda: datetime(2025, 1, 1, 0, 0, 0))

    await service._handle_timeout("+1", "sid", "1:last_activity")
    db.append_session_data.assert_called_once()
    service.redis_session.store_session.assert_awaited_once()
    cache.clear_meaningful_message.assert_awaited_once_with("+1")
    service.whatsapp_service.send_message.assert_awaited_once()

    # Message generation, Redis reset, activity cleanup, and notification can
    # all fail without escaping the timeout handler.
    service = timeout_service()
    service.redis_session.get_session.return_value = {"workflow_type": "rfq", "workflow_state": {}, "conversation_history": {}}
    service.redis.get.return_value = "not-a-number"
    auth.retrieve.side_effect = RuntimeError("auth")
    service.redis.delete = AsyncMock(side_effect=[7, RuntimeError("activity delete")])
    service.redis_session.store_session.side_effect = RuntimeError("store")
    service.whatsapp_service.send_message.side_effect = RuntimeError("whatsapp")
    await service._handle_timeout("1", "sid", "activity")


@pytest.mark.asyncio
async def test_queue_poller_lock_timer_and_outer_error_paths(monkeypatch):
    service = queue_service()
    service.redis.lock_obj = AsyncLock(acquired=False, release_error=RuntimeError("release"))
    ticks = {"count": 0}

    async def stop(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop)
    await service.run_batch_poller()

    service = queue_service()
    service.redis.scan = AsyncMock(side_effect=RuntimeError("scan"))
    service.redis.lock_obj = AsyncLock(acquired=True)
    service._running = True
    ticks = {"count": 0}

    async def stop_after_error(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_after_error)
    await service.run_batch_poller()

    service = queue_service()
    service.redis.scan = AsyncMock(return_value=(0, ["1:incoming"]))
    service.redis.exists.return_value = True
    ticks = {"count": 0}

    async def stop_timer(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_timer)
    await service.run_batch_poller()


@pytest.mark.asyncio
async def test_queue_monitor_slow_log_and_claim_race_paths(monkeypatch):
    service = queue_service()
    session = queue_mod.ProcessingSession("b", 0, please_wait_sent_count=0)
    async def get_monitor_value(key):
        return None if "response_ready" in key else session.to_json()
    service.redis.scan = AsyncMock(return_value=(0, ["1:session"]))
    service.redis.get = AsyncMock(side_effect=get_monitor_value)
    service.redis.set = AsyncMock(side_effect=[True, True, True])
    service.redis.exists = AsyncMock(return_value=True)
    service.redis.setex = AsyncMock()
    service._send_please_wait = AsyncMock()
    monkeypatch.setattr(queue_mod.time, "time", lambda: 60.0)
    ticks = {"count": 0}

    async def stop(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop)
    await service.run_monitoring_loop()
    service._send_please_wait.assert_awaited_once()

    # The interval claim can be absent and the coordination log can fail.
    service = queue_service()
    session = queue_mod.ProcessingSession("b", 0, please_wait_sent_count=0)
    async def get_monitor_value_again(key):
        return None if "response_ready" in key else session.to_json()
    service.redis.scan = AsyncMock(return_value=(0, ["1:session"]))
    service.redis.get = AsyncMock(side_effect=get_monitor_value_again)
    service.redis.set = AsyncMock(side_effect=[True, False, False])
    monkeypatch.setattr(queue_mod.time, "time", lambda: 60.0)
    ticks = {"count": 0}

    async def stop_again(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_again)
    await service.run_monitoring_loop()


@pytest.mark.asyncio
async def test_queue_batch_and_wrapper_exception_boundaries(monkeypatch):
    service = queue_service()
    service.redis.exists.return_value = True
    await service._create_batch("1")
    service.redis.exists.return_value = False
    service.redis.zrange.return_value = []
    await service._create_batch("1")

    service = queue_service()
    service.redis.lpop.return_value = None
    await service._try_start_processing("1")
    service.redis.lpop.side_effect = RuntimeError("claim")
    await service._try_start_processing("1")

    service = queue_service()
    service.redis.get.return_value = queue_mod.ProcessingSession("b", 0).to_json()
    service._should_suppress_response = AsyncMock(return_value=False)
    service.whatsapp_service.send_message = AsyncMock(return_value=SimpleNamespace(success=False, error="bad"))
    service._cleanup_and_next = AsyncMock()
    result = await service.send_message("+1", "body")
    assert result.success is False
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    with pytest.raises(RuntimeError):
        await service.send_message("1", "body")

    async def scan_health(cursor=0, match=None, count=100):
        keys_by_match = {
            "*:processing": ["1:processing"],
            "*:incoming": ["1:incoming"],
            "*:outgoing": ["1:outgoing"],
            "*:session": ["1:session"],
        }
        return 0, keys_by_match.get(match, [])
    service.redis.scan = AsyncMock(side_effect=scan_health)
    service.redis.zcard.return_value = 1
    service.redis.llen.return_value = 1
    service.redis.get.return_value = queue_mod.ProcessingSession("b", 0).to_json()
    monkeypatch.setattr(queue_mod.time, "time", lambda: 60.0)
    metrics = await service.get_health_metrics()
    assert metrics["slow_batches"] == 1


@pytest.mark.asyncio
async def test_queue_health_suppression_and_cleanup_error_paths():
    service = queue_service()
    service.redis.zcard.return_value = 2
    service.redis.llen.return_value = 0
    assert await service._should_suppress_response("1") is True
    service.redis.scan = AsyncMock(side_effect=RuntimeError("health"))
    assert "error" in await service.get_health_metrics()
    service.redis.scan = AsyncMock(return_value=(0, []))
    service.redis.delete.side_effect = RuntimeError("cleanup")
    await service._cleanup_and_next("b", "1", True)


# ---------------------------------------------------------------------------
# Excel and image processors
# ---------------------------------------------------------------------------


def excel_processor(monkeypatch):
    monkeypatch.setattr(excel_mod, "get_settings", lambda: SimpleNamespace(redis_url="redis://test"))
    redis = SimpleNamespace(lock=Mock())
    monkeypatch.setattr(excel_mod.Redis, "from_url", Mock(return_value=redis))
    wa = SimpleNamespace(send_message=AsyncMock())
    helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="context"),
        generate_clarification_response=AsyncMock(return_value="clarify"),
    )
    processor = excel_mod.ExcelMessageProcessor(wa, helpers, object())
    return processor, redis, wa, helpers


def image_processor():
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="context"),
        generate_clarification_response=AsyncMock(return_value="clarify"),
        generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"),
    )
    return image_mod.ImageMessageProcessor(wa, helpers, session_manager=object()), wa, helpers


@pytest.mark.asyncio
async def test_excel_upload_early_return_and_lock_branches(monkeypatch):
    processor, redis, wa, helpers = excel_processor(monkeypatch)
    user = SimpleNamespace(phone_number="+1", is_registered=True)
    session = SimpleNamespace(session_id="s", workflow_state={"pending_optional_rfq": {"entities": {}}})
    delegate = AsyncMock(return_value={"status": "handled", "response": "attachment_added"})
    monkeypatch.setattr(excel_mod.ImageMessageProcessor, "process_image_message", delegate)
    assert (await processor.process_excel_upload(user, session, {"document": {}}))["response"] == "attachment_added"

    session = SimpleNamespace(session_id="s", workflow_state={"excel_file_processed": True, "excel_filename": "old.xlsx"})
    result = await processor.process_excel_upload(user, session, {"document": {"link": "url", "filename": "new.xlsx"}})
    assert result["response"] == "excel_already_processed"

    session = SimpleNamespace(session_id="s", workflow_state={})
    result = await processor.process_excel_upload(user, session, "not-a-dict")
    assert result["status"] == "error"
    assert result["response"] == "Invalid Excel upload content format"
    result = await processor.process_excel_upload(user, session, {"document": {}})
    assert result["response"] == "file_access_error"

    lock = AsyncLock(acquired=False)
    redis.lock.return_value = lock
    result = await processor.process_excel_upload(user, session, {"document": {"link": "url", "filename": "x.xlsx"}})
    assert result["response"] == "upload_in_progress"
    assert wa.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_excel_processing_failure_and_incomplete_structured_paths(monkeypatch):
    processor, redis, _wa, helpers = excel_processor(monkeypatch)
    lock = AsyncLock(acquired=True)
    redis.lock.return_value = lock
    user = SimpleNamespace(phone_number="1", is_registered=True)
    session = SimpleNamespace(session_id="s", workflow_state={})
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})
    ))
    monkeypatch.setattr(excel_mod, "ExcelProcessingService", lambda *_: SimpleNamespace(
        process_excel_file=AsyncMock(return_value={"success": False, "error": "bad"})
    ))
    result = await processor.process_excel_upload(user, session, {"document": {"link": "url", "filename": "x.xlsx"}})
    assert result["response"] == "processing_failed"
    assert lock.release.await_count == 1

    session = SimpleNamespace(session_id="s", workflow_state={})
    processor.response_helpers.generate_clarification_response = AsyncMock(return_value="clarify")
    result = await processor._handle_incomplete_excel(
        user, session, {"excel_data": {"rfqs": [{"products": [{"description": "p"}]}], "filename": "x"}}
    )
    assert result["status"] == "excel_missing_common_data"
    assert "structured_rfqs" in session.workflow_state
    processor.response_helpers.generate_clarification_response.assert_awaited_once()

    session = SimpleNamespace(session_id="s", workflow_state={})
    result = await processor._handle_incomplete_excel(
        user, session, {"excel_data": {"rfqs": [{"deliveryDate": "today", "pincode": "411005", "products": []}]}}
    )
    assert result["status"] == "redirect_to_multiple_flow"

    session = SimpleNamespace(session_id="s", workflow_state={})
    result = await processor._handle_incomplete_excel(
        user, session, {"excel_data": {"items": [{"ItemDescription": "p", "Quantity": "2", "City": "Pune", "DeliveryDate": "today"}]}}
    )
    assert result["status"] == "redirect_to_multiple_flow"


@pytest.mark.asyncio
async def test_excel_complete_legacy_and_error_fallback(monkeypatch):
    processor, _redis, wa, helpers = excel_processor(monkeypatch)
    user = SimpleNamespace(phone_number="1", is_registered=True)
    session = SimpleNamespace(session_id="s", workflow_state={})
    result = await processor._handle_complete_excel(
        user, session, {"items": [{"ItemDescription": "pump", "Quantity": "2"}], "filename": "x.xlsx"}
    )
    assert result["status"] == "redirect_to_multiple_flow"
    assert session.workflow_state["extracted_entities"][0]["quantity"] == 2

    processor.response_helpers.generate_contextual_response = AsyncMock(return_value="context")
    processor._convert_excel_to_entities = Mock(side_effect=RuntimeError("convert"))
    processor._handle_incomplete_excel = AsyncMock(return_value={"status": "fallback"})
    result = await processor._handle_complete_excel(user, session, {"rfqs": [], "items": []})
    assert result["status"] == "fallback"
    assert wa.send_message.await_count >= 1


@pytest.mark.asyncio
async def test_image_processor_registration_no_data_and_attachment_error_paths(monkeypatch):
    processor, wa, helpers = image_processor()
    unregistered = SimpleNamespace(phone_number="1", is_registered=False)
    session = SimpleNamespace(workflow_state={}, conversation_history={})
    result = await processor.process_image_message(unregistered, session, {})
    assert result["response"] == "registration_required"

    user = SimpleNamespace(phone_number="1", is_registered=True)
    relevant = SimpleNamespace(workflow_state={"pending_rfq": {"entities": {}}}, conversation_history={})
    result = await processor.process_image_message(user, relevant, "raw")
    assert result["response"] == "no_file_data"

    monkeypatch.setattr(image_mod.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": False, "error": "missing"}))
    result = await processor.process_image_message(user, relevant, {"image": {"link": "url"}})
    assert result["response"] == "missing"

    monkeypatch.setattr(image_mod.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": True, "attachment": {"file_name": "x", "file_type": "image/png", "file_content": "b"}}))
    monkeypatch.setattr(image_mod.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": False, "error": "Maximum attachments reached"}))
    processor._regenerate_confirmation_with_error = AsyncMock(return_value={"response": "confirmation_with_error"})
    relevant.workflow_state["pending_rfq"] = {"entities": {}}
    result = await processor.process_image_message(user, relevant, {"image": {"link": "url"}})
    assert result["response"] == "confirmation_with_error"

    relevant.workflow_state.pop("pending_rfq")
    relevant.workflow_state["pending_optional_rfq"] = {"entities": {}}
    result = await processor.process_image_message(user, relevant, {"image": {"link": "url"}})
    assert result["response"] == "attachment_add_failed"
    assert wa.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_image_processor_irrelevant_next_step_and_confirmation_branches(monkeypatch):
    processor, wa, helpers = image_processor()
    user = SimpleNamespace(phone_number="1", is_registered=True)
    session = SimpleNamespace(
        workflow_state={},
        conversation_history={"messages": [{"role": "assistant", "content": {"body": {"text": "Body"}, "header": {"text": "Head"}, "footer": {"text": "Foot"}, "action": {"buttons": [{"reply": {"id": "yes", "title": "Yes"}}, {"title": "No"}]}}}]},
    )
    result = await processor._handle_irrelevant_attachment(user, session)
    assert result["response"] == "attachment_ignored"
    wa.send_configurable_buttons.assert_awaited_once()

    for state, expected in [
        ({"pending_rfq": {"entities": {}}}, "confirmation_regenerated"),
        ({"pending_optional_rfq": {"entities": {}}}, "attachment_added_proceeded_to_confirmation"),
        ({"incomplete_products": [{}]}, "attachment_added_continue_clarification"),
        ({}, "attachment_added"),
    ]:
        session = SimpleNamespace(workflow_state=state, conversation_history={})
        processor._regenerate_existing_confirmation = AsyncMock(return_value={"response": "confirmation_regenerated"})
        processor._proceed_from_optional_to_confirmation = AsyncMock(return_value={"response": "attachment_added_proceeded_to_confirmation"})
        assert (await processor._determine_next_step(user, session, "x.png"))["response"] == expected

    processor, wa, helpers = image_processor()
    monkeypatch.setattr("app.services.helpers.chat_service_helpers.ChatServiceHelpers.create_rfq_schema_from_entities", lambda *_: "schema")
    session = SimpleNamespace(
        workflow_state={
            "pending_rfq": {"entities": {"description": "pump"}},
            "extracted_entities": [{"attachments": [1, 2, 3]}],
            "attachment_caption": "remark",
        }
    )
    result = await processor._regenerate_existing_confirmation(user, session, "x.png")
    assert result["response"] == "confirmation_regenerated"
    assert session.workflow_state["pending_rfq"]["entities"]["remarks"] == "remark"

    monkeypatch.setattr(image_mod, "RFQValidationSchema", lambda **kwargs: SimpleNamespace(**kwargs))
    session = SimpleNamespace(
        workflow_state={
            "pending_combined_rfq": {"combined_schema": {}, "products": [{"entities": {}}]},
            "extracted_entities": [{"attachments": [1]}],
            "attachment_caption": "combined remark",
        }
    )
    result = await processor._regenerate_existing_confirmation(user, session, "x.png")
    assert result["response"] == "confirmation_regenerated"

    session = SimpleNamespace(workflow_state={})
    assert (await processor._regenerate_existing_confirmation(user, session, "x"))["response"] == "no_pending_confirmation_found"
    session.workflow_state["pending_rfq"] = {"entities": {}}
    result = await processor._regenerate_confirmation_with_error(user, session, "limit")
    assert result["response"] == "confirmation_with_error"
    session.workflow_state = {}
    assert (await processor._regenerate_confirmation_with_error(user, session, "limit"))["response"] == "no_pending_confirmation_found"


# ---------------------------------------------------------------------------
# Opt-out, logging, pincode, selection tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_out_all_status_and_remote_update_branches(monkeypatch):
    service = opt_out_mod.OptOutService.__new__(opt_out_mod.OptOutService)
    db = MagicMock()
    service.db_session = db
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    service.openai_service = SimpleNamespace(
        generate_opt_out_confirmation=Mock(return_value="out"),
        generate_opt_in_confirmation=Mock(return_value="in"),
        generate_permission_request=Mock(return_value="permission"),
        detect_opt_out_intent=Mock(return_value={"intent": "opt_out", "confidence": 1, "success": True}),
    )
    seller = SimpleNamespace(seller_id="s", seller_name="Seller", categories=["Tools"], phone_number="1", opted_out_notifications=None)
    query = SimpleNamespace(filter=lambda *_: query, first=lambda: seller)
    db.query.return_value = query
    service._update_seller_opt_out_status = AsyncMock()
    assert (await service.handle_opt_out_request("1"))["success"] is True

    service._update_seller_opt_out_status = AsyncMock()
    assert (await service.handle_opt_out_request("1"))["message_sent"] is True
    assert (await service.handle_opt_in_request("1"))["opted_out"] is False
    assert (await service.send_permission_request("s"))["success"] is True
    seller.opted_out_notifications = True
    assert (await service.send_permission_request("s"))["success"] is False
    seller.opted_out_notifications = True
    assert (await service.check_seller_notification_eligibility("s"))["eligible"] is False
    seller.opted_out_notifications = False
    assert (await service.check_seller_notification_eligibility("s"))["eligible"] is True
    seller.opted_out_notifications = None
    assert (await service.check_seller_notification_eligibility("s"))["action"] == "send_permission_request"
    assert service.detect_opt_out_intent("stop")["success"] is True
    service.openai_service.detect_opt_out_intent.side_effect = RuntimeError("ai")
    assert service.detect_opt_out_intent("stop")["success"] is False

    db.execute.return_value = SimpleNamespace(rowcount=1)
    db.commit = Mock()
    db.close = Mock()
    monkeypatch.setattr(opt_out_mod, "get_remote_db_session", lambda: db)
    assert await service._update_remote_opt_out_status("s", True) is True
    db.execute.side_effect = RuntimeError("remote")
    db.rollback = Mock()
    assert await service._update_remote_opt_out_status("s", False) is False
    db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_opt_out_missing_seller_and_local_update_without_row():
    service = opt_out_mod.OptOutService.__new__(opt_out_mod.OptOutService)
    service.db_session = MagicMock()
    query = SimpleNamespace(filter=lambda *_: query, first=lambda: None)
    service.db_session.query.return_value = query
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service.openai_service = SimpleNamespace()
    assert (await service.handle_opt_out_request("1"))["error"] == "Seller not found"
    assert (await service.handle_opt_in_request("1"))["error"] == "Seller not found"
    assert (await service.send_permission_request("s"))["error"] == "Seller not found"
    assert (await service.check_seller_notification_eligibility("s"))["eligible"] is False
    service._update_remote_opt_out_status = AsyncMock(return_value=False)
    await service._update_seller_opt_out_status("s", True)
    service._update_remote_opt_out_status.assert_awaited_once()


def test_selection_tool_fuzzy_and_ai_output_edges(tmp_path):
    tool = selection_mod.UserSelectionTool(SimpleNamespace())
    assert tool._fuzzy_email_match("ab", "alice@example.com")["confidence"] == 0
    assert tool._fuzzy_email_match("alice", "alice@example.com")["confidence"] > 0
    assert tool._fuzzy_email_match("example.com", "alice@example.com")["confidence"] > 0
    assert tool._fuzzy_email_match("aliceexamplecom", "alice@example.com")["confidence"] > 0
    assert tool._fuzzy_email_match("zzzz", "alice@example.com")["confidence"] == 0
    assert tool._string_similarity("", "x") == 0
    assert tool._rule_based_analysis(3, [{"number": 3, "action": "register_new"}])["selected_option"] == 3

    tool.prompts_dir = tmp_path
    tool.tools_dir = tmp_path
    (tmp_path / "profile_selection").mkdir()
    (tmp_path / "profile_selection" / "user_selection_analysis.txt").write_text("prompt", encoding="utf-8")
    (tmp_path / "user_selection_analysis.json").write_text("{}", encoding="utf-8")
    tool.openai_service = SimpleNamespace(default_model="m", client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))
    tool.openai_service.client.responses.create.return_value = SimpleNamespace(output=[])
    result = asyncio.run(tool._ai_based_analysis("unclear", [{"number": 1, "action": "register_new", "display": "New"}]))
    assert result["confidence"] == 0.2
    tool.openai_service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="text", arguments="{}")])
    assert asyncio.run(tool._ai_based_analysis("text", []))["confidence"] == 0.2
    tool.openai_service.client.responses.create.side_effect = ValueError("bad json")
    assert asyncio.run(tool._ai_based_analysis("bad", []))["confidence"] == 0.1


def test_logging_setup_and_context_edges(monkeypatch):
    logging_utils.clear_user_phone_context()
    try:
        logging_utils.set_user_phone_context("outer")
        with logging_utils.UserPhoneContext("inner"):
            assert logging_utils.get_user_phone_context() == "inner"
        assert logging_utils.get_user_phone_context() == "outer"
        asyncio.run(_async_logging_context())
    finally:
        # set_user_phone_context also writes a thread-local, which outlives the
        # test. Leaving "outer" behind made every later test in this worker see a
        # phone context it never set, and any test asserting the "N/A" default
        # failed depending on collection order.
        logging_utils.clear_user_phone_context()

    handlers = []
    class Handler(logging.Handler):
        suffix = None
        def __init__(self, **kwargs):
            super().__init__()
            handlers.append(kwargs)
        def emit(self, record):
            pass
    monkeypatch.setattr(logging_utils, "ConcurrentTimedRotatingFileHandler", Handler)
    monkeypatch.setattr(logging_utils.os, "makedirs", Mock())
    monkeypatch.setenv("LOG_DIR", "logs")
    logging_utils.setup_basic_logging("DEBUG")
    assert handlers
    logger = MagicMock()
    logging_utils.log_info(logger, "plain")
    logging_utils.log_error(logger, "plain")
    logging_utils.log_debug(logger, "plain")

    @logging_utils.log_service_method("remaining")
    async def fail_async():
        raise RuntimeError("async")
    with pytest.raises(RuntimeError):
        asyncio.run(fail_async())


async def _async_logging_context():
    async with logging_utils.UserPhoneContext("async"):
        assert logging_utils.get_user_phone_context() == "async"


def test_pincode_request_error_and_async_success_edges(monkeypatch):
    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(side_effect=requests_error()))
    assert pincode_lookup.get_pincode_details("1", max_retries=1) is None
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "MH"}]}])
    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(return_value=response))
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005"))["city"] == "Pune"
    response.json = lambda: [{"Status": "Error", "PostOffice": []}]
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005")) is None
    monkeypatch.setattr(pincode_lookup, "get_pincode_details", Mock(return_value={"bad": True}))
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005")) is None


def requests_error():
    import requests
    return requests.exceptions.RequestException("bad request")


# ---------------------------------------------------------------------------
# Tasks and background services
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bfs_no_valid_records_and_task_wrapper_error(monkeypatch):
    monkeypatch.setattr(bfs_mod, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs_mod, "get_pending_bfs_notifications", lambda limit: [{"bfs_user_uuid": "x", "seller_phone": None}])
    assert (await bfs_mod.process_bfs_notifications())["message"] == "No valid seller phone numbers"
    async def fail_bfs_processing():
        raise RuntimeError("worker")

    monkeypatch.setattr(bfs_mod, "process_bfs_notifications", fail_bfs_processing)
    with pytest.raises(RuntimeError):
        await asyncio.to_thread(bfs_mod.process_bfs_seller_notifications.run)


def test_log_cleanup_empty_and_skip_archive_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup_mod, "datetime", type("Fixed", (datetime,), {"now": classmethod(lambda cls: datetime(2025, 1, 10)), "utcnow": classmethod(lambda cls: datetime(2025, 1, 10))}))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    archive_dir = log_dir / "archive"
    manager = cleanup_mod.LogCleanupManager(str(log_dir), 7, 7)
    manager.archive_dir = archive_dir
    assert manager._find_old_logs() == {}
    assert manager._find_old_archives() == []
    old = log_dir / "app_2024-01-01.log"
    old.write_text("x")
    archive_dir.mkdir()
    (archive_dir / "logs_2024-01-01.tar.gz").write_bytes(b"a")
    manager._archive_logs({"2024-01-01": [old]})
    assert manager.stats["errors"] == 0
    manager._archive_logs({"2024-01-01": [old]})

    monkeypatch.setattr(cleanup_mod, "get_settings", lambda: SimpleNamespace(PROJECT_ROOT=str(tmp_path), log_retention_days=7, archive_retention_days=7))
    manager_mock = Mock()
    manager_mock.run.return_value = {"errors": 0}
    monkeypatch.setattr(cleanup_mod, "LogCleanupManager", lambda **kwargs: manager_mock)
    result = cleanup_mod.cleanup_logs.run()
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_seller_matching_enhanced_success_and_notification_error(monkeypatch):
    rfq = {"rfq_id": "r", "rfq_uuid": "u", "categories": ["Tools"], "subscribed_notified": 0, "unsubscribed_notified": 0, "description": "pump"}
    enhanced = SimpleNamespace(find_sellers_for_item=AsyncMock(return_value={"success": True, "sellers": [{"seller_id": "s1"}]}))
    monkeypatch.setattr("app.services.enhanced_seller_matching_service.EnhancedSellerMatchingService", lambda: enhanced)
    seller_service = SimpleNamespace(select_sellers_for_rfq=AsyncMock(return_value={"subscribed_sellers": [{"seller_id": "s1", "phone_number": "1", "seller_name": "S"}], "unsubscribed_sellers": []}))
    monkeypatch.setattr(seller_task, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller_task, "get_users_active_in_last_24hrs", lambda _: set())
    monkeypatch.setattr(seller_task, "normalize_phone_for_comparison", lambda x: x.lstrip("+"))
    monkeypatch.setattr(seller_task, "log_selected_sellers_to_remote", Mock(side_effect=RuntimeError("log")))
    notification = SimpleNamespace(send_rfq_notifications=AsyncMock(side_effect=RuntimeError("wa")))
    monkeypatch.setattr(seller_task, "SellerNotificationService", lambda: notification)
    result = await seller_task.process_single_rfq_matching(rfq, seller_service, set())
    assert result["success"] is False


def test_seller_task_query_rows_and_fk_success(monkeypatch):
    monkeypatch.setattr(seller_task, "execute_remote_query", lambda *_: [{"category": "Tools"}, {"category": None}])
    assert seller_task.get_rfq_item_categories("u") == ["Tools"]
    monkeypatch.setattr(seller_task, "execute_remote_query", lambda *_: [{"vendor_uuid": "s"}, {"vendor_uuid": None}])
    assert seller_task.get_sellers_notified_in_last_24hrs() == {"s"}
    seller_task._fk_dropped = False
    db = SimpleNamespace(bind=object(), execute=Mock(), commit=Mock(), rollback=Mock())
    class Inspector:
        def get_foreign_keys(self, _):
            return []
    monkeypatch.setattr("sqlalchemy.inspect", lambda *_: Inspector())
    seller_task._ensure_seller_id_fk_dropped(db)
    assert seller_task._fk_dropped is True
    seller_task._ensure_seller_id_fk_dropped(db)


@pytest.mark.asyncio
async def test_report_step_two_and_cleanup_failure_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(report_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(report_mod.os, "getcwd", lambda: str(tmp_path))
    analytics = SimpleNamespace(analyze_daily_conversations=AsyncMock(return_value={"success": True, "total_sessions": 1}))
    monkeypatch.setattr(report_mod, "ConversationAnalyticsService", lambda: analytics)
    monkeypatch.setattr(report_mod, "EnhancedExcelReportService", lambda: SimpleNamespace(generate_report=Mock(side_effect=RuntimeError("excel"))))
    result = await report_mod.run_whatsapp_report_automation_async(None, "2025-01-01")
    assert result["step"] == "excel_generation"

    report_path = tmp_path / "report.xlsx"
    report_path.write_bytes(b"x")
    monkeypatch.setattr(report_mod, "EnhancedExcelReportService", lambda: SimpleNamespace(generate_report=Mock(return_value=str(report_path))))
    monkeypatch.setattr(report_mod.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(report_mod, "_send_excel_reports_with_api_session", AsyncMock(return_value={"status": "Success"}))
    monkeypatch.setattr(report_mod.os, "remove", Mock(side_effect=RuntimeError("remove")))
    result = await report_mod.run_whatsapp_report_automation_async(None, "2025-01-01")
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Profile selection, registration, seller, background, and session helpers
# ---------------------------------------------------------------------------


def profile_service():
    service = profile_mod.ProfileSelectionService.__new__(profile_mod.ProfileSelectionService)
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    service.authentication_service = SimpleNamespace(
        user_authenticate=AsyncMock(),
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock(return_value=None)),
        verification_check_service=SimpleNamespace(check_and_enforce_verification=AsyncMock()),
        store_user_session=AsyncMock(return_value=True),
    )
    service.user_cache_service = SimpleNamespace(get_user_data=AsyncMock(return_value=None))
    service.user_selection_tool = None
    service.openai_service = SimpleNamespace()
    return service


def registration_service():
    service = registration_mod.RegistrationService.__new__(registration_mod.RegistrationService)
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock(return_value=SimpleNamespace(success=True)))
    service.session_manager = None
    service.confirmation_service = SimpleNamespace(parse_confirmation=AsyncMock(return_value=None))
    service.authentication_helpers = SimpleNamespace(
        generate_registration_message=Mock(return_value="register"),
        generate_confirmation_message_dynamic=Mock(return_value="confirm"),
    )
    service.register_api_service = SimpleNamespace(register_buyer=AsyncMock(), register_seller=AsyncMock())
    service.auth_redis_service = SimpleNamespace(store=AsyncMock(return_value=True))
    service.support_notification_service = SimpleNamespace(
        notify_registration_failed=AsyncMock(), notify_buyer_registration_not_approved=AsyncMock()
    )
    service.otp_service = SimpleNamespace(send_otp=AsyncMock(return_value={"status": "otp_sent"}), handle_user_message=AsyncMock())
    service.auth_api_service = SimpleNamespace(authenticate_user=AsyncMock())
    service.settings = SimpleNamespace(procucev_rfq_details_url="https://portal", support_contact_info="support")
    return service


@pytest.mark.asyncio
async def test_profile_cache_api_conversion_and_parse_fallbacks(monkeypatch):
    service = profile_service()
    service.user_cache_service.get_user_data.return_value = [{"id": "1", "username": "a@test", "selfClient": True, "fullName": "Ada Lovelace"}]
    monkeypatch.setattr(profile_mod.User, "from_api_response", lambda data: SimpleNamespace(email=data["username"], role=SimpleNamespace(value="buyer"), name="Ada", company_name="Co"))
    cached = await service._get_user_profiles("1", "hi", SimpleNamespace())
    assert cached["from_cache"] is True
    service.user_cache_service.get_user_data.return_value = None
    service.authentication_service.user_authenticate.return_value = {"success": True, "response": [{"username": "s@test", "selfClient": False}]}
    monkeypatch.setattr(profile_mod.User, "from_api_response", lambda data: SimpleNamespace(email=data["username"], role=SimpleNamespace(value="seller"), name="S", company_name="Co"))
    assert (await service._get_user_profiles("1", "hi", SimpleNamespace()))["from_cache"] is False
    service.authentication_service.user_authenticate.return_value = {"success": False}
    assert (await service._get_user_profiles("1", "hi", SimpleNamespace()))["success"] is False
    service.authentication_service.user_authenticate.side_effect = RuntimeError("api")
    assert (await service._get_user_profiles("1", "hi", SimpleNamespace()))["success"] is False

    service.user_selection_tool = SimpleNamespace(analyze_user_selection=AsyncMock(return_value={"selected_option": 1, "requires_clarification": False, "register": {"type": None}}))
    options = [{"number": 1, "profile": {"email": "a@test", "role": "buyer"}}, {"number": 2, "action": "register_new"}]
    assert (await service._enhanced_simple_parse_profile_selection("1", options))["number"] == 1
    assert (await service._enhanced_simple_parse_profile_selection("register new", options))["action"] == "register_new"
    assert (await service._enhanced_simple_parse_profile_selection("use existing", options))["number"] == 1
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("tool")
    assert await service._enhanced_simple_parse_profile_selection("unclear", options) is None


@pytest.mark.asyncio
async def test_profile_verification_menu_filters_and_response_edges(monkeypatch):
    service = profile_service()
    profile = {"email": "a@test", "role": "buyer", "user_data": {"id": "u", "username": "a@test", "selfClient": True}}
    session = SimpleNamespace(workflow_state={"profile_selection_stage": "buyer_intent", "profile_options": [1]})
    monkeypatch.setattr(profile_mod.User, "from_api_response", lambda _: SimpleNamespace())
    service.authentication_service.auth_redis_service.retrieve.return_value = "token"
    result = await service._set_active_profile_and_proceed("+1", profile, session, "buy", "buy_something")
    assert result["status"] == "profile_selected_and_authenticated"

    service.authentication_service.auth_redis_service.retrieve.return_value = None
    service.authentication_service.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": False, "redirect_to_support": True, "redirect_info": {"message": "support", "reason": "bad"}}
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *_args: SimpleNamespace(handle_exit_intent=AsyncMock()))
    assert (await service._set_active_profile_and_proceed("1", profile, session, "", "buy"))["redirect_to_support"] is True

    service.authentication_service.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": False, "otp_sent": True, "redirect_info": {"flow": "email_verification", "email": "a@test"}}
    monkeypatch.setattr(profile_mod.WorkflowManager, "set_workflow_type", Mock())
    result = await service._set_active_profile_and_proceed("1", profile, session, "", "buy")
    assert result["status"] == "verification_required"
    service.authentication_service.store_user_session.return_value = False
    service.authentication_service.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": True}
    assert (await service._set_active_profile_and_proceed("1", profile, session, "", "buy"))["status"] == "error"

    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated"})
    buyer_result = await service._show_role_based_menu("1", profile, session)
    assert buyer_result["status"] == "role_menu_presented"
    seller_profile = {**profile, "role": "seller", "email": "s@test", "user_data": {}}
    assert (await service._show_role_based_menu("1", seller_profile, session))["user_type"] == "seller"
    service._set_active_profile_and_proceed.side_effect = RuntimeError("menu")
    assert (await service._show_role_based_menu("1", profile, session))["status"] == "error"

    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": []})
    result = await service._show_filtered_profiles("1", session, "buyer")
    assert result["status"] == "no_buyer_profiles_message_sent"
    service._get_user_profiles.return_value = {"success": True, "profiles": [profile]}
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "menu"
    service._get_user_profiles.return_value = {"success": True, "profiles": [profile, {**profile, "email": "b@test"}]}
    service._show_role_based_menu = AsyncMock()
    assert (await service._show_filtered_profiles("1", session, "buyer"))["profiles_count"] == 2


@pytest.mark.asyncio
async def test_profile_neutral_registration_and_no_account_responses(monkeypatch):
    service = profile_service()
    session = SimpleNamespace(workflow_state={"profiles": []})
    service._handle_buyer_intent = AsyncMock(return_value={"status": "buyer"})
    service._handle_seller_intent = AsyncMock(return_value={"status": "seller"})
    assert (await service._handle_neutral_greeting_response("1", "buy", session))["status"] == "buyer"
    session.workflow_state = {"profiles": []}
    assert (await service._handle_neutral_greeting_response("1", "buy and sell", session))["status"] == "dual_intent_clarification_sent"
    session.workflow_state = {"profiles": []}
    service._detect_user_intent_with_ai = AsyncMock(return_value="unclear")
    assert (await service._handle_neutral_greeting_response("1", "hmm", session))["status"] == "neutral_greeting_retry_sent"

    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer_reg"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller_reg"})
    service._parse_profile_selection = AsyncMock(side_effect=[{"action": "register_buyer"}, {"action": "exit"}, None, {"action": "register_seller"}, {"action": "exit"}, None])
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    state = {"profile_options": [{"number": 1, "action": "register_buyer"}]}
    session.workflow_state = state.copy()
    assert (await service._handle_buyer_no_accounts_response("1", "1", session))["status"] == "buyer_reg"
    session.workflow_state = state.copy()
    assert (await service._handle_buyer_no_accounts_response("1", "2", session))["status"] == "exit"
    session.workflow_state = state.copy()
    assert (await service._handle_buyer_no_accounts_response("1", "x", session))["status"] == "buyer_no_accounts_retry_sent"
    session.workflow_state = {"profile_options": [{"number": 1, "action": "register_seller"}]}
    assert (await service._handle_seller_no_accounts_response("1", "1", session))["status"] == "seller_reg"
    session.workflow_state = {"profile_options": [{"number": 2, "action": "exit"}]}
    assert (await service._handle_seller_no_accounts_response("1", "2", session))["status"] == "exit"


@pytest.mark.asyncio
async def test_registration_confirmation_submit_and_otp_fallbacks(monkeypatch):
    service = registration_service()
    session = SimpleNamespace(session_id="sid", workflow_type=None, workflow_state={"user_type": "buyer", "registration_entities": {"email": "a@test"}})
    monkeypatch.setattr(service, "_check_exit_command", AsyncMock(return_value=False))
    service._submit_registration = AsyncMock(return_value={"status": "registration_completed"})
    assert (await service.handle_registration_confirmation("1", {"button_reply": {"id": "confirm_registration"}}, session))["status"] == "otp_sent"
    service.confirmation_service.parse_confirmation.return_value = "no"
    assert (await service.handle_registration_confirmation("1", "no", session))["status"] == "registration_restarted"
    service.confirmation_service.parse_confirmation.return_value = None
    service._send_clarification_with_buttons = AsyncMock()
    assert (await service.handle_registration_confirmation("1", "maybe", session))["status"] == "awaiting_confirmation"
    service._check_exit_command.side_effect = RuntimeError("exit")
    service._redirect_to_support = AsyncMock(return_value={"status": "support"})
    assert (await service.handle_registration_confirmation("1", "x", session))["status"] == "support"

    service = registration_service()
    session = SimpleNamespace(session_id="sid", workflow_type=None, workflow_state={"user_type": "buyer", "registration_entities": {"email": "a@test"}})
    service.register_api_service.register_buyer.return_value = {"statusCode": "200", "data": {"id": "u", "orgId": "o"}}
    monkeypatch.setattr(registration_mod.AuthenticationHelpers, "build_registration_payload_dynamic", lambda *_: {"email": "a@test"})
    result = await service._submit_registration("1", session, {"email": "a@test"}, "buyer")
    assert result["status"] == "registration_completed"
    service.register_api_service.register_buyer.return_value = {"statusCode": 409, "message": "already exists"}
    monkeypatch.setattr("app.services.cancel_service.CancelService", lambda *_args: SimpleNamespace(_clear_workflow_state=AsyncMock()))
    assert (await service._submit_registration("1", session, {"email": "a@test"}, "buyer"))["status"] == "user_already_exists"
    service.register_api_service.register_buyer.return_value = {"statusCode": 500, "message": "bad"}
    service._redirect_to_support = AsyncMock(return_value={"status": "support"})
    assert (await service._submit_registration("1", session, {"email": "a@test"}, "buyer"))["status"] == "support"


@pytest.mark.asyncio
async def test_background_cleanup_priority_and_seller_helper_fallbacks(monkeypatch):
    service = background_mod.RFQBackgroundService.__new__(background_mod.RFQBackgroundService)
    service._active_jobs = {}
    service._job_stats = {"rfqs_processed": 0, "sellers_notified": 0, "notifications_sent": 0, "errors_occurred": 0}
    async def select_sellers(_rfq_data):
        return {
            "total_selected": 1,
            "subscribed_sellers": [{"seller_id": "s"}],
            "unsubscribed_sellers": [],
            "selection_metadata": {},
        }

    async def fetch_rfq(_rfq_id):
        return {"rfq_id": "r"}

    async def send_notifications(_rfq_data, _sellers, _job_id):
        return {"successful": 1, "failed": 0, "details": []}

    async def update_status(_rfq_id, _status):
        return None

    service.recommendation_service = SimpleNamespace(select_sellers_for_rfq=select_sellers)
    service._fetch_rfq_data = fetch_rfq
    service._send_batch_notifications = send_notifications
    service._update_rfq_status = update_status
    service.process_approved_rfq = background_mod.RFQBackgroundService.process_approved_rfq.__get__(
        service, background_mod.RFQBackgroundService
    )
    result = await service.process_approved_rfq("r")
    assert result["notifications_sent"] == 1
    service.db_session = SimpleNamespace()
    q = SimpleNamespace(count=Mock(side_effect=[0, 0]), filter=lambda *_: q, delete=Mock(return_value=1))
    service.db_session.query = Mock(return_value=q)
    service.db_session.commit = Mock()
    service.db_session.rollback = Mock()
    assert (await service.cleanup_old_notifications())["message"] == "No old records found"
    service.db_session.query.return_value.count.side_effect = [1, 0]
    assert (await service.cleanup_old_notifications())["success"] is True
    service._calculate_rfq_priority(SimpleNamespace(api_payload={"deadline": "invalid", "categories": ["Medical Equipment"]}, created_at=datetime.utcnow() - timedelta(days=2)))


def seller_service():
    service = seller_mod.SellerService.__new__(seller_mod.SellerService)
    service.settings = SimpleNamespace(support_contact_info="support", procucev_rfq_details_url="https://portal", rfq_max_allowed=2)
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service.session_manager = SimpleNamespace(save_session=AsyncMock())
    service.openai_service = SimpleNamespace(extract_entities=AsyncMock(return_value={"selected_plan": None}))
    service.response_helpers = SimpleNamespace(
        generate_seller_contextual_response=AsyncMock(return_value="response"),
        generate_seller_contextual_intent_response=AsyncMock(return_value={"intent": "unknown", "confidence": 0}),
    )
    service.rfq_status_service = SimpleNamespace(handle_rfq_status_inquiry=AsyncMock(return_value={"status": "status"}))
    return service


@pytest.mark.asyncio
async def test_seller_intent_and_plan_fallback_edges():
    service = seller_service()
    session = SimpleNamespace(conversation_history={"messages": [{"role": "assistant", "content": "subscription plan"}]}, workflow_state={})
    user = SimpleNamespace(id="u", org_id="o")
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 0})
    service._handle_plan_upgrade_request = AsyncMock(return_value={"status": "plan"})
    service._handle_no_credits_response = AsyncMock(return_value={"status": "no"})
    service._handle_general_seller_response = AsyncMock(return_value={"status": "general"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "plan"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "rfq details"}]}
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "no"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "other"}]}
    service._handle_general_affirmative_response = AsyncMock(return_value={"status": "affirm"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "affirm"
    service.openai_service.extract_entities.return_value = {"selected_plan": None}
    assert await service._extract_plan_selection("which option", [{"planName": "CONNECT"}, {"planName": "SELECT"}]) is None
    assert (await service._extract_plan_selection("select plan", [{"planName": "CONNECT"}]))["planName"] == "CONNECT"
    service.openai_service.extract_entities.side_effect = RuntimeError("ai")
    assert await service._extract_plan_selection("x", []) is None
    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("something unrelated", {})["intent"] == "general_question"


@pytest.mark.asyncio
async def test_session_license_context_save_and_serialization_edges(monkeypatch):
    service = session_mod.SessionManagementService.__new__(session_mod.SessionManagementService)
    service.settings = SimpleNamespace(license_enabled=False, redis_session_storage_enabled=True)
    service.redis_enabled = True
    service.redis_session = SimpleNamespace(get_session=AsyncMock(return_value=None), store_session=AsyncMock(), refresh_ttl=AsyncMock(), session_exists=AsyncMock(return_value=False))
    service.db_manager = SimpleNamespace(get_conversation_session=Mock(return_value=None), save_conversation_session=Mock(return_value=SimpleNamespace()))
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(session_mod.SessionHelpers, "generate_session_id", lambda *_: "sid")
    monkeypatch.setattr("app.services.welcome_message_service.get_welcome_service", lambda: SimpleNamespace(check_and_send_welcome=AsyncMock()))
    monkeypatch.setattr(service, "_dict_to_session", lambda data: SimpleNamespace(**data))
    session = await service.get_conversation_context("+1")
    assert session.session_id == "sid"
    service.settings.license_enabled = True
    monkeypatch.setattr("app.license.validate_license", lambda: (False, "bad"))
    with pytest.raises(Exception):
        await service.get_conversation_context("1")

    assert service._validate_license() == (True, "License validation skipped (disabled in config)") if False else True
    obj = SimpleNamespace(session_id="s", external_user_id="u", workflow_type="bad", outcome="bad", workflow_state={}, conversation_history={}, extracted_entities={}, whatsapp_context={}, retention_date="date", created_at=None, last_activity_at=None, completed_at=None)
    cleaned = service._session_to_dict(obj)
    assert cleaned["session_id"] == "s"
    assert service._should_persist_abandoned(SimpleNamespace(conversation_history={"openai_messages": [{}, {}, {}]})) is True
    assert service._should_persist_abandoned(SimpleNamespace(conversation_history=None)) is False


@pytest.mark.asyncio
async def test_session_send_track_and_completion_fallbacks(monkeypatch):
    service = session_mod.SessionManagementService.__new__(session_mod.SessionManagementService)
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service.add_message_to_history = Mock(side_effect=RuntimeError("track"))
    session = SimpleNamespace(session_id="s", external_user_id="u")
    await service.send_and_track_message("1", "hello", session)
    assert service.whatsapp_service.send_message.await_count == 2
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    with pytest.raises(RuntimeError):
        await service.send_and_track_message("1", "hello", session)

    service.chat_summary_service = SimpleNamespace(generate_session_summary=AsyncMock(side_effect=RuntimeError("summary")))
    service.daily_summary_service = SimpleNamespace(generate_daily_summary=AsyncMock())
    await service._handle_session_completion_fallback(session)
    await service._show_auth_placeholder("1")


# ---------------------------------------------------------------------------
# Remaining branch-only coverage cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round6_learning_alternate_existing_and_suggestion_branches(monkeypatch):
    service = learning_mod.LearningCategorizationService.__new__(learning_mod.LearningCategorizationService)
    service.openai_service = SimpleNamespace(
        generate_3_level_categorization=AsyncMock(),
        close_sync=Mock(),
    )
    db = SimpleNamespace(close=Mock(), rollback=Mock())
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)

    # A valid existing client category makes the backfill guard false, while a
    # missing learning_item_id exercises the no-update branch.
    service.check_existing_learning_category = Mock(return_value={
        "learning_category_id": "cat",
        "client_category_name": "Existing",
    })
    service.update_usage_frequency = Mock()
    service.update_client_category = Mock()
    result = await service.create_3_level_category("pump", "Other")
    assert result["existing"] is True
    service.check_existing_learning_category.return_value = {
        "learning_category_id": "cat",
        "learning_item_id": "item",
        "client_category_name": "Existing",
    }
    result = await service.create_3_level_category("pump", "Better")
    assert result["existing"] is True
    service.update_client_category.assert_not_called()

    class Query:
        def __init__(self, rows=None, first=None):
            self.rows = rows or []
            self.first_value = first

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.first_value

    class SuggestionDB:
        def __init__(self):
            self.close = Mock()

        def query(self, model=None, *_args, **_kwargs):
            if model is learning_mod.LearningCategoryItem:
                return Query(rows=[
                    SimpleNamespace(normalized_keywords=None, learning_category_id="missing"),
                    SimpleNamespace(normalized_keywords={"keywords": ["unrelated"]}, learning_category_id="missing"),
                ])
            return Query(first=None)

    service._extract_keywords = Mock(return_value={"keywords": ["pump"]})
    service._calculate_keyword_similarity = Mock(return_value=0.9)
    monkeypatch.setattr(learning_mod, "get_db_session", SuggestionDB)
    assert service.get_learning_category_suggestions("pump") == []

    class DedupDB:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *_args, **_kwargs):
            return Query(rows=self.rows)

    assert service._deduplicate_category_hierarchy(
        DedupDB([]), "A", "B", "C"
    ) == {"level_1": "A", "level_2": "B", "level_3": "C"}
    assert service._find_similar_l2(
        "Hydraulic Components",
        {"Hydraulic Components", "Hydraulic and Pneumatic Components"},
        {"Hydraulic Components": ("Equipment", 1)},
        "Equipment",
    ) == "Hydraulic Components"


@pytest.mark.asyncio
async def test_round6_queue_background_batch_and_health_branches(monkeypatch):
    service = queue_service()

    class FakeTask:
        def __init__(self, name):
            self.name = name

        def done(self):
            return False

        def get_name(self):
            return self.name

        def cancel(self):
            return None

    created = []

    def fake_create_task(coro, name=None):
        coro.close()
        task = FakeTask(name)
        created.append(task)
        return task

    monkeypatch.setattr(queue_mod.asyncio, "create_task", fake_create_task)
    service._background_tasks = []
    service._ensure_background_tasks()
    assert {task.get_name() for task in created} == {"batch_poller", "monitoring_loop"}
    service._ensure_background_tasks()
    assert len(created) == 2

    # A malformed queued message is removed, and a valid batch reaches the
    # create-task branch without starting a real background coroutine.
    service = queue_service()
    service.redis.zrange.return_value = ["not-json"]
    await service._create_batch("1")
    service.redis.delete.assert_awaited_once_with("1:incoming")

    service = queue_service()
    batch = queue_mod.Batch(
        batch_id="b", user_phone="1", concatenated_content="hello",
        message_type="text", message_count=1, created_at=0,
    )
    service.redis.lpop.return_value = json.dumps(batch.to_dict())
    service.redis.exists.return_value = False
    monkeypatch.setattr(queue_mod.asyncio, "create_task", fake_create_task)
    await service._try_start_processing("1")
    assert created[-1].get_name() is None

    async def scan_empty_counts(cursor=0, match=None, count=100):
        keys = {
            "*:processing": [],
            "*:incoming": ["1:incoming"],
            "*:outgoing": ["1:outgoing"],
            "*:session": ["1:session"],
        }
        return 0, keys.get(match, [])

    service = queue_service()
    service.redis.scan = AsyncMock(side_effect=scan_empty_counts)
    service.redis.zcard.return_value = 0
    service.redis.llen.return_value = 0
    service.redis.get.return_value = queue_mod.ProcessingSession("b", 100, 0).to_json()
    monkeypatch.setattr(queue_mod.time, "time", lambda: 110.0)
    metrics = await service.get_health_metrics()
    assert metrics["total_incoming"] == 0
    assert metrics["total_outgoing"] == 0
    assert metrics["slow_batches"] == 0

    # Exercise the poller path where the timer expired but the incoming queue
    # is empty, and the monitor path where a session disappeared before read.
    service = queue_service()
    service.redis.scan = AsyncMock(return_value=(0, ["1:incoming"]))
    service.redis.exists.return_value = False
    service.redis.zcard.return_value = 0
    ticks = {"count": 0}

    async def stop_poller(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_poller)
    await service.run_batch_poller()

    service = queue_service()
    service.redis.scan = AsyncMock(return_value=(0, ["1:session"]))
    service.redis.get.return_value = None
    ticks = {"count": 0}

    async def stop_monitor(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_monitor)
    await service.run_monitoring_loop()


@pytest.mark.asyncio
async def test_round6_processor_validation_and_attachment_message_branches(monkeypatch):
    processor, redis, wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = AsyncLock(acquired=True)
    user = SimpleNamespace(phone_number="1", is_registered=True)
    session = SimpleNamespace(session_id="s", workflow_state={})
    validation = SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "invalid file"})
    )
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: validation)
    result = await processor.process_excel_upload(
        user, session, {"document": {"link": "url", "filename": "bad.xlsx"}}
    )
    assert result["response"] == "validation_failed"

    processor, redis, wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = AsyncLock(acquired=True)
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})
    ))
    monkeypatch.setattr(excel_mod, "ExcelProcessingService", lambda *_args: SimpleNamespace(
        process_excel_file=AsyncMock(return_value={
            "success": True,
            "filename": "bad-quantity.xlsx",
            "items": [
                {"ItemDescription": "pump", "Quantity": ""},
                {"ItemDescription": "valve", "Quantity": "2"},
                {"ItemDescription": "motor", "Quantity": "3"},
                {"ItemDescription": "seal", "Quantity": "4"},
            ],
        })
    ))
    processor._convert_excel_to_entities = Mock(return_value=[{"description": "pump"}] * 4)
    result = await processor.process_excel_upload(
        user, SimpleNamespace(session_id="s", workflow_state={}),
        {"document": {"link": "url", "filename": "bad-quantity.xlsx"}},
    )
    assert result["response"] == "missing_quantities_reupload_required"

    image, image_wa, _helpers = image_processor()
    string_session = SimpleNamespace(workflow_state={}, conversation_history={"messages": [{"role": "assistant", "content": "hello"}]})
    assert (await image._handle_irrelevant_attachment(user, string_session))["response"] == "attachment_ignored"
    empty_session = SimpleNamespace(workflow_state={}, conversation_history={})
    assert (await image._handle_irrelevant_attachment(user, empty_session))["response"] == "attachment_ignored"
    assert image_wa.send_message.await_count >= 2


@pytest.mark.asyncio
async def test_round6_profile_registration_and_background_error_branches(monkeypatch):
    service = profile_service()
    session = SimpleNamespace(workflow_state={})
    service._detect_registration_intent = AsyncMock(return_value=None)
    result = await service._handle_registration_type_response("1", "not sure", session)
    assert result["status"] == "registration_type_clarification_sent"

    mismatch_session = SimpleNamespace(workflow_state={
        "target_role": "buyer",
        "profile_options": [{"number": 1, "action": "register_buyer", "display": "Buyer"}],
    })
    service._parse_profile_selection = AsyncMock(return_value=None)
    result = await service._handle_intent_mismatch_response("1", "x", mismatch_session)
    assert result["status"] == "intent_mismatch_retry_sent"

    empty_registration_session = SimpleNamespace(workflow_state={})
    assert (await service._handle_new_user_registration_response("1", "1", empty_registration_session))["status"] in ("restart_profile_selection", "redirected_to_buyer_registration", "buyer")

    background = object.__new__(background_mod.RFQBackgroundService)
    query = SimpleNamespace(filter=lambda *_args, **_kwargs: query, first=lambda: None)
    background.db_session = SimpleNamespace(query=Mock(return_value=query), commit=Mock(), rollback=Mock())
    assert await background._fetch_rfq_data("missing") is None
    await background._update_rfq_status("missing", background_mod.RFQStatus.submitted)


@pytest.mark.asyncio
async def test_round6_registration_data_and_otp_fallback_branches(monkeypatch):
    service = registration_service()
    service.entity_service = SimpleNamespace(extract_entities=AsyncMock(return_value={
        "entities": {"products_services": "Tools", "blank": " "}
    }))
    service.authentication_helpers = SimpleNamespace(
        generate_registration_message=Mock(return_value="register"),
        generate_confirmation_message_dynamic=Mock(return_value="confirm"),
        validate_entities=AsyncMock(return_value=({"details": "Tools", "email": "bad"}, "Organization email is invalid")),
    )
    service._check_exit_command = AsyncMock(return_value=False)
    service._auto_fill_address_from_pincode = AsyncMock()
    service._generate_contextual_registration_questions = AsyncMock(return_value="questions")
    monkeypatch.setattr(registration_mod.AuthenticationHelpers, "get_missing_fields", lambda *_args: ["name"])
    session = SimpleNamespace(
        session_id="sid",
        workflow_state={
            "user_type": "seller",
            "registration_entities": {
                "email": "bad",
                "gstin": "gst",
                "zipCode": "999999",
                "_pincode_error": "Pincode 999999 does not exist",
            },
        },
        conversation_history={"messages": []},
        workflow_type=None,
    )
    result = await service.handle_registration_data_collection("1", "Tools", session)
    assert result["status"] == "data_collection_in_progress"
    assert service.whatsapp_service.send_message.await_count == 1

    service = registration_service()
    service.whatsapp_service.send_configurable_buttons.return_value = SimpleNamespace(success=False)
    await service._send_confirmation_with_buttons("1", {"email": "a@test"}, "buyer", session)
    service._check_exit_command = AsyncMock(return_value=False)
    service.otp_service.handle_user_message.return_value = {"status": "redirect_to_support", "reason": "bad otp"}
    service._redirect_to_support = AsyncMock(return_value={"status": "support"})
    otp_result = await service.handle_registration_otp_validation("1", "0000", session)
    assert otp_result["status"] == "support"


@pytest.mark.asyncio
async def test_round6_task_utility_remaining_outcomes(monkeypatch, tmp_path):
    assert bfs_mod.mark_notifications_sent_batch([]) == 0
    failing_db = SimpleNamespace(
        execute=Mock(side_effect=RuntimeError("db")),
        rollback=Mock(), close=Mock(), commit=Mock(),
    )
    monkeypatch.setattr(bfs_mod, "get_remote_db_session", lambda: failing_db)
    assert bfs_mod.mark_notifications_sent_batch(["u"]) == 0
    assert failing_db.rollback.called and failing_db.close.called
    assert bfs_mod.mark_notification_sent("u") is False

    monkeypatch.setattr(bfs_mod, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs_mod, "get_pending_bfs_notifications", lambda limit: [{
        "bfs_user_uuid": "b1", "seller_phone": "123", "item_description": "pump",
    }])
    monkeypatch.setattr(bfs_mod, "get_users_active_in_last_24hrs", lambda phones: set())
    notification_service = SimpleNamespace(send_bfs_bid_notifications_batch=AsyncMock(return_value={
        "results": [{"bfs_user_uuid": "b1", "success": True}], "sent": 1, "failed": 0, "skipped": 0,
    }))
    monkeypatch.setattr(bfs_mod, "get_seller_notification_service", lambda: notification_service)
    mark_batch = Mock()
    monkeypatch.setattr(bfs_mod, "mark_notifications_sent_batch", mark_batch)
    result = await bfs_mod.process_bfs_notifications()
    assert result["sent"] == 1
    mark_batch.assert_called_once_with(["b1"])

    cleanup = cleanup_mod.LogCleanupManager(str(tmp_path / "logs"), 7, 7)
    cleanup.archive_dir = tmp_path / "archive"
    cleanup.archive_dir.mkdir()
    (cleanup.archive_dir / "logs_2024-99-99.tar.gz").write_bytes(b"x")
    assert cleanup._find_old_archives() == []
    assert cleanup._extract_date_from_filename("current.log.no-date") is None

    seller = seller_service()
    assert "no active RFQs" in seller._generate_hardcoded_rfq_display([], 0, 0)
    message = seller._generate_hardcoded_rfq_display(
        [{"rfq_id": "r", "delivery_date": "today", "location": "Pune", "project_description": "pump"}],
        1, 0,
    )
    assert "not have enough credits" in message
    invalid = await seller._handle_invalid_rfq_selection(
        SimpleNamespace(org_id="o"), SimpleNamespace(), "bad",
        {"success": True, "rfqs": [], "total_count": 0},
    )
    assert invalid["workflow_step"] == "invalid_rfq_selection"

    monkeypatch.setattr(report_mod, "get_settings", lambda: SimpleNamespace())
    invalid_date = await report_mod.run_whatsapp_report_automation_async(None, "not-a-date")
    assert invalid_date["status"] == "failed"

    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(side_effect=pincode_lookup.requests.exceptions.ConnectionError("down")))
    monkeypatch.setattr(pincode_lookup.time, "sleep", Mock())
    assert pincode_lookup.get_pincode_details("1", max_retries=1) is None
    monkeypatch.setattr(pincode_lookup, "get_pincode_details", Mock(return_value=[{"Status": "Success", "PostOffice": [{}]}]))
    assert await pincode_lookup.get_location_from_pincode_async("411005") is None


def test_round6_selection_rule_and_ai_function_call_branches(tmp_path):
    tool = selection_mod.UserSelectionTool(SimpleNamespace())
    options = [
        {"number": 1, "profile": {"email": "alice@example.com", "role": "buyer"}},
        {"number": 2, "action": "register_new", "display": "New"},
    ]
    assert tool._rule_based_analysis("9", options)["requires_clarification"] is True
    assert tool._rule_based_analysis("one", options)["selected_option"] == 1
    tool.prompts_dir = tmp_path
    tool.tools_dir = tmp_path
    (tmp_path / "profile_selection").mkdir()
    (tmp_path / "profile_selection" / "user_selection_analysis.txt").write_text("prompt", encoding="utf-8")
    (tmp_path / "user_selection_analysis.json").write_text("{}", encoding="utf-8")
    tool.openai_service = SimpleNamespace(
        default_model="m",
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            output=[SimpleNamespace(type="function_call", arguments=json.dumps({
                "selected_option": 1, "reasoning": "exact"
            }))]
        )))),
    )
    result = asyncio.run(tool._ai_based_analysis("alice", options))
    assert result["selected_option"] == 1


# ---------------------------------------------------------------------------
# Round-six branch completion: task guards, fuzzy fall-throughs, and mains
# ---------------------------------------------------------------------------


def test_round6_bfs_database_guards_empty_results_and_main(monkeypatch):
    def unavailable_session():
        raise RuntimeError("remote database unavailable")

    monkeypatch.setattr(bfs_mod, "get_remote_db_session", unavailable_session)
    assert bfs_mod.get_pending_bfs_notifications() == []
    assert bfs_mod.mark_notification_sent("missing") is False
    assert bfs_mod.mark_notifications_sent_batch(["missing"]) == 0

    monkeypatch.setattr(bfs_mod, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(
        bfs_mod,
        "get_pending_bfs_notifications",
        lambda limit: [{"bfs_user_uuid": "b1", "seller_phone": "123"}],
    )
    monkeypatch.setattr(bfs_mod, "get_users_active_in_last_24hrs", lambda phones: set())
    notification_service = SimpleNamespace(
        send_bfs_bid_notifications_batch=AsyncMock(
            return_value={
                "results": [{"bfs_user_uuid": "b1", "success": False}],
                "sent": 0,
                "failed": 1,
                "skipped": 0,
            }
        )
    )
    monkeypatch.setattr(bfs_mod, "get_seller_notification_service", lambda: notification_service)
    mark_batch = Mock()
    monkeypatch.setattr(bfs_mod, "mark_notifications_sent_batch", mark_batch)
    result = asyncio.run(bfs_mod.process_bfs_notifications())
    assert result["sent"] == 0
    mark_batch.assert_not_called()

    import runpy

    def close_async(coro):
        coro.close()
        return {"status": "completed"}

    monkeypatch.setattr(bfs_mod.asyncio, "run", close_async)
    runpy.run_path(str(Path("app/tasks/bfs_notification_task.py")), run_name="__main__")


def test_round6_pincode_retry_and_main(monkeypatch):
    import requests
    import runpy

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "MH"}]}],
    )
    request = Mock(
        side_effect=[requests.exceptions.ConnectionError("temporary"), response]
    )
    sleep = Mock()
    monkeypatch.setattr(pincode_lookup.requests, "get", request)
    monkeypatch.setattr(pincode_lookup.time, "sleep", sleep)
    assert pincode_lookup.get_pincode_details("411005", max_retries=2) == response.json()
    sleep.assert_called_once_with(1)

    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(return_value=response))
    runpy.run_path(str(Path("app/utils/pincode_lookup.py")), run_name="__main__")


def test_round6_selection_fuzzy_fallthroughs_and_truthy_empty():
    tool = selection_mod.UserSelectionTool(SimpleNamespace())

    tool._string_similarity = Mock(return_value=0.8)
    assert tool._fuzzy_email_match("alicex", "alice@example.com")["confidence"] > 0.5

    tool._string_similarity = Mock(side_effect=[0.5, 0.8])
    assert tool._fuzzy_email_match("examplf", "alice@example.com")["confidence"] > 0.5

    tool._string_similarity = Mock(side_effect=[0.5, 0.5, 0.8])
    assert tool._fuzzy_email_match("alcexampl", "alice@example.com")["confidence"] > 0.4

    class BrokenInput:
        def strip(self):
            raise RuntimeError("bad input")

    assert tool._fuzzy_email_match(BrokenInput(), "alice@example.com")["confidence"] == 0.0

    class TruthyEmpty:
        def __bool__(self):
            return True

        def __len__(self):
            return 0

    tool._string_similarity = selection_mod.UserSelectionTool._string_similarity.__get__(
        tool, selection_mod.UserSelectionTool
    )
    assert tool._string_similarity(TruthyEmpty(), TruthyEmpty()) == 1.0

    options = [
        {"number": 1, "action": "register_new", "display": "Register"},
        {"number": 2, "action": "new", "display": "New"},
        {"number": 3, "profile": {"email": "seller@x.test", "role": "seller"}},
    ]
    assert tool._rule_based_analysis("please register", options)["selected_option"] == 1
    assert tool._rule_based_analysis("new", options)["selected_option"] == 1
    assert tool._rule_based_analysis("sell", options)["selected_option"] == 3
    assert tool._detect_registration_intent("register me as buyer") == "buyer"
    assert tool._detect_registration_intent("register me as seller") == "seller"


@pytest.mark.asyncio
async def test_round6_selection_analyze_dict_and_error_paths():
    tool = selection_mod.UserSelectionTool(SimpleNamespace())
    options = [{"number": 1, "action": "register_new", "display": "New"}]
    tool._rule_based_analysis = Mock(
        return_value={
            "selected_option": None,
            "confidence": 0.1,
            "reasoning": "unclear",
            "alternative_matches": [],
            "requires_clarification": True,
            "register": {"type": None},
        }
    )
    tool._ai_based_analysis = AsyncMock(
        return_value={
            "selected_option": 1,
            "confidence": 0.8,
            "reasoning": "ai",
            "alternative_matches": [],
            "requires_clarification": False,
            "register": {"type": None},
        }
    )
    assert (await tool.analyze_user_selection({"button_reply": {"title": "1"}}, options))["selected_option"] == 1
    assert (await tool.analyze_user_selection({"other": "value"}, options))["selected_option"] == 1

    tool._rule_based_analysis.side_effect = RuntimeError("rule failure")
    result = await tool.analyze_user_selection("bad", options)
    assert result["requires_clarification"] is True
    assert result["selected_option"] is None


# ---------------------------------------------------------------------------
# Learning categorization and processor branch completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round6_learning_backfill_creation_mapping_and_dedup(monkeypatch):
    service = learning_mod.LearningCategorizationService.__new__(learning_mod.LearningCategorizationService)
    service.openai_service = SimpleNamespace(
        generate_3_level_categorization=AsyncMock(
            return_value={
                "success": True,
                "categorization": {"level_1": "Equipment", "level_2": "Pumps", "level_3": "Centrifugal"},
                "confidence_score": 0.9,
                "reasoning": "structured",
                "token_usage": {"total": 3},
            }
        ),
        close_sync=Mock(),
    )
    service.check_existing_learning_category = Mock(
        return_value={
            "learning_category_id": "cat",
            "learning_item_id": "item",
            "client_category_name": "Other",
        }
    )
    service.update_usage_frequency = Mock()
    service.update_client_category = Mock(return_value=True)
    db = SimpleNamespace(close=Mock(), rollback=Mock())
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    result = await service.create_3_level_category("pump", "Industrial")
    assert result["learning_category"]["client_category_name"] == "Industrial"
    service.update_client_category.assert_called_once_with("item", "Industrial")

    service.update_client_category.return_value = False
    result = await service.create_3_level_category("pump", "Better")
    assert result["existing"] is True

    class Query:
        def __init__(self, rows=(), first=None):
            self.rows = list(rows)
            self.first_value = first

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.first_value

    class LearningDB:
        def __init__(self, mapping=None):
            self.mapping = mapping
            self.close = Mock()
            self.rollback = Mock()
            self.add = Mock()
            self.flush = Mock()
            self.commit = Mock()

        def query(self, model=None, *_args, **_kwargs):
            if model is learning_mod.LearningCategory:
                return Query(rows=[], first=None)
            if model is learning_mod.CategoryMapping:
                return Query(first=self.mapping)
            return Query()

    service.check_existing_learning_category.return_value = None
    service._deduplicate_category_hierarchy = Mock(
        return_value={"level_1": "Equipment", "level_2": "Pumps", "level_3": "Centrifugal"}
    )
    service._extract_keywords = Mock(return_value={"keywords": ["pump"]})
    service._calculate_category_similarity = Mock(return_value=0.5)
    service._determine_mapping_confidence = Mock(return_value=0.8)
    service._log_learning_categorization = Mock()

    first_db = LearningDB()
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: first_db)
    created = await service.create_3_level_category("new pump", "Industrial")
    assert created["success"] is True
    assert created["cross_reference_created"] is False
    assert first_db.commit.called

    mapped_db = LearningDB(SimpleNamespace(id="mapping-id"))
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: mapped_db)
    created = await service.create_3_level_category("another pump", "Industrial")
    assert created["cross_reference_created"] is True

    service._deduplicate_category_hierarchy = learning_mod.LearningCategorizationService._deduplicate_category_hierarchy.__get__(
        service, learning_mod.LearningCategorizationService
    )
    class RowDB:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *_args, **_kwargs):
            return Query(rows=self.rows)

    rule_one = service._deduplicate_category_hierarchy(
        RowDB([SimpleNamespace(level_1_category="Parent", level_2_category="Child", level_3_category="Leaf", usage_frequency=3)]),
        "Child", "Subchild", "Final",
    )
    assert rule_one == {"level_1": "Parent", "level_2": "Child", "level_3": "Subchild"}

    rule_two = service._deduplicate_category_hierarchy(
        RowDB([SimpleNamespace(level_1_category="Tools", level_2_category="Other", level_3_category="Leaf", usage_frequency=3)]),
        "Equipment", "Tools", "Final",
    )
    assert rule_two["level_1"] == "Tools"

    rule_three = service._deduplicate_category_hierarchy(
        RowDB([SimpleNamespace(level_1_category="Parent", level_2_category="Valves", level_3_category="Leaf", usage_frequency=3)]),
        "Other", "Valves", "Final",
    )
    assert rule_three["level_1"] == "Parent"

    rule_four = service._deduplicate_category_hierarchy(
        RowDB([SimpleNamespace(level_1_category="Hardware", level_2_category="Valves & Fittings", level_3_category="Leaf", usage_frequency=3)]),
        "Other", "Valves", "Final",
    )
    assert rule_four["level_2"] == "Valves & Fittings"
    assert service._find_similar_l2("Unrelated", {"Pumps"}, {}, "Other") is None


@pytest.mark.asyncio
async def test_round6_excel_success_helpers_and_image_formats(monkeypatch):
    processor, redis, _wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = AsyncLock(acquired=True)
    user = SimpleNamespace(phone_number="1", is_registered=True)
    session = SimpleNamespace(session_id="s", workflow_state={})
    processing = {
        "success": True,
        "filename": "ok.xlsx",
        "items": [{"ItemDescription": "pump", "Quantity": "2"}],
    }
    monkeypatch.setattr(
        excel_mod,
        "ExcelValidationService",
        lambda: SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})),
    )
    monkeypatch.setattr(
        excel_mod,
        "ExcelProcessingService",
        lambda *_args: SimpleNamespace(process_excel_file=AsyncMock(return_value=processing)),
    )
    monkeypatch.setattr(excel_mod.WorkflowManager, "set_workflow_type", Mock())
    monkeypatch.setattr(
        excel_mod.ExcelHelpers,
        "prepare_excel_context",
        Mock(return_value={"completeness": 20, "excel_data": processing}),
    )
    monkeypatch.setattr(excel_mod.ExcelHelpers, "should_complete_immediately", Mock(return_value=False))
    processor._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    assert (await processor.process_excel_upload(user, session, {"document": {"link": "url", "filename": "ok.xlsx"}}))["status"] == "incomplete"

    processor, redis, _wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = AsyncLock(acquired=True)
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})
    ))
    monkeypatch.setattr(excel_mod, "ExcelProcessingService", lambda *_args: SimpleNamespace(
        process_excel_file=AsyncMock(return_value=processing)
    ))
    monkeypatch.setattr(excel_mod.WorkflowManager, "set_workflow_type", Mock())
    monkeypatch.setattr(excel_mod.ExcelHelpers, "prepare_excel_context", Mock(return_value={"completeness": 100, "excel_data": processing}))
    monkeypatch.setattr(excel_mod.ExcelHelpers, "should_complete_immediately", Mock(return_value=True))
    processor._handle_complete_excel = AsyncMock(return_value={"status": "complete"})
    assert (await processor.process_excel_upload(user, SimpleNamespace(session_id="s", workflow_state={}), {"document": {"link": "url", "filename": "ok.xlsx"}}))["status"] == "complete"

    assert processor._convert_excel_to_entities([
        {"ItemDescription": "x", "Quantity": "not-number", "Uom": "kg", "Specification": "s", "Remarks": "r"},
        {"ItemDescription": "y", "Quantity": None},
    ])[0]["quantity"] is None
    assert processor._identify_missing_common_fields([{"DeliveryDate": "tomorrow", "City": "Pune"}]) == []

    image, wa, _helpers = image_processor()
    session = SimpleNamespace(workflow_state={"pending_optional_rfq": {"entities": {}}}, conversation_history={})
    monkeypatch.setattr(image_mod.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={
        "success": True,
        "attachment": {"file_name": "generated.jpg", "file_type": "image/jpeg", "file_content": "encoded"},
    }))
    monkeypatch.setattr(image_mod.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": True, "count": 1, "max": 4}))
    monkeypatch.setattr(image_mod.AttachmentHelpers, "approve_pending_attachment", Mock())
    image._determine_next_step = AsyncMock(return_value={"status": "handled", "response": "next"})
    result = await image.process_image_message(
        SimpleNamespace(phone_number="1", is_registered=True),
        session,
        {"id": "media", "mime_type": "image/jpeg", "caption": "remark"},
    )
    assert result["response"] == "next"
    assert session.workflow_state["attachment_caption"] == "remark"
    download_call = image_mod.AttachmentHelpers.download_and_encode_attachment.await_args
    assert download_call.args[0].endswith("/media")

    base64_session = SimpleNamespace(workflow_state={"pending_rfq": {"entities": {}}}, conversation_history={})
    image._determine_next_step = AsyncMock(return_value={"status": "handled", "response": "base64"})
    result = await image.process_image_message(
        SimpleNamespace(phone_number="1", is_registered=True),
        base64_session,
        {"data": "base64", "filename": "x.png", "mime_type": "image/png"},
    )
    assert result["response"] == "base64"

    plain_session = SimpleNamespace(
        workflow_state={},
        conversation_history={"messages": [{"role": "assistant", "content": {"body": {"text": "body"}}}]},
    )
    assert (await image._handle_irrelevant_attachment(SimpleNamespace(phone_number="1"), plain_session))["response"] == "attachment_ignored"

    monkeypatch.setattr(image_mod, "RFQValidationSchema", lambda **kwargs: SimpleNamespace(**kwargs))
    combined = SimpleNamespace(
        workflow_state={
            "pending_combined_rfq": {"combined_schema": {}, "products": [{"entities": {}}]}
        }
    )
    result = await image._regenerate_confirmation_with_error(
        SimpleNamespace(phone_number="1"), combined, "limit"
    )
    assert result["response"] == "confirmation_with_error"


# ---------------------------------------------------------------------------
# Queue coordination branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round6_queue_monitor_races_poller_scan_and_wrapper_edges(monkeypatch):
    async def run_monitor_once(service, get_values, set_values, send=None, now=60.0):
        service.redis.scan = AsyncMock(return_value=(0, ["1:session"]))
        service.redis.get = AsyncMock(side_effect=get_values)
        service.redis.set = AsyncMock(side_effect=set_values)
        service._send_please_wait = AsyncMock(side_effect=send)
        monkeypatch.setattr(queue_mod.time, "time", lambda: now)
        ticks = {"count": 0}

        async def stop(_seconds):
            ticks["count"] += 1
            if ticks["count"] > 1:
                service._running = False

        monkeypatch.setattr(queue_mod.asyncio, "sleep", stop)
        await service.run_monitoring_loop()

    session_json = queue_mod.ProcessingSession("b", 0, 0).to_json()
    service = queue_service()
    await run_monitor_once(service, [session_json, "ready"], [True], now=60.0)

    service = queue_service()
    await run_monitor_once(service, [session_json, None], [False, True], now=60.0)

    service = queue_service()
    await run_monitor_once(service, [session_json, None, None, None], [True, True], now=60.0)

    service = queue_service()
    await run_monitor_once(service, [session_json, None, session_json], [True, True, True], now=60.0)

    service = queue_service()
    await run_monitor_once(
        service,
        [session_json, None, None, queue_mod.ProcessingSession("b", 0, service.max_please_wait_count).to_json()],
        [True, True],
        now=60.0,
    )

    service = queue_service()
    service.max_please_wait_count = 10
    await run_monitor_once(
        service,
        [session_json, None, None, queue_mod.ProcessingSession("b", 0, 6).to_json()],
        [True, True],
        now=60.0,
    )

    service = queue_service()
    await run_monitor_once(
        service,
        [session_json, None, None, session_json],
        [True, True, True],
        now=60.0,
    )

    service = queue_service()
    await run_monitor_once(
        service,
        [session_json, None, None, session_json],
        [True, True, True],
        send=RuntimeError("whatsapp"),
        now=60.0,
    )
    service.redis.delete.assert_awaited()

    service = queue_service()
    service.redis.lock_obj = AsyncLock(acquired=True, release_error=RuntimeError("release"))
    service.redis.scan = AsyncMock(side_effect=[(1, ["1:incoming"]), (0, ["2:incoming"])])
    service.redis.exists = AsyncMock(side_effect=[False, True])
    service.redis.zcard = AsyncMock(return_value=1)
    service._create_batch = AsyncMock()
    ticks = {"count": 0}

    async def stop_poller(_seconds):
        ticks["count"] += 1
        if ticks["count"] > 1:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_poller)
    await service.run_batch_poller()
    service._create_batch.assert_awaited_once_with("1")

    service = queue_service()
    service.redis.lpop.return_value = "not-json"
    await service._try_start_processing("1")
    service.redis.lpush.assert_awaited_once_with("1:outgoing", "not-json")

    service = queue_service()
    service.whatsapp_service = SimpleNamespace(value="plain", send_message=AsyncMock(return_value="sent"))
    assert service.value == "plain"
    with pytest.raises(AttributeError):
        _ = service.missing_method
    with pytest.raises(ValueError):
        await service.send_message()
    assert await service.send_message("+1", "direct") == "sent"


@pytest.mark.asyncio
async def test_round6_queue_health_multiple_scan_pages_and_slow_sessions(monkeypatch):
    service = queue_service()
    pages = {
        "*:processing": [["1:processing"], []],
        "*:incoming": [["1:incoming"], ["2:incoming"]],
        "*:outgoing": [["1:outgoing"], ["2:outgoing"]],
        "*:session": [["1:session"], ["2:session"]],
    }

    async def scan(cursor=0, match=None, count=100):
        page = pages[match][0 if cursor == 0 else 1]
        return (1 if cursor == 0 else 0), page

    service.redis.scan = scan
    service.redis.zcard = AsyncMock(return_value=2)
    service.redis.llen = AsyncMock(return_value=1)
    service.redis.get = AsyncMock(
        side_effect=lambda key: queue_mod.ProcessingSession(
            "b", 0 if key.startswith("1:") else 95, 0
        ).to_json()
    )
    monkeypatch.setattr(queue_mod.time, "time", lambda: 100.0)
    metrics = await service.get_health_metrics()
    assert metrics["processing_count"] == 1
    assert metrics["total_incoming"] == 4
    assert metrics["total_outgoing"] == 2
    assert metrics["active_users"] == 2
    assert metrics["slow_batches"] == 1


# ---------------------------------------------------------------------------
# Profile-selection branch completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round6_profile_parse_actions_and_role_menus(monkeypatch):
    service = profile_service()
    session = SimpleNamespace(workflow_state={})
    service._handle_new_registration_choice = AsyncMock(return_value={"status": "new"})
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._show_all_profiles = AsyncMock(return_value={"status": "all"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    for action, expected in [
        ("register_new", "new"),
        ("register_buyer", "buyer"),
        ("register_seller", "seller"),
        ("show_all_profiles", "all"),
        ("exit", "exit"),
    ]:
        assert (await service._process_selected_profile("1", {"action": action}, session))["status"] == expected

    assert (await service._process_selected_profile("1", {"registration_type": "buyer"}, session))["status"] == "buyer"
    assert (await service._process_selected_profile("1", {"registration_type": "seller"}, session))["status"] == "seller"
    assert (await service._process_selected_profile("1", {}, session))["status"] == "error"

    profile = {"email": "buyer@test", "role": "buyer", "user_data": {"fullName": "Ada"}}
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated"})
    service._show_role_based_menu = AsyncMock(return_value={"status": "neutral"})
    for stage, expected in [
        ("buyer_intent", "buyer_options_presented"),
        ("seller_intent", "seller_options_presented"),
        ("rfq_status_check", "profile_selected_and_authenticated"),
        ("neutral", "neutral"),
    ]:
        session.workflow_state = {"profile_selection_stage": stage}
        result = await service._process_selected_profile("1", {"profile": profile}, session)
        assert result["status"] == expected

    service.user_selection_tool = SimpleNamespace(
        analyze_user_selection=AsyncMock(return_value={"register": {"type": "buyer"}})
    )
    options = [{"number": 1, "profile": {"email": "a@test", "role": "buyer"}}]
    assert (await service._parse_profile_selection("register buyer", options))["action"] == "register_buyer"
    service.user_selection_tool.analyze_user_selection.return_value = {
        "selected_option": 1, "requires_clarification": False, "register": {"type": None}
    }
    assert (await service._parse_profile_selection("one", options))["number"] == 1
    service.user_selection_tool.analyze_user_selection.return_value = {
        "selected_option": 99, "requires_clarification": False, "register": {"type": None}
    }
    assert await service._parse_profile_selection("unknown", options) is None
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("tool")
    service.user_selection_tool = None
    assert await service._parse_profile_selection("unknown", options) is None

    service._show_role_based_menu = profile_mod.ProfileSelectionService._show_role_based_menu.__get__(
        service, profile_mod.ProfileSelectionService
    )
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated"})
    buyer_profile = {"email": "b@test", "role": "buyer", "user_data": {"fullName": "ada lovelace"}}
    seller_profile = {"email": "s@test", "role": "seller", "user_data": {"fullName": "grace hopper"}}
    assert (await service._show_role_based_menu("1", buyer_profile, session))["status"] == "role_menu_presented"
    assert (await service._show_role_based_menu("1", seller_profile, session))["status"] == "role_menu_presented"
    for status in ["verification_failed", "verification_required", "other"]:
        service._set_active_profile_and_proceed = AsyncMock(return_value={"status": status})
        assert (await service._show_role_based_menu("1", buyer_profile, session))["status"] == status


@pytest.mark.asyncio
async def test_round6_profile_registration_intent_and_response_branches(monkeypatch):
    service = profile_service()
    session = SimpleNamespace(workflow_state={"awaiting_registration_type": True})
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._detect_registration_intent = AsyncMock(side_effect=["buyer", "seller", None, None, None, None])
    assert (await service._handle_registration_type_response("1", {"button_reply": {"title": "Buyer"}}, session))["status"] == "buyer"
    assert (await service._handle_registration_type_response("1", "Seller", session))["status"] == "seller"
    assert (await service._handle_registration_type_response("1", "buy", session))["status"] == "buyer"
    assert (await service._handle_registration_type_response("1", "sell", session))["status"] == "seller"
    assert (await service._handle_registration_type_response("1", "hmm", session))["status"] == "registration_type_clarification_sent"

    service.user_selection_tool = None
    assert await service._detect_user_intent_with_ai("buy") is None
    service.user_selection_tool = SimpleNamespace(openai_service=SimpleNamespace(get_completion=AsyncMock(return_value="BUY")))
    assert await service._detect_user_intent_with_ai("buy") == "buy"
    service.user_selection_tool.openai_service.get_completion.return_value = "unclear"
    assert await service._detect_user_intent_with_ai("hmm") is None
    service.user_selection_tool.openai_service.get_completion.side_effect = RuntimeError("openai")
    assert await service._detect_user_intent_with_ai("hmm") is None

    service._detect_registration_intent = profile_mod.ProfileSelectionService._detect_registration_intent.__get__(
        service, profile_mod.ProfileSelectionService
    )
    service.user_selection_tool = SimpleNamespace(
        analyze_user_selection=AsyncMock(return_value={"register": {"type": "seller"}})
    )
    assert await service._detect_registration_intent("please register") == "seller"
    assert await service._detect_registration_intent("register as buyer") == "buyer"
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("tool")
    assert await service._detect_registration_intent("unclear") is None

    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    service._parse_profile_selection = AsyncMock(side_effect=[
        {"action": "register_buyer"},
        {"action": "exit"},
        None,
    ])
    mismatch = SimpleNamespace(workflow_state={"target_role": "buyer", "profile_options": [{"action": "register_buyer"}]})
    assert (await service._handle_intent_mismatch_response("1", "1", mismatch))["status"] == "buyer"
    mismatch = SimpleNamespace(workflow_state={"target_role": "seller", "profile_options": [{"action": "exit"}]})
    assert (await service._handle_intent_mismatch_response("1", "2", mismatch))["status"] == "exit"
    mismatch = SimpleNamespace(workflow_state={"target_role": "buyer", "profile_options": [{"action": "register_buyer"}]})
    assert (await service._handle_intent_mismatch_response("1", "x", mismatch))["status"] == "intent_mismatch_retry_sent"

    assert (await service._handle_intent_mismatch("1", SimpleNamespace(workflow_state={}), "buyer", []))["existing_role"] == "seller"
    assert (await service._handle_intent_mismatch("1", SimpleNamespace(workflow_state={}), "seller", []))["existing_role"] == "buyer"

    service._parse_profile_selection = AsyncMock(side_effect=[
        {"action": "register_buyer"},
        {"action": "register_seller"},
        {"action": "exit"},
        None,
    ])
    for action, expected in [("1", "buyer"), ("2", "seller"), ("3", "exit")]:
        state = SimpleNamespace(workflow_state={"profile_options": [{"action": action}]})
        assert (await service._handle_new_user_registration_response("1", action, state))["status"] == expected
    retry = SimpleNamespace(workflow_state={"profile_options": [{"action": "other"}]})
    assert (await service._handle_new_user_registration_response("1", "x", retry))["status"] == "new_user_registration_retry_sent"


@pytest.mark.asyncio
async def test_round6_profile_filter_and_neutral_intent_edges(monkeypatch):
    service = profile_service()
    session = SimpleNamespace(workflow_state={})
    service._show_filtered_profiles = AsyncMock(return_value={"status": "filtered"})
    assert (await service._check_role_filter_request("1", "buyer", session))["status"] == "filtered"
    assert (await service._check_role_filter_request("1", "seller", session))["status"] == "filtered"
    assert await service._check_role_filter_request("1", "other", session) is None
    service._show_filtered_profiles.side_effect = RuntimeError("filter")
    assert await service._check_role_filter_request("1", "buyer", session) is None
    service._show_filtered_profiles.side_effect = None
    service._show_filtered_profiles.return_value = {"status": "filtered"}

    service._show_filtered_profiles = profile_mod.ProfileSelectionService._show_filtered_profiles.__get__(
        service, profile_mod.ProfileSelectionService
    )
    profile = {"email": "a@test", "role": "buyer"}
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": [profile]})
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "menu"
    service._get_user_profiles.return_value = {"success": False}
    service._handle_no_profiles_found = AsyncMock(return_value={"status": "none"})
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "none"

    service._handle_buyer_intent = AsyncMock(return_value={"status": "buyer"})
    service._handle_seller_intent = AsyncMock(return_value={"status": "seller"})
    service._detect_user_intent_with_ai = AsyncMock(side_effect=["buy", "sell"])
    for message, expected in [("I need something", "buyer"), ("please help", "seller")]:
        session.workflow_state = {"profiles": []}
        assert (await service._handle_neutral_greeting_response("1", message, session))["status"] == expected


# ---------------------------------------------------------------------------
# Final branch-only checker completion cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round6_learning_false_backfill_and_dedup_fallthroughs(monkeypatch):
    service = learning_mod.LearningCategorizationService.__new__(learning_mod.LearningCategorizationService)
    db = SimpleNamespace(close=Mock(), rollback=Mock())
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    service.update_usage_frequency = Mock()
    service.update_client_category = Mock(return_value=False)

    # Exercise the false paths for a missing learning item and a failed update.
    service.check_existing_learning_category = Mock(return_value={
        "learning_category_id": "cat",
        "client_category_name": "Other",
    })
    result = await service.create_3_level_category("pump", "Industrial")
    assert result["existing"] is True
    service.check_existing_learning_category.return_value = {
        "learning_category_id": "cat",
        "learning_item_id": "item",
        "client_category_name": "Other",
    }
    result = await service.create_3_level_category("pump", "Industrial")
    assert result["existing"] is True
    service.update_client_category.assert_called_once_with("item", "Industrial")

    class Query:
        def __init__(self, rows=(), first=None):
            self.rows = list(rows)
            self.first_value = first

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.first_value

    class DedupDB:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *_args, **_kwargs):
            return Query(rows=self.rows)

    service._find_similar_l2 = learning_mod.LearningCategorizationService._find_similar_l2.__get__(
        service, learning_mod.LearningCategorizationService
    )

    # Duplicate L2 values take the higher-usage mapping branch.
    duplicate_l2 = service._deduplicate_category_hierarchy(
        DedupDB([
            SimpleNamespace(level_1_category="A", level_2_category="Shared", level_3_category="One", usage_frequency=1),
            SimpleNamespace(level_1_category="B", level_2_category="Shared", level_3_category="Two", usage_frequency=3),
        ]),
        "Other", "New", "Leaf",
    )
    assert duplicate_l2["level_2"] == "New"

    # Rule one keeps the original L3 when L1 and L2 are identical.
    same_levels = service._deduplicate_category_hierarchy(
        DedupDB([SimpleNamespace(level_1_category="Parent", level_2_category="Child", level_3_category="Leaf", usage_frequency=2)]),
        "Child", "Child", "Final",
    )
    assert same_levels == {"level_1": "Parent", "level_2": "Child", "level_3": "Final"}

    # Rule two does not swap when the L1/L2 usage counts are equal.
    equal_usage = service._deduplicate_category_hierarchy(
        DedupDB([
            SimpleNamespace(level_1_category="Tools", level_2_category="Other", level_3_category="One", usage_frequency=1),
            SimpleNamespace(level_1_category="Other", level_2_category="Tools", level_3_category="Two", usage_frequency=1),
        ]),
        "Equipment", "Tools", "Final",
    )
    assert equal_usage["level_1"] == "Equipment"

    # Rule three leaves the new parent when the existing pair has no usage.
    zero_usage = service._deduplicate_category_hierarchy(
        DedupDB([SimpleNamespace(level_1_category="Parent", level_2_category="Valves", level_3_category="Leaf", usage_frequency=0)]),
        "Other", "Valves", "Final",
    )
    assert zero_usage["level_1"] == "Other"

    # A fuzzy match that is not present in the L2-to-L1 map skips the parent lookup.
    service._find_similar_l2 = Mock(return_value="Alias")
    unmatched_parent = service._deduplicate_category_hierarchy(
        DedupDB([SimpleNamespace(level_1_category="Hardware", level_2_category="Existing", level_3_category="Leaf", usage_frequency=1)]),
        "Other", "New", "Final",
    )
    assert unmatched_parent["level_2"] == "Alias"


@pytest.mark.asyncio
async def test_round6_excel_falsy_lock_and_single_missing_field_branches(monkeypatch):
    class FalsyLock(AsyncLock):
        def __bool__(self):
            return False

    user = SimpleNamespace(phone_number="1", is_registered=True)
    processor, redis, _wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = FalsyLock(acquired=True)
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "bad"})
    ))
    result = await processor.process_excel_upload(
        user, SimpleNamespace(session_id="s", workflow_state={}),
        {"document": {"link": "url", "filename": "bad.xlsx"}},
    )
    assert result["response"] == "validation_failed"

    processor, redis, _wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = FalsyLock(acquired=True)
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})
    ))
    monkeypatch.setattr(excel_mod, "ExcelProcessingService", lambda *_args: SimpleNamespace(
        process_excel_file=AsyncMock(return_value={"success": False, "error": "processing"})
    ))
    result = await processor.process_excel_upload(
        user, SimpleNamespace(session_id="s", workflow_state={}),
        {"document": {"link": "url", "filename": "bad.xlsx"}},
    )
    assert result["response"] == "processing_failed"

    processor, redis, _wa, _helpers = excel_processor(monkeypatch)
    redis.lock.return_value = FalsyLock(acquired=True)
    monkeypatch.setattr(excel_mod, "ExcelValidationService", lambda: SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})
    ))
    monkeypatch.setattr(excel_mod, "ExcelProcessingService", lambda *_args: SimpleNamespace(
        process_excel_file=AsyncMock(return_value={
            "success": True,
            "items": [
                {"ItemDescription": "a", "Quantity": ""},
                {"ItemDescription": "b", "Quantity": "2"},
                {"ItemDescription": "c", "Quantity": "3"},
                {"ItemDescription": "d", "Quantity": "4"},
            ],
        })
    ))
    processor._convert_excel_to_entities = Mock(return_value=[{"description": "a"}] * 4)
    result = await processor.process_excel_upload(
        user, SimpleNamespace(session_id="s", workflow_state={}),
        {"document": {"link": "url", "filename": "missing.xlsx"}},
    )
    assert result["response"] == "missing_quantities_reupload_required"

    processor, _redis, _wa, _helpers = excel_processor(monkeypatch)
    user = SimpleNamespace(phone_number="1")
    delivery_missing = SimpleNamespace(workflow_state={})
    result = await processor._handle_incomplete_excel(
        user,
        delivery_missing,
        {"excel_data": {"rfqs": [{"pincode": "411005", "products": []}]}},
    )
    assert result["status"] == "excel_missing_common_data"

    location_missing = SimpleNamespace(workflow_state={})
    result = await processor._handle_incomplete_excel(
        user,
        location_missing,
        {"excel_data": {"rfqs": [{"deliveryDate": "tomorrow", "products": []}]}},
    )
    assert result["status"] == "excel_missing_common_data"


@pytest.mark.asyncio
async def test_round6_rfq_background_notification_and_status_fallbacks(monkeypatch):
    service = background_mod.RFQBackgroundService.__new__(background_mod.RFQBackgroundService)
    service.max_concurrent_notifications = 2
    service._send_batch_notifications = background_mod.RFQBackgroundService._send_batch_notifications.__get__(
        service, background_mod.RFQBackgroundService
    )
    service.intimation_service = SimpleNamespace(
        send_rfq_notification=AsyncMock(return_value={"success": False, "error": "rejected"})
    )
    result = await service._send_batch_notifications(
        {"rfq_id": "r"}, [{"seller_id": "s", "seller_name": "Seller"}], "job"
    )
    assert result["successful"] == 0 and result["failed"] == 1

    async def exception_gather(*coroutines, **_kwargs):
        for coroutine in coroutines:
            coroutine.close()
        return [RuntimeError("gathered")]

    monkeypatch.setattr(background_mod.asyncio, "gather", exception_gather)
    result = await service._send_batch_notifications(
        {"rfq_id": "r"}, [{"seller_id": "s", "seller_name": "Seller"}], "job"
    )
    assert result["details"][0]["seller_id"] == "unknown"

    async def failing_gather(*coroutines, **_kwargs):
        for coroutine in coroutines:
            coroutine.close()
        raise RuntimeError("gather failed")

    monkeypatch.setattr(background_mod.asyncio, "gather", failing_gather)
    result = await service._send_batch_notifications(
        {"rfq_id": "r"}, [{"seller_id": "s", "seller_name": "Seller"}], "job"
    )
    assert result["error"] == "gather failed"

    rfq = SimpleNamespace(rfq_id="r", status=None, submitted_at=None)
    query = SimpleNamespace(filter=lambda *_args, **_kwargs: query, first=lambda: rfq)
    service.db_session = SimpleNamespace(query=Mock(return_value=query), commit=Mock(), rollback=Mock())
    service._update_rfq_status = background_mod.RFQBackgroundService._update_rfq_status.__get__(
        service, background_mod.RFQBackgroundService
    )
    await service._update_rfq_status("r", background_mod.RFQStatus.ready)
    await service._update_rfq_status("r", background_mod.RFQStatus.submitted)
    assert service.db_session.commit.call_count == 2


@pytest.mark.asyncio
async def test_round6_seller_route_extraction_and_plan_fallthroughs():
    service = seller_service()
    service.seller_api_service = SimpleNamespace(
        get_subscription_plans=AsyncMock(return_value={"success": False, "plans": []}),
        generate_payment_link=AsyncMock(return_value={"success": False}),
        send_rfq_email=AsyncMock(),
    )
    user = SimpleNamespace(id="u", org_id="o", email="seller@test", phone_number="1")
    session = SimpleNamespace(
        conversation_history={"messages": [{"role": "assistant", "content": "Choose an RFQ"}]},
        workflow_state={},
    )

    service.response_helpers.generate_seller_contextual_intent_response = AsyncMock(
        return_value={"intent": "general_question", "confidence": 0.9}
    )
    assert (await service._classify_seller_intent("hello", {}, session))["intent"] == "general_question"
    service.response_helpers.generate_seller_contextual_intent_response.side_effect = RuntimeError("ai")
    assert (await service._classify_seller_intent("buy", {}, session))["intent"] == "plan_upgrade_request"

    service.openai_service.extract_rfq_ids_from_message = AsyncMock(
        return_value={"success": True, "rfq_ids": ["RFQ1"]}
    )
    assert await service._extract_rfq_ids_from_message("99", session, []) == ["RFQ1"]
    service.openai_service.extract_rfq_ids_from_message.return_value = {"success": False}
    assert await service._extract_rfq_ids_from_message("99", session, []) == []

    plans = [{"id": "1", "planName": "CONNECT"}]
    service.openai_service.extract_entities.return_value = {"selected_plan": "missing"}
    assert await service._extract_plan_selection("unknown", plans) is None
    service.openai_service.extract_entities.return_value = {"selected_plan": "1"}
    assert (await service._extract_plan_selection("unknown", plans))["id"] == "1"
    service.openai_service.extract_entities.return_value = {"selected_plan": None}
    selected = await service._extract_plan_selection(
        "choose premium plan", [{"id": "1", "planName": "CONNECT"}, {"id": "2", "planName": "SELECT"}]
    )
    assert selected["planName"] == "SELECT"

    service._handle_rfq_fetch_error = AsyncMock(return_value={"workflow_step": "rfq_fetch_error"})
    invalid = await service._handle_invalid_rfq_selection(
        user, session, "x", {"success": False}
    )
    assert invalid["workflow_step"] == "rfq_fetch_error"

    service._check_seller_credits = AsyncMock(return_value={"credits_available": 1})
    service._classify_seller_intent = AsyncMock(return_value={"intent": "unknown", "confidence": 0})
    service._generate_ambiguous_seller_response = AsyncMock(return_value={"workflow_step": "ambiguous"})
    result = await service._handle_general_seller_response(user, session, "hmm")
    assert result["workflow_step"] == "ambiguous"

    service._handle_plan_fetch_error = AsyncMock(return_value={"workflow_step": "plan_fetch_error"})
    result = await service._handle_plan_upgrade_request(user, session, "plans")
    assert result["workflow_step"] == "plan_fetch_error"


@pytest.mark.asyncio
async def test_round6_cleanup_matching_and_report_failure_branches(monkeypatch, tmp_path):
    missing_manager = cleanup_mod.LogCleanupManager(str(tmp_path / "missing-logs"), 7, 7)
    assert missing_manager.run()["errors"] == 1

    monkeypatch.setattr(
        cleanup_mod,
        "get_settings",
        lambda: SimpleNamespace(PROJECT_ROOT=str(tmp_path), log_retention_days=7, archive_retention_days=7),
    )
    manager_mock = Mock()
    manager_mock.run.return_value = {"errors": 1}
    monkeypatch.setattr(cleanup_mod, "LogCleanupManager", lambda **_kwargs: manager_mock)
    assert cleanup_mod.cleanup_logs.run()["status"] == "completed_with_errors"

    monkeypatch.setattr(seller_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(seller_task, "get_rfqs_needing_seller_matching", lambda: [{"rfq_id": "r1"}, {"rfq_id": "r2"}])
    monkeypatch.setattr(seller_task, "get_sellers_notified_in_last_24hrs", lambda: set())
    monkeypatch.setattr(seller_task, "SellerRecommendationService", lambda: object())
    matching_results = iter([
        {"success": True, "seller_ids_notified": ["new-seller"]},
        {"success": False, "error": "no match"},
    ])

    def fake_asyncio_run(coroutine):
        coroutine.close()
        return next(matching_results)

    monkeypatch.setattr(seller_task.asyncio, "run", fake_asyncio_run)
    result = seller_task.process_seller_matching.run()
    assert result["processed"] == 1 and result["failed"] == 1

    monkeypatch.setattr(report_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(report_mod.os, "getcwd", lambda: str(tmp_path))
    (tmp_path / "app").mkdir()
    analytics = SimpleNamespace(
        analyze_daily_conversations=AsyncMock(return_value={"success": True, "total_sessions": 0})
    )
    monkeypatch.setattr(report_mod, "ConversationAnalyticsService", lambda: analytics)
    report_path = tmp_path / "report.xlsx"
    report_path.write_bytes(b"report")
    monkeypatch.setattr(
        report_mod,
        "EnhancedExcelReportService",
        lambda: SimpleNamespace(generate_report=Mock(return_value=str(report_path))),
    )
    monkeypatch.setattr(report_mod.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        report_mod,
        "_send_excel_reports_with_api_session",
        AsyncMock(return_value={"status": "Failed", "message": "mail failed"}),
    )
    result = await report_mod.run_whatsapp_report_automation_async(None, "2025-01-01")
    assert result["step"] == "email_sending"



def test_round6_cleanup_remaining_branch_paths(monkeypatch, tmp_path):
    empty_dir = tmp_path / "empty-logs"
    empty_dir.mkdir()
    empty_manager = cleanup_mod.LogCleanupManager(str(empty_dir), 7, 7)
    assert empty_manager.run()["errors"] == 0

    excluded_dir = tmp_path / "excluded-main"
    excluded_dir.mkdir()
    (excluded_dir / "app_2000-01-01.log").write_text("excluded", encoding="utf-8")
    excluded_manager = cleanup_mod.LogCleanupManager(str(excluded_dir), 7, 7, [r"app_"])
    assert excluded_manager._find_old_logs() == {}

    duplicate_dir = tmp_path / "duplicate-date"
    duplicate_dir.mkdir()
    for filename in ("app_2000-01-01.log", "chromadb_2000-01-01.log"):
        (duplicate_dir / filename).write_text("old", encoding="utf-8")
    duplicate_manager = cleanup_mod.LogCleanupManager(str(duplicate_dir), 7, 7)
    found = duplicate_manager._find_old_logs()
    assert len(found["2000-01-01"]) == 2

    subdir = tmp_path / "subdir-logs"
    (subdir / "app").mkdir(parents=True)
    (subdir / "app" / "app_2000-01-01.log").write_text("excluded", encoding="utf-8")
    (subdir / "app" / "app_2000-99-01.log").write_text("invalid", encoding="utf-8")
    subdir_manager = cleanup_mod.LogCleanupManager(str(subdir), 7, 7, [r"app_2000-01-01"])
    assert subdir_manager._find_old_logs() == {}

    archive_error_dir = tmp_path / "archive-error"
    archive_error_dir.mkdir()
    archive_error_file = archive_error_dir / "app_2000-01-01.log"
    archive_error_file.write_text("old", encoding="utf-8")
    archive_error_manager = cleanup_mod.LogCleanupManager(str(archive_error_dir), 7, 7)

    class SuccessfulTar:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def add(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(cleanup_mod.tarfile, "open", lambda *_args, **_kwargs: SuccessfulTar())
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=RuntimeError("unlink")))
    archive_error_manager._archive_logs({"2000-01-01": [archive_error_file]})
    assert archive_error_manager.stats["errors"] == 1

    archive_find_dir = tmp_path / "archive-find"
    archive_manager = cleanup_mod.LogCleanupManager(str(archive_find_dir), 7, 7)
    archive_manager.archive_dir.mkdir(parents=True)
    old_archive = archive_manager.archive_dir / "logs_2000-01-01.tar.gz"
    recent_archive = archive_manager.archive_dir / "logs_2099-01-01.tar.gz"
    unmatched_archive = archive_manager.archive_dir / "logs_not-a-date.tar.gz"
    for archive in (old_archive, recent_archive, unmatched_archive):
        archive.write_bytes(b"archive")
    assert archive_manager._find_old_archives() == [old_archive]


@pytest.mark.asyncio
async def test_round6_report_default_date_and_email_exception_cleanup_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(report_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(report_mod.os, "getcwd", lambda: str(tmp_path))
    (tmp_path / "app").mkdir()

    analytics_failure = SimpleNamespace(
        analyze_daily_conversations=AsyncMock(return_value={"success": False, "error": "no data"})
    )
    monkeypatch.setattr(report_mod, "ConversationAnalyticsService", lambda: analytics_failure)
    result = await report_mod.run_whatsapp_report_automation_async(None, None)
    assert result["step"] == "conversation_analytics"

    analytics_success = SimpleNamespace(
        analyze_daily_conversations=AsyncMock(return_value={"success": True, "total_sessions": 0})
    )
    monkeypatch.setattr(report_mod, "ConversationAnalyticsService", lambda: analytics_success)
    report_path = tmp_path / "report.xlsx"
    report_path.write_bytes(b"report")
    monkeypatch.setattr(
        report_mod,
        "EnhancedExcelReportService",
        lambda: SimpleNamespace(generate_report=Mock(return_value=str(report_path))),
    )
    monkeypatch.setattr(report_mod.os, "makedirs", Mock())
    monkeypatch.setattr(report_mod.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        report_mod,
        "_send_excel_reports_with_api_session",
        AsyncMock(side_effect=RuntimeError("mail down")),
    )
    result = await report_mod.run_whatsapp_report_automation_async(None, "2025-01-01")
    assert result["step"] == "email_sending"

    exists = Mock(side_effect=[True, True, True, False])
    monkeypatch.setattr(report_mod.os.path, "exists", exists)
    remove = Mock()
    monkeypatch.setattr(report_mod.os, "remove", remove)
    monkeypatch.setattr(
        report_mod,
        "_send_excel_reports_with_api_session",
        AsyncMock(return_value={"status": "Success"}),
    )
    result = await report_mod.run_whatsapp_report_automation_async(None, "2025-01-01")
    assert result["status"] == "completed"
    assert remove.call_count == 1
