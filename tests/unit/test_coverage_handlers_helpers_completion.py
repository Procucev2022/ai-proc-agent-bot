from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import ConversationOutcome, WorkflowType
from app.services.handlers import authentication_orchestrator as orchestrator_mod
from app.services.handlers import confirmation_handler as confirmation_mod
from app.services.handlers import format_modification_handler as format_mod_mod
from app.services.handlers import intent_switch_handler as intent_mod
from app.services.handlers import purchase_intent_handler as purchase_mod
from app.services.handlers import sectioned_rfq_creation_handler as sectioned_mod
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.helpers.attachment_helpers import AttachmentHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers


class BrokenMapping:
    def get(self, *_args, **_kwargs):
        raise RuntimeError("broken mapping")


def make_session(**state):
    return SimpleNamespace(
        session_id="session-completion",
        external_user_id="external-user",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={"openai_messages": [], "metadata": [], "messages": []},
        outcome=None,
        created_at=None,
        last_activity_at=None,
        completed_at=None,
        user_type=None,
        product_items=[],
        extracted_entities=[],
        incomplete_products=[],
        complete_products=[],
        rfq_ids=[],
        rfq_id=None,
        retention_date=None,
        bfs_products_searched=[],
        bfs_search_count=0,
        bfs_price_accepted=[],
        bfs_counter_offers=[],
        products_bid_for=[],
        bids_received=[],
        bids_accepted=[],
        counter_offers_made=[],
        counter_offers_accepted=[],
        rfqs_with_response=[],
    )


def make_user(role="buyer", registered=True):
    return SimpleNamespace(
        id="user-1",
        org_id="org-1",
        phone_number="+919999999999",
        role=role,
        is_registered=registered,
    )


# ---------------------------------------------------------------------------
# Auth/registration switch and authentication orchestrator edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_switch_account_roles_and_selection_alternates(monkeypatch):
    handler = AuthRegistrationIntentSwitch.__new__(AuthRegistrationIntentSwitch)
    handler.whatsapp_service = AsyncMock()
    handler.openai_service = MagicMock()

    assert handler._get_target_combination("sell_something", "buyer") == "authentication_seller"
    assert handler._get_target_combination("buy_something", "seller") == "authentication_buyer"
    assert handler._get_target_combination("register_seller", "seller") == "registration_seller"
    assert handler._get_target_combination("register_buyer", "buyer") == "registration_buyer"
    assert handler._get_target_combination("register_other", "seller") == "registration_seller"
    assert handler._get_target_combination("other", "seller") == "authentication_seller"

    user = make_user(role="buyer", registered=True)
    session = make_session(user_type="buyer")
    result = await handler.handle_account_change_confirmation(user, session, "sell", "seller", "sell_something")
    assert result["status"] == "account_change_confirmation_requested"
    assert session.workflow_state["pending_account_change"]["current_role"] == "buyer"
    assert "Switch to existing seller" in handler.whatsapp_service.send_message.await_args.args[1]

    same_role = make_session(user_type="buyer")
    result = await handler.handle_account_change_confirmation(user, same_role, "buy", "buyer", "buy_something")
    assert result["status"] == "account_change_confirmation_requested"
    assert "different buyer account" in handler.whatsapp_service.send_message.await_args.args[1]

    unauthenticated = make_user(role="buyer", registered=False)
    registration_session = make_session()
    result = await handler.handle_account_change_confirmation(
        unauthenticated, registration_session, "sell", "seller", "sell_something"
    )
    assert result["status"] == "account_change_confirmation_requested"
    assert "Would you like to register as a seller" in handler.whatsapp_service.send_message.await_args.args[1]

    role_session = make_session(pending_role_switch={
        "target_role": "seller", "current_role": "buyer", "original_message": "sell"
    })
    result = await handler.handle_role_switch_response(user, role_session, "maybe", AsyncMock())
    assert result["status"] == "role_switch_clarification_requested"
    role_session.workflow_state["pending_role_switch"] = {
        "target_role": "seller", "current_role": "buyer", "original_message": "sell"
    }
    result = await handler.handle_role_switch_response(user, role_session, "2", AsyncMock())
    assert result["status"] == "role_switch_declined"
    assert "pending_role_switch" not in role_session.workflow_state

    pending = {
        "target_role": "seller", "current_role": "buyer", "original_message": "sell"
    }
    options = {"formatted_options": [{"number": 1, "text": "1 seller@x", "email": "seller@x"}]}
    handler._parse_account_selection = AsyncMock(return_value=None)
    result = await handler._handle_enhanced_account_selection_response(
        user, make_session(pending_role_switch=pending), "bad", AsyncMock(), pending, options
    )
    assert result["status"] == "account_selection_clarification_requested"

    handler._parse_account_selection = AsyncMock(return_value={"action": "continue_current"})
    continue_session = make_session(pending_role_switch=pending)
    result = await handler._handle_enhanced_account_selection_response(
        user, continue_session, "3", AsyncMock(), pending, options
    )
    assert result["status"] == "role_switch_declined"
    assert continue_session.workflow_state == {}

    seller_user = make_user(role="seller")
    seller_pending = {**pending, "current_role": "seller", "target_role": "buyer"}
    seller_session = make_session(pending_role_switch=seller_pending)
    handler._parse_account_selection = AsyncMock(return_value={"action": "continue_current"})
    result = await handler._handle_enhanced_account_selection_response(
        seller_user, seller_session, "3", AsyncMock(), seller_pending,
        {"formatted_options": []},
    )
    assert result["continue_with_original_intent"] is True
    assert handler.whatsapp_service.send_configurable_buttons.await_count >= 2


