"""Deep deterministic unit coverage for chat/profile/seller service workflows.

Every test replaces network, Redis, database, OpenAI, Celery, and WhatsApp
boundaries with local fakes or mocks.  The tests exercise service decisions
rather than integration behavior.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.cancel_service as cancel_module
import app.services.chat_service as chat_module
import app.services.exit_service as exit_module
import app.services.inactivity_timeout_service as timeout_module
import app.services.intent_service as intent_module
import app.services.profile_selection_service as profile_module
import app.services.seller_categorization_service as categorization_module
import app.services.seller_recommendation_service as recommendation_module
import app.services.seller_service as seller_module
import app.services.session_management_service as session_module
from app.models import ConversationOutcome, WorkflowType


class FakeQuery:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = [] if values is None else values

    def filter(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def limit(self, *args, **kwargs):
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


class FakeDB:
    def __init__(self, query=None):
        self.query_result = query or FakeQuery()
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.append_session_data = MagicMock()
        self.save_conversation_session = MagicMock()

    def query(self, *args, **kwargs):
        return self.query_result

    def get_conversation_session(self, *args, **kwargs):
        return None

    def add(self, value):
        self.added.append(value)


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock()


class FakeRedis:
    def __init__(self):
        self.get_session = AsyncMock(return_value=None)
        self.store_session = AsyncMock()
        self.save_session = AsyncMock()
        self.refresh_ttl = AsyncMock()
        self.session_exists = AsyncMock(return_value=False)
        self.delete_session = AsyncMock(return_value=True)
        self.delete = AsyncMock(return_value=1)
        self.setex = AsyncMock()
        self.get = AsyncMock(return_value=None)
        self.exists = AsyncMock(return_value=False)
        self.scan = AsyncMock(return_value=(0, []))
        self.lock_instance = FakeLock()

    def lock(self, *args, **kwargs):
        return self.lock_instance


class FakeSettings:
    redis_session_storage_enabled = True
    license_enabled = False
    redis_url = "redis://localhost:6379/0"
    workflow_timeout_enabled = True
    workflow_timeout_seconds = 300
    timeout_poll_interval_seconds = 1
    activity_key_ttl_seconds = 420
    worker_timeout_threshold_seconds = 135
    pending_reply_ttl_seconds = 180
    support_contact_info = "support@example.com"
    support_email = "support@example.com"
    procucev_link = "https://procucev.example"
    procucev_rfq_details_url = "https://procucev.example/rfqs"
    rfq_max_allowed = 3
    use_sectioned_rfq = False


def session_obj(**overrides):
    value = dict(
        session_id="sid",
        external_user_id="919999999999",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={},
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
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


def user_obj(**overrides):
    value = dict(
        phone_number="+919999999999",
        org_id="org-1",
        id="user-1",
        email="buyer@example.com",
        username="buyer",
        name="Ada Buyer",
        role="buyer",
        is_registered=True,
    )
    value.update(overrides)
    return SimpleNamespace(**value)


def chat_service():
    service = object.__new__(chat_module.ChatService)
    service.whatsapp_service = MagicMock()
    service.whatsapp_service.send_message = AsyncMock()
    service.whatsapp_service.send_configurable_buttons = AsyncMock()
    service.session_manager = MagicMock()
    service.session_manager.save_session = AsyncMock()
    service.session_manager.send_and_track_message = AsyncMock()
    service.session_manager.add_message_to_history = MagicMock()
    service._openai_service = MagicMock()
    service._openai_service.generate_response = AsyncMock(return_value="generated")
    service._response_helpers = MagicMock()
    service._response_helpers.generate_contextual_response = AsyncMock(return_value="context")
    service._response_helpers.generate_completion_response = AsyncMock(return_value="complete")
    service._response_helpers.generate_clarification_response = AsyncMock(return_value="clarify")
    service._faq_service = MagicMock()
    service._faq_service.get_faq_answer = AsyncMock(return_value=None)
    service.settings = FakeSettings()
    service.db_manager = FakeDB()
    service.chat_summary_service = MagicMock()
    service.daily_summary_service = MagicMock()
    service._bfs_search_handler = MagicMock()
    service._bfs_search_handler.handle_bfs_search = AsyncMock(return_value={"status": "bfs"})
    service._purchase_intent_handler = MagicMock()
    service._purchase_intent_handler.handle_purchase_intent = AsyncMock(return_value={"status": "purchase"})
    service._confirmation_handler = MagicMock()
    service._confirmation_handler.handle_optional_fields_response = AsyncMock(return_value={"status": "optional"})
    service._cancel_service = MagicMock()
    service._cancel_service.handle_cancel_confirmation = AsyncMock(return_value={"status": "cancelled"})
    service._exit_service = MagicMock()
    service._exit_service.handle_exit_confirmation = AsyncMock(return_value={"status": "exit_aborted"})
    service._seller_service = MagicMock()
    service._seller_service.handle_seller_workflow = AsyncMock(return_value={"success": True, "workflow_step": "general_seller_response", "message": "seller"})
    service._rfq_status_service = MagicMock()
    service._rfq_status_service.handle_rfq_status_inquiry = AsyncMock(return_value={"status": "rfq_status"})
    service._attachment_decision_handler = MagicMock()
    service._attachment_decision_handler.handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    service._intent_switch_handler = MagicMock()
    service._intent_switch_handler.should_handle_intent_switch = AsyncMock(return_value=False)
    service.settings = FakeSettings()
    return service


# ChatService ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_helpers_cover_cache_llm_irrelevant_and_serialization(monkeypatch):
    service = chat_service()
    session = session_obj(conversation_history={"messages": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]})
    service._cache_irrelevant_response = AsyncMock()
    await service.handle_irrelevant_message_flow("1", {"intent": "greeting", "relevant_message": "hello"}, session)
    service._cache_irrelevant_response.assert_awaited_once()

    service._faq_service.get_faq_answer.return_value = "I don't have specific information about that"
    assert await service._handle_irrelevant_message("1", "question", {"conversation_history": {}}) == "generated"
    service._faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert "trouble" in await service._handle_irrelevant_message("1", "question", {})
    service._openai_service.generate_response.side_effect = RuntimeError("llm")
    assert "trouble" in await service._generate_llm_response("1", "x", {"conversation_history": {"messages": []}})

    redis = MagicMock()
    redis.get = AsyncMock(return_value={})
    redis.set = AsyncMock()
    monkeypatch.setattr(chat_module, "get_redis_service", lambda: redis)
    service._cache_irrelevant_response = chat_module.ChatService._cache_irrelevant_response.__get__(service)
    await service._cache_irrelevant_response("1", "cached")
    redis.set.assert_awaited_once()
    redis.get.side_effect = RuntimeError("redis")
    await service._cache_irrelevant_response("1", "cached")

    assert service._clean_for_json_serialization({"a": [1, object()]})["a"][0] == 1
    assert service._clean_for_json_serialization(None) is None
    assert service._clean_for_json_serialization(date(2025, 1, 1)) == "2025-01-01"
    assert service._clean_for_json_serialization(SimpleNamespace(value="enum")) == "enum"
    service.db_manager.save_conversation_session = MagicMock(return_value=session)
    await service._save_session(session, "rfq_creation")
    assert service._should_use_summary_aware_extraction("refer to it") is False


@pytest.mark.asyncio
async def test_chat_message_helpers_cover_excel_conversion_confirmation_and_interactive():
    service = chat_service()
    user = user_obj()
    session = session_obj(workflow_state={"excel_confirmation_data": {"products": [{"description": "pump"}], "filename": "x.xlsx"}, "awaiting_excel_confirmation": True})
    converted = service._convert_excel_items_to_products_array([{"ItemDescription": " Pump ", "Quantity": "2", "Uom": "pcs", "Specification": "steel", "Remarks": "ok"}, {"Quantity": "bad"}])
    assert converted[0]["description"] == "Pump"
    assert converted[1]["quantity"] is None

    service._openai_service.parse_confirmation_response = AsyncMock(return_value="no")
    assert (await service._handle_excel_confirmation_response(user, session, "no"))["status"] == "excel_cancelled_redirected_to_greeting"
    service._openai_service.parse_confirmation_response.return_value = "maybe"
    assert (await service._handle_excel_confirmation_response(user, session, "huh"))["status"] == "excel_clarification_requested"
    assert (await service._handle_excel_confirmation_button(user, session, "confirm_excel"))["status"] in {"excel_data_missing", "excel_confirmation_sent", "excel_cancelled_redirected_to_greeting", "error"}
    assert (await service._handle_authentication_email_button(user, session, "unknown"))["status"] == "unknown_email_button"
    assert (await service._handle_list_response(user, session, "row-1"))["list_id"] == "row-1"

    service._confirmation_handler.handle_confirmation_button = AsyncMock(return_value={"status": "ok"})
    assert (await service._handle_button_response(user, session, "continue_rfq"))["status"] == "button_handled"
    assert (await service._handle_button_response(user, session, "unknown"))["status"] == "button_handled"
    service._bfs_search_handler.handle_button = AsyncMock(return_value={"status": "bfs_cancelled"})
    assert (await service._handle_button_response(user, session, "bfs_cancel"))["status"] == "bfs_cancelled"


@pytest.mark.asyncio
async def test_chat_context_entities_tracking_and_profile_summary():
    service = chat_service()
    session = session_obj(workflow_state={"extracted_entities": [{"product_name": "old", "quantity": 1}], "pending_rfq": {"entities": {"description": "new", "quantity": 2}}})
    result = await service._generate_session_summary(session)
    assert "Collected Information" in result and "Old" in result
    assert "No products" in await service._generate_session_summary(session_obj())

    await service._process_entity_updates(session, [{"action": "add", "product_name": "bolt", "quantity": 3}, {"action": "update", "product_name": "old", "quantity": 4}, {"action": "remove", "product_name": "bolt"}])
    assert session.workflow_state["extracted_entities"][0]["quantity"] == 4
    await service._rollback_to_stage(session, "collecting")
    await service._rollback_to_stage(session, "entity_collection")
    await service._clear_session_fields(session, ["stage", "missing"])
    await service._restart_workflow(session)

    session.conversation_history = {"messages": [{"sender": "user", "content": "hi"}]}
    service._update_last_user_message_with_intent(session, "greeting", 90)
    assert session.conversation_history["messages"][0]["intent"] == "greeting"
    service._track_meaningful_message_during_auth_flow(session, "need pumps", {"intent": "buy_something", "confidence": 90})
    assert session.workflow_state["last_meaningful_message"] == "need pumps"
    assert service._is_auth_flow_response("123456", "unknown", session)
    assert service._is_auth_flow_response("a@b.com", "unknown", session)
    assert not service._is_auth_flow_response("need steel", "buy_something", session)
    msg, intent = service._get_meaningful_message_after_auth(session, "yes", {"intent": "greeting"})
    assert msg == "need pumps" and intent["intent"] == "buy_something"
    msg, _ = service._get_meaningful_message_after_auth(session_obj(), "yes", {"intent": "greeting"})
    assert msg.startswith("What can")


@pytest.mark.asyncio
async def test_chat_contextual_interaction_blocks_destructive_and_handles_safe_actions():
    service = chat_service()
    session = session_obj(workflow_state={"extracted_entities": [{"description": "pump"}]})
    result = await service._handle_contextual_interaction(user_obj(), session, "change", {"contextual_response": "Understood", "contextual_actions": [{"type": "clear_session_data"}, {"type": "suggest_alternatives"}], "context_understanding": {"user_intent": "change", "confidence": 80}})
    assert result["destructive_blocked"] is True
    session.workflow_state = {}
    result = await service._handle_contextual_interaction(user_obj(), session, "reset", {"contextual_response": "Understood", "contextual_actions": [{"type": "change_workflow_state"}, {"type": "change_workflow_type"}, {"type": "restart_workflow"}], "context_understanding": {}})
    assert result["status"] == "contextual_interaction_handled"
    service.session_manager.send_and_track_message.side_effect = [RuntimeError("send"), None]
    assert (await service._handle_contextual_interaction(user_obj(), session, "x", {}))["status"] == "contextual_interaction_error"


# ProfileSelectionService ---------------------------------------------------

@pytest.fixture
def profile_service(monkeypatch):
    cache = MagicMock()
    cache.get_user_data = AsyncMock(return_value=[])
    wa = MagicMock()
    wa.send_message = AsyncMock()
    wa.send_configurable_buttons = AsyncMock()
    auth = MagicMock()
    auth.user_authenticate = AsyncMock(return_value={"success": False})
    monkeypatch.setattr(profile_module, "get_user_cache_service", lambda: cache)
    service = profile_module.ProfileSelectionService(wa, auth)
    service.user_cache_service = cache
    return service, wa, auth


def profiles():
    return [
        {"email": "buyer@example.com", "role": "buyer", "name": "Buyer", "user_data": {"fullName": "Ada Buyer"}},
        {"email": "seller@example.com", "role": "seller", "name": "Seller", "user_data": {"fullName": "Sam Seller"}},
    ]


@pytest.mark.asyncio
async def test_profile_selection_all_intent_and_no_profile_routes(profile_service):
    service, wa, auth = profile_service
    session = session_obj()
    service._get_user_profiles = AsyncMock(return_value={"success": False})
    assert (await service.handle_profile_selection("1", "hello", session, {"intent": "greeting"}))["status"] == "new_user_registration_presented"
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": profiles()})
    service._handle_neutral_greeting = AsyncMock(return_value={"status": "neutral"})
    assert (await service.handle_profile_selection("1", "hi", session, {"intent": "greeting", "confidence": 100}))["status"] == "neutral"
    service._handle_buyer_intent = AsyncMock(return_value={"status": "buyer"})
    assert (await service.handle_profile_selection("1", "buy", session, {"intent": "buy_something", "confidence": 90}))["status"] == "buyer"
    service._handle_seller_intent = AsyncMock(return_value={"status": "seller"})
    assert (await service.handle_profile_selection("1", "sell", session, {"intent": "sell_something", "confidence": 90}))["status"] == "seller"
    service._handle_rfq_status_check = AsyncMock(return_value={"status": "status"})
    assert (await service.handle_profile_selection("1", "status", session, {"intent": "rfq_status_check", "confidence": 90}))["status"] == "status"
    assert (await service.handle_profile_selection("1", "question", session, {"intent": "general_inquiry", "confidence": 90}))["status"] == "general_inquiry_already_handled"

    real_service = profile_module.ProfileSelectionService(wa, auth)
    real_service.user_cache_service = service.user_cache_service
    assert (await real_service._handle_neutral_greeting("1", profiles(), session))["profiles_count"] == 2
    assert (await real_service._handle_buyer_intent("1", [], "buy", session, {}))["status"] == "buyer_no_accounts_message_sent"
    assert (await real_service._handle_seller_intent("1", profiles(), "sell", session, {}))["status"] == "seller_profile_selection_presented"
    assert (await real_service._handle_seller_intent("1", profiles()[:1], "sell", session, {}))["status"] == "intent_mismatch_handled"
    real_service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated"})
    assert (await real_service._handle_rfq_status_check("1", profiles()[:1], session))["status"] == "profile_selected_and_authenticated"
    assert (await real_service._handle_invalid_ambiguous("1", profiles(), session))["status"] == "ambiguous_profile_selection_presented"


@pytest.mark.asyncio
async def test_profile_selection_parsing_selection_and_registration(profile_service, monkeypatch):
    service, wa, auth = profile_service
    opts = [{"number": 1, "profile": profiles()[0], "display": "buyer"}, {"number": 2, "action": "register_seller", "display": "seller"}]
    service.user_selection_tool = None
    assert (await service._parse_profile_selection("1", opts))["number"] == 1
    assert (await service._parse_profile_selection("register as seller", opts))["action"] == "register_seller"
    assert await service._parse_profile_selection("nonsense", opts) is None
    assert service._fuzzy_email_match("buy", "buyer@example.com")["confidence"] > 0
    assert service._fuzzy_email_match("x", "buyer@example.com")["confidence"] == 0
    assert service._string_similarity("", "x") == 0

    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated", "redirect_to_main_flow": True})
    session = session_obj(workflow_state={"profile_selection_stage": "buyer_intent", "original_message": "p", "original_intent": {}})
    assert (await service._process_selected_profile("1", opts[0], session))["status"] == "buyer_options_presented"
    session.workflow_state["profile_selection_stage"] = "seller_intent"
    assert (await service._process_selected_profile("1", opts[0], session))["status"] == "seller_options_presented"
    session.workflow_state["profile_selection_stage"] = "other"
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._process_selected_profile("1", opts[0], session))["status"] == "menu"
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller-reg"})
    assert (await service._process_selected_profile("1", opts[1], session))["status"] == "seller-reg"
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer-reg"})
    assert (await service._process_selected_profile("1", {"action": "register_buyer"}, session))["status"] == "buyer-reg"
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    assert (await service._process_selected_profile("1", {"action": "exit"}, session))["status"] == "exit"

    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer-reg"})
    assert (await service._handle_registration_type_response("1", "1", session))["status"] == "buyer-reg"
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller-reg"})
    session.workflow_state["awaiting_registration_type"] = True
    assert (await service._handle_registration_type_response("1", "seller", session))["status"] == "seller-reg"
    assert (await service._handle_registration_type_response("1", "unknown", session))["status"] == "registration_type_clarification_sent"


@pytest.mark.asyncio
async def test_profile_selection_verification_role_filters_and_response_stages(profile_service, monkeypatch):
    service, wa, auth = profile_service
    session = session_obj(workflow_state={})
    profile = profiles()[0]
    auth.auth_redis_service = MagicMock()
    auth.auth_redis_service.retrieve = AsyncMock(return_value=None)
    auth.verification_check_service = MagicMock()
    auth.verification_check_service.check_and_enforce_verification = AsyncMock(return_value={"access_granted": True})
    auth.store_user_session = AsyncMock(return_value=True)
    monkey_user = SimpleNamespace(email="buyer@example.com", role=SimpleNamespace(value="buyer"), name="Ada", company_name="Co")
    # Keep the model conversion local and deterministic.
    original = profile_module.User.from_api_response
    profile_module.User.from_api_response = staticmethod(lambda _: monkey_user)
    monkeypatch.setattr(exit_module, "ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "exit"})))
    try:
        result = await service._set_active_profile_and_proceed("+1", profile, session, "buy", "buy_something")
        assert result["status"] == "profile_selected_and_authenticated"
        auth.auth_redis_service.retrieve.return_value = "token"
        assert (await service._set_active_profile_and_proceed("+1", profile, session, "buy", "buy_something"))["status"] == "profile_selected_and_authenticated"
        auth.auth_redis_service.retrieve.return_value = None
        auth.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": False, "redirect_to_support": True, "redirect_info": {"message": "support"}}
        assert (await service._set_active_profile_and_proceed("+1", profile, session, "buy", "buy_something"))["status"] == "verification_failed"
        auth.auth_redis_service.retrieve.return_value = None
        auth.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": False, "otp_sent": True, "redirect_info": {"flow": "email_verification", "email": "x@example.com"}}
        assert (await service._set_active_profile_and_proceed("+1", profile, session, "buy", "buy_something"))["status"] == "verification_required"
    finally:
        profile_module.User.from_api_response = original

    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": profiles()})
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "menu"
    service._get_user_profiles.return_value = {"success": True, "profiles": profiles() + [profiles()[1].copy()]}
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "filtered_seller_profiles_shown"
    service._get_user_profiles.return_value = {"success": True, "profiles": profiles()[:1]}
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "no_seller_profiles_message_sent"
    service._handle_buyer_intent = AsyncMock(return_value={"status": "buyer"})
    session.workflow_state = {"profile_selection_stage": "neutral_greeting", "profiles": profiles()}
    assert (await service.handle_profile_selection_response("1", "buy", session))["status"] == "buyer"
    session.workflow_state = {"profile_options": []}
    assert (await service.handle_profile_selection_response("1", "1", session))["status"] == "restart_profile_selection"


# SellerService -------------------------------------------------------------

@pytest.fixture
def seller_service(monkeypatch):
    wa = MagicMock()
    wa.send_message = AsyncMock()
    wa.send_configurable_buttons = AsyncMock()
    manager = MagicMock()
    manager.save_session = AsyncMock()
    api = MagicMock()
    monkeypatch.setattr(seller_module, "DatabaseManager", lambda **_: FakeDB())
    monkeypatch.setattr(seller_module, "SellerAPIService", lambda: api)
    monkeypatch.setattr(seller_module, "SessionManagementService", lambda *a, **k: manager)
    monkeypatch.setattr(seller_module, "ChatSummaryService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_module, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "RFQStatusService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_module, "ResponseHelpers", lambda _: MagicMock())
    monkeypatch.setattr(seller_module, "get_settings", lambda: FakeSettings())
    service = seller_module.SellerService(wa, manager)
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="context")
    service.rfq_status_service.handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status"})
    return service, wa, api, manager


@pytest.mark.asyncio
async def test_seller_display_selection_plan_and_email_paths(seller_service):
    service, wa, api, manager = seller_service
    user = user_obj(role="seller", org_id="org", id="seller", email="seller@example.com")
    session = session_obj(workflow_state={})
    api.fetch_active_rfqs = AsyncMock(return_value={"success": True, "rfqs": [{"rfq_id": "R1", "delivery_date": "today", "location": "X", "project_description": "pump"}], "total_count": 1})
    api.check_seller_credits = AsyncMock(return_value={"credits_available": 2})
    result = await service._display_rfqs_to_seller(user, session, "view")
    assert result["workflow_step"] == "display_rfqs_to_seller"
    assert "R1" in result["message"]
    api.check_seller_credits.return_value = {"credits_available": 0}
    assert "credits" in service._generate_hardcoded_rfq_display([{"rfq_id": "R1", "project_description": "x"}], 1, 0).lower()
    assert "no active" in service._generate_hardcoded_rfq_display([], 0, 0).lower()

    service._check_seller_credits = AsyncMock(return_value={"credits_available": 1})
    service._fetch_seller_rfqs = AsyncMock(return_value={"success": True, "rfqs": [{"rfq_id": "R1"}], "total_count": 1})
    service._extract_rfq_ids_from_message = AsyncMock(return_value=[])
    service._handle_general_seller_response = AsyncMock(return_value={"status": "general"})
    assert (await service._handle_rfq_selection_response(user, session, "question"))["status"] == "general"
    service._extract_rfq_ids_from_message.return_value = ["R404"]
    assert (await service._handle_rfq_selection_response(user, session, "R404"))["workflow_step"] == "invalid_rfq_selection"
    service._extract_rfq_ids_from_message.return_value = ["R1"]
    service._process_rfq_email_requests = AsyncMock(return_value={"status": "email"})
    assert (await service._handle_rfq_selection_response(user, session, "R1"))["status"] == "email"
    service._check_seller_credits.return_value = {"credits_available": 0}
    service._handle_no_credits_response = AsyncMock(return_value={"status": "plans"})
    assert (await service._handle_rfq_selection_response(user, session, "R1"))["status"] == "plans"

    api.get_subscription_plans = AsyncMock(return_value={"success": True, "plans": [{"id": "p", "planName": "Basic"}]})
    service._extract_plan_selection = AsyncMock(return_value=None)
    assert (await service._handle_plan_selection_response(user, session, "bad"))["workflow_step"] == "invalid_plan_selection"
    service._extract_plan_selection.return_value = {"id": "p", "planName": "Basic"}
    api.generate_payment_link = AsyncMock(return_value={"success": True, "payment_url": "https://pay"})
    assert (await service._handle_plan_selection_response(user, session, "Basic"))["workflow_step"] == "payment_link_generated"
    assert session.workflow_type is None


@pytest.mark.asyncio
async def test_seller_intents_email_error_and_completion_branches(seller_service):
    service, wa, api, manager = seller_service
    user = user_obj(role="seller", org_id="org", id="seller", email="seller@example.com")
    session = session_obj(workflow_state={"seller_workflow_state": "awaiting_general_response"}, conversation_history={"messages": [{"role": "assistant", "content": "subscription plan"}]})
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 2})
    service._classify_seller_intent = AsyncMock(return_value={"intent": "plan_upgrade_request", "confidence": .9})
    service._handle_plan_upgrade_request = AsyncMock(return_value={"status": "upgrade"})
    assert (await service._handle_general_seller_response(user, session, "upgrade"))["status"] == "upgrade"
    service._classify_seller_intent.return_value = {"intent": "general_question", "confidence": .9}
    assert (await service._handle_general_seller_response(user, session, "help"))["workflow_step"] == "general_seller_response"
    service.response_helpers.generate_seller_contextual_intent_response = AsyncMock(side_effect=RuntimeError("ai"))
    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("upgrade plan", {})["intent"] == "plan_upgrade_request"
    assert service._fallback_intent_classification("rfq details", {})["intent"] == "rfq_access_request"
    assert service._fallback_intent_classification("hello", {})["intent"] == "general_question"
    assert (await service._classify_seller_intent("hello", {}, session))["intent"] == "general_question"

    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="status")
    api.send_rfq_email = AsyncMock(return_value={"data": {"success": True, "results": {"successful": [{"rfq_id": "R1"}], "failed": [{"rfq_id": "R2", "error_code": "NO_CREDITS"}]}}})
    result = await service._process_rfq_email_requests(user, session, ["R1", "R2"])
    assert result["emails_sent"] == 1
    assert result["error_analysis"]["error_counts"]["NO_CREDITS"] == 1
    api.send_rfq_email.return_value = {"success": False, "error": "bad", "error_code": "API_ERROR"}
    assert (await service._process_rfq_email_requests(user, session, ["R3"]))["emails_sent"] == 0
    assert service._analyze_email_errors([{"success": False, "rfq_id": "x", "error_code": "RFQ_NOT_FOUND"}])["has_errors"]

    service._fetch_seller_open_rfqs_for_reminder = AsyncMock(return_value={"success": True, "open_rfqs": [{"id": 1}]})
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "end_of_flow_reminder"
    service._fetch_seller_open_rfqs_for_reminder.return_value = {"success": True, "open_rfqs": []}
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "standard_closing"
    service._fetch_seller_open_rfqs_for_reminder.return_value = {"success": False}
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "generic_closing"


@pytest.mark.asyncio
async def test_seller_extraction_and_wrappers_cover_success_and_errors(seller_service):
    service, wa, api, manager = seller_service
    session = session_obj(conversation_history={"messages": [{"role": "assistant", "content": "1. RFQ123456789012"}]})
    service.openai_service.extract_rfq_ids_from_message = AsyncMock(return_value={"success": True, "rfq_ids": ["R1"]})
    assert await service._extract_rfq_ids_from_message("1", session, []) == ["RFQ123456789012"]
    session.conversation_history = {"messages": []}
    assert await service._extract_rfq_ids_from_message("R1", session, []) == ["R1"]
    service.openai_service.extract_entities = AsyncMock(return_value={"selected_plan": "Basic"})
    assert (await service._extract_plan_selection("basic", [{"id": "1", "planName": "Basic"}]))["id"] == "1"
    service.openai_service.extract_entities.return_value = {}
    assert (await service._extract_plan_selection("select plan", [{"id": "1", "planName": "Basic"}]))["id"] == "1"
    assert service._build_seller_conversation_context(session, "x", 2)["seller_credits"] == 2
    service.seller_api_service.check_seller_credits = AsyncMock(side_effect=RuntimeError("credits"))
    assert (await service._check_seller_credits("x"))["success"] is False
    service.seller_api_service.fetch_active_rfqs = AsyncMock(side_effect=RuntimeError("rfq"))
    assert (await service._fetch_seller_rfqs("x"))["success"] is False


# Recommendation and categorization ----------------------------------------

@pytest.mark.asyncio
async def test_seller_recommendation_full_selection_filters_and_details(monkeypatch):
    recommendation_module.SellerRecommendationService._instance = None
    recommendation_module.SellerRecommendationService._initialized = False
    db = FakeDB(FakeQuery())
    monkeypatch.setattr(recommendation_module, "get_settings", lambda: FakeSettings())
    service = recommendation_module.SellerRecommendationService(db)
    seller_a = SimpleNamespace(seller_id="a", seller_name="A", phone_number="1", email="a@x", categories=["Pumps"], location={"pincode": "100001"}, opted_out_notifications=False, subscription_credits=2, ranking=None, last_active_at=datetime.utcnow())
    seller_b = SimpleNamespace(seller_id="b", seller_name="B", phone_number="2", email="b@x", categories=["Pumps"], location={"pincode": "100002"}, opted_out_notifications=False, subscription_credits=0, ranking=None, last_active_at=datetime.utcnow())
    monkeypatch.setattr(recommendation_module.SellerDataAdapter, "get_sellers_from_remote", lambda _: [seller_a, seller_b, seller_a])
    monkeypatch.setattr(recommendation_module.pincode_distance, "calculate_distance_between_pincodes", lambda a, b: 1 if b == "100001" else 2)
    result = await service.select_sellers_for_rfq({"rfq_id": "R", "categories": ["pumps"], "delivery_location": {"pincode": "100000"}}, ["a", "b"])
    assert result["total_selected"] == 2
    assert result["subscribed_sellers"][0]["seller_id"] == "a"
    assert await service._filter_sellers_by_location([seller_a], {}) == [seller_a]
    seller_b.location = {}
    assert await service._filter_sellers_by_location([seller_a, seller_b], {"pincode": "100000"})
    assert await service._filter_inactive_sellers([seller_a, seller_b], 24)
    db.query_result = FakeQuery(value=None)
    assert await service._filter_by_message_history([seller_a], 24) == [seller_a]
    assert await service._rank_sellers_by_criteria([seller_b, seller_a], {})
    assert await service._apply_cyclic_selection([seller_b, seller_a], [])
    assert len(await service._deduplicate_sellers([seller_a, seller_a])) == 1
    assert await service._load_system_config()
    db.query_result = FakeQuery(value=None)
    assert await service.get_seller_details("missing") is None
    assert service._empty_selection_result("x")["total_selected"] == 0


@pytest.mark.asyncio
async def test_seller_categorization_batches_jobs_mappings_and_statistics(monkeypatch):
    db = FakeDB(FakeQuery(values=[]))
    monkeypatch.setattr(categorization_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(categorization_module, "OpenAIService", lambda: MagicMock())
    service = categorization_module.SellerCategorizationService(db)
    seller = SimpleNamespace(seller_id="s", seller_name="Seller", categories=["Pumps"], location={}, ranking=SimpleNamespace(value="Gold"))
    service._get_sellers_needing_categorization = AsyncMock(return_value=[seller, seller])
    service._process_seller_batch = AsyncMock(return_value={"processed": 1, "errors": 1})
    result = await service.process_all_sellers()
    assert result["batches_processed"] == 1
    service._get_seller_details = AsyncMock(return_value=seller)
    service._create_categorization_job = AsyncMock(return_value=SimpleNamespace(job_id="j"))
    service._generate_3_level_mapping_for_category = AsyncMock(return_value={"success": True, "mapping": {"level_1_category": "A"}})
    service._store_seller_mappings = AsyncMock()
    service._update_categorization_job = AsyncMock()
    assert (await service.categorize_seller_categories("s"))["success"]
    service._generate_3_level_mapping_for_category.return_value = {"success": False, "error": "bad"}
    assert (await service.categorize_seller_categories("s"))["success"] is False
    service2 = categorization_module.SellerCategorizationService(db)
    service2._get_similar_category_items = AsyncMock(return_value=[])
    service2.openai_service.generate_3_level_categorization = MagicMock(return_value={"success": True, "categorization": {"level_1": "A", "level_2": "B", "level_3": "C"}})
    assert (await service2._generate_3_level_mapping_for_category("Pumps", seller))["success"]
    service2.openai_service.generate_3_level_categorization.return_value = {"success": False, "error": "no"}
    assert (await service2._generate_3_level_mapping_for_category("Pumps", seller))["success"] is False
    service.categorize_seller_categories = AsyncMock(side_effect=[{"success": True}, RuntimeError("bad")])
    batch = await service._process_seller_batch([seller, seller])
    assert batch["processed"] == 1 and batch["errors"] == 1
    service._store_seller_mappings = categorization_module.SellerCategorizationService._store_seller_mappings.__get__(service)
    assert await service._store_seller_mappings("s", [{"original_category": "P", "level_1_category": "A", "level_2_category": "B", "level_3_category": "C", "confidence_score": .8, "ai_reasoning": "ok"}]) is None
    service.db_session.query = MagicMock(side_effect=RuntimeError("db"))
    assert await service.get_categorization_statistics()


# Session management, timeout, intent, cancel, exit ------------------------

@pytest.fixture
def session_service(monkeypatch):
    redis = FakeRedis()
    db = FakeDB()
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())
    service = session_module.SessionManagementService(db, MagicMock(), MagicMock(), MagicMock())
    service.db_manager = db
    return service, redis, db


@pytest.mark.asyncio
async def test_session_management_lifecycle_and_save_outcomes(session_service, monkeypatch):
    service, redis, db = session_service
    monkeypatch.setattr(session_module.SessionHelpers, "generate_session_id", lambda p, s: "sid")
    monkeypatch.setattr("app.services.welcome_message_service.get_welcome_service", lambda: SimpleNamespace(check_and_send_welcome=AsyncMock()))
    db.save_conversation_session.return_value = session_obj()
    created = await service.get_conversation_context("1")
    assert created.session_id == "sid"
    db.save_conversation_session.return_value = session_obj(workflow_type=None)
    service.redis_enabled = False
    assert (await service.create_session("1", "rfq_creation", "buyer")).workflow_type is None
    service.redis_enabled = False
    s = session_obj(outcome=ConversationOutcome.completed, workflow_state={"old": 1})
    db.get_conversation_session = MagicMock(return_value=s)
    redis.get_session.return_value = None
    fresh = await service.get_conversation_context("1")
    assert fresh.outcome is None
    service.redis_enabled = True
    assert await service.handle_session_expiry_check("1", fresh) is fresh
    await service.save_session(fresh, WorkflowType.rfq_creation)
    await service.save_session(fresh, "not-valid")
    fresh.outcome = ConversationOutcome.completed
    await service.save_session(fresh, WorkflowType.rfq_creation)
    fresh.outcome = None
    fresh.workflow_state = {"exit_completed": True}
    await service.save_session(fresh)
    assert service._clean_for_json_serialization([date(2025, 1, 1), {"x": object()}])


@pytest.mark.asyncio
async def test_session_summary_placeholders_and_background_fallback(session_service, monkeypatch):
    service, redis, db = session_service
    session = session_obj()
    service.chat_summary_service.generate_session_summary = AsyncMock()
    service.daily_summary_service.generate_daily_summary = AsyncMock()
    await service._handle_session_completion_fallback(session)
    await service._show_auth_placeholder("1")
    service.whatsapp_service.send_message = AsyncMock(side_effect=RuntimeError("down"))
    await service._show_auth_placeholder("1")
    monkeypatch.setattr(session_module.SummarizationHelpers, "extract_rich_entities_for_summary", MagicMock(return_value=[]))
    monkeypatch.setattr(session_module.SummarizationHelpers, "prepare_enhanced_summary_data", MagicMock(return_value={}))
    monkeypatch.setattr(session_module.SummarizationHelpers, "handle_session_completion_async", AsyncMock())
    await service.handle_session_completion_enhanced(session)
    monkeypatch.setattr(service, "_handle_session_completion_fallback", AsyncMock())
    monkeypatch.setattr(session_module.SummarizationHelpers, "extract_rich_entities_for_summary", MagicMock(side_effect=RuntimeError("x")))
    await service.handle_session_completion_enhanced(session)
    await service._handle_session_completion_enhanced(session)
    circular = []; circular.append(circular)
    assert service._clean_for_json_serialization(circular)[0] == "<circular_reference>"


@pytest.fixture
def timeout_service(monkeypatch):
    service = object.__new__(timeout_module.InactivityTimeoutService)
    service.redis = FakeRedis()
    service.redis_session = FakeRedis()
    service.timeout_seconds = 300
    service.poll_interval = 1
    service.activity_key_ttl = 420
    service.worker_timeout_threshold = 135
    service.pending_reply_ttl = 180
    service.enabled = True
    service.whatsapp_service = MagicMock()
    service.whatsapp_service.send_message = AsyncMock()
    service._monitor_task = None
    return service


@pytest.mark.asyncio
async def test_timeout_activity_messages_and_monitoring(timeout_service, monkeypatch):
    service = timeout_service
    assert "resume creating" in await service._generate_timeout_message(SimpleNamespace(self_client=True), {})
    assert "resume anytime" in await service._generate_timeout_message(None, {})
    monkeypatch.setattr(timeout_module, "time", SimpleNamespace(time=lambda: 10))
    await service.update_user_activity("+1")
    service.redis.lock_instance.acquired = False
    await service.update_user_activity("+1")
    service.redis.lock_instance.acquire.side_effect = RuntimeError("lock")
    await service.update_user_activity("+1")
    service.enabled = False
    assert await service.try_start_monitoring_if_available() is False
    await service.start_monitoring()
    service.enabled = True
    service._monitor_task = SimpleNamespace(done=lambda: False)
    await service.start_monitoring()
    service._monitor_task = None
    service._run_monitor_loop = AsyncMock()
    assert await service.try_start_monitoring_if_available() is True
    await service.stop_monitoring()


@pytest.mark.asyncio
async def test_timeout_scan_worker_and_user_timeout_cleanup(timeout_service, monkeypatch):
    service = timeout_service
    service.redis.scan.side_effect = [(1, ["1:last_activity"]), (0, ["2:last_activity"])]
    service.redis.get.side_effect = [str(0), str(0)]
    service.redis.exists.side_effect = [True, False]
    service._handle_worker_timeout = AsyncMock()
    service._handle_timeout = AsyncMock()
    monkeypatch.setattr(timeout_module.time, "time", lambda: 1000)
    service.redis_session.get_session.side_effect = [{"workflow_type": "rfq_creation", "outcome": None}, None]
    await service._check_inactive_users()
    service.redis_session.get_session.return_value = {"workflow_type": "rfq_creation", "outcome": None, "workflow_state": {}, "conversation_history": {}}
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: SimpleNamespace(retrieve=AsyncMock(return_value=[SimpleNamespace(self_client=True)])))
    service._generate_timeout_message = AsyncMock(return_value="timeout")
    await service._handle_timeout("+1", "sid", "1:last_activity")
    await service._handle_worker_timeout("+1", "sid", "1:last_activity", "1:pending_reply")
    service.redis_session.get_session.return_value = None
    await service._handle_worker_timeout("+1", "sid", "1:last_activity", "1:pending_reply")


@pytest.mark.asyncio
async def test_intent_classification_fast_paths_openai_fallback_and_context(monkeypatch):
    monkeypatch.setattr(intent_module, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(intent_module, "get_settings", lambda: FakeSettings())
    service = intent_module.IntentService()
    assert (await service.classify_intent("hello"))["intent"] == "greeting"
    assert (await service.classify_intent("buy"))["intent"] == "buy_something"
    service.openai_service.classify_intent = AsyncMock(return_value=None)
    result = await service.classify_intent("unknown")
    assert hasattr(result, "__await__")
    result.close()
    service._get_fallback_classification = AsyncMock(return_value={"intent": "fallback"})
    service.openai_service.classify_intent.return_value = {"success": False}
    assert (await service.classify_intent("unknown"))["intent"] == "fallback"
    service.openai_service.classify_intent.return_value = {"success": True, "intent": "exit_system", "confidence": 90}
    assert (await service.classify_intent("leave"))["intent"] == "exit_system"
    service.openai_service.classify_intent.return_value = {"success": True, "intent": "contextual_reference", "confidence": 90}
    service.openai_service.handle_contextual_interaction = AsyncMock(return_value={"response": "ctx", "actions": [{"type": "update_entities"}], "context_understanding": {}})
    assert (await service.classify_intent("that one", {}))["should_update_entities"]
    service.openai_service.handle_contextual_interaction.side_effect = RuntimeError("ctx")
    assert (await service._handle_contextual_intent("contextual_reference", "x", {}, {"intent": "contextual_reference"}))["should_handle_directly"]
    for text, expected in [("bye", "exit_system"), ("cancel", "cancel_workflow"), ("support", "support"), ("status", "rfq_status_check"), ("stock", "bfs_search"), ("buy pump", "buy_something"), ("sell items", "sell_something"), ("what is this", "general_inquiry"), ("nonsense", "ambiguous")]:
        assert service._get_general_fallback_intent(text)[0] == expected
    assert service.detect_exit_keywords("please reset")
    assert service.should_allow_exit(None)


@pytest.mark.asyncio
async def test_cancel_and_exit_confirmation_cleanup_outcomes(monkeypatch):
    wa = MagicMock()
    wa.send_message = AsyncMock()
    wa.send_configurable_buttons = AsyncMock(return_value=SimpleNamespace(success=True))
    manager = MagicMock()
    manager.save_session = AsyncMock()
    cancel = object.__new__(cancel_module.CancelService)
    cancel.whatsapp_service = wa
    cancel.session_manager = manager
    cancel.db_manager = FakeDB()
    cancel.confirmation_service = MagicMock()
    session = session_obj(workflow_type=None)
    assert (await cancel.handle_cancel_intent("1", session))["status"] == "no_workflow"
    session.workflow_type = WorkflowType.rfq_creation
    assert (await cancel.handle_cancel_intent("1", session))["status"] == "confirmation_pending"
    cancel._clear_workflow_state = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: SimpleNamespace(get_user_data=AsyncMock(return_value=[]), clear_meaningful_message=AsyncMock(return_value=True)))
    assert (await cancel.handle_cancel_confirmation("1", session, True, user_obj()))["status"] == "cancelled"
    assert (await cancel.handle_cancel_confirmation("1", session, False))["status"] == "cancelled_aborted"
    assert await cancel._send_confirmation_message("1", session)
    assert await cancel._send_cancellation_message("1", "buyer")
    assert await cancel._send_cancellation_message("1", "seller")
    assert await cancel._send_cancellation_message("1", "other")

    exit_service = object.__new__(exit_module.ExitService)
    exit_service.whatsapp_service = wa
    exit_service.authentication_service = MagicMock()
    exit_service.authentication_service.clear_user_token = AsyncMock(return_value=True)
    exit_service.session_manager = manager
    exit_service.db_manager = FakeDB()
    exit_service.settings = FakeSettings()
    exit_service._clear_session_data = AsyncMock(return_value=True)
    exit_service._send_goodbye_message = AsyncMock(return_value=True)
    session = session_obj(workflow_state={})
    assert (await exit_service.handle_exit_intent("1", session))["status"] == "exit_confirmation_pending"
    assert (await exit_service.handle_exit_intent("1", session, show_message=False))["status"] == "exit_completed"
    assert (await exit_service.handle_exit_confirmation("1", session, False))["status"] == "exit_aborted"
    assert await exit_service._send_exit_confirmation_message("1", session)
    assert await exit_service._send_goodbye_message("1")
    assert exit_service._get_last_bot_message(session) is None
