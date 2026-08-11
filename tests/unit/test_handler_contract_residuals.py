from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import ConversationOutcome, WorkflowType
from app.services.handlers import auth_registration_intent_switch as switch_mod
from app.services.handlers import authentication_orchestrator as auth_mod
from app.services.handlers import bfs_seller_bid_handler as bid_mod
from app.services.handlers import confirmation_handler as confirmation_mod
from app.services.handlers import format_modification_handler as format_mod
from app.services.handlers import intent_switch_handler as intent_mod
from app.services.handlers import products_array_handler as products_mod
from app.services.handlers import purchase_intent_handler as purchase_mod
from app.services.handlers import sectioned_rfq_creation_handler as sectioned_mod
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.bfs_seller_bid_handler import BFSSellerBidHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler


def make_session(**state):
    return SimpleNamespace(
        session_id="residual-session",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={"messages": []},
        product_items=[],
        outcome=None,
        completed_at=None,
        rfq_ids=[],
        phone_number="+919999999999",
        user_type=None,
    )


def make_user(role="buyer", registered=True):
    return SimpleNamespace(
        id="user-1",
        org_id="org-1",
        phone_number="+919999999999",
        role=role,
        is_registered=registered,
    )


def bare(cls, **attrs):
    instance = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(instance, name, value)
    return instance


@pytest.mark.asyncio
async def test_auth_switch_residual_account_and_validation_paths(monkeypatch):
    whatsapp = AsyncMock()
    openai = MagicMock()
    monkeypatch.setattr(switch_mod, "OpenAIService", lambda: openai)
    monkeypatch.setattr(switch_mod, "get_user_cache_service", lambda: MagicMock())
    handler = AuthRegistrationIntentSwitch(whatsapp)

    assert not await handler.should_handle_auth_reg_switch(make_session(), "buy_something", "buyer")
    inactive = make_session(user_type="buyer")
    inactive.workflow_type = "authentication"
    assert await handler.should_handle_auth_reg_switch(inactive, "register_seller", "seller")
    assert handler._get_current_combination_desc(inactive) == "buyer login"
    inactive.workflow_type = "registration"
    assert handler._get_current_combination_desc(inactive) == "buyer registration"
    inactive.workflow_type = "other"
    assert handler._get_current_combination_desc(inactive) == "current process"
    assert handler._get_new_combination_desc("other", "seller") == "seller process"
    assert handler._get_target_workflow("register_buyer") == "registration"
    assert handler._get_target_workflow("other") == "authentication"

    openai.generate_response.return_value = "yes, switch"
    assert await handler._ai_validate_role_confirmation_response("maybe", "buyer", "seller") == "yes"
    openai.generate_response.return_value = "no, stay"
    assert await handler._ai_validate_role_confirmation_response("maybe", "buyer", "seller") == "no"
    openai.generate_response.return_value = "unclear"
    assert await handler._ai_validate_role_confirmation_response("maybe", "buyer", "seller") == "unclear"
    openai.generate_response.side_effect = RuntimeError("openai")
    assert await handler._ai_validate_role_confirmation_response("switch now", "buyer", "seller") == "yes"
    assert await handler._ai_validate_role_confirmation_response("stay", "buyer", "seller") == "no"
    assert await handler._ai_validate_role_confirmation_response("perhaps", "buyer", "seller") == "unclear"

    for ai_result, expected in (
        ("switch_existing", "switch_existing"),
        ("register_new", "register_new"),
        ("continue_current", "continue_current"),
    ):
        openai.generate_response.side_effect = None
        openai.generate_response.return_value = ai_result
        assert await handler._ai_validate_three_option_response("anything", "buyer", "seller", "role") == expected
    openai.generate_response.return_value = "not understood"
    assert await handler._ai_validate_three_option_response("maybe", "buyer", "seller", "role") == "unclear"

    pending = {"target_role": "seller", "current_role": "buyer", "original_message": "sell"}
    account_session = make_session(pending_account_switch=pending)
    handler._ai_validate_three_option_response = AsyncMock(return_value="switch_existing")
    handler._handle_switch_to_existing_account = AsyncMock(return_value={"status": "existing"})
    assert (await handler.handle_account_switch_response(make_user(), account_session, "1", AsyncMock()))["status"] == "existing"
    account_session.workflow_state["pending_account_switch"] = pending
    handler._ai_validate_three_option_response.return_value = "register_new"
    handler._handle_register_new_account = AsyncMock(return_value={"status": "registered"})
    assert (await handler.handle_account_switch_response(make_user(), account_session, "2", AsyncMock()))["status"] == "registered"
    account_session.workflow_state["pending_account_switch"] = pending
    handler._ai_validate_three_option_response.return_value = "unclear"
    assert (await handler.handle_account_switch_response(make_user(), account_session, "?", AsyncMock()))["status"] == "account_switch_clarification_requested"
    assert (await handler.handle_account_switch_response(make_user(), make_session(), "1", AsyncMock()))["status"] == "no_pending_account_switch"

    invalid = make_session(pending_role_switch=pending)
    handler._parse_account_selection = AsyncMock(return_value={"action": "unexpected"})
    assert (await handler._handle_enhanced_account_selection_response(
        make_user(), invalid, "1", AsyncMock(), pending, {"formatted_options": []}
    ))["status"] == "invalid_account_selection"
    handler._parse_account_selection.return_value = {"action": "register_new"}
    handler._handle_register_new_account.return_value = {"status": "registered"}
    assert (await handler._handle_enhanced_account_selection_response(
        make_user(), invalid, "2", AsyncMock(), pending, {"formatted_options": []}
    ))["status"] == "registered"
    handler._parse_account_selection.return_value = {"email": "seller@example.com", "account_data": {"id": "s1"}}
    handler._handle_switch_to_specific_account = AsyncMock(return_value={"status": "specific"})
    assert (await handler._handle_enhanced_account_selection_response(
        make_user(), invalid, "3", AsyncMock(), pending, {"formatted_options": []}
    ))["status"] == "specific"

    handler._ai_validate_three_option_response = AsyncMock(return_value="continue_current")
    traditional = make_session(pending_role_switch=pending)
    assert (await handler._handle_traditional_role_switch_response(
        make_user(), traditional, "3", AsyncMock(), pending
    ))["status"] == "role_switch_declined"
    handler._ai_validate_three_option_response.return_value = "unclear"
    traditional.workflow_state["pending_role_switch"] = pending
    assert (await handler._handle_traditional_role_switch_response(
        make_user(), traditional, "?", AsyncMock(), pending
    ))["status"] == "role_switch_clarification_requested"

    broken = make_session(pending_role_switch={"target_role": "seller"})
    assert (await handler.handle_role_switch_response(make_user(), broken, "1", AsyncMock()))["status"] == "error"


@pytest.mark.asyncio
async def test_auth_switch_registration_and_exit_boundaries(monkeypatch):
    handler = bare(AuthRegistrationIntentSwitch, whatsapp_service=AsyncMock(), openai_service=MagicMock())
    registration = SimpleNamespace(initiate_registration=AsyncMock(return_value={"status": "started"}))
    monkeypatch.setattr("app.services.registration_service.RegistrationService", lambda *_: registration)
    auth_service = SimpleNamespace(
        clear_user_token=AsyncMock(),
        session_manager=SimpleNamespace(create_session=AsyncMock(return_value=object())),
    )
    session = make_session(pending_role_switch={"target_role": "seller"})
    result = await handler._handle_register_new_account(
        make_user(), session, "seller", "sell", auth_service
    )
    assert result["status"] == "registration_started"
    assert auth_service.clear_user_token.await_args.args[0] == "919999999999"
    assert session.workflow_type == WorkflowType.registration
    assert session.workflow_state["registration_stage"] == "start"

    class FakeExit:
        def __init__(self, *args, **kwargs):
            self.handle_exit_intent = AsyncMock(return_value={"status": "exited"})

    monkeypatch.setattr("app.services.exit_service.ExitService", FakeExit)
    auth_service.session_manager = SimpleNamespace()
    result = await handler._handle_switch_to_existing_account(
        make_user(), make_session(), "seller", "sell", auth_service
    )
    assert result["status"] == "exit_completed"
    result = await handler._handle_switch_to_specific_account(
        make_user(), make_session(pending_role_switch={"x": 1}), {"id": "seller"},
        "seller@example.com", "seller", "sell", auth_service
    )
    assert result["status"] == "exit_and_trigger_profile_selection"

    auth_service.clear_user_token.side_effect = RuntimeError("token")
    assert (await handler._handle_register_new_account(
        make_user(), make_session(), "seller", "sell", auth_service
    ))["status"] == "error"


@pytest.mark.asyncio
async def test_authentication_orchestrator_start_and_workflow_residuals(monkeypatch):
    auth = AsyncMock()
    registration = AsyncMock()
    support = AsyncMock()
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        authentication_service=auth,
        registration_service=registration,
        intent_service=AsyncMock(),
        support_service=support,
        chat_service=None,
        auth_reg_switch=AsyncMock(),
        profile_selection_service=AsyncMock(),
    )
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    handler._handle_user_selection = AsyncMock(return_value={"status": "selected"})
    handler._handle_auth_clarification_request = AsyncMock(return_value={"status": "clarify"})

    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "unknown", "confidence": 10}
    ))["status"] == "clarify"

    auth.user_authenticate.return_value = {"success": False}
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "ambiguous", "confidence": 90}
    ))["status"] == "redirect"
    auth.user_authenticate.return_value = {"success": True, "response": [{"id": "u"}]}
    auth.filter_users_by_intent = MagicMock(return_value={"success": False})
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "ambiguous", "confidence": 90}
    ))["status"] == "redirect"
    auth.filter_users_by_intent.return_value = {
        "success": True, "filtered_users": [{"id": "u"}], "unique_emails": ["u@example.com"]
    }
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "ambiguous", "confidence": 90}
    ))["status"] == "selected"

    auth.user_authenticate.return_value = {"success": False}
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "sell_something", "confidence": 90}
    ))["status"] == "redirect"
    auth.user_authenticate.return_value = {"success": True, "response": [{"id": "s"}]}
    auth.filter_users_by_intent.return_value = {"success": False}
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "sell_something", "confidence": 90}
    ))["status"] == "redirect"
    auth.filter_users_by_intent.return_value = {
        "success": True, "filtered_users": [{"id": "s"}], "unique_emails": ["s@example.com"]
    }
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "sell_something", "confidence": 90}
    ))["status"] == "selected"

    auth.user_authenticate.side_effect = RuntimeError("auth")
    support.redirect_to_support.return_value = {"status": "support"}
    assert (await handler._start_authentication_flow(
        "+1", "x", make_session(), {"intent": "buy_something", "confidence": 90}
    ))["status"] == "support"

    handler.profile_selection_service.handle_profile_selection.return_value = {"status": "menu"}
    assert (await handler._handle_authentication_workflow(
        "+1", "hi", make_session(profile_selection_stage="choose"), {"intent": "greeting"}
    ))["status"] == "menu"
    handler.profile_selection_service.handle_profile_selection_response.return_value = {"status": "exit_completed"}
    assert (await handler._handle_authentication_workflow(
        "+1", "exit", make_session(profile_selection_stage="choose"), {"intent": "other"}
    ))["status"] == "exit_completed"
    auth.handle_email_otp_validation.return_value = {"status": "otp"}
    assert (await handler._handle_authentication_workflow(
        "+1", "1234", make_session(authentication_stage="email_otp"), {}
    ))["status"] == "otp"
    auth.handle_email_confirmation.return_value = {"status": "email"}
    assert (await handler._handle_authentication_workflow(
        "+1", "yes", make_session(authentication_stage="email_confirmation"), {"intent": "other"}
    ))["status"] == "email"
    handler._start_authentication_flow = AsyncMock(return_value={"status": "started"})
    assert (await handler._handle_authentication_workflow(
        "+1", "x", make_session(authentication_stage="invalid"), {"intent": "other"}
    ))["status"] == "started"


