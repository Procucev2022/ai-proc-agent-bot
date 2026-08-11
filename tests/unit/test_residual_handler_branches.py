"""Deterministic branch tests for the requested residual handlers.

The tests use bare handler instances and in-memory session/user fakes. External
services, persistence, AI, API, WhatsApp, and workflow boundaries are mocked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.handlers.auth_registration_intent_switch as auth_switch_mod
import app.services.handlers.authentication_orchestrator as auth_orchestrator_mod
import app.services.handlers.confirmation_handler as confirmation_mod
import app.services.handlers.format_modification_handler as format_mod
import app.services.handlers.intent_switch_handler as intent_mod
import app.services.handlers.products_array_handler as products_mod
import app.services.handlers.purchase_intent_handler as purchase_intent_mod
import app.services.handlers.purchase_workflow_handler as purchase_workflow_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
from app.models import ConversationOutcome, WorkflowType
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.purchase_workflow_handler import PurchaseWorkflowHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler


def make_session(**state):
    return SimpleNamespace(
        session_id="sid",
        phone_number="+91123",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history=[],
        product_items=[],
        extracted_entities=[],
        outcome=None,
        completed_at=None,
        rfq_ids=[],
        user_type=None,
    )


def make_user(role="buyer", registered=True):
    return SimpleNamespace(
        id="user-1",
        org_id="org-1",
        phone_number="+91123",
        role=role,
        is_registered=registered,
        self_client=role == "buyer",
    )


def bare(cls, **attrs):
    instance = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(instance, name, value)
    return instance


@pytest.mark.asyncio
async def test_auth_registration_switch_success_invalid_and_ai_fallback(monkeypatch):
    handler = bare(
        AuthRegistrationIntentSwitch,
        whatsapp_service=AsyncMock(),
        openai_service=MagicMock(),
    )
    current = make_session(user_type="buyer")
    current.workflow_type = "authentication"

    assert await handler.should_handle_auth_reg_switch(current, "sell_something", "seller")
    current.workflow_state["pending_auth_reg_switch"] = {"pending": True}
    assert not await handler.should_handle_auth_reg_switch(current, "sell_something", "seller")
    assert not await handler.should_handle_auth_reg_switch(make_session(), "sell_something", "seller")

    current.workflow_state.pop("pending_auth_reg_switch")
    presented = await handler.handle_auth_reg_switch_choice(
        "+91123", current, "I want to sell", "sell_something", "seller"
    )
    assert presented["status"] == "auth_reg_switch_choice_presented"
    assert current.workflow_state["pending_auth_reg_switch"]["new_user_type"] == "seller"

    assert (await handler.handle_auth_reg_switch_response("+91123", current, "1"))["status"] == "continue_current_workflow"
    current.workflow_state["pending_auth_reg_switch"] = {
        "new_intent": "sell_something",
        "new_user_type": "seller",
        "new_message": "sell",
    }
    switched = await handler.handle_auth_reg_switch_response("+91123", current, "2")
    assert switched["status"] == "switch_to_new_combination"
    assert current.workflow_type is None and current.outcome == "abandoned"

    current.workflow_state["pending_auth_reg_switch"] = {
        "new_intent": "sell_something",
        "new_user_type": "seller",
        "new_message": "sell",
    }
    handler.openai_service.generate_response.side_effect = RuntimeError("AI unavailable")
    unclear = await handler.handle_auth_reg_switch_response("+91123", current, "possibly")
    assert unclear["status"] == "clarification_requested"
    assert (await handler.handle_auth_reg_switch_response("+91123", make_session(), "1"))["status"] == "no_pending_switch"

    handler.openai_service.generate_response.side_effect = None
    handler.openai_service.generate_response.return_value = "continue_current"
    assert await handler._analyze_switch_choice("perhaps") == "continue_current"
    handler.openai_service.generate_response.return_value = "unrelated"
    assert await handler._analyze_switch_choice("perhaps") == "unclear"


@pytest.mark.asyncio
async def test_auth_registration_account_role_and_exception_branches(monkeypatch):
    handler = bare(AuthRegistrationIntentSwitch, whatsapp_service=AsyncMock(), openai_service=MagicMock())
    registered = make_user("buyer", True)
    unregistered = make_user("buyer", False)

    assert (await handler.handle_account_change_confirmation(registered, make_session(), "sell", "seller", "sell_something"))["status"] == "account_change_confirmation_requested"
    assert (await handler.handle_account_change_confirmation(unregistered, make_session(), "sell", "seller", "sell_something"))["status"] == "account_change_confirmation_requested"
    assert (await handler.handle_account_switch_confirmation(registered, make_session(), "change", "buyer"))["status"] == "account_switch_confirmation_requested"
    assert (await handler.handle_role_switch_confirmation(registered, make_session(), "sell", "seller"))["status"] == "role_switch_confirmation_requested"

    pending = {"target_role": "seller", "current_role": "buyer", "original_message": "sell"}
    account_session = make_session(pending_account_switch=pending)
    handler._ai_validate_three_option_response = AsyncMock(return_value="continue_current")
    result = await handler.handle_account_switch_response(registered, account_session, "3", AsyncMock())
    assert result["status"] == "account_switch_declined"
    assert "pending_account_switch" not in account_session.workflow_state

    role_session = make_session(pending_role_switch=pending)
    handler._handle_switch_to_existing_account = AsyncMock(return_value={"status": "switched"})
    assert (await handler.handle_role_switch_response(registered, role_session, "1", AsyncMock()))["status"] == "switched"
    assert (await handler.handle_role_switch_response(registered, make_session(), "1", AsyncMock()))["status"] == "no_pending_role_switch"

    handler._ai_validate_three_option_response = AsyncMock(return_value="unclear")
    account_session = make_session(pending_account_switch=pending)
    assert (await handler.handle_account_switch_response(registered, account_session, "?", AsyncMock()))["status"] == "account_switch_clarification_requested"

    assert (await handler._show_account_selection_clarification(registered, {"formatted_options": [{"text": "1. a@x"}]}, "seller"))["status"] == "account_selection_clarification_requested"
    assert await handler._parse_account_selection("missing", [{"number": 1, "email": "a@x"}]) is None

    handler.whatsapp_service.send_message.side_effect = RuntimeError("send failure")
    error = await handler.handle_role_switch_confirmation(registered, make_session(), "x", "seller")
    assert error["status"] == "error"


@pytest.mark.asyncio
async def test_authentication_orchestrator_entry_selection_and_workflow_errors(monkeypatch):
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
    handler.profile_selection_service.handle_profile_selection.return_value = {"status": "profile"}
    handler.profile_selection_service.handle_profile_selection_response.return_value = {"status": "selected"}
    handler.support_service.redirect_to_support.return_value = {"status": "support"}
    handler.authentication_service.validate_token.return_value = None

    assert (await handler.authentication_orchestrator_flow(
        "+91123", "1", make_session(profile_selection_stage="choose"), {"intent": "other", "confidence": 90}
    ))["status"] == "selected"
    assert (await handler.authentication_orchestrator_flow(
        "+91123", "buy", make_session(), {"intent": "buy_something", "confidence": 90}
    ))["status"] == "profile"

    missing_intent = await handler.authentication_orchestrator_flow("+91123", "hello", make_session(), None)
    assert missing_intent["status"] == "fallback_handled"

    handler.authentication_service.validate_token.side_effect = RuntimeError("database unavailable")
    assert (await handler.authentication_orchestrator_flow("+91123", "x", make_session(), {"intent": "other"}))["status"] == "support"

    auth_session = make_session(authentication_stage="email_otp")
    auth_session.workflow_type = WorkflowType.authentication
    handler.authentication_service.handle_email_otp_validation.return_value = {"status": "otp"}
    assert (await handler._handle_authentication_workflow("+91123", "1234", auth_session, {}))["status"] == "otp"

    handler.authentication_service.user_authenticate.return_value = {"success": False}
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "registration"})
    assert (await handler._start_authentication_flow(
        "+91123", "sell", make_session(), {"intent": "sell_something", "confidence": 90}
    ))["status"] == "registration"

    handler.authentication_service.user_authenticate.return_value = {
        "success": True, "response": [{"email": "buyer@example.com"}]
    }
    handler.authentication_service.filter_users_by_intent = MagicMock(return_value={"success": True, "filtered_users": [{"email": "buyer@example.com"}], "unique_emails": ["buyer@example.com"]})
    handler.authentication_service.initiate_email_confirmation.return_value = {"status": "email_confirmation"}
    selected = await handler._start_authentication_flow(
        "+91123", "buy", make_session(), {"intent": "buy_something", "confidence": 90}
    )
    assert selected["status"] == "email_confirmation"

    handler._start_authentication_flow = AsyncMock(side_effect=RuntimeError("flow"))
    assert (await handler._handle_authentication_workflow("+91123", "x", make_session(), {}))["status"] == "support"


@pytest.mark.asyncio
async def test_confirmation_handler_button_fallback_submission_and_restart():
    handler = bare(
        ConfirmationHandler,
        whatsapp_service=AsyncMock(),
        response_helpers=AsyncMock(),
        cancel_service=None,
        session_manager=None,
        confirmation_service=AsyncMock(),
        _bfs_search_handler=None,
    )
    for button, method in [
        ("confirm_rfq", "_handle_rfq_acceptance"),
        ("no_rfq", "_handle_rfq_modification"),
        ("continue_rfq", "_proceed_to_confirmation_from_optional"),
        ("confirm_no_changes", "_handle_rfq_acceptance"),
    ]:
        setattr(handler, method, AsyncMock(return_value={"status": button}))
        assert (await handler.handle_confirmation_button(make_user(), make_session(), button))["status"] == button
    assert (await handler.handle_confirmation_button(make_user(), make_session(), "bad"))["status"] == "unknown_button"

    handler.confirmation_service.parse_confirmation.return_value = "unclear"
    handler._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"})
    handler._handle_rfq_modification = AsyncMock(return_value={"status": "modified"})
    handler._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarified"})
    assert (await handler.handle_pending_confirmations(make_user(), make_session(), "yes", {"intent": "confirmation_response", "confidence": .8, "context_analysis": {"confirmation_details": {"response_type": "accept", "has_conditions": False}}}))["status"] == "accepted"
    assert (await handler.handle_pending_confirmations(make_user(), make_session(), "change", {"intent": "confirmation_response", "confidence": .8, "context_analysis": {"confirmation_details": {"response_type": "accept", "has_conditions": True}}}))["status"] == "modified"
    assert (await handler.handle_pending_confirmations(make_user(), make_session(), "?", {}))["status"] == "clarified"

    handler._send_completion_response = AsyncMock()
    handler._submit_rfq_to_backend = AsyncMock(return_value={"success": True, "rfq_id": "R-1"})
    handler._handle_rfq_acceptance = ConfirmationHandler._handle_rfq_acceptance.__get__(handler)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(confirmation_mod, "RFQValidationSchema", lambda **data: SimpleNamespace(**data))
    completed = make_session(pending_combined_rfq={"combined_schema": {"project_desc": "pump"}})
    result = await handler._handle_rfq_acceptance(make_user(), completed, "confirm")
    assert result["status"] == "multiple_rfqs_created"
    assert completed.outcome == ConversationOutcome.completed and completed.workflow_state == {}
    monkeypatch.undo()

    assert (await handler._handle_restart_workflow(make_user(), make_session()))["status"] == "error"
    handler.cancel_service = AsyncMock()
    handler.session_manager = AsyncMock()
    handler.cancel_service.handle_cancel_intent.return_value = {"status": "restarted"}
    assert (await handler._handle_restart_workflow(make_user(), make_session()))["status"] == "restarted"


@pytest.mark.asyncio
async def test_sectioned_handler_routing_validation_restart_and_buttons(monkeypatch):
    handler = bare(
        SectionedRFQCreationHandler,
        entity_service=AsyncMock(),
        whatsapp_service=AsyncMock(),
        cancel_service=AsyncMock(),
        session_manager=AsyncMock(),
        confirmation_handler=AsyncMock(),
    )
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda s: bool(s.workflow_state.get("restart_pending")))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda s: s.workflow_state.get("section", "unknown"))
    pending = make_session(restart_pending=True, section="date_location")
    handler._handle_restart_confirmation_response = AsyncMock(return_value={"status": "restart_response"})
    assert (await handler.handle_sectioned_rfq(make_user(), pending, {"text": {"body": "yes"}}))["status"] == "restart_response"

    unknown = make_session(section="unknown")
    assert (await handler.handle_sectioned_rfq(make_user(), unknown, 12))["status"] == "error"

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, name: s.workflow_state.get(name))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda s, name, value: s.workflow_state.__setitem__(name, value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification", MagicMock())
    handler.entity_service.extract_entities.return_value = {"deliveryDate": "tomorrow", "pincode": "560001", "products": []}
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "2 January 2030"})
    handler._autofill_location_from_pincode = AsyncMock(return_value={"is_valid": True, "delivery_data": {"deliveryDate": "2 January 2030", "pincode": "560001", "city": "Pune", "state": "Maharashtra"}})
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "delivery_confirmation"})
    delivery = await handler._handle_date_location_section(make_user(), make_session(section="date_location"), "details", [])
    assert delivery["status"] == "delivery_confirmation"

    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": False, "error": "bad date"})
    handler._display_delivery_validation_error = AsyncMock(return_value={"status": "validation_error"})
    invalid = await handler._handle_date_location_section(make_user(), make_session(section="date_location"), "bad", [])
    assert invalid["status"] == "validation_error"

    handler._handle_section_confirm = AsyncMock(return_value={"status": "confirmed"})
    handler._handle_section_modify = AsyncMock(return_value={"status": "modified"})
    handler._handle_restart_rfq = AsyncMock(return_value={"status": "restart"})
    handler._handle_final_rfq_submission = AsyncMock(return_value={"status": "submitted"})
    handler._handle_final_cancel = AsyncMock(return_value={"status": "cancelled"})
    for button, expected in [("confirm_items", "confirmed"), ("modify_items", "modified"), ("restart_rfq", "restart"), ("final_confirm_rfq", "submitted"), ("final_cancel_rfq", "cancelled"), ("bad", "error")]:
        assert (await handler.handle_section_button_click(make_user(), make_session(), button))["status"] == expected

    handler.cancel_service._clear_workflow_state = AsyncMock()
    handler.cancel_service._send_cancellation_message = AsyncMock()
    assert (await handler._cancel_after_max_retries(make_user(), make_session(), "items"))["status"] == "max_retries_cancelled"
    handler._autofill_location_from_pincode = SectionedRFQCreationHandler._autofill_location_from_pincode.__get__(handler)
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    assert not (await handler._autofill_location_from_pincode({"pincode": "560001"}))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert "Error validating" in (await handler._autofill_location_from_pincode({"pincode": "560001"}))["error"]


@pytest.mark.asyncio
async def test_products_array_handler_empty_incomplete_complete_and_merge_branches(monkeypatch):
    handler = bare(
        ProductsArrayHandler,
        whatsapp_service=AsyncMock(),
        openai_service=MagicMock(),
        response_helpers=AsyncMock(),
        session_manager=AsyncMock(),
    )
    handler._track_product_categories = AsyncMock()
    handler._categorize_products_by_completeness = AsyncMock(return_value=([], [{"index": 1, "entities": {"description": "pump"}}]))
    handler._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    assert (await handler.handle_products_array(make_user(), make_session(), "x", [], global_supplementary_fields={"city": "Pune"}))["status"] == "need_product_description"

    handler._categorize_products_by_completeness.return_value = ([{"index": 1, "entities": {"description": "pump"}, "missing_fields": ["quantity"]}], [])
    handler._handle_incomplete_products = AsyncMock(return_value={"status": "incomplete"})
    result = await handler.handle_products_array(make_user(), make_session(), "x", [{"description": "pump"}])
    assert result["status"] == "incomplete"

    existing = [{"entities": {}}, {"entities": {}}]
    merged = await handler._merge_with_existing_incomplete_products(existing, [{"description": "pump"}, {"description": "bolt"}])
    assert [item["description"] for item in merged] == ["pump", "bolt"]
    fallback = await handler._merge_with_existing_incomplete_products([{"entities": {"description": "pump"}}], [{"bad": object()}])
    assert fallback

    assert handler._no_products_mentioned([])
    assert handler._no_products_mentioned([{"description": "NO_PRODUCTS_MENTIONED"}])
    assert not handler._no_products_mentioned([{"description": "pump"}])

    schema = SimpleNamespace(get_missing_mandatory_fields=lambda: ["quantity"], get_combined_questions=lambda: {"mandatory": ["Quantity?"], "optional": [], "has_mandatory": True, "has_optional": False})
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: schema)
    incomplete, complete = await handler._categorize_products_by_completeness([{"description": "pump"}])
    assert incomplete and not complete
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", MagicMock(side_effect=RuntimeError("schema")))
    incomplete, complete = await handler._categorize_products_by_completeness([{"description": "bolt"}])
    assert incomplete[0]["missing_fields"] and not complete


@pytest.mark.asyncio
async def test_purchase_intent_handler_sectioned_priority_and_error_branches(monkeypatch):
    handler = PurchaseIntentHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr(purchase_intent_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    handler.chat_summary_service.load_user_context.return_value = ["summary"]
    extracted = {"products": [{"description": "pump"}], "global_supplementary_fields": {"city": "Pune"}}
    handler.entity_service.extract_entities.return_value = extracted
    handler.entity_service.extract_entities_with_summary_context.return_value = extracted
    handler.products_array_handler.handle_products_array.return_value = {"status": "products"}
    result = await handler.handle_purchase_intent(
        make_user(), make_session(session_archive={"stale": True}), {"text": {"body": "buy"}}, {"intent": "buy_something"}, lambda _: True
    )
    assert result["status"] == "products"
    assert "session_archive" not in handler.session_manager.mock_calls[0][1] if handler.session_manager.mock_calls else True

    handler.entity_service.extract_entities.return_value = {"error_type": "quantity_limit", "quantity_violations": [{"description": "pump", "quantity": 100000001}]}
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "quantity_limit_violation"
    handler.entity_service.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "change"))["status"] == "modification_clarification_sent"
    handler.entity_service.extract_entities.side_effect = RuntimeError("extract")
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "error"

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True))
    monkeypatch.setattr(purchase_intent_mod.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(purchase_intent_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    sectioned = MagicMock(handle_sectioned_rfq=AsyncMock(return_value={"status": "sectioned"}))
    monkeypatch.setattr(purchase_intent_mod, "SectionedRFQCreationHandler", lambda **_: sectioned, raising=False)
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", lambda **_: sectioned)
    monkeypatch.setattr("app.services.cancel_service.CancelService", MagicMock())
    handler.entity_service.extract_entities.side_effect = None
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "buy"))["status"] == "sectioned"


@pytest.mark.asyncio
async def test_purchase_workflow_handler_success_empty_nonprocurable_and_exception(monkeypatch):
    handler = bare(
        PurchaseWorkflowHandler,
        entity_service=AsyncMock(),
        rfq_service=AsyncMock(),
        whatsapp_service=AsyncMock(),
        openai_service=AsyncMock(),
        response_helpers=AsyncMock(),
    )
    handler.entity_service.extract_entities.return_value = {"products": [{"description": "pump"}]}
    handler._handle_products_array = AsyncMock(return_value={"status": "products"})
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "buy"))["status"] == "products"

    handler.entity_service.extract_entities.return_value = {"entities": {"description": "pump"}}
    handler._handle_single_entity = AsyncMock(return_value={"status": "single"})
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "buy"))["status"] == "single"
    handler.entity_service.extract_entities.return_value = {}
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "buy"))["status"] == "no_new_entities"
    assert await PurchaseWorkflowHandler._handle_products_array(handler, make_user(), make_session(), "x", []) is None
    assert await PurchaseWorkflowHandler._handle_single_entity(handler, make_user(), make_session(), "x", {}) is None

    handler.entity_service.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await handler.handle_purchase_intent(make_user(), make_session(), "change"))["status"] == "modification_clarification_sent"
    handler.entity_service.extract_entities.side_effect = RuntimeError("extract")
    with pytest.raises(RuntimeError):
        await handler.handle_purchase_intent(make_user(), make_session(), "buy")


@pytest.mark.asyncio
async def test_format_modification_handler_not_waiting_retry_max_and_replacement(monkeypatch):
    handler = FormatModificationHandler(AsyncMock())
    session = make_session()
    user = make_user()
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda *_: (False, None))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda *_: (True, "unknown"))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda *_: (True, "delivery"))
    handler.session_manager = AsyncMock()
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda *_: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_, **__: 1)
    monkeypatch.setattr(format_mod.WorkflowManager, "clear_awaiting_modification", MagicMock())
    result = await handler.handle_format_modification("bad", session, user)
    assert result["status"] == "format_error"

    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_, **__: 3)
    result = await handler.handle_format_modification("bad", session, user)
    assert result["status"] == "max_retries_reached"

    monkeypatch.setattr(format_mod.WorkflowManager, "get_delivery_details", lambda *_: {"delivery_date": "2030", "pincode": "560001", "city": "Pune", "state": "Maharashtra"})
    session.workflow_state["incomplete_products"] = [1]
    session.workflow_state["complete_products"] = [2]
    await handler._replace_products_array(session, [{"description": "pump"}, {"description": "bolt", "city": "Other"}])
    assert session.workflow_state["extracted_entities"][0]["city"] == "Pune"
    assert session.workflow_state["extracted_entities"][1]["city"] == "Other"
    assert "incomplete_products" not in session.workflow_state


@pytest.mark.asyncio
async def test_intent_switch_handler_confidence_context_response_and_descriptions():
    handler = bare(IntentSwitchHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), openai_service=AsyncMock())
    active = make_session(extracted_entities=[{"product_name": "Laptop"}])
    active.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(active, "buy_something", 89)
    assert not await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "collecting", "references_existing_data": True})
    assert await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "new_request"})
    active.workflow_state["pending_intent_switch"] = {"new_intent": "x"}
    assert not await handler.should_handle_intent_switch(active, "general_inquiry", 100)
    active.workflow_state.pop("pending_intent_switch")

    handler.response_helpers.generate_contextual_response.return_value = "choose"
    assert (await handler.handle_intent_switch_choice(make_user(), active, "sell", "sell_something", {"intent": "sell_something"}))["status"] == "intent_switch_choice_presented"

    handler.openai_service.analyze_intent_switch_response.return_value = {"chosen_action": "continue_current"}
    assert (await handler.handle_intent_switch_response(make_user(), active, "1"))["status"] == "continue_current_workflow"
    active.workflow_state["pending_intent_switch"] = {"new_intent": "sell_something", "new_intent_message": "sell", "intent_result": {}}
    handler.openai_service.analyze_intent_switch_response.return_value = {"chosen_action": "switch_to_new"}
    switched = await handler.handle_intent_switch_response(make_user(), active, "2")
    assert switched["status"] == "switch_to_new_intent"
    assert active.workflow_type is None and active.outcome == ConversationOutcome.abandoned

    corrupted = make_session(pending_intent_switch="bad")
    assert (await handler.handle_intent_switch_response(make_user(), corrupted, "x"))["status"] == "corrupted_intent_switch_data"
    incomplete = make_session(pending_intent_switch={"new_intent": "buy_something"})
    assert (await handler.handle_intent_switch_response(make_user(), incomplete, "2"))["status"] == "error"

    handler.openai_service.analyze_intent_switch_response.side_effect = RuntimeError("AI")
    fallback = make_session(pending_intent_switch={"new_intent": "buy_something", "new_intent_message": "buy", "intent_result": {}})
    assert (await handler.handle_intent_switch_response(make_user(), fallback, "1 continue current"))["status"] == "continue_current_workflow"
    assert handler._get_workflow_description(make_session()) == "current request"
    assert handler._get_intent_description("unknown") == "handle your request"
