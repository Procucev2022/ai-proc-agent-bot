"""Deterministic coverage tests for lifecycle and integration-facing services.

All collaborators that can touch a network, database, Redis, OpenAI, WhatsApp,
filesystem, or email API are replaced with small in-memory fakes.  These tests
intentionally exercise failure and fallback branches as well as happy paths.
"""
from __future__ import annotations

import asyncio
import builtins
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, mock_open

import pytest

from app.models import ConversationOutcome, WorkflowType
from app.services import cancel_service as cancel_mod
from app.services import email_service as email_mod
from app.services import exit_service as exit_mod
from app.services import global_error_handler as error_mod
from app.services import inactivity_timeout_service as timeout_mod
from app.services import media_downloader_service as media_mod
from app.services import message_queue_service as queue_mod
from app.services import openai_service as openai_mod
from app.services import opt_out_service as opt_mod
from app.services import session_management_service as session_mod
from app.services import whatsapp_service as wa_mod
from app.services.whatsapp_service import MessageResponse


class FakeLock:
    def __init__(self, acquired: bool = True, error: Exception | None = None,
                 release_error: Exception | None = None):
        self.acquired = acquired
        self.error = error
        self.release_error = release_error
        self.released = 0

    async def acquire(self, **_kwargs):
        if self.error:
            raise self.error
        return self.acquired

    async def release(self):
        self.released += 1
        if self.release_error:
            raise self.release_error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        return False


class FakeRedis:
    """Async Redis-shaped fake shared by the service tests."""

    def __init__(self):
        self.get = AsyncMock(return_value=None)
        self.set = AsyncMock(return_value=True)
        self.setex = AsyncMock(return_value=True)
        self.delete = AsyncMock(return_value=1)
        self.exists = AsyncMock(return_value=False)
        self.scan = AsyncMock(return_value=(0, []))
        self.close = AsyncMock()
        self.lock = Mock(return_value=FakeLock())
        self.client = self
        self.delete_pattern = AsyncMock(return_value=1)
        self.init_client = AsyncMock()
        self.session_exists = AsyncMock(return_value=False)
        self.delete_session = AsyncMock(return_value=True)
        self.get_session = AsyncMock(return_value=None)
        self.store_session = AsyncMock()
        self.save_session = AsyncMock()
        self.refresh_ttl = AsyncMock()
        self.append_message_to_history = AsyncMock()
        self.zcard = AsyncMock(return_value=0)
        self.llen = AsyncMock(return_value=0)
        self.lpop = AsyncMock(return_value=None)
        self.lpush = AsyncMock()
        self.rpush = AsyncMock()
        self.zrange = AsyncMock(return_value=[])
        self.zrem = AsyncMock()
        self.zadd = AsyncMock()


class FakeQuery:
    def __init__(self, values):
        self.values = list(values)

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None

    def count(self):
        return len(self.values)


class FakeDB:
    def __init__(self, values=None):
        self.values = list(values or [])
        self.query_calls = []
        self.commit = Mock()
        self.rollback = Mock()
        self.close = Mock()
        self.save_conversation_session = Mock()
        self.append_session_data = Mock()
        self.execute = Mock()

    def query(self, model=None, *_args, **_kwargs):
        self.query_calls.append(model)
        return FakeQuery(self.values)


def settings(**overrides):
    values = dict(
        redis_url="redis://unit", redis_session_storage_enabled=True,
        license_enabled=False, workflow_timeout_enabled=True,
        workflow_timeout_seconds=300, timeout_poll_interval_seconds=1,
        activity_key_ttl_seconds=420, worker_timeout_threshold_seconds=135,
        pending_reply_ttl_seconds=180, batch_window_seconds=3,
        please_wait_threshold_seconds=15, max_please_wait_count=3,
        monitoring_poll_interval_seconds=1, WHATSAPP_USERNAME="u",
        WHATSAPP_PASSWORD="p", WHATSAPP_FROM_NUMBER="f",
        WHATSAPP_BASE_URL="https://wa", WHATSAPP_TEMPLATE_BASE_URL="https://wa/t",
        WHATSAPP_MEDIA_DOWNLOAD_URL="https://media", WHATSAPP_MOCK_MODE=True,
        retry_max_attempts=1, retry_initial_delay=0, email_templates_path="templates",
        support_email="support@example.com", support_team_numbers=["999"],
        email_signature="Regards", support_contact_info="help@example.com",
        openai_model_default="gpt-5.4-mini", openai_model_advanced="gpt-5.4-mini",
        procucev_link="https://procucev.example", PROCUCEV_PORTAL_URL="https://portal",
        rfq_followup_note="https://portal/login",
        database_mode="local", local_database_url="mysql+pymysql://u:pw@host/db",
        client_database_url="mysql+pymysql://c:cp@host/db",
        remote_database_url="mysql+pymysql://r:rp@host/db",
        enable_remote_categorization=True, sql_debug=False,
        enable_error_notifications=True, is_ssl_enabled=lambda: False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_session(**overrides):
    values = dict(
        session_id="sid", external_user_id="+919999999999",
        workflow_type=WorkflowType.rfq_creation, outcome=None,
        workflow_state={},
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
        extracted_entities={}, whatsapp_context={}, retention_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1), last_activity_at=datetime(2025, 1, 1),
        completed_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_timeout_service(redis=None, redis_session=None):
    service = timeout_mod.InactivityTimeoutService.__new__(timeout_mod.InactivityTimeoutService)
    service.redis = redis or FakeRedis()
    service.redis_session = redis_session or FakeRedis()
    service.timeout_seconds = 10
    service.poll_interval = 1
    service.activity_key_ttl = 30
    service.enabled = True
    service.worker_timeout_threshold = 5
    service.pending_reply_ttl = 20
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service._monitor_task = None
    return service


def make_queue_service(redis=None):
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = redis or FakeRedis()
    service.batch_window = 3
    service.please_wait_threshold = 10
    service.max_please_wait_count = 2
    service.monitoring_poll_interval = 1
    service.response_ready_ttl = 60
    service.monitor_lock_ttl = 30
    service.please_wait_interval_ttl = 60
    service.whatsapp_service = SimpleNamespace(
        send_message=AsyncMock(return_value=MessageResponse(True, "m")),
        send_configurable_buttons=AsyncMock(return_value=MessageResponse(True, "b")),
        plain_value="plain",
    )
    service._background_tasks = []
    service._running = True
    return service


def response(args=None, *, output_type="function_call", output_text="", usage=True,
             raw=False):
    output = []
    if args is not None:
        arguments = args if raw else json.dumps(args)
        output = [SimpleNamespace(type=output_type, arguments=arguments)]
    return SimpleNamespace(
        output=output,
        output_text=output_text,
        usage=(SimpleNamespace(
            input_tokens=10, output_tokens=4,
            input_tokens_details=SimpleNamespace(cached_tokens=2),
        ) if usage else None),
    )


def add_tools(tmp_path: Path, *names: str):
    for name in names:
        (tmp_path / name).write_text("{}", encoding="utf-8")


def make_openai_service(monkeypatch, tmp_path):
    opts = settings()
    interaction = MagicMock()
    monkeypatch.setattr(openai_mod, "get_settings", lambda: opts)
    monkeypatch.setattr(openai_mod, "get_interaction_logger", lambda: interaction)
    service = openai_mod.OpenAIService()
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock()),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
        close=AsyncMock(),
    )
    service._client = client
    service._client_closed = False
    service.tools_dir = tmp_path
    service.prompts_dir = tmp_path
    service._load_prompt = Mock(return_value="PROMPT")
    return service, client, interaction, opts


@pytest.mark.asyncio
async def test_cancel_service_constructor_state_machine_and_cleanup(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)))
    manager = SimpleNamespace(save_session=AsyncMock())
    db = FakeDB()
    confirmation = object()
    monkeypatch.setattr(cancel_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(cancel_mod, "DatabaseManager", lambda: db)
    monkeypatch.setattr(cancel_mod, "OpenAIService", lambda: "ai")
    monkeypatch.setattr(cancel_mod, "ConfirmationTool", lambda ai: ("tool", ai))
    monkeypatch.setattr(cancel_mod, "ConfirmationService", lambda tool: confirmation)
    default_service = cancel_mod.CancelService()
    assert default_service.confirmation_service is confirmation

    service = cancel_mod.CancelService(wa, manager, db, confirmation)
    no_workflow = make_session(workflow_type=None)
    assert (await service.handle_cancel_intent("1", no_workflow))["status"] == "no_workflow"
    no_workflow.workflow_type = WorkflowType.user_exit
    assert (await service.handle_cancel_intent("1", no_workflow))["status"] == "no_workflow"

    active = make_session(
        workflow_state={},
        conversation_history={"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "old"}]},
    )
    result = await service.handle_cancel_intent("1", active)
    assert result["status"] == "confirmation_pending"
    assert active.workflow_state["last_bot_message_before_cancel"] == "old"
    declined = await service.handle_cancel_intent("1", active, "no")
    assert declined["status"] == "cancelled_aborted"

    pending = make_session(workflow_state={"cancel_pending": True})
    confirmed = await service.handle_cancel_intent(
        "1", pending, {"type": "button_reply", "button_reply": {"id": "confirm_yes", "title": "yes"}},
        user=SimpleNamespace(role=True),
    )
    assert confirmed["status"] == "cancelled"
    assert pending.workflow_type is None

    service._send_confirmation_message = AsyncMock(side_effect=RuntimeError("confirmation"))
    assert (await service.handle_cancel_intent("1", make_session()))["status"] == "cancel_error"
    service._send_confirmation_message = cancel_mod.CancelService._send_confirmation_message.__get__(service)
    wa.send_configurable_buttons.side_effect = RuntimeError("down")
    assert not await service._send_confirmation_message("1")
    wa.send_configurable_buttons.side_effect = None
    wa.send_configurable_buttons.return_value = MessageResponse(False)
    assert not await service._send_confirmation_message("1")

    # Exercise both Redis cleanup success and direct DB fallback.
    monkeypatch.setattr("app.utils.datetime_utils.utc_now", lambda: datetime(2025, 1, 2, tzinfo=timezone.utc))
    redis = FakeRedis()
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis)
    clean = make_session(external_user_id="+1")
    assert await service._clear_workflow_state(clean)
    assert clean.workflow_type is None and manager.save_session.await_count >= 1
    direct_db = FakeDB()
    direct = cancel_mod.CancelService(wa, None, direct_db, confirmation)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: (_ for _ in ()).throw(RuntimeError("redis")))
    assert await direct._clear_workflow_state(make_session())
    assert direct_db.save_conversation_session.call_count == 1
    assert await direct._clear_workflow_state(None)

    # User-type branches and all send fallbacks.
    wa.send_configurable_buttons.return_value = MessageResponse(True)
    for user_type in ("buyer", "seller", "unknown", None):
        assert await service._send_cancellation_message("1", user_type)
    wa.send_configurable_buttons.side_effect = RuntimeError("buttons")
    assert not await service._send_cancellation_message("1", "buyer")
    wa.send_configurable_buttons.side_effect = None
    wa.send_message.side_effect = RuntimeError("message")
    assert not await service._send_cancellation_message("1", "unknown")

    auth = FakeRedis()
    auth.retrieve = AsyncMock(return_value=SimpleNamespace(role=SimpleNamespace(value="seller")))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    assert await service._get_user_type_from_auth("+1") == "seller"
    auth.retrieve.return_value = None
    assert await service._get_user_type_from_auth("1") is None
    auth.retrieve.side_effect = RuntimeError("auth")
    assert await service._get_user_type_from_auth("1") is None
    assert service._get_last_bot_message(make_session(conversation_history={"messages": []})) is None
    assert service._get_last_bot_message(SimpleNamespace(conversation_history=object())) is None