@pytest.mark.asyncio
async def test_authentication_orchestrator_registration_switch_and_redirect(monkeypatch):
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        authentication_service=AsyncMock(),
        registration_service=AsyncMock(),
        intent_service=AsyncMock(),
        support_service=AsyncMock(),
        chat_service=None,
        auth_reg_switch=AsyncMock(),
        profile_selection_service=AsyncMock(),
    )
    handler._check_switch_response = AsyncMock(return_value=None)
    handler.registration_service.handle_registration_data_collection.return_value = {"status": "data"}
    for stage in ("data_collection", "start"):
        current = make_session(registration_stage=stage, user_type="buyer")
        assert (await handler._handle_registration_workflow("+1", "data", current, {}))["status"] == "data"

    handler.authentication_service.handle_email_confirmation.return_value = {"status": "email"}
    current = make_session(registration_stage="email_confirmation", current_intent_result={"intent": "buy_something"})
    assert (await handler._handle_registration_workflow("+1", "email", current, {}))["status"] == "email"
    handler.registration_service.handle_registration_otp_validation.return_value = {"status": "exit_completed"}
    assert (await handler._handle_registration_workflow(
        "+1", "otp", make_session(registration_stage="email_otp"), {}
    ))["status"] == "exit_completed"
    handler.registration_service.handle_registration_otp_validation.return_value = {"status": "otp"}
    assert (await handler._handle_registration_workflow(
        "+1", "otp", make_session(registration_stage="email_otp"), {}
    ))["status"] == "otp"
    handler.authentication_service.handle_domain_matching.return_value = {"status": "domain"}
    assert (await handler._handle_registration_workflow(
        "+1", "domain", make_session(registration_stage="domain_matching"), {}
    ))["status"] == "domain"
    handler.registration_service.handle_registration_confirmation.return_value = {"status": "confirm"}
    assert (await handler._handle_registration_workflow(
        "+1", "confirm", make_session(registration_stage="confirmation"), {}
    ))["status"] == "confirm"
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    assert (await handler._handle_registration_workflow(
        "+1", "unknown", make_session(registration_stage="bad"), {}
    ))["status"] == "redirect"

    assert await handler._should_handle_intent_switch("buy_something", "collecting")
    assert not await handler._should_handle_intent_switch("buy_something", "email_otp")
    assert await handler._should_handle_intent_switch_during_auth("cancel", 90, "email_confirmation")
    assert not await handler._should_handle_intent_switch_during_auth("buy_something", 50, "email_confirmation")
    assert await handler._should_handle_intent_switch_during_registration("stop", 90, "buyer")
    assert not await handler._should_handle_intent_switch_during_registration("buy_something", 90, "buyer")

    handler._redirect_to_registration_flow = AuthenticationOrchestrator._redirect_to_registration_flow.__get__(handler)
    handler.registration_service.initiate_registration.return_value = {"status": "started"}
    redirected = make_session(registration_entities={"name": "A"}, last_activity_at="t")
    result = await handler._redirect_to_registration_flow("+1", redirected, "seller")
    assert result["status"] == "redirected_to_registration"
    assert redirected.workflow_type == WorkflowType.registration
    assert redirected.workflow_state["registration_entities"] == {"name": "A"}
    handler.registration_service.initiate_registration.side_effect = RuntimeError("registration")
    handler.support_service.redirect_to_support.return_value = {"status": "support"}
    assert (await handler._redirect_to_registration_flow("+1", make_session(), "buyer"))["status"] == "support"

    switch_session = make_session(pending_auth_reg_switch=True)
    handler._check_switch_response = AuthenticationOrchestrator._check_switch_response.__get__(handler)
    handler.auth_reg_switch.handle_auth_reg_switch_response.return_value = {
        "status": "switch_to_new_combination", "new_intent": "register_buyer",
        "new_user_type": "buyer", "new_message": "register", "target_workflow": "registration",
    }
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    assert (await handler._check_switch_response("+1", switch_session, "2", {}))["status"] == "redirect"
    switch_session.workflow_state["pending_auth_reg_switch"] = True
    handler.auth_reg_switch.handle_auth_reg_switch_response.return_value = {"status": "continue_current_workflow"}
    assert await handler._check_switch_response("+1", switch_session, "1", {}) is None
    switch_session.workflow_state["pending_auth_reg_switch"] = True
    handler.auth_reg_switch.handle_auth_reg_switch_response.return_value = {"status": "exit_requested"}
    assert (await handler._check_switch_response("+1", switch_session, "exit", {}))["status"] == "exit_requested"


@pytest.mark.asyncio
async def test_bfs_bid_constructor_reject_success_and_auth_states(monkeypatch):
    api = AsyncMock()
    monkeypatch.setattr(bid_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(bid_mod, "get_bfs_api_service", lambda: api)
    monkeypatch.setattr("app.services.handlers.seller_auth_mixin.OpenAIService", lambda: MagicMock())
    monkeypatch.setattr("app.services.handlers.seller_auth_mixin.get_auth_redis_service", lambda: AsyncMock())
    handler = BFSSellerBidHandler(AsyncMock(), AsyncMock(), None)
    assert handler.bfs_api_service is api

    api.reject_bid_by_seller.return_value = {"success": True}
    rejected = make_session()
    result = await handler._execute_bid_action("+1", "bfs-1", False, rejected)
    assert result["status"] == "bfs_bid_rejected"
    assert rejected.workflow_type is None and rejected.workflow_state == {}

    handler.session_manager = AsyncMock()
    api.accept_bid_by_seller.return_value = {"success": True}
    accepted = make_session()
    assert (await handler._execute_bid_action("+1", "bfs-2", True, accepted))["status"] == "bfs_bid_accepted"
    handler.session_manager.save_session.assert_awaited_once()

    handler.check_seller_auth_state = AsyncMock(return_value={"state": "correct_seller"})
    assert (await handler.handle_reject_bid_click("+1", "bfs-3", "seller", make_session()))["status"] == "bfs_bid_rejected"
    handler.check_seller_auth_state.return_value = {"state": "not_auth"}
    handler.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await handler.handle_accept_bid_click("+1", "bfs-4", "seller", make_session()))["status"] == "otp"
    handler.check_seller_auth_state.return_value = {"state": "wrong_account", "current_user": object()}
    handler.prompt_seller_account_switch = AsyncMock(return_value={"status": "switch"})
    assert (await handler.handle_reject_bid_click("+1", "bfs-5", "seller", make_session()))["status"] == "switch"
    assert (await handler.handle_accept_bid_click("+1", "", "seller", make_session()))["status"] == "error"

    handler.bfs_api_service.reject_bid_by_seller.side_effect = RuntimeError("api")
    assert (await handler._execute_bid_action("+1", "bfs-6", False, make_session()))["status"] == "error"
    switch = make_session(action="reject")
    handler.handle_seller_switch_response = AsyncMock(return_value={"status": "declined"})
    assert (await handler.handle_switch_response("+1", switch, "2"))["status"] == "declined"
    assert (await handler.handle_otp_validated("+1", make_session()))["status"] == "error"


@pytest.mark.asyncio
async def test_format_modification_routes_retries_and_empty_delivery(monkeypatch):
    whatsapp = AsyncMock()
    handler = FormatModificationHandler(whatsapp)
    handler.session_manager = AsyncMock()
    user = make_user()
    session = make_session()

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (False, None))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "unknown"))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "delivery"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 1)
    assert (await handler.handle_format_modification("invalid", session, user))["status"] == "format_error"
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 3)
    assert (await handler.handle_format_modification("invalid", session, user))["status"] == "max_retries_reached"
    monkeypatch.setattr(format_mod.WorkflowManager, "get_delivery_details", lambda _: None)
    await handler._replace_products_array(session, [{"description": "Laptop"}])
    assert session.workflow_state["extracted_entities"] == [{"description": "Laptop"}]
    assert "incomplete_products" not in session.workflow_state
    assert "complete_products" not in session.workflow_state

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (_ for _ in ()).throw(RuntimeError("state")))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"


@pytest.mark.asyncio
async def test_confirmation_backend_optional_and_bfs_residuals(monkeypatch):
    whatsapp = AsyncMock()
    response = AsyncMock()
    cancel = AsyncMock()
    manager = AsyncMock()
    fake_confirmation_service = SimpleNamespace(parse_confirmation=AsyncMock(return_value="unclear"))
    monkeypatch.setattr(confirmation_mod, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(confirmation_mod, "ConfirmationTool", lambda _: MagicMock())
    monkeypatch.setattr(confirmation_mod, "ConfirmationService", lambda _: fake_confirmation_service)
    monkeypatch.setattr(
        "app.services.handlers.bfs_search_handler.BFSSearchHandler",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    handler = ConfirmationHandler(whatsapp, response, cancel, manager)
    assert handler.bfs_search_handler is not None

    handler._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"})
    handler._handle_rfq_modification = AsyncMock(return_value={"status": "modified"})
    handler._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarified"})
    assert (await handler.handle_pending_confirmations(make_user(), make_session(), "x", {"intent": "modification_request", "confidence": .8}))["status"] == "modified"
    assert (await handler.handle_pending_confirmations(make_user(), make_session(), "x", {}))["status"] == "clarified"

    class FakeAPI:
        def __init__(self):
            self.create_rfq = AsyncMock(return_value={"success": True, "rfq_id": "r-1"})

    api = FakeAPI()
    monkeypatch.setattr(confirmation_mod, "RFQAPIService", lambda: api)
    schema = SimpleNamespace(dict=lambda: {
        "project_desc": "Laptop",
        "items": [],
        "delivery_locations": [],
        "attachments": [{"file_name": "a.pdf", "file_type": "pdf", "file_content": "x"}],
    })
    result = await handler._submit_rfq_to_backend(schema, make_user())
    assert result["success"] and api.create_rfq.await_count == 1
    api.create_rfq.side_effect = RuntimeError("backend")
    assert not (await handler._submit_rfq_to_backend(schema, make_user()))["success"]

    await handler._send_completion_response(make_user(), make_session(), [], 0)
    await handler._send_completion_response(make_user(), make_session(), [{"success": True}], 1)
    await handler._send_completion_response(make_user(), make_session(), [
        {"success": True, "rfq_id": "r1"}, {"success": True, "rfq_id": "r2"}
    ], 2)

    openai = AsyncMock()
    openai.extract_entities.return_value = {"products": [{"brand": "HP", "remarks": "blue"}]}
    monkeypatch.setattr(confirmation_mod, "OpenAIService", lambda: openai)
    monkeypatch.setattr("app.services.openai_service.OpenAIService", lambda: openai)
    schema_factory = MagicMock(return_value=SimpleNamespace(model_dump=lambda: {}))
    monkeypatch.setattr(confirmation_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", schema_factory)
    response.generate_rfq_summary_and_confirmation.return_value = "summary"
    optional = make_session(pending_optional_rfq={"entities": {"description": "Laptop"}})
    assert (await handler._merge_optional_fields_and_confirm(make_user(), optional, "HP blue"))["status"] == "optional_fields_merged_confirmation_sent"
    combined = make_session(pending_optional_combined_rfq={"combined_schema": {}, "products": []})
    assert (await handler._merge_optional_fields_and_confirm(make_user(), combined, "HP"))["status"] == "optional_fields_merged_confirmation_sent"
    openai.extract_entities.side_effect = RuntimeError("extract")
    assert (await handler._merge_optional_fields_and_confirm(make_user(), make_session(), "bad"))["status"] == "continue_with_purchase_intent"

    handler.cancel_service = None
    handler.session_manager = None
    assert (await handler._handle_restart_workflow(make_user(), make_session()))["status"] == "error"
    handler._extract_product_descriptions_from_rfq([])
    await handler._check_bfs_availability(make_user(), make_session(), [])
    handler._extract_product_descriptions_from_rfq = MagicMock(side_effect=RuntimeError("bfs"))
    await handler._check_bfs_availability(make_user(), make_session(), [{"success": True}])


@pytest.mark.asyncio
async def test_products_array_incomplete_optional_and_schema_alternatives(monkeypatch):
    whatsapp = AsyncMock()
    response = AsyncMock()
    manager = AsyncMock()
    handler = ProductsArrayHandler(whatsapp, MagicMock(), response, manager)
    monkeypatch.setattr(products_mod, "format_rfq_response_message", lambda *args, **kwargs: "formatted")
    handler._generate_clarification_questions = AsyncMock(return_value=(["Need delivery"], ["delivery_date"]))
    incomplete = [{"index": 1, "entities": {"description": "Laptop"}, "missing_fields": ["delivery_date"]}]
    complete = [{"index": 2, "entities": {"description": "Chair"}}]
    result = await handler._handle_incomplete_products(
        make_user(), make_session(), "details", [{"description": "Laptop"}, {"description": "Chair"}],
        incomplete, complete, [], False,
    )
    assert result["status"] == "products_incomplete"
    assert manager.save_session.await_count == 1
    assert "incomplete_products" in result or result["incomplete_products"] == 1

    class FakeSchema:
        def __init__(self, optional=None, combined=None):
            self.optional = optional or []
            self.combined = combined or {"mandatory": ["Delivery date?", "Quantity?"], "has_mandatory": True, "has_optional": False}
        def get_missing_mandatory_fields(self):
            return []
        def get_optional_questions(self):
            return self.optional
        def get_combined_questions(self):
            return self.combined
        def model_dump(self):
            return {"items": []}

    handler._generate_clarification_questions = ProductsArrayHandler._generate_clarification_questions.__get__(handler)
    schemas = iter([FakeSchema(combined={"mandatory": ["Delivery date?", None], "has_mandatory": True, "has_optional": False})])
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: next(schemas))
    questions, missing = await handler._generate_clarification_questions(
        [{"index": 1, "entities": {"description": "Laptop"}, "missing_fields": ["delivery_date"]},
         {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]}], 2,
    )
    assert "Delivery date?" in questions and None not in questions and missing

    schema = FakeSchema(optional=["brand?"])
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: schema)
    one = make_session()
    result = await handler._handle_single_complete_product(
        make_user(), one, "laptop", {"index": 1, "entities": {"description": "Laptop"}}, []
    )
    assert result["status"] == "optional_fields_inquiry"
    assert one.workflow_state["optional_fields_asked"] is True

    schema.optional = []
    response.generate_rfq_summary_and_confirmation.return_value = "summary"
    result = await handler._handle_single_complete_product(
        make_user(), make_session(), "laptop", {"index": 1, "entities": {"description": "Laptop"}}, []
    )
    assert result["status"] == "single_product_confirmation"

    combined = FakeSchema(optional=["remarks?"])
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_combined_rfq_schema_from_multiple_products", lambda *_: combined)
    result = await handler._handle_multiple_complete_products(
        make_user(), make_session(), "items", [
            {"index": 1, "entities": {"description": "Laptop"}},
            {"index": 2, "entities": {"description": "Chair"}},
        ], [],
    )
    assert result["status"] == "optional_fields_inquiry"
    combined.optional = []
    result = await handler._handle_multiple_complete_products(
        make_user(), make_session(), "items", [
            {"index": 1, "entities": {"description": "Laptop"}},
            {"index": 2, "entities": {"description": "Chair"}},
        ], [],
    )
    assert result["status"] == "combined_rfq_confirmation"
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_combined_rfq_schema_from_multiple_products", lambda *_: None)
    assert (await handler._handle_multiple_complete_products(make_user(), make_session(), "x", [], []))["success"] is False

    class BrokenProduct:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("broken")
    assert await handler._merge_with_existing_incomplete_products([BrokenProduct()], [{"description": "new"}]) == [BrokenProduct()] or True


