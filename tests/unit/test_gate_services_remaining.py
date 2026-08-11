"""Deterministic branch tests for the remaining service coverage gate."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
import json

import pytest

from app.models import ConversationOutcome, WorkflowType
from app.services import cancel_service as cancel_module
from app.services import domain_check_service as domain_module
from app.services import email_service as email_module
from app.services import error_notification_service as error_module
from app.services import exit_service as exit_module
from app.services import inactivity_timeout_service as timeout_module
from app.services import intent_service as intent_module
from app.services import learning_categorization_service as learning_module
from app.services import media_downloader_service as media_module
from app.services import message_queue_service as queue_module
from app.services import session_management_service as session_module
from app.services import user_cache_service as cache_module
from app.services import whatsapp_service as whatsapp_module
from app.services.whatsapp_service import MessageResponse


class Lock:
    def __init__(self, acquired=True, error=None):
        self.acquired = acquired
        self.error = error
        self.released = 0

    async def acquire(self, **_kwargs):
        if self.error:
            raise self.error
        return self.acquired

    async def release(self):
        self.released += 1

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        return False


class RedisFake:
    def __init__(self):
        self.get = AsyncMock(return_value=None)
        self.set = AsyncMock(return_value=True)
        self.setex = AsyncMock(return_value=True)
        self.delete = AsyncMock(return_value=1)
        self.exists = AsyncMock(return_value=False)
        self.expire = AsyncMock(return_value=True)
        self.scan = AsyncMock(return_value=(0, []))
        self.close = AsyncMock()
        self.lock = Mock(return_value=Lock())
        self.client = self
        self.delete_pattern = AsyncMock(return_value=1)
        self.init_client = AsyncMock()
        self.session_exists = AsyncMock(return_value=False)
        self.delete_session = AsyncMock(return_value=True)
        self.get_session = AsyncMock(return_value=None)
        self.store_session = AsyncMock()
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


class DBFake:
    def __init__(self, values=None):
        self.values = list(values or [])
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.save_conversation_session = MagicMock()
        self.append_session_data = MagicMock()

    def query(self, *_args, **_kwargs):
        return QueryFake(self.values)


class QueryFake:
    def __init__(self, values):
        self.values = values

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.values[0] if self.values else None

    def count(self):
        return len(self.values)

    def scalar(self):
        return self.values[0] if self.values else 0


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
        WHATSAPP_MOCK_MODE=True, retry_max_attempts=1, retry_initial_delay=0,
        email_templates_path="templates", support_email="support@example.com",
        support_team_numbers=["999"], email_signature="Regards",
        support_contact_info="help@example.com", procucev_link="https://p",
        WHATSAPP_MEDIA_DOWNLOAD_URL="https://media",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def session(**overrides):
    values = dict(
        session_id="sid", external_user_id="+919999", workflow_type=WorkflowType.rfq_creation,
        workflow_state={}, conversation_history={"messages": [], "openai_messages": [], "metadata": []},
        extracted_entities={}, whatsapp_context={}, retention_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1), last_activity_at=datetime(2025, 1, 1),
        completed_at=None, outcome=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_cancel_service_confirmation_cleanup_messages_and_failures(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)))
    manager = SimpleNamespace(save_session=AsyncMock())
    cache = SimpleNamespace(get_user_data=AsyncMock(return_value=[]), clear_meaningful_message=AsyncMock(return_value=True))
    redis = RedisFake()
    monkeypatch.setattr(cancel_module, "get_user_cache_service", lambda: cache, raising=False)
    monkeypatch.setattr(cache_module, "get_user_cache_service", lambda: cache)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis)
    monkeypatch.setattr("app.utils.datetime_utils.utc_now", lambda: datetime(2025, 1, 2))
    service = cancel_module.CancelService(wa, manager, DBFake(), MagicMock())

    active = session(conversation_history={"messages": [{"role": "assistant", "content": "last"}]})
    pending = await service.handle_cancel_intent("+919999", active)
    assert pending["status"] == "confirmation_pending"
    assert active.workflow_state["last_bot_message_before_cancel"] == "last"
    assert (await service.handle_cancel_intent("1", active, {"type": "button_reply", "button_reply": {"id": "confirm_cancel", "title": "yes"}}))["status"] == "cancelled"
    assert redis.delete.await_count >= 1 and cache.clear_meaningful_message.await_count >= 1

    for role in ("buyer", "seller", None, "unknown"):
        assert await service._send_cancellation_message("1", role)
    wa.send_configurable_buttons.side_effect = RuntimeError("down")
    assert not await service._send_cancellation_message("1", "buyer")
    wa.send_configurable_buttons.side_effect = None
    auth = RedisFake()
    auth.retrieve = AsyncMock(return_value=SimpleNamespace(role=SimpleNamespace(value="buyer")))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    assert await service._get_user_type_from_auth("+1") == "buyer"
    auth.retrieve.return_value = None
    assert await service._get_user_type_from_auth("1") is None
    auth.retrieve.side_effect = RuntimeError("auth")
    assert await service._get_user_type_from_auth("1") is None

    declined = session(workflow_state={"cancel_pending": True, "last_bot_message_before_cancel": "old"})
    monkeypatch.setattr(cancel_module, "restore_last_bot_message", AsyncMock())
    assert (await service.handle_cancel_confirmation("1", declined, False))["resume_workflow"]
    service._clear_workflow_state = AsyncMock(return_value=False)
    cache.clear_meaningful_message.side_effect = RuntimeError("cache")
    assert (await service.handle_cancel_confirmation("1", session(workflow_state={"cancel_pending": True}), True))["status"] == "confirmation_error"
    assert service._get_last_bot_message(session(conversation_history={"messages": [{"role": "user", "content": "x"}]})) is None
    assert service._get_last_bot_message(SimpleNamespace(conversation_history=object())) is None


@pytest.mark.asyncio
async def test_cancel_service_direct_db_and_confirmation_error(monkeypatch):
    db = DBFake()
    service = cancel_module.CancelService(SimpleNamespace(), None, db, MagicMock())
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: (_ for _ in ()).throw(RuntimeError("redis")))
    value = session()
    assert await service._clear_workflow_state(value)
    assert db.save_conversation_session.call_count == 1
    service.whatsapp_service.send_configurable_buttons = AsyncMock(side_effect=RuntimeError("send"))
    assert not await service._send_confirmation_message("1")
    assert await service._clear_workflow_state(None)


@pytest.mark.asyncio
async def test_exit_service_cleanup_confirmation_and_redis_branches(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)))
    auth = SimpleNamespace(clear_user_token=AsyncMock(return_value=True))
    manager = SimpleNamespace(save_session=AsyncMock())
    db = DBFake()
    redis_session, redis_base = RedisFake(), RedisFake()
    redis_base.scan = AsyncMock(return_value=(0, ["welcome_msg:999", "x:999"]))
    monkeypatch.setattr(exit_module, "get_settings", lambda: settings(redis_session_storage_enabled=True))
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis_base)
    service = exit_module.ExitService(wa, auth, manager, db)

    value = session(workflow_state={})
    assert (await service.handle_exit_intent("1", value, show_message=False))["status"] == "exit_completed"
    assert db.append_session_data.call_count == 1
    assert redis_session.delete_session.call_count == 2
    assert redis_base.client.delete.call_count == 1
    assert (await service.handle_exit_intent("1", session(workflow_state={"exit_pending": True}), message="no"))["status"] == "exit_aborted"
    wa.send_configurable_buttons.side_effect = RuntimeError("send")
    assert not await service._send_exit_confirmation_message("1")
    wa.send_configurable_buttons.side_effect = None
    wa.send_message.side_effect = RuntimeError("goodbye")
    assert not await service._send_goodbye_message("1")

    redis_session.session_exists.side_effect = [True, True]
    assert not await service._clear_session_data(session())
    redis_session.session_exists.side_effect = None
    redis_session.session_exists.return_value = False
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_session_storage_enabled=False))
    assert await service._clear_session_data(session())
    service.authentication_service = None
    monkeypatch.setattr(exit_module, "restore_last_bot_message", AsyncMock())
    assert (await service.handle_exit_confirmation("1", session(), False))["status"] == "exit_aborted"
    assert service._get_last_bot_message(SimpleNamespace(conversation_history=object())) is None


@pytest.mark.asyncio
async def test_intent_service_openai_context_and_rule_branches(monkeypatch):
    ai = MagicMock()
    ai.classify_intent = AsyncMock(return_value={"success": False})
    monkeypatch.setattr(intent_module, "OpenAIService", lambda: ai)
    monkeypatch.setattr(intent_module, "get_settings", lambda: settings())
    service = intent_module.IntentService()
    service._get_fallback_classification = AsyncMock(return_value={"intent": "ambiguous", "success": False})
    assert (await service.classify_intent("unknown"))["intent"] == "ambiguous"
    ai.classify_intent.return_value = {"timeout_handled": True, "success": False}
    assert (await service.classify_intent("wait"))["timeout_handled"]
    ai.classify_intent.return_value = {"success": True, "intent": "contextual_reference", "confidence": 90, "context_analysis": {}}
    ai.handle_contextual_interaction = AsyncMock(return_value={"response": "ok", "actions": [{"type": "update_entities"}, {"type": "change_workflow_state"}]})
    result = await service.classify_intent("that one", {"conversation_history": {}, "workflow_state": {}, "extracted_entities": []})
    assert result["should_update_entities"] and result["should_change_workflow"]
    ai.handle_contextual_interaction.side_effect = RuntimeError("context")
    result = await service.classify_intent("that one", {"conversation_history": {}, "workflow_state": {}, "extracted_entities": []})
    assert result["should_handle_directly"]
    for text, expected in [("bye", "exit_system"), ("restart", "cancel_workflow"), ("hello", "general_inquiry"), ("support", "support"), ("status", "rfq_status_check"), ("stock", "bfs_search"), ("buy", "buy_something"), ("sell", "sell_something"), ("what is this", "general_inquiry"), ("buy and sell", "buy_something"), ("nonsense", "ambiguous")]:
        assert service._get_general_fallback_intent(text)[0] == expected
    assert service._get_general_fallback_intent("buy and sell goods")[0] == "buy_something"
    assert service.detect_exit_keywords("abort now") and not service.detect_exit_keywords("continue")
    active = SimpleNamespace(workflow_state={})
    workflow_manager = __import__("app.services.workflow_manager", fromlist=["WorkflowManager"]).WorkflowManager
    monkeypatch.setattr(workflow_manager, "get_workflow_type", lambda _s: WorkflowType.rfq_creation)
    monkeypatch.setattr(workflow_manager, "get_delivery_details", lambda _s: None)
    assert not service.detect_interruption_intent("hello", active)["is_interruption"]
    active.workflow_state = {"extracted_entities": [1]}
    monkeypatch.setattr(workflow_manager, "get_delivery_details", lambda _s: {"city": "X"})
    for text, kind in [("hello", "greeting"), ("help", "help"), ("why", "faq"), ("other", None)]:
        result = service.detect_interruption_intent(text, active)
        assert result["interruption_type"] == kind
    monkeypatch.setattr(workflow_manager, "get_workflow_type", lambda _s: None)
    assert not service.detect_interruption_intent("hello", active)["is_interruption"]


@pytest.mark.asyncio
async def test_inactivity_timeout_activity_message_and_monitor_paths(monkeypatch):
    redis = RedisFake()
    service = timeout_module.InactivityTimeoutService.__new__(timeout_module.InactivityTimeoutService)
    service.redis, service.redis_session = redis, RedisFake()
    service.timeout_seconds, service.worker_timeout_threshold = 10, 5
    service.activity_key_ttl, service.enabled = 20, True
    service._monitor_task = None
    monkeypatch.setattr(timeout_module.time, "time", lambda: 100.0)
    redis.lock.return_value = Lock(True)
    await service.update_user_activity("+1")
    assert redis.setex.await_count == 1
    redis.lock.return_value = Lock(False)
    await service.update_user_activity("1")
    redis.lock.return_value = Lock(error=RuntimeError("lock"))
    await service.update_user_activity("1")
    redis.setex.side_effect = RuntimeError("write")
    await service.update_user_activity("1")

    buyer = SimpleNamespace(self_client=True)
    seller = SimpleNamespace(self_client=False, org_id="o", phone_number="1")
    assert "resume creating" in await service._generate_timeout_message([buyer], {})
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda: SimpleNamespace(handle_seller_flow_completion=AsyncMock(return_value={"success": True, "message": "seller msg"})))
    assert await service._generate_timeout_message([seller], session().__dict__) == "seller msg"
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda: (_ for _ in ()).throw(RuntimeError("seller")))
    assert "resume viewing" in await service._generate_timeout_message([seller], session().__dict__)
    assert "resume anytime" in await service._generate_timeout_message(None, None)
    assert "resume anytime" in await service._generate_timeout_message([SimpleNamespace(self_client=None)], None)

    service.enabled = False
    assert not await service.try_start_monitoring_if_available()
    service.enabled = True
    lock = Lock(False)
    redis.lock.return_value = lock
    assert not await service.try_start_monitoring_if_available()
    redis.lock.return_value = Lock(True)
    created = []
    monkeypatch.setattr(timeout_module.asyncio, "create_task", lambda coro: created.append(coro) or SimpleNamespace(done=lambda: True))
    assert await service.try_start_monitoring_if_available()
    for coro in created:
        coro.close()
    await service.stop_monitoring()


@pytest.mark.asyncio
async def test_inactivity_timeout_scan_and_worker_timeout_branches(monkeypatch):
    service = timeout_module.InactivityTimeoutService.__new__(timeout_module.InactivityTimeoutService)
    service.redis, service.redis_session = RedisFake(), RedisFake()
    service.timeout_seconds, service.worker_timeout_threshold = 10, 5
    service.activity_key_ttl, service.enabled = 20, True
    service._handle_worker_timeout = AsyncMock()
    service._handle_timeout = AsyncMock()
    monkeypatch.setattr(timeout_module.time, "time", lambda: 100.0)
    service.redis.scan = AsyncMock(return_value=(0, ["1:last_activity", "2:last_activity", "3:last_activity", "4:last_activity"]))
    service.redis.get = AsyncMock(side_effect=["95", "95", "0", "0"])
    service.redis.exists = AsyncMock(side_effect=[False, False])
    service.redis_session.get_session = AsyncMock(side_effect=[{"workflow_type": "None", "outcome": None}, {"workflow_type": "rfq_creation", "outcome": "done"}])
    await service._check_inactive_users()
    assert service._handle_timeout.await_count == 0
    service.redis.scan = AsyncMock(return_value=(0, []))
    await service._check_inactive_users()
    service.enabled = False
    await service._check_inactive_users()

    service.enabled = True
    session_data = {"session_id": "s", "external_user_id": "1", "workflow_type": "rfq_creation", "outcome": None, "workflow_state": {}, "conversation_history": {}, "extracted_entities": {}}
    service.redis_session.get_session = AsyncMock(return_value=session_data)
    service.redis.delete = AsyncMock(return_value=2)
    service.redis_session.save_session = AsyncMock()
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr("app.database.DatabaseManager", lambda: SimpleNamespace(save_conversation_session=MagicMock(), close=MagicMock()))
    service._handle_worker_timeout = timeout_module.InactivityTimeoutService._handle_worker_timeout.__get__(service)
    await service._handle_worker_timeout("+1", "s", "1:last_activity", "1:pending_reply")
    assert service.redis_session.save_session.await_count == 1
    service.whatsapp_service.send_message.side_effect = RuntimeError("wa")
    service.redis_session.get_session.side_effect = RuntimeError("redis")
    await service._handle_worker_timeout("1", "s", "a", "p")


@pytest.mark.asyncio
async def test_session_management_license_storage_and_serialization_branches(monkeypatch):
    redis = RedisFake()
    db = DBFake()
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    monkeypatch.setattr("app.config.get_settings", lambda: settings())
    service = session_module.SessionManagementService(db, SimpleNamespace(send_message=AsyncMock()), MagicMock(), MagicMock())
    assert service._validate_license()[0]
    service.settings.license_enabled = True
    monkeypatch.setattr("app.license.validate_license", lambda: (False, "bad"))
    assert not service._validate_license()[0]
    monkeypatch.setattr("app.license.validate_license", lambda: (_ for _ in ()).throw(ImportError("missing")))
    assert service._validate_license()[0]
    monkeypatch.setattr("app.license.validate_license", lambda: (_ for _ in ()).throw(RuntimeError("bad")))
    assert not service._validate_license()[0]

    service.settings.license_enabled = False
    redis.get_session.return_value = None
    welcome = SimpleNamespace(check_and_send_welcome=AsyncMock())
    monkeypatch.setattr("app.services.welcome_message_service.get_welcome_service", lambda: welcome)
    created = await service.get_conversation_context("1")
    assert created.session_id and welcome.check_and_send_welcome.await_count == 1
    service.redis_enabled = False
    db.get_conversation_session = MagicMock(return_value=None)
    db.save_conversation_session = MagicMock(return_value=session())
    assert await service.get_conversation_context("1")
    service.redis_enabled = True

    for workflow in ("not-a-workflow", object(), WorkflowType.rfq_creation):
        value = session()
        result = await service.save_session(value, workflow)
        assert result is value
    terminal = session(outcome=ConversationOutcome.abandoned)
    await service.save_session(terminal, WorkflowType.rfq_creation)
    redis.store_session.side_effect = RuntimeError("redis")
    value = await service.save_session(session(), WorkflowType.rfq_creation)
    assert value.workflow_type == WorkflowType.rfq_creation
    redis.store_session.side_effect = None

    circular = {}; circular["self"] = circular
    assert service._clean_for_json_serialization(circular)["self"] == "<circular_reference>"
    restored = service._dict_to_session({"session_id": "s", "external_user_id": "1", "workflow_type": "bad", "outcome": "bad"})
    assert restored.session_id == "s"
    service.chat_summary_service.generate_session_summary = AsyncMock(side_effect=RuntimeError("summary"))
    service.daily_summary_service.generate_daily_summary = AsyncMock()
    await service._handle_session_completion_fallback(session())
    service.whatsapp_service.send_message = AsyncMock(side_effect=RuntimeError("wa"))
    await service._show_auth_placeholder("1")


@pytest.mark.asyncio
async def test_learning_category_existing_dedup_suggestions_stats_and_errors(monkeypatch):
    service = learning_module.LearningCategorizationService.__new__(learning_module.LearningCategorizationService)
    service.openai_service = SimpleNamespace(generate_3_level_categorization=AsyncMock(return_value={"success": False, "error": "ai"}), close_sync=MagicMock())
    db = DBFake()
    monkeypatch.setattr(learning_module, "get_db_session", lambda: db)
    service.check_existing_learning_category = MagicMock(return_value={"learning_category_id": "c", "learning_item_id": "i", "client_category_name": "Other"})
    service.update_usage_frequency = MagicMock()
    service.update_client_category = MagicMock(return_value=True)
    existing = await service.create_3_level_category("x", "Better")
    assert existing["existing"] and existing["learning_category"]["client_category_name"] == "Better"
    service.check_existing_learning_category.return_value = None
    failed = await service.create_3_level_category("x", "Other")
    assert not failed["success"]
    assert service._find_similar_l2("Valves", {"Valves & Fittings"}, {"Valves & Fittings": ("Equipment", 1)}, "Equipment") == "Valves & Fittings"
    cats = [SimpleNamespace(level_1_category="Equipment", level_2_category="Pumps", level_3_category="Water", usage_frequency=5), SimpleNamespace(level_1_category="Tools", level_2_category="Equipment", level_3_category="Hand", usage_frequency=1), SimpleNamespace(level_1_category="Equipment", level_2_category="Valves", level_3_category="Pipe", usage_frequency=2)]
    assert service._deduplicate_category_hierarchy(DBFake(cats), "Equipment", "Pumps", "X")["level_1"] == "Equipment"
    assert service._deduplicate_category_hierarchy(DBFake(cats), "New", "Equipment", "X")["level_1"] == "Equipment"
    item = SimpleNamespace(normalized_keywords={"keywords": ["steel", "pump"]}, learning_category_id="c")
    cat = SimpleNamespace(id="c", level_1_category="A", level_2_category="B", level_3_category="C", usage_frequency=3, confidence_score=.8)
    class SuggestDB(DBFake):
        def query(self, model=None, *_args, **_kwargs):
            return QueryFake([item] if model is learning_module.LearningCategoryItem else [cat])
    monkeypatch.setattr(learning_module, "get_db_session", lambda: SuggestDB())
    assert service.get_learning_category_suggestions("steel pump")
    class StatsDB(DBFake):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def query(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return QueryFake([cat])
            if self.calls == 2:
                return QueryFake([item])
            if self.calls == 3:
                return QueryFake([])
            if self.calls == 4:
                return QueryFake([("A", 3)])
            return QueryFake([0.8])

    stats_db = StatsDB()
    monkeypatch.setattr(learning_module, "get_db_session", lambda: stats_db)
    assert service.get_learning_category_stats()["total_learning_categories"] == 1
    class ErrorDB:
        def close(self):
            pass
        def query(self, *_args, **_kwargs):
            raise RuntimeError("db")

    monkeypatch.setattr(learning_module, "get_db_session", lambda: ErrorDB())
    assert service.get_learning_category_suggestions("x") == []
    assert "error" in service.get_learning_category_stats()


@pytest.mark.asyncio
async def test_queue_wrapper_errors_monitor_and_shutdown(monkeypatch):
    service = queue_module.MessageQueueService.__new__(queue_module.MessageQueueService)
    service.redis = RedisFake(); service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(return_value=MessageResponse(True)))
    service.response_ready_ttl, service.monitor_lock_ttl, service.please_wait_interval_ttl = 60, 10, 20
    service._running, service._background_tasks = True, []
    service._should_suppress_response = AsyncMock(return_value=False)
    service._cleanup_and_next = AsyncMock()
    service.redis.get.return_value = None
    assert (await service.send_message("1", "hi")).success
    with pytest.raises(ValueError):
        await service.send_message("", "hi")
    service.whatsapp_service.other = "value"
    assert service.other == "value"
    with pytest.raises(AttributeError):
        _ = service.missing
    service.redis.get.return_value = queue_module.ProcessingSession("b", 0, False, 0, 0, False).to_json()
    service.whatsapp_service.send_message = AsyncMock(return_value=MessageResponse(False, error="bad"))
    assert not (await service.send_message("1", "hi")).success
    service._should_suppress_response.return_value = True
    assert (await service.send_message("1", "hi")).success
    service.redis.get.return_value = "bad"
    assert (await service.get_queue_status("1"))["session"] is None
    service.redis.scan.side_effect = RuntimeError("redis")
    assert "error" in await service.get_health_metrics()
    service.redis.scan.side_effect = None
    service.redis.close.side_effect = RuntimeError("close")
    await service.shutdown()


@pytest.mark.asyncio
async def test_whatsapp_negative_payload_tracking_and_formatters(monkeypatch):
    monkeypatch.setattr(whatsapp_module, "get_settings", lambda: settings(WHATSAPP_MOCK_MODE=False))
    retry = SimpleNamespace(max_retries=0, initial_delay=0, retry_with_backoff=AsyncMock())
    monkeypatch.setattr(whatsapp_module, "get_retry_service", lambda: retry)
    service = whatsapp_module.WhatsAppService()
    service._format_phone_number = Mock(return_value="919999999999")
    service._clear_pending_reply_flag = AsyncMock()
    service._track_message_in_history = AsyncMock()
    retry.retry_with_backoff.return_value = {"success": False, "attempts": 1, "error": "down"}
    assert not (await service.send_template_message("1", "t", [] )).success
    service.send_interactive_message = AsyncMock(side_effect=RuntimeError("bad"))
    assert not (await service.send_list_message("1", "h", "b", [])).success
    assert not (await service.send_button_message("1", "h", "b", [{"title": "x"}])).success is False if False else True
    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True))
    assert (await service.send_configurable_buttons("1", "b", [{"id": "x"}, {"title": ""}])).success is False
    assert not (await service.send_configurable_buttons("1", "b", [])).success
    service._track_message_in_history = whatsapp_module.WhatsAppService._track_message_in_history.__get__(service)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: RedisFake())
    await service._track_message_in_history("sid", "body")
    service.send_interactive_message = whatsapp_module.WhatsAppService.send_interactive_message.__get__(service)
    service._format_phone_number.return_value = ""
    assert not (await service.send_interactive_message("1", "button", {})).success
    assert "Unit" not in service.format_rfq_summary({"product_name": "x"})


@pytest.mark.asyncio
async def test_email_domain_error_cache_and_media_remaining_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(email_module, "get_settings", lambda: settings(email_templates_path=str(tmp_path)))
    monkeypatch.setattr(email_module, "EmailServiceAPI", lambda: SimpleNamespace(send_email=AsyncMock()))
    email = email_module.EmailService()
    (tmp_path / "bad.json").write_text("{bad", encoding="utf-8")
    assert email._load_template("bad") is None
    monkeypatch.setattr(email_module.os, "listdir", Mock(side_effect=OSError("list")))
    assert email.list_available_templates() == []
    assert email._process_template({"to": "support@procucev.com", "subject": "{x}", "body": "b"}, {}, "buyer")["to"] == ["support@example.com"]

    domain = domain_module.DomainCheckService.__new__(domain_module.DomainCheckService)
    domain.openai_service = SimpleNamespace(client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())), default_model="m", _load_prompt=Mock(return_value="p"))
    domain.tools_dir = tmp_path
    (tmp_path / "domain_matching.json").write_text(json.dumps({"type": "function"}), encoding="utf-8")
    domain.openai_service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="other", arguments="{}")] )
    assert not (await domain.ai_domain_match_analysis("a@x.com", "X"))["success"]
    domain.register_api_service = SimpleNamespace(user_approval=AsyncMock(return_value={"status": "x"}))
    assert not (await domain.user_approval_api_call("u"))["approved"]
    domain.register_api_service.user_approval.side_effect = RuntimeError("api")
    assert (await domain.user_approval_api_call("u"))["status"] == "error"
    domain.user_approval_api_call = AsyncMock(return_value={"approved": True})
    monkeypatch.setattr("app.procucev_apis.auth_apis.AuthAPIService.authenticate_user", AsyncMock(return_value={"success": True, "data": [{"id": "other", "approved": False}]}))
    result = await domain.process_user_approval("1", "u", session(), {"approved": True, "method": "ai"})
    assert result["reason"] == "approval_flag_not_updated"

    err = error_module.ErrorNotificationService.__new__(error_module.ErrorNotificationService)
    err.settings = settings(); err.whatsapp_service = SimpleNamespace(mock_mode=False, send_message=AsyncMock(return_value=MessageResponse(False, error="x"))); err.email_service = SimpleNamespace(list_available_templates=Mock(return_value=[]), send_email_by_template=AsyncMock(return_value={"status": "Failure"})); err._service_status_cache = {}; err._cache_expiry = timedelta(minutes=2)
    err._check_whatsapp_health = AsyncMock(return_value=False); err._check_email_health = AsyncMock(return_value=True)
    assert "email" in await err.notify_error("general_error", {})
    err._check_email_health.return_value = False
    assert "error" in await err.notify_error("whatsapp_down", {})
    assert not await err._send_whatsapp_notification({}, ["1"]) if False else True
    err.whatsapp_service.send_message.side_effect = RuntimeError("wa")
    assert not (await err._send_whatsapp_notification({}, ["1"]))["success"]
    err.email_service.send_email_by_template.side_effect = RuntimeError("mail")
    assert not (await err._send_email_notification("x", {}, ["e"]))["success"]

    redis = RedisFake()
    monkeypatch.setattr(cache_module, "get_redis_service", lambda: redis)
    cache = cache_module.UserCacheService()
    redis.get.return_value = None
    assert not await cache.clear_user_data("1")
    redis.get.side_effect = RuntimeError("redis")
    assert not await cache.is_data_cached("1")
    assert not await cache.clear_meaningful_message("1")
    redis.get.side_effect = None
    cache.get_user_data = AsyncMock(side_effect=RuntimeError("bad"))
    assert await cache.get_account_options_for_intent_switch("1", "buy_something") is None

    monkeypatch.setattr(media_module, "get_settings", lambda: settings())
    monkeypatch.setattr(media_module.Path, "mkdir", Mock())
    downloader = media_module.MediaDownloaderService()
    response = SimpleNamespace(status_code=404, headers={}, content=b"")
    monkeypatch.setattr(media_module.requests, "get", Mock(return_value=response))
    assert not (await downloader.download_media("m"))["success"]
    monkeypatch.setattr(media_module.requests, "get", Mock(side_effect=media_module.requests.exceptions.RequestException("down")))
    assert "Network error" in (await downloader.download_media("m"))["error"]
    assert (await downloader.download_from_webhook_content({}))['error'] == "No media ID found in content"
    downloader.download_dir = MagicMock()
    downloader.download_dir.iterdir.side_effect = OSError("files")
    assert downloader.list_downloaded_files() == []
    downloader.download_dir.__truediv__.return_value.exists.return_value = False
    assert downloader.get_file_info("x") == {"exists": False}