@pytest.mark.asyncio
async def test_exit_service_confirmation_and_cleanup_matrix(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)))
    auth = SimpleNamespace(clear_user_token=AsyncMock(return_value=True))
    manager = SimpleNamespace(save_session=AsyncMock())
    db = FakeDB()
    redis_session, redis_base = FakeRedis(), FakeRedis()
    redis_base.scan = AsyncMock(return_value=(0, ["welcome_msg:999", "other:999"]))
    opts = settings(redis_session_storage_enabled=True)
    monkeypatch.setattr(exit_mod, "get_settings", lambda: opts)
    monkeypatch.setattr("app.config.get_settings", lambda: opts)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis_base)
    service = exit_mod.ExitService(wa, auth, manager, db)

    value = make_session(workflow_state={}, external_user_id="+9")
    pending = await service.handle_exit_intent("9", value)
    assert pending["status"] == "exit_confirmation_pending"
    assert value.workflow_state["exit_pending"] is True
    aborted = await service.handle_exit_intent("9", value, message={"type": "button_reply", "button_reply": {"id": "decline_exit", "title": "no"}})
    assert aborted["status"] == "exit_aborted"
    completed = await service.handle_exit_intent("9", make_session(), show_message=False)
    assert completed["status"] == "exit_completed"
    assert db.append_session_data.call_count >= 1

    # Button confirmation, missing auth service, and goodbye suppression.
    service.authentication_service = None
    assert (await service.handle_exit_confirmation("9", make_session(), True, show_message=False))["auth_cleared"] is False
    monkeypatch.setattr(exit_mod, "restore_last_bot_message", AsyncMock())
    assert (await service.handle_exit_confirmation("9", make_session(), False))["status"] == "exit_aborted"
    service._clear_session_data = AsyncMock(side_effect=RuntimeError("clear"))
    assert (await service.handle_exit_confirmation("9", make_session(), True))["status"] == "exit_confirmation_error"
    service._clear_session_data = exit_mod.ExitService._clear_session_data.__get__(service)

    wa.send_configurable_buttons.side_effect = RuntimeError("buttons")
    assert not await service._send_exit_confirmation_message("9")
    wa.send_configurable_buttons.side_effect = None
    wa.send_configurable_buttons.return_value = MessageResponse(False)
    assert not await service._send_exit_confirmation_message("9")
    wa.send_message.side_effect = RuntimeError("goodbye")
    assert not await service._send_goodbye_message("9")

    # Redis scan failure is non-fatal; a session that remains after the retry fails.
    redis_base.init_client.side_effect = RuntimeError("scan")
    assert await service._clear_session_data(make_session())
    redis_base.init_client.side_effect = None
    redis_session.session_exists.side_effect = [True, True]
    assert not await service._clear_session_data(make_session())
    redis_session.session_exists.side_effect = None
    redis_session.session_exists.return_value = False

    # Redis disabled path skips all session-key cleanup.
    opts.redis_session_storage_enabled = False
    monkeypatch.setattr("app.config.get_settings", lambda: opts)
    assert await service._clear_session_data(make_session())
    assert service._get_last_bot_message(make_session(conversation_history={"messages": []})) is None
    assert service._get_last_bot_message(SimpleNamespace(conversation_history=object())) is None


@pytest.mark.asyncio
async def test_timeout_constructor_messages_activity_and_lifecycle(monkeypatch):
    opts = settings()
    redis = FakeRedis()
    session_redis = FakeRedis()
    wa = SimpleNamespace()
    monkeypatch.setattr(timeout_mod, "get_settings", lambda: opts)
    monkeypatch.setattr(timeout_mod.Redis, "from_url", Mock(return_value=redis))
    monkeypatch.setattr(timeout_mod, "get_session_redis_service", lambda: session_redis)
    monkeypatch.setattr(timeout_mod, "WhatsAppService", lambda: wa)
    service = timeout_mod.InactivityTimeoutService()
    assert service.redis is redis and service.enabled
    timeout_mod._timeout_service_instance = None
    monkeypatch.setattr(timeout_mod, "InactivityTimeoutService", lambda: service)
    assert timeout_mod.get_timeout_service() is service
    assert timeout_mod.get_timeout_service() is service

    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100.0)
    lock = FakeLock(True)
    redis.lock.return_value = lock
    await service.update_user_activity("+1")
    assert redis.setex.await_count == 1 and lock.released == 1
    redis.lock.return_value = FakeLock(False)
    await service.update_user_activity("1")
    redis.lock.return_value = FakeLock(error=RuntimeError("lock"))
    await service.update_user_activity("1")
    redis.setex.side_effect = RuntimeError("write")
    await service.update_user_activity("1")

    buyer = SimpleNamespace(self_client=True)
    assert "resume creating" in await service._generate_timeout_message([buyer], {})
    seller = SimpleNamespace(self_client=False, org_id="org", phone_number="1")
    seller_handler = SimpleNamespace(handle_seller_flow_completion=AsyncMock(return_value={"success": True, "message": "seller remainder"}))
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda: seller_handler)
    assert await service._generate_timeout_message([seller], make_session().__dict__) == "seller remainder"
    seller_handler.handle_seller_flow_completion.return_value = {"success": False, "message": ""}
    assert "resume viewing" in await service._generate_timeout_message([seller], make_session().__dict__)
    seller_handler.handle_seller_flow_completion.side_effect = RuntimeError("seller")
    assert "away" in await service._generate_timeout_message([seller], make_session().__dict__)
    assert "resume viewing" in await service._generate_timeout_message([seller], None)
    assert "resume anytime" in await service._generate_timeout_message({"selfClient": None}, None)

    service.enabled = False
    await service.start_monitoring()
    assert await service.try_start_monitoring_if_available() is False
    service.enabled = True
    service._monitor_task = SimpleNamespace(done=lambda: False)
    await service.start_monitoring()
    assert await service.try_start_monitoring_if_available() is True
    service._monitor_task = None
    created = []
    monkeypatch.setattr(timeout_mod.asyncio, "create_task", lambda coro: created.append(coro) or SimpleNamespace(done=lambda: True))
    redis.lock.return_value = FakeLock(False)
    assert await service.try_start_monitoring_if_available() is False
    redis.lock.return_value = FakeLock(True)
    assert await service.try_start_monitoring_if_available() is True
    redis.lock.return_value = FakeLock(error=RuntimeError("availability"))
    service._monitor_task = None
    assert await service.try_start_monitoring_if_available() is True
    for coro in created:
        if hasattr(coro, "close"):
            coro.close()
    service._monitor_task = None
    await service.stop_monitoring()


@pytest.mark.asyncio
async def test_timeout_scan_all_inactive_and_database_paths(monkeypatch):
    service = make_timeout_service()
    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100.0)
    monkeypatch.setattr(timeout_mod.SessionHelpers, "generate_session_id", lambda phone, _kind: f"sid-{phone}")
    service.redis.scan = AsyncMock(return_value=(0, ["worker:last_activity", "idle:last_activity", "done:last_activity", "none:last_activity", "missing:last_activity", "bad:last_activity"]))
    service.redis.get = AsyncMock(side_effect=["0", "0", "0", "0", "0", "bad"])
    service.redis.exists = AsyncMock(side_effect=[True, False, False, False, False])
    service.redis_session.get_session = AsyncMock(side_effect=[
        {"workflow_type": None, "outcome": None},
        {"workflow_type": "None", "outcome": None},
        {"workflow_type": "rfq_creation", "outcome": "completed"},
        {"workflow_type": None, "outcome": None},
        None,
    ])
    service._handle_worker_timeout = AsyncMock()
    service._handle_timeout = AsyncMock()
    db = SimpleNamespace(get_conversation_session=Mock(return_value=None), close=Mock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)
    await service._check_inactive_users()
    service._handle_worker_timeout.assert_awaited_once()
    assert service._handle_timeout.await_count == 0
    assert service.redis.delete.await_count >= 1

    # Active workflow without pending reply reaches the user inactivity handler.
    service.redis.scan = AsyncMock(return_value=(0, ["active:last_activity"]))
    service.redis.get = AsyncMock(return_value="0")
    service.redis.exists = AsyncMock(return_value=False)
    service.redis_session.get_session = AsyncMock(return_value={"workflow_type": "rfq_creation", "outcome": None})
    await service._check_inactive_users()
    service._handle_timeout.assert_awaited_once()

    # Database fallback: old completed sessions and DB failures only clean the key.
    completed_at = datetime(2020, 1, 1)
    db_session = SimpleNamespace(
        session_id="sid-old", external_user_id="old", workflow_type=WorkflowType.rfq_creation,
        outcome=None, workflow_state={}, conversation_history={}, extracted_entities={},
        retention_date=None, created_at=None, last_activity_at=None, completed_at=completed_at,
    )
    service.redis_session.get_session = AsyncMock(return_value=None)
    service.redis.scan = AsyncMock(return_value=(0, ["old:last_activity"]))
    service.redis.get = AsyncMock(return_value="0")
    service.redis.exists = AsyncMock(return_value=False)
    db.get_conversation_session.return_value = db_session
    monkeypatch.setattr(timeout_mod, "utc_now", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))
    await service._check_inactive_users()
    assert service.redis.delete.await_count >= 2
    db.get_conversation_session.side_effect = RuntimeError("db")
    await service._check_inactive_users()
    assert db.close.call_count >= 2

    # Scan-level errors are swallowed, and disabled service exits immediately.
    service.redis.scan.side_effect = RuntimeError("scan")
    await service._check_inactive_users()
    service.enabled = False
    await service._check_inactive_users()