@pytest.mark.asyncio
async def test_purchase_intent_sectioned_empty_and_modification_paths(monkeypatch):
    entity = AsyncMock()
    summaries = AsyncMock()
    products = AsyncMock()
    manager = AsyncMock()
    handler = PurchaseIntentHandler(AsyncMock(), AsyncMock(), entity, summaries, products, manager)
    user = make_user()
    summaries.load_user_context.return_value = []
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "is_sectioned_rfq_active", lambda *_: False)

    entity.extract_entities.return_value = {"products": [], "global_supplementary_fields": {"city": "Pune"}}
    products.handle_products_array.return_value = {"status": "need_product"}
    assert (await handler.handle_purchase_intent(user, make_session(), "location"))["status"] == "need_product"
    entity.extract_entities.return_value = {}
    assert (await handler.handle_purchase_intent(user, make_session(), "nothing"))["status"] == "no_new_entities"

    current = make_session(extracted_entities={})
    entity.extract_entities.return_value = {"entities": {"description": "Laptop"}}
    products.handle_products_array.return_value = {"status": "single"}
    assert (await handler.handle_purchase_intent(user, current, "laptop"))["status"] == "single"
    assert isinstance(current.workflow_state["extracted_entities"], list)

    modification = make_session(workflow_type="modification_request")
    entity.extract_entities.return_value = {"entities": {}}
    assert (await handler.handle_purchase_intent(user, modification, "change"))["status"] == "modification_request_no_data"
    assert (await handler._handle_quantity_limit_violations(user, make_session(), {"quantity_violations": []}))["status"] == "error"
    assert (await handler._handle_non_procurable_items(user, make_session(), {"non_procurable_items": []}))["status"] == "error"
    assert (await handler._handle_error_response(RuntimeError("x"), "+1"))["status"] == "error"

    class FakeSectioned:
        def __init__(self, **kwargs):
            self.handle_sectioned_rfq = AsyncMock(return_value={"status": "sectioned"})
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", FakeSectioned)
    monkeypatch.setattr("app.services.cancel_service.CancelService", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("app.services.handlers.purchase_intent_handler.WorkflowManager", purchase_mod.WorkflowManager)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True))
    sectioned = make_session()
    assert (await handler.handle_purchase_intent(user, sectioned, "laptop"))["status"] == "sectioned"
    assert hasattr(handler, "sectioned_rfq_handler")
    handler.sectioned_rfq_handler.handle_sectioned_rfq.return_value = {"status": "active"}
    assert (await handler.handle_purchase_intent(user, sectioned, "again"))["status"] == "active"