@pytest.mark.asyncio
async def test_auth_switch_ai_fallback_and_response_states():
    handler = AuthRegistrationIntentSwitch.__new__(AuthRegistrationIntentSwitch)
    handler.whatsapp_service = AsyncMock()
    handler.openai_service = MagicMock()

    handler.openai_service.generate_response.return_value = "continue_current"
    assert await handler._analyze_switch_choice("I want to stay") == "continue_current"
    handler.openai_service.generate_response.side_effect = RuntimeError("openai")
    assert await handler._analyze_switch_choice("not clear") == "unclear"

    session = make_session(pending_auth_reg_switch={
        "new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"
    })
    assert (await handler.handle_auth_reg_switch_response("9", session, "continue"))["status"] == "continue_current_workflow"

    session.workflow_state["pending_auth_reg_switch"] = {
        "new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"
    }
    assert (await handler.handle_auth_reg_switch_response("9", session, "exit"))["status"] == "exit_requested"
    assert session.outcome == "abandoned"

    session.workflow_state["pending_auth_reg_switch"] = {
        "new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"
    }
    handler.openai_service.generate_response.side_effect = None
    handler.openai_service.generate_response.return_value = "switch_to_new"
    result = await handler.handle_auth_reg_switch_response("9", session, "maybe")
    assert result["status"] == "switch_to_new_combination"
    assert result["target_workflow"] == "authentication"

    assert await handler.handle_auth_reg_switch_response("9", make_session(), "1") == {"status": "no_pending_switch"}