@pytest.mark.asyncio
async def test_timeout_worker_and_user_timeout_persistence_failures(monkeypatch):
    redis, redis_session = FakeRedis(), FakeRedis()
    service = make_timeout_service(redis, redis_session)
    session_data = {
        "session_id": "S", "external_user_id": "1", "workflow_type": "rfq_creation",
        "outcome": None, "workflow_state": {}, "conversation_history": "bad",
        "extracted_entities": [], "retention_date": None, "created_at": None,
        "last_activity_at": None, "completed_at": None,
    }
    redis_session.get_session = AsyncMock(return_value=session_data)
    db = SimpleNamespace(save_conversation_session=Mock(), append_session_data=Mock(), close=Mock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: db)
    await service._handle_worker_timeout("+1", "S", "1:last_activity", "1:pending_reply")
    assert db.save_conversation_session.call_count == 1
    assert redis_session.save_session.await_count == 1

    # ConversationSession conversion and to_dict conversion branches.
    real_session = make_session(session_id="S2", external_user_id="2")
    redis_session.get_session.return_value = real_session
    await service._handle_worker_timeout("2", "S2", "2:last_activity", "2:pending_reply")
    convertible = SimpleNamespace(to_dict=lambda: dict(session_data))
    redis_session.get_session.return_value = convertible
    await service._handle_worker_timeout("3", "S3", "3:last_activity", "3:pending_reply")

    db.save_conversation_session.side_effect = RuntimeError("persist")
    redis_session.get_session.return_value = session_data
    await service._handle_worker_timeout("4", "S4", "4:last_activity", "4:pending_reply")
    redis_session.get_session.side_effect = RuntimeError("read")
    redis.delete.side_effect = RuntimeError("delete")
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    await service._handle_worker_timeout("5", "S5", "a", "p")

    # User timeout persistence and reset branches.
    redis_session.get_session.side_effect = None
    redis_session.get_session.return_value = {
        "session_id": "U", "external_user_id": "+1", "workflow_type": "rfq_creation",
        "outcome": None, "workflow_state": "bad", "conversation_history": "bad",
        "extracted_entities": {}, "retention_date": None,
    }
    redis.get.side_effect = ["0"]
    redis.delete.side_effect = None
    auth = SimpleNamespace(retrieve=AsyncMock(return_value=SimpleNamespace(self_client=True)))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    cache = SimpleNamespace(clear_meaningful_message=AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)
    db.append_session_data.side_effect = RuntimeError("append")
    await service._handle_timeout("+1", "U", "1:last_activity")
    assert redis_session.store_session.await_count >= 1
    assert service.whatsapp_service.send_message.await_count >= 1

    # Race abort and invalid latest activity leave state untouched.
    redis.get.return_value = "99"
    before = redis.delete.await_count
    await service._handle_timeout("1", "U", "1:last_activity")
    assert redis.delete.await_count == before
    redis.get.return_value = "not-a-number"
    redis_session.get_session.return_value = None
    service.whatsapp_service.send_message.side_effect = None
    await service._handle_timeout("1", "U", "1:last_activity")


@pytest.mark.asyncio
async def test_queue_dataclasses_enqueue_batch_and_background_paths(monkeypatch):
    msg = queue_mod.Message("m", "1", "hello", "text", 1.0, {})
    assert queue_mod.Message.from_dict(msg.to_dict()).content == "hello"
    batch = queue_mod.Batch("b", "1", "hello", "text", 1, 1.0)
    assert queue_mod.Batch.from_dict(batch.to_dict()).batch_id == "b"
    proc = queue_mod.ProcessingSession("b", 1.0)
    assert queue_mod.ProcessingSession.from_json(proc.to_json()).batch_id == "b"

    service = make_queue_service()
    service.redis.zadd = AsyncMock()
    service.redis.exists = AsyncMock(return_value=False)
    service._create_batch = AsyncMock()
    await service.enqueue_message({"from": "+1", "timestamp": "2024-01-01 00:00:00", "text": {"body": "hello"}})
    assert service._create_batch.awaited_once_with("1")
    service.redis.exists.return_value = True
    service._refresh_batch_timer = AsyncMock()
    await service.enqueue_message({"from": "1", "timestamp": 2, "type": "image", "content": ""})
    service._refresh_batch_timer.assert_awaited_once_with("1")
    # An unreadable timestamp falls back to arrival time; the message survives.
    service.redis.zadd.reset_mock()
    await service.enqueue_message({"from": "1", "timestamp": "bad", "type": "text", "content": "kept"})
    service.redis.zadd.assert_awaited()
    service.redis.zadd.side_effect = RuntimeError("redis")
    with pytest.raises(RuntimeError):
        await service.enqueue_message({"from": "1", "timestamp": 1, "content": "x"})
    await service._refresh_batch_timer("1")

    # Background task creation, cleanup of completed tasks, and idempotence.
    class Task:
        def __init__(self, name, done=False): self.name, self._done = name, done
        def get_name(self): return self.name
        def done(self): return self._done
        def cancel(self): self._done = True
    service._background_tasks = [Task("old", True)]
    created = []
    def create_task(coro, name=None):
        coro.close()
        task = Task(name or "task")
        created.append(task)
        return task
    monkeypatch.setattr(queue_mod.asyncio, "create_task", create_task)
    service._ensure_background_tasks()
    assert {task.name for task in created} == {"batch_poller", "monitoring_loop"}
    service._background_tasks = [Task("batch_poller"), Task("monitoring_loop")]
    service._ensure_background_tasks()

    # Batch creation: processing/no messages, malformed data, deduplication, and lock error.
    service = make_queue_service()
    service.redis.lock.return_value = FakeLock()
    service.redis.exists.return_value = True
    service._try_start_processing = AsyncMock()
    await service._create_batch("1")
    service.redis.exists.return_value = False
    service.redis.zrange.return_value = []
    await service._create_batch("1")
    service.redis.zrange.return_value = ["bad"]
    await service._create_batch("1")
    assert service.redis.delete.await_count >= 1
    valid1 = queue_mod.Message("1", "1", " Hello ", "text", 1, {}).to_dict()
    valid2 = queue_mod.Message("2", "1", "hello", "text", 2, {}).to_dict()
    valid3 = queue_mod.Message("3", "1", "   ", "text", 3, {}).to_dict()
    service.redis.zrange.return_value = [json.dumps(valid1), json.dumps(valid2), json.dumps(valid3)]
    await service._create_batch("1")
    assert service.redis.rpush.await_count == 1
    service.redis.lock.return_value = FakeLock(error=RuntimeError("lock"))
    await service._create_batch("1")

    # Next batch claim paths.
    service = make_queue_service()
    service.redis.lock.return_value = FakeLock()
    service.redis.exists.return_value = True
    await service._try_start_processing("1")
    service.redis.exists.return_value = False
    service.redis.lpop.return_value = None
    await service._try_start_processing("1")
    service.redis.lpop.return_value = "bad"
    await service._try_start_processing("1")
    service.redis.lpop.side_effect = RuntimeError("claim")
    await service._try_start_processing("1")


@pytest.mark.asyncio
async def test_queue_poller_monitor_processing_wrapper_and_cleanup(monkeypatch):
    service = make_queue_service()
    # Poller lock skipped, then acquired and creates an expired batch; release errors are ignored.
    service.redis.lock.return_value = FakeLock(False)
    ticks = {"n": 0}
    async def poll_sleep(_):
        ticks["n"] += 1
        if ticks["n"] > 2:
            service._running = False
    monkeypatch.setattr(queue_mod.asyncio, "sleep", poll_sleep)
    await service.run_batch_poller()

    service = make_queue_service()
    service.redis.lock.return_value = FakeLock(True, release_error=RuntimeError("release"))
    service.redis.scan = AsyncMock(return_value=(0, ["1:incoming"]))
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.zcard = AsyncMock(return_value=1)
    service._create_batch = AsyncMock()
    ticks = {"n": 0}
    async def poll_once(_):
        ticks["n"] += 1
        if ticks["n"] > 1:
            service._running = False
    monkeypatch.setattr(queue_mod.asyncio, "sleep", poll_once)
    await service.run_batch_poller()
    service.redis.scan.side_effect = RuntimeError("cycle")
    service._running = False
    await service.run_batch_poller()

    # Monitoring success, max-count, log branches, and malformed session errors.
    service = make_queue_service()
    monkeypatch.setattr(queue_mod.time, "time", lambda: 60.0)
    old = queue_mod.ProcessingSession("b", 0, please_wait_sent_count=0).to_json()
    service.redis.scan = AsyncMock(return_value=(0, ["1:session", "bad:session"]))
    service.redis.get = AsyncMock(side_effect=[old, None, None, old, "bad"])
    service.redis.set = AsyncMock(side_effect=[True, True, True, True])
    service.redis.exists = AsyncMock(return_value=True)
    service.redis.setex = AsyncMock()
    service._send_please_wait = AsyncMock()
    ticks = {"n": 0}
    async def monitor_once(_):
        ticks["n"] += 1
        if ticks["n"] > 1:
            service._running = False
    monkeypatch.setattr(queue_mod.asyncio, "sleep", monitor_once)
    await service.run_monitoring_loop()
    assert service._send_please_wait.await_count >= 1

    # Wrapper direct send, suppression, failure, exception, and invalid recipient.
    service = make_queue_service()
    service.redis.get.return_value = None
    assert (await service.send_message("+1", "direct")).success
    with pytest.raises(ValueError):
        await service.send_message("", "bad")
    service.redis.get.return_value = queue_mod.ProcessingSession("b", 0).to_json()
    service._should_suppress_response = AsyncMock(return_value=True)
    service._cleanup_and_next = AsyncMock()
    assert (await service.send_message("+1", "new")).success
    service._should_suppress_response.return_value = False
    service.whatsapp_service.send_message.return_value = MessageResponse(False, error="remote")
    assert not (await service.send_message("+1", "failed")).success
    service.whatsapp_service.send_message.side_effect = RuntimeError("remote")
    with pytest.raises(RuntimeError):
        await service.send_message("+1", "exception")
    service.redis.get.return_value = "bad-json"
    with pytest.raises(json.JSONDecodeError):
        await service.send_message("1", "bad-session")
    assert service.plain_value == "plain"
    with pytest.raises(AttributeError):
        _ = service.not_present

    # Process-batch success/failure and cleanup queue branches.
    service = make_queue_service()
    service.redis.exists.return_value = True
    service._cleanup_and_next = AsyncMock()
    chat = SimpleNamespace(process_message=AsyncMock(), cleanup=AsyncMock())
    class ChatFactory:
        def __init__(self, **_kwargs): pass
        async def process_message(self, **kwargs): await chat.process_message(**kwargs)
        async def cleanup(self): await chat.cleanup()
    class DbContext:
        def __enter__(self): return "db"
        def __exit__(self, *_args): return False
    monkeypatch.setattr("app.services.chat_service.ChatService", ChatFactory)
    monkeypatch.setattr("app.database.get_db_session_context", lambda: DbContext())
    b = queue_mod.Batch("b", "1", "body", "text", 1, 1)
    await service._process_batch(b)
    chat.process_message.side_effect = RuntimeError("chat")
    await service._process_batch(b)

    service = make_queue_service()
    service.redis.scan = AsyncMock(return_value=(0, ["1:please_wait:interval:1"]))
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service._try_start_processing = AsyncMock()
    await service._cleanup_and_next("b", "1", True)
    service.redis.zcard.return_value = 1
    service._create_batch = AsyncMock()
    await service._cleanup_and_next("b", "1", False)
    service.redis.delete.side_effect = RuntimeError("cleanup")
    await service._cleanup_and_next("b", "1", False)