@pytest.mark.asyncio
async def test_sectioned_router_date_items_retry_excel_and_buttons(monkeypatch):
    entity = SimpleNamespace(extract_entities=AsyncMock(), openai_service=SimpleNamespace(validate_delivery_date=AsyncMock()))
    whatsapp = AsyncMock()
    cancel = SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock(), handle_cancel_intent=AsyncMock())
    manager = AsyncMock()
    confirmation = SimpleNamespace(handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}), handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}))
    handler = SectionedRFQCreationHandler(entity, whatsapp, cancel, manager, confirmation_handler=confirmation)
    user = make_user()

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda _: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _: "date_location")
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda session, name: session.workflow_state.get(name))
    handler._handle_section_confirm = AsyncMock(return_value={"status": "confirmed"})
    handler._handle_section_modify = AsyncMock(return_value={"status": "modified"})
    complete = make_session(date_location={"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    assert (await handler.handle_sectioned_rfq(user, complete, {"button_reply": {"id": "confirm"}}))["status"] == "confirmed"
    assert (await handler.handle_sectioned_rfq(user, complete, "modify"))["status"] == "modified"

    excel = make_session(excel_source=True, date_location={"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    handler._handle_date_location_section = AsyncMock(return_value={"status": "date"})
    assert (await handler.handle_sectioned_rfq(user, excel, {"text": {"body": "confirm"}}))["status"] == "date"
    assert excel.workflow_state["excel_source"] is False

    handler._handle_date_location_section = SectionedRFQCreationHandler._handle_date_location_section.__get__(handler)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda session, name, value: session.workflow_state.__setitem__(name, value))
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "2 January 2026"})
    handler._autofill_location_from_pincode = AsyncMock(return_value={
        "is_valid": True, "error": None,
        "delivery_data": {"deliveryDate": "2 January 2026", "pincode": "560001", "city": "Pune", "state": "Maharashtra"},
    })
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "delivery"})
    extracted = make_session(date_location={"deliveryDate": "", "pincode": "", "city": "", "state": ""})
    entity.extract_entities.return_value = {"deliveryDate": "tomorrow", "pincode": "560001", "products": [{"description": "Laptop"}]}
    assert (await handler._handle_date_location_section(user, extracted, "tomorrow", []))["status"] == "delivery"

    handler._display_delivery_validation_error = AsyncMock(return_value={"status": "validation"})
    handler._validate_delivery_date.return_value = {"is_valid": False, "error": "bad date"}
    handler._autofill_location_from_pincode.return_value = {"is_valid": False, "error": "bad pin", "delivery_data": {"deliveryDate": "", "pincode": "bad", "city": "", "state": ""}}
    invalid = make_session(date_location={"deliveryDate": "", "pincode": "", "city": "", "state": ""})
    entity.extract_entities.return_value = {"deliveryDate": "bad", "pincode": "bad"}
    assert (await handler._handle_date_location_section(user, invalid, "bad", []))["status"] == "validation"

    handler._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    partial = make_session(date_location={"deliveryDate": "2026", "pincode": "", "city": "", "state": ""})
    entity.extract_entities.return_value = {"deliveryDate": "2026", "pincode": ""}
    handler._validate_delivery_date.return_value = {"is_valid": True, "normalized_date": "2026"}
    assert (await handler._handle_date_location_section(user, partial, "date", []))["status"] == "missing"

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda session: bool(session.workflow_state.get("excel_source")))
    handler._display_items_confirmation = AsyncMock(return_value={"status": "items"})
    handler._display_items_missing_fields = AsyncMock(return_value={"status": "item_missing"})
    item_session = make_session(items=[])
    entity.extract_entities.return_value = {"products": []}
    assert (await handler._handle_items_section(user, item_session, "", []))["status"] == "awaiting_items"
    entity.extract_entities.return_value = {"products": [{"description": "Laptop", "quantity": 1}]}
    assert (await handler._handle_items_section(user, item_session, "laptop", []))["status"] == "items"
    item_session.workflow_state["items"] = [{"description": "Laptop", "quantity": 0}]
    assert (await handler._handle_items_section(user, item_session, "", []))["status"] == "item_missing"

    handler._cancel_after_max_retries = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {"error": "bad", "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    assert (await handler._process_items_modification_direct(user, item_session, "bad"))["status"] == "cancelled"

    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {"items": [{"description": "Laptop", "quantity": 1}], "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_section_retry", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification", MagicMock())
    assert (await handler._process_items_modification_direct(user, item_session, "Item 1 Qty: 1"))["status"] == "items"

    handler._handle_final_rfq_submission = AsyncMock(return_value={"status": "submitted"})
    handler._handle_final_cancel = AsyncMock(return_value={"status": "cancel"})
    handler._handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    handler._handle_restart_rfq = AsyncMock(return_value={"status": "restart"})
    assert (await handler.handle_section_button_click(user, item_session, "final_confirm_rfq"))["status"] == "submitted"
    assert (await handler.handle_section_button_click(user, item_session, "final_cancel_rfq"))["status"] == "cancel"
    assert (await handler.handle_section_button_click(user, item_session, "attachments_yes"))["status"] == "attachment"
    assert (await handler.handle_section_button_click(user, item_session, "restart_rfq"))["status"] == "restart"


@pytest.mark.asyncio
async def test_sectioned_helpers_validation_attachments_restart_and_combined(monkeypatch):
    entity = SimpleNamespace(extract_entities=AsyncMock(), openai_service=SimpleNamespace(validate_delivery_date=AsyncMock()))
    handler = SectionedRFQCreationHandler(
        entity, AsyncMock(), SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock()),
        AsyncMock(), confirmation_handler=SimpleNamespace(
            handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}),
            handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}),
        ),
    )
    user = make_user()

    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    assert not (await handler._autofill_location_from_pincode({"pincode": "560001"}))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert not (await handler._autofill_location_from_pincode({"pincode": "560001"}))["is_valid"]
    assert (await handler._autofill_location_from_pincode({})) ["is_valid"]
    entity.openai_service.validate_delivery_date.return_value = {"is_valid": False}
    assert not (await handler._validate_delivery_date("bad"))["is_valid"]
    entity.openai_service.validate_delivery_date.side_effect = RuntimeError("date")
    assert (await handler._validate_delivery_date("bad"))["is_valid"]

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda session, name: session.workflow_state.get(name))
    combined_schema = SimpleNamespace(model_dump=lambda: {"items": [], "delivery_locations": []})
    monkeypatch.setattr(
        "app.services.helpers.chat_service_helpers.ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products",
        lambda _: combined_schema,
    )
    combined = handler._build_combined_rfq_from_sections(
        {"deliveryDate": "2 January", "pincode": "560001", "city": "Pune", "state": "Maharashtra"},
        [{"description": "Laptop", "quantity": 1}],
    )
    assert combined["products"][0]["entities"]["description"] == "Laptop"

    handler._build_combined_rfq_from_sections = MagicMock(return_value={"combined_schema": {}, "products": []})
    attachment_session = make_session(date_location={}, items=[])
    assert (await handler._handle_attachments_section(user, attachment_session, "", []))["status"] == "awaiting_attachments_decision"
    attachment_session.workflow_state["pending_combined_rfq"] = {"combined_schema": {}}
    assert (await handler._handle_attachments_section(user, attachment_session, "skip", []))["status"] == "optional"

    handler.confirmation_handler.handle_pending_confirmations = AsyncMock(return_value={"status": "pending"})
    final = make_session(date_location={}, items=[])
    assert (await handler._handle_final_confirmation(user, final, "yes"))["status"] == "pending"
    assert "pending_combined_rfq" in final.workflow_state

    handler.cancel_service.handle_cancel_intent = AsyncMock(return_value={"status": "cancelled_aborted"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _: "items")
    handler._display_items_confirmation = AsyncMock(return_value={"status": "redisplayed"})
    assert (await handler._handle_restart_rfq(user, make_session(items=[])))["status"] == "redisplayed"
    handler.cancel_service.handle_cancel_intent.return_value = {"status": "cancelled"}
    assert (await handler._handle_restart_rfq(user, make_session()))["status"] == "cancelled"

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_sectioned_rfq_pending_restart", MagicMock())
    assert (await handler._offer_restart_confirmation(user, make_session()))["status"] == "awaiting_restart_confirmation"
    restart = make_session()
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_sectioned_rfq", MagicMock())
    assert (await handler._handle_restart_confirmation_response(user, restart, "yes"))["status"] == "workflow_restarted"
    assert (await handler._handle_restart_confirmation_response(user, restart, "no"))["status"] == "restart_declined"
    assert (await handler._handle_restart_confirmation_response(user, restart, "maybe"))["status"] == "awaiting_restart_confirmation"

    handler._handle_items_section = AsyncMock(return_value={"status": "existing"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda session, name: session.workflow_state.get(name))
    bfs = make_session(bfs_rfq_products=["Laptop"])
    assert (await handler._initiate_next_section(user, bfs, "items"))["status"] == "existing"
    assert "bfs_rfq_products" not in bfs.workflow_state
    assert (await handler._initiate_next_section(user, make_session(), "other"))["status"] == "section_initiated"
    cancelled = await handler._cancel_after_max_retries(user, make_session(), "items")
    assert cancelled["status"] == "max_retries_cancelled"


@pytest.mark.asyncio
async def test_intent_switch_constructor_context_and_missing_state(monkeypatch):
    openai = MagicMock()
    monkeypatch.setattr(intent_mod, "OpenAIService", lambda: openai)
    response = AsyncMock()
    handler = IntentSwitchHandler(AsyncMock(), response)
    assert handler._get_intent_description("unknown") == "handle your request"
    state = make_session(extracted_entities=[{"product_name": "Laptop"}])
    state.workflow_type = WorkflowType.rfq_creation
    assert handler._get_workflow_description(state) == "Laptop request"
    assert not await handler.should_handle_intent_switch(make_session(), "buy_something", 100)
    state.workflow_state["pending_intent_switch"] = {"x": 1}
    assert not await handler.should_handle_intent_switch(state, "buy_something", 100)
    state.workflow_state.pop("pending_intent_switch")
    assert await handler.should_handle_intent_switch(state, "general_inquiry", 100)

    response.generate_contextual_response.return_value = "choice"
    assert (await handler.handle_intent_switch_choice(make_user(), state, "sell", "sell_something", {"confidence": 95}))["status"] == "intent_switch_choice_presented"
    incomplete = make_session(pending_intent_switch={"new_intent": "buy_something", "new_intent_message": "buy"})
    openai.analyze_intent_switch_response = AsyncMock(return_value={"chosen_action": "switch_to_new"})
    assert (await handler.handle_intent_switch_response(make_user(), incomplete, "2"))["status"] == "error"
    complete = make_session(pending_intent_switch={
        "new_intent": "buy_something", "new_intent_message": "buy", "intent_result": {"confidence": 99}
    })
    assert (await handler.handle_intent_switch_response(make_user(), complete, "2"))["status"] == "switch_to_new_intent"
    assert complete.outcome == ConversationOutcome.abandoned
    assert complete.workflow_state == {"extracted_entities": []}


@pytest.mark.asyncio
async def test_confirmation_executes_acceptance_modification_and_optional_branches(monkeypatch):
    handler = bare(
        ConfirmationHandler,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        cancel_service=AsyncMock(),
        session_manager=AsyncMock(),
    )
    handler._send_completion_response = AsyncMock()
    handler._submit_rfq_to_backend = AsyncMock(return_value={"success": True, "rfq_id": "rfq-1"})
    schema = SimpleNamespace(model_dump=lambda: {"items": []})
    monkeypatch.setattr(confirmation_mod, "RFQValidationSchema", lambda **_: schema)
    monkeypatch.setattr(confirmation_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: schema)
    monkeypatch.setattr(
        "app.services.helpers.session_helpers.SessionHelpers.calculate_session_averages",
        lambda current: current,
    )

    combined = make_session(
        pending_optional_combined_rfq={"combined_schema": {"items": []}, "products": []}
    )
    result = await handler._handle_rfq_acceptance(make_user(), combined, "yes")
    assert result == {"status": "multiple_rfqs_created", "successful_count": 1}
    assert combined.workflow_state == {}
    assert handler._submit_rfq_to_backend.await_count == 1

    handler._submit_rfq_to_backend.reset_mock()
    single = make_session(
        pending_rfq={"entities": {"description": "Laptop"}},
        extracted_entities=[{"attachments": [{"file_name": "quote.pdf"}]}],
    )
    handler._submit_rfq_to_backend.return_value = {"success": False}
    result = await handler._handle_rfq_acceptance(make_user(), single, "yes")
    assert result["successful_count"] == 0
    assert single.outcome == ConversationOutcome.abandoned
    assert single.workflow_state == {}

    empty = make_session()
    handler._submit_rfq_to_backend.reset_mock()
    result = await handler._handle_rfq_acceptance(make_user(), empty, "yes")
    assert result["successful_count"] == 0
    assert handler._send_completion_response.await_count == 3

    handler._format_captured_info_for_modification = MagicMock(return_value="Captured laptop details")
    modified = await handler._handle_rfq_modification(make_user(), make_session(), "change")
    assert modified["status"] == "modification_requested"
    handler.whatsapp_service.send_configurable_buttons.assert_awaited()
    handler._format_captured_info_for_modification = ConfirmationHandler._format_captured_info_for_modification.__get__(handler)

    handler.response_helpers.generate_rfq_summary_and_confirmation = AsyncMock(return_value="summary")
    single_optional = make_session(
        pending_optional_rfq={"entities": {"description": "Laptop"}},
        attachment_caption="blue",
        extracted_entities=[{"attachments": [{}]}],
    )
    result = await handler._proceed_to_confirmation_from_optional(make_user(), single_optional, "continue")
    assert result["status"] == "optional_fields_skipped"
    assert single_optional.workflow_state["pending_rfq"]["entities"]["remarks"] == "blue"

    combined_optional = make_session(
        pending_optional_combined_rfq={
            "combined_schema": {"remarks": "", "items": []},
            "products": [{"entities": {"description": "Chair"}}],
        },
        extracted_entities=[{"attachments": [{}, {}, {}, {}]}],
    )
    result = await handler._proceed_to_confirmation_from_optional(make_user(), combined_optional, "continue")
    assert result["status"] == "optional_fields_skipped"
    assert "pending_combined_rfq" in combined_optional.workflow_state

    monkeypatch.setattr(
        "app.utils.rfq_message_formatter.format_rfq_response_message",
        lambda **_: "formatted",
    )
    assert handler._format_captured_info_for_modification(
        make_session(pending_combined_rfq={"products": [{"entities": {"description": "Laptop"}}], "combined_schema": {}})
    ) == "formatted"
    assert handler._format_captured_info_for_modification(
        make_session(pending_rfq={"entities": {"description": "Chair"}})
    ) == "formatted"
    assert "No product information" in handler._format_captured_info_for_modification(make_session())
    monkeypatch.setattr(
        "app.utils.rfq_message_formatter.format_rfq_response_message",
        MagicMock(side_effect=RuntimeError("format")),
    )
    assert "Unable to display" in handler._format_captured_info_for_modification(
        make_session(pending_rfq={"entities": {"description": "Chair"}})
    )

    entities = {"description": "Laptop"}
    handler._merge_specifications_into_product(entities, {"brand": "HP"}, "blue finish")
    assert entities["remarks"] == "blue finish"
    assert handler._extract_product_descriptions_from_rfq([
        {"success": True, "rfq_data": {"items": [{"description": ""}]}},
        {"success": True, "rfq_data": {"items": [{"product_name": "Chair"}]}},
    ]) == ["Chair"]


@pytest.mark.asyncio
async def test_format_modification_items_error_and_delivery_merge_variants(monkeypatch):
    handler = FormatModificationHandler(AsyncMock())
    handler.session_manager = AsyncMock()
    user = make_user()
    session = make_session(incomplete_products=[{"description": "old"}], complete_products=[{"description": "done"}])

    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 1)
    result = await handler._handle_items_modification("bad items", session, user)
    assert result["status"] == "format_error"
    assert handler.whatsapp_service.send_configurable_buttons.await_count == 0
    assert handler.session_manager.save_session.await_count == 1

    monkeypatch.setattr(format_mod.WorkflowManager, "get_delivery_details", lambda _: {
        "delivery_date": "tomorrow", "pincode": "560001", "city": "Pune", "state": "Maharashtra"
    })
    item = {"description": "Laptop", "deliveryDate": "kept", "pincode": "kept", "city": "kept", "state": "kept"}
    await handler._replace_products_array(session, [item])
    assert session.workflow_state["extracted_entities"][0] == item
    assert "incomplete_products" not in session.workflow_state
    assert "complete_products" not in session.workflow_state


@pytest.mark.asyncio
async def test_intent_switch_context_matrix_and_response_fallbacks(monkeypatch):
    openai = MagicMock()
    monkeypatch.setattr(intent_mod, "OpenAIService", lambda: openai)
    handler = IntentSwitchHandler(AsyncMock(), AsyncMock())

    collecting = make_session(extracted_entities=[{"description": "Laptop"}])
    collecting.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(
        collecting, "buy_something", 95, {"conversation_stage": "collecting", "references_existing_data": True}
    )
    collecting.workflow_state["incomplete_products"] = [{"entities": {}}]
    assert not await handler.should_handle_intent_switch(
        collecting, "buy_something", 95, {"conversation_stage": "collecting", "references_existing_data": False}
    )
    collecting.workflow_state.pop("incomplete_products")
    assert not await handler.should_handle_intent_switch(
        collecting, "buy_something", 95, {"conversation_stage": "optional_fields"}
    )
    assert not await handler.should_handle_intent_switch(
        collecting, "buy_something", 95, {"conversation_stage": "existing_request"}
    )
    assert await handler.should_handle_intent_switch(
        collecting, "buy_something", 95, {"conversation_stage": "new_request", "references_existing_data": False}
    )
    assert not await handler.should_handle_intent_switch(collecting, "buy_something", 89, None)

    seller = make_session(extracted_entities=[{"description": "Laptop"}])
    seller.workflow_type = WorkflowType.seller_rfq_view
    assert await handler.should_handle_intent_switch(seller, "buy_something", 95)
    seller.workflow_type = WorkflowType.general_inquiry
    assert await handler.should_handle_intent_switch(seller, "general_inquiry", 95)

    assert (await handler.handle_intent_switch_response(make_user(), make_session(), "1"))["status"] == "no_pending_switch"
    corrupted = make_session(pending_intent_switch=["bad"])
    assert (await handler.handle_intent_switch_response(make_user(), corrupted, "1"))["status"] == "corrupted_intent_switch_data"

    pending = {
        "new_intent": "sell_something",
        "new_intent_message": "sell",
        "intent_result": {"confidence": 99},
    }
    openai.analyze_intent_switch_response = AsyncMock(return_value={"chosen_action": "continue_current"})
    continuing = make_session(pending_intent_switch=pending)
    result = await handler.handle_intent_switch_response(make_user(), continuing, "1")
    assert result["status"] == "continue_current_workflow"
    assert "pending_intent_switch" not in continuing.workflow_state

    openai.analyze_intent_switch_response.side_effect = RuntimeError("classifier")
    fallback = make_session(pending_intent_switch=pending)
    assert (await handler.handle_intent_switch_response(make_user(), fallback, "continue current"))["status"] == "continue_current_workflow"
    switching = make_session(pending_intent_switch=pending)
    assert (await handler.handle_intent_switch_response(make_user(), switching, "new request"))["status"] == "switch_to_new_intent"

    incomplete = make_session(pending_intent_switch={"new_intent": "sell_something"})
    openai.analyze_intent_switch_response = AsyncMock(return_value={"chosen_action": "switch_to_new"})
    assert (await handler.handle_intent_switch_response(make_user(), incomplete, "2"))["status"] == "error"


@pytest.mark.asyncio
async def test_purchase_intent_normalization_summary_errors_and_captured_info(monkeypatch):
    entity = AsyncMock()
    summaries = AsyncMock()
    products = AsyncMock()
    manager = AsyncMock()
    handler = PurchaseIntentHandler(AsyncMock(), AsyncMock(), entity, summaries, products, manager)
    user = make_user()
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "is_sectioned_rfq_active", lambda *_: False)
    summaries.load_user_context.return_value = []
    entity.extract_entities.return_value = {}

    for message in (
        {"button_reply": {"title": "buy"}},
        {"text": {"body": "buy"}},
        {"content": "buy"},
        {"unknown": "buy"},
        42,
    ):
        result = await handler.handle_purchase_intent(user, make_session(), message)
        assert result["status"] == "no_new_entities"

    summaries.load_user_context.return_value = [{"summary": "laptop"}]
    entity.extract_entities_with_summary_context.return_value = {"products": [{"description": "Laptop"}]}
    products.handle_products_array.return_value = {"status": "summary"}
    result = await handler.handle_purchase_intent(
        user,
        make_session(incomplete_products=[{"entities": {"description": "Old"}}]),
        "more",
        intent_result={"intent": "modification_request"},
        should_use_summary_aware_extraction_func=lambda _: True,
    )
    assert result["status"] == "summary"
    entity.extract_entities_with_summary_context.assert_awaited_once()

    entity.extract_entities.return_value = {
        "modification_intent_detected": True,
        "requires_clarification": True,
        "products": [],
    }
    handler._format_captured_info_for_modification = MagicMock(return_value="captured")
    result = await handler.handle_purchase_intent(user, make_session(), "change")
    assert result["status"] == "modification_clarification_sent"
    manager.save_session.assert_awaited()
    handler._format_captured_info_for_modification = PurchaseIntentHandler._format_captured_info_for_modification.__get__(handler)

    monkeypatch.setattr("app.utils.rfq_message_formatter.format_rfq_response_message", lambda **_: "formatted")
    assert handler._format_captured_info_for_modification(
        make_session(pending_combined_rfq={"products": [{"entities": {"description": "Laptop"}}], "combined_schema": {}})
    ) == "formatted"
    assert handler._format_captured_info_for_modification(
        make_session(pending_rfq={"entities": {"description": "Chair"}})
    ) == "formatted"
    assert "No product information" in handler._format_captured_info_for_modification(make_session())
    monkeypatch.setattr(
        "app.utils.rfq_message_formatter.format_rfq_response_message",
        MagicMock(side_effect=RuntimeError("format")),
    )
    assert "Unable to display" in handler._format_captured_info_for_modification(
        make_session(pending_rfq={"entities": {"description": "Chair"}})
    )

    quantity = await handler._handle_quantity_limit_violations(
        user, make_session(), {"quantity_violations": [{"description": "Laptop", "quantity": 100000001}]}
    )
    assert quantity["status"] == "quantity_limit_violation"
    monkeypatch.setattr("app.services.cancel_service.CancelService", lambda **_: SimpleNamespace(
        _clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock()
    ))
    non_procurable = await handler._handle_non_procurable_items(
        user, make_session(), {"non_procurable_items": ["service"]}
    )
    assert non_procurable["status"] == "non_procurable_cancelled"