@pytest.mark.asyncio
async def test_authentication_orchestrator_priority_role_switch_and_registration_edges(monkeypatch):
    handler = AuthenticationOrchestrator.__new__(AuthenticationOrchestrator)
    handler.whatsapp_service = AsyncMock()
    handler.authentication_service = AsyncMock()
    handler.registration_service = AsyncMock()
    handler.intent_service = AsyncMock()
    handler.support_service = AsyncMock()
    handler.chat_service = None
    handler.auth_reg_switch = AsyncMock()
    handler.profile_selection_service = AsyncMock()

    class FakeExit:
        def __init__(self, *_args, **_kwargs):
            pass

        async def handle_exit_intent(self, *_args, **_kwargs):
            return {"status": "exited"}

    monkeypatch.setattr(orchestrator_mod, "ExitService", FakeExit)
    exit_session = make_session()
    result = await handler.authentication_orchestrator_flow(
        "9", "bye", exit_session, {"intent": "exit_system", "confidence": 99}
    )
    assert result["status"] == "exited"

    role_session = make_session(role_switch_in_progress=True, user_type="seller")
    handler.authentication_service.user_authenticate.return_value = {
        "success": True, "response": [{"email": "seller@x", "selfClient": False}]
    }
    handler.authentication_service.filter_users_by_intent = MagicMock(return_value={
        "success": True, "filtered_users": [{"email": "seller@x"}], "unique_emails": ["seller@x"]
    })
    handler._handle_user_selection = AsyncMock(return_value={"status": "selected"})
    result = await handler.authentication_orchestrator_flow("9", "sell", role_session, {"intent": "sell_something"})
    assert result["status"] == "selected"
    assert "role_switch_in_progress" not in role_session.workflow_state

    role_session = make_session(role_switch_in_progress=True, user_type="seller")
    handler.authentication_service.user_authenticate.return_value = {"success": False}
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "registered"})
    assert (await handler.authentication_orchestrator_flow("9", "sell", role_session, {"intent": "sell_something"}))["status"] == "registered"

    handler.registration_service.handle_registration_otp_validation.return_value = {
        "status": "registration_completed", "registration_flow_complete": True
    }
    reg_session = make_session(registration_stage="email_otp", user_type="buyer")
    reg_session.workflow_type = "registration"
    result = await handler._handle_registration_workflow("9", "1234", reg_session, {})
    assert result["registration_flow_complete"] is True
    assert reg_session.workflow_type is None and reg_session.workflow_state == {}

    handler.authentication_service.handle_domain_matching.return_value = {"status": "domain"}
    domain_session = make_session(registration_stage="domain_matching", user_type="buyer")
    assert (await handler._handle_registration_workflow("9", "acme", domain_session, {}))["status"] == "domain"

    handler.registration_service.handle_registration_confirmation.return_value = {"status": "confirmed"}
    confirm_session = make_session(registration_stage="confirmation", user_type="buyer")
    assert (await handler._handle_registration_workflow("9", "yes", confirm_session, {}))["status"] == "confirmed"

    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    invalid_session = make_session(registration_stage="unknown", user_type="seller")
    assert (await handler._handle_registration_workflow("9", "x", invalid_session, {}))["status"] == "redirect"


@pytest.mark.asyncio
async def test_authentication_orchestrator_selection_and_auth_flow_error_fallbacks():
    handler = AuthenticationOrchestrator.__new__(AuthenticationOrchestrator)
    handler.whatsapp_service = AsyncMock()
    handler.authentication_service = AsyncMock()
    handler.registration_service = AsyncMock()
    handler.support_service = AsyncMock()
    handler.auth_reg_switch = AsyncMock()
    handler.profile_selection_service = AsyncMock()
    handler.chat_service = None

    session = make_session()
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    result = await handler._handle_user_selection(
        "9", session, {"filtered_users": [], "unique_emails": []}, {"intent": "sell_something"}, "sell", []
    )
    assert result["status"] == "redirect"

    handler.authentication_service.initiate_email_confirmation.return_value = {"status": "email_prompt"}
    result = await handler._handle_user_selection(
        "9", session, {"filtered_users": [{"email": "a@x"}], "unique_emails": ["a@x"]},
        {"intent": "buy_something"}, "buy", [{"email": "a@x"}],
    )
    assert result["status"] == "email_prompt"
    assert session.workflow_state["authentication_stage"] == "email_confirmation"

    handler.authentication_service.user_authenticate.side_effect = RuntimeError("auth")
    handler.support_service.redirect_to_support.return_value = {"status": "support"}
    result = await handler._start_authentication_flow("9", "buy", make_session(), {"intent": "buy_something", "confidence": 90})
    assert result["status"] == "support"

    handler.whatsapp_service.send_message.side_effect = RuntimeError("send")
    result = await handler._handle_auth_clarification_request("9", "unclear")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Confirmation, intent switching, and format state machines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_submission_completion_and_backend_edges(monkeypatch):
    handler = ConfirmationHandler.__new__(ConfirmationHandler)
    handler.whatsapp_service = AsyncMock()
    handler.response_helpers = AsyncMock()
    handler.confirmation_service = AsyncMock()
    handler.cancel_service = None
    handler.session_manager = None
    handler._bfs_search_handler = None

    class FakeRFQService:
        def __init__(self):
            self.create_rfq = AsyncMock(return_value={"success": True, "rfq_id": "rfq-1"})

    monkeypatch.setattr(confirmation_mod, "RFQAPIService", FakeRFQService)
    schema = SimpleNamespace(model_dump=lambda: {
        "project_desc": "Laptop", "items": [{"description": "Laptop", "quantity": 2, "unit_of_measures": "pcs"}],
        "delivery_locations": [{"state": "K", "city": "B", "pincode": "560001"}],
        "attachments": [{"file_name": "quote.pdf"}],
    })
    result = await handler._submit_rfq_to_backend(schema, make_user())
    assert result["success"] is True and result["rfq_data"]["items"][0]["description"] == "Laptop"

    class FailingRFQService:
        def __init__(self):
            self.create_rfq = AsyncMock(side_effect=RuntimeError("backend down"))

    monkeypatch.setattr(confirmation_mod, "RFQAPIService", FailingRFQService)
    result = await handler._submit_rfq_to_backend(schema, make_user())
    assert result["success"] is False and "backend down" in result["error"]

    result = await handler._send_completion_response(make_user(), make_session(), [], 0)
    assert result is None
    result = await handler._send_completion_response(
        make_user(), make_session(),
        [{"success": True, "rfq_id": "a", "rfq_data": {"items": [{"description": "A|part"}]}},
         {"success": True, "rfq_id": "b", "rfq_data": {"items": [{"product_name": "B"}]}}],
        2,
    )
    assert result is None
    buttons = handler.whatsapp_service.send_configurable_buttons.await_args.args[2]
    assert buttons[2]["id"].startswith("check_availability_rfq|A part")