@pytest.mark.asyncio
async def test_queue_status_health_noops_and_shutdown(monkeypatch):
    service = make_queue_service()
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service.redis.get = AsyncMock(return_value=queue_mod.ProcessingSession("b", 0, suppressed=True).to_json())
    service.redis.exists = AsyncMock(return_value=True)
    status = await service.get_queue_status("1")
    assert status["session"]["suppressed"] is True
    await service._send_acknowledgment("1")
    await service._send_please_wait("1")
    assert await service._should_send_acknowledgment("1") is False
    assert await service._should_suppress_response("1") is False
    service.redis.zcard.return_value = 1
    assert await service._should_suppress_response("1") is True
    await service.cleanup_user_state("1")

    service.redis.scan = AsyncMock(side_effect=[
        (0, ["1:processing"]), (0, ["1:incoming"]), (0, ["1:outgoing"]), (0, ["1:session"]),
    ])
    service.redis.zcard.return_value = 2
    service.redis.llen.return_value = 1
    service.redis.get.return_value = queue_mod.ProcessingSession("b", 0).to_json()
    metrics = await service.get_health_metrics()
    assert metrics["processing_count"] == 1 and metrics["active_users"] == 1
    service.redis.scan.side_effect = RuntimeError("metrics")
    assert "error" in await service.get_health_metrics()

    class RunningTask:
        def __init__(self): self.cancelled = False
        def done(self): return False
        def cancel(self): self.cancelled = True
        def __await__(self):
            async def wait():
                return None
            return wait().__await__()
    task = RunningTask()
    service._background_tasks = [task]
    service.redis.close.side_effect = RuntimeError("close")
    await service.shutdown()
    assert task.cancelled and service._running is False


@pytest.mark.asyncio
async def test_openai_lifecycle_prompt_history_and_intent_paths(monkeypatch, tmp_path):
    service, client, interaction, opts = make_openai_service(monkeypatch, tmp_path)
    add_tools(tmp_path, "intent_classification.json")
    constructed = MagicMock(close=AsyncMock())
    monkeypatch.setattr(openai_mod, "AsyncOpenAI", lambda **kwargs: constructed)
    service._client = None
    assert service.client is constructed
    service._client_closed = True
    assert service.client is constructed
    await service.close()
    constructed.close.side_effect = RuntimeError("close")
    await service.close()
    service._client = None
    service.close_sync()
    service._client = client
    service.close_sync()
    await service.__aexit__(None, None, None)
    service._client_closed = False
    assert await service.__aenter__() is service

    service._track_openai_call("x", "phone")
    service._track_openai_call("x", "phone")
    service._track_openai_call("y")
    assert service.get_call_summary("phone")["x"] == 2
    assert service.get_call_summary("missing") == {}
    notifier = SimpleNamespace(notify_general_error=AsyncMock())
    service._get_error_notification_service = Mock(return_value=notifier)
    await service._notify_openai_error("timeout", "down", "method")
    notifier.notify_general_error.side_effect = RuntimeError("notify")
    await service._notify_openai_error("timeout", "down", "method")

    # The later _load_prompt definition is the runtime implementation.
    service.prompts_dir = tmp_path
    (tmp_path / "response_generation").mkdir()
    (tmp_path / "response_generation" / "_get_seller_end_of_flow_reminder_prompt.txt").write_text("end {support_email}", encoding="utf-8")
    (tmp_path / "response_generation" / "_get_seller_common_response_prompt.txt").write_text("common {support_info_email}", encoding="utf-8")
    service._load_prompt = openai_mod.OpenAIService._load_prompt.__get__(service)
    assert service._load_prompt("response_generation", "_get_seller_common_response_prompt", workflow_state="end_of_flow_reminder") == "end support@example.com"
    assert service._load_prompt("response_generation", "_get_seller_common_response_prompt", workflow_state="normal") == "common help@example.com"
    assert service._load_prompt("missing", "missing").startswith("Generate")

    history = [{"role": "user", "content": str(i)} for i in range(12)]
    assert len(service._build_messages_with_history({"conversation_history": {"openai_messages": history}}, "now")) == 11
    assert service._build_messages_with_history({"conversation_history": {"openai_messages": "bad"}}, "") == []
    assert service._build_messages_with_history(None) == []
    assert service._is_image_content('{"mime_type":"image/png"}')
    assert service._is_image_content("plain", {"user_message": {"mime_type": "image/png"}})
    assert not service._is_image_content("plain", {"user_message": "plain"})
    assert service.extract_text_from_message({"content": {"type": "button_reply", "button_reply": {"id": "yes"}}}) == "yes"
    assert service.extract_text_from_message({"content": [{"type": "text", "text": "a"}]}) == "a"
    assert service.extract_text_from_message({"content": 4}) == "[unknown content]"

    client.responses.create.return_value = response({"intent": "buy_something", "confidence": 90, "reasoning": "r"})
    context = {
        "user_role": "buyer", "workflow_type": "rfq", "conversation_stage": "collecting",
        "conversation_history": {"openai_messages": [{"role": "user", "content": "old"}]},
        "workflow_state": {"sectioned_rfq": {"active": True, "current_section": "items", "awaiting_delivery_modification": True, "awaiting_items_modification": True},
                           "pending_rfq": {"x": 1}, "pending_optional_rfq": {"x": 1}, "pending_attachment_decision": True,
                           "extracted_entities": {"description": "bolt"}},
    }
    result = await service.classify_intent({"text": "buy"}, context)
    assert result["success"] and result["intent"] == "buy_something"
    client.responses.create.return_value = response({"intent": "x"}, output_type="text")
    service._get_fallback_classification = AsyncMock(return_value={"fallback": True})
    assert await service.classify_intent("bad") == {"fallback": True}
    class Rate(Exception): pass
    monkeypatch.setattr(openai_mod, "RateLimitError", Rate)
    client.responses.create.side_effect = Rate("rate")
    service._handle_rate_limit_timeout = AsyncMock()
    monkeypatch.setattr(openai_mod, "get_user_phone_context", lambda: "+1")
    assert (await service.classify_intent("rate"))["timeout_handled"]
    service._handle_rate_limit_timeout.assert_awaited_once_with("+1")


