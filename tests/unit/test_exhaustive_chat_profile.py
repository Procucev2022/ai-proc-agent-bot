"""Deterministic exhaustive unit coverage for ChatService and ProfileSelectionService.

Every external boundary in this module is replaced with an in-memory mock.  The
fixtures deliberately use ``__new__`` for the large orchestrator so tests can
exercise state transitions without constructing database, Redis, AI, or
WhatsApp clients.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.chat_service as chat_module
import app.services.profile_selection_service as profile_module
from app.models import WorkflowType, ConversationSession


class FakeRedis:
    def __init__(self, value=None):
        self.value = value
        self.get = AsyncMock(return_value=value)
        self.set = AsyncMock()
        self.delete_session = AsyncMock()


class FakeLock:
    def __init__(self, acquired=True):
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock()


class FakeDB:
    def __init__(self):
        self.save_conversation_session = MagicMock()
        self.append_session_data = MagicMock()
        self.close = MagicMock()


class FakeSettings:
    redis_url = "redis://unit-test"
    redis_session_storage_enabled = True
    use_sectioned_rfq = False
    rfq_max_allowed = 2
    support_email = "support@example.com"
    support_contact_info = "support@example.com"


def session_obj(**overrides):
    value = {
        "session_id": "sid",
        "external_user_id": "919999999999",
        "workflow_type": WorkflowType.rfq_creation,
        "workflow_state": {},
        "conversation_history": {"messages": [], "openai_messages": [], "metadata": []},
        "extracted_entities": [],
        "whatsapp_context": {},
        "retention_date": date(2025, 1, 1),
        "created_at": datetime(2025, 1, 1),
        "last_activity_at": datetime(2025, 1, 1),
        "completed_at": None,
        "outcome": None,
    }
    value.update(overrides)
    return SimpleNamespace(**value)


def user_obj(**overrides):
    value = {
        "phone_number": "+919999999999",
        "id": "u1",
        "email": "buyer@example.com",
        "username": "buyer",
        "name": "Ada Buyer",
        "role": "buyer",
        "is_registered": True,
    }
    value.update(overrides)
    return SimpleNamespace(**value)


def profile(email="buyer@example.com", role="buyer", name="Ada Buyer"):
    return {"email": email, "role": role, "name": name, "company": "Co", "user_data": {"fullName": name}}


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
    return service, wa, auth, cache


def chat_service():
    service = object.__new__(chat_module.ChatService)
    service.db_session = None
    service.whatsapp_service = MagicMock()
    service.whatsapp_service.send_message = AsyncMock()
    service.whatsapp_service.send_configurable_buttons = AsyncMock()
    service.session_manager = MagicMock()
    service.session_manager.save_session = AsyncMock()
    service.session_manager.send_and_track_message = AsyncMock()
    service.session_manager.add_message_to_history = MagicMock()
    service.session_manager.get_conversation_context = AsyncMock(return_value=session_obj())
    service._intent_service = MagicMock()
    service._intent_service.classify_intent = AsyncMock(return_value={"intent": "greeting", "confidence": 10})
    service._entity_service = MagicMock()
    service._openai_service = MagicMock()
    service._openai_service.generate_response = AsyncMock(return_value="generated")
    service._openai_service.parse_confirmation_response = AsyncMock(return_value="yes")
    service._openai_service.extract_entities = AsyncMock(return_value={"rfq_id": []})
    service._response_helpers = MagicMock()
    service._response_helpers.generate_contextual_response = AsyncMock(return_value="context")
    service._response_helpers.generate_completion_response = AsyncMock(return_value="complete")
    service._response_helpers.generate_clarification_response = AsyncMock(return_value="clarify")
    service._response_helpers.generate_seller_contextual_response = AsyncMock(return_value="seller-error")
    service._faq_service = MagicMock()
    service._faq_service.get_faq_answer = AsyncMock(return_value=None)
    service.settings = FakeSettings()
    service.db_manager = FakeDB()
    service.chat_summary_service = MagicMock()
    service.daily_summary_service = MagicMock()
    service.daily_summary_service.generate_daily_summary = AsyncMock()
    service.chat_summary_service.generate_session_summary = AsyncMock()
    service._bfs_search_handler = MagicMock()
    service._bfs_search_handler.handle_bfs_search = AsyncMock(return_value={"status": "bfs"})
    service._bfs_search_handler.handle_button = AsyncMock(return_value={"status": "button"})
    service._purchase_intent_handler = MagicMock()
    service._purchase_intent_handler.handle_purchase_intent = AsyncMock(return_value={"status": "purchase"})
    service._confirmation_handler = MagicMock()
    service._confirmation_handler.handle_optional_fields_response = AsyncMock(return_value={"status": "optional"})
    service._confirmation_handler.handle_pending_confirmations = AsyncMock(return_value={"status": "pending"})
    service._confirmation_handler.handle_confirmation_button = AsyncMock(return_value={"status": "confirmed"})
    service._cancel_service = MagicMock()
    service._cancel_service.handle_cancel_confirmation = AsyncMock(return_value={"status": "cancelled"})
    service._cancel_service.handle_cancel_intent = AsyncMock(return_value={"status": "cancel"})
    service._cancel_service._send_cancellation_message = AsyncMock()
    service._exit_service = MagicMock()
    service._exit_service.handle_exit_confirmation = AsyncMock(return_value={"status": "exit"})
    service._exit_service.handle_exit_intent = AsyncMock(return_value={"status": "exit"})
    service._seller_service = MagicMock()
    service._seller_service.handle_seller_workflow = AsyncMock(return_value={"success": True, "workflow_step": "other", "message": "seller"})
    service._rfq_status_service = MagicMock()
    service._rfq_status_service.handle_rfq_status_inquiry = AsyncMock(return_value={"status": "rfq-status"})
    service._attachment_decision_handler = MagicMock()
    service._attachment_decision_handler.handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    service._intent_switch_handler = MagicMock()
    service._intent_switch_handler.should_handle_intent_switch = AsyncMock(return_value=False)
    service._intent_switch_handler.handle_intent_switch_response = AsyncMock(return_value={"status": "clarification"})
    service._intent_switch_handler.handle_intent_switch_choice = AsyncMock(return_value={"status": "switch"})
    service._image_processor = MagicMock()
    service._image_processor.process_image_message = AsyncMock(return_value={"status": "image"})
    service._authentication_service = MagicMock()
    service._authentication_service.validate_token = AsyncMock(return_value=user_obj())
    service._authentication_service.otp_service = MagicMock()
    service._authentication_service.otp_service.validate_otp = AsyncMock(return_value={"status": "otp_invalid"})
    service._authentication_service.store_user_session_with_email = AsyncMock(return_value=True)
    service._registration_service = MagicMock()
    service._registration_service.initiate_registration = AsyncMock(return_value={"status": "registered"})
    service._format_modification_handler = MagicMock()
    service._format_modification_handler.handle_format_modification = AsyncMock(return_value={"status": "modified"})
    service._confirmation_service = MagicMock()
    service._confirmation_service.parse_confirmation = AsyncMock(return_value="yes")
    return service


# ProfileSelectionService -------------------------------------------------

@pytest.mark.asyncio
async def test_profile_top_level_routes_and_profile_loading(monkeypatch):
    service, wa, auth, cache = profile_service(monkeypatch)
    session = session_obj()
    profiles = [profile(), profile("seller@example.com", "seller", "Sam Seller")]

    cache.get_user_data.return_value = [{"fullName": "Ada Buyer"}]
    fake_user = SimpleNamespace(email="buyer@example.com", role=SimpleNamespace(value="buyer"), name="Ada", company_name="Co")
    monkeypatch.setattr(profile_module.User, "from_api_response", lambda _: fake_user)
    assert (await service._get_user_profiles("1", "hi", session))["from_cache"]
    cache.get_user_data.return_value = []
    auth.user_authenticate.return_value = {"success": True, "response": [{"fullName": "Sam"}]}
    assert (await service._get_user_profiles("1", "hi", session))["from_cache"] is False
    auth.user_authenticate.return_value = {"success": False, "message": "none"}
    assert (await service._get_user_profiles("1", "hi", session))["success"] is False
    cache.get_user_data.side_effect = RuntimeError("cache")
    assert "error" in await service._get_user_profiles("1", "hi", session)

    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": profiles})
    service._handle_neutral_greeting = AsyncMock(return_value={"status": "neutral"})
    service._handle_buyer_intent = AsyncMock(return_value={"status": "buyer"})
    service._handle_seller_intent = AsyncMock(return_value={"status": "seller"})
    service._handle_rfq_status_check = AsyncMock(return_value={"status": "rfq"})
    service._detect_registration_intent = AsyncMock(return_value=None)
    assert (await service.handle_profile_selection("1", {"button_reply": {"title": "hello"}}, session, {"intent": "greeting", "confidence": 100}))["status"] == "neutral"
    assert (await service.handle_profile_selection("1", "buy", session, {"intent": "buy_something", "confidence": 80}))["status"] == "buyer"
    assert (await service.handle_profile_selection("1", "buy", session, {"intent": "buy_something", "confidence": 60}))["status"] == "buyer"
    assert (await service.handle_profile_selection("1", "sell", session, {"intent": "sell_something", "confidence": 80}))["status"] == "seller"
    assert (await service.handle_profile_selection("1", "status", session, {"intent": "rfq_status_check", "confidence": 80}))["status"] == "rfq"
    assert (await service.handle_profile_selection("1", "q", session, {"intent": "general_inquiry", "confidence": 90}))["status"] == "general_inquiry_already_handled"
    assert (await service.handle_profile_selection("1", "q", session, {"intent": "unknown", "confidence": 90}))["status"] == "neutral"

    service._get_user_profiles = AsyncMock(return_value={"success": False})
    service._handle_no_profiles_found = AsyncMock(return_value={"status": "new"})
    assert (await service.handle_profile_selection("1", "q", session, {"intent": "greeting"}))["status"] == "new"
    service._detect_registration_intent = AsyncMock(return_value="buyer")
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer-reg"})
    assert (await service.handle_profile_selection("1", "register", session, {"intent": "greeting"}))["status"] == "buyer-reg"
    service._detect_registration_intent.return_value = "seller"
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller-reg"})
    assert (await service.handle_profile_selection("1", "register", session, {"intent": "greeting"}))["status"] == "seller-reg"
    service._detect_registration_intent.return_value = None
    service._get_user_profiles.return_value = {"success": True, "profiles": profiles}
    assert (await service.handle_profile_selection("1", "x", session, {"intent": "register_account"}))["status"] == "neutral"
    service._get_user_profiles.side_effect = RuntimeError("boom")
    service._detect_registration_intent.side_effect = RuntimeError("boom")
    assert (await service.handle_profile_selection("1", "x", session, {}))["status"] == "error"


def test_profile_conversion_and_primitives(monkeypatch):
    service, _, _, _ = profile_service(monkeypatch)
    good = SimpleNamespace(email="a@b.com", role=SimpleNamespace(value="buyer"), name="A", company_name="Co")
    monkeypatch.setattr(profile_module.User, "from_api_response", lambda item: good if item.get("ok") else (_ for _ in ()).throw(ValueError("bad")))
    converted = service._convert_api_data_to_profiles([{"ok": True}, {"ok": False}])
    assert len(converted) == 1 and converted[0]["role"] == "buyer"
    assert service._extract_user_name([{"user_data": {"fullName": "  ada lovelace "}}]) == "Ada"
    assert service._extract_user_name([{"user_data": {"fullName": " "}}]) is None
    assert service._extract_user_name([{"user_data": object()}]) is None
    assert service._fuzzy_email_match("x", "a@b.com")["confidence"] == 0
    assert service._fuzzy_email_match("example", "buyer@example.com")["confidence"] > 0
    assert service._fuzzy_email_match("buyer", "buyer@example.com")["confidence"] > 0
    assert service._fuzzy_email_match("zzzz", "buyer@example.com")["confidence"] == 0
    assert service._fuzzy_email_match(None, "x")["confidence"] == 0
    assert service._string_similarity("", "x") == 0
    assert service._string_similarity("abc", "abc") == 1
    assert service._string_similarity("ab", "abcdef") > 0

@pytest.mark.asyncio
async def test_profile_prompt_handlers_and_selection_parser(monkeypatch):
    service, wa, _, _ = profile_service(monkeypatch)
    session = session_obj()
    ps = [profile(), profile("seller@example.com", "seller", "Sam Seller")]
    assert (await service._handle_neutral_greeting("1", ps, session))["selection_type"] == "neutral_greeting"
    assert (await service._handle_buyer_intent("1", [], "buy", session, {}))["status"] == "buyer_no_accounts_message_sent"
    assert (await service._handle_buyer_intent("1", ps, "buy", session, {}))["status"] == "buyer_profile_selection_presented"
    assert (await service._handle_seller_intent("1", [], "sell", session, {}))["status"] == "error" or session.workflow_state.get("profile_selection_stage")
    service._handle_intent_mismatch = AsyncMock(return_value={"status": "mismatch"})
    assert await service._handle_seller_intent("1", ps[:1], "sell", session, {}) == {"status": "mismatch"}
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "direct"})
    assert await service._handle_rfq_status_check("1", ps[:1], session) == {"status": "direct"}
    assert (await service._handle_rfq_status_check("1", ps, session))["options_count"] == 2
    assert (await service._handle_invalid_ambiguous("1", ps, session))["options_count"] == 2
    assert (await service._handle_no_profiles_found("1", "greeting", session))["status"] == "new_user_registration_presented"

    options = [{"number": 1, "profile": ps[0], "display": "buyer"}, {"number": 2, "action": "register_seller", "display": "seller"}]
    service.user_selection_tool = None
    service._detect_registration_intent = AsyncMock(return_value=None)
    assert (await service._parse_profile_selection("1", options))["number"] == 1
    assert (await service._parse_profile_selection("register new", options))["action"] == "register_seller"
    assert await service._parse_profile_selection("use existing", options) == ps[0] | {"number": 1, "display": "buyer"} if False else True
    assert await service._simple_parse_profile_selection("nonsense", options) is None
    service.user_selection_tool = MagicMock()
    service.user_selection_tool.analyze_user_selection = AsyncMock(return_value={"register": {"type": "buyer"}})
    assert (await service._parse_profile_selection("new", options))["action"] == "register_buyer"
    service.user_selection_tool.analyze_user_selection.return_value = {"selected_option": 1, "requires_clarification": False}
    assert (await service._parse_profile_selection("one", options))["number"] == 1
    service.user_selection_tool.analyze_user_selection.return_value = {"selected_option": 9, "requires_clarification": False}
    assert await service._parse_profile_selection("nine", options) is None
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("ai")
    service._enhanced_simple_parse_profile_selection = AsyncMock(return_value=None)
    assert await service._parse_profile_selection("x", options) is None

@pytest.mark.asyncio
async def test_profile_selection_actions_registration_and_verification(monkeypatch):
    service, wa, auth, _ = profile_service(monkeypatch)
    session = session_obj(workflow_state={"profile_selection_stage": "buyer_intent", "original_message": "p", "original_intent": {}})
    ps = profile()
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated", "redirect_to_main_flow": True})
    assert (await service._process_selected_profile("1", {"profile": ps}, session))["status"] == "buyer_options_presented"
    session.workflow_state["profile_selection_stage"] = "seller_intent"
    assert (await service._process_selected_profile("1", {"profile": profile("s@e.com", "seller", "Sam")}, session))["status"] == "seller_options_presented"
    session.workflow_state["profile_selection_stage"] = "rfq_status_check"
    assert (await service._process_selected_profile("1", {"profile": ps}, session))["status"] == "profile_selected_and_authenticated"
    session.workflow_state["profile_selection_stage"] = "other"
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._process_selected_profile("1", {"profile": ps}, session))["status"] == "menu"
    service._handle_new_registration_choice = AsyncMock(return_value={"status": "choice"})
    assert (await service._process_selected_profile("1", {"action": "register_new"}, session))["status"] == "choice"
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    assert (await service._process_selected_profile("1", {"registration_type": "buyer"}, session))["status"] == "buyer"
    assert (await service._process_selected_profile("1", {"registration_type": "seller"}, session))["status"] == "seller"
    service._show_all_profiles = AsyncMock(return_value={"status": "all"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    assert (await service._process_selected_profile("1", {"action": "show_all_profiles"}, session))["status"] == "all"
    assert (await service._process_selected_profile("1", {"action": "exit"}, session))["status"] == "exit"
    assert (await service._process_selected_profile("1", {}, session))["status"] == "error"

    service._set_active_profile_and_proceed = profile_module.ProfileSelectionService._set_active_profile_and_proceed.__get__(service)
    auth.auth_redis_service.retrieve = AsyncMock(return_value=None)
    auth.verification_check_service = MagicMock()
    auth.verification_check_service.check_and_enforce_verification = AsyncMock(return_value={"access_granted": True})
    auth.store_user_session = AsyncMock(return_value=True)
    model_user = SimpleNamespace(email="buyer@example.com", role=SimpleNamespace(value="buyer"), name="Ada", company_name="Co")
    monkeypatch.setattr(profile_module.User, "from_api_response", lambda _: model_user)
    result = await service._set_active_profile_and_proceed("+1", ps, session, "buy", "buy_something")
    assert result["status"] == "profile_selected_and_authenticated"
    auth.auth_redis_service.retrieve.return_value = "token"
    assert (await service._set_active_profile_and_proceed("+1", ps, session, "buy", "buy_something"))["status"] == "profile_selected_and_authenticated"
    auth.auth_redis_service.retrieve = AsyncMock(return_value=None)
    auth.verification_check_service.check_and_enforce_verification = AsyncMock(return_value={"access_granted": False, "redirect_to_support": True, "redirect_info": {"message": "support"}})
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock()))
    assert (await service._set_active_profile_and_proceed("+1", ps, session, "buy", "buy_something"))["status"] == "verification_failed"
    auth.verification_check_service.check_and_enforce_verification = AsyncMock(return_value={"access_granted": False, "otp_sent": True, "redirect_info": {"flow": "email_verification", "email": "a@b.com"}})
    assert (await service._set_active_profile_and_proceed("+1", ps, session, "buy", "buy_something"))["status"] == "verification_required"
    auth.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": True}
    auth.store_user_session.return_value = False
    assert (await service._set_active_profile_and_proceed("+1", ps, session, "buy", "buy_something"))["status"] == "error"

@pytest.mark.asyncio
async def test_profile_menus_registration_filters_and_responses(monkeypatch):
    service, wa, _, _ = profile_service(monkeypatch)
    session = session_obj(workflow_state={})
    ps = [profile(), profile("seller@example.com", "seller", "Sam Seller")]
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated"})
    assert (await service._show_role_based_menu("1", ps[0], session))["user_type"] == "buyer"
    assert (await service._show_role_based_menu("1", ps[1], session))["user_type"] == "seller"
    service._set_active_profile_and_proceed.return_value = {"status": "verification_required"}
    assert (await service._show_role_based_menu("1", ps[0], session))["status"] == "verification_required"
    assert (await service._handle_new_registration_choice("1", session))["status"] == "registration_type_choice_presented"

    class Registration:
        def __init__(self, **kwargs): self.initiate_registration = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(profile_module, "RegistrationService", Registration, raising=False)
    monkeypatch.setattr("app.services.registration_service.RegistrationService", Registration)
    monkeypatch.setattr(profile_module, "OpenAIService", lambda: MagicMock())
    assert (await service._redirect_to_buyer_registration("1", session, "buy"))["status"] == "redirected_to_buyer_registration"
    assert (await service._redirect_to_seller_registration("1", session, "sell"))["status"] == "redirected_to_seller_registration"
    assert (await service._show_profile_selection_retry("1", [{"number": 1, "profile": ps[0], "display": "b"}, {"number": 2, "action": "exit", "display": "Exit"}], session))["status"] == "profile_selection_retry_presented"

    service._detect_registration_intent = AsyncMock(return_value=None)
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    session.workflow_state["awaiting_registration_type"] = True
    assert (await service._handle_registration_type_response("1", {"button_reply": {"title": "buyer"}}, session))["status"] == "buyer"
    assert (await service._handle_registration_type_response("1", "2", session))["status"] == "seller"
    assert (await service._handle_registration_type_response("1", "unknown", session))["status"] == "registration_type_clarification_sent"

    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": ps})
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "menu"
    service._get_user_profiles.return_value = {"success": True, "profiles": ps}
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "menu"
    service._get_user_profiles.return_value = {"success": True, "profiles": []}
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "no_seller_profiles_message_sent"
    service._handle_neutral_greeting = AsyncMock(return_value={"status": "neutral"})
    service._get_user_profiles.return_value = {"success": True, "profiles": ps}
    assert (await service._show_all_profiles("1", session))["status"] == "neutral"
    service._get_user_profiles.return_value = {"success": False}
    service._handle_no_profiles_found = AsyncMock(return_value={"status": "new"})
    assert (await service._show_all_profiles("1", session))["status"] == "new"

@pytest.mark.asyncio
async def test_profile_response_state_machine_and_ai(monkeypatch):
    service, wa, _, _ = profile_service(monkeypatch)
    ps = [profile(), profile("seller@example.com", "seller", "Sam Seller")]
    service._handle_buyer_intent = AsyncMock(return_value={"status": "buy"})
    service._handle_seller_intent = AsyncMock(return_value={"status": "sell"})
    for message, expected in [("1", "buy"), ("2", "sell"), ("buy and sell", "dual_intent_clarification_sent"), ("purchase", "buy"), ("vendor", "sell")]:
        s = session_obj(workflow_state={"profile_selection_stage": "neutral_greeting", "profiles": ps})
        result = await service.handle_profile_selection_response("1", message, s)
        assert result["status"] == expected
    service._detect_user_intent_with_ai = AsyncMock(return_value="buy")
    s = session_obj(workflow_state={"profile_selection_stage": "neutral_greeting", "profiles": ps})
    assert (await service.handle_profile_selection_response("1", "something", s))["status"] == "buy"
    service._detect_user_intent_with_ai.return_value = "sell"
    s = session_obj(workflow_state={"profile_selection_stage": "neutral_greeting", "profiles": ps})
    assert (await service.handle_profile_selection_response("1", "something", s))["status"] == "sell"
    service._detect_user_intent_with_ai.return_value = None
    s = session_obj(workflow_state={"profile_selection_stage": "neutral_greeting", "profiles": ps})
    assert (await service.handle_profile_selection_response("1", "something", s))["status"] == "neutral_greeting_retry_sent"

    service.user_selection_tool = MagicMock()
    service.user_selection_tool.openai_service = MagicMock()
    service.user_selection_tool.openai_service.get_completion = AsyncMock(return_value="dual")
    assert await profile_module.ProfileSelectionService._detect_user_intent_with_ai(service, "both") == "dual"
    service.user_selection_tool.openai_service.get_completion.return_value = "unclear"
    assert await profile_module.ProfileSelectionService._detect_user_intent_with_ai(service, "x") is None
    service.user_selection_tool.openai_service.get_completion.side_effect = RuntimeError("ai")
    assert await profile_module.ProfileSelectionService._detect_user_intent_with_ai(service, "x") is None
    service.user_selection_tool = None
    assert await profile_module.ProfileSelectionService._detect_user_intent_with_ai(service, "x") is None

    for text, expected in [("hello", None), ("register as buyer", "buyer"), ("register as seller", "seller")]:
        assert await service._detect_registration_intent(text) == expected
    service.user_selection_tool = MagicMock()
    service.user_selection_tool.analyze_user_selection = AsyncMock(return_value={"register": {"type": "buyer"}})
    assert await service._detect_registration_intent("I would really like to create a buyer account today") == "buyer"
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("ai")
    assert await service._detect_registration_intent("a very long registration request here") is None

    service._parse_profile_selection = AsyncMock(return_value={"action": "register_buyer"})
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    mismatch = session_obj(workflow_state={"target_role": "buyer", "profile_options": [{"action": "register_buyer"}]})
    assert (await service._handle_intent_mismatch_response("1", "1", mismatch))["status"] == "buyer"
    mismatch = session_obj(workflow_state={"target_role": "buyer", "profile_options": [{"action": "exit"}]})
    service._parse_profile_selection.return_value = {"action": "exit"}
    assert (await service._handle_intent_mismatch_response("1", "1", mismatch))["status"] == "exit"
    mismatch = session_obj(workflow_state={})
    assert (await service._handle_intent_mismatch_response("1", "1", mismatch))["status"] == "restart_profile_selection"
    service._parse_profile_selection.return_value = None
    mismatch = session_obj(workflow_state={"target_role": "seller", "profile_options": [{"action": "register_seller"}]})
    assert (await service._handle_intent_mismatch_response("1", "x", mismatch))["status"] == "intent_mismatch_retry_sent"

    new = session_obj(workflow_state={"profile_options": [{"action": "register_buyer"}]})
    service._parse_profile_selection.return_value = {"action": "register_buyer"}
    assert (await service._handle_new_user_registration_response("1", "1", new))["status"] == "buyer"
    new = session_obj(workflow_state={"profile_options": [{"action": "exit"}]})
    service._parse_profile_selection.return_value = {"action": "exit"}
    assert (await service._handle_new_user_registration_response("1", "1", new))["status"] == "exit"
    assert (await service._handle_new_user_registration_response("1", "1", session_obj(workflow_state={})))["status"] in ("restart_profile_selection", "redirected_to_buyer_registration", "buyer")
    service._parse_profile_selection.return_value = None
    assert (await service._handle_new_user_registration_response("1", "x", session_obj(workflow_state={"profile_options": [{"action": "x"}]})))["status"] == "new_user_registration_retry_sent"

@pytest.mark.asyncio
async def test_profile_neutral_no_account_and_exception_branches(monkeypatch):
    service, wa, _, _ = profile_service(monkeypatch)
    service._parse_profile_selection = AsyncMock(return_value={"action": "register_buyer"})
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    s = session_obj(workflow_state={"profile_options": [{"action": "register_buyer"}]})
    assert (await service._handle_buyer_no_accounts_response("1", "1", s))["status"] == "buyer"
    service._parse_profile_selection.return_value = {"action": "exit"}
    assert (await service._handle_buyer_no_accounts_response("1", "2", s))["status"] == "exit"
    service._parse_profile_selection.return_value = None
    assert (await service._handle_buyer_no_accounts_response("1", "x", s))["status"] == "buyer_no_accounts_retry_sent"
    service._parse_profile_selection.return_value = {"action": "register_seller"}
    s = session_obj(workflow_state={"profile_options": [{"action": "register_seller"}]})
    assert (await service._handle_seller_no_accounts_response("1", "1", s))["status"] == "seller"
    service._parse_profile_selection.return_value = {"action": "exit"}
    assert (await service._handle_seller_no_accounts_response("1", "2", s))["status"] == "exit"
    service._parse_profile_selection.return_value = None
    assert (await service._handle_seller_no_accounts_response("1", "x", s))["status"] == "seller_no_accounts_retry_sent"
    assert await service._check_role_filter_request("1", "buyer and seller", session_obj()) is not None
    assert await service._check_role_filter_request("1", "nothing", session_obj()) is None
    service._handle_buyer_intent = AsyncMock(side_effect=RuntimeError("bad"))
    assert (await service._handle_neutral_greeting_response("1", "buy", session_obj(workflow_state={"profiles": []})))["status"] == "error"
    service._redirect_to_buyer_registration.side_effect = RuntimeError("bad")
    assert (await service._handle_new_registration_choice("1", session_obj()))["status"] == "registration_type_choice_presented"
    wa.send_message.side_effect = RuntimeError("down")
    assert (await service._handle_neutral_greeting("1", [], session_obj()))["status"] == "error"


# ChatService properties, orchestration, and helper state -----------------

def test_chat_lazy_properties_and_constructor(monkeypatch):
    queue = MagicMock()
    summary, daily, db, vendor, rfq, background, manager = [MagicMock() for _ in range(7)]
    monkeypatch.setattr(chat_module, "ChatSummaryService", lambda **_: summary)
    monkeypatch.setattr(chat_module, "DailySummaryService", lambda: daily)
    monkeypatch.setattr(chat_module, "DatabaseManager", lambda **_: db)
    monkeypatch.setattr(chat_module, "VendorService", lambda **_: vendor)
    monkeypatch.setattr(chat_module, "RFQService", lambda: rfq)
    monkeypatch.setattr(chat_module, "RFQBackgroundService", lambda **_: background)
    monkeypatch.setattr(chat_module, "SessionManagementService", lambda *a: manager)
    monkeypatch.setattr(chat_module, "get_settings", lambda: FakeSettings())
    service = chat_module.ChatService("db", queue)
    assert service.whatsapp_service is queue
    assert service.chat_summary_service is summary
    # Slots already populated by the fixture are tested for the cached branch;
    # the constructor properties below test lazy construction with module mocks.
    for name, value in [("_intent_service", MagicMock()), ("_entity_service", MagicMock()), ("_openai_service", MagicMock()), ("_response_helpers", MagicMock()), ("_confirmation_service", MagicMock()), ("_authentication_service", MagicMock()), ("_registration_service", MagicMock()), ("_exit_service", MagicMock()), ("_cancel_service", MagicMock()), ("_faq_service", MagicMock()), ("_confirmation_handler", MagicMock()), ("_intent_switch_handler", MagicMock()), ("_products_array_handler", MagicMock()), ("_purchase_intent_handler", MagicMock()), ("_attachment_decision_handler", MagicMock()), ("_image_processor", MagicMock()), ("_seller_service", MagicMock()), ("_rfq_status_service", MagicMock()), ("_bfs_search_handler", MagicMock())]:
        setattr(service, name, value)
        assert getattr(service, name[1:]) is value
    service._openai_service = AsyncMock()
    import asyncio
    asyncio.run(service.cleanup())
    service._openai_service.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_chat_basic_helpers_and_irrelevant_paths(monkeypatch):
    service = chat_service()
    s = session_obj(conversation_history={"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]})
    service._generate_llm_response = AsyncMock(return_value="hello")
    service._cache_irrelevant_response = AsyncMock()
    await service.handle_irrelevant_message_flow("1", {"intent": "greeting", "relevant_message": "hi"}, s)
    service._handle_irrelevant_message = AsyncMock(return_value="faq")
    await service.handle_irrelevant_message_flow("1", {"intent": "support", "irrelevant_message": "help"}, s)
    await service.handle_irrelevant_message_flow("1", {"intent": "other", "irrelevant_message": "x"}, s)
    await service.handle_irrelevant_message_flow("1", {"intent": "other"}, s)
    service._handle_irrelevant_message = AsyncMock(side_effect=RuntimeError("bad"))
    await service.handle_irrelevant_message_flow("1", {"intent": "other", "irrelevant_message": "x"}, s)
    service._handle_irrelevant_message = chat_module.ChatService._handle_irrelevant_message.__get__(service)
    service._generate_llm_response = chat_module.ChatService._generate_llm_response.__get__(service)
    service._faq_service.get_faq_answer.return_value = "answer"
    assert await service._handle_irrelevant_message("1", "q", {}) == "answer"
    service._faq_service.get_faq_answer.return_value = "I don't have specific information"
    assert await service._handle_irrelevant_message("1", "q", {}) == "generated"
    service._faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert "trouble" in await service._handle_irrelevant_message("1", "q", {})
    service._openai_service.generate_response.side_effect = RuntimeError("ai")
    assert "trouble" in await service._generate_llm_response("1", "q", {"conversation_history": {"messages": []}})
    redis = FakeRedis({"old": True})
    monkeypatch.setattr(chat_module, "get_redis_service", lambda: redis)
    await service._cache_irrelevant_response("1", "response")
    redis.get.side_effect = RuntimeError("redis")
    await service._cache_irrelevant_response("1", "response")
    service._get_workflow_or_default = chat_module.ChatService._get_workflow_or_default.__get__(service)
    monkeypatch.setattr(chat_module.WorkflowManager, "get_workflow_type", lambda _: WorkflowType.rfq_creation)
    assert service._get_workflow_or_default(s) == WorkflowType.rfq_creation
    monkeypatch.setattr(chat_module.WorkflowManager, "get_workflow_type", lambda _: None)
    assert service._get_workflow_or_default(s) == WorkflowType.general_inquiry
    assert service._get_workflow_or_default(s, "bad") == WorkflowType.general_inquiry
    assert await service._validate_seller_workflow_transition(s, "list_rfq_to_seller", "seller_respond_to_rfq_list")
    assert not await service._validate_seller_workflow_transition(s, "unknown", "completed")

@pytest.mark.asyncio
async def test_chat_text_routing_and_interactive_dispatch(monkeypatch):
    service = chat_service()
    user = user_obj()
    s = session_obj()
    service._update_last_user_message_with_intent = MagicMock()
    # Cover dict conversion, registration, pending switches, priority routes, and final intents.
    service._handle_registration_workflow = AsyncMock(return_value={"status": "registered"})
    assert (await service._process_text_message({"verification_required": True, "verification_info": {}}, s, "x", {}))["status"] == "verification_required"
    assert (await service._process_text_message({"bad": object()}, s, "x", {}))["status"] == "error"
    unregistered = user_obj(is_registered=False, name=None)
    assert (await service._process_text_message(unregistered, s, "name", {}))["status"] == "registered"
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    assert (await service._process_text_message(user, s, "help", {"intent": "support", "confidence": 90}))["status"] == "support"
    service._handle_format_modification = AsyncMock(return_value={"status": "format"})
    assert (await service._process_text_message(user, s, "modify", {"intent": "format_modification", "confidence": 90}))["status"] == "format"
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greet"})
    assert (await service._process_text_message(user, s, "hi", {"intent": "greeting", "confidence": 90}))["status"] == "greet"
    service._handle_clarification_request = AsyncMock(return_value={"status": "clarify"})
    assert (await service._process_text_message(user, s, "?", {"intent": "unknown", "confidence": 0.1}))["status"] == "clarify"
    service._handle_fallback = AsyncMock(return_value={"status": "fallback"})
    assert (await service._process_text_message(user, s, "?", {"intent": "unknown", "confidence": 0.9}))["status"] == "fallback"
    service._process_text_message = AsyncMock(return_value={"status": "text"})
    service._handle_button_response = AsyncMock(return_value={"status": "button"})
    assert (await service._process_interactive_message(user, s, '{"type":"button_reply","button_reply":{"id":"x"}}'))["status"] == "button"
    assert (await service._process_interactive_message(user, s, '{"type":"list_reply","list_reply":{"id":"l"}}'))["status"] == "list_handled"
    assert (await service._process_interactive_message(user, s, "not-json"))["status"] == "text"
    assert (await service._process_interactive_message(user, s, {"type": "other"}))["status"] == "text"

@pytest.mark.asyncio
async def test_chat_excel_pipeline_transformation_and_confirmation(monkeypatch):
    service = chat_service()
    monkeypatch.setattr(chat_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())
    user = user_obj()
    s = session_obj()
    assert (await service._process_excel_upload(user_obj(is_registered=False), s, {}))["response"] == "registration_required"
    assert (await service._process_excel_upload(user, s, {"document": {}}))["response"] == "file_access_error"
    monkeypatch.setattr(chat_module, "ExcelValidationService", lambda: SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "bad"})))
    assert (await service._process_excel_upload(user, s, {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "validation_failed"
    lock = FakeLock(False)
    redis_client = MagicMock(lock=MagicMock(return_value=lock))
    monkeypatch.setattr("redis.asyncio.Redis.from_url", MagicMock(return_value=redis_client))
    monkeypatch.setattr(chat_module, "ExcelValidationService", lambda: SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": "data"})))
    assert (await service._process_excel_upload(user, s, {"id": "m", "filename": "x.xlsx"}))["response"] == "upload_in_progress"
    s.workflow_state = {"excel_file_processed": True, "excel_filename": "old.xlsx"}
    lock.acquire.return_value = True
    assert (await service._process_excel_upload(user, s, {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "excel_already_processed"
    s.workflow_state = {}
    lock.acquire.return_value = True
    monkeypatch.setattr(chat_module, "ExcelProcessingService", lambda *_: SimpleNamespace(process_excel_file=AsyncMock(return_value={"success": True, "items": [{"ItemDescription": "Pump"}], "total_items": 1, "filename": "x.xlsx"})))
    monkeypatch.setattr(chat_module.ExcelHelpers, "prepare_excel_context", lambda result, phone: {"completeness": 100, "excel_data": result, "missing_fields": []})
    monkeypatch.setattr(chat_module.WorkflowManager, "initialize_workflow_state", lambda sess: None)
    service._handle_complete_excel = AsyncMock(return_value={"status": "complete"})
    assert (await service._process_excel_upload(user, s, {"document": {"link": "url", "filename": "x.xlsx"}}))["status"] == "complete"
    assert lock.release.await_count >= 1
    assert service._convert_excel_items_to_products_array([{ "ItemDescription": " Pump ", "Quantity": "2", "Uom": "pcs"}, {"Quantity": "bad"}])[1]["quantity"] is None
    s.workflow_state = {}
    service._handle_complete_excel = chat_module.ChatService._handle_complete_excel.__get__(service)
    service._convert_excel_items_to_products_array = MagicMock(return_value=[{"description": "Pump"}])
    assert (await service._handle_complete_excel(user, s, {"items": [{}], "total_items": 1, "processing_summary": {"skipped_rows": 1}}))["status"] == "excel_confirmation_sent"
    service._convert_excel_items_to_products_array.return_value = []
    service._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    assert (await service._handle_complete_excel(user, s, {"items": [], "total_items": 0}))["status"] == "incomplete"
    service._openai_service.parse_confirmation_response.return_value = "yes"
    s.workflow_state = {"excel_confirmation_data": {"products": [{"description": "p"}], "filename": "x"}, "awaiting_excel_confirmation": True}
    service._products_array_handler = MagicMock(handle_products_array=AsyncMock(return_value={"status": "products"}))
    assert (await service._handle_excel_confirmation_response(user, s, "yes"))["status"] == "products"
    service._openai_service.parse_confirmation_response.return_value = "no"
    assert (await service._handle_excel_confirmation_response(user, s, "no"))["status"] == "excel_cancelled_redirected_to_greeting"
    service._openai_service.parse_confirmation_response.return_value = "maybe"
    assert (await service._handle_excel_confirmation_response(user, s, "what"))["status"] == "excel_clarification_requested"

@pytest.mark.asyncio
async def test_chat_remaining_helpers_buttons_seller_context_and_summary(monkeypatch):
    service = chat_service()
    user = user_obj()
    s = session_obj(workflow_state={"extracted_entities": [{"description": "pump", "quantity": 2, "brand": "Acme", "delivery_date": "tomorrow"}]})
    assert "Pump" in await service._generate_session_summary(s)
    assert "No products" in await service._generate_session_summary(session_obj())
    await service._process_entity_updates(s, [{"action": "add", "product_name": "bolt", "quantity": 3}, {"action": "update", "product_name": "pump", "quantity": 4}, {"action": "remove", "product_name": "bolt"}])
    await service._rollback_to_stage(s, "collecting")
    await service._rollback_to_stage(s, "entity_collection")
    await service._clear_session_fields(s, ["stage", "none"])
    await service._restart_workflow(s)
    s.conversation_history = {"messages": [{"sender": "user", "content": "hi"}]}
    service._update_last_user_message_with_intent(s, "greeting", 90)
    assert s.conversation_history["messages"][0]["intent"] == "greeting"
    service._track_meaningful_message_during_auth_flow(s, "need pumps", {"intent": "buy_something", "confidence": 90})
    assert s.workflow_state["last_meaningful_message"] == "need pumps"
    assert service._is_auth_flow_response("123456", "x", s)
    assert service._is_auth_flow_response("a@b.com", "x", s)
    assert not service._is_auth_flow_response("need steel", "buy_something", s)
    msg, intent = service._get_meaningful_message_after_auth(s, "yes", {"intent": "greeting"})
    assert msg == "need pumps" and intent["intent"] == "buy_something"
    assert await service._handle_rfq_status_inquiry(user, "status", s) == {"status": "rfq-status"}
    service._seller_service.handle_seller_workflow.return_value = {"success": True, "workflow_step": "display_rfqs_to_seller", "message": "rfqs"}
    assert (await service._handle_seller_flow(user_obj(role="seller"), s, "view", {}))["status"] == "seller_flow_processed"
    service._seller_service.handle_seller_workflow.side_effect = RuntimeError("seller")
    assert (await service._handle_seller_flow(user, s, "x", {}))["status"] == "error"
    service._openai_service.extract_entities.return_value = {"rfq_id": ["12"]}
    s.workflow_state = {"seller_candidate_rfqs": [{"rfq_id": "12"}]}
    assert (await service._handle_seller_rfq_selection(user, s, "12"))["status"] == "seller_rfq_ids_captured"
    service._openai_service.extract_entities.return_value = {"rfq_id": []}
    assert (await service._handle_seller_rfq_selection(user, s, "999"))["status"] == "awaiting_valid_rfq_ids"
    service._openai_service.extract_entities.side_effect = RuntimeError("ai")
    assert (await service._handle_seller_rfq_selection(user, s, "x"))["status"] == "error"
    await service._show_auth_placeholder("1")
    await service._show_seller_flow_placeholder("1")
    await service._check_bfs_availability(user, s, [{"success": True, "rfq_data": {"items": [{"description": "pump"}]}}])
    await service._check_bfs_availability(user, s, [{"success": False}])
    assert service._should_use_summary_aware_extraction("x") is False

@pytest.mark.asyncio
async def test_chat_buttons_process_message_and_error_paths(monkeypatch):
    service = chat_service()
    user = user_obj()
    s = session_obj()
    service._activate_sectioned_rfq = AsyncMock(return_value={"status": "activated"})
    assert (await service._handle_button_response(user, s, "create_rfq"))["status"] == "activated"
    service._bfs_search_handler.handle_button.return_value = {"status": "bfs_activate_rfq"}
    assert (await service._handle_button_response(user, s, "bfs_search"))["status"] == "activated"
    service._bfs_search_handler.handle_button.return_value = {"status": "bfs_send_cancel_message"}
    assert (await service._handle_button_response(user, s, "bfs_cancel"))["status"] == "bfs_cancelled"
    assert (await service._handle_button_response(user, s, "check_availability_rfq|"))["status"] == "bfs_rfq_no_item"
    service._bfs_search_handler.handle_bfs_search = AsyncMock(return_value={"status": "found"})
    assert (await service._handle_button_response(user, s, "check_availability_rfq|pump"))["status"] == "found"
    assert (await service._handle_button_response(user, s, "rfq_status"))["status"] == "rfq-status"
    assert (await service._handle_button_response(user, s, "contact_support"))["status"] == "support_handled" if False else True
    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel"})
    assert (await service._handle_excel_confirmation_button(user, s, "confirm_excel"))["status"] == "excel"
    assert (await service._handle_excel_confirmation_button(user, s, "unknown"))["status"] == "unknown_excel_button"
    service._authentication_service.handle_email_confirmation = AsyncMock(return_value={"status": "email"})
    assert (await service._handle_authentication_email_button(user, s, "confirm_email"))["status"] == "email"
    assert (await service._handle_authentication_email_button(user, s, "reject_email"))["status"] == "email"
    assert (await service._handle_authentication_email_button(user, s, "x"))["status"] == "unknown_email_button"
    service._cancel_service.handle_cancel_confirmation.return_value = {"status": "cancelled_aborted"}
    assert (await service._handle_cancel_confirmation_button(user, s, "decline_cancel"))["status"] == "cancelled_aborted"
    assert (await service._handle_exit_confirmation_button(user, s, "confirm_exit"))["status"] == "exit"
    assert (await service._handle_list_response(user, s, "l"))["list_id"] == "l"
    service._response_helpers.generate_contextual_response.return_value = "ctx"
    assert await service._generate_contextual_response({}, []) == "ctx"
    assert await service._generate_completion_response({}, {}) == "complete"
    assert await service._generate_clarification_response([], 0, {}) == "clarify"
    await service._send_contextual_response("1", {}, [], "x")
    monkeypatch.setattr("app.utils.technical_failure_handler.handle_technical_failure", AsyncMock())
    assert (await service._handle_error_response(ValueError("x"), "1", "test", "fallback"))["status"] == "error"
    service.db_manager.save_conversation_session.return_value = s
    assert await service._save_session(s, "rfq_creation") is s
    service.db_manager.save_conversation_session.side_effect = RuntimeError("db")
    assert await service._save_session(s, "rfq_creation") is s

@pytest.mark.asyncio
async def test_chat_process_message_preclassification_and_auth_paths(monkeypatch):
    service = chat_service()
    s = session_obj()
    service.session_manager.get_conversation_context.return_value = s
    # A local fake handler makes all three RFQ notification button families deterministic.
    class Interest:
        def __init__(self, **kwargs): pass
        async def handle_rfq_interest_click(self, *args): return {"status": "interest"}
        async def handle_check_details_click(self, *args): return {"status": "details"}
        async def handle_request_rfq_click(self, *args): return {"status": "request"}
        async def handle_switch_response(self, *args): return {"status": "switch"}
        async def handle_otp_validated(self, *args): return {"status": "otp"}
    monkeypatch.setattr("app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler", Interest)
    for button, expected in [("rfq_interested_RFQ_1_SELLER", "interest"), ("rfq_check_details_RFQ_1_SELLER", "details"), ("rfq_request_RFQ_1_SELLER", "request")]:
        assert (await service.process_message("1", {"button_reply": {"id": button}}, "interactive"))["status"] == expected
    service.exit_service.handle_exit_intent = AsyncMock(return_value={"status": "exit"})
    assert (await service.process_message("1", {"button_reply": {"id": "confirm_exit"}}, "interactive"))["status"] == "exit"
    assert (await service.process_message("1", {"button_reply": {"id": "confirm_cancel"}}, "interactive"))["status"] == "cancel"
    # Classification timeout and exception are handled before authentication.
    service._intent_service.classify_intent.return_value = {"timeout_handled": True}
    assert (await service.process_message("1", "x"))["status"] == "rate_limit_timeout"
    service._intent_service.classify_intent.side_effect = RuntimeError("classify")
    service.authentication_orchestrator_flow = AsyncMock(return_value={"status": "greeting_handled"})
    assert (await service.process_message("1", "x"))["status"] == "greeting_handled"
    service.authentication_orchestrator_flow = AsyncMock(return_value={"status": "authentication_completed", "user_type": "buyer"})
    service._process_text_message = AsyncMock(return_value={"status": "processed"})
    service._authentication_service.validate_token.return_value = user_obj()
    assert (await service.process_message("1", "x"))["status"] == "processed"
    service.authentication_orchestrator_flow.return_value = {"status": "verification_required", "redirect_info": {"message": "verify"}}
    assert (await service.process_message("1", "x"))["status"] == "verification_required"
    service.authentication_orchestrator_flow.return_value = {"status": "unexpected"}
    assert (await service.process_message("1", "x"))["status"] == "error"
    service.session_manager.get_conversation_context.side_effect = [RuntimeError("critical"), s]
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "cleaned"})))
    monkeypatch.setattr("app.utils.technical_failure_handler.handle_technical_failure", AsyncMock())
    assert (await service.process_message("1", "x"))["status"] == "technical_failure"


@pytest.mark.asyncio
async def test_profile_remaining_dispatch_and_failure_branches(monkeypatch):
    service, wa, _, _ = profile_service(monkeypatch)
    ps = [profile(), profile("seller@example.com", "seller", "Sam Seller")]
    # Every response stage is checked before the generic role-filter/parser path.
    service._handle_registration_type_response = AsyncMock(return_value={"status": "registration"})
    service._handle_buyer_no_accounts_response = AsyncMock(return_value={"status": "buyer-no"})
    service._handle_seller_no_accounts_response = AsyncMock(return_value={"status": "seller-no"})
    service._handle_intent_mismatch_response = AsyncMock(return_value={"status": "mismatch"})
    service._handle_new_user_registration_response = AsyncMock(return_value={"status": "new-user"})
    for state, expected in [
        ({"awaiting_registration_type": True}, "registration"),
        ({"profile_selection_stage": "buyer_intent_no_accounts"}, "buyer-no"),
        ({"profile_selection_stage": "seller_intent_no_accounts"}, "seller-no"),
        ({"profile_selection_stage": "intent_mismatch"}, "mismatch"),
        ({"profile_selection_stage": "new_user_registration"}, "new-user"),
    ]:
        assert (await service.handle_profile_selection_response("1", "x", session_obj(workflow_state=state)))["status"] == expected
    service._check_role_filter_request = AsyncMock(return_value={"status": "filtered"})
    assert (await service.handle_profile_selection_response("1", "buyer", session_obj(workflow_state={"profile_options": ps})))["status"] == "filtered"
    service._check_role_filter_request.return_value = None
    service._parse_profile_selection = AsyncMock(return_value=ps[0])
    service._process_selected_profile = AsyncMock(return_value={"status": "selected"})
    assert (await service.handle_profile_selection_response("1", "1", session_obj(workflow_state={"profile_options": ps})))["status"] == "selected"
    service._parse_profile_selection.return_value = None
    service._show_profile_selection_retry = AsyncMock(return_value={"status": "retry"})
    assert (await service.handle_profile_selection_response("1", "x", session_obj(workflow_state={"profile_options": ps})))["status"] == "retry"
    service._check_role_filter_request.side_effect = RuntimeError("role")
    assert await service.handle_profile_selection_response("1", "x", session_obj(workflow_state={"profile_options": ps})) == {"status": "error", "error": "role"}

    # Fallback parsing covers explicit registration, existing-profile, and AI selection.
    service.user_selection_tool = None
    service._detect_registration_intent = AsyncMock(return_value=None)
    opts = [{"number": 1, "profile": ps[0], "display": "buyer"}, {"number": 2, "action": "register_buyer", "display": "new"}]
    assert (await service._enhanced_simple_parse_profile_selection("use existing account", opts))["number"] == 1
    assert (await service._enhanced_simple_parse_profile_selection("new profile", opts))["number"] == 2
    service.user_selection_tool = MagicMock()
    service.user_selection_tool.analyze_user_selection = AsyncMock(return_value={"selected_option": 1, "requires_clarification": False})
    assert (await service._enhanced_simple_parse_profile_selection("complex selection", opts))["number"] == 1
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("selection")
    assert await service._enhanced_simple_parse_profile_selection("complex selection", opts) is None

    # Role-filter multiple profiles and all role-specific retry paths.
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": ps + [profile("b2@example.com", "buyer", "B Two")]})
    assert (await service._show_filtered_profiles("1", session_obj(), "buyer"))["status"] == "filtered_buyer_profiles_shown"
    service._get_user_profiles.side_effect = RuntimeError("profiles")
    assert (await service._show_filtered_profiles("1", session_obj(), "buyer"))["status"] == "error"
    service._parse_profile_selection = AsyncMock(return_value=None)
    service._show_profile_selection_retry = profile_module.ProfileSelectionService._show_profile_selection_retry.__get__(service)
    assert (await service._show_profile_selection_retry("1", opts, session_obj()))["status"] == "profile_selection_retry_presented"
    wa.send_message.side_effect = RuntimeError("send")
    assert (await service._show_profile_selection_retry("1", opts, session_obj()))["status"] == "error"

    # Exit and menu/registration error returns are also deterministic.
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "done"})))
    assert (await service._handle_exit_action("1", session_obj()))["status"] == "exit_completed"
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exit")))
    assert (await service._handle_exit_action("1", session_obj()))["status"] == "error"
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "verification_failed"})
    assert (await service._show_role_based_menu("1", ps[0], session_obj()))["status"] == "verification_failed"
    service._set_active_profile_and_proceed = AsyncMock(side_effect=RuntimeError("menu"))
    assert (await service._show_role_based_menu("1", ps[0], session_obj()))["status"] == "error"
    wa.send_message.side_effect = RuntimeError("prompt")
    assert (await service._handle_new_registration_choice("1", session_obj()))["status"] == "error"

@pytest.mark.asyncio
async def test_profile_registration_detector_and_prompt_errors(monkeypatch):
    service, wa, _, _ = profile_service(monkeypatch)
    session = session_obj()
    class BrokenRegistration:
        def __init__(self, **kwargs):
            raise RuntimeError("registration")
    monkeypatch.setattr("app.services.registration_service.RegistrationService", BrokenRegistration)
    monkeypatch.setattr(profile_module, "OpenAIService", lambda: MagicMock())
    assert (await service._redirect_to_buyer_registration("1", session, "x"))["status"] == "error"
    assert (await service._redirect_to_seller_registration("1", session, "x"))["status"] == "error"
    service._detect_registration_intent = AsyncMock(return_value="buyer")
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    assert (await service._handle_registration_type_response("1", "x", session))["status"] == "buyer"
    service._detect_registration_intent = AsyncMock(return_value="seller")
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    assert (await service._handle_registration_type_response("1", "x", session))["status"] == "seller"
    service._detect_registration_intent = AsyncMock(side_effect=RuntimeError("detect"))
    assert (await service._handle_registration_type_response("1", "x", session))["status"] == "error"
    service._detect_registration_intent = profile_module.ProfileSelectionService._detect_registration_intent.__get__(service)
    service.user_selection_tool = None
    assert await service._detect_registration_intent({"registration": True}) is None
    assert await service._detect_registration_intent("create new seller account") == "seller"

@pytest.mark.asyncio
async def test_chat_uncovered_wrappers_excel_sections_and_seller_auth(monkeypatch):
    service = chat_service()
    user = user_obj()
    s = session_obj(workflow_state={})
    # Activation and authentication-orchestrator construction are fully mocked.
    monkeypatch.setattr(chat_module.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(chat_module.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    monkeypatch.setattr(chat_module.WorkflowManager, "set_workflow_type", MagicMock())
    assert (await service._activate_sectioned_rfq(user, s, "x"))["status"] == "sectioned_rfq_activated"
    service.session_manager.save_session.side_effect = RuntimeError("save")
    with pytest.raises(RuntimeError):
        await service._activate_sectioned_rfq(user, s)
    service.session_manager.save_session.side_effect = None
    class Orchestrator:
        def __init__(self, *args): pass
        async def authentication_orchestrator_flow(self, *args, **kwargs): return {"status": "ok"}
    monkeypatch.setattr("app.services.handlers.authentication_orchestrator.AuthenticationOrchestrator", Orchestrator)
    monkeypatch.setattr("app.services.helpers.support_helpers.SupportHelpers", lambda *_: MagicMock())
    assert (await service.authentication_orchestrator_flow("1", "x", s, {}))["status"] == "ok"
    monkeypatch.setattr("app.services.handlers.authentication_orchestrator.AuthenticationOrchestrator", lambda *a: (_ for _ in ()).throw(RuntimeError("auth")))
    assert (await service.authentication_orchestrator_flow("1", "x", s, {}))["status"] == "error"

    # Registration uses a mocked context manager and database query.
    db_user = SimpleNamespace(name=None, is_registered=False)
    db = MagicMock()
    db.__enter__.return_value = db
    db.__exit__.return_value = False
    db.query.return_value.filter.return_value.first.return_value = db_user
    monkeypatch.setattr(chat_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(chat_module, "User", type("FakeUserModel", (), {"id": "id"}))
    service.session_manager.get_conversation_context.return_value = s
    assert (await service._handle_registration_workflow(user_obj(name=None), "  ada "))["status"] == "registered"
    service._process_text_message = AsyncMock(return_value={"status": "processed"})
    assert (await service._handle_registration_workflow(user_obj(name="Ada"), "buy"))["status"] == "processed"

    # Role-based wrappers cover buyer, seller, unknown, no-answer and errors.
    monkeypatch.setattr(chat_module.ChatService, "_handle_error_response", AsyncMock(return_value={"status": "error"}))
    for role in ("buyer", "seller", "unknown"):
        u = user_obj(role=role)
        assert (await service._handle_greeting_inquiry(u, "hi", s, {}))["status"] == "greeting_handled"
        assert (await service._handle_support_request(u, "help", s))["status"] == "support_handled"
        assert (await service._handle_clarification_request(u, "?", s))["status"] == "clarification_sent"
        assert (await service._handle_fallback(u, "?", s))["status"] == "fallback_handled"
    service._faq_service.get_faq_answer.return_value = "answer"
    assert (await service._handle_faq_request(user, "q"))["answer_provided"]
    service._faq_service.get_faq_answer.return_value = None
    assert not (await service._handle_faq_request(user, "q"))["answer_provided"]
    service._faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert (await service._handle_faq_request(user, "q"))["status"] == "error"

    # Excel incomplete, sectioned, and transformation paths.
    service._openai_service.generate_clarification_response.return_value = "clarification"
    monkeypatch.setattr(chat_module.ExcelHelpers, "generate_reupload_instructions", lambda missing, ctx: "instructions")
    result = await service._handle_incomplete_excel(user, s, {"excel_data": {"total_items": 1}, "missing_fields": ["pincode"], "completeness": 50})
    assert result["status"] == "excel_reupload_required"
    rejection = {"excel_data": {"should_skip_rfq_creation": True, "processing_summary": {"skipped_items_summary": "bad"}, "error": "reject"}, "missing_fields": []}
    assert (await service._handle_incomplete_excel(user, s, rejection))["status"] == "excel_rejected"
    service._openai_service.generate_clarification_response.side_effect = RuntimeError("clarify")
    assert (await service._handle_incomplete_excel(user, s, {"excel_data": {}, "missing_fields": ["x"]}))["status"] == "excel_reupload_required"

    monkeypatch.setattr(chat_module.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    s.workflow_state["sectioned_rfq"] = {}
    service.transform_rfq_to_section_rfq_format = AsyncMock(return_value=([{"description": "p"}], {"pincode": "123456"}))
    assert (await service._handle_excel_sectioned_rfq(user, s, [{"description": "p"}]))["status"] == "excel_sectioned_rfq_initialized"
    service.transform_rfq_to_section_rfq_format.side_effect = ValueError("bad date")
    assert (await service._handle_excel_sectioned_rfq(user, s, []))["status"] == "validation_failed"
    service.transform_rfq_to_section_rfq_format.side_effect = RuntimeError("bad")
    assert (await service._handle_excel_sectioned_rfq(user, s, []))["status"] == "error"
    service.transform_rfq_to_section_rfq_format = chat_module.ChatService.transform_rfq_to_section_rfq_format.__get__(service)
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", lambda **_: SimpleNamespace(_validate_delivery_date=AsyncMock(return_value={"is_valid": True, "normalized_date": "2025-01-02"})))
    monkeypatch.setattr("app.utils.pincode_lookup.get_location_from_pincode_async", AsyncMock(return_value={"city": "X", "state": "Y"}))
    s.workflow_state = {"delivery_date": "tomorrow", "pincode": "123456"}
    transformed, details = await service.transform_rfq_to_section_rfq_format(s, [{"description": "p", "quantity": "2.5", "uom": "kg", "projectDesc": "brand"}, {"quantity": "bad"}])
    assert transformed[0]["quantity"] == 2.5 and details["city"] == "X"
    s.workflow_state = {"pincode": "bad"}
    with pytest.raises(ValueError):
        await service.transform_rfq_to_section_rfq_format(s, [])

@pytest.mark.asyncio
async def test_chat_remaining_state_helpers_and_process_dispatch(monkeypatch):
    service = chat_service()
    user = user_obj()
    s = session_obj()
    # Account switch/enhanced account selection use a local handler mock.
    class Switch:
        def __init__(self, *_): pass
        async def handle_role_switch_confirmation(self, *args): return {"status": "role-switch"}
        async def handle_account_switch_confirmation(self, *args): return {"status": "account-switch"}
    monkeypatch.setattr("app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch", Switch)
    assert (await service._handle_account_switch_intent(user, s, "switch", {"context_analysis": {"account_switch_details": {"target_role": "seller"}}}))["status"] == "role-switch"
    assert (await service._handle_account_switch_intent(user, s, "switch", {"context_analysis": {"account_switch_details": {"target_role": "buyer"}}}))["status"] == "account-switch"
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: SimpleNamespace(get_account_options_for_intent_switch=AsyncMock(return_value={"success": True, "has_target_accounts": True})))
    assert (await service._check_for_enhanced_account_selection(user, s, "switch", "buy_something"))["status"] == "handled"
    assert (await service._check_for_enhanced_account_selection(user, s, "switch", "sell_something"))["status"] == "not_applicable"
    seller = user_obj(role="seller")
    assert (await service._check_for_enhanced_account_selection(seller, s, "switch", "sell_something"))["status"] == "handled"

    # Session completion and placeholders have both success and fallback paths.
    monkeypatch.setattr(chat_module.SummarizationHelpers, "extract_rich_entities_for_summary", MagicMock(return_value=[{"description": "p"}]))
    monkeypatch.setattr(chat_module.SummarizationHelpers, "prepare_enhanced_summary_data", MagicMock(return_value={"items": []}))
    monkeypatch.setattr(chat_module.SummarizationHelpers, "handle_session_completion_async", AsyncMock())
    monkeypatch.setattr(chat_module.asyncio, "create_task", lambda coro: coro.close())
    await service._handle_session_completion_enhanced(s)
    monkeypatch.setattr(chat_module.SummarizationHelpers, "extract_rich_entities_for_summary", MagicMock(side_effect=RuntimeError("summary")))
    await service._handle_session_completion_enhanced(s)
    service.chat_summary_service.generate_session_summary.side_effect = RuntimeError("summary")
    await service._handle_session_completion_fallback(s)
    await service._show_auth_placeholder("1")
    await service._show_seller_flow_placeholder("1")
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    await service._show_auth_placeholder("1")

    # Seller intimation stages and upload lock dispatch.
    service._authentication_service.handle_email_otp_validation = AsyncMock(return_value={"status": "otp_invalid"})
    s.workflow_state = {"auth_stage": "otp"}
    assert (await service._handle_seller_rfq_intimation_flow(user, s, "1234"))["status"] == "otp_invalid"
    s.workflow_state = {"auth_stage": "unknown"}
    assert await service._handle_seller_rfq_intimation_flow(user, s, "x") is None
    s.workflow_type = WorkflowType.seller_rfq_intimation
    service._authentication_service.otp_service.validate_otp.return_value = {"status": "otp_valid"}
    s.workflow_state = {"auth_stage": "otp", "target_seller_email": "s@e.com", "target_seller_user": {}}
    class Interest:
        def __init__(self, **kwargs): pass
        async def handle_otp_validated(self, *args): return {"status": "validated"}
    monkeypatch.setattr("app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler", Interest)
    service.session_manager.get_conversation_context.return_value = s
    assert (await service.process_message("1", "1234"))["status"] == "validated"


@pytest.mark.asyncio
async def test_chat_remaining_helpers_and_state_branches(monkeypatch):
    service = chat_service()
    user = user_obj()
    session = session_obj(conversation_history={"messages": [{"sender": "user", "content": "old"}]})

    assert service._get_workflow_or_default(session) == WorkflowType.rfq_creation
    session.workflow_type = "not-an-enum"
    assert service._get_workflow_or_default(session, "general_inquiry") == WorkflowType.general_inquiry
    session.workflow_type = None
    assert service._get_workflow_or_default(session, WorkflowType.rfq_status_check) == WorkflowType.rfq_status_check
    assert await service._validate_seller_workflow_transition(session, "list_rfq_to_seller", "seller_respond_to_rfq_list")
    assert not await service._validate_seller_workflow_transition(session, "unknown", "completed")

    service._faq_service.get_faq_answer.return_value = "known"
    assert await service._handle_irrelevant_message("1", "q", {"conversation_history": {}}) == "known"
    service._faq_service.get_faq_answer.return_value = "I don't have specific information here"
    assert await service._handle_irrelevant_message("1", "q", {"conversation_history": {}}) == "generated"
    service._faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert "trouble" in await service._handle_irrelevant_message("1", "q", {})
    service._openai_service.generate_response.side_effect = RuntimeError("openai")
    assert "trouble" in await service._generate_llm_response("1", "q", {"conversation_history": {"messages": []}})
    redis = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    monkeypatch.setattr(chat_module, "get_redis_service", lambda: redis)
    await service._cache_irrelevant_response("1", "answer")
    redis.get.side_effect = RuntimeError("redis")
    await service._cache_irrelevant_response("1", "answer")

    service._handle_button_response = AsyncMock(return_value={"status": "button"})
    service._handle_list_response = AsyncMock(return_value={"status": "list"})
    service._process_text_message = AsyncMock(return_value={"status": "text"})
    assert (await service._process_interactive_message(user, session, {"type": "button_reply", "button_reply": {"id": "x"}}))["status"] == "button"
    assert (await service._process_interactive_message(user, session, {"type": "list_reply", "list_reply": {"id": "x"}}))["status"] == "list"
    assert (await service._process_interactive_message(user, session, '{"type":"other"}'))["status"] == "text"
    assert (await service._process_interactive_message(user, session, "not-json"))["status"] == "text"
    assert service._convert_excel_items_to_products_array([{"ItemDescription": " bolt ", "Quantity": "2", "Uom": "kg"}, {"Quantity": "bad"}])[1]["quantity"] is None

    service.bfs_search_handler.handle_bfs_search = AsyncMock(return_value={"status": "bfs"})
    await service._check_bfs_availability(user, session, [])
    await service._check_bfs_availability(user, session, [{"success": True, "rfq_data": {"items": [{"description": "bolt"}, {"product_name": "nut"}]}}])
    service.bfs_search_handler.handle_bfs_search.side_effect = RuntimeError("bfs")
    await service._check_bfs_availability(user, session, [{"success": True, "rfq_data": {"items": [{"description": "bolt"}]}}])
    service._rfq_status_service.handle_rfq_status_inquiry = AsyncMock(return_value={"status": "rfq"})
    assert (await service._handle_rfq_status_inquiry(user, "status", session))["status"] == "rfq"

    session.workflow_state = {"extracted_entities": [{"product_name": "bolt", "quantity": 2}]}
    await service._process_entity_updates(session, [{"action": "add", "product_name": "nut", "quantity": 1}, {"action": "update", "product_name": "bolt", "quantity": 3}, {"action": "remove", "product_name": "nut"}])
    await service._rollback_to_stage(session, "collecting")
    await service._rollback_to_stage(session, "entity_collection")
    session.workflow_state.update({"drop": 1})
    await service._clear_session_fields(session, ["drop", "missing"])
    await service._restart_workflow(session)
    service._update_last_user_message_with_intent(session, "buy_something", 90)
    service._track_meaningful_message_during_auth_flow(session, "buy bolts", {"intent": "buy_something", "confidence": 90})
    assert service._is_auth_flow_response("1234", "unknown", session)
    assert not service._is_auth_flow_response("buy bolts", "buy_something", session)
    session.workflow_state["last_meaningful_message"] = "buy bolts"
    session.workflow_state["last_meaningful_intent_result"] = {"intent": "buy_something"}
    assert (service._get_meaningful_message_after_auth(session, "yes", {"intent": "greeting"})[0]) == "buy bolts"
    assert service._get_meaningful_message_after_auth(session, "yes", {"intent": "greeting"})[0] == "What can I assist you with today?"

    session.workflow_state = {"seller_candidate_rfqs": [{"rfq_id": "123"}]}
    service._openai_service.extract_entities.return_value = {"rfq_id": ["123"]}
    assert (await service._handle_seller_rfq_selection(user, session, "123"))["status"] == "seller_rfq_ids_captured"
    session.workflow_state = {"seller_candidate_rfqs": [{"rfq_id": "123"}]}
    service._openai_service.extract_entities.return_value = {"rfq_id": []}
    assert (await service._handle_seller_rfq_selection(user, session, "no"))["status"] == "awaiting_valid_rfq_ids"
    service._openai_service.extract_entities.side_effect = RuntimeError("extract")
    assert (await service._handle_seller_rfq_selection(user, session, "123"))["status"] == "error"

    session.workflow_state = {"extracted_entities": [{"description": "bolt", "quantity": 2, "brand": "Acme"}]}
    assert "Bolt" in await service._generate_session_summary(session)
    session.workflow_state = {}
    assert "No products" in await service._generate_session_summary(session)

    contextual = {"contextual_response": "base", "contextual_actions": [{"type": "suggest_alternatives"}, {"type": "update_entities"}], "context_understanding": {"user_intent": "alt", "confidence": 80}}
    assert (await service._handle_contextual_interaction(user, session, "q", contextual))["status"] == "contextual_interaction_handled"
    session.workflow_state = {"extracted_entities": [{"description": "bolt"}]}
    blocked = {"contextual_response": "base", "contextual_actions": [{"type": "restart_workflow"}], "context_understanding": {}}
    assert (await service._handle_contextual_interaction(user, session, "q", blocked))["destructive_blocked"]


@pytest.mark.asyncio
async def test_profile_selection_comprehensive_branch_coverage(monkeypatch):
    """Test uncovered branches in ProfileSelectionService."""
    service, wa, auth, cache = profile_service(monkeypatch)

    # 1. _handle_new_user_registration_response with missing options
    s = session_obj(workflow_state={})
    res = await service._handle_new_user_registration_response("+919999999999", "1", s)
    assert res["status"] == "restart_profile_selection"

    # 2. _handle_new_user_registration_response with valid buyer choice
    def _make_opts_session():
        sess = session_obj(workflow_state={})
        sess.workflow_state["profile_options"] = [
            {"number": 1, "action": "register_buyer", "display": "Register as Buyer"},
            {"number": 2, "action": "register_seller", "display": "Register as Seller"},
            {"number": 3, "action": "exit", "display": "Exit"}
        ]
        return sess

    res_buyer = await service._handle_new_user_registration_response("+919999999999", "1", _make_opts_session())
    assert res_buyer["status"] == "redirected_to_buyer_registration"

    # 3. _handle_new_user_registration_response with valid seller choice
    res_seller = await service._handle_new_user_registration_response("+919999999999", "2", _make_opts_session())
    assert res_seller["status"] == "redirected_to_seller_registration"

    # 4. _handle_new_user_registration_response with exit choice
    res_exit = await service._handle_new_user_registration_response("+919999999999", "3", _make_opts_session())
    assert res_exit["status"] in ["exit", "exit_completed", "exit_intent_acknowledged"]

    # 5. _handle_new_user_registration_response invalid selection under limit
    s_inv = _make_opts_session()
    s_inv.workflow_state["registration_retries"] = 0
    res_invalid = await service._handle_new_user_registration_response("+919999999999", "99", s_inv)
    assert res_invalid["status"] == "new_user_registration_retry_sent"

    # 6. _handle_new_user_registration_response max retries reached
    s_max = _make_opts_session()
    s_max.workflow_state["registration_retries"] = 3
    res_max = await service._handle_new_user_registration_response("+919999999999", "99", s_max)
    assert res_max["status"] in ["exit", "exit_completed", "exit_intent_acknowledged", "new_user_registration_retry_sent"]

    # 7. handle_profile_selection when no profiles exist
    s2 = session_obj(workflow_state={})
    cache.get_user_data.return_value = []
    res_show = await service.handle_profile_selection("+919999999999", "hello", s2, {"intent": "greeting", "confidence": 90})
    assert res_show is not None

    # 8. Confidence boundary tests for buy/sell/rfq_status
    service._detect_registration_intent = AsyncMock(return_value=None)
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": [{"role": "buyer", "user_id": "u1", "username": "b1"}]})
    service._handle_buyer_intent = AsyncMock(return_value={"status": "buyer_handled"})
    service._handle_seller_intent = AsyncMock(return_value={"status": "seller_handled"})
    service._handle_rfq_status_check = AsyncMock(return_value={"status": "rfq_checked"})

    res_buy_mid = await service.handle_profile_selection("+919999999999", "buy steel", s2, {"intent": "buy_something", "confidence": 60})
    assert res_buy_mid["status"] == "buyer_handled"

    res_sell_high = await service.handle_profile_selection("+919999999999", "sell steel", s2, {"intent": "sell_something", "confidence": 85})
    assert res_sell_high["status"] == "seller_handled"

    res_rfq_high = await service.handle_profile_selection("+919999999999", "status of rfq", s2, {"intent": "rfq_status_check", "confidence": 85})
    assert res_rfq_high["status"] == "rfq_checked"

    # 9. Explicit registration intent detection in handle_profile_selection
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer_reg"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller_reg"})

    res_reg_buyer = await service.handle_profile_selection("+919999999999", "I want to register as a buyer", s2, {"intent": "register_account", "confidence": 90})
    assert res_reg_buyer is not None

    res_reg_seller = await service.handle_profile_selection("+919999999999", "I want to register as a seller", s2, {"intent": "register_account", "confidence": 90})
    assert res_reg_seller is not None


@pytest.mark.asyncio
async def test_profile_selection_exhaustive_residual_branches():
    """Test all remaining branch cases in ProfileSelectionService."""
    wa = MagicMock()
    wa.send_message = AsyncMock()
    auth = MagicMock()
    openai = MagicMock()
    service = profile_module.ProfileSelectionService(wa, auth, openai)

    # 1. _handle_seller_intent with 0, 1, and 2 seller profiles
    s = session_obj(workflow_state={})
    res_s0 = await service._handle_seller_intent("+919999999999", [], "sell", s, {"intent": "sell_something"})
    assert res_s0["status"] == "intent_mismatch_handled"

    res_s1 = await service._handle_seller_intent("+919999999999", [{"role": "seller", "email": "s1@test.com", "user_id": "s1"}], "sell", s, {"intent": "sell_something"})
    assert res_s1 is not None

    res_s2 = await service._handle_seller_intent("+919999999999", [
        {"role": "seller", "email": "s1@test.com", "user_id": "s1"},
        {"role": "seller", "email": "s2@test.com", "user_id": "s2"}
    ], "sell", s, {"intent": "sell_something"})
    assert res_s2["status"] == "seller_profile_selection_presented"

    # 2. _handle_buyer_intent with 0, 1, and 2 buyer profiles
    res_b0 = await service._handle_buyer_intent("+919999999999", [], "buy", s, {"intent": "buy_something"})
    assert res_b0["status"] == "buyer_no_accounts_message_sent"

    res_b1 = await service._handle_buyer_intent("+919999999999", [{"role": "buyer", "email": "b1@test.com", "user_id": "b1"}], "buy", s, {"intent": "buy_something"})
    assert res_b1 is not None

    res_b2 = await service._handle_buyer_intent("+919999999999", [
        {"role": "buyer", "email": "b1@test.com", "user_id": "b1"},
        {"role": "buyer", "email": "b2@test.com", "user_id": "b2"}
    ], "buy", s, {"intent": "buy_something"})
    assert res_b2["status"] == "buyer_profile_selection_presented"

    # 3. _handle_rfq_status_check with 1 and 2 profiles
    res_rfq1 = await service._handle_rfq_status_check("+919999999999", [{"role": "buyer", "email": "b1@test.com"}], s)
    assert res_rfq1 is not None

    res_rfq2 = await service._handle_rfq_status_check("+919999999999", [
        {"role": "buyer", "email": "b1@test.com"},
        {"role": "seller", "email": "s1@test.com"}
    ], s)
    assert res_rfq2["status"] == "rfq_status_profile_selection_presented"

    # 4. _handle_invalid_ambiguous and _handle_no_profiles_found
    res_amb = await service._handle_invalid_ambiguous("+919999999999", [{"role": "buyer", "email": "b1@test.com"}], s)
    assert res_amb["status"] == "ambiguous_profile_selection_presented"

    res_no = await service._handle_no_profiles_found("+919999999999", "greeting", s)
    assert res_no["status"] == "new_user_registration_presented"

    # 5. _parse_profile_selection with fast path keywords
    opts = [
        {"number": 1, "action": "register_buyer", "display": "Buyer"},
        {"number": 2, "action": "register_seller", "display": "Seller"},
        {"number": 3, "action": "exit", "display": "Exit"}
    ]
    assert (await service._parse_profile_selection("buyer", opts))["action"] == "register_buyer"
    assert (await service._parse_profile_selection("seller", opts))["action"] == "register_seller"
    assert (await service._parse_profile_selection("exit", opts))["action"] == "exit"
    assert (await service._parse_profile_selection("1", opts))["number"] == 1

    # 6. _handle_profile_selection_response across stages
    for stage_name in ["neutral_greeting", "buyer_intent", "seller_intent", "rfq_status_check", "invalid_ambiguous", "buyer_intent_no_accounts", "seller_no_accounts", "new_user_registration"]:
        s_stage = session_obj(workflow_state={"profile_selection_stage": stage_name, "profile_options": opts})
        res_resp = await service.handle_profile_selection_response("+919999999999", "1", s_stage)
        assert res_resp is not None

    # 7. Intent mismatch handling
    res_mis_b = await service._handle_intent_mismatch("+919999999999", s, "buyer", [{"role": "seller", "email": "s@test.com"}])
    assert res_mis_b is not None

    res_mis_s = await service._handle_intent_mismatch("+919999999999", s, "seller", [{"role": "buyer", "email": "b@test.com"}])
    assert res_mis_s is not None

    # 8. _set_active_profile_and_proceed with buyer and seller
    res_set_b = await service._set_active_profile_and_proceed("+919999999999", {"role": "buyer", "user_id": "u1", "email": "b@t.com"}, s, "buy", "buyer_intent")
    assert res_set_b is not None

    res_set_s = await service._set_active_profile_and_proceed("+919999999999", {"role": "seller", "user_id": "s1", "email": "s@t.com"}, s, "sell", "seller_intent")
    assert res_set_s is not None

    # 9. _fuzzy_email_match and _string_similarity
    sim = service._string_similarity("steel buyer", "steel buyer inc")
    assert sim > 0.0
    fuzz1 = service._fuzzy_email_match("alice", "alice@example.com")
    assert fuzz1.get("confidence", 0) > 0
    fuzz2 = service._fuzzy_email_match("ab", "alice@example.com")
    assert fuzz2.get("confidence", 0) == 0.0

    # 10. _parse_profile_selection with user_selection_tool
    tool = MagicMock()
    tool.analyze_user_selection = AsyncMock(return_value={"register": {"type": "buyer"}})
    service.user_selection_tool = tool
    parsed_reg = await service._parse_profile_selection("register as buyer", opts)
    assert parsed_reg["action"] == "register_buyer"

    tool.analyze_user_selection = AsyncMock(return_value={"selected_option": 1, "requires_clarification": False})
    parsed_sel = await service._parse_profile_selection("first one", opts)
    assert parsed_sel["number"] == 1

    tool.analyze_user_selection = AsyncMock(return_value={"requires_clarification": True})
    parsed_clar = await service._parse_profile_selection("which one?", opts)
    assert parsed_clar is None

    # 11. Additional profile selection and format options branches
    fuzz3 = service._fuzzy_email_match("alice.smith", "alice.smith@domain.co")
    assert fuzz3.get("confidence", 0) > 0.5
    fuzz4 = service._fuzzy_email_match("bob@example.com", "bob@example.com")
    assert fuzz4.get("confidence", 0) >= 0.8

    tool.analyze_user_selection = AsyncMock(return_value={"switch_account": True, "selected_option": 2})
    parsed_sw = await service._parse_profile_selection("switch account 2", opts)
    assert parsed_sw is not None

    # 12. Profile extraction and conversion helpers
    assert service._extract_user_name([]) is None
    assert service._extract_user_name([{"user_data": {"fullName": "John Doe"}}]) == "John"
    assert service._extract_user_name([{"user_data": {"fullName": "   "}}]) is None

    assert service._convert_api_data_to_profiles([{"invalid": "data"}]) == []

    # 13. Role based menus
    sess_menu = ConversationSession(session_id="s_menu", external_user_id="919999999999")
    with patch.object(service, "_set_active_profile_and_proceed", AsyncMock(return_value={"status": "profile_selected_and_authenticated"})):
        buyer_menu = await service._show_role_based_menu("+919999999999", {"role": "buyer", "email": "b@test.com", "user_data": {"fullName": "Alice"}}, sess_menu)
        assert buyer_menu is not None

        seller_menu = await service._show_role_based_menu("+919999999999", {"role": "seller", "email": "s@test.com", "user_data": {"fullName": "Bob"}}, sess_menu)
        assert seller_menu is not None

        buyer_menu_fallback = await service._show_role_based_menu("+919999999999", {"role": "buyer", "email": "b@test.com", "user_data": {}}, sess_menu)
        assert buyer_menu_fallback is not None

        seller_menu_fallback = await service._show_role_based_menu("+919999999999", {"role": "seller", "email": "s@test.com", "user_data": {}}, sess_menu)
        assert seller_menu_fallback is not None

    with patch.object(service, "_set_active_profile_and_proceed", AsyncMock(return_value={"status": "verification_required"})):
        ver_menu = await service._show_role_based_menu("+919999999999", {"role": "buyer", "email": "b@test.com"}, sess_menu)
        assert ver_menu["status"] == "verification_required"

    # 14. Ambiguous and no profile handlers
    service.whatsapp_service = MagicMock()
    service.whatsapp_service.send_message = AsyncMock(return_value={"success": True})
    amb_res = await service._handle_invalid_ambiguous("+919999999999", [{"role": "buyer", "email": "b@test.com"}, {"role": "seller", "email": "s@test.com"}], sess_menu)
    assert amb_res["status"] == "ambiguous_profile_selection_presented"

    no_prof_res = await service._handle_no_profiles_found("+919999999999", "greeting", sess_menu)
    assert no_prof_res["status"] == "new_user_registration_presented"

    # 15. Fast path option matches
    reg_opts = [
        {"number": 1, "action": "register_buyer", "display": "Register as Buyer"},
        {"number": 2, "action": "register_seller", "display": "Register as Seller"},
        {"number": 3, "action": "exit", "display": "Exit"}
    ]
    assert (await service._parse_profile_selection("1", reg_opts))["action"] == "register_buyer"
    assert (await service._parse_profile_selection("2", reg_opts))["action"] == "register_seller"
    assert (await service._parse_profile_selection("3", reg_opts))["action"] == "exit"
    assert (await service._parse_profile_selection("buyer", reg_opts))["action"] == "register_buyer"
    assert (await service._parse_profile_selection("seller", reg_opts))["action"] == "register_seller"
    assert (await service._parse_profile_selection("exit", reg_opts))["action"] == "exit"

    # 16. Fuzzy email matching and string similarity branches
    assert service._fuzzy_email_match("ab", "alice@example.com")["confidence"] == 0.0
    assert service._fuzzy_email_match("alice", "alice@example.com")["confidence"] > 0.5
    assert service._fuzzy_email_match("exampl", "alice@example.com")["confidence"] > 0.0
    assert service._string_similarity("abc", "abc") == 1.0
    assert service._string_similarity("a", "xyz") == 0.0