@pytest.mark.asyncio
async def test_confirmation_optional_caption_restart_and_unknown_paths(monkeypatch):
    handler = ConfirmationHandler.__new__(ConfirmationHandler)
    handler.whatsapp_service = AsyncMock()
    handler.response_helpers = AsyncMock()
    handler.confirmation_service = AsyncMock()
    handler.cancel_service = None
    handler.session_manager = None
    handler._bfs_search_handler = None

    assert (await handler.handle_confirmation_button(make_user(), make_session(), "bad"))["status"] == "unknown_button"
    assert (await handler.handle_confirmation_button(make_user(), make_session(), "restart_rfq"))["status"] == "error"

    class FakeSchema:
        def __init__(self, **data):
            self.data = data

    monkeypatch.setattr(confirmation_mod, "RFQValidationSchema", FakeSchema)
    monkeypatch.setattr(
        confirmation_mod.ChatServiceHelpers,
        "create_rfq_schema_from_entities",
        lambda entities, *_args: SimpleNamespace(model_dump=lambda: entities),
    )
    handler.response_helpers.generate_rfq_summary_and_confirmation.return_value = "summary"
    session = make_session(
        pending_optional_rfq={"entities": {"description": "Laptop", "remarks": ""}},
        attachment_caption="urgent",
        extracted_entities=[{"attachments": [{"file_name": "a"}]}],
    )
    result = await handler._proceed_to_confirmation_from_optional(make_user(), session, "continue")
    assert result["status"] == "optional_fields_skipped"
    assert session.workflow_state["pending_rfq"]["entities"]["remarks"] == "urgent"

    combined = make_session(
        pending_optional_combined_rfq={
            "combined_schema": {"project_desc": "Laptop", "items": [], "remarks": ""},
            "products": [{"entities": {"description": "Laptop", "remarks": ""}}],
        },
        attachment_caption="same note",
    )
    result = await handler._proceed_to_confirmation_from_optional(make_user(), combined, "continue")
    assert result["status"] == "optional_fields_skipped"
    assert combined.workflow_state["pending_combined_rfq"]["products"][0]["entities"]["remarks"] == "same note"


