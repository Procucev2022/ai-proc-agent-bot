"""Deterministic unit coverage for the remaining service layer.

All integrations are replaced with in-memory mocks.  These tests intentionally
exercise service-level decisions and formatting rather than network clients.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_module
import app.services.profile_selection_service as profile_module
import app.services.seller_service as seller_module
import app.services.inactivity_timeout_service as timeout_module
import app.services.session_management_service as session_module
import app.services.learning_categorization_service as learning_module
import app.services.seller_notification_service as notification_module
import app.services.seller_recommendation_service as recommendation_module
import app.services.seller_categorization_service as seller_cat_module
import app.services.rfq_background_service as background_module
import app.services.rfq_intimation_service as intimation_module
import app.services.cancel_service as cancel_module
import app.services.exit_service as exit_module
import app.services.intent_service as intent_module
import app.services.email_service as email_module
from app.models import ConversationOutcome, RFQStatus, WorkflowType
from app.services.whatsapp_service import MessageResponse


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.store_session = AsyncMock()
        self.refresh_ttl = AsyncMock()
        self.get_session = AsyncMock(return_value=None)
        self.session_exists = AsyncMock(return_value=False)
        self.delete_session = AsyncMock(return_value=True)
        self.delete = AsyncMock()


class FakeQuery:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = values if values is not None else []

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self.value

    def all(self):
        return self.values

    def count(self):
        return self.value or 0

    def scalar(self):
        return self.value or 0

    def delete(self):
        return self.value or 0

    def group_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


class FakeDB:
    def __init__(self, query=None):
        self.query_result = query or FakeQuery()
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.append_session_data = MagicMock()

    def query(self, *args, **kwargs):
        return self.query_result

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None


def settings(**overrides):
    base = dict(
        redis_session_storage_enabled=True,
        license_enabled=False,
        workflow_timeout_enabled=True,
        workflow_timeout_seconds=300,
        timeout_poll_interval_seconds=1,
        activity_key_ttl_seconds=420,
        worker_timeout_threshold_seconds=135,
        pending_reply_ttl_seconds=180,
        redis_url="redis://localhost:6379/0",
        enable_remote_categorization=False,
        WHATSAPP_TEMPLATE_RFQ_NOTIFICATION="rfq-template",
        WHATSAPP_TEMPLATE_BFS_BID_NOTIFICATION="bfs-template",
        PROCUCEV_PORTAL_URL="https://portal.example",
        support_email="support@example.com",
        support_contact_info="support@example.com",
        email_signature="Regards",
        email_templates_path="templates",
        procucev_link="https://procucev.example",
        procucev_rfq_details_url="https://portal.example/rfqs",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def session_obj(**overrides):
    value = dict(
        session_id="sid",
        external_user_id="919999999999",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={},
        conversation_history={"messages": [], "openai_messages": []},
        extracted_entities={},
        whatsapp_context={},
        retention_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1),
        last_activity_at=datetime(2025, 1, 1),
        completed_at=None,
        outcome=None,
    )
    value.update(overrides)
    return SimpleNamespace(**value)


# ChatService and profile selection ---------------------------------------

def test_chat_constructor_lazy_properties_cleanup_and_activation(monkeypatch):
    queue = MagicMock()
    summary = MagicMock()
    daily = MagicMock()
    db_manager = MagicMock()
    vendor = MagicMock()
    rfq = MagicMock()
    background = MagicMock()
    session_manager = MagicMock()
    monkeypatch.setattr(chat_module, "ChatSummaryService", lambda **_: summary)
    monkeypatch.setattr(chat_module, "DailySummaryService", lambda: daily)
    monkeypatch.setattr(chat_module, "DatabaseManager", lambda **_: db_manager)
    monkeypatch.setattr(chat_module, "VendorService", lambda **_: vendor)
    monkeypatch.setattr(chat_module, "RFQService", lambda: rfq)
    monkeypatch.setattr(chat_module, "RFQBackgroundService", lambda **_: background)
    monkeypatch.setattr(chat_module, "SessionManagementService", lambda *args: session_manager)
    monkeypatch.setattr(chat_module, "get_settings", lambda: settings())

    service = chat_module.ChatService(db_session="db", message_queue_service=queue)
    assert service.whatsapp_service is queue
    assert service.db_manager is db_manager
    assert service.intent_service is service.intent_service
    assert service.entity_service is service.entity_service

    service._openai_service = AsyncMock()
    assert service.response_helpers is service.response_helpers
    awaitable = service.cleanup()
    import asyncio
    asyncio.run(awaitable)
    service._openai_service.close.assert_awaited_once()

    wf = SimpleNamespace(workflow_state={})
    user = SimpleNamespace(phone_number="9199")
    session_manager.save_session = AsyncMock()
    queue.send_message = AsyncMock()
    monkeypatch.setattr(chat_module.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(chat_module.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    monkeypatch.setattr(chat_module.WorkflowManager, "set_workflow_type", MagicMock())
    import asyncio
    assert asyncio.run(service._activate_sectioned_rfq(user, wf, "hello"))["status"] == "sectioned_rfq_activated"
    queue.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_selection_cache_conversion_and_response_branches(monkeypatch):
    cache = MagicMock()
    cache.get_user_data = AsyncMock(return_value=[])
    wa = MagicMock()
    auth = MagicMock()
    auth.user_authenticate = AsyncMock(return_value={"success": False})
    monkeypatch.setattr(profile_module, "get_user_cache_service", lambda: cache)
    service = profile_module.ProfileSelectionService(wa, auth)
    assert service.user_selection_tool is None
    assert await service._get_user_profiles("1", "hi", session_obj()) == {"success": False, "message": "No profiles found"}

    fake_user = SimpleNamespace(email="b@example.com", role=SimpleNamespace(value="buyer"), name="Buyer", company_name="Co")
    monkeypatch.setattr(profile_module.User, "from_api_response", lambda _: fake_user)
    profiles = service._convert_api_data_to_profiles([{"fullName": "Ada Lovelace"}, {"bad": True}])
    assert profiles[0]["role"] == "buyer"
    assert service._extract_user_name(profiles) == "Ada"

    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": profiles})
    service._handle_neutral_greeting = AsyncMock(return_value={"status": "neutral"})
    result = await service.handle_profile_selection("1", "hello", session_obj(), {"intent": "greeting", "confidence": 100})
    assert result == {"status": "neutral"}
    service._detect_registration_intent = AsyncMock(return_value=None)
    result = await service.handle_profile_selection("1", "anything", session_obj(), {"intent": "general_inquiry", "confidence": 90})
    assert result == {"status": "general_inquiry_already_handled"}

    session = session_obj(workflow_state={"profile_options": profiles})
    service._parse_profile_selection = AsyncMock(return_value=profiles[0])
    service._process_selected_profile = AsyncMock(return_value={"status": "selected"})
    assert await service.handle_profile_selection_response("1", "1", session) == {"status": "selected"}
    service._parse_profile_selection = AsyncMock(return_value=None)
    service._show_profile_selection_retry = AsyncMock(return_value={"status": "retry"})
    assert await service.handle_profile_selection_response("1", "bad", session) == {"status": "retry"}


@pytest.mark.asyncio
async def test_profile_selection_parsers_cover_numeric_email_and_invalid(monkeypatch):
    service = object.__new__(profile_module.ProfileSelectionService)
    options = [
        {"number": 1, "email": "buyer@example.com", "role": "buyer", "name": "Buyer"},
        {"number": 2, "email": "seller@example.com", "role": "seller", "name": "Seller"},
    ]
    assert (await service._simple_parse_profile_selection("1", options)) == options[0]
    assert (await service._simple_parse_profile_selection("2", options)) == options[1]
    assert service._fuzzy_email_match("buyer@example.com", "buyer@example.com")["confidence"] > 0
    assert service._fuzzy_email_match("other@example.com", "buyer@example.com")["confidence"] == 0
    assert service._string_similarity("abc", "abc") == 1.0


# Seller and session management -------------------------------------------

def seller_service_fixture(monkeypatch):
    wa = MagicMock()
    manager = MagicMock()
    db = MagicMock()
    api = MagicMock()
    monkeypatch.setattr(seller_module, "DatabaseManager", lambda **_: db)
    monkeypatch.setattr(seller_module, "SellerAPIService", lambda: api)
    monkeypatch.setattr(seller_module, "SessionManagementService", lambda *args, **kwargs: manager)
    monkeypatch.setattr(seller_module, "ChatSummaryService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_module, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "RFQStatusService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_module, "ResponseHelpers", lambda _: MagicMock())
    monkeypatch.setattr(seller_module, "get_settings", lambda: settings())
    return seller_module.SellerService(whatsapp_service=wa, session_manager=manager, db_session=db), wa, api, manager


def test_seller_formatting_fallback_and_parsers(monkeypatch):
    service, _, _, _ = seller_service_fixture(monkeypatch)
    display = service._generate_hardcoded_rfq_display(
        [{"rfq_id": "R1", "title": "A", "description": "x" * 300}], 1, 3
    )
    assert "R1" in display and "3" in display
    assert service._fallback_intent_classification("show my rfqs", {})["intent"] == "rfq_access_request"
    assert service._extract_sequence_numbers("1, 3 and 2") == [1, 3, 2]
    bot = {"content": "1. RFQ123456789012\n2. RFQ999999999999"}
    assert service._map_sequence_to_rfq_ids([2], bot) == ["RFQ999999999999"]
    assert service._is_view_available_rfq_request("show available rfqs")
    assert not service._is_view_available_rfq_request("hello")
    assert service._analyze_email_errors([{"success": True}, {"success": False, "error": "not found"}, {"success": False, "error": "timeout"}])["total_failed"] == 2


@pytest.mark.asyncio
async def test_seller_workflow_completion_and_error_paths(monkeypatch):
    service, wa, api, manager = seller_service_fixture(monkeypatch)
    user = SimpleNamespace(phone_number="1", id="seller", org_id="org", role="seller")
    session = session_obj(workflow_state={})
    service._handle_initial_seller_flow = AsyncMock(return_value={"status": "initial"})
    service._display_rfqs_to_seller = AsyncMock(return_value={"status": "initial"})
    assert await service.handle_seller_workflow(user, session, "hello") == {"status": "initial"}
    service._fetch_seller_open_rfqs_for_reminder = AsyncMock(return_value={"success": False})
    service._send_generic_closing_message = AsyncMock(return_value={"status": "closed"})
    assert await service.handle_seller_flow_completion(user, session) == {"status": "closed"}
    service._fetch_seller_open_rfqs_for_reminder = AsyncMock(return_value={"success": True, "rfqs": [{"id": "r"}]})
    service._send_standard_closing_message = AsyncMock(return_value={"status": "reminder"})
    assert await service.handle_seller_flow_completion(user, session) == {"status": "reminder"}


def session_service_fixture(monkeypatch, redis_enabled=True):
    redis = FakeRedis()
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_session_storage_enabled=redis_enabled))
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    service = session_module.SessionManagementService(FakeDB(), MagicMock(), MagicMock(), MagicMock())
    return service, redis


@pytest.mark.asyncio
async def test_session_management_serialization_save_and_send(monkeypatch):
    service, redis = session_service_fixture(monkeypatch)
    assert (await service.get_or_create_user("123")).phone_number == "123"
    session = session_obj(workflow_state={"when": date(2025, 1, 2), "nested": object()})
    data = service._session_to_dict(session)
    assert data["workflow_type"] == "rfq_creation"
    restored = service._dict_to_session(data)
    assert restored.session_id == "sid"
    assert service._clean_for_json_serialization({"x": date(2025, 1, 1)})["x"] == "2025-01-01"

    service.whatsapp_service.send_message = AsyncMock()
    service.add_message_to_history = MagicMock()
    await service.send_and_track_message("1", "hello", session)
    service.whatsapp_service.send_message.assert_awaited_once()
    service.db_manager.save_conversation_session = MagicMock(return_value=session)
    await service.save_session(session, "rfq_creation", persist_to_db=True)
    redis.store_session.assert_awaited()
    service.whatsapp_service.send_message.side_effect = RuntimeError("down")
    service.add_message_to_history.side_effect = RuntimeError("tracking")
    with pytest.raises(RuntimeError):
        await service.send_and_track_message("1", "again", session)


@pytest.mark.asyncio
async def test_session_context_cache_and_completed_session_reset(monkeypatch):
    service, redis = session_service_fixture(monkeypatch)
    cached = service._session_to_dict(session_obj())
    redis.get_session.return_value = cached
    result = await service.get_conversation_context("1")
    assert result.session_id == "sid"
    redis.refresh_ttl.assert_awaited_once()
    assert await service.handle_session_expiry_check("1", result) is result
    assert service._should_persist_abandoned(session_obj(conversation_history={"openai_messages": [1, 2]})) is False
    assert service._should_persist_abandoned(session_obj(conversation_history={"openai_messages": [1, 2, 3]})) is True


# Learning and vector categorization --------------------------------------

def learning_fixture(monkeypatch):
    monkeypatch.setattr(learning_module, "OpenAIService", lambda: MagicMock())
    return learning_module.LearningCategorizationService()


def test_learning_pure_helpers_and_db_error_paths(monkeypatch):
    service = learning_fixture(monkeypatch)
    assert service._extract_keywords("The red steel pump for delivery")['keywords']
    assert service._calculate_keyword_similarity(["steel"], ["steel"]) == 1.0
    assert service._calculate_category_similarity("A > B > C", "A") > 0
    assert service._determine_mapping_confidence(.9) == "high"
    assert service._determine_mapping_confidence(.6) == "medium"
    assert service._determine_mapping_confidence(.1) == "low"
    assert service._deduplicate_category_hierarchy(db=None, level_1="Equipment", level_2="Equipment", level_3="Pump")["level_2"] == "Equipment"
    db = FakeDB(FakeQuery(value=None))
    monkeypatch.setattr(learning_module, "get_db_session", lambda: db)
    assert service.check_existing_learning_category("item") is None
    assert service.update_usage_frequency("id") is False
    assert service.update_client_category("id", "new") is False
    assert service.get_learning_category_suggestions("x") == []
    assert service.get_learning_category_stats()["total_learning_categories"] == 0


@pytest.mark.asyncio
async def test_learning_async_success_and_validation(monkeypatch):
    service = learning_fixture(monkeypatch)
    service.openai_service.generate_3_level_categorization = AsyncMock(return_value={"success": True, "categorization": {"level_1": "Equipment", "level_2": "Pumps", "level_3": "Water"}, "confidence_score": .9})
    service.openai_service.close_sync = MagicMock()
    monkeypatch.setattr(service, "check_existing_learning_category", MagicMock(return_value=None))
    monkeypatch.setattr(service, "_log_learning_categorization", MagicMock())
    db = FakeDB(FakeQuery(value=None))
    monkeypatch.setattr(learning_module, "get_db_session", lambda: db)
    result = await service.create_3_level_category("water pump", "Equipment", [], "u")
    assert result["success"] is True
    service.openai_service.validate_learning_category = AsyncMock(return_value={"success": True, "confidence": .9})
    service.openai_service.close_sync = MagicMock()
    assert (await service.validate_learning_category("A", "B", "C", "pump"))["success"]
    service.openai_service.validate_learning_category.side_effect = RuntimeError("ai")
    assert (await service.validate_learning_category("A", "B", "C", "pump"))["is_valid"] is False


def notification_fixture(monkeypatch):
    wa = MagicMock()
    monkeypatch.setattr(notification_module, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(notification_module, "get_settings", lambda: settings())
    service = notification_module.SellerNotificationService()
    return service, wa


@pytest.mark.asyncio
async def test_seller_notifications_format_and_batch_success_failure(monkeypatch):
    service, wa = notification_fixture(monkeypatch)
    assert "RFQ ID" in service.format_rfq_message({"rfq_id": "R1", "categories": ["A"], "delivery_date": "2025-01-02", "delivery_location": {"city": "X", "state": "Y"}, "description": "desc"})
    assert service._format_date("bad") == "bad"
    assert service._get_rfq_buttons("R", "S")[0]["id"] == "rfq_interested_R_S"
    assert service._get_bfs_buttons("B", "S")[1]["id"] == "bfs_seller_reject_B_S"
    wa.send_configurable_buttons = AsyncMock(return_value=MessageResponse(success=True, message_id="m1"))
    result = await service.send_rfq_notifications({"rfq_id": "R", "categories": []}, [{"seller_id": "s", "seller_name": "S", "phone_number": "1"}], skip_workflow_check=True)
    assert result["sent"] == 1
    wa.send_configurable_buttons.return_value = MessageResponse(success=False, error="down")
    result = await service.send_rfq_notifications({"rfq_id": "R"}, [{"seller_id": "s", "phone_number": "2"}], skip_workflow_check=True)
    assert result["failed"] == 1
    assert (await service.send_rfq_notifications({"rfq_id": "R"}, []))["total"] == 0
    assert (await service.send_bfs_bid_notification("", {}, "b", "s"))["success"] is False
    assert (await service.send_bfs_bid_notification("1", {}, "b", ""))["success"] is False
    wa.send_configurable_buttons.return_value = MessageResponse(success=True, message_id="b1")
    assert (await service.send_bfs_bid_notification("1", {"item_description": "x", "ask_price": 10}, "b", "s", True))["success"]
    assert (await service.send_bfs_bid_notifications_batch([]))["total"] == 0


def test_seller_recommendation_helpers_and_singleton(monkeypatch):
    recommendation_module.SellerRecommendationService._instance = None
    recommendation_module.SellerRecommendationService._initialized = False
    monkeypatch.setattr(recommendation_module, "get_settings", lambda: settings())
    db = FakeDB()
    service = recommendation_module.SellerRecommendationService(db)
    assert service._empty_selection_result("none")["total_selected"] == 0
    seller = SimpleNamespace(seller_id="s", seller_name="Seller", phone_number="1", email="e", categories=[], location={}, subscription_credits=1, ranking=SimpleNamespace(value="Gold"), last_active_at=None)
    data = service._seller_to_dict(seller)
    assert data["seller_id"] == "s"
    assert service._instance is service


@pytest.mark.asyncio
async def test_seller_categorization_empty_and_stats(monkeypatch):
    db = FakeDB(FakeQuery(values=[]))
    monkeypatch.setattr(seller_cat_module, "get_db_session", lambda: db)
    monkeypatch.setattr(seller_cat_module, "get_settings", lambda: settings())
    monkeypatch.setattr(seller_cat_module, "OpenAIService", lambda: MagicMock())
    service = seller_cat_module.SellerCategorizationService(db)
    result = await service.process_all_sellers()
    assert result["success"]
    stats = await service.get_categorization_statistics()
    assert isinstance(stats, dict)
    assert (await service.categorize_seller_categories("missing"))["success"] is False


# RFQ background/intimation -----------------------------------------------
def background_fixture(monkeypatch):
    background_module.RFQBackgroundService._instance = None
    background_module.RFQBackgroundService._initialized = False
    db = FakeDB()
    recommendation = MagicMock()
    intimation = MagicMock()
    monkeypatch.setattr(background_module, "get_db_session", lambda: db)
    monkeypatch.setattr(background_module, "get_settings", lambda: settings())
    monkeypatch.setattr(background_module, "SellerRecommendationService", lambda *_: recommendation)
    monkeypatch.setattr(background_module, "RFQIntimationService", lambda *_: intimation)
    return background_module.RFQBackgroundService(db), db, recommendation, intimation


@pytest.mark.asyncio
async def test_rfq_background_empty_selection_batch_and_statistics(monkeypatch):
    service, _, recommendation, _ = background_fixture(monkeypatch)
    service._fetch_rfq_data = AsyncMock(return_value={"rfq_id": "R", "rfq_title": "R"})
    recommendation.select_sellers_for_rfq = AsyncMock(return_value={"total_selected": 0, "subscribed_sellers": [], "unsubscribed_sellers": []})
    result = await service.process_approved_rfq("R")
    assert result["sellers_selected"] == 0
    service.process_approved_rfq = AsyncMock(side_effect=[{"success": True, "notifications_sent": 2}, RuntimeError("bad")])
    batch = await service.process_multiple_rfqs(["R1", "R2"])
    assert batch["successful_rfqs"] == 1 and batch["failed_rfqs"] == 1
    assert service.get_job_statistics()["total_jobs_tracked"] >= 1
    assert service.get_active_jobs()


@pytest.mark.asyncio
async def test_rfq_intimation_eligibility_subscription_and_missing_seller(monkeypatch):
    db = FakeDB()
    wa = MagicMock()
    opt = MagicMock()
    monkeypatch.setattr(intimation_module, "get_settings", lambda: settings())
    monkeypatch.setattr(intimation_module, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(intimation_module, "OptOutService", lambda *_: opt)
    service = intimation_module.RFQIntimationService(db)
    service._get_seller_details = AsyncMock(return_value=None)
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["error"] == "Seller not found"
    seller = SimpleNamespace(subscription_credits=2, phone_number="1")
    service._get_seller_details.return_value = seller
    opt.check_seller_notification_eligibility = AsyncMock(return_value={"eligible": False, "reason": "opted out"})
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["success"] is False
    opt.check_seller_notification_eligibility.return_value = {"eligible": True}
    wa.send_message = AsyncMock(return_value=MessageResponse(success=True, message_id="m"))
    service._generate_rfq_brief = AsyncMock(return_value="brief")
    service._create_credited_seller_message = AsyncMock(return_value="message")
    service._record_notification = AsyncMock()
    service._record_interaction = AsyncMock()
    service._start_conversation_timeout = AsyncMock()
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["next_step"] == "rfq_id_request"
    assert (await service.handle_subscription_selection("s", "R", "bad"))["error"] == "Invalid subscription plan"
    service._get_seller_details.return_value = seller
    service.mock_procurev = MagicMock()
    service.mock_procurev.generate_payment_link = AsyncMock(return_value={"success": False, "error": "no"})
    assert (await service.handle_subscription_selection("s", "R", "basic"))["success"] is False


# Matching, cancel, exit, intent, email -----------------------------------


@pytest.mark.asyncio
async def test_cancel_and_exit_confirmation_branches(monkeypatch):
    wa = MagicMock()
    wa.send_message = AsyncMock()
    wa.send_configurable_buttons = AsyncMock(return_value=MessageResponse(success=True))
    manager = MagicMock()
    manager.save_session = AsyncMock()
    confirmation = MagicMock()
    monkeypatch.setattr(cancel_module, "WhatsAppService", lambda: wa)
    cancel = cancel_module.CancelService(wa, manager, MagicMock(), confirmation)
    empty = session_obj(workflow_type=None)
    assert (await cancel.handle_cancel_intent("1", empty))["status"] == "no_workflow"
    active = session_obj(workflow_state={"messages": []})
    assert (await cancel.handle_cancel_intent("1", active))["status"] == "confirmation_pending"
    assert active.workflow_state["cancel_pending"]
    monkeypatch.setattr(cancel_module, "restore_last_bot_message", AsyncMock())
    declined = await cancel.handle_cancel_confirmation("1", active, False)
    assert declined["status"] == "cancelled_aborted"

    monkeypatch.setattr(exit_module, "get_settings", lambda: settings())
    auth = MagicMock()
    auth.clear_user_token = AsyncMock(return_value=True)
    exit_service = exit_module.ExitService(wa, auth, manager, MagicMock())
    pending = session_obj(workflow_state={})
    assert (await exit_service.handle_exit_intent("1", pending))["status"] == "exit_confirmation_pending"
    monkeypatch.setattr(exit_module, "restore_last_bot_message", AsyncMock())
    assert (await exit_service.handle_exit_confirmation("1", pending, False))["status"] == "exit_aborted"
    exit_service._clear_session_data = AsyncMock(return_value=True)
    assert (await exit_service.handle_exit_intent("1", session_obj(), show_message=False))["status"] == "exit_completed"


@pytest.mark.asyncio
async def test_intent_fast_paths_context_fallback_and_detectors(monkeypatch):
    openai = MagicMock()
    monkeypatch.setattr(intent_module, "OpenAIService", lambda: openai)
    monkeypatch.setattr(intent_module, "get_settings", lambda: settings())
    service = intent_module.IntentService()
    assert (await service.classify_intent("hello"))["intent"] == "greeting"
    assert (await service.classify_intent("1"))["intent"] == "ambiguous"
    openai.classify_intent = AsyncMock(return_value={"success": True, "intent": "exit_system", "confidence": 90})
    assert (await service.classify_intent("leave"))["intent"] == "exit_system"
    openai.classify_intent.return_value = {"success": False}
    service._get_fallback_classification = AsyncMock(return_value={"intent": "ambiguous", "success": False})
    fallback = await service.classify_intent("please cancel")
    assert fallback["intent"] == "ambiguous"
    assert service._get_general_fallback_intent("goodbye")[0] == "exit_system"
    assert service.detect_exit_keywords("please reset")
    assert service.should_allow_exit(SimpleNamespace())
    session = SimpleNamespace(workflow_state={"extracted_entities": [{"x": 1}]})
    monkeypatch.setattr("app.services.workflow_manager.WorkflowManager.get_workflow_type", lambda _: WorkflowType.rfq_creation)
    monkeypatch.setattr("app.services.workflow_manager.WorkflowManager.get_delivery_details", lambda _: {"city": "X"})
    assert service.detect_interruption_intent("hello", session)["is_interruption"]


@pytest.mark.asyncio
async def test_email_templates_cache_recipients_and_api_errors(monkeypatch, tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "welcome.json").write_text('{"scenario_id":"welcome","to":"support@procucev.com,{email}","subject":"Hi {name}","body":"Hello {name}"}', encoding="utf-8")
    api = MagicMock()
    api.send_email = AsyncMock(return_value={"status": "Success"})
    monkeypatch.setattr(email_module, "get_settings", lambda: settings(email_templates_path=str(template_dir)))
    monkeypatch.setattr(email_module, "EmailServiceAPI", lambda: api)
    service = email_module.EmailService()
    result = await service.send_email_by_template("welcome", {"name": "Ada", "email": "a@example.com"}, attachments=[{"name": "a"}])
    assert result["status"] == "Success"
    assert api.send_email.await_args.args[0]["to"] == ["support@example.com", "a@example.com"]
    assert service.get_template_info("welcome") is not None
    assert len(service.list_available_templates()) == 1
    assert (await service.send_email_by_template("missing", {}))["statusCode"] == "404"
    (template_dir / "empty.json").write_text('{"to":"","subject":"x","body":"x"}', encoding="utf-8")
    assert (await service.send_email_by_template("empty", {}))["statusCode"] == "400"
    api.send_email.side_effect = RuntimeError("mail down")
    assert (await service.send_email_by_template("welcome", {"name": "A", "email": "a"}))["statusCode"] == "500"
    assert service._process_recipients("a, {email}", {"email": "b"}) == ["a", "b"]
    assert "{missing}" in service._substitute_variables("{missing}", {})


@pytest.mark.asyncio
async def test_inactivity_timeout_constructor_activity_and_lifecycle(monkeypatch):
    direct_redis = MagicMock()
    direct_redis.setex = AsyncMock(return_value=True)
    direct_redis.get = AsyncMock(return_value=None)
    direct_redis.delete = AsyncMock(return_value=1)
    direct_redis.scan = AsyncMock(return_value=(0, []))
    session_redis = FakeRedis()
    monkeypatch.setattr(timeout_module.Redis, "from_url", lambda *args, **kwargs: direct_redis)
    monkeypatch.setattr(timeout_module, "get_session_redis_service", lambda: session_redis)
    monkeypatch.setattr(timeout_module, "get_settings", lambda: settings())
    monkeypatch.setattr(timeout_module, "WhatsAppService", lambda: MagicMock())
    service = timeout_module.InactivityTimeoutService()
    await service.update_user_activity("+9199")
    direct_redis.setex.assert_awaited_once()
    service.enabled = False
    assert await service.try_start_monitoring_if_available() is False
    await service.stop_monitoring()
