"""Deterministic round-three coverage for services, tasks, tools, and utilities.

All cases use local fakes or mocks at database, Redis, HTTP, filesystem,
Celery, OpenAI, and WhatsApp boundaries.  The tests intentionally exercise
fallback, empty, retry, and error branches left by the earlier coverage rounds.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import logging
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import requests

from app.models import ConversationOutcome, UserType, WorkflowType
from app.services import inactivity_timeout_service as timeout_mod
from app.services import learning_categorization_service as learning_mod
from app.services import media_downloader_service as media_mod
from app.services import message_queue_service as queue_mod
from app.services import openai_service as openai_mod
from app.services import opt_out_service as opt_out_mod
from app.services import profile_selection_service as profile_mod
from app.services import registration_service as registration_mod
from app.services import rfq_background_service as background_mod
from app.services import rfq_intimation_service as intimation_mod
from app.services import seller_categorization_service as seller_cat_mod
from app.services import seller_service as seller_mod
from app.services import session_management_service as session_mod
from app.services import user_cache_service as cache_mod
from app.services import vendor_service as vendor_mod
from app.services import webhook_health_monitor_service as health_mod
from app.services import welcome_message_service as welcome_mod
from app.services import whatsapp_service as whatsapp_mod
from app.services.processors import excel_message_processor as excel_processor_mod
from app.services.processors import image_message_processor as image_processor_mod
from app.tasks import bfs_notification_task as bfs_task
from app.tasks import log_cleanup_task as cleanup_task
from app.tasks import seller_matching_task as matching_task
from app.tasks import vector_store_sync_task as vector_task
from app.tasks import whatsapp_report_automation_task as report_task
from app.tools import interaction_logger as interaction_tool
from app.tools import user_selection_tool as selection_tool
from app.utils import logging_utils, pincode_distance, pincode_lookup


class FakeLock:
    def __init__(self, acquired=True, release_error=None):
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock(side_effect=release_error)

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            return self
        return self

    async def __aexit__(self, *_args):
        await self.release()
        return False


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *_args):
        return False


class QueryFake:
    def __init__(self, values=(), first=None, scalar=0, delete=0):
        self.values = list(values)
        self.first_value = first
        self.scalar_value = scalar
        self.delete_value = delete

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.first_value

    def scalar(self):
        return self.scalar_value

    def count(self):
        return self.scalar_value

    def delete(self, **_kwargs):
        return self.delete_value


class DBFake:
    def __init__(self, queries=(), default=None):
        self.queries = list(queries)
        self.default = default or QueryFake()
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.flushed = 0
        self.appended = []

    def query(self, *_args, **_kwargs):
        return self.queries.pop(0) if self.queries else self.default

    def add(self, value):
        self.added.append(value)

    def flush(self):
        self.flushed += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def append_session_data(self, value):
        self.appended.append(value)


class FakeHTTPResponse:
    def __init__(self, status_code=200, content=b"payload", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "image/png"}

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


def bare(cls, **attrs):
    value = cls.__new__(cls)
    for key, item in attrs.items():
        setattr(value, key, item)
    return value


def make_session(**state):
    return SimpleNamespace(
        session_id="session-1",
        external_user_id="+15550001",
        workflow_state=dict(state),
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
        extracted_entities={},
        workflow_type=None,
        outcome=None,
        retention_date=None,
        created_at=None,
        completed_at=None,
        last_activity_at=None,
        user_type=None,
    )


def make_user(role="buyer", registered=True, **overrides):
    values = {
        "id": "user-1",
        "org_id": "org-1",
        "seller_id": "seller-1",
        "phone_number": "+15550001",
        "email": "user@example.com",
        "seller_name": "Acme",
        "is_registered": registered,
        "self_client": role == "buyer",
        "role": role,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_queue_service(redis=None):
    redis = redis or MagicMock()
    for name, value in {
        "set": AsyncMock(return_value=True),
        "exists": AsyncMock(return_value=False),
        "setex": AsyncMock(return_value=True),
        "zadd": AsyncMock(return_value=1),
        "zrange": AsyncMock(return_value=[]),
        "zrem": AsyncMock(return_value=1),
        "rpush": AsyncMock(return_value=1),
        "lpop": AsyncMock(return_value=None),
        "lpush": AsyncMock(return_value=1),
        "scan": AsyncMock(return_value=(0, [])),
        "get": AsyncMock(return_value=None),
        "zcard": AsyncMock(return_value=0),
        "llen": AsyncMock(return_value=0),
        "delete": AsyncMock(return_value=1),
        "close": AsyncMock(),
    }.items():
        setattr(redis, name, value)
    service = bare(
        queue_mod.MessageQueueService,
        redis=redis,
        batch_window=3,
        please_wait_threshold=10,
        max_please_wait_count=2,
        monitoring_poll_interval=1,
        response_ready_ttl=60,
        monitor_lock_ttl=30,
        please_wait_interval_ttl=60,
        _background_tasks=[],
        _running=True,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
    )
    return service


def make_openai_service(monkeypatch):
    settings = SimpleNamespace(
        openai_model_default="default-model",
        openai_model_advanced="advanced-model",
        support_email="support@example.com",
        support_contact_info="call +1",
        PROCUCEV_PORTAL_URL="https://portal.invalid",
    )
    monkeypatch.setattr(openai_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_mod, "get_interaction_logger", lambda: MagicMock())
    service = openai_mod.OpenAIService()
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock()),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
        close=AsyncMock(),
    )
    service._client = client
    service._client_closed = False
    service._load_prompt = Mock(return_value="system prompt")
    return service, client


def function_response(arguments, output_type="function_call", output_text=""):
    return SimpleNamespace(
        output=[SimpleNamespace(type=output_type, arguments=json.dumps(arguments))],
        output_text=output_text,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            input_tokens_details=SimpleNamespace(cached_tokens=2),
        ),
    )


@pytest.mark.asyncio
async def test_timeout_remaining_user_shapes_and_cleanup_paths(monkeypatch):
    redis = MagicMock()
    redis.lock.return_value = FakeLock(acquired=True)
    redis.setex = AsyncMock()
    service = bare(
        timeout_mod.InactivityTimeoutService,
        redis=redis,
        redis_session=SimpleNamespace(get_session=AsyncMock(return_value=None), save_session=AsyncMock()),
        timeout_seconds=10,
        activity_key_ttl=30,
        worker_timeout_threshold=5,
        enabled=True,
        poll_interval=1,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        _monitor_task=None,
    )

    assert "resume creating RFQs" in await service._generate_timeout_message({"selfClient": True}, {})
    seller = {"selfClient": False, "org_id": "org", "phone_number": "+1"}
    monkeypatch.setattr(timeout_mod, "ConversationSession", lambda **data: SimpleNamespace(**data))
    seller_service = SimpleNamespace(handle_seller_flow_completion=AsyncMock(return_value={"success": False}))
    monkeypatch.setattr(timeout_mod, "SellerService", seller_service, raising=False)
    # The implementation imports SellerService inside the branch, so patch the module path.
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda: seller_service)
    assert "resume viewing RFQs" in await service._generate_timeout_message([seller], {"session_id": "s"})

    # Exercise the disabled scan and malformed activity branches without touching Redis/network.
    service.enabled = False
    await service._check_inactive_users()
    service.enabled = True
    service.redis.scan = AsyncMock(return_value=(0, ["1:last_activity"]))
    service.redis.get = AsyncMock(return_value="not-a-number")
    await service._check_inactive_users()
    assert service.redis.get.await_count == 1


@pytest.mark.asyncio
async def test_learning_existing_backfill_and_hierarchy_fallback(monkeypatch):
    db = DBFake()
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    service = bare(learning_mod.LearningCategorizationService)
    service.check_existing_learning_category = Mock(return_value={
        "learning_category_id": "cat-1",
        "learning_item_id": "item-1",
        "client_category_name": "Other",
    })
    service.update_usage_frequency = Mock(return_value=True)
    service.update_client_category = Mock(return_value=True)

    result = await service.create_3_level_category("pump", "Industrial")
    assert result["existing"] is True
    assert result["learning_category"]["client_category_name"] == "Industrial"
    service.update_usage_frequency.assert_called_once_with("cat-1")
    service.update_client_category.assert_called_once_with("item-1", "Industrial")
    assert db.closed

    service._find_similar_l2 = Mock(side_effect=RuntimeError("similarity"))
    existing = [SimpleNamespace(
        level_1_category="Hardware",
        level_2_category="Valves",
        level_3_category="Parts",
        usage_frequency=1,
    )]
    assert service._deduplicate_category_hierarchy(
        DBFake(default=QueryFake(values=existing)), "New", "Fittings", "x"
    ) == {"level_1": "New", "level_2": "Fittings", "level_3": "x"}


@pytest.mark.asyncio
async def test_media_downloader_http_filename_and_error_boundaries(monkeypatch, tmp_path):
    service = bare(media_mod.MediaDownloaderService, download_dir=tmp_path, media_download_url="https://media.invalid")
    monkeypatch.setattr(media_mod.requests, "get", Mock(return_value=FakeHTTPResponse(
        headers={"content-disposition": 'attachment; filename="report.pdf"', "content-type": "application/pdf"}
    )))
    result = await service.download_media("media-1")
    assert result["success"] and result["file_info"]["filename"] == "report.pdf"
    assert (tmp_path / "report.pdf").read_bytes() == b"payload"

    monkeypatch.setattr(media_mod.requests, "get", Mock(return_value=FakeHTTPResponse(status_code=503)))
    assert (await service.download_media("bad"))["error"] == "HTTP 503"
    monkeypatch.setattr(media_mod.requests, "get", Mock(side_effect=requests.exceptions.Timeout("offline")))
    assert "Network error" in (await service.download_media("offline"))["error"]
    monkeypatch.setattr(media_mod.requests, "get", Mock(side_effect=RuntimeError("write")))
    assert "Unexpected error" in (await service.download_media("unexpected"))["error"]

    assert service._get_extension_from_content_type("image/png") == ".png"
    assert service._get_extension_from_content_type("application/octet-stream") == ".bin"
    assert (await service.download_from_webhook_content({}))["error"] == "No media ID found in content"
    service.download_media = AsyncMock(return_value={"success": True, "file_info": {}})
    hooked = await service.download_from_webhook_content({"id": "m", "mime_type": "image/png"})
    assert hooked["file_info"]["original_mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_message_queue_models_enqueue_duplicate_and_background_tasks(monkeypatch):
    message = queue_mod.Message("m", "1", "hello", "text", 1.5, {"x": 1})
    assert queue_mod.Message.from_dict(message.to_dict()).content == "hello"
    batch = queue_mod.Batch("b", "1", "hello", "text", 1.5, 1)
    assert queue_mod.Batch.from_dict(batch.to_dict()).batch_id == "b"
    processing = queue_mod.ProcessingSession("b", 1.0, ack_sent=True, suppressed=True)
    assert queue_mod.ProcessingSession.from_json(processing.to_json()).ack_sent is True

    service = make_queue_service()
    service.redis.set = AsyncMock(side_effect=[True, False])
    service.redis.exists = AsyncMock(return_value=False)
    await service.enqueue_message({"from": "+1", "message_id": "m", "timestamp": "2024-01-01 00:00:00", "text": {"body": "hello"}})
    await service.enqueue_message({"from": "+1", "message_id": "m", "timestamp": 2, "type": "image"})
    service.redis.zadd.assert_awaited_once()
    service.redis.setex.assert_awaited_once()

    created = []
    monkeypatch.setattr(queue_mod.asyncio, "create_task", lambda coro, name=None: created.append((coro, name)) or SimpleNamespace(done=lambda: False, get_name=lambda: name))
    service._background_tasks = []
    service._ensure_background_tasks()
    assert {name for _, name in created} == {"batch_poller", "monitoring_loop"}
    for coro, _ in created:
        coro.close()
    service._background_tasks = [SimpleNamespace(done=lambda: False, get_name=lambda: "batch_poller"), SimpleNamespace(done=lambda: False, get_name=lambda: "monitoring_loop")]
    service._ensure_background_tasks()


@pytest.mark.asyncio
async def test_message_queue_batch_processing_and_suppression_edges(monkeypatch):
    service = make_queue_service()
    lock = FakeLock(acquired=True)
    service.redis.lock.return_value = lock
    service.redis.exists = AsyncMock(return_value=True)
    await service._create_batch("1")
    service.redis.zrange.assert_not_awaited()

    service.redis.exists = AsyncMock(return_value=False)
    service.redis.zrange = AsyncMock(return_value=["bad-json"])
    await service._create_batch("1")
    service.redis.delete.assert_awaited()

    service.redis.zcard = AsyncMock(return_value=2)
    service.redis.llen = AsyncMock(return_value=0)
    assert await service._should_suppress_response("1") is True
    assert await service._should_send_acknowledgment("1") is False
    assert service._mock_success().success is True

    service.redis.scan = AsyncMock(return_value=(0, ["1:please_wait:interval:1"]))
    service._create_batch = AsyncMock()
    service._try_start_processing = AsyncMock()
    await service._cleanup_and_next("b", "1", True)
    service.redis.delete.assert_awaited()


@pytest.mark.asyncio
async def test_openai_prompt_fallback_rate_limit_and_timeout_cleanup(monkeypatch):
    service, client = make_openai_service(monkeypatch)
    service.settings.support_email = "support@example.com"
    service.prompts_dir = Path("C:/missing-prompts")
    service._load_prompt = openai_mod.OpenAIService._load_prompt.__get__(service, openai_mod.OpenAIService)
    assert "Generate an appropriate response" in service._load_prompt("missing", "missing")
    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad format")))
    assert "Generate an appropriate response" in service._load_prompt("x", "y")
    assert service._build_messages_with_history({"conversation_history": {"openai_messages": "wrong"}}, "current")[-1]["content"] == "current"

    cancellation = AsyncMock()
    monkeypatch.setattr(openai_mod, "CancelService", lambda: SimpleNamespace(_send_cancellation_message=cancellation), raising=False)
    # Patch the import used by the method, not a network service.
    with patch("app.services.cancel_service.CancelService", return_value=SimpleNamespace(_send_cancellation_message=cancellation)):
        result = await service._get_fallback_classification("hello", {"user_role": "buyer"}, user_phone="+1")
    # The fallback returns a usable classification and sends nothing: it used to
    # return None (dereferenced as a dict by callers) and also message the user,
    # which duplicated the caller's own reply.
    cancellation.assert_not_awaited()
    assert result["success"] is False
    assert result["intent"] == "general_inquiry"

    class RateLimit(Exception):
        pass

    monkeypatch.setattr(openai_mod, "RateLimitError", RateLimit)
    service._notify_openai_error = AsyncMock()
    service._get_fallback_classification = AsyncMock(return_value={"fallback": True})
    client.responses.create.side_effect = RateLimit("rate")
    monkeypatch.setattr(openai_mod, "get_user_phone_context", lambda: None)
    assert await service.classify_intent("hello") == {"fallback": True}

    # The timeout helper is tested with all persistence and WhatsApp boundaries mocked.
    redis_session = SimpleNamespace(
        get_session=AsyncMock(return_value=None),
        delete_session=AsyncMock(),
    )
    direct_redis = SimpleNamespace(init_client=AsyncMock(), client=SimpleNamespace(delete=AsyncMock(return_value=9)))
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: direct_redis)
    wa = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr("app.services.whatsapp_service.WhatsAppService", lambda: wa)
    await service._handle_rate_limit_timeout("+1")
    direct_redis.client.delete.assert_awaited_once()
    wa.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_openai_empty_tool_paths_confirmation_and_date_edges(monkeypatch):
    service, client = make_openai_service(monkeypatch)
    service.tools_dir = Path("C:/tools")
    tool_payload = json.dumps({"name": "tool"})
    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: io.StringIO(tool_payload))
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="")
    assert await service.detect_excel_header_row([]) == {"header_row_index": 0, "confidence": 50, "reasoning": "Fallback to first row", "success": False}
    assert (await service.validate_field_value("quantity", "x", {}))["is_valid"] is True
    assert await service.generate_rfq_confirmation({}, {}) == "Here's a summary of your RFQ."

    client.responses.create.return_value = function_response({
        "is_valid": True,
        "normalized_date": "not-a-date",
        "validation_issues": [],
        "confidence": 90,
        "reasoning": "parsed",
    })
    invalid = await service.validate_delivery_date("tomorrow")
    assert invalid["is_valid"] is False and "Invalid date format" in invalid["validation_issues"]

    client.responses.create.side_effect = RuntimeError("date api")
    error = await service.validate_delivery_date("bad")
    assert error["parsing_method"] == "error"


@pytest.mark.asyncio
async def test_opt_out_all_consent_and_remote_failure_paths():
    seller = SimpleNamespace(
        seller_id="s1", seller_name="Acme", phone_number="+1", categories=["Tools"],
        opted_out_notifications=None,
    )
    db = DBFake(default=QueryFake(first=seller))
    service = bare(
        opt_out_mod.OptOutService,
        db_session=db,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(success=True))),
        openai_service=SimpleNamespace(
            generate_opt_out_confirmation=AsyncMock(return_value="bye"),
            generate_opt_in_confirmation=AsyncMock(return_value="welcome"),
            generate_permission_request=AsyncMock(return_value="allow?"),
            detect_opt_out_intent=Mock(return_value={"intent": "opt_out", "confidence": 90, "success": True}),
        ),
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(opt_out_mod, "get_remote_db_session", lambda: DBFake())
        assert (await service.send_permission_request("s1"))["success"] is True
        seller.opted_out_notifications = True
        assert (await service.check_seller_notification_eligibility("s1"))["reason"] == "Seller opted out"
        seller.opted_out_notifications = False
        assert (await service.check_seller_notification_eligibility("s1"))["eligible"] is True
        seller.opted_out_notifications = None
        assert (await service.check_seller_notification_eligibility("s1"))["action"] == "send_permission_request"
        assert service.detect_opt_out_intent("stop")["intent"] == "opt_out"
        service.openai_service.detect_opt_out_intent.side_effect = RuntimeError("ai")
        assert service.detect_opt_out_intent("?")["success"] is False
        assert (await service._update_remote_opt_out_status("s1", True)) is False
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_excel_and_image_processors_cover_registration_locks_and_formats(monkeypatch):
    response_helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="try again"),
        generate_clarification_response=AsyncMock(return_value="need fields"),
        generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"),
    )
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    processor = bare(excel_processor_mod.ExcelMessageProcessor, whatsapp_service=whatsapp, response_helpers=response_helpers, openai_service=object())
    processor.redis = MagicMock()
    processor.redis.lock.return_value = FakeLock(acquired=False)
    unregistered = make_user(registered=False)
    assert (await processor.process_excel_upload(unregistered, make_session(), {}))["response"] == "registration_required"

    user = make_user()
    optional = make_session(pending_optional_rfq={"x": 1})
    monkeypatch.setattr(excel_processor_mod, "ImageMessageProcessor", lambda **_: SimpleNamespace(process_image_message=AsyncMock(return_value={"delegated": True})))
    assert await processor.process_excel_upload(user, optional, {"image": {}}) == {"delegated": True}

    processor.redis.lock.return_value = FakeLock(acquired=True)
    monkeypatch.setattr(excel_processor_mod, "ExcelValidationService", lambda: SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "bad"})))
    result = await processor.process_excel_upload(user, make_session(), {"document": {"link": "url", "filename": "items.xlsx"}})
    assert result["response"] == "validation_failed"

    image = image_processor_mod.ImageMessageProcessor(whatsapp, response_helpers)
    unregistered_result = await image.process_image_message(unregistered, make_session(), {})
    assert unregistered_result["response"] == "registration_required"
    irrelevant = make_session()
    irrelevant.conversation_history = {"messages": [{"role": "assistant", "content": "Use text"}]}
    assert (await image.process_image_message(user, irrelevant, {}))["response"] == "attachment_ignored"

    relevant = make_session(pending_rfq={"entities": {}})
    monkeypatch.setattr(image_processor_mod.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": True, "count": 1, "max": 4}))
    monkeypatch.setattr(image_processor_mod.AttachmentHelpers, "approve_pending_attachment", Mock())
    monkeypatch.setattr(image_processor_mod.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": False, "error": "download"}))
    failed = await image.process_image_message(user, relevant, {"image": {"link": "url", "filename": "x.png", "mime_type": "image/png"}})
    assert failed["response"] == "download"

    monkeypatch.setattr(image_processor_mod.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": True, "attachment": {"file_name": "x.png"}}))
    image._determine_next_step = AsyncMock(return_value={"status": "handled", "response": "ok"})
    success = await image.process_image_message(user, relevant, {"data": "base64", "filename": "x.png"})
    assert success["response"] == "ok"


@pytest.mark.asyncio
async def test_profile_selection_intents_filters_and_verification(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    auth = SimpleNamespace(
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock(return_value=None)),
        verification_check_service=SimpleNamespace(check_and_enforce_verification=AsyncMock(return_value={"access_granted": True})),
        store_user_session=AsyncMock(return_value=True),
    )
    service = bare(
        profile_mod.ProfileSelectionService,
        whatsapp_service=whatsapp,
        authentication_service=auth,
        user_selection_tool=SimpleNamespace(analyze_user_selection=AsyncMock(return_value={"register": {"type": None}})),
    )
    assert await service._detect_registration_intent("register me as buyer") == "buyer"
    service.user_selection_tool.analyze_user_selection.return_value = {"register": {"type": "seller"}}
    assert await service._detect_registration_intent("please sign me up") == "seller"
    service.user_selection_tool.openai_service = SimpleNamespace(get_completion=AsyncMock(return_value="dual"))
    assert await service._detect_user_intent_with_ai("buy and sell") == "dual"

    session = make_session()
    profiles = [{"role": "buyer", "email": "b@example.com", "user_data": {"id": "buyer-1", "username": "b@example.com", "selfClient": True, "phone": "+1", "fullName": "Ada Lovelace"}}]
    assert (await service._handle_buyer_intent("+1", profiles, "buy", session, {"intent": "buy_something"}))["options_count"] == 2
    seller_session = make_session()
    assert (await service._handle_seller_intent("+1", profiles, "sell", seller_session, {}))["status"] == "intent_mismatch_handled"
    assert (await service._handle_no_profiles_found("+1", "greeting", make_session()))["status"] == "new_user_registration_presented"

    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated", "redirect_to_main_flow": True})
    assert (await service._handle_rfq_status_check("+1", profiles, make_session()))["status"] == "profile_selected_and_authenticated"
    service._set_active_profile_and_proceed.reset_mock()
    multiple = profiles + [{"role": "seller", "email": "s@example.com", "user_data": {}}]
    assert (await service._handle_rfq_status_check("+1", multiple, make_session()))["options_count"] == 2

    filtered_session = make_session()
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": multiple})
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert await service._show_filtered_profiles("+1", filtered_session, "buyer") == {"status": "menu"}
    service._get_user_profiles.return_value = {"success": True, "profiles": []}
    assert (await service._show_filtered_profiles("+1", filtered_session, "seller"))["status"] == "no_seller_profiles_message_sent"

    auth.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": False, "redirect_to_support": True, "redirect_info": {"message": "support", "reason": "blocked"}}
    service._set_active_profile_and_proceed = profile_mod.ProfileSelectionService._set_active_profile_and_proceed.__get__(service, profile_mod.ProfileSelectionService)
    exit_handler = AsyncMock()
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *_args, **_kwargs: SimpleNamespace(handle_exit_intent=exit_handler))
    blocked = await service._set_active_profile_and_proceed("+1", profiles[0], make_session(), "buy", "buy_something")
    assert blocked["redirect_to_support"] is True


@pytest.mark.asyncio
async def test_registration_data_confirmation_and_payload_error_paths(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock(return_value=SimpleNamespace(success=True)))
    session_manager = SimpleNamespace(send_and_track_message=AsyncMock(), save_session=AsyncMock())
    service = bare(
        registration_mod.RegistrationService,
        whatsapp_service=whatsapp,
        session_manager=session_manager,
        authentication_helpers=SimpleNamespace(
            generate_registration_message=Mock(return_value="intro"),
            validate_entities=AsyncMock(return_value=({}, "missing")),
            get_missing_fields=Mock(return_value=["email"]),
        ),
        entity_service=SimpleNamespace(extract_entities=AsyncMock(return_value={"entities": {"name": "Ada"}})),
        confirmation_service=SimpleNamespace(parse_confirmation=AsyncMock(return_value="no")),
        _check_exit_command=AsyncMock(return_value=False),
        _generate_contextual_registration_questions=AsyncMock(return_value="need email"),
        _send_confirmation_with_buttons=AsyncMock(),
        settings=SimpleNamespace(),
    )
    initiated = await service.initiate_registration("+1", make_session(), "buyer")
    assert initiated["status"] == "registration_initiated"

    data_session = make_session(user_type="buyer")
    collected = await service.handle_registration_data_collection("+1", "Ada", data_session)
    assert collected["status"] == "data_collection_in_progress"
    service.confirmation_service.parse_confirmation.return_value = "no"
    confirmation = await service.handle_registration_confirmation("+1", "no", data_session)
    assert confirmation["status"] == "registration_restarted"
    assert service._parse_button_response({"button_reply": {"id": "confirm_registration"}}) == "yes"
    assert service._parse_button_response("restart") == "no"
    assert service._parse_button_response(4) is None

    service._submit_registration = AsyncMock(return_value={"status": "registration_completed"})
    service.otp_service = SimpleNamespace(send_otp=AsyncMock(return_value={"status": "otp_sent"}))
    service.confirmation_service.parse_confirmation.return_value = "yes"
    data_session.workflow_state["registration_entities"] = {"email": "a@example.com"}
    submitted = await service.handle_registration_confirmation("+1", "yes", data_session)
    assert submitted["status"] == "otp_sent"

    service._submit_registration = registration_mod.RegistrationService._submit_registration.__get__(service, registration_mod.RegistrationService)
    service.support_notification_service = SimpleNamespace(notify_registration_failed=AsyncMock())
    service._redirect_to_support = AsyncMock(return_value={"status": "registration_failed"})
    monkeypatch.setattr(registration_mod.AuthenticationHelpers, "build_registration_payload_dynamic", Mock(return_value={"email": "a@example.com"}))
    service.register_api_service = SimpleNamespace(register_buyer=AsyncMock(return_value={"statusCode": "500", "message": "bad"}))
    failed = await service._submit_registration("+1", data_session, {"email": "a@example.com"}, "buyer")
    assert failed["status"] == "registration_failed"


@pytest.mark.asyncio
async def test_rfq_background_and_intimation_success_failure_batches(monkeypatch):
    background = object.__new__(background_mod.RFQBackgroundService)
    background.max_concurrent_notifications = 10
    background.max_concurrent_rfqs = 5
    background._active_jobs = {}
    background._job_stats = {"rfqs_processed": 0, "sellers_notified": 0, "notifications_sent": 0, "errors_occurred": 0}
    background._fetch_rfq_data = AsyncMock(return_value={"rfq_id": "r", "rfq_title": "Pump"})
    background.recommendation_service = SimpleNamespace(select_sellers_for_rfq=AsyncMock(return_value={
        "total_selected": 2, "subscribed_sellers": [{"seller_id": "s1", "seller_name": "One"}],
        "unsubscribed_sellers": [{"seller_id": "s2", "seller_name": "Two"}], "selection_metadata": {},
    }))
    async def send_notification(*, seller_id, rfq_data):
        if seller_id == "s1":
            return {"success": True, "message_id": "m"}
        raise RuntimeError("down")
    background.intimation_service = SimpleNamespace(send_rfq_notification=AsyncMock(side_effect=send_notification))
    background._update_rfq_status = AsyncMock()
    result = await background.process_approved_rfq("r")
    assert result["success"] and result["notifications_sent"] == 1
    multi = await background.process_multiple_rfqs(["r", "bad"])
    assert multi["successful_rfqs"] >= 1

    db = DBFake(default=QueryFake())
    seller = SimpleNamespace(seller_id="s1", seller_name="Acme", phone_number="+1", email="a@x.com", subscription_credits=1, categories=["Tools"], opted_out_notifications=False)
    intimation = bare(
        intimation_mod.RFQIntimationService,
        db_session=db,
        settings=SimpleNamespace(support_contact_info="support", contact_email="support@example.com"),
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(return_value=whatsapp_mod.MessageResponse(True, "mid"))),
        opt_out_service=SimpleNamespace(check_seller_notification_eligibility=AsyncMock(return_value={"eligible": True}), send_permission_request=AsyncMock()),
        subscription_plans={"basic": {"price": 499, "credits": 5}},
        conversation_timeout_minutes=5,
        mock_procurev=SimpleNamespace(generate_payment_link=AsyncMock(return_value={"success": True, "payment_link": "pay", "payment_id": "p"}), send_rfq_email=AsyncMock(return_value={"success": True, "email_id": "e"}), get_seller_pending_bids=AsyncMock(return_value={"success": True, "pending_bids": []})),
    )
    intimation._get_seller_details = AsyncMock(return_value=seller)
    intimation._record_notification = AsyncMock()
    intimation._record_interaction = AsyncMock()
    intimation._start_conversation_timeout = AsyncMock()
    sent = await intimation.send_rfq_notification("s1", {"rfq_id": "r", "rfq_title": "Pump"})
    assert sent["success"]
    intimation._get_seller_details.return_value = None
    assert (await intimation.send_rfq_notification("missing", {}))["error"] == "Seller not found"
    intimation._get_seller_details.return_value = seller
    assert (await intimation.handle_subscription_selection("s1", "r", "bad"))["error"] == "Invalid subscription plan"
    payment = await intimation.handle_subscription_selection("s1", "r", "basic")
    assert payment["success"]

    intimation._validate_rfq_id = AsyncMock(return_value=False)
    assert (await intimation.handle_rfq_id_request("s1", "bad", "bad"))["error"] == "Invalid RFQ ID"
    intimation._validate_rfq_id.return_value = True
    seller.subscription_credits = 1
    result = await intimation.handle_rfq_id_request("s1", "r", "r")
    assert result["success"]
    intimation._update_notification_response = AsyncMock()
    timeout = await intimation.handle_conversation_timeout("s1", "r")
    assert timeout["success"]


@pytest.mark.asyncio
async def test_seller_service_plan_email_and_closing_fallbacks():
    user = make_user(role="seller")
    session = make_session(seller_workflow_state="awaiting_plan_selection")
    response_helpers = SimpleNamespace(generate_seller_contextual_response=AsyncMock(return_value="response"))
    service = bare(
        seller_mod.SellerService,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        seller_api_service=SimpleNamespace(
            get_subscription_plans=AsyncMock(return_value={"success": True, "plans": [{"id": "pro", "planName": "pro"}]}),
            generate_payment_link=AsyncMock(return_value={"success": True, "payment_url": "https://pay"}),
            send_rfq_email=AsyncMock(return_value={"data": {"success": True, "results": {"successful": [{"rfq_id": "r1"}], "failed": [{"rfq_id": "r2", "error_code": "NO_CREDITS"}]}}}),
            check_seller_credits=AsyncMock(return_value={"credits_available": 1}),
            fetch_seller_open_rfqs_for_reminder=AsyncMock(return_value={"success": False}),
        ),
        response_helpers=response_helpers,
        openai_service=SimpleNamespace(extract_entities=AsyncMock(return_value={"selected_plan": "pro"})),
        settings=SimpleNamespace(support_contact_info="support", procucev_rfq_details_url="https://portal"),
        rfq_status_service=SimpleNamespace(handle_rfq_status_inquiry=AsyncMock()),
    )
    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("subscribe", {})["intent"] == "plan_upgrade_request"
    assert service._fallback_intent_classification("RFQ details", {})["intent"] == "rfq_access_request"
    assert service._fallback_intent_classification("unknown", {})["intent"] == "general_question"
    plan = await service._handle_plan_upgrade_request(user, session, "upgrade")
    assert plan["workflow_step"] == "show_subscription_plans"
    selected = await service._handle_plan_selection_response(user, session, "pro")
    assert selected["success"] and selected["payment_url"] == "https://pay"

    session = make_session()
    email = await service._process_rfq_email_requests(user, session, ["r1", "r2"])
    assert email["emails_sent"] == 1
    analysis = service._analyze_email_errors([{"success": False, "rfq_id": "r", "error_code": "RFQ_NOT_FOUND"}, {"success": False, "rfq_id": "x"}])
    assert analysis["error_counts"]["RFQ_NOT_FOUND"] == 1 and analysis["error_counts"]["UNKNOWN"] == 1
    closing = await service.handle_seller_flow_completion(user, session)
    assert closing["workflow_step"] == "generic_closing"


@pytest.mark.asyncio
async def test_session_cache_vendor_welcome_and_whatsapp_boundaries(monkeypatch):
    db = DBFake()
    settings = SimpleNamespace(redis_session_storage_enabled=False, license_enabled=False)
    service = bare(
        session_mod.SessionManagementService,
        db_manager=SimpleNamespace(get_conversation_session=Mock(return_value=None), save_conversation_session=Mock(return_value=make_session())),
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        chat_summary_service=SimpleNamespace(), daily_summary_service=SimpleNamespace(),
        redis_session=SimpleNamespace(), settings=settings, redis_enabled=False,
        summarization_helpers=SimpleNamespace(),
    )
    monkeypatch.setattr(session_mod.SessionHelpers, "generate_session_id", lambda *_: "sid")
    monkeypatch.setattr(welcome_mod, "get_welcome_service", lambda: SimpleNamespace(check_and_send_welcome=AsyncMock()))
    created = await service.get_conversation_context("+1")
    assert created is not None
    assert (await service.get_or_create_user("+1")).phone_number == "+1"
    assert service._validate_license() == (True, "License validation disabled")

    redis = SimpleNamespace(get=AsyncMock(return_value={"user_data": [{"id": "u1", "username": "a@x.com", "selfClient": False}]}), set=AsyncMock(return_value=True))
    cache = bare(cache_mod.UserCacheService, redis_service=redis)
    assert await cache.get_user_data("+1")
    assert await cache.store_user_data("+1", [{"username": "a@x.com"}])
    cache.get_user_data = AsyncMock(return_value=[{"id": "u1", "username": "a@x.com", "selfClient": False}])
    options = await cache.get_account_options_for_intent_switch("+1", "buy_something")
    assert options["has_target_accounts"] is False
    assert cache._get_cache_key("+1") == "user_cache:1"

    vendor = vendor_mod.VendorService.__new__(vendor_mod.VendorService)
    class FakeColumn:
        def any(self, *_args):
            return self
    class FakeVendorModel:
        vendor_services = FakeColumn()
        geographic_coverage = FakeColumn()
    monkeypatch.setattr(vendor_mod, "Vendor", FakeVendorModel)
    vendor_obj = SimpleNamespace(vendor_id="v", vendor_name="Vendor", vendor_services=["Tools"], geographic_coverage=["New York City"])
    vendor.db_session = SimpleNamespace(query=lambda *_: QueryFake(values=[vendor_obj]))
    results = vendor.search_vendors({"entities": {"category": "Tools", "location": "York"}})
    assert results[0]["relevance_score"] > 0
    assert vendor.search_vendors({}) == []

    welcome = bare(welcome_mod.WelcomeMessageService, redis_service=SimpleNamespace(exists=AsyncMock(return_value=False), set=AsyncMock(return_value=True), expireat=AsyncMock(return_value=True), delete=AsyncMock(return_value=1)), welcome_flag_prefix="welcome_msg")
    assert await welcome.should_send_welcome("+1")
    assert await welcome.mark_welcome_sent("+1")
    assert await welcome.reset_welcome_flag("+1")
    wa = SimpleNamespace(send_message=AsyncMock(return_value=whatsapp_mod.MessageResponse(True)))
    assert await welcome.check_and_send_welcome("+1", wa)

    whatsapp = bare(
        whatsapp_mod.WhatsAppService,
        mock_mode=False,
        username="u", password="p", from_number="f", base_url="https://wa", template_base_url="https://wa",
        retry_service=SimpleNamespace(retry_with_backoff=AsyncMock(return_value={"success": False, "attempts": 2, "error": "down"})),
    )
    assert (await whatsapp.send_interactive_message("bad", "button", {})).success is False
    assert (await whatsapp.send_list_message("+1", "h", "b", [{"title": str(i)} for i in range(11)])).success is False
    assert (await whatsapp.send_configurable_buttons("+1", "body", [] )).success is False
    assert whatsapp._format_phone_number("+919999999999")


@pytest.mark.asyncio
async def test_health_monitor_http_states_leadership_and_recovery(monkeypatch):
    settings = SimpleNamespace(
        WHATSAPP_BASE_URL="https://wa.invalid", WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p",
        webhook_health_check_interval_seconds=1, webhook_api_response_threshold_seconds=0.01,
        webhook_api_timeout_seconds=1, webhook_failure_grace_period_seconds=0,
        webhook_recovery_confirmations=1, webhook_warning_consecutive_threshold=1,
        webhook_alert_recipients=[], webhook_health_monitoring_enabled=True,
        webhook_alert_state_ttl_seconds=60,
    )
    redis = SimpleNamespace(
        init_client=AsyncMock(), client=SimpleNamespace(set=AsyncMock(return_value=True)),
        get=AsyncMock(return_value=None), set=AsyncMock(), expire=AsyncMock(), delete=AsyncMock(),
    )
    monitor = bare(health_mod.WebhookHealthMonitorService, settings=settings, redis=redis, email_service=SimpleNamespace(send_email_by_template=AsyncMock()), response_threshold=0.01, api_timeout=1, grace_period=0, recovery_confirmations=1, alert_recipients=[], _session=None, _running=False, worker_id="worker")
    response = SimpleNamespace(status=200)
    monitor._get_session = AsyncMock(return_value=SimpleNamespace(post=Mock(return_value=AsyncContext(response))))
    status, _, error = await monitor._check_api_health()
    assert status is health_mod.HealthStatus.OK and error is None
    monitor._get_session = AsyncMock(side_effect=TimeoutError())
    status, _, error = await monitor._check_api_health()
    assert status is health_mod.HealthStatus.CRITICAL and "Timeout" in error
    assert await monitor._try_acquire_leader_lock()
    redis.client.set.return_value = False
    redis.get.return_value = "other"
    assert await monitor._try_acquire_leader_lock() is False

    state = {"current_state": "HEALTHY", "consecutive_failures": 0, "consecutive_successes": 0, "consecutive_warnings": 0, "is_alerting": False, "failure_start_time": None}
    monitor._save_state = AsyncMock()
    await monitor._process_check_result(state, health_mod.HealthStatus.CRITICAL, 100, "down")
    assert state["current_state"] == "FAILING"
    state["failure_start_time"] = (datetime.utcnow() - timedelta(seconds=2)).isoformat()
    monitor._send_critical_alert = AsyncMock()
    await monitor._process_check_result(state, health_mod.HealthStatus.CRITICAL, 100, "down")
    assert state["current_state"] == "ALERTING"
    state["current_state"] = "RECOVERED"
    state["consecutive_successes"] = 0
    monitor._send_recovery_notification = AsyncMock()
    await monitor._process_check_result(state, health_mod.HealthStatus.OK, 1, None)
    assert state["current_state"] == "HEALTHY"


@pytest.mark.asyncio
async def test_low_level_seller_categorization_and_processors_helpers(monkeypatch):
    service = bare(seller_cat_mod.SellerCategorizationService, _processing_stats={"sellers_processed": 0, "categories_mapped": 0, "openai_calls_made": 0, "errors_encountered": 0, "processing_time_total": 0})
    service._get_seller_details = AsyncMock(return_value=None)
    assert (await service.categorize_seller_categories("missing"))["success"] is False
    service._get_seller_details.return_value = SimpleNamespace(seller_id="s", categories=[])
    assert (await service.categorize_seller_categories("s"))["success"] is False

    processor = excel_processor_mod.ExcelMessageProcessor.__new__(excel_processor_mod.ExcelMessageProcessor)
    converted = processor._convert_excel_to_entities([{"ItemDescription": "pump", "Quantity": "2.5", "Uom": "kg"}, {"ItemDescription": "nut", "Quantity": "bad"}])
    assert converted[0]["quantity"] == 2 and converted[1]["quantity"] is None
    assert processor._identify_missing_common_fields([{}]) == ["delivery_date", "location"]

    image = image_processor_mod.ImageMessageProcessor(SimpleNamespace(), SimpleNamespace())
    assert image._is_attachment_relevant(make_session(incomplete_products=[{"description": "x"}]))
    history = make_session()
    history.conversation_history = {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "answer"}]}
    assert image._get_last_bot_message(history) == "answer"


@pytest.mark.asyncio
async def test_tasks_tools_logging_and_pincode_edges(monkeypatch, tmp_path):
    # BFS database and processing paths.
    db = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(keys=lambda: ["uuid"], fetchall=lambda: [("b1",)])), close=Mock(), commit=Mock(), rollback=Mock())
    monkeypatch.setattr(bfs_task, "get_remote_db_session", lambda: db)
    assert bfs_task.get_pending_bfs_notifications(1) == [{"uuid": "b1"}]
    assert bfs_task.mark_notification_sent("b1") is True
    assert bfs_task.mark_notifications_sent_batch([]) == 0
    db.execute.side_effect = RuntimeError("db")
    assert bfs_task.mark_notifications_sent_batch(["b1"]) == 0
    monkeypatch.setattr(bfs_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs_task, "get_pending_bfs_notifications", lambda **_: [{"seller_phone": None, "bfs_user_uuid": "x"}])
    assert (await bfs_task.process_bfs_notifications())["processed"] == 0
    monkeypatch.setattr(bfs_task, "process_bfs_notifications", AsyncMock(side_effect=RuntimeError("task")))
    with pytest.raises(RuntimeError):
        bfs_task.process_bfs_seller_notifications.run()

    # Matching helpers and no-category result.
    monkeypatch.setattr(matching_task, "get_sellers_already_notified_for_rfq", lambda *_: set())
    monkeypatch.setattr(matching_task, "get_rfq_item_categories", lambda *_: [])
    result = await matching_task.process_single_rfq_matching({"rfq_id": "r", "rfq_uuid": "u", "subscribed_notified": 0, "unsubscribed_notified": 0}, SimpleNamespace(), set())
    assert result["message"] == "No categories found in items"
    assert matching_task.extract_delivery_location({"delivery_city": "Pune"}) == {"city": "Pune"}

    # Vector task (seller category mapping) success with the DB mocked.
    monkeypatch.setattr(vector_task, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=True))
    monkeypatch.setattr(vector_task, "_run_seller_category_mapping", lambda: {"success": True, "processed_count": 1})
    assert vector_task.sync_vector_store.run()["status"] == "completed"

    # Log cleanup uses a temporary directory and no external service.
    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "app_2020-01-01.log"
    old.write_text("x", encoding="utf-8")
    manager = cleanup_task.LogCleanupManager(str(logs), 1, 1)
    manager.archive_dir.mkdir()
    found = manager._find_old_logs()
    assert found
    manager._archive_logs(found)
    assert manager.stats["archives_created"] == 1
    assert (logs / "archives" / "logs_2020-01-01.tar.gz").exists()

    interaction = interaction_tool.InteractionLogger(str(tmp_path))
    monkeypatch.setattr(builtins, "open", Mock(side_effect=OSError("disk")))
    interaction.log_error("intent", "message", "error")
    tool = selection_tool.UserSelectionTool(SimpleNamespace())
    assert tool._rule_based_analysis("buyer@example.com", [{"number": 1, "profile": {"email": "buyer@example.com", "role": "buyer"}}])["selected_option"] == 1
    assert tool._detect_registration_intent("register as seller") == "seller"

    logger = logging.getLogger("round3")
    logging_utils.log_info(logger, "info", user_id="u")
    logging_utils.log_debug(logger, "debug")
    logging_utils.log_error(logger, "error", error=RuntimeError("bad"))
    with logging_utils.UserPhoneContext("+1"):
        assert logging_utils.get_user_phone_context() == "+1"
    logging_utils.clear_user_phone_context()

    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(return_value=SimpleNamespace(raise_for_status=Mock(), json=lambda: [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "MH"}]}])))
    assert pincode_lookup.get_pincode_details("411005")
    assert await pincode_lookup.get_location_from_pincode_async("411005") == {"pincode": "411005", "city": "Pune", "state": "MH"}
    assert await pincode_lookup.get_location_from_pincode_async("bad") is None
    pincode_distance._pincode_cache.clear()
    monkeypatch.setattr(pincode_distance, "_get_nominatim", lambda: None)
    assert pincode_distance.get_coordinates_from_pincode("bad") is None
    assert pincode_distance.calculate_distance_between_pincodes("bad", "bad") is None


@pytest.mark.asyncio
async def test_report_success_and_openai_selection_tool_fallback(monkeypatch, tmp_path):
    analytics = SimpleNamespace(analyze_daily_conversations=AsyncMock(return_value={"success": True, "total_sessions": 1, "sessions_df": report_task.pd.DataFrame()}))
    monkeypatch.setattr(report_task, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(report_task, "ConversationAnalyticsService", lambda: analytics)
    monkeypatch.setattr(report_task.os, "getcwd", lambda: str(tmp_path))
    (tmp_path / "app" / "reportStore").mkdir(parents=True)
    report_path = str(tmp_path / "report.xlsx")
    monkeypatch.setattr(report_task, "EnhancedExcelReportService", lambda: SimpleNamespace(generate_report=lambda **_: report_path))
    monkeypatch.setattr(report_task.os.path, "exists", lambda _: True)
    monkeypatch.setattr(report_task, "_send_excel_reports_with_api_session", AsyncMock(return_value={"status": "Success"}))
    result = await report_task.run_whatsapp_report_automation_async(None, "2025-01-02")
    assert result["status"] in {"completed", "success"}

    service = SimpleNamespace(default_model="model", client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))
    tool = selection_tool.UserSelectionTool(service)
    service.client.responses.create.side_effect = RuntimeError("openai")
    fallback = await tool._ai_based_analysis("unclear", [])
    assert fallback["confidence"] == 0.1
    tool._rule_based_analysis = Mock(side_effect=RuntimeError("rules"))
    result = await tool.analyze_user_selection("unclear", [])
    assert result["requires_clarification"] is True