@pytest.mark.asyncio
async def test_intent_switch_continuation_corruption_and_fallback(monkeypatch):
    handler = IntentSwitchHandler.__new__(IntentSwitchHandler)
    handler.whatsapp_service = AsyncMock()
    handler.response_helpers = AsyncMock()
    handler.openai_service = AsyncMock()

    session = make_session(extracted_entities=[{"product_name": "Laptop"}])
    session.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(
        session, "buy_something", 99,
        {"conversation_stage": "collecting", "references_existing_data": True},
    )
    assert not await handler.should_handle_intent_switch(
        session, "buy_something", 99, {"conversation_stage": "optional_fields"}
    )
    assert await handler.should_handle_intent_switch(
        session, "buy_something", 99, {"conversation_stage": "new_request"}
    )

    seller_session = make_session(extracted_entities=[{"description": "x"}])
    seller_session.workflow_type = WorkflowType.seller_rfq_view
    assert await handler.should_handle_intent_switch(seller_session, "buy_something", 95)

    assert (await handler.handle_intent_switch_response(make_user(), make_session(pending_intent_switch="bad"), "x"))["status"] == "corrupted_intent_switch_data"

    incomplete = make_session(pending_intent_switch={"new_intent": "buy_something"})
    handler.openai_service.analyze_intent_switch_response.return_value = {"chosen_action": "switch_to_new"}
    result = await handler.handle_intent_switch_response(make_user(), incomplete, "2")
    assert result["status"] == "error" and incomplete.workflow_state == {}

    pending = {
        "new_intent": "buy_something", "new_intent_message": "laptops",
        "intent_result": {"intent": "buy_something", "confidence": 95},
    }
    handler.openai_service.analyze_intent_switch_response.side_effect = RuntimeError("AI unavailable")
    fallback = make_session(pending_intent_switch=pending)
    result = await handler.handle_intent_switch_response(make_user(), fallback, "1 continue current")
    assert result["status"] == "continue_current_workflow"

    switch_session = make_session(pending_intent_switch=pending)
    result = await handler.handle_intent_switch_response(make_user(), switch_session, "new request")
    assert result["status"] == "switch_to_new_intent"
    assert switch_session.outcome == ConversationOutcome.abandoned
    assert switch_session.conversation_history["messages"] == []