@pytest.mark.asyncio
async def test_openai_extraction_reference_response_and_timeout_paths(monkeypatch, tmp_path):
    service, client, interaction, _ = make_openai_service(monkeypatch, tmp_path)
    add_tools(tmp_path, "entity_extraction_with_summaries.json", "reference_detection.json", "reference_merging.json", "contextual_response_generation.json", "completion_response_generation.json", "clarification_response_generation.json", "field_validation.json")
    client.responses.create.return_value = response({"products": [{"description": "bolt"}], "resolved_references": [{"phrase": "usual"}], "confidence": 90})
    assert (await service.extract_entities_with_summary_context("same", [{"summary": "old", "entities": {}, "rfq_ids": [], "outcome": ""}]))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.extract_entities_with_summary_context("x", []))["success"]
    client.responses.create.side_effect = RuntimeError("summary")
    assert not (await service.extract_entities_with_summary_context("x", []))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"has_references": True, "confidence": 80, "reference_types": ["address"], "detected_phrases": ["usual"]})
    assert (await service.analyze_reference_context("usual"))["has_references"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.analyze_reference_context("x"))["success"]
    client.responses.create.side_effect = RuntimeError("reference")
    assert not (await service.analyze_reference_context("x"))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"success": True, "updated_products": [{"city": "Pune"}]})
    assert (await service.merge_resolved_references_with_entities([], [], "x"))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.merge_resolved_references_with_entities([{"x": 1}], [], "x"))["success"]
    client.responses.create.side_effect = RuntimeError("merge")
    assert "merge" in (await service.merge_resolved_references_with_entities([{"x": 1}], [], "x"))["error"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="generated")
    assert await service.generate_response({"user_message": "x"}, [{"x": 1}]) == "generated"
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="")
    assert "apologize" in await service.generate_response({})
    class ApiError(Exception): pass
    monkeypatch.setattr(openai_mod, "APIError", ApiError)
    client.responses.create.side_effect = ApiError("api")
    assert "support" in await service.generate_response({})

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"acknowledgment": "A", "progress_update": "P", "next_question": "Q"})
    assert await service.generate_contextual_response({"user_message": "x"}, ["base"]) == "A\n\nP\n\nQ"
    client.responses.create.return_value = response({"confirmation": "done", "summary": "sum", "next_steps": "next", "reference_id": "R"})
    completed = await service.generate_completion_response({}, {"user_message": "x"})
    assert "Summary:" in completed and "Reference: R" in completed
    client.responses.create.return_value = response({"progress_acknowledgment": "ok", "questions": ["one", "two"]})
    assert "• one" in await service.generate_clarification_response(["fallback"], 20, {})
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert "fallback" in await service.generate_clarification_response(["fallback"], 20, {})

    client.responses.create.return_value = response({"is_valid": False, "validation_score": 50, "severity": "warning"})
    assert (await service.validate_field_value("qty", "x", {"a": 1}))["severity"] == "warning"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.validate_field_value("qty", "x", {}))["is_valid"]
    client.responses.create.side_effect = RuntimeError("field")
    assert (await service.validate_field_value("qty", "x", {}))["severity"] == "warning"

    # Rate-limit timeout with a persisted session, and an empty/error session.
    redis_session, direct_redis = FakeRedis(), FakeRedis()
    redis_session.get_session = AsyncMock(return_value={"session_id": "s", "external_user_id": "1", "workflow_type": "rfq", "workflow_state": {}, "conversation_history": {}, "extracted_entities": {}})
    direct_redis.client = direct_redis
    direct_redis.delete = AsyncMock(return_value=2)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: direct_redis)
    monkeypatch.setattr("app.database.DatabaseManager", lambda: SimpleNamespace(save_conversation_session=Mock(), close=Mock()))
    monkeypatch.setattr("app.services.helpers.session_helpers.SessionHelpers.generate_session_id", staticmethod(lambda *_: "s"))
    monkeypatch.setattr(openai_mod, "ConversationSession", lambda **kwargs: SimpleNamespace(**kwargs), raising=False)
    monkeypatch.setattr("app.services.whatsapp_service.WhatsAppService", lambda: SimpleNamespace(send_message=AsyncMock()))
    await service._handle_rate_limit_timeout("+1")
    redis_session.get_session.side_effect = RuntimeError("redis")
    direct_redis.init_client.side_effect = RuntimeError("init")
    await service._handle_rate_limit_timeout("1")


@pytest.mark.asyncio
async def test_openai_learning_excel_seller_and_confirmation_matrix(monkeypatch, tmp_path):
    service, client, interaction, _ = make_openai_service(monkeypatch, tmp_path)
    service.close_sync = Mock()
    add_tools(tmp_path, "excel_header_detection.json", "division_selection.json", "excel_column_mapping.json", "parse_seller_products.json", "auto_categorization.json", "learning_categorization.json", "category_validation.json", "seller_existing_category_mapping.json", "seller_batch_category_mapping.json", "seller_selection.json", "rfq_confirmation_generation.json", "date_validation.json", "opt_out_intent_detection.json", "entity_extraction_registration_buyer.json", "entity_extraction_registration_seller.json", "email_confirmation_parsing.json", "contextual_interaction_handling.json")
    client.responses.create.return_value = response({"header_row_index": 2, "confidence": 90})
    assert (await service.detect_excel_header_row([[None, "x"]]))["header_row_index"] == 2
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.detect_excel_header_row([]))["header_row_index"] == 0
    client.responses.create.side_effect = RuntimeError("header")
    assert not (await service.detect_excel_header_row([]))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"selected_division": "IT", "confidence": 80})
    assert (await service.select_division({"x": 1}))["selected_division"] == "IT"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.select_division({}))["selected_division"] == "Admin & IT"
    client.responses.create.side_effect = RuntimeError("division")
    assert (await service.select_division({}))["confidence"] == 20

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"column_mapping": {"Qty": "Quantity"}})
    assert (await service.map_excel_columns(["Qty"]))["column_mapping"]["Qty"] == "Quantity"
    client.responses.create.return_value = response({"mapping_details": [{"excel_header": "Item", "target_column": "ItemDescription"}]})
    assert (await service.map_excel_columns(["Item"]))["column_mapping"]["Item"] == "ItemDescription"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.map_excel_columns(["x"]))["success"]
    client.responses.create.side_effect = RuntimeError("columns")
    assert not (await service.map_excel_columns(["x"]))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"items": ["a", "b"], "confidence": 95})
    assert (await service.parse_seller_product_items("a,b"))["items"] == ["a", "b"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.parse_seller_product_items("a"))["fallback_used"]
    client.responses.create.side_effect = RuntimeError("parse")
    assert (await service.parse_seller_product_items("a"))["fallback_used"]

    similar = [{"item": "bolt", "category": "Hardware", "similarity_score": 0.9}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"category": "Hardware", "confidence_score": .9})
    assert (await service.categorize_with_similar_items("bolt", similar, ["Hardware"]))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.categorize_with_similar_items("bolt", similar))["category"] == "Hardware"
    client.responses.create.side_effect = RuntimeError("category")
    assert (await service.categorize_with_similar_items("bolt", []))["category"] is None

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"level_1_category": "IT", "level_2_category": "Hardware", "level_3_category": "Laptop"})
    assert (await service.generate_3_level_categorization("laptop", similar, "IT"))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.generate_3_level_categorization("x", []))["success"]
    client.responses.create.side_effect = RuntimeError("three")
    assert not (await service.generate_3_level_categorization("x", []))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"is_valid": True, "confidence_score": .8})
    assert (await service.validate_learning_category("a", "b", "c", "x"))["is_valid"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.validate_learning_category("a", "b", "c", "x"))["is_valid"]
    client.responses.create.side_effect = RuntimeError("validate")
    assert not (await service.validate_learning_category("a", "b", "c", "x"))["is_valid"]

    cats = [{"id": 1, "level_1_category": "IT", "level_2_category": "Hardware", "level_3_category": "Laptop"}, {"id": 2, "level_1_category": "IT", "level_2_category": "Hardware", "level_3_category": "Desktop"}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"selected_category_id": 1})
    assert (await service.map_seller_category_to_existing_learning("Computers", cats, "Acme", {"city": "Pune"}))["success"]
    client.responses.create.return_value = response({"selected_category_id": 99}, usage=False)
    assert not (await service.map_seller_category_to_existing_learning("x", cats))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.map_seller_category_to_existing_learning("x", cats))["success"]
    class RateMap(Exception): pass
    monkeypatch.setattr(openai_mod, "RateLimitError", RateMap)
    monkeypatch.setattr(openai_mod.asyncio, "sleep", AsyncMock())
    client.responses.create.side_effect = [RateMap("rate"), response({"selected_category_id": 1})]
    assert (await service.map_seller_category_to_existing_learning("x", cats))["success"]
    client.responses.create.side_effect = RateMap("rate")
    assert not (await service.map_seller_category_to_existing_learning("x", cats))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"mappings": [{"seller_category": "Computers", "selected_category_id": 1}, {"seller_category": "bad", "selected_category_id": 99}]})
    batch = await service.map_seller_categories_batch(["Computers", "bad"], cats)
    assert batch["Computers"]["success"] and not batch["bad"]["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert await service.map_seller_categories_batch(["none"], cats) == {}
    client.responses.create.side_effect = RuntimeError("batch")
    assert not (await service.map_seller_categories_batch(["x"], cats))["x"]["success"]

    candidate = [{"seller_id": "S1", "seller_name": "Acme", "ranking": "gold", "category_match": {"original_category": "Hardware", "similarity_score": .8}, "distance_km": 2, "phone_number": "1", "location": {"city": "Pune", "state": "MH"}}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"selected_seller_ids": ["S1", "missing"]})
    assert (await service.select_best_sellers("bolt", candidate))["total_selected"] == 1
    assert not (await service.select_best_sellers("bolt", []))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.select_best_sellers("bolt", candidate))["success"]
    client.responses.create.side_effect = RuntimeError("select")
    assert not (await service.select_best_sellers("bolt", candidate))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"summary": "line1\\nline2 " * 250})
    rfq = {"items": [{"description": "x", "brand": "b\x00" + "z" * 100, "remarks": "r"}] * 6, "delivery_date": "2030-01-01"}
    assert "+1 more items" in await service.generate_rfq_confirmation(rfq, {"user_message": "buy"})
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert await service.generate_rfq_confirmation({}, {}) == "Here's a summary of your RFQ."
    client.responses.create.side_effect = RuntimeError("confirm")
    assert await service.generate_rfq_confirmation({}, {}) == "Here's a summary of your RFQ."
    assert service._strip_base64_from_entities({"attachments": [{"file_content": "secret"}]})["attachments"][0]["file_content"] == "[base64_data]"
    assert service._clean_for_json_serialization({"f": 2.0, "d": date(2024, 1, 1), "x": object()})["f"] == 2