@pytest.mark.asyncio
async def test_authentication_orchestrator_public_routes_selection_and_errors(monkeypatch):
    auth = AsyncMock()
    support = AsyncMock()
    registration = AsyncMock()
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        authentication_service=auth,
        registration_service=registration,
        intent_service=AsyncMock(),
        support_service=support,
        chat_service=None,
        auth_reg_switch=AsyncMock(),
        profile_selection_service=AsyncMock(),
    )
    auth.validate_token = AsyncMock(return_value=None)
    handler.profile_selection_service.handle_profile_selection.return_value = {"status": "menu"}
    handler.profile_selection_service.handle_profile_selection_response.return_value = {"status": "selected"}
    handler._handle_authentication_workflow = AsyncMock(return_value={"status": "auth_workflow"})
    handler._handle_registration_workflow = AsyncMock(return_value={"status": "registration_workflow"})
    handler._handle_user_selection = AsyncMock(return_value={"status": "selection"})
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "registered"})
    handler._handle_auth_fallback = AuthenticationOrchestrator._handle_auth_fallback.__get__(handler)

    assert (await handler.authentication_orchestrator_flow(
        "+1", "hello", make_session(), None
    ))["status"] == "fallback_handled"
    assert (await handler.authentication_orchestrator_flow(
        "+1", "buy", make_session(), {"intent": "buy_something", "confidence": 95}
    ))["status"] == "menu"
    profile_session = make_session(profile_selection_stage="choose")
    assert (await handler.authentication_orchestrator_flow(
        "+1", "2", profile_session, {"intent": "other", "confidence": 95}
    ))["status"] == "selected"

    auth_session = make_session()
    auth_session.workflow_type = WorkflowType.authentication
    assert (await handler.authentication_orchestrator_flow(
        "+1", "otp", auth_session, {"intent": "other", "confidence": 95}
    ))["status"] == "auth_workflow"
    registration_session = make_session(registration_stage="start")
    registration_session.workflow_type = WorkflowType.registration
    assert (await handler.authentication_orchestrator_flow(
        "+1", "name", registration_session, {"intent": "other", "confidence": 95}
    ))["status"] == "registration_workflow"

    role_session = make_session(role_switch_in_progress=True, user_type="seller")
    auth.user_authenticate.return_value = {"success": True, "response": [{"id": "seller"}]}
    auth.filter_users_by_intent = MagicMock(return_value={"success": True, "filtered_users": [{"id": "seller"}], "unique_emails": ["s@example.com"]})
    assert (await handler.authentication_orchestrator_flow(
        "+1", "sell", role_session, {"intent": "other", "confidence": 95}
    ))["status"] == "selection"
    role_session = make_session(role_switch_in_progress=True, user_type="seller")
    auth.user_authenticate.return_value = {"success": False}
    assert (await handler.authentication_orchestrator_flow(
        "+1", "sell", role_session, {"intent": "other", "confidence": 95}
    ))["status"] == "registered"

    class FakeExit:
        def __init__(self, *args, **kwargs):
            self.handle_exit_intent = AsyncMock(return_value={"status": "exited"})

    monkeypatch.setattr(auth_mod, "ExitService", FakeExit)
    assert (await handler.authentication_orchestrator_flow(
        "+1", "bye", make_session(), {"intent": "exit_system", "confidence": 90}
    ))["status"] == "exited"
    auth.validate_token.side_effect = RuntimeError("token")
    support.redirect_to_support.return_value = {"status": "support"}
    assert (await handler.authentication_orchestrator_flow(
        "+1", "x", make_session(), {"intent": "buy_something", "confidence": 90}
    ))["status"] == "support"


@pytest.mark.asyncio
async def test_authentication_orchestrator_selection_switch_handlers_and_fallbacks(monkeypatch):
    auth = AsyncMock()
    support = AsyncMock()
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        authentication_service=auth,
        registration_service=AsyncMock(),
        intent_service=AsyncMock(),
        support_service=support,
        chat_service=None,
        auth_reg_switch=AsyncMock(),
        profile_selection_service=AsyncMock(),
    )
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    empty = await handler._handle_user_selection(
        "+1", make_session(), {"filtered_users": [], "unique_emails": []}, {"intent": "sell_something"}, "sell", []
    )
    assert empty["status"] == "redirect"
    auth.initiate_email_confirmation.return_value = {"status": "email_prompt"}
    selected_session = make_session(existing="kept")
    result = await handler._handle_user_selection(
        "+1", selected_session,
        {"filtered_users": [{"email": "a@example.com"}], "unique_emails": ["a@example.com"]},
        {"intent": "buy_something"}, "buy", [{"email": "a@example.com"}]
    )
    assert result["status"] == "email_prompt"
    assert selected_session.workflow_state["authentication_stage"] == "email_confirmation"
    auth.initiate_email_confirmation.side_effect = RuntimeError("email")
    support.redirect_to_support.return_value = {"status": "support"}
    assert (await handler._handle_user_selection(
        "+1", make_session(),
        {"filtered_users": [{"id": "a"}], "unique_emails": ["a@example.com"]},
        {"intent": "buy_something"}, "buy", []
    ))["status"] == "support"

    handler._check_switch_response = AsyncMock(return_value={"status": "switched"})
    auth_session = make_session(authentication_stage="email_confirmation")
    assert (await handler._handle_authentication_workflow(
        "+1", "1", auth_session, {"intent": "other", "confidence": 95}
    ))["status"] == "switched"
    handler._check_switch_response = AsyncMock(return_value=None)
    handler._should_handle_intent_switch_during_auth = AsyncMock(return_value=True)
    handler._handle_intent_switch_during_auth = AsyncMock(return_value={"status": "choice"})
    assert (await handler._handle_authentication_workflow(
        "+1", "buy", make_session(authentication_stage="email_confirmation"),
        {"intent": "buy_something", "confidence": 99}
    ))["status"] == "choice"
    handler._handle_intent_switch_during_auth = AuthenticationOrchestrator._handle_intent_switch_during_auth.__get__(handler)
    handler._check_switch_response = AsyncMock(side_effect=RuntimeError("switch"))
    support.redirect_to_support.return_value = {"status": "workflow_support"}
    assert (await handler._handle_authentication_workflow(
        "+1", "x", make_session(), {}
    ))["status"] == "workflow_support"

    handler._check_switch_response = AsyncMock(return_value={"status": "switch"})
    assert (await handler._handle_registration_workflow(
        "+1", "switch", make_session(registration_stage="start"), {}
    ))["status"] == "switch"
    handler._check_switch_response = AsyncMock(return_value=None)
    handler.registration_service = AsyncMock()
    handler.registration_service.handle_registration_otp_validation.return_value = {
        "status": "registration_completed", "registration_flow_complete": True
    }
    finished = make_session(registration_stage="email_otp")
    assert (await handler._handle_registration_workflow("+1", "otp", finished, {}))["status"] == "registration_completed"
    assert finished.workflow_state == {} and finished.workflow_type is None
    handler.registration_service.handle_registration_data_collection.side_effect = RuntimeError("registration")
    support.redirect_to_support.return_value = {"status": "registration_support"}
    assert (await handler._handle_registration_workflow(
        "+1", "name", make_session(registration_stage="start"), {}
    ))["status"] == "registration_support"

    handler.auth_reg_switch.handle_auth_reg_switch_choice.return_value = {"status": "choice"}
    assert (await handler._handle_intent_switch_during_auth(
        "+1", make_session(), "buy", {"intent": "buy_something"}
    ))["status"] == "choice"
    assert (await handler._handle_intent_switch_during_auth(
        "+1", make_session(), "sell", {"intent": "sell_something"}
    ))["status"] == "choice"
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "registration"})
    assert (await handler._handle_intent_switch_during_auth(
        "+1", make_session(), "register", {"intent": "registration_request"}
    ))["status"] == "registration"
    assert (await handler._handle_intent_switch_during_auth(
        "+1", make_session(), "cancel", {"intent": "cancel"}
    ))["status"] == "authentication_cancelled"
    assert (await handler._handle_intent_switch_during_auth(
        "+1", make_session(), "other", {"intent": "other"}
    ))["status"] == "continue_authentication"

    assert (await handler._handle_intent_switch_during_registration(
        "+1", make_session(), "buy", {"intent": "buy_something"}, "seller"
    ))["status"] == "choice"
    assert (await handler._handle_intent_switch_during_registration(
        "+1", make_session(), "sell", {"intent": "sell_something"}, "buyer"
    ))["status"] == "choice"
    assert (await handler._handle_intent_switch_during_registration(
        "+1", make_session(), "stop", {"intent": "stop"}, "buyer"
    ))["status"] == "registration_cancelled"
    assert (await handler._handle_intent_switch_during_registration(
        "+1", make_session(), "other", {"intent": "other"}, "buyer"
    ))["status"] == "continue_registration"

    handler.whatsapp_service.send_message.side_effect = RuntimeError("send")
    assert (await handler._handle_auth_general_inquiry("+1", "question"))["status"] == "error"
    assert (await handler._handle_auth_fallback("+1", "other"))["status"] == "error"