@pytest.mark.asyncio
async def test_format_modification_retry_and_replacement_edges(monkeypatch):
    handler = FormatModificationHandler(AsyncMock())
    handler.session_manager = AsyncMock()
    session = make_session(incomplete_products=[1], complete_products=[2])
    user = make_user()
    monkeypatch.setattr(format_mod_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 1)
    result = await handler._handle_format_error(session, user, "invalid", "delivery")
    assert result["status"] == "format_error" and result["retry_count"] == 1

    monkeypatch.setattr(format_mod_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 3)
    result = await handler._handle_format_error(session, user, "invalid", "items")
    assert result["status"] == "max_retries_reached"

    monkeypatch.setattr(format_mod_mod.WorkflowManager, "get_delivery_details", lambda *_args: None)
    await handler._replace_products_array(session, [{"description": "Laptop"}])
    assert session.workflow_state["extracted_entities"] == [{"description": "Laptop"}]
    assert "incomplete_products" not in session.workflow_state
    assert "complete_products" not in session.workflow_state

    monkeypatch.setattr(format_mod_mod.WorkflowManager, "is_awaiting_modification", lambda *_args: (_ for _ in ()).throw(RuntimeError("state")))
    result = await handler.handle_format_modification("x", session, user)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Products, purchase, and sectioned RFQ edge paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_products_array_merge_questions_and_validation_edges(monkeypatch):
    handler = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())
    handler._track_product_categories = AsyncMock()

    positional = await handler._merge_with_existing_incomplete_products(
        [{"entities": {"quantity": 1}}, {"entities": {"quantity": 2}}],
        [{"description": "Laptop"}, {"description": "Chair"}],
    )
    assert [item["description"] for item in positional] == ["Laptop", "Chair"]

    merged = await handler._merge_with_existing_incomplete_products(
        [{"entities": {"description": "Laptop", "quantity": 1, "date_validation_error": "old"}}],
        [{"description": "Laptop", "deliveryDate": "2026-01-01", "date_validation_error": ""},
         {"city": "Bengaluru"}],
    )
    assert merged[0]["deliveryDate"] == "2026-01-01"
    assert "date_validation_error" not in merged[0]
    assert merged[0]["city"] == "Bengaluru"

    class FakeSchema:
        def get_combined_questions(self):
            return {"mandatory": ["Delivery date?", None, "Quantity?"], "has_mandatory": True, "has_optional": False}

    monkeypatch.setattr(
        ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_args: FakeSchema()
    )
    questions, missing = await handler._generate_clarification_questions(
        [{"index": 1, "entities": {"description": "Laptop"}, "missing_fields": ["delivery_date"]},
         {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]}], 2,
    )
    assert "Delivery date?" in questions and None not in questions and "delivery_date" in missing

    assert handler._no_products_mentioned([])
    assert handler._no_products_mentioned([{"description": "NO_PRODUCTS_MENTIONED"}])
    assert handler._no_products_mentioned([{"description": "product"}])
    assert not handler._no_products_mentioned([{"description": "laptop"}])

    handler._categorize_products_by_completeness = AsyncMock(side_effect=RuntimeError("schema"))
    result = await handler.handle_products_array(make_user(), make_session(), "x", [{"description": "Laptop"}])
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_purchase_intent_summary_sectioned_error_and_special_item_paths(monkeypatch):
    entity = AsyncMock()
    summaries = AsyncMock()
    products = AsyncMock()
    handler = PurchaseIntentHandler(AsyncMock(), AsyncMock(), entity, summaries, products, AsyncMock())
    user = make_user()
    session = make_session()

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "is_sectioned_rfq_active", lambda *_: False)
    summaries.load_user_context.return_value = ["summary"]
    entity.extract_entities_with_summary_context.return_value = {"products": [{"description": "Laptop"}]}
    products.handle_products_array.return_value = {"status": "summary-products"}
    result = await handler.handle_purchase_intent(
        user, session, {"text": {"body": "laptop"}},
        should_use_summary_aware_extraction_func=lambda _: True,
    )
    assert result["status"] == "summary-products"
    entity.extract_entities_with_summary_context.assert_awaited_once()

    # Quantity violations exercise both one-item and many-item formatting.
    result = await handler._handle_quantity_limit_violations(
        user, make_session(), {"quantity_violations": [{"description": "Laptop", "quantity": 10000001}]}
    )
    assert result["status"] == "quantity_limit_violation"
    result = await handler._handle_quantity_limit_violations(
        user, make_session(), {"quantity_violations": [
            {"description": "Laptop", "quantity": 10000001}, {"description": "Chair", "quantity": 10000002}
        ]}
    )
    assert len(result["violations"]) == 2

    class FakeCancel:
        def __init__(self, *args, **kwargs):
            self.clear = AsyncMock()
            self.send = AsyncMock()

        async def _clear_workflow_state(self, session):
            await self.clear(session)

        async def _send_cancellation_message(self, phone, role):
            await self.send(phone, role)

    fake_cancel = FakeCancel()
    monkeypatch.setattr("app.services.cancel_service.CancelService", lambda *args, **kwargs: fake_cancel)
    result = await handler._handle_non_procurable_items(
        user, make_session(), {"non_procurable_items": ["service"]}
    )
    assert result["status"] == "non_procurable_cancelled"
    result = await handler._handle_non_procurable_items(
        user, make_session(), {"non_procurable_items": ["service", "advice"]}
    )
    assert result["status"] == "non_procurable_cancelled"

    modification_session = make_session(workflow_type="modification_request")
    handler.session_manager = AsyncMock()
    entity_result = {"modification_intent_detected": True, "requires_clarification": True}
    result = await handler._handle_modification_clarification(user, modification_session, "change", entity_result, [])
    assert result["status"] == "modification_clarification_sent"

    entity.extract_entities.side_effect = RuntimeError("extract")
    result = await handler.handle_purchase_intent(user, make_session(), "x")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_sectioned_validation_restart_excel_and_attachment_edges(monkeypatch):
    entity = AsyncMock()
    entity.openai_service = AsyncMock()
    handler = SectionedRFQCreationHandler(
        entity, AsyncMock(), AsyncMock(), AsyncMock(), confirmation_handler=AsyncMock()
    )
    user = make_user()

    assert (await handler._autofill_location_from_pincode({"pincode": ""}))["is_valid"] is True
    invalid = await handler._autofill_location_from_pincode({"pincode": "12"})
    assert invalid["is_valid"] is False
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Bengaluru", "state": "Karnataka"}))
    valid = await handler._autofill_location_from_pincode({"pincode": "560001", "city": "Wrong", "state": "Wrong"})
    assert valid["delivery_data"]["city"] == "Bengaluru"
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    assert (await handler._validate_pincode_and_get_location("560001"))["is_valid"] is False
    assert (await handler._validate_pincode_and_get_location("bad"))["is_valid"] is False
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert "Error validating" in (await handler._validate_pincode_and_get_location("560001"))["error"]

    entity.openai_service.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": "2026-01-02"}
    assert (await handler._validate_delivery_date("tomorrow"))["is_valid"] is True
    entity.openai_service.validate_delivery_date.return_value = {"is_valid": False, "user_friendly_message": "bad date"}
    assert (await handler._validate_delivery_date("yesterday"))["error"] == "bad date"
    entity.openai_service.validate_delivery_date.side_effect = RuntimeError("ai")
    assert (await handler._validate_delivery_date("future"))["is_valid"] is True

    # Excel input skips the normal confirmation keyword branch and clears its source flag.
    excel_session = make_session(
        sectioned_rfq={"active": True, "current_section": "date_location"},
        excel_source=True,
        date_location={"deliveryDate": "1 Jan", "pincode": "560001", "city": "B", "state": "K"},
    )
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "date_location")
    handler._handle_date_location_section = AsyncMock(return_value={"status": "date"})
    result = await handler.handle_sectioned_rfq(user, excel_session, {"text": {"body": "confirm"}})
    assert result["status"] == "date"
    assert excel_session.workflow_state["excel_source"] is False

    # Restart responses cover unclear, decline, and confirmed reset states.
    restart_session = make_session(sectioned_rfq_pending_restart=True, sectioned_rfq={"current_section": "items"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda *_: True)
    assert (await handler.handle_sectioned_rfq(user, restart_session, "maybe"))["status"] == "awaiting_restart_confirmation"
    assert (await handler.handle_sectioned_rfq(user, restart_session, "no"))["status"] == "restart_declined"
    restart_session.workflow_state["sectioned_rfq_pending_restart"] = True
    assert (await handler.handle_sectioned_rfq(user, restart_session, "yes"))["status"] == "workflow_restarted"

    handler.confirmation_handler.handle_optional_fields_response.return_value = {"status": "optional"}
    attachment_session = make_session(
        sectioned_rfq={"current_section": "attachments"},
        sectioned_rfq_attachment_question_asked=True,
    )
    result = await handler._handle_attachments_section(user, attachment_session, "skip", [])
    assert result["status"] == "optional"


@pytest.mark.asyncio
async def test_sectioned_delivery_and_items_validation_matrix(monkeypatch):
    handler = SectionedRFQCreationHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    user = make_user()
    session = make_session()
    handler.session_manager = AsyncMock()

    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_delivery_format", lambda _: {
        "deliveryDate": "2026-01-02", "pincode": "560001", "additional_text": ""
    })
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": False, "error": "bad date"})
    handler._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": False, "error": "bad pin"})
    result = await handler._process_delivery_modification_direct(user, session, "Delivery Date: bad\nDelivery Pincode: bad")
    assert result["status"] == "validation_error"

    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "2 January 2026"})
    handler._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": True, "city": "B", "state": "K"})
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmed"})
    result = await handler._process_delivery_modification_direct(user, session, "Delivery Date: 2026\nDelivery Pincode: 560001")
    assert result["status"] == "confirmed"

    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {
        "error": "bad items", "additional_text": ""
    })
    result = await handler._process_items_modification_direct(user, session, "Item bad")
    assert result["status"] == "format_error"

    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {
        "items": [{"description": "Laptop", "quantity": 1}], "additional_text": ""
    })
    handler._display_items_confirmation = AsyncMock(return_value={"status": "items-confirm"})
    result = await handler._process_items_modification_direct(user, session, "Item 1 Qty: 1")
    assert result["status"] == "items-confirm"

    assert handler._get_next_section("date_location") == "items"
    assert handler._get_next_section("final_confirmation") is None
    assert handler._get_next_section("unknown") is None
    assert handler._is_delivery_complete({"deliveryDate": "x", "pincode": "1", "city": "B", "state": "K"})
    assert not handler._is_delivery_complete({"deliveryDate": "x", "pincode": "1", "city": "", "state": "K"})