@pytest.mark.asyncio
async def test_openai_dates_registration_confirmation_and_misc(monkeypatch, tmp_path):
    service, client, interaction, _ = make_openai_service(monkeypatch, tmp_path)
    add_tools(tmp_path, "date_validation.json", "opt_out_intent_detection.json", "entity_extraction_registration_buyer.json", "entity_extraction_registration_seller.json", "email_confirmation_parsing.json", "contextual_interaction_handling.json", "registration_type_detection.json")
    (tmp_path / "confirmation_response_classification.txt").write_text("PROMPT", encoding="utf-8")
    (tmp_path / "profile_selection").mkdir()
    (tmp_path / "profile_selection" / "registration_type_detection.txt").write_text("PROMPT", encoding="utf-8")
    future = (date.today() + timedelta(days=10)).isoformat()
    client.responses.create.return_value = response({"is_valid": True, "normalized_date": future, "validation_issues": []})
    assert (await service.validate_delivery_date("next week", future))["is_valid"]
    client.responses.create.return_value = response({"is_valid": True, "normalized_date": "not-a-date"})
    assert not (await service.validate_delivery_date("bad"))["is_valid"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.validate_delivery_date("bad"))["success"]
    client.responses.create.side_effect = RuntimeError("date")
    assert (await service.validate_delivery_date("bad"))["parsing_method"] == "error"

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"intent": "opt_out", "confidence": 90})
    assert (await service.detect_opt_out_intent("stop"))["intent"] == "opt_out"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert (await service.detect_opt_out_intent("?"))["intent"] == "none"
    client.responses.create.side_effect = RuntimeError("opt")
    assert (await service.detect_opt_out_intent("?"))["confidence"] == 20

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"entities": {"email": "a@test"}, "completeness": 80, "confidence": 90})
    assert (await service.extract_registration_entities("email", "old", "seller", {"name": "A"}))["entities"]["email"] == "a@test"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.extract_registration_entities("x"))["success"]
    client.responses.create.side_effect = RuntimeError("registration")
    assert not (await service.extract_registration_entities("x"))["success"]

    service.classify_intent = Mock(return_value={"intent": "sell_something", "confidence": 70, "success": True})
    assert service.classify_auth_intent("sell")["intent"] == "sell"
    service.classify_intent.side_effect = RuntimeError("auth")
    assert service.classify_auth_intent("?")["intent"] == "unclear"

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"status": "confirmed", "selected_email": "a@test"})
    assert (await service.parse_email_confirmation("yes", ["a@test"]))["success"]
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.parse_email_confirmation("?", []))["success"]
    client.responses.create.side_effect = RuntimeError("email")
    assert not (await service.parse_email_confirmation("?", []))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text=" YES ")
    assert await service.parse_confirmation_response("yes") == "yes"
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="maybe")
    assert await service.parse_confirmation_response("maybe") == "unclear"
    client.responses.create.side_effect = RuntimeError("confirmation")
    assert await service.parse_confirmation_response("?") == "unclear"
    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"registration_type": "buyer", "confidence": 90})
    assert (await service.detect_registration_type("buy"))["registration_type"] == "buyer"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.detect_registration_type("?"))["success"]
    client.responses.create.side_effect = RuntimeError("type")
    assert not (await service.detect_registration_type("?"))["success"]

    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="completion"))])
    assert await service.get_completion("prompt") == "completion"
    client.chat.completions.create.side_effect = RuntimeError("chat")
    with pytest.raises(RuntimeError):
        await service.get_completion("prompt")
    client.chat.completions.create.side_effect = None
    client.chat.completions.create.return_value = SimpleNamespace(choices=[])
    with pytest.raises(IndexError):
        await service.get_completion("prompt")

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"response": "done", "actions": [{"type": "modify"}], "context_understanding": {"user_intent": "modify", "confidence": 90}})
    assert (await service.handle_contextual_interaction("change", {"messages": []}, {}, []))["response"] == "done"
    client.responses.create.return_value = response({"x": 1}, output_type="text")
    assert not (await service.handle_contextual_interaction("?", {}, {}, []))["success"]
    client.responses.create.side_effect = RuntimeError("interaction")
    assert (await service.handle_contextual_interaction("?", {}, {}, []))["context_understanding"]["user_intent"] == "error"
    assert "CURRENT EXTRACTED ENTITIES" in service._build_contextual_analysis_prompt("x", {"messages": []}, {"workflow_type": "rfq"}, [{"description": "bolt"}])
    assert service._build_contextual_analysis_prompt("x", {}, {}, [])


@pytest.mark.asyncio
async def test_opt_out_service_local_remote_and_intent_paths(monkeypatch):
    seller = SimpleNamespace(seller_id="S1", seller_name="Seller", categories=["Tools"], phone_number="9199", opted_out_notifications=None, updated_at=None)
    db = FakeDB([seller])
    wa = SimpleNamespace(send_message=AsyncMock(return_value=MessageResponse(True)))
    ai = SimpleNamespace(
        generate_opt_out_confirmation=Mock(return_value="out"),
        generate_opt_in_confirmation=Mock(return_value="in"),
        generate_permission_request=Mock(return_value="permission"),
        detect_opt_out_intent=Mock(return_value={"intent": "opt_out", "confidence": 90, "reasoning": "clear", "success": True}),
    )
    monkeypatch.setattr(opt_mod, "get_db_session", lambda: db)
    monkeypatch.setattr(opt_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(opt_mod, "OpenAIService", lambda: ai)
    remote = MagicMock()
    remote.execute.return_value = SimpleNamespace(rowcount=1)
    monkeypatch.setattr(opt_mod, "get_remote_db_session", lambda: remote)
    service = opt_mod.OptOutService()
    assert (await service.handle_opt_out_request("9199"))["success"]
    wa.send_message.return_value = MessageResponse(False)
    assert not (await service.handle_opt_in_request("9199"))["message_sent"]
    service._get_seller_by_phone = AsyncMock(return_value=None)
    assert (await service.handle_opt_out_request("missing"))["error"] == "Seller not found"
    service._get_seller_by_phone = AsyncMock(return_value=seller)
    wa.send_message.side_effect = RuntimeError("send")
    assert "send" in (await service.handle_opt_in_request("9199"))["error"]

    service._get_seller_by_id = AsyncMock(return_value=None)
    assert (await service.send_permission_request("S1"))["error"] == "Seller not found"
    service._get_seller_by_id = AsyncMock(return_value=SimpleNamespace(**{**seller.__dict__, "opted_out_notifications": True}))
    assert (await service.send_permission_request("S1"))["error"] == "Seller already has consent status"
    service._get_seller_by_id = AsyncMock(return_value=seller)
    seller.opted_out_notifications = None
    wa.send_message.side_effect = None
    assert (await service.send_permission_request("S1"))["success"]
    for value, expected in [(True, False), (False, True), (None, False)]:
        service._get_seller_by_id = AsyncMock(return_value=SimpleNamespace(**{**seller.__dict__, "opted_out_notifications": value}))
        assert (await service.check_seller_notification_eligibility("S1"))["eligible"] is expected
    service._get_seller_by_id = AsyncMock(side_effect=RuntimeError("lookup"))
    assert "Error: lookup" in (await service.check_seller_notification_eligibility("S1"))["reason"]
    assert service.detect_opt_out_intent("stop")["intent"] == "opt_out"
    ai.detect_opt_out_intent.side_effect = RuntimeError("ai")
    assert service.detect_opt_out_intent("?")["success"] is False

    remote = MagicMock()
    remote.execute.return_value = SimpleNamespace(rowcount=1)
    monkeypatch.setattr(opt_mod, "get_remote_db_session", lambda: remote)
    await service._update_seller_opt_out_status("S1", True)
    assert seller.opted_out_notifications is True and db.commit.called and remote.commit.called
    remote.execute.return_value.rowcount = 0
    assert not await service._update_remote_opt_out_status("S1", False)
    remote.execute.side_effect = RuntimeError("remote")
    assert not await service._update_remote_opt_out_status("S1", False)
    assert remote.rollback.called and remote.close.called
    db.values = []
    remote.execute.side_effect = None
    remote.execute.return_value.rowcount = 1
    await service._update_seller_opt_out_status("missing", False)


@pytest.mark.asyncio
async def test_whatsapp_send_retry_interactive_cache_and_formatters(monkeypatch):
    opts = settings(WHATSAPP_MOCK_MODE=False)
    retry = SimpleNamespace(max_retries=1, initial_delay=0, retry_with_backoff=AsyncMock())
    monkeypatch.setattr(wa_mod, "get_settings", lambda: opts)
    monkeypatch.setattr(wa_mod, "get_retry_service", lambda: retry)
    redis = FakeRedis()
    monkeypatch.setattr(wa_mod, "get_redis_service", lambda: redis)
    service = wa_mod.WhatsAppService()
    service._format_phone_number = Mock(return_value="919999999999")
    service._track_message_in_history = AsyncMock()
    service._clear_pending_reply_flag = AsyncMock()

    retry.retry_with_backoff.return_value = {"success": True, "result": MessageResponse(True, "m"), "attempts": 1, "error": None}
    assert (await service.send_message("+1", "hello", session_id="sid")).success
    service._clear_pending_reply_flag.assert_awaited_once()
    retry.retry_with_backoff.return_value = {"success": False, "attempts": 2, "error": "down"}
    assert not (await service.send_message("1", "fail", clear_pending_reply=False)).success
    service._clear_pending_reply_flag = wa_mod.WhatsAppService._clear_pending_reply_flag.__get__(service)
    redis.delete.return_value = 0
    await service._clear_pending_reply_flag("+1")
    redis.delete.side_effect = RuntimeError("redis")
    await service._clear_pending_reply_flag("1")

    # Real send path exercises concatenation, invalid phone, API success/error, and cache clearing.
    monkeypatch.setattr(wa_mod, "post_to_gateway", AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok", json=lambda: {"mid": "id"})))
    redis.delete.side_effect = None
    redis.get.return_value = {"irrelevant_response": {"user_message": "earlier"}}
    async def run_retry(fn):
        try:
            return {"success": True, "result": await fn(), "attempts": 1, "error": None}
        except Exception as exc:
            return {"success": False, "result": None, "attempts": 1, "error": str(exc)}
    retry.retry_with_backoff.side_effect = run_retry
    assert (await service.send_message("1", "body")).success
    redis.get.return_value = None
    service._format_phone_number.return_value = ""
    assert not (await service.send_message("1", "body")).success
    service._format_phone_number.return_value = "919999999999"
    monkeypatch.setattr(wa_mod, "post_to_gateway", AsyncMock(return_value=SimpleNamespace(status_code=400, text="bad", json=lambda: {"Error": "no"})))
    assert not (await service.send_message("1", "body")).success
    monkeypatch.setattr(wa_mod, "post_to_gateway", AsyncMock(side_effect=RuntimeError("post")))
    assert not (await service.send_message("1", "body")).success

    retry.retry_with_backoff.side_effect = None
    retry.retry_with_backoff.return_value = {"success": True, "result": MessageResponse(True, "t"), "attempts": 1, "error": None}
    assert (await service.send_template_message("1", "welcome", ["A", "B"])).success
    retry.retry_with_backoff.return_value = {"success": False, "attempts": 1, "error": "template"}
    assert not (await service.send_template_message("1", "welcome", [])).success

    service._format_phone_number.return_value = "919999999999"
    monkeypatch.setattr(wa_mod, "post_to_gateway", AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok", json=lambda: {"status": "ok"})))
    assert (await service.send_interactive_message("1", "button", {"body": {"text": "x"}})).success
    service._format_phone_number.return_value = ""
    assert not (await service.send_interactive_message("1", "button", {})).success
    service._format_phone_number.return_value = "919999999999"
    monkeypatch.setattr(wa_mod, "post_to_gateway", AsyncMock(side_effect=RuntimeError("interactive")))
    assert not (await service.send_interactive_message("1", "button", {})).success
    assert (await service.send_cta_button_message("1", "body", "go", "https://x")).success is False

    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True))
    assert (await service.send_list_message("1", "h", "b", [{"title": str(i)} for i in range(11)])).success
    assert (await service.send_button_message("1", "h", "b", [{"title": str(i)} for i in range(4)])).success
    service.send_interactive_message.side_effect = RuntimeError("list")
    assert not (await service.send_list_message("1", "h", "b", [])).success

    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True))
    redis.get.return_value = {"irrelevant_response": {"user_message": "cache"}}
    assert (await service.send_configurable_buttons("1", "body\x00", [{"id": "x", "title": "X"}, {"id": "y", "title": "Y"}, {"id": "z", "title": "Z"}, {"id": "w", "title": "W"}], header="H\x00", session_id="sid")).success
    assert not (await service.send_configurable_buttons("1", "body", [])).success
    assert not (await service.send_configurable_buttons("1", "body", [{"id": "x", "title": ""}])).success

    for phone in ("", "abc", "123", "01234567890", "9876543210", "919876543210", "123456789012", "1" * 16):
        service._format_phone_number(phone)
    assert service.format_vendor_results([]).startswith("No vendors")
    assert service.format_bfs_results([]).startswith("No products")
    assert "Product" in service.format_rfq_summary({"product_name": "bolt", "quantity": 2, "unit_of_measure": "kg", "deadline": "tomorrow", "delivery_city": "Pune", "delivery_state": "MH", "division": "IT", "specifications": "steel", "remarks": "urgent"})


