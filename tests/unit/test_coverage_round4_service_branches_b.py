"""Deterministic branch coverage for the remaining service-level gaps.

All collaborators in this module are in-memory fakes.  No test reaches a
network, database, Redis, Celery, OpenAI, email, or WhatsApp boundary.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import app.services.profile_selection_service as profile_mod
import app.services.registration_service as registration_mod
import app.services.rfq_background_service as background_mod
import app.services.rfq_intimation_service as intimation_mod
import app.services.seller_categorization_service as categorization_mod
import app.services.seller_service as seller_mod
import app.services.session_management_service as session_mod
import app.services.user_cache_service as cache_mod
import app.services.vendor_service as vendor_mod
import app.services.whatsapp_service as whatsapp_mod
from app.models import ConversationOutcome, InteractionType, JobStatus, ResponseType, RFQStatus, WorkflowType
from app.services.whatsapp_service import MessageResponse


PHONE = "+919876543210"


def bare(cls, **attrs):
    service = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(service, name, value)
    return service


def make_session(state=None, *, outcome=None, workflow_type=None):
    return SimpleNamespace(
        session_id="session-1",
        external_user_id=PHONE,
        workflow_state=dict(state or {}),
        workflow_type=workflow_type,
        outcome=outcome,
        conversation_history={"messages": [], "metadata": [], "openai_messages": []},
        extracted_entities={},
        whatsapp_context={},
        retention_date=date(2026, 9, 1),
        created_at=datetime(2026, 8, 1),
        last_activity_at=datetime(2026, 8, 12),
        completed_at=None,
        user_type=None,
    )


def profile(email="buyer@example.com", role="buyer", name="Ada Buyer"):
    return {
        "email": email,
        "role": role,
        "name": name,
        "company": "Example Co",
        "user_data": {
            "id": email,
            "username": email,
            "fullName": name,
            "selfClient": role == "buyer",
            "companyName": "Example Co",
        },
    }


def make_profile_service():
    whatsapp = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)),
    )
    auth = SimpleNamespace(
        user_authenticate=AsyncMock(),
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock(return_value=None)),
        verification_check_service=SimpleNamespace(check_and_enforce_verification=AsyncMock()),
        store_user_session=AsyncMock(return_value=True),
    )
    service = bare(
        profile_mod.ProfileSelectionService,
        whatsapp_service=whatsapp,
        authentication_service=auth,
        user_cache_service=SimpleNamespace(get_user_data=AsyncMock(return_value=[])),
        user_selection_tool=None,
    )
    return service, whatsapp, auth


def make_registration_service():
    whatsapp = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(return_value=MessageResponse(True)),
    )
    manager = SimpleNamespace(send_and_track_message=AsyncMock(), save_session=AsyncMock())
    service = bare(
        registration_mod.RegistrationService,
        whatsapp_service=whatsapp,
        session_manager=manager,
        entity_service=SimpleNamespace(extract_entities=AsyncMock(return_value={"entities": {}})),
        openai_service=SimpleNamespace(parse_seller_product_items=AsyncMock()),
        response_helpers=MagicMock(),
        confirmation_service=SimpleNamespace(parse_confirmation=AsyncMock()),
        register_api_service=SimpleNamespace(register_buyer=AsyncMock(), register_seller=AsyncMock()),
        auth_redis_service=SimpleNamespace(store=AsyncMock(return_value=True)),
        support_notification_service=SimpleNamespace(
            notify_registration_failed=AsyncMock(),
            notify_buyer_registration_not_approved=AsyncMock(),
        ),
        authentication_helpers=MagicMock(),
        otp_service=SimpleNamespace(send_otp=AsyncMock(return_value={"status": "otp_sent"}), handle_user_message=AsyncMock()),
        auth_api_service=SimpleNamespace(authenticate_user=AsyncMock()),
        settings=SimpleNamespace(
            support_contact_info="support@example.com",
            procucev_rfq_details_url="https://example.test/profile",
            categorization_init_timeout_seconds=20.0,
            categorization_item_timeout_seconds=15.0,
        ),
    )
    return service, whatsapp, manager


class QueryFake:
    def __init__(self, *, rows=None, first=None, scalar=0, count=0, delete_count=0):
        self.rows = list(rows or [])
        self.first_value = first
        self.scalar_value = scalar
        self.count_value = count
        self.delete_value = delete_count
        self.deleted = False

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def distinct(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.first_value

    def scalar(self):
        return self.scalar_value

    def count(self):
        return self.count_value

    def delete(self):
        self.deleted = True
        return self.delete_value


# Profile selection ---------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_selection_remaining_entry_routes_and_prompt_failures():
    service, whatsapp, _ = make_profile_service()
    session = make_session()
    service._detect_registration_intent = AsyncMock(return_value="buyer")
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    assert (await service.handle_profile_selection("1", {"button_reply": {"title": "register"}}, session, {"intent": "greeting"}))["status"] == "buyer"

    service._detect_registration_intent.return_value = "seller"
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    assert (await service.handle_profile_selection("1", "register", session, {"intent": "greeting"}))["status"] == "seller"

    service._detect_registration_intent.return_value = None
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": [profile()]})
    service._handle_neutral_greeting = AsyncMock(return_value={"status": "neutral"})
    assert (await service.handle_profile_selection("1", "hello", session, {"intent": "register_account"}))["status"] == "neutral"

    service._detect_registration_intent.side_effect = RuntimeError("detector")
    assert (await service.handle_profile_selection("1", "hello", session, {}))["status"] == "error"

    service._detect_registration_intent.side_effect = None
    service._detect_registration_intent.return_value = None
    service._get_user_profiles = AsyncMock(return_value={"success": False})
    assert (await service.handle_profile_selection("1", "hello", session, {"intent": "greeting"}))["status"] == "new_user_registration_presented"

    profiles = [profile(), profile("seller@example.com", "seller", "Sam Seller")]
    assert (await service._handle_seller_intent("1", profiles, "sell", session, {}))["status"] == "seller_profile_selection_presented"
    service._handle_intent_mismatch = AsyncMock(return_value={"status": "mismatch"})
    assert await service._handle_seller_intent("1", [profiles[0]], "sell", session, {}) == {"status": "mismatch"}

    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "selected"})
    assert await service._handle_rfq_status_check("1", [profiles[0]], session) == {"status": "selected"}
    assert (await service._handle_rfq_status_check("1", profiles, session))["options_count"] == 2
    assert (await service._handle_invalid_ambiguous("1", profiles, session))["options_count"] == 2
    assert (await service._handle_no_profiles_found("1", "greeting", session))["status"] == "new_user_registration_presented"

    whatsapp.send_message.side_effect = RuntimeError("send")
    assert (await service._handle_no_profiles_found("1", "greeting", session))["status"] == "error"


@pytest.mark.asyncio
async def test_profile_selection_parser_fuzzy_and_fallback_branches():
    service, _, _ = make_profile_service()
    options = [{"number": 1, "profile": profile(), "display": "buyer@example.com"}, {"number": 2, "action": "register_seller", "display": "Seller"}]

    assert (await service._parse_profile_selection("1", options))["number"] == 1
    assert (await service._parse_profile_selection("new profile", options))["action"] == "register_seller"
    assert (await service._parse_profile_selection("use existing account", options))["number"] == 1
    assert await service._parse_profile_selection("nonsense", options) is None

    assert (service._fuzzy_email_match("x", "buyer@example.com"))["confidence"] == 0
    assert (service._fuzzy_email_match("buyerx", "buyer@example.com"))["confidence"] > 0.6
    assert (service._fuzzy_email_match("examplecom", "buyer@example.com"))["confidence"] > 0.4
    assert (service._fuzzy_email_match("buyerexample", "buyer@example.com"))["confidence"] >= 0
    assert (service._fuzzy_email_match(None, "buyer@example.com"))["confidence"] == 0
    assert service._string_similarity("", "x") == 0
    assert service._string_similarity("abc", "abc") == 1
    assert service._string_similarity("ab", "abcdef") > 0
    assert service._string_similarity("xyz", "abc") == 0

    service.user_selection_tool = MagicMock()
    service.user_selection_tool.analyze_user_selection = AsyncMock(return_value={"register": {"type": "buyer"}})
    assert (await service._parse_profile_selection("register", options))["registration_type"] == "buyer"
    service.user_selection_tool.analyze_user_selection.return_value = {"selected_option": 1, "requires_clarification": False}
    assert (await service._parse_profile_selection("one", options))["number"] == 1
    service.user_selection_tool.analyze_user_selection.return_value = {"selected_option": 9, "requires_clarification": False}
    assert await service._parse_profile_selection("nine", options) is None
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("AI")
    assert await service._parse_profile_selection("complex", options) is None

    service.user_selection_tool.analyze_user_selection.side_effect = None
    service.user_selection_tool.analyze_user_selection.return_value = {"selected_option": 1, "requires_clarification": False}
    assert (await service._enhanced_simple_parse_profile_selection("complex", options))["number"] == 1
    service.user_selection_tool.analyze_user_selection.side_effect = RuntimeError("AI")
    assert await service._enhanced_simple_parse_profile_selection("complex", options) is None


@pytest.mark.asyncio
async def test_profile_selection_response_actions_and_verification_edges(monkeypatch):
    service, whatsapp, auth = make_profile_service()
    profiles = [profile(), profile("seller@example.com", "seller", "Sam Seller")]
    session = make_session({"profile_selection_stage": "buyer_intent", "original_message": "buy", "original_intent": {}})

    real_set_active_profile_and_proceed = service._set_active_profile_and_proceed
    real_show_role_based_menu = service._show_role_based_menu
    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated", "redirect_to_main_flow": True})
    assert (await service._process_selected_profile("1", {"profile": profiles[0]}, session))["status"] == "buyer_options_presented"
    session.workflow_state["profile_selection_stage"] = "seller_intent"
    assert (await service._process_selected_profile("1", {"profile": profiles[1]}, session))["status"] == "seller_options_presented"
    session.workflow_state["profile_selection_stage"] = "rfq_status_check"
    assert (await service._process_selected_profile("1", {"profile": profiles[0]}, session))["status"] == "profile_selected_and_authenticated"
    session.workflow_state["profile_selection_stage"] = "other"
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._process_selected_profile("1", {"profile": profiles[0]}, session))["status"] == "menu"
    service._set_active_profile_and_proceed = real_set_active_profile_and_proceed
    service._show_role_based_menu = real_show_role_based_menu

    service._handle_new_registration_choice = AsyncMock(return_value={"status": "choice"})
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._show_all_profiles = AsyncMock(return_value={"status": "all"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    assert (await service._process_selected_profile("1", {"action": "register_new"}, session))["status"] == "choice"
    assert (await service._process_selected_profile("1", {"action": "register_buyer"}, session))["status"] == "buyer"
    assert (await service._process_selected_profile("1", {"action": "register_seller"}, session))["status"] == "seller"
    assert (await service._process_selected_profile("1", {"action": "show_all_profiles"}, session))["status"] == "all"
    assert (await service._process_selected_profile("1", {"action": "exit"}, session))["status"] == "exit"
    assert (await service._process_selected_profile("1", {"registration_type": "buyer"}, session))["status"] == "buyer"
    assert (await service._process_selected_profile("1", {"registration_type": "seller"}, session))["status"] == "seller"
    assert (await service._process_selected_profile("1", {}, session))["status"] == "error"

    auth.auth_redis_service.retrieve.return_value = None
    auth.verification_check_service.check_and_enforce_verification.return_value = {
        "access_granted": False,
        "otp_sent": True,
        "redirect_info": {"flow": "email_verification", "email": "buyer@example.com"},
    }
    assert (await service._set_active_profile_and_proceed("+1", profiles[0], session, "buy", "buy_something"))["status"] == "verification_required"
    auth.verification_check_service.check_and_enforce_verification.return_value = {
        "access_granted": False,
        "redirect_to_support": True,
        "redirect_info": {"message": "support", "reason": "blocked"},
    }
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock()))
    assert (await service._set_active_profile_and_proceed("+1", profiles[0], session, "buy", "buy_something"))["status"] == "verification_failed"
    auth.verification_check_service.check_and_enforce_verification.return_value = {"access_granted": True}
    auth.store_user_session.return_value = False
    assert (await service._set_active_profile_and_proceed("+1", profiles[0], session, "buy", "buy_something"))["status"] == "error"
    auth.store_user_session.return_value = True
    assert (await service._set_active_profile_and_proceed("+1", profiles[0], session, "buy", "buy_something"))["status"] == "profile_selected_and_authenticated"

    service._set_active_profile_and_proceed = AsyncMock(return_value={"status": "profile_selected_and_authenticated"})
    assert (await service._show_role_based_menu("1", profiles[0], make_session()))["user_type"] == "buyer"
    assert (await service._show_role_based_menu("1", profiles[1], make_session()))["user_type"] == "seller"
    service._set_active_profile_and_proceed.return_value = {"status": "verification_required"}
    assert (await service._show_role_based_menu("1", profiles[0], make_session()))["status"] == "verification_required"


@pytest.mark.asyncio
async def test_profile_selection_registration_filters_and_state_machine(monkeypatch):
    service, whatsapp, _ = make_profile_service()
    session = make_session({"awaiting_registration_type": True})
    service._detect_registration_intent = AsyncMock(return_value=None)
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    assert (await service._handle_registration_type_response("1", {"button_reply": {"title": "buyer"}}, session))["status"] == "buyer"
    session.workflow_state["awaiting_registration_type"] = True
    assert (await service._handle_registration_type_response("1", "2", session))["status"] == "seller"
    session.workflow_state["awaiting_registration_type"] = True
    assert (await service._handle_registration_type_response("1", "unclear", session))["status"] == "registration_type_clarification_sent"

    profiles = [profile(), profile("seller@example.com", "seller", "Sam Seller"), profile("buyer2@example.com", "buyer", "B Two")]
    service._get_user_profiles = AsyncMock(return_value={"success": True, "profiles": profiles})
    service._show_role_based_menu = AsyncMock(return_value={"status": "menu"})
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "filtered_buyer_profiles_shown"
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "menu"
    assert (await service._show_filtered_profiles("1", session, "buyer"))["status"] == "filtered_buyer_profiles_shown"
    service._get_user_profiles.return_value = {"success": True, "profiles": []}
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "no_seller_profiles_message_sent"
    service._get_user_profiles.return_value = {"success": False}
    service._handle_no_profiles_found = AsyncMock(return_value={"status": "new"})
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "new"
    service._get_user_profiles.side_effect = RuntimeError("cache")
    assert (await service._show_filtered_profiles("1", session, "seller"))["status"] == "error"

    service._get_user_profiles.side_effect = None
    service._get_user_profiles.return_value = {"success": True, "profiles": profiles}
    service._handle_neutral_greeting = AsyncMock(return_value={"status": "neutral"})
    assert (await service._show_all_profiles("1", session))["status"] == "neutral"
    service._get_user_profiles.return_value = {"success": False}
    assert (await service._show_all_profiles("1", session))["status"] == "new"

    service._handle_buyer_intent = AsyncMock(return_value={"status": "buy"})
    service._handle_seller_intent = AsyncMock(return_value={"status": "sell"})
    for message, expected in (("1", "buy"), ("2", "sell"), ("buy and sell", "dual_intent_clarification_sent"), ("purchase", "buy"), ("vendor", "sell")):
        state = make_session({"profile_selection_stage": "neutral_greeting", "profiles": profiles})
        assert (await service.handle_profile_selection_response("1", message, state))["status"] == expected
    service._detect_user_intent_with_ai = AsyncMock(return_value="buy")
    assert (await service.handle_profile_selection_response("1", "something", make_session({"profile_selection_stage": "neutral_greeting", "profiles": profiles})))["status"] == "buy"
    service._detect_user_intent_with_ai.return_value = "sell"
    assert (await service.handle_profile_selection_response("1", "something", make_session({"profile_selection_stage": "neutral_greeting", "profiles": profiles})))["status"] == "sell"
    service._detect_user_intent_with_ai.return_value = None
    assert (await service.handle_profile_selection_response("1", "something", make_session({"profile_selection_stage": "neutral_greeting", "profiles": profiles})))["status"] == "neutral_greeting_retry_sent"

    service._parse_profile_selection = AsyncMock(return_value={"action": "register_buyer"})
    service._redirect_to_buyer_registration.return_value = {"status": "buyer"}
    buyer_no = make_session({"profile_selection_stage": "buyer_intent_no_accounts", "profile_options": [{"action": "register_buyer"}]})
    assert (await service.handle_profile_selection_response("1", "1", buyer_no))["status"] == "buyer"
    service._parse_profile_selection.return_value = None
    assert (await service.handle_profile_selection_response("1", "x", make_session({"profile_selection_stage": "buyer_intent_no_accounts", "profile_options": []})))["status"] == "buyer_no_accounts_retry_sent"
    seller_no = make_session({"profile_selection_stage": "seller_intent_no_accounts", "profile_options": [{"action": "register_seller"}]})
    service._parse_profile_selection.return_value = {"action": "register_seller"}
    assert (await service.handle_profile_selection_response("1", "1", seller_no))["status"] == "seller"

    service._parse_profile_selection.return_value = None
    mismatch = make_session({"profile_selection_stage": "intent_mismatch", "target_role": "seller", "profile_options": [{"action": "register_seller"}]})
    assert (await service.handle_profile_selection_response("1", "x", mismatch))["status"] == "intent_mismatch_retry_sent"
    new_user = make_session({"profile_selection_stage": "new_user_registration", "profile_options": [{"action": "register_buyer"}]})
    assert (await service.handle_profile_selection_response("1", "x", new_user))["status"] == "new_user_registration_retry_sent"

    assert await service._check_role_filter_request("1", "hello", make_session()) is None
    service._show_filtered_profiles = AsyncMock(return_value={"status": "filtered"})
    assert (await service._check_role_filter_request("1", "buyer", make_session()))["status"] == "filtered"
    assert (await service._check_role_filter_request("1", "seller", make_session()))["status"] == "filtered"


@pytest.mark.asyncio
async def test_profile_selection_redirect_exit_and_detector_edges(monkeypatch):
    service, whatsapp, _ = make_profile_service()

    class Registration:
        def __init__(self, **kwargs):
            self.initiate_registration = AsyncMock(return_value={"status": "started"})

    monkeypatch.setattr("app.services.registration_service.RegistrationService", Registration)
    monkeypatch.setattr(profile_mod, "OpenAIService", lambda: MagicMock())
    session = make_session()
    assert (await service._redirect_to_buyer_registration("1", session, "buy"))["status"] == "redirected_to_buyer_registration"
    assert (await service._redirect_to_seller_registration("1", session, "sell"))["status"] == "redirected_to_seller_registration"

    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "done"})))
    assert (await service._handle_exit_action("1", session))["status"] == "exit_completed"
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("exit")))
    assert (await service._handle_exit_action("1", session))["status"] == "error"

    service.user_selection_tool = MagicMock()
    service.user_selection_tool.openai_service = MagicMock()
    service.user_selection_tool.openai_service.get_completion = AsyncMock(return_value="dual")
    assert await service._detect_user_intent_with_ai("both") == "dual"
    service.user_selection_tool.openai_service.get_completion.return_value = "unclear"
    assert await service._detect_user_intent_with_ai("x") is None
    service.user_selection_tool.openai_service.get_completion.side_effect = RuntimeError("AI")
    assert await service._detect_user_intent_with_ai("x") is None
    service.user_selection_tool = None
    assert await service._detect_user_intent_with_ai("x") is None
    assert await service._detect_registration_intent("register as buyer") == "buyer"
    assert await service._detect_registration_intent("register as seller") == "seller"
    assert await service._detect_registration_intent("ordinary question") is None

    service._parse_profile_selection = AsyncMock(return_value={"action": "register_buyer"})
    service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer"})
    service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit"})
    mismatch = make_session({"target_role": "buyer", "profile_options": [{"action": "register_buyer"}]})
    assert (await service._handle_intent_mismatch_response("1", "1", mismatch))["status"] == "buyer"
    mismatch = make_session({"target_role": "buyer", "profile_options": [{"action": "exit"}]})
    service._parse_profile_selection.return_value = {"action": "exit"}
    assert (await service._handle_intent_mismatch_response("1", "1", mismatch))["status"] == "exit"
    assert (await service._handle_intent_mismatch_response("1", "1", make_session()))["status"] == "restart_profile_selection"

    service._parse_profile_selection.return_value = None
    mismatch = make_session({"target_role": "seller", "profile_options": [{"action": "register_seller"}]})
    assert (await service._handle_intent_mismatch_response("1", "x", mismatch))["status"] == "intent_mismatch_retry_sent"
    assert (await service._handle_new_user_registration_response("1", "x", make_session()))["status"] == "restart_profile_selection"
    assert (await service._handle_new_user_registration_response("1", "x", make_session({"profile_options": []})))["status"] == "restart_profile_selection"


# Registration --------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_entry_context_confirmation_and_exit_edges(monkeypatch):
    service, whatsapp, manager = make_registration_service()
    session = make_session({"user_type": "buyer"})
    service.authentication_helpers.generate_registration_message = Mock(return_value="intro")
    assert (await service.initiate_registration("1", session, "buyer"))["status"] == "registration_initiated"
    manager.send_and_track_message.reset_mock()
    service.session_manager = None
    assert (await service.initiate_registration("1", session, "seller"))["user_type"] == "seller"
    whatsapp.send_message.side_effect = RuntimeError("send")
    service._redirect_to_support = AsyncMock(return_value={"status": "support"})
    assert (await service.initiate_registration("1", session, "buyer"))["status"] == "support"

    service.session_manager = manager
    session.conversation_history = {"messages": [{"content": " old "}, {"content": 5}, {"content": ""}]}
    assert service._build_registration_context(session, " current ") == "old current"
    assert service._build_registration_context(session, {"button_reply": {"id": "confirm"}}) == "old confirm"
    broken = SimpleNamespace(conversation_history=None)
    assert service._build_registration_context(broken, "text") == "text"

    service._check_exit_command = AsyncMock(return_value=True)
    service._handle_registration_exit = AsyncMock(return_value={"status": "exit"})
    assert (await service.handle_registration_data_collection("1", "exit", session))["status"] == "exit"
    service._check_exit_command.return_value = False
    service.entity_service.extract_entities.return_value = {"entities": {"name": "Ada"}}
    service._auto_fill_address_from_pincode = AsyncMock()
    service.authentication_helpers.validate_entities = AsyncMock(return_value=({"name": "Ada"}, "email missing"))
    with patch.object(registration_mod.AuthenticationHelpers, "get_missing_fields", return_value=["email"]):
        service._generate_contextual_registration_questions = AsyncMock(return_value="question")
        result = await service.handle_registration_data_collection("1", "Ada", session)
    assert result["status"] == "data_collection_in_progress"

    service.entity_service.extract_entities.return_value = {"entities": {"name": "Ada", "email": "a@example.com"}}
    service.authentication_helpers.validate_entities.return_value = ({"name": "Ada", "email": "a@example.com"}, None)
    service._send_confirmation_with_buttons = AsyncMock()
    with patch.object(registration_mod.AuthenticationHelpers, "get_missing_fields", return_value=[]):
        result = await service.handle_registration_data_collection("1", "complete", session)
    assert result["status"] == "awaiting_confirmation"

    service.session_manager = None
    whatsapp.send_configurable_buttons.return_value = MessageResponse(False, error="buttons")
    await service._send_confirmation_with_buttons("1", {"name": "Ada"}, "buyer", session)
    assert whatsapp.send_message.await_count >= 1
    service.session_manager = manager
    await service._send_clarification_with_buttons("1", session)
    whatsapp.send_configurable_buttons.return_value = MessageResponse(False, error="buttons")
    await service._send_clarification_with_buttons("1", session)
    assert manager.send_and_track_message.await_count >= 1

    service._check_exit_command = AsyncMock(return_value=True)
    service._handle_registration_exit = AsyncMock(return_value={"status": "exit"})
    assert (await service.handle_registration_confirmation("1", "exit", session))["status"] == "exit"
    service._check_exit_command.return_value = False
    service._parse_button_response = Mock(return_value="no")
    service.authentication_helpers.generate_registration_message = Mock(return_value="restart")
    assert (await service.handle_registration_confirmation("1", "restart", session))["status"] == "registration_restarted"
    service._parse_button_response.return_value = None
    service.confirmation_service.parse_confirmation.return_value = None
    service._send_clarification_with_buttons = AsyncMock()
    assert (await service.handle_registration_confirmation("1", "maybe", session))["status"] == "awaiting_confirmation"

    assert registration_mod.RegistrationService._parse_button_response(service, {"button_reply": {"id": "confirm_registration"}}) == "yes"
    assert registration_mod.RegistrationService._parse_button_response(service, {"button_reply": {"id": "restart"}}) == "no"
    assert registration_mod.RegistrationService._parse_button_response(service, {"button_reply": {}}) is None
    assert registration_mod.RegistrationService._parse_button_response(service, "unknown") is None
    assert registration_mod.RegistrationService._parse_button_response(service, 4) is None


@pytest.mark.asyncio
async def test_registration_submission_and_product_categorization_edges(monkeypatch):
    service, whatsapp, manager = make_registration_service()
    session = make_session({"user_type": "buyer"})
    real_categorize_seller_products = service._categorize_seller_products
    service.authentication_helpers.build_registration_payload_dynamic = Mock(return_value={"name": "Ada", "email": "a@example.com"})
    service.register_api_service.register_buyer.return_value = {"statusCode": "200", "data": {"userId": "u1", "orgId": "o1"}}
    entities = {"name": "Ada", "email": "a@example.com"}
    result = await service._submit_registration("1", session, entities, "buyer")
    assert result["status"] == "registration_completed" and entities["user_id"] == "u1"

    service.authentication_helpers.build_registration_payload_dynamic.return_value = {"details": "pumps"}
    service._categorize_seller_products = AsyncMock(return_value=[{"category": "Pumps", "division": ""}])
    service.register_api_service.register_seller.return_value = {"type": {"id": "s1", "orgId": "o1"}, "status": "Success"}
    seller_result = await service._submit_registration("1", session, {"products_services": "pumps"}, "seller")
    assert seller_result["user_type"] == "seller"

    cancel = SimpleNamespace(_clear_workflow_state=AsyncMock())
    monkeypatch.setattr("app.services.cancel_service.CancelService", lambda *a, **k: cancel)
    service.register_api_service.register_buyer.return_value = {"statusCode": 409, "message": "already exists"}
    session.workflow_state["user_type"] = "buyer"
    assert (await service._submit_registration("1", session, entities, "buyer"))["status"] == "user_already_exists"
    service.register_api_service.register_buyer.return_value = {"statusCode": 500, "message": "bad"}
    service._redirect_to_support = AsyncMock(return_value={"status": "support"})
    assert (await service._submit_registration("1", session, entities, "buyer"))["status"] == "support"
    service.register_api_service.register_buyer.side_effect = RuntimeError("api")
    assert (await service._submit_registration("1", session, entities, "buyer"))["status"] == "support"

    service._categorize_seller_products = real_categorize_seller_products
    service.openai_service.parse_seller_product_items.return_value = {"success": False, "items": []}
    assert await service._categorize_seller_products("pumps", "1", session) == []
    service.openai_service.parse_seller_product_items.return_value = {"success": True, "items": ["pump", "valve"]}
    monkeypatch.setattr(
        "app.services.auto_categorization_service.get_auto_categorization_service_async",
        AsyncMock(side_effect=RuntimeError("model")),
    )
    assert await service._categorize_seller_products("pumps", "1", session) == []

    # A cold model load that overruns its budget must degrade to no categorization
    # rather than leaving the seller without a reply.
    monkeypatch.setattr(
        "app.services.auto_categorization_service.get_auto_categorization_service_async",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    )
    assert await service._categorize_seller_products("pumps", "1", session) == []

    cat = SimpleNamespace(categorize_item=AsyncMock(side_effect=[{"success": True, "category": "Pumps", "confidence_score": .8, "method": "ai"}, {"success": False, "error": "bad"}]))
    monkeypatch.setattr(
        "app.services.auto_categorization_service.get_auto_categorization_service_async",
        AsyncMock(return_value=cat),
    )
    result = await service._categorize_seller_products("pumps", "1", session)
    assert result == [{"category": "Pumps", "division": ""}]
    cat.categorize_item.side_effect = RuntimeError("item")
    assert await service._categorize_seller_products("pumps", "1", session) == []

    service.auth_redis_service.store.return_value = False
    assert not await service.store_user_session("+1", SimpleNamespace(dict=Mock(return_value={"id": "u"})))
    service.auth_redis_service.store.side_effect = RuntimeError("redis")
    assert not await service.store_user_session("+1", SimpleNamespace(dict=Mock(return_value={"id": "u"})))
    service.auth_redis_service.store.side_effect = None
    service.auth_redis_service.store.return_value = True
    assert await service._store_user_session_after_registration("1", {"name": "Ada", "email": "a@example.com"}, "buyer")
    service.auth_redis_service.store.return_value = False
    assert not await service._store_user_session_after_registration("1", {"name": "Ada"}, "seller")

    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "exit"})))
    service.session_manager = manager
    assert (await registration_mod.RegistrationService._redirect_to_support(service, "1", "bad", "details", session))["status"] == "redirected_to_support"
    service.session_manager = None
    whatsapp.send_message.side_effect = RuntimeError("send")
    assert (await registration_mod.RegistrationService._redirect_to_support(service, "1", "bad", "details", session))["status"] == "error"
    assert await service._check_exit_command({"button_reply": {"id": "exit"}})
    assert not await service._check_exit_command({"button_reply": {}})
    assert await service._check_exit_command(" cancel ")
    assert not await service._check_exit_command(1)


@pytest.mark.asyncio
async def test_registration_pincode_otp_and_confirmation_success_edges(monkeypatch):
    service, whatsapp, manager = make_registration_service()
    session = make_session({"user_type": "buyer", "pending_registration_data": {"user_id": "u1", "email": "a@example.com", "name": "Ada"}})
    entities = session.workflow_state["pending_registration_data"]
    monkeypatch.setattr(registration_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "MH"}))
    await service._auto_fill_address_from_pincode({"zipCode": "411001"}, "1", session)
    assert await service._auto_fill_address_from_pincode({"zipCode": "bad"}, "1", session) is None
    await service._auto_fill_address_from_pincode({"zipCode": "411001", "address1": "Existing"}, "1", session)
    monkeypatch.setattr(registration_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    invalid = {"zipCode": "411002"}
    await service._auto_fill_address_from_pincode(invalid, "1", session)
    assert "_pincode_error" in invalid
    monkeypatch.setattr(registration_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    await service._auto_fill_address_from_pincode({"zipCode": "411003"}, "1", session)

    service.otp_service.handle_user_message.return_value = {"status": "redirect_to_support", "reason": "invalid"}
    service._redirect_to_support = AsyncMock(return_value={"status": "support"})
    assert (await service.handle_registration_otp_validation("1", "x", session))["status"] == "support"
    service.otp_service.handle_user_message.return_value = {"status": "max_otp_exceeded"}
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "exit"})))
    assert (await service.handle_registration_otp_validation("1", "x", session))["status"] == "exit"

    service.otp_service.handle_user_message.return_value = {"status": "otp_valid"}
    service.auth_api_service.authenticate_user.return_value = {"success": False}
    service._store_user_session_after_registration = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *a, **k: SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "exit"})))
    assert (await service.handle_registration_otp_validation("1", "1234", session))["status"] == "redirect_to_support"

    session = make_session({"user_type": "seller", "pending_registration_data": {"email": "s@example.com", "name": "Sam"}, "current_intent_result": {"intent": "sell_something", "original_message": "sell"}})
    service.auth_api_service.authenticate_user.return_value = {"success": True, "data": [{"id": "s1", "username": "s@example.com", "fullName": "Sam", "selfClient": False, "approved": True}]}
    service.store_user_session = AsyncMock(return_value=True)
    assert (await service.handle_registration_otp_validation("1", "1234", session))["user_type"] == "seller"

    session = make_session({"user_type": "buyer", "pending_registration_data": {"user_id": "u1", "email": "a@example.com", "name": "Ada"}})
    service.auth_api_service.authenticate_user.return_value = {"success": True, "data": [{"id": "u1", "username": "a@example.com", "fullName": "Ada", "selfClient": True, "approved": True}]}
    service.store_user_session = AsyncMock(return_value=True)
    with patch("app.services.verification_check_service.VerificationCheckService", return_value=SimpleNamespace(_check_domain_approval=AsyncMock(return_value={"approved": True}))):
        assert (await service.handle_registration_otp_validation("1", "1234", session))["status"] == "buyer_options_presented"


# RFQ intimation ------------------------------------------------------------


def make_intimation():
    db = MagicMock()
    service = bare(
        intimation_mod.RFQIntimationService,
        db_session=db,
        settings=SimpleNamespace(support_contact_info="support@example.com", contact_email="help@example.com"),
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        opt_out_service=SimpleNamespace(check_seller_notification_eligibility=AsyncMock()),
        mock_procurev=SimpleNamespace(),
        subscription_plans={"basic": {"price": 499, "credits": 5}, "pro": {"price": 999, "credits": 15}},
        conversation_timeout_minutes=5,
    )
    return service, db


def seller_record(*, credits=2):
    return SimpleNamespace(seller_id="seller-1", seller_name="Seller", phone_number="919999999999", email="seller@example.com", subscription_credits=credits)


@pytest.mark.asyncio
async def test_rfq_intimation_notification_subscription_and_lazy_procurev(monkeypatch):
    service, db = make_intimation()
    service.mock_procurev = None
    fake_procurev = MagicMock()
    monkeypatch.setattr("app.services.procucev_service.MockProcucevService", lambda: fake_procurev)
    assert service._get_mock_procurev() is fake_procurev

    service._get_seller_details = AsyncMock(return_value=None)
    assert (await service.send_rfq_notification("seller-1", {"rfq_id": "R1"}))["error"] == "Seller not found"
    service._get_seller_details.return_value = seller_record()
    service.opt_out_service.check_seller_notification_eligibility.return_value = {"eligible": False, "reason": "opted out"}
    assert (await service.send_rfq_notification("seller-1", {"rfq_id": "R1"}))["error"] == "opted out"
    service.opt_out_service.check_seller_notification_eligibility.return_value = {"eligible": True}
    service._generate_rfq_brief = AsyncMock(return_value="Brief")
    service._create_credited_seller_message = AsyncMock(return_value="message")
    service.whatsapp_service.send_message.return_value = MessageResponse(False, error="down")
    assert "WhatsApp delivery failed" in (await service.send_rfq_notification("seller-1", {"rfq_id": "R1"}))["error"]

    service.whatsapp_service.send_message.return_value = MessageResponse(True, message_id="m1")
    service._record_notification = AsyncMock()
    service._record_interaction = AsyncMock()
    service._start_conversation_timeout = AsyncMock()
    success = await service.send_rfq_notification("seller-1", {"rfq_id": "R1", "rfq_title": "Bolts", "categories": ["Tools"], "quantity_info": "10"})
    assert success["success"] and success["next_step"] == "rfq_id_request"
    service._get_seller_details.return_value = seller_record(credits=0)
    service._create_uncredited_seller_message = AsyncMock(return_value="subscribe")
    success = await service.send_rfq_notification("seller-1", {"rfq_id": "R1"})
    assert success["next_step"] == "subscription_selection"

    service._get_seller_details.return_value = seller_record()
    service._get_mock_procurev = Mock(return_value=SimpleNamespace(generate_payment_link=AsyncMock(return_value={"success": False, "error": "payment"})))
    assert "Payment link generation failed" in (await service.handle_subscription_selection("seller-1", "R1", "basic"))["error"]
    assert (await service.handle_subscription_selection("seller-1", "R1", "unknown"))["error"] == "Invalid subscription plan"
    service._get_mock_procurev.return_value.generate_payment_link.return_value = {"success": True, "payment_link": "https://pay", "payment_id": "p1"}
    service.whatsapp_service.send_message.return_value = MessageResponse(False, error="down")
    assert (await service.handle_subscription_selection("seller-1", "R1", "basic"))["error"] == "Failed to send payment message"
    service.whatsapp_service.send_message.return_value = MessageResponse(True, message_id="m")
    service._record_interaction = AsyncMock()
    assert (await service.handle_subscription_selection("seller-1", "R1", "basic"))["success"]
    service._get_seller_details.return_value = None
    assert (await service.handle_subscription_selection("seller-1", "R1", "basic"))["error"] == "Seller not found"


@pytest.mark.asyncio
async def test_rfq_intimation_id_email_timeout_and_database_edges():
    service, db = make_intimation()
    seller = seller_record()
    service._get_seller_details = AsyncMock(return_value=None)
    assert (await service.handle_rfq_id_request("s", "R", "R"))["error"] == "Seller not found"
    service._get_seller_details.return_value = seller
    service._validate_rfq_id = AsyncMock(return_value=False)
    assert (await service.handle_rfq_id_request("s", "R", "R"))["error"] == "Invalid RFQ ID"
    service._validate_rfq_id.return_value = True
    seller.subscription_credits = 0
    assert (await service.handle_rfq_id_request("s", "R", "R"))["error"] == "Insufficient credits"
    seller.subscription_credits = 2
    service._get_mock_procurev = Mock(return_value=SimpleNamespace(send_rfq_email=AsyncMock(return_value={"success": False, "error": "mail"})))
    assert "Email sending failed" in (await service.handle_rfq_id_request("s", "R", "R"))["error"]
    service._get_mock_procurev.return_value.send_rfq_email.return_value = {"success": True, "email_id": "e1"}
    service._deduct_seller_credit = AsyncMock()
    service.whatsapp_service.send_message.return_value = MessageResponse(False, error="down")
    assert (await service.handle_rfq_id_request("s", "R", "R"))["error"] == "Failed to send confirmation message"
    service.whatsapp_service.send_message.return_value = MessageResponse(True, message_id="m")
    service._record_interaction = AsyncMock()
    service._update_notification_response = AsyncMock()
    assert (await service.handle_rfq_id_request("s", "R", "R"))["success"]

    service._get_seller_details.return_value = None
    assert (await service.handle_conversation_timeout("s", "R"))["error"] == "Seller not found"
    service._get_seller_details.return_value = seller
    service._get_mock_procurev = Mock(return_value=SimpleNamespace(get_seller_pending_bids=AsyncMock(return_value={"success": True, "pending_bids": [{"rfq_title": "A", "rfq_id": "R1", "deadline": "tomorrow"}, {"rfq_id": "R2"}, {"rfq_id": "R3"}, {"rfq_id": "R4"}]})))
    service.whatsapp_service.send_message.return_value = MessageResponse(True, message_id="m")
    service._record_interaction = AsyncMock()
    service._update_notification_response = AsyncMock()
    result = await service.handle_conversation_timeout("s", "R")
    assert result["pending_bids_count"] == 4
    service._get_mock_procurev.return_value.get_seller_pending_bids.return_value = {"success": True, "pending_bids": []}
    assert (await service.handle_conversation_timeout("s", "R"))["pending_bids_count"] == 0
    service._get_mock_procurev.return_value.get_seller_pending_bids.return_value = {"success": False}
    assert (await service.handle_conversation_timeout("s", "R"))["pending_bids_count"] == 0
    service.whatsapp_service.send_message.return_value = MessageResponse(False, error="down")
    assert (await service.handle_conversation_timeout("s", "R"))["error"] == "Failed to send timeout reminder"

    assert (await service._generate_rfq_brief({})).startswith("RFQ Available")
    assert await service._generate_rfq_brief({"rfq_title": "A", "categories": ["X"], "quantity_info": "2"}) == "A - X (2)"
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(rfq_id="R")
    assert await service._validate_rfq_id("R")
    db.query.return_value.filter.return_value.first.side_effect = [None, SimpleNamespace(rfq_id="R")]
    assert await service._validate_rfq_id("R")
    db.query.return_value.filter.return_value.first.side_effect = [seller, None]
    await service._deduct_seller_credit("s")
    seller.subscription_credits = 0
    db.query.return_value.filter.return_value.first.side_effect = [seller]
    await service._deduct_seller_credit("s")
    service._start_conversation_timeout = intimation_mod.RFQIntimationService._start_conversation_timeout.__get__(service)
    captured = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(intimation_mod.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(intimation_mod.asyncio, "create_task", lambda coro: captured.append(coro))
    service.handle_conversation_timeout = AsyncMock()
    await service._start_conversation_timeout("s", "R")
    await captured[0]
    service.handle_conversation_timeout.assert_awaited_once_with("s", "R")
    monkeypatch.undo()


# RFQ background ------------------------------------------------------------


def make_background():
    service = bare(
        background_mod.RFQBackgroundService,
        db_session=MagicMock(),
        settings=SimpleNamespace(),
        recommendation_service=SimpleNamespace(select_sellers_for_rfq=AsyncMock()),
        intimation_service=SimpleNamespace(send_rfq_notification=AsyncMock()),
        max_concurrent_rfqs=5,
        max_concurrent_notifications=10,
        notification_batch_size=50,
        retry_attempts=3,
        _active_jobs={},
        _job_stats={"rfqs_processed": 0, "sellers_notified": 0, "notifications_sent": 0, "errors_occurred": 0},
    )
    return service


@pytest.mark.asyncio
async def test_rfq_background_batch_fetch_cleanup_and_status_edges(monkeypatch):
    service = make_background()
    service.process_approved_rfq = background_mod.RFQBackgroundService.process_approved_rfq.__get__(service)
    real_fetch_rfq_data = background_mod.RFQBackgroundService._fetch_rfq_data.__get__(service)
    service._fetch_rfq_data = AsyncMock(return_value={"rfq_id": "R"})
    service.recommendation_service.select_sellers_for_rfq.return_value = {"total_selected": 0}
    assert (await service.process_approved_rfq("R"))["reason"] == "No qualifying sellers found"
    service._fetch_rfq_data.return_value = None
    assert not (await service.process_approved_rfq("missing"))["success"]

    service.process_approved_rfq = AsyncMock(side_effect=[{"success": True, "notifications_sent": 2}, RuntimeError("worker"), {"success": False, "error": "bad"}])
    result = await service.process_multiple_rfqs(["a", "b", "c"])
    assert result["successful_rfqs"] == 1 and result["failed_rfqs"] == 2 and len(result["errors"]) == 2

    service.intimation_service.send_rfq_notification.side_effect = [
        {"success": True, "message_id": "m"},
        {"success": False, "error": "down"},
        RuntimeError("worker"),
    ]
    result = await service._send_batch_notifications({"rfq_id": "R"}, [{"seller_id": "1", "seller_name": "A"}, {"seller_id": "2", "seller_name": "B"}, {"seller_id": "3", "seller_name": "C"}], "job")
    assert result["successful"] == 1 and result["failed"] == 2

    rfq = SimpleNamespace(
        rfq_id="R", api_payload={"rfq_title": "Title", "categories": ["Medical Equipment"], "deadline": "2026-08-15"},
        status=SimpleNamespace(value="ready"), created_at=datetime.utcnow() - timedelta(days=2), external_user_id="u",
    )
    service._fetch_rfq_data = real_fetch_rfq_data
    service.db_session.query.return_value.filter.return_value.first.return_value = rfq
    result = await service._fetch_rfq_data("R")
    assert result["rfq_title"] == "Title"
    service.db_session.query.return_value.filter.return_value.first.return_value = None
    assert await service._fetch_rfq_data("R") is None
    service.db_session.query.return_value.filter.return_value.first.side_effect = RuntimeError("db")
    assert await service._fetch_rfq_data("R") is None

    qn = QueryFake(count=0)
    qi = QueryFake(count=0)
    service.db_session.query.side_effect = [qn, qi]
    assert (await service.cleanup_old_notifications())["message"] == "No old records found"
    qn = QueryFake(count=2, delete_count=2)
    qi = QueryFake(count=1, delete_count=1)
    service.db_session.query.side_effect = [qn, qi, qn, qi]
    result = await service.cleanup_old_notifications(1)
    assert result["notifications_deleted"] == 2 and result["interactions_deleted"] == 1
    service.db_session.query.side_effect = RuntimeError("db")
    assert not (await service.cleanup_old_notifications())["success"]

    pending = SimpleNamespace(rfq_id="R", api_payload={"rfq_title": "T", "categories": []}, created_at=datetime.utcnow(), external_user_id="u", status=RFQStatus.ready)
    q_pending = QueryFake(rows=[pending])
    q_count_zero = QueryFake(scalar=0)
    service.db_session.query.side_effect = [q_pending, q_count_zero]
    result = await service.get_pending_rfqs()
    assert result and result[0]["rfq_id"] == "R"
    q_pending = QueryFake(rows=[pending])
    q_count_nonzero = QueryFake(scalar=1)
    service.db_session.query.side_effect = [q_pending, q_count_nonzero]
    assert await service.get_pending_rfqs() == []
    service.db_session.query.side_effect = RuntimeError("db")
    assert await service.get_pending_rfqs() == []

    urgent = SimpleNamespace(api_payload={"deadline": "bad", "categories": ["Emergency Supplies"]}, created_at=datetime.utcnow() - timedelta(days=1))
    assert service._calculate_rfq_priority(urgent) >= 130
    service.db_session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(status=None)
    await service._update_rfq_status("R", RFQStatus.ready)
    service.db_session.query.return_value.filter.return_value.first.side_effect = RuntimeError("db")
    await service._update_rfq_status("R", RFQStatus.submitted)
    service._active_jobs = {"j": {"rfq_id": "R", "status": "processing", "stage": "x", "started_at": datetime.utcnow()}}
    assert service.get_job_statistics()["active_jobs"] == 1
    assert service.get_active_jobs()[0]["job_id"] == "j"


# Seller categorization -----------------------------------------------------


@pytest.mark.asyncio
async def test_seller_categorization_query_mapping_statistics_and_batch_edges():
    db = MagicMock()
    service = bare(
        categorization_mod.SellerCategorizationService,
        db_session=db,
        settings=SimpleNamespace(),
        openai_service=SimpleNamespace(generate_3_level_categorization=Mock()),
        batch_size=1,
        max_concurrent_jobs=2,
        retry_attempts=2,
        _processing_stats={"sellers_processed": 0, "categories_mapped": 0, "openai_calls_made": 0, "errors_encountered": 0, "processing_time_total": 0},
    )
    real_generate_3_level_mapping = service._generate_3_level_mapping_for_category
    real_categorize_seller_categories = service.categorize_seller_categories
    real_process_seller_batch = service._process_seller_batch
    seller = SimpleNamespace(seller_id="s", seller_name="Seller", location="Pune", ranking=None, categories=["Tools"])
    db.query.return_value.all.return_value = [seller]
    assert await service._get_sellers_needing_categorization(True) == [seller]
    db.query.return_value.distinct.return_value = db.query.return_value
    db.query.return_value.filter.return_value.all.return_value = [seller]
    assert await service._get_sellers_needing_categorization(False) == [seller]
    db.query.side_effect = RuntimeError("db")
    assert await service._get_sellers_needing_categorization() == []
    db.query.side_effect = None
    db.query.return_value.filter.return_value.first.return_value = seller
    assert await service._get_seller_details("s") is seller
    db.query.return_value.filter.return_value.first.side_effect = RuntimeError("db")
    db.add.side_effect = RuntimeError("db")
    with pytest.raises(RuntimeError):
        await service._create_categorization_job("s", ["Tools"])
    db.add.side_effect = None

    db.query.return_value.join.return_value.filter.return_value.limit.return_value.all.return_value = []
    assert await service._get_similar_category_items("Tools") == []
    db.query.return_value.join.return_value.filter.return_value.limit.return_value.all.side_effect = RuntimeError("query")
    assert await service._get_similar_category_items("Tools") == []

    service._get_sellers_needing_categorization = AsyncMock(return_value=[seller, seller])
    service._process_seller_batch = AsyncMock(return_value={"processed": 1, "errors": 0})
    await service.process_all_sellers()
    service._process_seller_batch.side_effect = RuntimeError("batch")
    assert not (await service.process_all_sellers())["success"]

    service._process_seller_batch = real_process_seller_batch
    service.categorize_seller_categories = AsyncMock(side_effect=[{"success": True}, RuntimeError("bad"), {"success": False, "error": "no"}])
    batch = await service._process_seller_batch([seller, seller, seller])
    assert batch["processed"] == 1 and batch["errors"] == 2
    service.categorize_seller_categories = AsyncMock(side_effect=RuntimeError("outer"))
    assert (await service._process_seller_batch([seller]))["errors"] == 1

    service.categorize_seller_categories = real_categorize_seller_categories
    service._get_seller_details = AsyncMock(return_value=None)
    assert (await service.categorize_seller_categories("missing"))["error"] == "Seller not found"
    service._get_seller_details.return_value = seller
    service._create_categorization_job = AsyncMock(return_value=SimpleNamespace(job_id="j"))
    service._generate_3_level_mapping_for_category = AsyncMock(return_value={"success": False, "error": "AI"})
    service._update_categorization_job = AsyncMock()
    assert "No categories" in (await service.categorize_seller_categories("s"))["error"]

    service._generate_3_level_mapping_for_category = real_generate_3_level_mapping
    service._get_similar_category_items = AsyncMock(return_value=[])
    service.openai_service.generate_3_level_categorization.return_value = {"success": False, "error": "AI"}
    assert not (await service._generate_3_level_mapping_for_category("Tools", seller))["success"]
    service.openai_service.generate_3_level_categorization.return_value = {"success": True, "categorization": {"level_1": "Hardware", "level_2": "Tools", "level_3": "Hand Tools"}}
    result = await service._generate_3_level_mapping_for_category("Tools", seller)
    assert result["success"] and result["mapping"]["level_1_category"] == "Hardware"
    service.openai_service.generate_3_level_categorization.side_effect = RuntimeError("AI")
    assert not (await service._generate_3_level_mapping_for_category("Tools", seller))["success"]

    job = SimpleNamespace(job_status=None, completed_at=None, output_mappings=None, processing_time_ms=None, error_message=None)
    db.query.return_value.filter.return_value.first.return_value = job
    await service._update_categorization_job("j", JobStatus.completed, [{"x": 1}], 10)
    await service._update_categorization_job("j", JobStatus.failed, None, None, "bad")

    db.query.return_value.filter.return_value.delete.return_value = 1
    learning = SimpleNamespace(id="cat")
    db.query.return_value.filter.return_value.first.side_effect = [learning, None]
    mappings = [{"original_category": "Tools", "level_1_category": "Hardware", "level_2_category": "Tools", "level_3_category": "Hand Tools", "confidence_score": .8, "ai_reasoning": "reason"}]
    await service._store_seller_mappings("s", mappings)
    await service._store_seller_mappings("s", mappings)
    db.query.side_effect = RuntimeError("db")
    with pytest.raises(RuntimeError):
        await service._store_seller_mappings("s", mappings)

    db.query.side_effect = None
    db.query.return_value.scalar.side_effect = [10, 5, 7, 8, 6, 1]
    db.query.return_value.filter.return_value.scalar.side_effect = [4, 2]
    db.query.return_value.filter.return_value.count.return_value = 3
    stats = await service.get_categorization_statistics()
    assert "database_statistics" in stats
    db.query.side_effect = RuntimeError("stats")
    assert "error" in await service.get_categorization_statistics()


# Seller service ------------------------------------------------------------


def make_seller_service():
    service = bare(
        seller_mod.SellerService,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        seller_api_service=SimpleNamespace(),
        settings=SimpleNamespace(support_contact_info="support@example.com", procucev_rfq_details_url="https://example.test/rfqs", rfq_max_allowed=3),
        response_helpers=SimpleNamespace(generate_seller_contextual_response=AsyncMock(return_value="response"), generate_seller_contextual_intent_response=AsyncMock(return_value={"intent": "general_question", "confidence": .8})),
        rfq_status_service=SimpleNamespace(handle_rfq_status_inquiry=AsyncMock(return_value={"status": "rfq-status"})),
        openai_service=SimpleNamespace(),
    )
    return service


@pytest.mark.asyncio
async def test_seller_workflow_display_classification_and_plan_edges():
    service = make_seller_service()
    user = SimpleNamespace(id="s", org_id="org", phone_number="1", email="s@example.com")
    session = make_session()
    assert "no active" in service._generate_hardcoded_rfq_display([], 0, 0).lower()
    assert "not have enough" in service._generate_hardcoded_rfq_display([{"rfq_id": "R", "delivery_date": "tomorrow", "location": "Pune", "project_description": "x"}], 1, 0).lower()
    assert "available" in service._generate_hardcoded_rfq_display([{"rfq_id": "R", "delivery_date": "tomorrow", "location": "Pune", "project_description": "x"}], 1, 2, skip_intro=True).lower()

    service._fetch_seller_rfqs = AsyncMock(return_value={"success": False})
    service._handle_rfq_fetch_error = AsyncMock(return_value={"step": "fetch"})
    assert (await service._handle_initial_seller_flow(user, session, "x"))["step"] == "fetch"
    service._fetch_seller_rfqs.return_value = {"success": True, "rfqs": [], "total_count": 0}
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 1})
    assert (await service._display_rfqs_to_seller(user, session, "x"))["success"]
    service._fetch_seller_rfqs.side_effect = RuntimeError("fetch")
    with pytest.raises(RuntimeError):
        await service._display_rfqs_to_seller(user, session, "x")

    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("subscribe", {})["intent"] == "plan_upgrade_request"
    assert service._fallback_intent_classification("send RFQ", {})["intent"] == "rfq_access_request"
    assert service._fallback_intent_classification("???", {})["intent"] == "general_question"
    service.response_helpers.generate_seller_contextual_intent_response.side_effect = RuntimeError("AI")
    assert (await service._classify_seller_intent("plan", {}, session))["intent"] == "plan_upgrade_request"

    session.conversation_history = {"messages": [{"role": "assistant", "content": "choose a subscription plan"}]}
    service._handle_plan_upgrade_request = AsyncMock(return_value={"step": "upgrade"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["step"] == "upgrade"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "RFQ details"}]}
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 2})
    service._handle_rfq_selection_prompt = AsyncMock(return_value={"step": "prompt"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["step"] == "prompt"
    session.conversation_history = {"messages": []}
    service._handle_general_affirmative_response = AsyncMock(return_value={"step": "generic"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["step"] == "generic"

    service.seller_api_service.get_subscription_plans = AsyncMock(return_value={"success": False})
    service._handle_plan_fetch_error = AsyncMock(return_value={"step": "plan-error"})
    assert (await seller_mod.SellerService._handle_plan_upgrade_request(service, user, session, "upgrade"))["step"] == "plan-error"
    service.seller_api_service.get_subscription_plans.return_value = {"success": True, "plans": [{"id": "p", "planName": "Basic"}]}
    assert (await seller_mod.SellerService._handle_plan_upgrade_request(service, user, session, "upgrade"))["success"]
    service._extract_plan_selection = AsyncMock(return_value=None)
    assert not (await service._handle_plan_selection_response(user, session, "bad"))["success"]
    service._extract_plan_selection.return_value = {"id": "p"}
    service.seller_api_service.generate_payment_link = AsyncMock(return_value={"success": False})
    service._handle_payment_link_error = AsyncMock(return_value={"step": "payment-error"})
    assert (await service._handle_plan_selection_response(user, session, "p"))["step"] == "payment-error"
    service.seller_api_service.generate_payment_link.return_value = {"success": True, "payment_url": "https://pay"}
    assert (await service._handle_plan_selection_response(user, session, "p"))["success"]


@pytest.mark.asyncio
async def test_seller_email_completion_extraction_and_error_categories():
    service = make_seller_service()
    user = SimpleNamespace(id="s", org_id="org", phone_number="1", email="s@example.com")
    session = make_session()
    service.seller_api_service.send_rfq_email = AsyncMock(return_value={"data": {"success": True, "results": {"successful": [{"rfq_id": "R1"}], "failed": [{"rfq_id": "R2", "error_code": "RFQ_NOT_FOUND"}, {"rfq_id": "R3", "error_code": "UNKNOWN"}]}}})
    assert (await service._process_rfq_email_requests(user, session, ["R1", "R2", "R3"]))["emails_sent"] == 1
    service.seller_api_service.send_rfq_email.return_value = {"success": False, "error": "batch", "error_code": "API_ERROR"}
    assert (await service._process_rfq_email_requests(user, session, ["R4"]))["error_analysis"]["error_counts"]["API_ERROR"] == 1
    service.seller_api_service.send_rfq_email.side_effect = RuntimeError("HTTP")
    assert (await service._process_rfq_email_requests(user, session, ["R5"]))["error_analysis"]["error_counts"]["API_ERROR"] == 1

    analysis = service._analyze_email_errors([
        {"success": True, "rfq_id": "R0"},
        {"success": False, "rfq_id": "R1", "error_code": "NO_CREDITS"},
        {"success": False, "rfq_id": "R2", "error_code": "RFQ_NOT_FOUND"},
        {"success": False, "rfq_id": "R3", "error_code": "API_ERROR"},
        {"success": False, "rfq_id": "R4"},
    ])
    assert analysis["total_failed"] == 4 and analysis["total_successful"] == 1

    service.seller_api_service.get_subscription_plans = AsyncMock(return_value={"success": False})
    service._handle_plan_fetch_error = AsyncMock(return_value={"step": "plan"})
    assert (await service._handle_no_credits_response(user, session))["step"] == "plan"
    service.seller_api_service.get_subscription_plans.return_value = {"success": True, "plans": []}
    assert (await service._handle_no_credits_response(user, session))["success"]

    service._fetch_seller_open_rfqs_for_reminder = AsyncMock(return_value={"success": False})
    result = await service.handle_seller_flow_completion(user, session)
    assert result["workflow_step"] == "generic_closing"
    service._fetch_seller_open_rfqs_for_reminder.return_value = {"success": True, "open_rfqs": []}
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "standard_closing"
    service._fetch_seller_open_rfqs_for_reminder.return_value = {"success": True, "open_rfqs": [{"id": "R"}]}
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "end_of_flow_reminder"
    service.seller_api_service.fetch_seller_open_rfqs_for_reminder = AsyncMock(side_effect=RuntimeError("API"))
    assert not (await seller_mod.SellerService._fetch_seller_open_rfqs_for_reminder(service, "s"))["success"]

    session.conversation_history = {"messages": [{"role": "assistant", "content": "1. RFQ123456789012\n2. RFQ223456789012"}]}
    service._extract_sequence_numbers("1, 2")
    assert await service._extract_rfq_ids_from_message("1 2", session, []) == ["RFQ123456789012", "RFQ223456789012"]
    service._map_sequence_to_rfq_ids([9], {"content": "1. RFQ123456789012"})
    service._extract_sequence_numbers = Mock(return_value=[])
    service.openai_service.extract_rfq_ids_from_message = AsyncMock(return_value={"success": True, "rfq_ids": ["R1", "R2", "R3", "R4"]})
    assert await service._extract_rfq_ids_from_message("ids", make_session(), []) == ["R1", "R2", "R3"]
    service.openai_service.extract_rfq_ids_from_message.return_value = {"success": False}
    assert await service._extract_rfq_ids_from_message("ids", make_session(), []) == []
    service.openai_service.extract_rfq_ids_from_message.side_effect = RuntimeError("AI")
    assert await service._extract_rfq_ids_from_message("ids", make_session(), []) == []

    service.openai_service.extract_entities = AsyncMock(return_value={"selected_plan": "Basic"})
    plans = [{"id": "p", "planName": "Basic"}, {"id": "q", "planName": "Premium"}]
    assert (await service._extract_plan_selection("Basic", plans))["id"] == "p"
    service.openai_service.extract_entities.return_value = {"selected_plan": None}
    assert (await service._extract_plan_selection("select plan", [plans[0]]))["id"] == "p"
    assert await service._extract_plan_selection("nothing", plans) is None
    service.openai_service.extract_entities.side_effect = RuntimeError("AI")
    assert await service._extract_plan_selection("nothing", plans) is None
    assert service._is_view_available_rfq_request("show available rfqs")
    assert not service._is_view_available_rfq_request("hello")
    assert service._build_seller_conversation_context(session, "hello", 2)["seller_credits"] == 2


# Session management --------------------------------------------------------


def make_session_service(*, redis_enabled=False):
    return bare(
        session_mod.SessionManagementService,
        db_manager=SimpleNamespace(get_conversation_session=Mock(), save_conversation_session=Mock()),
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        chat_summary_service=SimpleNamespace(generate_session_summary=AsyncMock()),
        daily_summary_service=SimpleNamespace(generate_daily_summary=AsyncMock()),
        redis_session=SimpleNamespace(),
        settings=SimpleNamespace(redis_session_storage_enabled=redis_enabled, license_enabled=False),
        redis_enabled=redis_enabled,
        summarization_helpers=MagicMock(),
    )


@pytest.mark.asyncio
async def test_session_management_license_context_and_creation_edges(monkeypatch):
    service = make_session_service(redis_enabled=False)
    assert service._validate_license() == (True, "License validation disabled")
    service.settings.license_enabled = True
    monkeypatch.setattr("app.license.validate_license", lambda: (False, "expired"))
    assert service._validate_license() == (False, "expired")
    monkeypatch.setattr("app.license.validate_license", lambda: (_ for _ in ()).throw(RuntimeError("license")))
    assert service._validate_license() == (False, "License validation failed")

    monkeypatch.setattr(session_mod.SessionHelpers, "generate_session_id", lambda *_: "sid")
    service.settings.license_enabled = False
    service.db_manager.get_conversation_session.return_value = None
    created = make_session()
    service.db_manager.save_conversation_session.return_value = created
    monkeypatch.setattr("app.services.welcome_message_service.get_welcome_service", lambda: SimpleNamespace(check_and_send_welcome=AsyncMock()))
    assert await service.get_conversation_context("1") is created
    existing = make_session()
    service.db_manager.get_conversation_session.return_value = existing
    assert await service.create_session("1", "registration", "buyer") is existing
    service.db_manager.get_conversation_session.return_value = make_session()
    assert await service.create_session("1") is service.db_manager.get_conversation_session.return_value

    old = make_session(outcome=ConversationOutcome.completed, workflow_type="old", state={"old": 1})
    service.db_manager.get_conversation_session.return_value = old
    assert await service.create_session("1", WorkflowType.registration, "seller") is old
    assert old.outcome is None

    session = make_session()
    assert await service.handle_session_expiry_check("1", session) is session
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    service.add_message_to_history = Mock(side_effect=RuntimeError("track"))
    service.whatsapp_service.send_message.side_effect = [None, RuntimeError("retry")]
    with pytest.raises(RuntimeError):
        await service.send_and_track_message("1", "hello", session)


@pytest.mark.asyncio
async def test_session_management_redis_save_completion_serialization_and_persistence(monkeypatch):
    service = make_session_service(redis_enabled=True)
    session = make_session(state={"pending_optional_combined_rfq": {"id": "R"}})
    service.redis_session.store_session = AsyncMock()
    service.redis_session.refresh_ttl = AsyncMock()
    service._session_to_dict = Mock(return_value={"session_id": "session-1"})
    monkeypatch.setattr(session_mod.WorkflowManager, "initialize_workflow_state", Mock())
    monkeypatch.setattr(session_mod.WorkflowManager, "get_workflow_type", Mock(return_value=None))
    assert await service.save_session(session, WorkflowType.registration) is session
    assert service.redis_session.store_session.await_count == 1
    session.outcome = ConversationOutcome.completed
    assert await service.save_session(session) is session
    session.outcome = None
    session.workflow_state = {"exit_completed": True}
    assert await service.save_session(session) is session

    service.redis_enabled = False
    saved = make_session()
    service.db_manager.save_conversation_session.return_value = saved
    assert await service.save_session(make_session(), persist_to_db=False) is saved
    service.db_manager.save_conversation_session.side_effect = RuntimeError("db")
    result = await service.save_session(make_session(state={"x": 1}))
    assert result.workflow_state == {"x": 1}

    task_coroutines = []
    monkeypatch.setattr(session_mod.asyncio, "create_task", lambda coro: task_coroutines.append(coro))
    monkeypatch.setattr(session_mod.SummarizationHelpers, "extract_rich_entities_for_summary", Mock(return_value={"x": 1}))
    monkeypatch.setattr(session_mod.SummarizationHelpers, "prepare_enhanced_summary_data", Mock(return_value={"x": 1}))
    monkeypatch.setattr(session_mod.SummarizationHelpers, "handle_session_completion_async", AsyncMock())
    await service.handle_session_completion_enhanced(make_session())
    task_coroutines[0].close()
    monkeypatch.setattr(session_mod.SummarizationHelpers, "extract_rich_entities_for_summary", Mock(side_effect=RuntimeError("summary")))
    service._handle_session_completion_fallback = AsyncMock()
    await service.handle_session_completion_enhanced(make_session())
    service.chat_summary_service.generate_session_summary.side_effect = RuntimeError("summary")
    await service._handle_session_completion_fallback(make_session())
    await service._show_auth_placeholder("1")
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    await service._show_auth_placeholder("1")

    circular = {}
    circular["self"] = circular
    assert service._clean_for_json_serialization(circular)["self"] == "<circular_reference>"
    assert service._clean_for_json_serialization(None) is None
    assert service._clean_for_json_serialization(SimpleNamespace(value="enum")) == "enum"
    assert service._clean_for_json_serialization(datetime(2026, 1, 1)).startswith("2026")
    assert service._clean_for_json_serialization(object()) is not None
    data = session_mod.SessionManagementService._session_to_dict(service, make_session(workflow_type=WorkflowType.registration, outcome=ConversationOutcome.completed))
    assert data["workflow_type"] == WorkflowType.registration.value
    restored = service._dict_to_session({"session_id": "s", "external_user_id": "1", "workflow_type": "bad", "outcome": "bad", "retention_date": None})
    assert restored.workflow_type is None and restored.outcome is None
    assert not service._should_persist_abandoned(make_session())
    short_history = make_session()
    short_history.conversation_history = {"openai_messages": [1, 2]}
    assert not service._should_persist_abandoned(short_history)
    long_history = make_session()
    long_history.conversation_history = {"openai_messages": [1, 2, 3]}
    assert service._should_persist_abandoned(long_history)


# User cache and vendor -----------------------------------------------------


@pytest.mark.asyncio
async def test_user_cache_remaining_redis_and_filter_branches(monkeypatch):
    redis = SimpleNamespace(
        get=AsyncMock(return_value=None), set=AsyncMock(return_value=True), delete=AsyncMock(return_value=True), exists=AsyncMock(return_value=False), expire=AsyncMock(return_value=False)
    )
    service = bare(cache_mod.UserCacheService, redis_service=redis)
    assert await service.store_user_data("+91 99-1", [{"username": "a", "selfClient": True}])
    redis.get.return_value = {"user_data": [{"username": "a", "selfClient": True}]}
    assert await service.get_user_data("1")
    service.get_user_data = AsyncMock(return_value=[{"username": "a", "selfClient": True}])
    service._filter_users_by_intent = Mock(return_value={"success": False, "message": "none"})
    assert await service.get_filtered_user_data("1", "sell_something") is None
    service._filter_users_by_intent = Mock(return_value={"success": True, "count": 1, "filtered_users": []})
    assert (await service.get_filtered_user_data("1", "buy_something"))["count"] == 1

    redis.get.return_value = {"user_data": [{"id": "1"}]}
    assert await service.clear_user_data("1", preserve_meaningful_message=False)
    redis.get.return_value = {"user_data": [{"id": "1"}], "meaningful_message": "m"}
    assert await service.clear_user_data("1", preserve_meaningful_message=True)
    redis.get.return_value = None
    assert not await service.clear_user_data("1")
    redis.get.side_effect = RuntimeError("redis")
    assert not await service.clear_user_data("1")
    redis.get.side_effect = None

    redis.exists.return_value = False
    assert not await service.is_data_cached("1")
    redis.exists.side_effect = RuntimeError("redis")
    assert not await service.is_data_cached("1")
    redis.exists.side_effect = None
    assert not await service.refresh_cache_expiry("1", 2)
    redis.expire.side_effect = RuntimeError("redis")
    assert not await service.refresh_cache_expiry("1")

    redis.get.return_value = None
    assert await service.store_meaningful_message("1", "hello", {"intent": "buy"})
    redis.get.return_value = {"meaningful_message": "hello", "meaningful_intent_result": {"intent": "buy"}}
    assert (await service.get_meaningful_message("1"))["message"] == "hello"
    redis.get.return_value = {"other": 1}
    assert await service.get_meaningful_message("1") is None
    assert await service.clear_meaningful_message("1")
    redis.get.side_effect = RuntimeError("redis")
    assert not await service.store_meaningful_message("1", "x", {})
    assert await service.clear_meaningful_message("1") is False

    service.get_user_data = AsyncMock(return_value=[])
    assert await service.get_account_options_for_intent_switch("1", "sell_something") is None
    service.get_user_data.return_value = [{"username": "s@example.com", "selfClient": False, "companyName": "Co"}]
    service._filter_users_by_intent = Mock(return_value={"success": False})
    assert (await service.get_account_options_for_intent_switch("1", "sell_something"))["has_target_accounts"] is False
    service._filter_users_by_intent.return_value = {"success": True, "filtered_users": [{"username": "s@example.com", "selfClient": False, "companyName": "Co"}]}
    assert (await service.get_account_options_for_intent_switch("1", "sell_something", "other@example.com"))["has_target_accounts"]
    service._filter_users_by_intent.side_effect = RuntimeError("filter")
    assert await service.get_account_options_for_intent_switch("1", "sell_something") is None

    assert cache_mod.UserCacheService._filter_users_by_intent(service, [], "buy_something")["success"] is False
    assert cache_mod.UserCacheService._filter_users_by_intent(service, [{"id": "b", "username": "b", "selfClient": True}], "unknown")["success"]
    cache_mod._user_cache_service = None
    monkeypatch.setattr(cache_mod, "UserCacheService", lambda: service)
    assert cache_mod.get_user_cache_service() is service
    assert cache_mod.get_user_cache_service() is service


def test_vendor_partial_loop_and_placeholder(monkeypatch):
    service = bare(vendor_mod.VendorService, db_session=MagicMock())

    class ArrayColumn:
        def any(self, value):
            return value

    class VendorModel:
        vendor_services = ArrayColumn()
        geographic_coverage = ArrayColumn()

    monkeypatch.setattr(vendor_mod, "Vendor", VendorModel)
    assert service.search_vendors({}) == []
    assert service.search_vendors({"entities": {}}) == []
    assert service.search_vendors({"entities": {"category": None, "location": None}}) == []
    vendor = SimpleNamespace(vendor_id="v", vendor_name="Vendor", vendor_services=["Tools"], geographic_coverage=["Far", "New York City"])
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = [vendor]
    service.db_session.query.return_value = query
    result = service.search_vendors({"entities": {"category": "Tools", "location": "York"}})
    assert result[0]["vendor_id"] == "v"
    assert service._calculate_relevance(vendor, {"category": "Tools", "location": "York"}) == 50 + 15 + 2 + 2
    assert service.search_bfs_inventory({"entities": {"category": "Tools"}})
    assert service.get_vendor_recommendations({"entities": {"category": "Tools"}})
    service.update_vendor_learning("r", "v", ["Tools"])


# WhatsApp ------------------------------------------------------------------


@pytest.fixture
def whatsapp_service(monkeypatch):
    settings = SimpleNamespace(
        WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p", WHATSAPP_FROM_NUMBER="f",
        WHATSAPP_BASE_URL="https://wa", WHATSAPP_TEMPLATE_BASE_URL="https://wa/templates",
        WHATSAPP_MOCK_MODE=False, retry_max_attempts=1, retry_initial_delay=0,
    )
    retry = SimpleNamespace(max_retries=0, initial_delay=0, retry_with_backoff=AsyncMock())
    monkeypatch.setattr(whatsapp_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(whatsapp_mod, "get_retry_service", lambda: retry)
    service = whatsapp_mod.WhatsAppService()
    service._clear_pending_reply_flag = AsyncMock()
    service._track_message_in_history = AsyncMock()
    return service, retry


@pytest.mark.asyncio
async def test_whatsapp_delivery_interactive_list_button_and_cache_edges(whatsapp_service, monkeypatch):
    service, retry = whatsapp_service
    response = SimpleNamespace(status_code=200, text="ok", json=lambda: {"mid": "m1"})
    monkeypatch.setattr(whatsapp_mod, "post_to_gateway", AsyncMock(return_value=response))
    retry.retry_with_backoff.return_value = {"success": True, "attempts": 1, "result": MessageResponse(True, "m")}
    assert (await service.send_message("+919999999999", "hello", session_id="sid")).success
    assert (await service.send_message("+919999999999", "ack", clear_pending_reply=False)).success
    retry.retry_with_backoff.return_value = {"success": False, "attempts": 2, "error": "down"}
    assert not (await service.send_message("+919999999999", "fail", session_id="sid")).success
    retry.retry_with_backoff.return_value = {"success": True, "attempts": 1, "result": MessageResponse(True, "t")}
    assert (await service.send_template_message("1", "welcome", ["A"])).success
    retry.retry_with_backoff.return_value = {"success": False, "attempts": 1, "error": "template"}
    assert not (await service.send_template_message("1", "welcome", [])).success

    assert (await service.send_interactive_message("bad", "button", {})).success is False
    monkeypatch.setattr(whatsapp_mod, "post_to_gateway", AsyncMock(side_effect=RuntimeError("HTTP")))
    assert not (await service.send_interactive_message("1", "button", {})).success
    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True, "m"))
    assert (await service.send_cta_button_message("1", "body", "go", "https://x")).success
    assert (await service.send_list_message("1", "H", "B", [{"title": str(i)} for i in range(11)])).success
    service.send_interactive_message.side_effect = RuntimeError("interactive")
    assert not (await service.send_list_message("1", "H", "B", [])).success
    service.send_interactive_message.side_effect = None
    service.send_interactive_message.return_value = MessageResponse(True, "m")
    assert (await service.send_button_message("1", "H", "B", [{"title": str(i)} for i in range(4)])).success
    service.send_interactive_message.side_effect = RuntimeError("interactive")
    assert not (await service.send_button_message("1", "H", "B", [{"title": "x"}])).success

    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True, "m"))
    redis = SimpleNamespace(get=AsyncMock(return_value={"irrelevant_response": {"user_message": "old"}}), set=AsyncMock(return_value=True))
    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: redis)
    result = await service.send_configurable_buttons("+919999999999", "body\x00", [{"id": "a", "title": "A"}], header="H\x00", session_id="sid")
    assert result.success and redis.set.await_count == 1
    assert (await service.send_configurable_buttons("+919999999999", "body", [{"id": "a", "title": "A"}], skip_concatenation=True)).success
    assert not (await service.send_configurable_buttons("+919999999999", "body", [])).success
    assert not (await service.send_configurable_buttons("+919999999999", "body", [{"id": "a", "title": ""}])).success
    assert (await service.send_configurable_buttons("+919999999999", "body", [{"id": str(i), "title": str(i)} for i in range(4)])).success


@pytest.mark.asyncio
async def test_whatsapp_response_formatting_phone_and_tracking_edges(whatsapp_service, monkeypatch):
    service, _ = whatsapp_service
    response = lambda status, payload: SimpleNamespace(status_code=status, text="body", json=lambda: payload)
    assert service._handle_api_response(response(200, [{"mid": 4}])).message_id == "4"
    assert service._handle_api_response(response(200, {"mid": 5})).message_id == "5"
    assert service._handle_api_response(response(200, {"status": "ok"})).success
    assert service._handle_api_response(response(200, {"other": "x"})).success
    assert not service._handle_api_response(response(500, {"Error": "down"})).success
    assert not service._handle_api_response(SimpleNamespace(status_code=200, text="bad", json=Mock(side_effect=ValueError("json")))).success
    assert not service._handle_api_response(SimpleNamespace(status_code=500, text="bad", json=Mock(side_effect=ValueError("json")))).success

    assert service.format_vendor_results([]).startswith("No vendors")
    assert "more vendors" in service.format_vendor_results([{"name": str(i), "description": "d", "contact": "c", "location": "l"} for i in range(6)])
    assert service.format_bfs_results([]).startswith("No products")
    assert "more products" in service.format_bfs_results([{"name": str(i), "price": 1, "quantity": 2, "description": "d"} for i in range(6)])
    summary = service.format_rfq_summary({"product_name": "P", "quantity": 2, "unit_of_measure": "kg", "deadline": "tomorrow", "delivery_city": "Pune", "delivery_state": "MH", "division": "D", "specifications": "S", "remarks": "R"})
    assert "Specifications" in summary and "Pune, MH" in summary

    assert service._format_phone_number("") == ""
    assert service._format_phone_number("12") == ""
    assert service._format_phone_number("0" * 16) == ""
    assert service._format_phone_number("9876543210") == "919876543210"
    assert service._format_phone_number("09876543210") == "919876543210"
    assert service._format_phone_number("919876543210") == "919876543210"
    assert service._format_phone_number("123456789012") == "123456789012"
    assert service._format_phone_number("+91 98765 43210") == "919876543210"

    redis = SimpleNamespace(delete=AsyncMock(return_value=True))
    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: redis)
    await service._clear_pending_reply_flag("+919999999999")
    redis.delete.side_effect = RuntimeError("redis")
    await service._clear_pending_reply_flag("1")
    service._track_message_in_history = whatsapp_mod.WhatsAppService._track_message_in_history.__get__(service)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: SimpleNamespace(append_message_to_history=AsyncMock()))
    await service._track_message_in_history("sid", "body")
    await service._track_message_in_history(None, "body")
    broken = SimpleNamespace(session_id="s", conversation_history={})
    monkeypatch.setattr("app.services.helpers.summarization_helpers.SummarizationHelpers.add_to_conversation_history", Mock(side_effect=RuntimeError("history")))
    await service._track_message_in_history(broken, "body")