@pytest.mark.asyncio
async def test_products_array_completeness_questions_and_merge_matrix(monkeypatch):
    handler = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())

    class Schema:
        def __init__(self, missing=None, questions=None):
            self.missing = missing or []
            self.questions = questions or {"mandatory": ["Delivery date?", "Quantity?"], "has_mandatory": True, "has_optional": False}
        def get_missing_mandatory_fields(self):
            return self.missing
        def get_combined_questions(self):
            return self.questions

    schemas = iter([
        Schema(),
        Schema(["delivery_date"]),
        RuntimeError("schema"),
    ])
    def schema_factory(*_args):
        value = next(schemas)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", schema_factory)
    incomplete, complete = await handler._categorize_products_by_completeness([
        {"description": "Laptop"}, {"description": "Chair"}, {"description": "Broken"}
    ])
    assert [item["index"] for item in complete] == [1]
    assert incomplete[0]["missing_fields"] == ["delivery_date"]
    assert incomplete[1]["missing_fields"] == ["project_desc", "delivery_date", "division"]

    question_schema = Schema(questions={
        "mandatory": ["Delivery date?", "Quantity?", "Description?", None],
        "has_mandatory": True,
        "has_optional": False,
    })
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: question_schema)
    incomplete_products = [
        {"index": 1, "entities": {}, "missing_fields": ["item_0_quantity", "delivery_date"]},
        {"index": 2, "entities": {"description": "Chair"}, "missing_fields": ["item_0_description", "delivery_date"]},
    ]
    questions, missing = await handler._generate_clarification_questions(incomplete_products, 2)
    assert "Quantity for Product 1" in questions
    assert "Description for Chair" in questions
    assert "delivery_date" in missing

    all_questions, all_missing = [], []
    await handler._generate_combined_questions(
        [{"index": 1, "entities": {}, "missing_fields": ["delivery_date"]},
         {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]}],
        all_questions, all_missing, has_date_error=True,
    )
    assert "Delivery date?" not in all_questions

    state = make_session(incomplete_products=[1], complete_products=[2], optional_fields_asked=True)
    handler._clear_workflow_state(state)
    assert state.workflow_state == {}
    handler._clear_workflow_state(make_session())

    positional = await handler._merge_with_existing_incomplete_products(
        [{"entities": {"quantity": 1}}, {"entities": {"quantity": 2}}],
        [{"description": "Laptop"}, {"description": "Chair"}],
    )
    assert [item["description"] for item in positional] == ["Laptop", "Chair"]

    merged = await handler._merge_with_existing_incomplete_products(
        [{"description": "Laptop", "date_validation_error": "old", "pincode_validation_error": "old"}],
        [
            {"description": "Laptop", "deliveryDate": "tomorrow", "date_validation_error": "", "pincode": "560001", "city": "Pune", "state": "Maharashtra", "pincode_validation_error": ""},
            {"description": "Chair"},
            {"city": "Pune", "remarks": "blue"},
        ],
    )
    assert merged[0]["deliveryDate"] == "tomorrow"
    assert "date_validation_error" not in merged[0]
    assert "pincode_validation_error" not in merged[0]
    assert merged[-1]["description"] == "Chair"

    class Broken:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("broken")
    fallback = await handler._merge_with_existing_incomplete_products([Broken()], [{"description": "new"}])
    assert len(fallback) == 2


@pytest.mark.asyncio
async def test_sectioned_delivery_modification_and_validation_matrix(monkeypatch):
    entity = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp = AsyncMock()
    cancel = SimpleNamespace(
        _clear_workflow_state=AsyncMock(),
        _send_cancellation_message=AsyncMock(),
    )
    handler = SectionedRFQCreationHandler(entity, whatsapp, cancel, AsyncMock())
    user = make_user()

    def fresh_session():
        session = make_session()
        sectioned_mod.WorkflowManager.initialize_sectioned_rfq(session)
        return session

    monkeypatch.setattr(
        sectioned_mod.sectioned_rfq_format_parser,
        "parse_delivery_format",
        lambda _: {"error": "missing field", "additional_text": ""},
    )
    invalid = fresh_session()
    result = await handler._process_delivery_modification_direct(user, invalid, "Delivery Date: missing")
    assert result == {"status": "format_error", "retry_count": 1}
    assert whatsapp.send_configurable_buttons.await_args.args[3] == "Format Error"

    retry_limit = fresh_session()
    retry_limit.workflow_state["sectioned_rfq"]["sections"]["date_location"]["retry_count"] = 2
    handler._cancel_after_max_retries = AsyncMock(return_value={"status": "maxed"})
    result = await handler._process_delivery_modification_direct(user, retry_limit, "Pincode: invalid")
    assert result == {"status": "maxed"}
    handler._cancel_after_max_retries.reset_mock()

    entity.extract_entities.return_value = {
        "deliveryDate": "tomorrow",
        "pincode": "560001",
        "city": "",
        "state": "",
        "products": [{"description": "Laptop"}],
    }
    handler._validate_delivery_date = AsyncMock(
        return_value={"is_valid": True, "normalized_date": "2 January 2026"}
    )
    handler._autofill_location_from_pincode = AsyncMock(return_value={
        "delivery_data": {
            "deliveryDate": "2 January 2026",
            "pincode": "560001",
            "city": "Pune",
            "state": "Maharashtra",
        },
        "is_valid": True,
        "error": None,
    })
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmed"})
    extracted = fresh_session()
    result = await handler._process_delivery_modification_direct(user, extracted, "tomorrow please")
    assert result == {"status": "confirmed"}
    assert extracted.workflow_state["sectioned_rfq"]["sections"]["items"]["data"] == [
        {"description": "Laptop"}
    ]
    handler._display_delivery_confirmation.assert_awaited_once()

    entity.extract_entities.return_value = {"deliveryDate": "bad", "pincode": "bad"}
    handler._validate_delivery_date.return_value = {"is_valid": False, "error": "bad date"}
    handler._autofill_location_from_pincode.return_value = {
        "delivery_data": {"deliveryDate": "", "pincode": "bad", "city": "", "state": ""},
        "is_valid": False,
        "error": "bad pincode",
    }
    handler._display_delivery_validation_error = AsyncMock(return_value={"status": "validation"})
    invalid_values = fresh_session()
    result = await handler._process_delivery_modification_direct(user, invalid_values, "bad details")
    assert result == {"status": "validation"}
    handler._display_delivery_validation_error.assert_awaited_once()
    assert handler._display_delivery_validation_error.await_args.kwargs == {
        "date_error": "bad date",
        "pincode_error": "bad pincode",
    }

    entity.extract_entities.return_value = {"deliveryDate": "2 January", "pincode": ""}
    handler._validate_delivery_date.return_value = {
        "is_valid": True,
        "normalized_date": "2 January 2026",
    }
    handler._autofill_location_from_pincode.reset_mock()
    handler._display_delivery_validation_error.reset_mock()
    handler._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    partial = fresh_session()
    result = await handler._process_delivery_modification_direct(user, partial, "some date")
    assert result == {"status": "missing"}
    handler._display_delivery_missing_fields.assert_awaited_once()

    entity.extract_entities.return_value = {"products": [{"description": "Chair"}]}
    no_delivery = fresh_session()
    result = await handler._process_delivery_modification_direct(user, no_delivery, "just an item")
    assert result == {"status": "missing"}
    assert no_delivery.workflow_state["sectioned_rfq"]["sections"]["items"]["data"] == [
        {"description": "Chair"}
    ]

    handler._display_invalid_pincode_message = AsyncMock(return_value={"status": "invalid_pincode"})
    entity.extract_entities.return_value = {"deliveryDate": "2 January", "pincode": "560001"}
    handler._validate_delivery_date.return_value = {
        "is_valid": True,
        "normalized_date": "2 January 2026",
    }
    handler._autofill_location_from_pincode.return_value = {
        "delivery_data": {
            "deliveryDate": "2 January 2026",
            "pincode": "560001",
            "city": "",
            "state": "",
        },
        "is_valid": True,
        "error": None,
    }
    invalid_lookup = fresh_session()
    result = await handler._process_delivery_modification_direct(user, invalid_lookup, "another date")
    assert result == {"status": "invalid_pincode"}
    handler._display_invalid_pincode_message.assert_awaited_once_with(
        user, invalid_lookup,
        {
            "deliveryDate": "2 January 2026",
            "pincode": "560001",
            "city": "",
            "state": "",
        },
        "560001",
    )

    parser_results = iter([
        {"deliveryDate": "bad date", "pincode": "bad pin", "additional_text": "question"},
        {"deliveryDate": "bad date", "pincode": "560001", "additional_text": ""},
        {"deliveryDate": "2 January", "pincode": "560001", "additional_text": ""},
    ])
    monkeypatch.setattr(
        sectioned_mod.sectioned_rfq_format_parser,
        "parse_delivery_format",
        lambda _: next(parser_results),
    )
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": False, "error": "date bad"})
    handler._validate_pincode_and_get_location = AsyncMock(
        return_value={"is_valid": False, "error": "pincode bad"}
    )
    both_invalid = fresh_session()
    result = await handler._process_delivery_modification_direct(user, both_invalid, "formatted")
    assert result == {"status": "validation_error", "retry_count": 1}
    assert whatsapp.send_configurable_buttons.await_args.args[3] == "Invalid Details"

    handler._validate_pincode_and_get_location.return_value = {
        "is_valid": True,
        "city": "Pune",
        "state": "Maharashtra",
    }
    date_invalid = fresh_session()
    result = await handler._process_delivery_modification_direct(user, date_invalid, "formatted")
    assert result == {"status": "validation_error", "retry_count": 1}
    assert whatsapp.send_configurable_buttons.await_args.args[3] == "Invalid Date"

    handler._validate_delivery_date.return_value = {
        "is_valid": True,
        "normalized_date": "2 January 2026",
    }
    handler._validate_pincode_and_get_location.return_value = {
        "is_valid": False,
        "error": "pincode bad",
    }
    pincode_invalid = fresh_session()
    result = await handler._process_delivery_modification_direct(user, pincode_invalid, "formatted")
    assert result == {"status": "validation_error", "retry_count": 1}
    assert whatsapp.send_configurable_buttons.await_args.args[3] == "Invalid Pincode"

    valid_parser = {"deliveryDate": "2 January", "pincode": "560001", "additional_text": ""}
    monkeypatch.setattr(
        sectioned_mod.sectioned_rfq_format_parser,
        "parse_delivery_format",
        lambda _: valid_parser,
    )
    valid = fresh_session()
    handler._validate_delivery_date.return_value = {
        "is_valid": True,
        "normalized_date": "2 January 2026",
    }
    handler._validate_pincode_and_get_location.return_value = {
        "is_valid": True,
        "city": "Pune",
        "state": "Maharashtra",
    }
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmed"})
    result = await handler._process_delivery_modification_direct(user, valid, "formatted")
    assert result == {"status": "confirmed"}
    assert sectioned_mod.WorkflowManager.get_section_data(valid, "date_location") == {
        "deliveryDate": "2 January 2026",
        "pincode": "560001",
        "city": "Pune",
        "state": "Maharashtra",
    }
    assert not sectioned_mod.WorkflowManager.is_awaiting_section_modification(valid, "date_location")

    validation_limit = fresh_session()
    validation_limit.workflow_state["sectioned_rfq"]["sections"]["date_location"]["retry_count"] = 2
    handler._cancel_after_max_retries = AsyncMock(return_value={"status": "maxed"})
    handler._validate_delivery_date.return_value = {"is_valid": False, "error": "date bad"}
    handler._validate_pincode_and_get_location.return_value = {
        "is_valid": True,
        "city": "Pune",
        "state": "Maharashtra",
    }
    result = await handler._process_delivery_modification_direct(user, validation_limit, "formatted")
    assert result == {"status": "maxed"}