@pytest.mark.asyncio
async def test_whatsapp_history_and_api_response_variants(monkeypatch):
    service = wa_mod.WhatsAppService.__new__(wa_mod.WhatsAppService)
    service.mock_mode = True
    redis_session = FakeRedis()
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    session = make_session()
    await service._track_message_in_history(session, "hello")
    await service._track_message_in_history("sid", "body", "interactive")
    await service._track_message_in_history(None, "ignored")
    redis_session.append_message_to_history.side_effect = RuntimeError("history")
    await service._track_message_in_history("sid", "ignored")

    for raw in ([{"mid": 1}], {"mid": 2}, {"status": "ok"}, {}, {"Error": "bad"}):
        status = 200 if "Error" not in raw else 500
        result = service._handle_api_response(SimpleNamespace(status_code=status, text="text", json=lambda raw=raw: raw))
        assert isinstance(result, MessageResponse)
    assert not service._handle_api_response(SimpleNamespace(status_code=500, text="bad", json=lambda: (_ for _ in ()).throw(ValueError("json")))).success
    assert not service._handle_api_response(SimpleNamespace(status_code=200, text=object(), json=lambda: (_ for _ in ()).throw(RuntimeError("json")))).success


@pytest.mark.asyncio
async def test_email_service_template_recipients_send_and_listing(monkeypatch, tmp_path):
    opts = settings(email_templates_path=str(tmp_path))
    api = SimpleNamespace(send_email=AsyncMock(return_value={"status": "Success"}))
    monkeypatch.setattr(email_mod, "get_settings", lambda: opts)
    monkeypatch.setattr(email_mod, "EmailServiceAPI", lambda: api)
    service = email_mod.EmailService()
    (tmp_path / "welcome.json").write_text(json.dumps({"scenario_id": "welcome", "to": "support@procucev.com,{email}", "cc": " {cc} ", "subject": "Hi {name}", "body": "Body {name}", "supports_buyer": True, "signature": "Sig"}), encoding="utf-8")
    assert service.get_template_info("welcome")["scenario_id"] == "welcome"
    assert service.get_template_info("welcome") is service.get_template_info("welcome")
    processed = service._process_template(service.get_template_info("welcome"), {"email": "a@test", "cc": "c@test", "name": "Ada"}, "buyer")
    assert processed["to"] == ["support@example.com", "a@test"] and processed["cc"] == ["c@test"]
    assert service._process_recipients("", {}) == []
    assert service._substitute_variables("{missing}", {}) == "{missing}"
    assert service._substitute_variables("{x}", {"x": object()}).startswith("<")
    result = await service.send_email_by_template("welcome", {"email": "a@test", "cc": "c@test", "name": "Ada"}, attachments=[{"name": "a"}])
    assert result["status"] == "Success"
    assert await service.send_support_email("welcome", {"email": "a@test", "cc": "", "name": "A"})
    assert (await service.send_email_by_template("missing", {}))["statusCode"] == "404"
    (tmp_path / "empty.json").write_text(json.dumps({"subject": "x", "body": "x"}), encoding="utf-8")
    assert (await service.send_email_by_template("empty", {}))["statusCode"] == "400"
    api.send_email.side_effect = RuntimeError("mail")
    assert (await service.send_email_by_template("welcome", {"email": "a", "name": "A"}))["statusCode"] == "500"
    assert any(item["name"] == "welcome" for item in service.list_available_templates())
    monkeypatch.setattr(email_mod.os, "listdir", Mock(side_effect=OSError("list")))
    assert service.list_available_templates() == []
    (tmp_path / "bad.json").write_text("{bad", encoding="utf-8")
    assert service._load_template("bad") is None


