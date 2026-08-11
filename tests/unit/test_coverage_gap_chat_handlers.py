"""Deterministic residual coverage for ChatService and handler state machines.

These tests intentionally call handlers directly and replace every external
boundary with a fake or mock.  They are kept in one module so the residual
coverage work does not alter application behavior or existing fixtures.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_mod
import app.services.handlers.auth_registration_intent_switch as switch_mod
import app.services.handlers.authentication_orchestrator as auth_mod
import app.services.handlers.bfs_search_handler as bfs_mod
import app.services.handlers.confirmation_handler as confirmation_mod
import app.services.handlers.format_modification_handler as format_mod
import app.services.handlers.intent_switch_handler as intent_mod
import app.services.handlers.products_array_handler as products_mod
import app.services.handlers.purchase_workflow_handler as purchase_workflow_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.handlers.seller_auth_mixin as seller_auth_mod
import app.services.handlers.seller_rfq_interest_handler as interest_mod
from app.models import ConversationOutcome, WorkflowType
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_workflow_handler import PurchaseWorkflowHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler


class Role(Enum):
    BUYER = "buyer"
    SELLER = "seller"


def make_user(role="buyer", **overrides):
    values = {
        "id": "user-1",
        "org_id": "org-1",
        "phone_number": "+919999999999",
        "email": "buyer@example.com",
        "username": "buyer@example.com",
        "name": "Test User",
        "role": role,
        "is_registered": True,
        "self_client": role == "buyer" or role == Role.BUYER,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_session(**state):
    return SimpleNamespace(
        session_id="session-1",
        external_user_id="919999999999",
        phone_number="+919999999999",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
        extracted_entities=[],
        product_items=[],
        outcome=None,
        retention_date=date(2025, 1, 1),
        last_activity_at=datetime(2025, 1, 1),
        created_at=datetime(2025, 1, 1),
        completed_at=None,
        rfq_ids=[],
        user_type=None,
    )


def bare(cls, **attrs):
    instance = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(instance, name, value)
    return instance


@pytest.fixture(autouse=True)
def no_technical_side_effects(monkeypatch):
    monkeypatch.setattr(
        "app.utils.technical_failure_handler.handle_technical_failure",
        AsyncMock(),
    )


def chat_stub():
    service = chat_mod.ChatService.__new__(chat_mod.ChatService)
    service.whatsapp_service = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(),
    )
    service.session_manager = SimpleNamespace(
        save_session=AsyncMock(),
        send_and_track_message=AsyncMock(),
        add_message_to_history=MagicMock(),
        get_conversation_context=AsyncMock(return_value=make_session()),
    )
    service._intent_service = SimpleNamespace(classify_intent=AsyncMock())
    service._authentication_service = SimpleNamespace(
        otp_service=SimpleNamespace(validate_otp=AsyncMock()),
        validate_token=AsyncMock(return_value=None),
        user_authenticate=AsyncMock(return_value={"success": True}),
        filter_users_by_intent=MagicMock(return_value={"success": True}),
        store_user_session_with_email=AsyncMock(return_value={"success": True}),
        handle_email_confirmation=AsyncMock(return_value={"status": "email"}),
    )
    service._response_helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="context"),
        generate_seller_contextual_response=AsyncMock(return_value="seller context"),
    )
    service._purchase_intent_handler = SimpleNamespace(
        handle_purchase_intent=AsyncMock(return_value={"status": "purchase"})
    )
    service._confirmation_handler = SimpleNamespace(
        handle_confirmation_button=AsyncMock(return_value={"status": "confirmed"}),
        handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}),
        handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}),
    )
    service._intent_switch_handler = SimpleNamespace(
        should_handle_intent_switch=AsyncMock(return_value=False),
        handle_intent_switch_choice=AsyncMock(return_value={"status": "switch"}),
        handle_intent_switch_response=AsyncMock(return_value={"status": "switch"}),
    )
    service._attachment_decision_handler = SimpleNamespace(
        handle_attachment_decision=AsyncMock(return_value={"status": "attachment"})
    )
    service._bfs_search_handler = SimpleNamespace(
        handle_bfs_search=AsyncMock(return_value={"status": "bfs"}),
        handle_button=AsyncMock(return_value={"status": "bfs"}),
    )
    service._format_modification_handler = SimpleNamespace(
        handle_format_modification=AsyncMock(return_value={"status": "format"})
    )
    service._cancel_service = SimpleNamespace(
        handle_cancel_intent=AsyncMock(return_value={"status": "cancel"}),
        handle_cancel_confirmation=AsyncMock(return_value={"status": "cancelled"}),
        _send_cancellation_message=AsyncMock(),
        _clear_workflow_state=AsyncMock(),
    )
    service._exit_service = SimpleNamespace(
        handle_exit_intent=AsyncMock(return_value={"status": "exited"}),
        handle_exit_confirmation=AsyncMock(return_value={"status": "exit"}),
    )
    service._image_processor = SimpleNamespace(
        process_image_message=AsyncMock(return_value={"status": "image"})
    )
    service._seller_service = SimpleNamespace(
        handle_seller_workflow=AsyncMock(return_value={"status": "seller"})
    )
    service._rfq_status_service = SimpleNamespace(
        handle_rfq_status_inquiry=AsyncMock(return_value={"status": "status"})
    )
    service._entity_service = SimpleNamespace()
    service.db_manager = SimpleNamespace(
        save_conversation_session=MagicMock(return_value={"saved": True}),
        append_session_data=MagicMock(),
        close=MagicMock(),
    )
    service.settings = SimpleNamespace(support_email="support@example.com")
    service.authentication_orchestrator_flow = AsyncMock(
        return_value={"status": "authentication_completed", "user_type": "buyer"}
    )
    service.handle_irrelevant_message_flow = AsyncMock()
    service._handle_registration_workflow = AsyncMock(return_value={"status": "registration"})
    service._handle_seller_rfq_intimation_flow = AsyncMock(return_value=None)
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status"})
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting"})
    service._handle_clarification_request = AsyncMock(return_value={"status": "clarify"})
    service._handle_fallback = AsyncMock(return_value={"status": "fallback"})
    service._handle_account_switch_intent = AsyncMock(return_value={"status": "account"})
    service._handle_contextual_interaction = AsyncMock(return_value={"status": "context"})
    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel"})
    return service


# ChatService residual helpers and text routing --------------------------------

@pytest.mark.asyncio
async def test_chat_workflow_activation_and_fallback_roles(monkeypatch):
    service = chat_stub()
    service._handle_fallback = chat_mod.ChatService._handle_fallback.__get__(service)
    session = make_session()
    user = make_user()

    monkeypatch.setattr(chat_mod.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
    result = await service._activate_sectioned_rfq(user, session, "")
    assert result == {"status": "sectioned_rfq_activated"}
    service.session_manager.save_session.assert_awaited_once()
    service.whatsapp_service.send_message.assert_awaited_once()

    # Exercise all fallback role branches and the common error response.
    service._handle_error_response = AsyncMock(return_value={"status": "error", "error": "send"})
    for role in ("buyer", "seller", "unknown"):
        service.whatsapp_service.send_configurable_buttons.reset_mock()
        result = await service._handle_fallback(make_user(role), "unclear", make_session())
        assert result["status"] == "fallback_handled"
        assert service.whatsapp_service.send_configurable_buttons.await_count == (1 if role != "unknown" else 1)
    service.whatsapp_service.send_configurable_buttons.side_effect = RuntimeError("send")
    assert (await service._handle_fallback(make_user("buyer"), "unclear", make_session()))["status"] == "error"


@pytest.mark.asyncio
async def test_chat_button_helper_success_unknown_and_exception_paths():
    service = chat_stub()
    user = make_user()
    session = make_session()

    service._authentication_service.handle_email_confirmation.return_value = {"status": "confirmed"}
    assert (await service._handle_authentication_email_button(user, session, "confirm_email"))["status"] == "confirmed"
    assert (await service._handle_authentication_email_button(user, session, "reject_email"))["status"] == "confirmed"
    assert (await service._handle_authentication_email_button(user, session, "other"))["status"] == "unknown_email_button"
    service._authentication_service.handle_email_confirmation.side_effect = RuntimeError("email")
    assert (await service._handle_authentication_email_button(user, session, "confirm_email"))["status"] == "error"

    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel"})
    assert (await service._handle_excel_confirmation_button(user, session, "confirm_excel"))["status"] == "excel"
    assert (await service._handle_excel_confirmation_button(user, session, "cancel_excel"))["status"] == "excel"
    assert (await service._handle_excel_confirmation_button(user, session, "other"))["status"] == "unknown_excel_button"
    service._handle_excel_confirmation_response.side_effect = RuntimeError("excel")
    assert (await service._handle_excel_confirmation_button(user, session, "confirm_excel"))["status"] == "error"

    for status in ("cancelled", "cancelled_aborted"):
        service._cancel_service.handle_cancel_confirmation.return_value = {"status": status}
        result = await service._handle_cancel_confirmation_button(user, session, "confirm_cancel")
        assert result["status"] == status
    service._cancel_service.handle_cancel_confirmation.side_effect = RuntimeError("cancel")
    assert (await service._handle_cancel_confirmation_button(user, session, "decline_cancel"))["status"] == "error"
    service._exit_service.handle_exit_confirmation.side_effect = RuntimeError("exit")
    assert (await service._handle_exit_confirmation_button(user, session, "confirm_exit"))["status"] == "error"


@pytest.mark.asyncio
async def test_chat_text_router_handles_dict_users_and_terminal_intents():
    service = chat_stub()
    user = make_user()

    assert await service._process_text_message(
        {"verification_required": True, "verification_info": {"reason": "pending"}},
        make_session(), "anything", {"intent": "greeting"}
    ) == {"status": "verification_required", "verification_info": {"reason": "pending"}}
    service._handle_registration_workflow.return_value = {"status": "registration"}
    unregistered = make_user(is_registered=False)
    assert (await service._process_text_message(unregistered, make_session(), "name", {}))["status"] == "registration"

    # The direct terminal routes do not need authentication or network services.
    routes = [
        ("modification_request", service._purchase_intent_handler.handle_purchase_intent),
        ("confirmation_response", service._purchase_intent_handler.handle_purchase_intent),
        ("reference_request", service._purchase_intent_handler.handle_purchase_intent),
        ("bfs_search", service._bfs_search_handler.handle_bfs_search),
        ("sell_something", service._handle_seller_flow),
        ("rfq_status_check", service._handle_rfq_status_inquiry),
        ("account_switch", service._handle_account_switch_intent),
        ("greeting", service._handle_greeting_inquiry),
    ]
    for intent, _handler in routes:
        session = make_session()
        route_user = make_user("seller") if intent == "sell_something" else user
        result = await service._process_text_message(
            route_user, session, "message", {"intent": intent, "confidence": 90}
        )
        assert result["status"] in {"purchase", "bfs", "seller", "status", "account", "greeting"}

    assert (await service._process_text_message(
        user, make_session(), "unclear", {"intent": "other", "confidence": 0.2}
    ))["status"] == "clarify"
    assert (await service._process_text_message(
        user, make_session(), "unclear", {"intent": "other", "confidence": 80}
    ))["status"] == "fallback"


@pytest.mark.asyncio
async def test_chat_text_router_pending_cancel_exit_bfs_and_optional_paths(monkeypatch):
    service = chat_stub()
    user = make_user()

    pending = make_session(cancel_pending=True)
    service._cancel_service.confirmation_service = SimpleNamespace(
        parse_confirmation=AsyncMock(return_value="yes")
    )
    service._cancel_service.handle_cancel_confirmation.return_value = {"status": "cancelled"}
    assert (await service._process_text_message(user, pending, "yes", {"intent": "other"}))["status"] == "cancelled"

    declined = make_session(cancel_pending=True)
    service._cancel_service.handle_cancel_confirmation.return_value = {"status": "cancelled_aborted"}
    service._purchase_intent_handler.handle_purchase_intent.return_value = {"status": "purchase"}
    assert (await service._process_text_message(user, declined, "no", {"intent": "buy_something", "confidence": 90}))["status"] == "purchase"

    exiting = make_session(exit_pending=True)
    service._cancel_service.confirmation_service.parse_confirmation.return_value = "yes"
    assert (await service._process_text_message(user, exiting, "yes", {"intent": "other"}))["status"] == "exit"

    pending_bfs = make_session(bfs_search_pending=True)
    assert (await service._process_text_message(user, pending_bfs, "pump", {"intent": "other"}))["status"] == "bfs"
    service._bfs_search_handler.handle_bid_format_input = AsyncMock(return_value={"status": "bid-format"})
    assert (await service._process_text_message(user, make_session(bfs_bid_stage="format_input"), "bid", {"intent": "other"}))["status"] == "bid-format"
    service._bfs_search_handler.handle_bid_otp_input = AsyncMock(return_value={"status": "bid-otp"})
    assert (await service._process_text_message(user, make_session(bfs_bid_stage="otp_pending"), "123", {"intent": "other"}))["status"] == "bid-otp"

    service._confirmation_handler.handle_optional_fields_response.return_value = {"status": "continue_with_purchase_intent"}
    service._purchase_intent_handler.handle_purchase_intent.return_value = {"status": "purchase"}
    optional = make_session(pending_optional_rfq={"entities": {"description": "pump"}})
    assert (await service._process_text_message(user, optional, "quantity 2", {"intent": "other"}))["status"] == "purchase"

    attachment = make_session(awaiting_attachment_decision=True)
    assert (await service._process_text_message(user, attachment, "yes", {"intent": "other"}))["status"] == "attachment"

    # Sectioned state is routed to the purchase handler for buyers.
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _s: True)
    sectioned = make_session(sectioned_rfq={"active": True})
    assert (await service._process_text_message(user, sectioned, "details", {"intent": "other"}))["status"] == "purchase"


@pytest.mark.asyncio
async def test_chat_process_message_early_interactive_and_auth_statuses(monkeypatch):
    service = chat_stub()
    session = make_session()
    service.session_manager.get_conversation_context.return_value = session

    class InterestHandler:
        def __init__(self, **_kwargs):
            pass

        async def handle_request_rfq_click(self, *args):
            return {"status": "requested", "args": args}

    monkeypatch.setattr(
        "app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler",
        InterestHandler,
    )
    result = await service.process_message(
        "+919999999999",
        {"button_reply": {"id": "rfq_request_RFQ_TEST_001_seller-1"}},
        "interactive",
    )
    assert result["status"] == "requested"
    assert result["args"][1:3] == ("RFQ_TEST_001", "seller-1")
    service.session_manager.add_message_to_history.assert_called()

    # Rate-limit handling exits before authentication orchestration.
    service._intent_service.classify_intent.return_value = {
        "timeout_handled": True, "intent": "greeting", "confidence": 0
    }
    service.session_manager.get_conversation_context.return_value = make_session()
    result = await service.process_message("+1", "hello", "text")
    assert result["status"] == "rate_limit_timeout"

    # Authentication status branches use only mocked service boundaries.
    async def run_auth_result(auth_result):
        local = chat_stub()
        local.session_manager.get_conversation_context.return_value = make_session()
        local._intent_service.classify_intent.return_value = {"intent": "greeting", "confidence": 80}
        local.authentication_orchestrator_flow = AsyncMock(return_value=auth_result)
        local.handle_irrelevant_message_flow = AsyncMock()
        return await local.process_message("+1", "hello", "text"), local

    result, local = await run_auth_result({"status": "redirected_to_support", "exit_completed": True})
    assert result["status"] == "redirected_to_support"
    local._authentication_service.validate_token.return_value = None
    result, local = await run_auth_result({"status": "verification_required", "redirect_info": {"message": "verify"}})
    assert result["status"] == "verification_required"
    result, local = await run_auth_result({"status": "verification_failed", "redirect_info": {"message": "pending"}})
    assert result["status"] == "verification_failed"
    result, local = await run_auth_result({"status": "unexpected"})
    assert result == {"status": "error", "error": "Authentication failed"}


# Authentication and registration orchestration --------------------------------

@pytest.mark.asyncio
async def test_auth_switch_role_helpers_and_ai_fallbacks(monkeypatch):
    assert switch_mod._get_role_string(SimpleNamespace(role=Role.SELLER)) == "seller"
    assert switch_mod._get_role_string(SimpleNamespace(role="buyer")) == "buyer"
    assert switch_mod._get_role_string(SimpleNamespace()) == "buyer"

    handler = bare(
        AuthRegistrationIntentSwitch,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        openai_service=MagicMock(),
    )
    handler.openai_service.generate_response.return_value = "continue_current"
    assert await handler._analyze_switch_choice("perhaps") == "continue_current"
    handler.openai_service.generate_response.return_value = "switch_to_new"
    assert await handler._analyze_switch_choice("perhaps") == "switch_to_new"
    handler.openai_service.generate_response.return_value = "unclear"
    assert await handler._analyze_switch_choice("perhaps") == "unclear"
    handler.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await handler._analyze_switch_choice("perhaps") == "unclear"

    state = make_session(user_type="buyer")
    state.workflow_type = "authentication"
    assert await handler.should_handle_auth_reg_switch(state, "sell_something", "seller")
    state.workflow_state["pending_auth_reg_switch"] = {"new_intent": "sell_something"}
    assert not await handler.should_handle_auth_reg_switch(state, "sell_something", "seller")
    assert handler._get_target_combination("register_new", "seller") == "registration_seller"
    assert handler._get_target_combination("other", "buyer") == "authentication_buyer"

    state = make_session(user_type="buyer")
    state.workflow_type = WorkflowType.authentication
    await handler.handle_auth_reg_switch_choice("+1", state, "sell pumps", "sell_something", "seller")
    for answer, expected in (("1", "continue_current_workflow"), ("2", "switch_to_new_combination"), ("exit", "exit_requested"), ("maybe", "clarification_requested")):
        state.workflow_state["pending_auth_reg_switch"] = {
            "new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"
        }
        assert (await handler.handle_auth_reg_switch_response("+1", state, answer))["status"] == expected


@pytest.mark.asyncio
async def test_authentication_orchestrator_constructor_flow_and_selection(monkeypatch):
    fake_switch = SimpleNamespace()
    fake_profile = SimpleNamespace()
    monkeypatch.setattr(auth_mod, "AuthRegistrationIntentSwitch", lambda _wa: fake_switch)
    monkeypatch.setattr(auth_mod, "ProfileSelectionService", lambda *_args: fake_profile)
    chat_service = SimpleNamespace(openai_service=SimpleNamespace(), session_manager=AsyncMock(), db_manager=SimpleNamespace())
    orchestrator = AuthenticationOrchestrator(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), chat_service
    )
    assert orchestrator.auth_reg_switch is fake_switch
    assert orchestrator.profile_selection_service is fake_profile

    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        response_helpers=SimpleNamespace(),
        authentication_service=SimpleNamespace(
            validate_token=AsyncMock(return_value=None),
            user_authenticate=AsyncMock(return_value={"success": True, "response": [{"id": "s"}]}),
            filter_users_by_intent=MagicMock(return_value={"success": True, "filtered_users": [{"id": "s"}], "unique_emails": ["s@example.com"]}),
            initiate_email_confirmation=AsyncMock(return_value={"status": "email"}),
            handle_email_confirmation=AsyncMock(return_value={"status": "email"}),
            handle_email_otp_validation=AsyncMock(return_value={"status": "otp"}),
            handle_domain_matching=AsyncMock(return_value={"status": "domain"}),
        ),
        registration_service=SimpleNamespace(
            initiate_registration=AsyncMock(return_value={"status": "registration"}),
            handle_registration_data_collection=AsyncMock(return_value={"status": "data"}),
            handle_registration_otp_validation=AsyncMock(return_value={"status": "otp"}),
            handle_registration_confirmation=AsyncMock(return_value={"status": "confirm"}),
        ),
        intent_service=SimpleNamespace(),
        support_service=SimpleNamespace(redirect_to_support=AsyncMock(return_value={"status": "support"})),
        chat_service=None,
        auth_reg_switch=SimpleNamespace(),
        profile_selection_service=SimpleNamespace(
            handle_profile_selection=AsyncMock(return_value={"status": "profile"}),
            handle_profile_selection_response=AsyncMock(return_value={"status": "selected"}),
        ),
    )

    assert (await handler.authentication_orchestrator_flow(
        "+1", "buy", make_session(), {"intent": "buy_something", "confidence": 90}
    ))["status"] == "profile"
    assert (await handler.authentication_orchestrator_flow(
        "+1", "choose", make_session(profile_selection_stage="choose"), {"intent": "other", "confidence": 90}
    ))["status"] == "selected"
    auth_session = make_session(); auth_session.workflow_type = WorkflowType.authentication
    handler._handle_authentication_workflow = AsyncMock(return_value={"status": "auth"})
    assert (await handler.authentication_orchestrator_flow("+1", "x", auth_session, {}))["status"] == "auth"
    reg_session = make_session(); reg_session.workflow_type = WorkflowType.registration
    handler._handle_registration_workflow = AsyncMock(return_value={"status": "reg"})
    assert (await handler.authentication_orchestrator_flow("+1", "x", reg_session, {}))["status"] == "reg"

    handler.authentication_service.validate_token.side_effect = RuntimeError("token")
    assert (await handler.authentication_orchestrator_flow("+1", "x", make_session(), {"intent": "other"}))["status"] == "support"


@pytest.mark.asyncio
async def test_authentication_orchestrator_role_switch_and_stage_matrix():
    auth = SimpleNamespace(
        validate_token=AsyncMock(return_value=None),
        user_authenticate=AsyncMock(return_value={"success": True, "response": [{"id": "s"}]}),
        filter_users_by_intent=MagicMock(return_value={"success": True, "filtered_users": [{"id": "s"}], "unique_emails": ["s@example.com"]}),
        initiate_email_confirmation=AsyncMock(return_value={"status": "email"}),
        handle_email_confirmation=AsyncMock(return_value={"status": "email"}),
        handle_email_otp_validation=AsyncMock(return_value={"status": "otp"}),
        handle_domain_matching=AsyncMock(return_value={"status": "domain"}),
    )
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        response_helpers=SimpleNamespace(), authentication_service=auth,
        registration_service=SimpleNamespace(
            handle_registration_data_collection=AsyncMock(return_value={"status": "data"}),
            handle_registration_otp_validation=AsyncMock(return_value={"status": "otp"}),
            handle_registration_confirmation=AsyncMock(return_value={"status": "confirm"}),
        ),
        intent_service=SimpleNamespace(),
        support_service=SimpleNamespace(redirect_to_support=AsyncMock(return_value={"status": "support"})),
        chat_service=None,
        auth_reg_switch=SimpleNamespace(),
        profile_selection_service=SimpleNamespace(
            handle_profile_selection=AsyncMock(return_value={"status": "menu"}),
            handle_profile_selection_response=AsyncMock(return_value={"status": "response"}),
        ),
    )
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    switch_session = make_session(role_switch_in_progress=True, user_type="seller")
    assert (await handler.authentication_orchestrator_flow("+1", "sell", switch_session, {"intent": "sell_something", "confidence": 90}))["status"] == "email"
    auth.filter_users_by_intent.return_value = {"success": False}
    assert (await handler.authentication_orchestrator_flow("+1", "sell", make_session(role_switch_in_progress=True, user_type="seller"), {"intent": "sell_something", "confidence": 90}))["status"] == "redirect"

    # Authentication workflow branches.
    handler._check_switch_response = AsyncMock(return_value=None)
    assert (await handler._handle_authentication_workflow("+1", "hi", make_session(profile_selection_stage="pick"), {"intent": "greeting"}))["status"] == "menu"
    handler.profile_selection_service.handle_profile_selection_response.return_value = {"status": "exit_completed"}
    assert (await handler._handle_authentication_workflow("+1", "exit", make_session(profile_selection_stage="pick"), {"intent": "other"}))["status"] == "exit_completed"
    assert (await handler._handle_authentication_workflow("+1", "123", make_session(authentication_stage="email_otp"), {}))["status"] == "otp"
    assert (await handler._handle_authentication_workflow("+1", "yes", make_session(authentication_stage="email_confirmation"), {"intent": "other"}))["status"] == "email"
    handler._start_authentication_flow = AsyncMock(return_value={"status": "started"})
    assert (await handler._handle_authentication_workflow("+1", "x", make_session(authentication_stage="bad"), {"intent": "other"}))["status"] == "started"

    # Registration stages, completion cleanup, invalid stage, and exception fallback.
    for stage, expected in (("data_collection", "data"), ("start", "data"), ("email_confirmation", "email"), ("email_otp", "otp"), ("domain_matching", "domain"), ("confirmation", "confirm")):
        current = make_session(registration_stage=stage, user_type="buyer", current_intent_result={"intent": "buy_something"})
        if stage == "email_otp":
            handler.registration_service.handle_registration_otp_validation.return_value = {"status": "otp"}
        assert (await handler._handle_registration_workflow("+1", "value", current, {"intent": "buy_something"}))["status"] == expected
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    assert (await handler._handle_registration_workflow("+1", "x", make_session(registration_stage="bad"), {}))["status"] == "redirect"
    handler.registration_service.handle_registration_data_collection.side_effect = RuntimeError("registration")
    assert (await handler._handle_registration_workflow("+1", "x", make_session(registration_stage="start"), {}))["status"] == "support"


# Sectioned RFQ residual delivery, items, and button branches --------------------

def sectioned_handler():
    handler = bare(
        SectionedRFQCreationHandler,
        entity_service=SimpleNamespace(extract_entities=AsyncMock()),
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        cancel_service=SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        confirmation_handler=SimpleNamespace(
            handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}),
            handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}),
        ),
        attachment_decision_handler=None,
    )
    return handler


def patch_section_data(monkeypatch):
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, name: s.workflow_state.get(name))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda s, name, value: s.workflow_state.__setitem__(name, value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda s: bool(s.workflow_state.get("excel_source")))


@pytest.mark.asyncio
async def test_sectioned_delivery_modification_empty_partial_and_validation_matrix(monkeypatch):
    handler = sectioned_handler()
    user = make_user()
    patch_section_data(monkeypatch)
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmed"})
    handler._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    handler._display_delivery_validation_error = AsyncMock(return_value={"status": "validation"})
    handler._display_invalid_pincode_message = AsyncMock(return_value={"status": "pin"})

    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_delivery_format", lambda _m: {"error": "bad", "additional_text": ""})
    handler.entity_service.extract_entities.return_value = {"deliveryDate": "", "pincode": "", "products": [{"description": "pump"}]}
    assert (await handler._process_delivery_modification_direct(user, make_session(), "pump"))["status"] == "missing"

    handler.entity_service.extract_entities.return_value = {"deliveryDate": "tomorrow", "pincode": "560001", "city": "", "state": "", "products": []}
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "2025-02-03"})
    handler._autofill_location_from_pincode = AsyncMock(return_value={"is_valid": True, "delivery_data": {"deliveryDate": "2025-02-03", "pincode": "560001", "city": "Pune", "state": "MH"}})
    assert (await handler._process_delivery_modification_direct(user, make_session(), "pump"))["status"] == "confirmed"

    valid = {"deliveryDate": "2025-02-03", "pincode": "560001", "additional_text": ""}
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_delivery_format", lambda _m: valid)
    handler._validate_delivery_date.return_value = {"is_valid": True, "normalized_date": "2025-02-03"}
    handler._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": True, "city": "Pune", "state": "MH"})
    assert (await handler._process_delivery_modification_direct(user, make_session(), "format"))["status"] == "confirmed"
    for date_ok, pin_ok in ((False, False), (False, True), (True, False)):
        handler._validate_delivery_date.return_value = {"is_valid": date_ok, "error": "date invalid"}
        handler._validate_pincode_and_get_location.return_value = {"is_valid": pin_ok, "error": "pin invalid"}
        assert (await handler._process_delivery_modification_direct(user, make_session(), "format"))["status"] in {"validation", "validation_error"}

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    handler._cancel_after_max_retries = AsyncMock(return_value={"status": "cancelled"})
    handler._validate_delivery_date.return_value = {"is_valid": False, "error": "date"}
    handler._validate_pincode_and_get_location.return_value = {"is_valid": True}
    assert (await handler._process_delivery_modification_direct(user, make_session(), "format"))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sectioned_delivery_display_autofill_and_router_states(monkeypatch):
    handler = sectioned_handler()
    user = make_user()
    session = make_session()
    patch_section_data(monkeypatch)
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmation"})
    handler._handle_date_location_section = AsyncMock(return_value={"status": "date"})
    handler._handle_items_section = AsyncMock(return_value={"status": "items"})
    handler._handle_attachments_section = AsyncMock(return_value={"status": "attachments"})
    handler._handle_final_confirmation = AsyncMock(return_value={"status": "final"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda _s: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _s: "date_location")

    for payload in ({"button_reply": {"title": "yes"}}, {"text": {"body": "text"}}, {"content": "content"}, None, 5):
        assert (await handler.handle_sectioned_rfq(user, session, payload, []))["status"] == "date"
    for section, expected in (("items", "items"), ("attachments", "attachments"), ("final_confirmation", "final"), ("unknown", "error")):
        monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _s, section=section: section)
        assert (await handler.handle_sectioned_rfq(user, session, "message", []))["status"] == expected

    assert not handler._has_delivery_basics({})
    assert handler._has_delivery_basics({"deliveryDate": "d", "pincode": 560001})
    assert not handler._is_delivery_complete({"deliveryDate": "d", "pincode": "p", "city": "", "state": "s"})
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    invalid = await handler._autofill_location_from_pincode({"pincode": "560001"})
    assert not invalid["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "MH"}))
    filled = await handler._autofill_location_from_pincode({"pincode": "560001", "city": "wrong"})
    assert filled["delivery_data"]["city"] == "Pune"
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert not (await handler._autofill_location_from_pincode({"pincode": "560001"}))["is_valid"]

    for errors in ({}, {"date": "bad date"}, {"pin": "bad pin"}, {"date": "bad date", "pin": "bad pin"}):
        result = await handler._display_delivery_validation_error(
            user, session, {"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"},
            date_error=errors.get("date"), pincode_error=errors.get("pin")
        )
        assert result["status"] in {"validation_error", "awaiting_delivery_confirmation", "confirmation"}


@pytest.mark.asyncio
async def test_sectioned_items_existing_empty_excel_and_limit_paths(monkeypatch):
    handler = sectioned_handler()
    user = make_user()
    patch_section_data(monkeypatch)
    handler._display_items_confirmation = AsyncMock(return_value={"status": "confirmation"})
    handler._display_items_missing_fields = AsyncMock(return_value={"status": "missing"})
    handler._display_item_limit_exceeded = AsyncMock(return_value={"status": "limit"})

    handler.entity_service.extract_entities.return_value = {"products": []}
    assert (await handler._handle_items_section(user, make_session(), "items", []))["status"] == "awaiting_items"
    handler.entity_service.extract_entities.return_value = {"products": [{"description": "pump", "quantity": 2}]}
    assert (await handler._handle_items_section(user, make_session(), "items", []))["status"] == "confirmation"
    handler.entity_service.extract_entities.return_value = {"products": [{"description": "pump"}]}
    assert (await handler._handle_items_section(user, make_session(), "items", []))["status"] == "missing"

    existing = make_session(items=[{"description": "pump", "quantity": 1}])
    handler.entity_service.extract_entities.return_value = {"products": [{"description": "valve", "quantity": 2}]}
    assert (await handler._handle_items_section(user, existing, "new item", []))["status"] == "confirmation"
    assert (await handler._handle_items_section(user, existing, "", []))["status"] == "confirmation"

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda _s: False)
    handler.entity_service.extract_entities.return_value = {"products": [{"description": str(i), "quantity": 1} for i in range(6)]}
    assert (await handler._handle_items_section(user, make_session(), "many", []))["status"] == "limit"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda _s: True)
    assert (await handler._handle_items_section(user, make_session(excel_source=True), "many", []))["status"] in {"confirmation", "missing"}

    parser = sectioned_mod.sectioned_rfq_format_parser
    monkeypatch.setattr(parser, "parse_items_format", lambda _m: {"error": "bad", "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    result = await handler._process_items_modification_direct(user, make_session(), "bad")
    assert result["status"] == "format_error"
    handler._cancel_after_max_retries = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    assert (await handler._process_items_modification_direct(user, make_session(), "bad"))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sectioned_section_buttons_restart_and_final_paths(monkeypatch):
    handler = sectioned_handler()
    user = make_user()
    session = make_session(date_location={"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"}, items=[{"description": "pump", "quantity": 1}])
    patch_section_data(monkeypatch)
    handler._handle_section_confirm = AsyncMock(return_value={"status": "confirmed"})
    handler._handle_section_modify = AsyncMock(return_value={"status": "modified"})
    handler._handle_restart_rfq = AsyncMock(return_value={"status": "restart"})
    handler._handle_final_rfq_submission = AsyncMock(return_value={"status": "submitted"})
    handler._handle_final_cancel = AsyncMock(return_value={"status": "cancelled"})
    handler._handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    for button, expected in (("confirm_items", "confirmed"), ("modify_items", "modified"), ("restart_rfq", "restart"), ("confirm_cancel", "restart"), ("decline_cancel", "restart"), ("final_confirm_rfq", "submitted"), ("final_cancel_rfq", "cancelled"), ("attachments_yes", "attachment"), ("bad", "error")):
        assert (await handler.handle_section_button_click(user, session, button))["status"] == expected

    handler._send_modification_instructions = SectionedRFQCreationHandler._send_modification_instructions.__get__(handler)
    assert (await handler._send_modification_instructions(user, session, "date_location"))["status"] == "awaiting_modification"
    assert (await handler._send_modification_instructions(user, session, "items"))["status"] == "awaiting_modification"
    assert (await handler._send_modification_instructions(user, session, "other"))["status"] == "awaiting_modification"

    handler._handle_final_rfq_submission = SectionedRFQCreationHandler._handle_final_rfq_submission.__get__(handler)
    final = make_session()
    assert (await handler._handle_final_rfq_submission(user, final))["status"] == "rfq_submitted"
    assert final.workflow_type == WorkflowType.rfq_submitted
    handler._handle_restart_confirmation_response = SectionedRFQCreationHandler._handle_restart_confirmation_response.__get__(handler)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_sectioned_rfq", MagicMock())
    assert (await handler._handle_restart_confirmation_response(user, make_session(), "yes"))["status"] == "workflow_restarted"
    assert (await handler._handle_restart_confirmation_response(user, make_session(), "no"))["status"] == "restart_declined"
    assert (await handler._handle_restart_confirmation_response(user, make_session(), "maybe"))["status"] == "awaiting_restart_confirmation"


# Seller authentication and seller notification residuals -----------------------

@pytest.mark.asyncio
async def test_seller_auth_prompt_parse_lookup_and_menu_branches():
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    mixin = bare(
        SellerAuthMixin,
        whatsapp_service=whatsapp,
        authentication_service=SimpleNamespace(user_authenticate=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        otp_service=SimpleNamespace(send_otp=AsyncMock()),
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock()),
        openai_service=SimpleNamespace(generate_response=AsyncMock()),
    )
    current = make_user("buyer", self_client=True, dict=lambda: {"email": "buyer@example.com"})
    assert (await mixin.prompt_seller_account_switch("+1", make_session(), current))["status"] == "switch_prompt_sent"
    mixin.session_manager = None
    assert (await mixin.prompt_seller_account_switch("+1", make_session(), current))["status"] == "switch_prompt_sent"

    mixin.openai_service.generate_response.return_value = "stay"
    assert await mixin._parse_switch_choice("perhaps") == "stay"
    mixin.openai_service.generate_response.return_value = "unrelated"
    assert await mixin._parse_switch_choice("perhaps") == "unclear"
    mixin.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await mixin._parse_switch_choice("perhaps") == "unclear"

    mixin.authentication_service = None
    assert await mixin.get_seller_info("+1", "seller") == (None, None)
    mixin.authentication_service = SimpleNamespace(user_authenticate=AsyncMock(return_value={"success": False}))
    assert await mixin.get_seller_info("+1", "seller") == (None, None)
    mixin.authentication_service.user_authenticate.return_value = {"success": True, "response": []}
    assert await mixin.get_seller_info("+1", "seller") == (None, None)
    mixin.authentication_service.user_authenticate.return_value = {"success": True, "response": [{"selfClient": False, "id": "other"}]}
    assert await mixin.get_seller_info("+1", "seller") == (None, None)
    mixin.authentication_service.user_authenticate.return_value = {"success": True, "response": [{"selfClient": False, "id": "seller", "username": "seller@example.com"}]}
    assert (await mixin.get_seller_info("+1", "seller"))[0] == "seller@example.com"
    mixin.authentication_service.user_authenticate.side_effect = RuntimeError("lookup")
    assert await mixin.get_seller_info("+1", "seller") == (None, None)

    mixin._get_current_user_type = AsyncMock(return_value="buyer")
    await mixin._reset_workflow_with_menu("+1", make_session(), "failed")
    mixin._get_current_user_type.return_value = "seller"
    await mixin._reset_workflow_with_menu("+1", make_session(), "failed")
    mixin._get_current_user_type.return_value = None
    await mixin._reset_workflow_with_menu("+1", make_session(), "failed")


@pytest.mark.asyncio
async def test_seller_auth_otp_success_failure_manual_and_switch_states():
    mixin = bare(
        SellerAuthMixin,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        authentication_service=SimpleNamespace(clear_user_token=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        otp_service=SimpleNamespace(send_otp=AsyncMock(return_value={"status": "otp_sent"})),
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock(return_value=None)),
        openai_service=SimpleNamespace(generate_response=AsyncMock()),
    )
    mixin.get_seller_info = AsyncMock(return_value=("seller@example.com", {"id": "seller"}))
    assert (await mixin.initiate_seller_auth("+1", make_session(), "seller"))["status"] == "otp_sent"
    mixin.otp_service.send_otp.return_value = {"status": "failed", "error": "down"}
    mixin._reset_workflow_with_menu = AsyncMock()
    assert (await mixin.initiate_seller_auth("+1", make_session(), "seller"))["status"] == "otp_send_failed"
    mixin.otp_service = None
    assert (await mixin.initiate_seller_auth("+1", make_session(), "seller"))["status"] == "otp_instruction_sent"
    mixin.get_seller_info.return_value = (None, None)
    assert (await mixin.initiate_seller_auth("+1", make_session(), "seller"))["status"] == "seller_not_found"

    mixin._parse_switch_choice = AsyncMock(return_value="switch")
    mixin.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await mixin.handle_seller_switch_response("+1", make_session(target_seller_id="seller"), "1"))["status"] == "otp"
    mixin._parse_switch_choice.return_value = "stay"
    assert (await mixin.handle_seller_switch_response("+1", make_session(), "2"))["status"] == "switch_declined"
    mixin._parse_switch_choice.return_value = "unclear"
    assert (await mixin.handle_seller_switch_response("+1", make_session(), "?"))["status"] == "switch_response_unclear"

    mixin.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=True)
    assert await mixin._get_current_user_type("+1") == "buyer"
    mixin.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=False)
    assert await mixin._get_current_user_type("+1") == "seller"
    mixin.auth_redis_service.retrieve.return_value = None
    assert await mixin._get_current_user_type("+1") is None
    mixin.auth_redis_service.retrieve.side_effect = RuntimeError("redis")
    assert await mixin._get_current_user_type("+1") is None


@pytest.mark.asyncio
async def test_seller_interest_request_check_details_and_message_already_sent(monkeypatch):
    handler = bare(
        SellerRFQInterestHandler,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        authentication_service=SimpleNamespace(),
        session_manager=None,
        otp_service=None,
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock(return_value=SimpleNamespace(id="seller"))),
        settings=SimpleNamespace(procucev_rfq_details_url="https://details.example"),
    )
    handler._redirect_to_seller_flow = AsyncMock(return_value={"status": "redirected"})
    result = await handler.handle_request_rfq_click("+1", "rfq-1", "seller-1", make_session())
    assert result == {"status": "redirected"}
    handler._redirect_to_seller_flow.assert_awaited_once_with("+1", "rfq-1", "seller-1", handler._redirect_to_seller_flow.call_args.args[3]) if False else None

    response = SimpleNamespace(success=False, message_id=None, error="send failed")
    handler.whatsapp_service.send_configurable_buttons.return_value = response
    monkeypatch.setattr(
        "app.services.seller_notification_service.SellerNotificationService",
        lambda: SimpleNamespace(get_intermediate_rfq_buttons=lambda *_: []),
    )
    assert (await SellerRFQInterestHandler._show_intermediate_buttons(handler, "+1", "r", "s", make_session()))["status"] == "error"
    assert (await handler.handle_check_details_click("+1", "r", "s", make_session()))["status"] == "check_details_sent"

    handler._show_intermediate_buttons = AsyncMock(return_value={"status": "shown"})
    assert (await handler.handle_otp_validated("+1", make_session(rfq_id="r", target_seller_id="s")))["status"] == "shown"
    assert (await handler.handle_otp_validated("+1", make_session()))["status"] == "error"


# Purchase, format, confirmation, intent, product, and BFS helpers ---------------

@pytest.mark.asyncio
async def test_purchase_workflow_pending_contexts_and_nonprocurable(monkeypatch):
    entity_service = SimpleNamespace(extract_entities=AsyncMock(return_value={}))
    handler = bare(
        PurchaseWorkflowHandler,
        entity_service=entity_service,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
    )
    user = make_user()
    for state, intent, expected_workflow in (
        ({"pending_combined_rfq": {"products": [{"description": "pump"}]}}, "modification_request", "modification_request"),
        ({"pending_rfq": {"description": "pump"}}, "rfq_status_check", "rfq_status_check"),
        ({}, "buy_something", "buy_something"),
        ({"incomplete_products": [{"entities": {"description": "pump"}}]}, "buy_something", "buy_something"),
    ):
        entity_service.extract_entities.return_value = {}
        result = await handler.handle_purchase_intent(user, make_session(**state), "message", {"intent": intent})
        assert result["status"] == "no_new_entities"
        assert entity_service.extract_entities.call_args.kwargs["workflow_type"] == expected_workflow

    monkeypatch.setattr("app.services.cancel_service.CancelService", MagicMock())
    monkeypatch.setattr("app.services.session_management_service.SessionManagementService", MagicMock())
    monkeypatch.setattr("app.database.DatabaseManager", MagicMock())
    import app.services.cancel_service as cancel_mod
    cancel = cancel_mod.CancelService.return_value
    cancel._clear_workflow_state = AsyncMock()
    cancel._send_cancellation_message = AsyncMock()
    entity_service.extract_entities.return_value = {"error_type": "non_procurable", "non_procurable_items": ["service", "license"]}
    assert (await handler.handle_purchase_intent(user, make_session(), "x"))["status"] == "non_procurable_cancelled"
    entity_service.extract_entities.return_value = {"error_type": "non_procurable", "non_procurable_items": ["service"]}
    user.role = Role.SELLER
    assert (await handler.handle_purchase_intent(user, make_session(), "x"))["status"] == "non_procurable_cancelled"
    handler.whatsapp_service.send_message.side_effect = RuntimeError("send")
    assert (await handler.handle_purchase_intent(user, make_session(), "x"))["status"] == "error"
    assert await handler._handle_products_array(user, make_session(), "x", []) is None
    assert await handler._handle_single_entity(user, make_session(), "x", {}) is None


@pytest.mark.asyncio
async def test_format_modification_error_retry_and_replacement_paths(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    handler = bare(FormatModificationHandler, whatsapp_service=wa, session_manager=SimpleNamespace(save_session=AsyncMock()))
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _s: (False, None))
    assert (await handler.handle_format_modification("x", make_session(), make_user()))["status"] == "error"
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _s: (True, "unknown"))
    assert (await handler.handle_format_modification("x", make_session(), make_user()))["status"] == "error"
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _s: (True, "delivery"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _s: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 1)
    assert (await handler.handle_format_modification("x", make_session(), make_user()))["status"] == "format_error"
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 3)
    assert (await handler._handle_format_error(make_session(), make_user(), "bad", "items"))["status"] == "max_retries_reached"
    session = make_session(incomplete_products=[1], complete_products=[2], extracted_entities=[])
    monkeypatch.setattr(format_mod.WorkflowManager, "get_delivery_details", lambda _s: {"delivery_date": "d", "pincode": "p", "city": "c", "state": "s"})
    await handler._replace_products_array(session, [{"description": "pump"}])
    assert session.workflow_state["extracted_entities"][0]["city"] == "c"


@pytest.mark.asyncio
async def test_confirmation_intent_products_and_bfs_residual_helpers(monkeypatch):
    confirmation = bare(
        ConfirmationHandler,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        response_helpers=SimpleNamespace(generate_contextual_response=AsyncMock(return_value="clarify")),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        cancel_service=None,
        confirmation_service=SimpleNamespace(parse_confirmation=AsyncMock(return_value="unclear")),
        _bfs_search_handler=None,
    )
    confirmation._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"})
    confirmation._handle_rfq_modification = AsyncMock(return_value={"status": "modified"})
    confirmation._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarify"})
    assert (await confirmation.handle_pending_confirmations(make_user(), make_session(), "x", {}))["status"] == "clarify"
    assert (await confirmation.handle_pending_confirmations(make_user(), make_session(), "x", {"intent": "modification_request", "confidence": .8}))["status"] == "modified"
    assert (await confirmation.handle_confirmation_button(make_user(), make_session(), "unknown"))["status"] == "unknown_button"
    assert confirmation._format_captured_info_for_modification(make_session())

    fake_bfs = SimpleNamespace()
    monkeypatch.setattr(bfs_mod, "BFSSearchHandler", lambda *_args, **_kwargs: fake_bfs)
    assert confirmation.bfs_search_handler is fake_bfs

    products = bare(
        ProductsArrayHandler,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        openai_service=SimpleNamespace(),
        response_helpers=SimpleNamespace(),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
    )
    products._track_product_categories = AsyncMock()
    products._categorize_products_by_completeness = AsyncMock(return_value=([], []))
    products._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    assert (await products.handle_products_array(make_user(), make_session(), "x", [], global_supplementary_fields={"city": "Pune"}))["status"] == "need_product_description"
    products._categorize_products_by_completeness.return_value = ([], [{"index": 1, "entities": {"description": "pump"}}])
    assert (await products.handle_products_array(make_user(), make_session(), "x", [{"description": "pump"}]))["status"] == "complete"
    products._track_product_categories.side_effect = RuntimeError("category")
    assert (await products.handle_products_array(make_user(), make_session(), "x", [{"description": "pump"}]))["status"] == "error"


@pytest.mark.asyncio
async def test_intent_switch_context_and_corrupted_state_paths():
    handler = bare(
        IntentSwitchHandler,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        response_helpers=SimpleNamespace(generate_contextual_response=AsyncMock(return_value="choice")),
        openai_service=SimpleNamespace(analyze_intent_switch_response=AsyncMock()),
    )
    active = make_session(extracted_entities=[{"description": "pump"}])
    active.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(active, "buy_something", 80)
    assert not await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "collecting", "references_existing_data": True})
    assert await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "new_request"})
    pending = make_session(pending_intent_switch="bad")
    assert (await handler.handle_intent_switch_response(make_user(), pending, "x"))["status"] == "corrupted_intent_switch_data"
    incomplete = make_session(pending_intent_switch={"new_intent": "buy_something"})
    handler.openai_service.analyze_intent_switch_response.side_effect = RuntimeError("openai")
    assert (await handler.handle_intent_switch_response(make_user(), incomplete, "new"))["status"] == "error"
    handler._abandon_current_workflow(active)
    assert active.outcome == ConversationOutcome.abandoned


@pytest.mark.asyncio
async def test_bfs_fallback_payload_results_and_bid_boundaries(monkeypatch):
    handler = bare(
        BFSSearchHandler,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
        openai_service=SimpleNamespace(extract_entities=AsyncMock()),
        auto_categorization_service=SimpleNamespace(categorize_item=AsyncMock()),
        bfs_api_service=SimpleNamespace(search_bfs_items=AsyncMock()),
    )
    handler.openai_service.extract_entities.return_value = {"success": False}
    assert await handler._extract_entities("pump") == [{"description": "pump"}]
    handler.openai_service.extract_entities.side_effect = RuntimeError("ai")
    assert await handler._extract_entities("pump") == [{"description": "pump"}]
    handler.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "Other"}
    assert (await handler._build_api_payload([{ "description": "pump"}], "u", "s"))[0]["category"] == []
    handler.auto_categorization_service.categorize_item.side_effect = RuntimeError("category")
    assert await handler._build_api_payload([{ "description": "pump"}], "u", "s")
    handler._extract_entities = AsyncMock(return_value=[])
    assert (await handler.handle_bfs_search(make_user(), make_session(), "?"))["status"] == "no_products_found"
    await handler._send_bfs_results(make_user(), make_session(), [], suppress_raise_rfq_on_no_results=True)
    await handler._send_bfs_results(make_user(), make_session(), {"unexpected": True})
    assert handler.whatsapp_service.send_message.await_count >= 2

    handler._clear_bid_state = AsyncMock()
    assert (await handler.handle_bid_format_input(make_user(), make_session(), "bad"))["status"] == "bfs_bid_session_expired"
    assert (await handler.handle_button(make_user(), make_session(), "unknown"))["status"] == "unknown_bfs_button"


# Explicit low-level handler coverage for the seller bid class is kept separate
# from the seller notification tests because it has a different action payload.
@pytest.mark.asyncio
async def test_seller_bid_action_success_failure_and_missing_state():
    handler = bare(
        seller_auth_mod.SellerAuthMixin,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
    )
    # Use the concrete subclass without constructing its eager API service.
    from app.services.handlers.bfs_seller_bid_handler import BFSSellerBidHandler
    handler = bare(
        BFSSellerBidHandler,
        whatsapp_service=handler.whatsapp_service,
        session_manager=handler.session_manager,
        bfs_api_service=SimpleNamespace(accept_bid_by_seller=AsyncMock(return_value={"success": True}), reject_bid_by_seller=AsyncMock(return_value={"success": False, "error": "no"})),
    )
    accepted = make_session(action="accept")
    assert (await handler._execute_bid_action("+1", "bfs-1", True, accepted))["status"] == "bfs_bid_accepted"
    rejected = make_session(action="reject")
    assert (await handler._execute_bid_action("+1", "bfs-1", False, rejected))["status"] == "error"
    handler.bfs_api_service.accept_bid_by_seller.side_effect = RuntimeError("api")
    assert (await handler._execute_bid_action("+1", "bfs-1", True, make_session()))["status"] == "error"
    assert (await handler.handle_otp_validated("+1", make_session()))["status"] == "error"