# ---------------------------------------------------------------------------
# Low-coverage helper edges with all I/O seams mocked
# ---------------------------------------------------------------------------


def test_attachment_and_excel_helper_exception_boundaries():
    assert AttachmentHelpers.validate_attachment_type(
        "drawing.dwg", "application/octet-stream"
    )["valid"]
    assert AttachmentHelpers.validate_attachment_type("drawing.exe", "text/plain")["valid"] is False
    broken = make_session(extracted_entities=[BrokenMapping()])
    summary = AttachmentHelpers.get_attachment_summary(broken)
    assert summary["total_count"] == 0

    assert ExcelHelpers.convert_excel_to_entities([BrokenMapping()]) == []
    assert ExcelHelpers.calculate_excel_completeness([BrokenMapping()]) == 0
    with pytest.raises(RuntimeError, match="broken mapping"):
        ExcelHelpers.generate_excel_summary(BrokenMapping())
    assert ExcelHelpers.identify_missing_fields([BrokenMapping()]) == []


def test_authentication_chat_and_response_helper_fallbacks(monkeypatch):
    assert AuthenticationHelpers.extract_user_details([BrokenMapping()]) is None
    assert AuthenticationHelpers.format_email_list(BrokenMapping()) == ""
    assert not AuthenticationHelpers.validate_otp_format(None)
    assert AuthenticationHelpers.extract_emails_from_user_data(BrokenMapping()) == []

    assert ChatServiceHelpers.serialize_products_for_session({"when": datetime(2025, 1, 1)})["when"].startswith("2025-01-01")
    assert ChatServiceHelpers.transform_entities_to_schema({"deliveryDate": "not-a-date"}) == {}

    helper = ResponseHelpers.__new__(ResponseHelpers)
    helper.settings = SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal", support_email="support@x", support_contact_info="help", rfq_max_allowed=2)
    helper.openai_service = SimpleNamespace(
        default_model="model",
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("openai")))),
        _load_prompt=MagicMock(return_value="prompt"),
    )
    import asyncio
    assert asyncio.run(helper._generate_common_seller_response("state", "seller", {}, "fallback")) == "fallback"
    assert helper._get_email_status_fallback({"successful_emails": 0, "total_requested": 0}).startswith("✅")


@pytest.mark.asyncio
async def test_session_summarization_and_processing_helper_failure_fallbacks(monkeypatch):
    session = make_session()
    renewed = await SessionHelpers.renew_session_activity(session)
    assert renewed.last_activity_at is not None and "last_activity_at" in renewed.workflow_state
    assert SessionHelpers.clean_for_json_serialization({"value": object()})["value"] is not None
    assert SessionHelpers.should_use_summary_aware_extraction("use prior context") is False
    chat = AsyncMock()
    daily = AsyncMock()
    chat.generate_session_summary.side_effect = RuntimeError("summary")
    await SummarizationHelpers.handle_session_completion_async(
        chat, daily, {"session_id": "s", "user_id": "u", "rfq_ids": []}
    )
    assert daily.generate_daily_summary.await_count == 0
    auto = MagicMock()
    auto.categorize_item.side_effect = RuntimeError("categorizer")
    seller = AsyncMock()
    seller.select_sellers_for_rfq.side_effect = RuntimeError("seller service")