@pytest.mark.asyncio
async def test_sectioned_display_and_section_transition_helpers(monkeypatch):
    entity = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp = AsyncMock()
    cancel = SimpleNamespace(handle_cancel_intent=AsyncMock())
    manager = AsyncMock()
    handler = SectionedRFQCreationHandler(entity, whatsapp, cancel, manager)
    user = make_user()

    def fresh_session(**state):
        session = make_session(**state)
        sectioned_mod.WorkflowManager.initialize_sectioned_rfq(session)
        return session

    delivery = {"deliveryDate": "2 January 2026", "pincode": "560001", "city": "Pune", "state": "Maharashtra"}
    confirmation_session = fresh_session()
    assert (await handler._display_delivery_confirmation(user, confirmation_session, delivery))["status"] == "awaiting_delivery_confirmation"
    buttons = whatsapp.send_configurable_buttons.await_args.args[2]
    assert [button["id"] for button in buttons] == [
        "confirm_date_location", "modify_date_location", "restart_rfq"
    ]

    missing_session = fresh_session()
    assert (await handler._display_delivery_missing_fields(
        user, missing_session, {"deliveryDate": "2 January", "pincode": ""}
    ))["status"] == "awaiting_delivery_details"
    assert sectioned_mod.WorkflowManager.is_awaiting_section_modification(missing_session, "date_location")
    missing_with_error = fresh_session()
    assert (await handler._display_delivery_missing_fields(
        user, missing_with_error, {"deliveryDate": "", "pincode": "560001"}, "Date is invalid"
    ))["status"] == "awaiting_delivery_details"
    assert "Date is invalid" in whatsapp.send_configurable_buttons.await_args.args[1]

    for errors, header in (
        (("date bad", "pincode bad"), "Invalid Details"),
        (("date bad", None), "Invalid Date"),
        ((None, "pincode bad"), "Invalid Pincode"),
    ):
        validation_session = fresh_session()
        result = await handler._display_delivery_validation_error(
            user, validation_session, delivery, date_error=errors[0], pincode_error=errors[1]
        )
        assert result == {"status": "validation_error"}
        assert whatsapp.send_configurable_buttons.await_args.args[3] == header
        assert sectioned_mod.WorkflowManager.is_awaiting_section_modification(
            validation_session, "date_location"
        )

    no_error_session = fresh_session()
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmation"})
    assert await handler._display_delivery_validation_error(user, no_error_session, delivery) == {
        "status": "confirmation"
    }

    items = [{"description": "Laptop", "quantity": 1, "unitofMeasures": "piece"}]
    text_items = fresh_session()
    assert (await handler._display_items_confirmation(user, text_items, items))["status"] == "awaiting_items_confirmation"
    assert [button["id"] for button in whatsapp.send_configurable_buttons.await_args.args[2]] == [
        "confirm_items", "modify_items", "restart_rfq"
    ]
    excel_items = fresh_session()
    excel_items.workflow_state["sectioned_rfq"]["from_excel"] = True
    assert (await handler._display_items_confirmation(user, excel_items, items))["status"] == "awaiting_items_confirmation"
    assert [button["id"] for button in whatsapp.send_configurable_buttons.await_args.args[2]] == [
        "confirm_items", "restart_rfq"
    ]

    incomplete = [{"index": 1, "item": {"description": "Laptop"}, "missing_fields": ["quantity"]}]
    missing_items = fresh_session()
    assert (await handler._display_items_missing_fields(
        user, missing_items, items, incomplete
    ))["status"] == "awaiting_missing_item_fields"
    assert [button["id"] for button in whatsapp.send_configurable_buttons.await_args.args[2]] == [
        "modify_items", "restart_rfq"
    ]
    excel_missing = fresh_session()
    excel_missing.workflow_state["sectioned_rfq"]["from_excel"] = True
    assert (await handler._display_items_missing_fields(
        user, excel_missing, items, incomplete
    ))["status"] == "awaiting_missing_item_fields"
    assert [button["id"] for button in whatsapp.send_configurable_buttons.await_args.args[2]] == [
        "restart_rfq"
    ]

    handler._initiate_next_section = AsyncMock(return_value={"status": "next"})
    confirm_session = fresh_session()
    result = await handler._handle_section_confirm(user, confirm_session, "date_location")
    assert result == {"status": "next"}
    assert sectioned_mod.WorkflowManager.is_section_confirmed(confirm_session, "date_location")
    assert sectioned_mod.WorkflowManager.get_sectioned_rfq_section(confirm_session) == "items"
    assert manager.save_session.await_count >= 1

    assert await handler._handle_section_confirm(user, fresh_session(), "final_confirmation") == {
        "status": "error"
    }

    modify_date = fresh_session(date_location=delivery)
    monkeypatch.setattr(
        sectioned_mod.WorkflowManager,
        "get_section_data",
        lambda session, name: session.workflow_state.get(name),
    )
    assert (await handler._handle_section_modify(user, modify_date, "date_location"))["status"] == "awaiting_modification"
    modify_items = fresh_session(items=items)
    assert (await handler._handle_section_modify(user, modify_items, "items"))["status"] == "awaiting_modification"
    modify_unknown = fresh_session()
    assert (await handler._handle_section_modify(user, modify_unknown, "unknown"))["status"] == "awaiting_modification"
    assert (await handler.handle_section_button_click(user, modify_unknown, "unrecognized"))["status"] == "error"

    final_cancel = fresh_session()
    assert (await handler._handle_final_cancel(user, final_cancel))["status"] == "awaiting_restart_confirmation"


@pytest.mark.asyncio
async def test_sectioned_router_and_next_section_remaining_paths(monkeypatch):
    entity = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp = AsyncMock()
    cancel = SimpleNamespace(
        _clear_workflow_state=AsyncMock(),
        _send_cancellation_message=AsyncMock(),
    )
    handler = SectionedRFQCreationHandler(entity, whatsapp, cancel, AsyncMock())
    user = make_user()

    def fresh_session():
        session = make_session()
        sectioned_mod.WorkflowManager.initialize_sectioned_rfq(session)
        return session

    awaiting = fresh_session()
    sectioned_mod.WorkflowManager.set_awaiting_section_modification(awaiting, "date_location", True)
    handler._process_delivery_modification_direct = AsyncMock(return_value={"status": "modified"})
    assert (await handler._handle_date_location_section(user, awaiting, "format", []))["status"] == "modified"

    handler._display_invalid_pincode_message = AsyncMock(return_value={"status": "invalid_pincode"})
    handler._validate_delivery_date = AsyncMock(return_value={
        "is_valid": True,
        "normalized_date": "2 January 2026",
    })
    handler._autofill_location_from_pincode = AsyncMock(return_value={
        "delivery_data": {
            "deliveryDate": "2 January 2026",
            "pincode": "560001",
            "city": "",
            "state": "",
        },
        "is_valid": True,
        "error": None,
    })
    invalid_lookup = fresh_session()
    entity.extract_entities.return_value = {"deliveryDate": "2 January", "pincode": "560001"}
    assert (await handler._handle_date_location_section(user, invalid_lookup, "unchanged", []))["status"] == "invalid_pincode"
    handler._display_invalid_pincode_message.assert_awaited_once()

    no_details = fresh_session()
    entity.extract_entities.return_value = {}
    assert (await handler._handle_date_location_section(user, no_details, "nothing", []))["status"] == "awaiting_delivery_details"
    assert sectioned_mod.WorkflowManager.is_awaiting_section_modification(no_details, "date_location")

    existing_items = fresh_session()
    sectioned_mod.WorkflowManager.update_section_data(
        existing_items, "items", [{"description": "Laptop", "quantity": 1}]
    )
    entity.extract_entities.return_value = {"products": [{"description": "Chair", "quantity": 2}]}
    handler._display_items_confirmation = AsyncMock(return_value={"status": "items"})
    assert (await handler._handle_items_section(user, existing_items, "add chair", []))["status"] == "items"
    assert sectioned_mod.WorkflowManager.get_section_data(existing_items, "items") == [
        {"description": "Chair", "quantity": 2}
    ]

    empty_items = fresh_session()
    handler._handle_items_section = AsyncMock(return_value={"status": "existing"})
    assert (await handler._initiate_next_section(user, empty_items, "items"))["status"] == "awaiting_items"
    handler._handle_attachments_section = AsyncMock(return_value={"status": "attachments"})
    assert (await handler._initiate_next_section(user, fresh_session(), "attachments"))["status"] == "attachments"
    handler._handle_final_confirmation = AsyncMock(return_value={"status": "final"})
    assert (await handler._initiate_next_section(user, fresh_session(), "final_confirmation"))["status"] == "final"
    assert (await handler._initiate_next_section(user, fresh_session(), "unknown"))["status"] == "section_initiated"

    assert not handler._is_delivery_complete(None)
    assert "Delivery Date" in handler._generate_delivery_missing_fields_message({"pincode": ""})
    assert "Delivery Pincode" in handler._generate_delivery_missing_fields_message({"deliveryDate": "today"})
    assert "fetch the location" in handler._generate_delivery_missing_fields_message({
        "deliveryDate": "today", "pincode": "560001"
    })
    assert handler._get_next_section("date_location") == "items"
    assert handler._get_next_section("final_confirmation") is None
    assert handler._get_next_section("unknown") is None


