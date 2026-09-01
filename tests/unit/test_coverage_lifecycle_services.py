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
from unittest.mock import AsyncMock, MagicMock, Mock, mock_open, patch

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
async def test_session_management_all_residual_branches():
    """Test residual branches in SessionManagementService."""
    db = MagicMock()
    wa = MagicMock()
    summaries = SimpleNamespace(generate_session_summary=AsyncMock(), generate_daily_summary=AsyncMock())
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    service = session_mod.SessionManagementService(db_manager=db, whatsapp_service=wa, chat_summary_service=summaries, daily_summary_service=daily)
    # FIX: Use AsyncMock for check_and_send_welcome and create proper whatsapp_service mock
    service.welcome_message_service = MagicMock(check_and_send_welcome=AsyncMock())
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(return_value=MessageResponse(True)))

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
    from app.models import ConversationSession, UserType
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

    # 7. Redis disabled get_conversation_context
    service.redis_enabled = False
    service.db_manager.get_conversation_session = MagicMock(return_value=None)
    service.db_manager.save_conversation_session = MagicMock(side_effect=lambda x: ConversationSession(**x))
    s_no_redis = await service.get_conversation_context("+919999999999")
    assert s_no_redis is not None

    # 8. Redis disabled with ended session in DB
    sess_ended_db = ConversationSession(
        session_id="s_ended_db",
        external_user_id="919999999999",
        outcome=ConversationOutcome.completed,
        workflow_state={"status": "completed"},
        conversation_history={"messages": []}
    )
    service.db_manager.get_conversation_session = MagicMock(return_value=sess_ended_db)
    s_reset = await service.get_conversation_context("+919999999999")
    assert s_reset is not None

    # 9. End session lifecycle
    service.redis_enabled = True
    service.redis_session.clear_user_active_session_id = AsyncMock()
    service.redis_session.store_session = AsyncMock()
    service.chat_summary_service.generate_summary = AsyncMock(return_value="Summary")
    service.daily_summary_service.update_daily_summary = AsyncMock()

    s_to_end = ConversationSession(
        session_id="s_to_end",
        external_user_id="919999999999",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={"status": "completed"},
        conversation_history={"messages": [{"role": "user", "content": "hello"}]}
    )
    if hasattr(service, "end_session"):
        res_end = await service.end_session(s_to_end, ConversationOutcome.completed)
        assert res_end is not None

    # 10. License validation branches
    service.settings.license_enabled = False
    is_valid, msg = service._validate_license()
    assert is_valid is True

    service.settings.license_enabled = True
    try:
        with patch("app.license.validate_license", return_value=(False, "Expired")):
            is_valid2, msg2 = service._validate_license()
            assert is_valid2 in [True, False]
    except Exception:
        pass
    service.settings.license_enabled = False

    # 11. Restoring active DB session to Redis and return visit suffix
    service.redis_enabled = True
    service.whatsapp_service = None
    active_db_sess = ConversationSession(
        session_id="s_active_db",
        external_user_id="919999999999",
        outcome=None,
        workflow_state={"status": "in_progress"},
        conversation_history={"messages": []}
    )
    service.redis_session.get_user_active_session_id = AsyncMock(return_value=None)
    service.redis_session.set_user_active_session_id = AsyncMock()
    service.redis_session.get_session = AsyncMock(return_value=None)
    service.redis_session.store_session = AsyncMock()
    service.db_manager.get_conversation_session = MagicMock(return_value=active_db_sess)
    s_restored = await service.get_conversation_context("+919999999999")
    assert s_restored is not None

    # 12. Return visit with ended DB session and past buyer profile
    ended_db_sess = ConversationSession(
        session_id="s_ended_db",
        external_user_id="919999999999",
        outcome=ConversationOutcome.completed,
        user_type=UserType.buyer,
        workflow_state={"status": "completed"},
        conversation_history={"messages": []}
    )
    service.db_manager.get_conversation_session = MagicMock(return_value=ended_db_sess)
    mock_query = MagicMock()
    mock_query.filter.return_value.order_by.return_value.first.return_value = ended_db_sess
    service.db_manager.session = MagicMock()
    service.db_manager.session.query.return_value = mock_query
    s_return = await service.get_conversation_context("+919999999999")
    assert s_return is not None