@pytest.mark.asyncio
async def test_global_error_handler_notifications_database_details_and_singleton(monkeypatch):
    opts = settings(support_team_numbers=["1", "2"])
    wa = SimpleNamespace(send_message=AsyncMock(side_effect=[MessageResponse(True), MessageResponse(False, error="bad")]))
    email = SimpleNamespace(email_api=SimpleNamespace(send_email=AsyncMock(return_value={"status": "Success"})))
    monkeypatch.setattr(error_mod, "get_settings", lambda: opts)
    monkeypatch.setattr(error_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(error_mod, "EmailService", lambda: email)
    handler = error_mod.GlobalErrorHandler()
    context = error_mod.ErrorContext("Database Error", "broken", user_phone="1", user_name="Ada", user_email="a@test", current_flow="rfq", api_name="db", endpoint="/x", payload={"x": "y"}, timestamp=datetime.now(timezone.utc))
    formatted = handler._format_support_message(context)
    assert "Database Mode" in formatted and "****" in formatted and "User Name" in formatted
    assert handler._mask_database_url("mysql+pymysql://u:pw@host/db").endswith("@host/db")
    assert handler._get_database_details()
    assert await handler._send_whatsapp_notification("m") is True
    # All WhatsApp recipients failing causes email fallback.
    wa.send_message.side_effect = RuntimeError("wa")
    assert await handler._send_whatsapp_notification("m") is False
    await handler._send_email_notification(context)
    email.email_api.send_email.side_effect = RuntimeError("email")
    await handler._send_email_notification(context)
    handler.notification_enabled = True
    handler._send_user_response = AsyncMock()
    handler._notify_support_team = AsyncMock()
    assert await handler.handle_error(error_mod.ErrorContext("Server Error", "x", user_phone="1"))
    handler._notify_support_team.side_effect = RuntimeError("notify")
    assert not await handler.handle_error(error_mod.ErrorContext("Server Error", "x"))
    opts.database_mode = "client"
    opts.local_database_url = None
    opts.client_database_url = "mysql+pymysql://c:pw@host/db"
    assert "Client DB URL" in handler._get_database_details()
    opts.database_mode = "other"
    opts.enable_remote_categorization = False
    assert "Database Mode" in handler._get_database_details()
    error_mod._global_error_handler = None
    monkeypatch.setattr(error_mod, "GlobalErrorHandler", lambda: handler)
    assert error_mod.get_global_error_handler() is handler
    assert error_mod.get_global_error_handler() is handler
    await error_mod.handle_api_error("api", "/x", "bad", {"a": 1}, "1", "A", "a@test", "flow")
    await error_mod.handle_database_error("db")
    await error_mod.handle_server_error("server")


@pytest.mark.asyncio
async def test_session_management_context_creation_save_and_completion(monkeypatch):
    redis = FakeRedis()
    db = FakeDB()
    wa = SimpleNamespace(send_message=AsyncMock())
    summaries = SimpleNamespace(generate_session_summary=AsyncMock(), generate_daily_summary=AsyncMock())
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    opts = settings(redis_session_storage_enabled=True)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    monkeypatch.setattr("app.config.get_settings", lambda: opts)
    service = session_mod.SessionManagementService(db, wa, summaries, daily)
    assert (await service.get_or_create_user("1")).phone_number == "1"
    assert await service.handle_session_expiry_check("1", make_session())

    welcome = SimpleNamespace(check_and_send_welcome=AsyncMock())
    monkeypatch.setattr("app.services.welcome_message_service.get_welcome_service", lambda: welcome)
    redis.get_session.return_value = None
    created = await service.get_conversation_context("1")
    assert created.session_id and welcome.check_and_send_welcome.await_count == 1
    redis.get_session.return_value = service._session_to_dict(make_session(session_id="sid2"))
    redis.session_exists.return_value = True
    found = await service.get_conversation_context("1")
    assert found.session_id == "sid2"

    # Completed Redis session reset, DB-only fallback reset, and creation.
    terminal_data = service._session_to_dict(make_session(outcome=ConversationOutcome.abandoned, workflow_state={"old": True}))
    redis.get_session.return_value = terminal_data
    redis.session_exists.return_value = False
    await service.get_conversation_context("1")
    service.redis_enabled = False
    db.get_conversation_session = Mock(return_value=make_session(outcome=ConversationOutcome.completed))
    db.save_conversation_session = Mock(return_value=make_session(workflow_type=None))
    await service.get_conversation_context("1")
    db.get_conversation_session.return_value = make_session(workflow_type=WorkflowType.rfq_creation, outcome=None)
    assert await service.get_conversation_context("1")
    service.redis_enabled = True

    # create_session existing terminal/nonterminal and fresh session.
    db.get_conversation_session = Mock(return_value=make_session(outcome=ConversationOutcome.timeout))
    await service.create_session("1", "rfq_creation", "buyer")
    db.get_conversation_session.return_value = make_session(outcome=None)
    assert await service.create_session("1")
    db.get_conversation_session.return_value = None
    db.save_conversation_session.return_value = make_session(workflow_type="rfq_creation")
    assert await service.create_session("1", "rfq_creation", "seller")

    # send-and-track success, tracking failure with retry, and double send failure.
    service.add_message_to_history = Mock()
    await service.send_and_track_message("1", "hello", make_session())
    service.add_message_to_history.side_effect = RuntimeError("track")
    await service.send_and_track_message("1", "again", make_session())
    wa.send_message.side_effect = [RuntimeError("first"), RuntimeError("second")]
    with pytest.raises(RuntimeError):
        await service.send_and_track_message("1", "fail", make_session())

    # save_session variants and prevention of terminal Redis writes.
    service.redis_enabled = True
    redis.store_session.side_effect = None
    value = make_session(workflow_state={"pending_optional_rfq": {"x": 1}})
    db.save_conversation_session.side_effect = lambda data: value
    assert await service.save_session(value, WorkflowType.rfq_creation) is value
    db.save_conversation_session.side_effect = lambda data: service._dict_to_session(data)
    assert await service.save_session(make_session(), "not-a-workflow")
    assert await service.save_session(make_session(), object())
    terminal = make_session(outcome=ConversationOutcome.abandoned)
    await service.save_session(terminal, WorkflowType.rfq_creation)
    exited = make_session(workflow_state={"exit_completed": True})
    await service.save_session(exited, WorkflowType.rfq_creation)
    redis.store_session.side_effect = RuntimeError("redis")
    failed = make_session()
    assert await service.save_session(failed, WorkflowType.rfq_creation) is failed
    redis.store_session.side_effect = None
    service.redis_enabled = False
    db.save_conversation_session.return_value = make_session()
    assert await service.save_session(make_session(), persist_to_db=False)

    # Enhanced completion success and fallback, serialization helpers, and persistence predicate.
    service.redis_enabled = True
    monkeypatch.setattr(session_mod.SummarizationHelpers, "extract_rich_entities_for_summary", Mock(return_value={"rich": True}))
    monkeypatch.setattr(session_mod.SummarizationHelpers, "prepare_enhanced_summary_data", Mock(return_value={"x": 1}))
    completion = AsyncMock()
    monkeypatch.setattr(session_mod.SummarizationHelpers, "handle_session_completion_async", completion)
    created_tasks = []
    monkeypatch.setattr(session_mod.asyncio, "create_task", lambda coro: created_tasks.append(coro) or SimpleNamespace())
    await service.handle_session_completion_enhanced(make_session())
    for coro in created_tasks:
        coro.close()
    monkeypatch.setattr(session_mod.SummarizationHelpers, "extract_rich_entities_for_summary", Mock(side_effect=RuntimeError("rich")))
    service._handle_session_completion_fallback = AsyncMock()
    await service.handle_session_completion_enhanced(make_session())
    await service._handle_session_completion_enhanced(make_session())
    summaries.generate_session_summary.side_effect = RuntimeError("summary")
    await service._handle_session_completion_fallback(make_session())
    wa.send_message.side_effect = RuntimeError("placeholder")
    await service._show_auth_placeholder("1")

    circular = {}; circular["self"] = circular
    assert service._clean_for_json_serialization(circular)["self"] == "<circular_reference>"
    assert service._clean_for_json_serialization(None) is None
    assert service._clean_for_json_serialization(SimpleNamespace(x=1))
    clean = service._session_to_dict(make_session(outcome=ConversationOutcome.completed, completed_at=datetime(2025, 1, 2)))
    assert clean["outcome"] == "completed"
    restored = service._dict_to_session({"session_id": "x", "external_user_id": "1", "workflow_type": "bad", "outcome": "bad"})
    assert restored.session_id == "x"
    assert not service._should_persist_abandoned(make_session(conversation_history={}))
    assert service._should_persist_abandoned(make_session(conversation_history={"openai_messages": [1, 2, 3]}))


@pytest.mark.asyncio
async def test_media_downloader_singleton_and_webhook_errors(monkeypatch, tmp_path):
    opts = settings()
    monkeypatch.setattr(media_mod, "get_settings", lambda: opts)
    monkeypatch.setattr(media_mod.Path, "mkdir", Mock())
    media_mod._media_downloader = None
    downloader = media_mod.MediaDownloaderService()
    downloader.download_dir = tmp_path
    response_obj = SimpleNamespace(status_code=200, headers={"content-type": "text/plain"}, content=b"data")
    monkeypatch.setattr(media_mod.requests, "get", Mock(return_value=response_obj))
    monkeypatch.setattr(builtins, "open", mock_open())
    assert (await downloader.download_media("m"))["success"]
    assert media_mod.get_media_downloader() is not None
    assert (await downloader.download_from_webhook_content({}))["error"] == "No media ID found in content"
    downloader.download_media = AsyncMock(side_effect=RuntimeError("download"))
    assert "Webhook download error" in (await downloader.download_from_webhook_content({"id": "m"}))["error"]


@pytest.mark.asyncio
async def test_session_management_comprehensive_paths(monkeypatch):
    """Test uncovered branches of SessionManagementService."""
    db = SimpleNamespace(
        save_conversation_session=Mock(side_effect=lambda data: make_session(state=data.get("workflow_state", {}))),
        get_conversation_session=Mock(return_value=None),
        session=SimpleNamespace(query=Mock(return_value=SimpleNamespace(filter=Mock(return_value=SimpleNamespace(order_by=Mock(return_value=SimpleNamespace(first=Mock(return_value=SimpleNamespace(user_type=SimpleNamespace(value="buyer"))))))))))
    )
    wa = SimpleNamespace(send_message=AsyncMock())
    redis_sess = SimpleNamespace(
        store_session=AsyncMock(),
        get_session=AsyncMock(return_value=None),
        delete_session=AsyncMock(),
        session_exists=AsyncMock(return_value=False),
        set_user_active_session_id=AsyncMock(),
        get_user_active_session_id=AsyncMock(return_value=None),
        delete_user_active_session_id=AsyncMock(),
        refresh_ttl=AsyncMock(),
        ttl=AsyncMock(return_value=300),
    )

    summaries = SimpleNamespace(generate_session_summary=AsyncMock(), generate_daily_summary=AsyncMock())
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    service = session_mod.SessionManagementService(db_manager=db, whatsapp_service=wa, chat_summary_service=summaries, daily_summary_service=daily)
    service.redis_session = redis_sess
    service.redis_enabled = True

    # 1. get_conversation_context for new user with past buyer history
    s_new = await service.get_conversation_context("+919999999999")
    assert s_new is not None

    # 2. get_conversation_context with ended session in DB when redis is disabled
    service.redis_enabled = False
    ended_db_session = make_session(state={"x": 1})
    ended_db_session.outcome = ConversationOutcome.completed
    ended_db_session.completed_at = datetime(2026, 1, 1)
    db.get_conversation_session.return_value = ended_db_session
    s_reset = await service.get_conversation_context("+919999999999")
    assert s_reset is not None

    # 3. save_session with persist_to_db = True
    s_to_save = make_session(state={"test": "val"})
    saved = await service.save_session(s_to_save, persist_to_db=True)
    assert saved is not None

    # 4. save_session when DB throws error
    db.save_conversation_session.side_effect = RuntimeError("db crash")
    saved_fallback = await service.save_session(s_to_save, persist_to_db=True)
    assert saved_fallback is not None


@pytest.mark.asyncio
async def test_session_management_all_residual_branches():
    """Test residual branches in SessionManagementService."""
    db = MagicMock()
    wa = MagicMock()
    summaries = SimpleNamespace(generate_session_summary=AsyncMock(), generate_daily_summary=AsyncMock())
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    service = session_mod.SessionManagementService(db_manager=db, whatsapp_service=wa, chat_summary_service=summaries, daily_summary_service=daily)
    service.welcome_message_service = MagicMock(check_and_send_welcome_message=AsyncMock())

    # 1. Redis lookup throws Exception on get_user_active_session_id and None on get_session
    service.redis_enabled = True
    service.redis_session = MagicMock()
    service.redis_session.get_user_active_session_id = AsyncMock(side_effect=RuntimeError("redis down"))
    service.redis_session.get_session = AsyncMock(return_value=None)
    service.redis_session.store_session = AsyncMock()
    service.redis_session.set_user_active_session_id = AsyncMock()
    service.redis_session.exists = AsyncMock(return_value=True)
    db.get_conversation_session.return_value = None
    s1 = await service.get_conversation_context("+919999999999")
    assert s1 is not None

    # 2. Redis active session found
    sess_dict = {
        "session_id": "s_active",
        "external_user_id": "919999999999",
        "user_type": "buyer",
        "outcome": None,
        "workflow_state": {},
        "conversation_history": {"messages": []},
        "extracted_entities": {}
    }
    service.redis_session.get_user_active_session_id = AsyncMock(return_value="s_active")
    service.redis_session.get_session = AsyncMock(return_value=sess_dict)
    service.redis_session.refresh_ttl = AsyncMock()
    service.redis_session.set_user_active_session_id = AsyncMock()
    service.redis_session.store_session = AsyncMock()
    s2 = await service.get_conversation_context("+919999999999")
    assert s2 is not None
    assert s2.session_id == "s_active"

    # 3. Redis ended session found
    sess_ended = dict(sess_dict, outcome="completed")
    service.redis_session.get_session = AsyncMock(return_value=sess_ended)
    service.redis_session.clear_user_active_session_id = AsyncMock()
    service.redis_session.store_session = AsyncMock()
    s3 = await service.get_conversation_context("+919999999999")
    assert s3 is not None

    # 4. Invalid license raises exception
    service._validate_license = MagicMock(return_value=(False, "License invalid"))
    with pytest.raises(Exception):
        await service.get_conversation_context("+919999999999")

    # 5. History management
    service._validate_license = MagicMock(return_value=(True, "OK"))
    sess_test = ConversationSession(session_id="s_test", external_user_id="919999999999")
    service.add_message_to_history(sess_test, "user", "Hello", "text", "greeting", 90)
    service.add_message_to_history(sess_test, "assistant", "Hi there", "text")
    assert "messages" in sess_test.conversation_history
    assert len(sess_test.conversation_history["messages"]) == 2

    # 6. Save session
    service.db_manager.save_session = AsyncMock()
    await service.save_session(sess_test)
    assert service.redis_session.store_session.called