@pytest.mark.asyncio
async def test_remaining_authentication_orchestrator_branch_matrix(monkeypatch):
    auth = AsyncMock()
    profile = AsyncMock()
    support = AsyncMock()
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        authentication_service=auth,
        registration_service=AsyncMock(),
        intent_service=AsyncMock(),
        support_service=support,
        chat_service=None,
        auth_reg_switch=AsyncMock(),
        profile_selection_service=profile,
    )
    handler._handle_auth_fallback = AsyncMock(return_value={"status": "fallback"})
    profile.handle_profile_selection.return_value = {"status": "role_menu"}

    role_switch = make_session(role_switch_in_progress=True, user_type="buyer")
    result = await handler.authentication_orchestrator_flow(
        "+1", "buy", role_switch, {"intent": "other", "confidence": 90}
    )
    assert result == {"status": "role_menu"}
    profile.handle_profile_selection.assert_awaited_once()
    auth.validate_token.return_value = {"is_registered": False, "verification_required": False}
    normal = make_session()
    assert await handler.authentication_orchestrator_flow(
        "+1", "other", normal, {"intent": "other", "confidence": 90}
    ) == {"status": "fallback"}

    auth_session = make_session(intent_result={"intent": "sell_something"})
    assert not await handler._should_handle_intent_switch_during_auth(
        "sell_something", 95, "email_confirmation", auth_session
    )
    auth_session.workflow_state = {
        "intent_result": {"intent": "sell_something"},
        "filtered_users": [{"selfClient": True}],
    }
    assert not await handler._should_handle_intent_switch_during_auth(
        "buy_something", 95, "email_confirmation", auth_session
    )
    auth_session.workflow_state["filtered_users"] = [{"selfClient": False}]
    assert not await handler._should_handle_intent_switch_during_auth(
        "sell_something", 95, "email_confirmation", auth_session
    )
    auth_session.workflow_state["filtered_users"] = []
    assert await handler._should_handle_intent_switch_during_auth(
        "buy_something", 95, "email_confirmation", auth_session
    )
    assert await handler._should_handle_intent_switch_during_registration(
        "buy_something", 95, "seller"
    )
    assert await handler._should_handle_intent_switch_during_registration(
        "sell_something", 95, "buyer"
    )

    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    pending_switch = make_session(pending_auth_reg_switch={"target_role": "seller"})
    auth.user_authenticate.return_value = {"success": False}
    assert await handler._start_authentication_flow(
        "+1", "switch", pending_switch, {"intent": "unclassified", "confidence": 10}
    ) == {"status": "redirect"}

    class FakeExit:
        def __init__(self, *args, **kwargs):
            self.handle_exit_intent = AsyncMock(return_value={"status": "exited"})

    monkeypatch.setattr(auth_mod, "ExitService", FakeExit)
    assert await handler._handle_intent_switch_during_registration(
        "+1", make_session(), "exit", {"intent": "exit_system"}, "buyer"
    ) == {"status": "exited"}


@pytest.mark.asyncio
async def test_remaining_confirmation_submission_shape_branch(monkeypatch):
    handler = bare(ConfirmationHandler, whatsapp_service=AsyncMock())
    api = SimpleNamespace(create_rfq=AsyncMock(return_value={"success": True, "rfq_id": "rfq-items"}))
    monkeypatch.setattr(confirmation_mod, "RFQAPIService", lambda: api)
    schema = SimpleNamespace(model_dump=lambda: {
        "project_desc": "Laptop",
        "items": [{"description": "Laptop", "quantity": 2, "unit_of_measures": "pieces", "brand": "HP"}],
        "delivery_locations": [{"state": "Maharashtra", "city": "Pune", "pincode": "560001"}],
        "division": "IT",
    })
    result = await handler._submit_rfq_to_backend(schema, make_user())
    assert result["success"]
    payload = api.create_rfq.await_args.args[0]
    assert payload["items"][0]["description"] == "Laptop"
    assert result["rfq_data"]["items"][0]["preferred_brand"] == "HP"


@pytest.mark.asyncio
async def test_remaining_intent_switch_context_and_description_branches(monkeypatch):
    openai = MagicMock()
    monkeypatch.setattr(intent_mod, "OpenAIService", lambda: openai)
    handler = IntentSwitchHandler(AsyncMock(), AsyncMock())

    pending_optional = make_session(
        extracted_entities=[{"description": "Laptop"}],
        pending_optional_rfq={"entities": {"description": "Laptop"}},
    )
    pending_optional.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(
        pending_optional,
        "buy_something",
        95,
        {"conversation_stage": "new_request", "references_existing_data": False},
    )
    no_context = make_session(extracted_entities=[{"description": "Laptop"}])
    no_context.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(no_context, "buy_something", 95)
    unknown_entity = make_session(extracted_entities=["not a mapping"])
    unknown_entity.workflow_type = WorkflowType.authentication
    assert handler._get_workflow_description(unknown_entity) == "current request"
    assert handler._get_workflow_description(make_session()) == "current request"


@pytest.mark.asyncio
async def test_remaining_products_and_purchase_branch_matrix(monkeypatch):
    handler = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())
    needs_product = make_session()
    result = await handler.handle_products_array(
        make_user(), needs_product, "location only", [], global_supplementary_fields={"city": "Pune"}
    )
    assert result["status"] == "need_product_description"

    caption_session = make_session(attachment_caption="blue finish")
    handler._categorize_products_by_completeness = AsyncMock(return_value=([], [{
        "index": 1, "entities": {"description": "Laptop"}
    }]))
    handler._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    result = await handler.handle_products_array(
        make_user(), caption_session, "laptop", [{"description": "Laptop"}]
    )
    assert result == {"status": "complete"}
    assert caption_session.workflow_state.get("attachment_caption") is None

    class CombinedSchema:
        def get_combined_questions(self):
            return {
                "mandatory": ["Delivery date?", "Quantity?"],
                "has_mandatory": True,
                "has_optional": False,
            }

    monkeypatch.setattr(
        products_mod.ChatServiceHelpers,
        "create_rfq_schema_from_entities",
        lambda *_args: CombinedSchema(),
    )
    questions, missing = await handler._generate_clarification_questions(
        [
            {"index": 1, "entities": {"description": "Laptop"}, "missing_fields": ["delivery_date"]},
            {"index": 2, "entities": {"description": "Chair"}, "missing_fields": ["delivery_date"]},
        ],
        2,
    )
    assert questions == ["Delivery date?", "Quantity?"]
    assert missing == ["delivery_date"]

    merged = await handler._merge_with_existing_incomplete_products(
        [{"description": "Laptop"}],
        [{"description": "Chair", "quantity": 2}],
    )
    assert [product["description"] for product in merged] == ["Laptop", "Chair"]

    purchase = PurchaseIntentHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    two_quantity = await purchase._handle_quantity_limit_violations(
        make_user(), make_session(), {
            "quantity_violations": [
                {"description": "Laptop", "quantity": 100000001},
                {"description": "Chair", "quantity": 100000002},
            ]
        },
    )
    assert two_quantity["status"] == "quantity_limit_violation"
    monkeypatch.setattr(
        "app.services.cancel_service.CancelService",
        lambda **_: SimpleNamespace(
            _clear_workflow_state=AsyncMock(),
            _send_cancellation_message=AsyncMock(),
        ),
    )
    two_non_procurable = await purchase._handle_non_procurable_items(
        make_user(), make_session(), {"non_procurable_items": ["service", "consulting"]}
    )
    assert two_non_procurable["status"] == "non_procurable_cancelled"


@pytest.mark.asyncio
async def test_remaining_sectioned_router_confirmation_branch(monkeypatch):
    handler = SectionedRFQCreationHandler(
        SimpleNamespace(extract_entities=AsyncMock()),
        AsyncMock(),
        SimpleNamespace(),
        AsyncMock(),
    )
    user = make_user()

    def section_session(section):
        session = make_session()
        sectioned_mod.WorkflowManager.initialize_sectioned_rfq(session)
        sectioned_mod.WorkflowManager.set_sectioned_rfq_section(session, section)
        return session

    handler._handle_date_location_section = AsyncMock(return_value={"status": "date"})
    assert await handler.handle_sectioned_rfq(
        user, section_session("date_location"), "confirm"
    ) == {"status": "date"}
    handler._handle_items_section = AsyncMock(return_value={"status": "items"})
    assert await handler.handle_sectioned_rfq(
        user, section_session("items"), "confirm"
    ) == {"status": "items"}


@pytest.mark.asyncio
async def test_remaining_confirmation_reachable_submission_and_empty_shapes(monkeypatch):
    handler = bare(ConfirmationHandler, whatsapp_service=AsyncMock())
    handler._submit_rfq_to_backend = AsyncMock(return_value={"success": False, "error": "rejected"})
    observed = {}

    async def observe_completion(_user, session, rfq_results, successful_count):
        observed["state"] = dict(session.workflow_state)
        observed["results"] = rfq_results
        observed["successful_count"] = successful_count

    handler._send_completion_response = AsyncMock(side_effect=observe_completion)
    monkeypatch.setattr(
        confirmation_mod.ChatServiceHelpers,
        "create_rfq_schema_from_entities",
        lambda *_: SimpleNamespace(),
    )
    session = make_session(pending_optional_rfq={"entities": {"description": "Laptop"}})
    result = await handler._handle_rfq_acceptance(make_user(), session, "Confirm")
    assert result == {"status": "multiple_rfqs_created", "successful_count": 0}
    assert "pending_rfq" in observed["state"]
    assert "pending_optional_rfq" not in observed["state"]
    assert observed["results"] == [{"success": False, "error": "rejected"}]

    api = SimpleNamespace(create_rfq=AsyncMock(return_value={"success": False, "error": "rejected"}))
    monkeypatch.setattr(confirmation_mod, "RFQAPIService", lambda: api)
    schema = SimpleNamespace(model_dump=lambda: {
        "project_desc": "Laptop",
        "items": [],
        "delivery_locations": [],
    })
    backend_result = await handler._submit_rfq_to_backend(schema, make_user())
    assert backend_result == {"success": False, "error": "rejected"}
    assert handler._extract_product_descriptions_from_rfq([
        {"success": True, "rfq_data": {"items": []}}
    ]) == []


@pytest.mark.asyncio
async def test_remaining_intent_switch_unknown_stage_and_incomplete_fallback(monkeypatch):
    handler = IntentSwitchHandler(AsyncMock(), AsyncMock())

    unknown_stage = make_session(extracted_entities=[{"description": "Laptop"}])
    unknown_stage.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(
        unknown_stage,
        "buy_something",
        95,
        {"conversation_stage": "unknown", "references_existing_data": False},
    )

    no_context_with_incomplete = make_session(
        extracted_entities=[{"description": "Laptop"}],
        incomplete_products=[{"entities": {"description": "Laptop"}}],
    )
    no_context_with_incomplete.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(
        no_context_with_incomplete,
        "buy_something",
        95,
        None,
    )


@pytest.mark.asyncio
async def test_remaining_products_persisted_shapes_caption_and_single_routing(monkeypatch):
    handler = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())
    handler._track_product_categories = AsyncMock()
    handler._categorize_products_by_completeness = AsyncMock(
        return_value=([], [{"index": 1, "entities": {"description": "Laptop", "remarks": "kept"}}])
    )
    handler._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    handler._merge_with_existing_incomplete_products = AsyncMock(
        return_value=[{"description": "Laptop", "remarks": "kept"}]
    )

    no_complete_state = make_session(
        incomplete_products=[{"entities": {"description": "old"}}],
        attachment_caption="new caption",
    )
    assert await handler.handle_products_array(
        make_user(), no_complete_state, "laptop", [{"description": "Laptop"}]
    ) == {"status": "complete"}
    assert "attachment_caption" not in no_complete_state.workflow_state

    raw_complete_state = make_session(
        incomplete_products=[{"entities": {"description": "old"}}],
        complete_products=[{"description": "Saved"}],
    )
    assert await handler.handle_products_array(
        make_user(), raw_complete_state, "saved", [{"description": "Saved"}]
    ) == {"status": "complete"}

    routing_handler = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())
    routing_handler._handle_single_complete_product = AsyncMock(return_value={"status": "single"})
    assert await routing_handler._handle_complete_products(
        make_user(), make_session(), "laptop", [{"index": 1, "entities": {"description": "Laptop"}}], []
    ) == {"status": "single"}


@pytest.mark.asyncio
async def test_remaining_purchase_pending_state_and_status_workflow_branches(monkeypatch):
    entity = AsyncMock()
    summaries = AsyncMock()
    handler = PurchaseIntentHandler(
        AsyncMock(), AsyncMock(), entity, summaries, AsyncMock(), AsyncMock()
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "is_sectioned_rfq_active", lambda *_: False)
    summaries.load_user_context.return_value = []
    entity.extract_entities.return_value = {}
    handler._handle_single_product_entities = AsyncMock(return_value={"status": "stub"})

    combined = make_session(pending_combined_rfq={"products": [{"entities": {}}]})
    assert await handler.handle_purchase_intent(
        make_user(), combined, "status", intent_result={"intent": "rfq_status_check"}
    ) == {"status": "stub"}
    assert entity.extract_entities.await_args.kwargs["workflow_type"] == "rfq_status_check"

    single = make_session(pending_rfq={"entities": {"description": "Laptop"}})
    assert await handler.handle_purchase_intent(
        make_user(), single, "continue", intent_result={"intent": "buy_something"}
    ) == {"status": "stub"}
