"""Residual deterministic coverage for handlers and helper orchestration paths."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.handlers.auth_registration_intent_switch as switch_mod
import app.services.handlers.authentication_orchestrator as auth_mod
import app.services.handlers.bfs_search_handler as bfs_mod
import app.services.handlers.confirmation_handler as confirmation_mod
import app.services.handlers.format_modification_handler as format_mod
import app.services.handlers.intent_switch_handler as intent_mod
import app.services.handlers.products_array_handler as products_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.handlers.seller_rfq_interest_handler as interest_mod
import app.services.helpers.authentication_helpers as authentication_helpers_mod
import app.services.helpers.chat_service_helpers as chat_helpers_mod
import app.services.helpers.support_helpers as support_mod
from app.models import ConversationOutcome, WorkflowType
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.purchase_workflow_handler import PurchaseWorkflowHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.excel_confirmation_helpers import ExcelConfirmationHelpers
from app.services.helpers.support_helpers import SupportHelpers


def session(**state):
    return SimpleNamespace(
        session_id="sid", external_user_id="uid", phone_number="+911234",
        workflow_type=None, workflow_state=dict(state), conversation_history={
            "metadata": [], "messages": []
        }, product_items=[], extracted_entities={}, outcome=None,
        completed_at=None, rfq_ids=[], user_type=None, user_type_value=None,
    )


def user(role="buyer"):
    return SimpleNamespace(id="u1", org_id="o1", phone_number="+911234",
                           role=role, is_registered=True, self_client=role == "buyer")


def bare(cls, **attrs):
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


# Pure helper residuals -----------------------------------------------------

def test_authentication_and_excel_helpers_remaining_shapes(monkeypatch):
    assert AuthenticationHelpers.extract_user_details([{"id": 1}]) == {"id": 1}
    assert AuthenticationHelpers.extract_user_details(None) is None
    assert AuthenticationHelpers.extract_user_details("bad") is None
    assert AuthenticationHelpers.format_email_list([]) == ""
    assert AuthenticationHelpers.validate_otp_format(" 1234 ")
    assert not AuthenticationHelpers.validate_otp_format(None)
    assert AuthenticationHelpers.extract_emails_from_user_data({"email": ["a@x", "", "bad"]}) == ["a@x"]
    assert AuthenticationHelpers.extract_emails_from_user_data({"email": 42}) == []

    monkeypatch.setattr(authentication_helpers_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    validated, error = __import__("asyncio").run(
        AuthenticationHelpers.validate_entities({"email": "bad", "zipCode": "bad", "gstin": "short"}, object)
    )
    assert validated["email"] == "bad" and "valid email" in error

    missing = ExcelConfirmationHelpers.generate_confirmation_message(
        {"total_rows": 3, "identified_for_rfq": 2, "extracted": 1, "skipped": 2,
         "has_missing_items": True, "skipped_items_summary": "row 2"}, "items.xlsx"
    )
    complete = ExcelConfirmationHelpers.generate_confirmation_message(
        {"total_rows": 1, "identified_for_rfq": 1, "extracted": 1}, "ok.xlsx"
    )
    assert "Missing quantity" in missing and "row 2" in missing
    assert "successfully" in complete

    s = SimpleNamespace(workflow_state=None)
    ExcelConfirmationHelpers.save_excel_confirmation_data(s, {"rfqs": [], "items": [{"ItemDescription": "x"}], "filename": "x"})
    assert ExcelConfirmationHelpers.get_excel_confirmation_data(s)["ready_for_multiple_rfq_creation"]
    assert ExcelConfirmationHelpers.prepare_multiple_rfq_data({"items": [{"ItemDescription": "x", "Quantity": 2}]})[0]["products"][0]["quantity"] == 2
    assert ExcelConfirmationHelpers.prepare_multiple_rfq_data({}) == []
    ExcelConfirmationHelpers.clear_excel_confirmation_data(s)
    assert ExcelConfirmationHelpers.get_excel_confirmation_data(s) == {}
    assert "Created: 2 RFQs" in ExcelConfirmationHelpers.format_rfq_creation_summary([
        {"products": [{"description": "x", "quantity": 1}]}, {"products": []}
    ])


def test_chat_helper_date_serialization_and_context_fallback(monkeypatch):
    now = datetime(2025, 1, 2)
    serialized = ChatServiceHelpers.serialize_products_for_session({"when": now, "items": [now]})
    assert serialized == {"when": now.isoformat(), "items": [now.isoformat()]}
    assert ChatServiceHelpers.build_context("collecting", "hello", completeness=90, extra=True)["extra"]
    transformed = ChatServiceHelpers.transform_entities_to_schema({"deliveryDate": now, "description": "x", "quantity": 2})
    assert transformed["delivery_date"] == now and transformed["items"][0]["quantity"] == 2
    assert ChatServiceHelpers.transform_entities_to_schema({"deliveryDate": "not-a-date"}) == {}

    s = session()
    s.conversation_history = {"messages": [{"sender": "user", "intent": "general_inquiry", "content": "old"}]}
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(s, "current") == "current"
    s.workflow_state["original_message"] = "original"
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(s, "current") == "original"
    s.conversation_history["messages"].append({"sender": "user", "intent": "sell_something", "content": "sell chairs"})
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(s, "current") == "sell chairs"

    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(chat_helpers_mod, "logger", MagicMock())
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    s.external_user_id = "uid"
    context = __import__("asyncio").run(ChatServiceHelpers.build_conversation_context(s, "hello"))
    assert context["user_role"] is None and context["session_status"]["has_incomplete_products"] is False


def test_support_helper_current_checkout_paths(tmp_path, monkeypatch):
    openai = MagicMock()
    email = SimpleNamespace(send_email=AsyncMock(return_value={"status": "Success"}))
    helper = SupportHelpers(openai_service=openai, email_service=email)
    helper.templates_dir = tmp_path
    assert helper.load_template("missing") is None
    (tmp_path / "bad.json").write_text("{bad", encoding="utf-8")
    assert helper.load_template("bad") is None
    with pytest.raises(AttributeError):
        helper.integrate_template_and_data({"subject": "Hi {name}"}, {"name": "A"})
    assert __import__("asyncio").run(helper.send_support_email("missing", {}))["success"] is False
    helper.load_template = MagicMock(return_value={"subject": "x"})
    helper.integrate_template_and_data = MagicMock(side_effect=RuntimeError("format"))
    result = __import__("asyncio").run(helper.send_support_email("x", {}))
    assert result["success"] is False and "format" in result["error"]




# Authentication and product orchestration --------------------------------
@pytest.mark.asyncio
async def test_auth_switch_constructor_and_all_choice_paths(monkeypatch):
    fake_openai = MagicMock()
    monkeypatch.setattr(switch_mod, "OpenAIService", lambda: fake_openai)
    monkeypatch.setattr(switch_mod, "get_user_cache_service", lambda: MagicMock())
    h = AuthRegistrationIntentSwitch(AsyncMock())
    assert h.openai_service is fake_openai
    assert switch_mod._get_role_string(SimpleNamespace(role=SimpleNamespace(value="seller"))) == "seller"
    assert switch_mod._get_role_string(SimpleNamespace(role="buyer")) == "buyer"
    s = session(user_type="buyer"); s.workflow_type = "authentication"
    assert await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    assert not await h.should_handle_auth_reg_switch(session(), "sell_something", "seller")
    s.workflow_state["pending_auth_reg_switch"] = {"x": 1}
    assert not await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    assert h._get_target_combination("register_x", "seller") == "registration_seller"
    assert h._get_current_combination_desc(s) == "buyer login"
    assert h._get_new_combination_desc("other", "seller") == "seller process"
    assert h._get_target_workflow("register_buyer") == "registration"

    for choice, expected in [("1", "continue_current_workflow"), ("2", "switch_to_new_combination"),
                             ("exit", "exit_requested"), ("?", "clarification_requested")]:
        state = session(pending_auth_reg_switch={"new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"})
        state.workflow_type = WorkflowType.authentication
        result = await h.handle_auth_reg_switch_response("+1", state, choice)
        assert result["status"] == expected
    fake_openai.generate_response.return_value = "switch_to_new"
    assert await h._analyze_switch_choice("perhaps") == "switch_to_new"
    fake_openai.generate_response.side_effect = RuntimeError("ai")
    assert await h._analyze_switch_choice("perhaps") == "unclear"

    u = user("buyer")
    assert (await h.handle_account_change_confirmation(u, session(), "x", "seller", "sell"))["status"] == "account_change_confirmation_requested"
    u.is_registered = False
    assert (await h.handle_account_change_confirmation(u, session(), "x", "seller", "sell"))["status"] == "account_change_confirmation_requested"
    assert (await h.handle_role_switch_confirmation(user(), session(), "x", "seller"))["status"] == "role_switch_confirmation_requested"
    assert (await h._handle_role_switch_fallback(user(), session(), "seller", "buyer"))["status"] == "role_switch_confirmation_requested"


@pytest.mark.asyncio
async def test_authentication_orchestrator_residual_flow_matrix(monkeypatch):
    wa, auth, registration, support = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    auth.filter_users_by_intent = MagicMock()
    h = bare(AuthenticationOrchestrator, whatsapp_service=wa, response_helpers=AsyncMock(),
             authentication_service=auth, registration_service=registration,
             intent_service=AsyncMock(), support_service=support, chat_service=None,
             auth_reg_switch=AsyncMock(), profile_selection_service=AsyncMock())
    auth.validate_token.return_value = {"verification_required": True}
    h.profile_selection_service.handle_profile_selection.return_value = {"status": "profile"}
    assert (await h.authentication_orchestrator_flow("1", "buy", session(), {"intent": "buy_something", "confidence": 90}))['status'] == "profile"
    auth.validate_token.return_value = {"is_registered": True, "id": "u", "name": "A", "email": "a@x"}
    assert (await h.authentication_orchestrator_flow("1", "buy", session(), {"intent": "buy_something", "confidence": 90})).id == "u"
    auth.validate_token.return_value = None
    s = session(role_switch_in_progress=True, user_type="seller")
    auth.user_authenticate.return_value = {"success": True, "response": [{"email": "s@x"}]}
    auth.filter_users_by_intent.return_value = {"success": True, "filtered_users": [{}], "unique_emails": ["s@x"]}
    auth.initiate_email_confirmation.return_value = {"status": "email"}
    assert (await h.authentication_orchestrator_flow("1", "sell", s, {"intent": "sell_something", "confidence": 90}))['status'] == "email"
    auth.filter_users_by_intent.return_value = {"success": False}
    h._redirect_to_registration_flow = AsyncMock(return_value={"status": "registration"})
    assert (await h.authentication_orchestrator_flow("1", "sell", session(role_switch_in_progress=True, user_type="seller"), {"intent": "sell_something", "confidence": 90}))['status'] == "registration"
    h._handle_auth_fallback = AsyncMock(return_value={"status": "fallback"})
    assert (await h.authentication_orchestrator_flow("1", "x", session(), None))['status'] == "fallback"
    h._handle_auth_fallback = AuthenticationOrchestrator._handle_auth_fallback.__get__(h)
    assert (await h._handle_auth_fallback("1", "x"))['status'] == "fallback_handled"

    auth.user_authenticate.return_value = {"success": False}
    assert (await h._start_authentication_flow("1", "x", session(), {"intent": "sell_something", "confidence": 90}))['status'] == "registration"
    assert (await h._start_authentication_flow("1", "x", session(), {"intent": "bad", "confidence": 10}))['status'] == "clarification_sent"
    h._redirect_to_registration_flow = AsyncMock(return_value={"status": "registration"})
    assert (await h._start_authentication_flow("1", "x", session(), {"intent": "ambiguous", "confidence": 90}))['status'] == "registration"
    assert not await h._should_handle_intent_switch_during_auth("buy_something", 50, "email_confirmation")
    assert await h._should_handle_intent_switch_during_auth("buy_something", 90, "email_confirmation", session())
    assert await h._should_handle_intent_switch_during_registration("buy_something", 90, "seller")
    assert not await h._should_handle_intent_switch_during_registration("buy_something", 50, "seller")
    assert (await h._handle_auth_general_inquiry("1", "x"))["status"] == "general_inquiry_handled"


@pytest.mark.asyncio
async def test_products_array_residual_merge_tracking_and_confirmation(monkeypatch):
    h = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())
    h.session_manager.save_session = AsyncMock()
    s = session(incomplete_products=[{"entities": {"description": "old"}}],
                complete_products=[{"entities": {"description": "done"}}], attachment_caption="urgent")
    h._merge_with_existing_incomplete_products = AsyncMock(return_value=[{"description": "new"}])
    h._track_product_categories = AsyncMock()
    h._categorize_products_by_completeness = AsyncMock(return_value=([], [{"index": 1, "entities": {"description": "done"}}]))
    h._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    assert (await h.handle_products_array(user(), s, "x", [{"description": "new"}]))["status"] == "complete"
    assert s.workflow_state["incomplete_products"] and s.workflow_state.get("attachment_caption") is None

    s = session()
    h._track_product_categories = ProductsArrayHandler._track_product_categories.__get__(h)
    await h._track_product_categories(s, [{"category": "IT", "description": "laptop"}, {"description": "chair"}, {}, "bad"])
    assert len(s.product_items) == 2
    s.product_items = None
    await h._track_product_categories(s, [{"category": "Office"}])
    assert s.product_items[0]["category"] == "Office"
    assert h._no_products_mentioned([{"description": "item"}])
    assert not h._no_products_mentioned([{"description": "", "quantity": 2}])

    schema = MagicMock()
    schema.get_combined_questions.return_value = {"mandatory": [], "optional": [], "has_mandatory": False, "has_optional": False}
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: schema)
    questions, missing = await h._generate_clarification_questions([
        {"index": 1, "entities": {"description": "x"}, "missing_fields": ["delivery_date"]},
        {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]},
    ], 2)
    assert questions == [] and missing == []

    combined = MagicMock(); combined.get_optional_questions.return_value = ["brand?"]; combined.model_dump.return_value = {}
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_combined_rfq_schema_from_multiple_products", lambda *_: combined)
    h.whatsapp_service.send_configurable_buttons = AsyncMock()
    result = await h._handle_multiple_complete_products(user(), session(), "x", [
        {"index": 1, "entities": {"description": "x"}}, {"index": 2, "entities": {"description": "y"}}
    ], [])
    assert result["status"] == "optional_fields_inquiry"
    combined.get_optional_questions.return_value = []
    h.response_helpers.generate_rfq_summary_and_confirmation = AsyncMock(return_value="summary")
    result = await h._handle_multiple_complete_products(user(), session(), "x", [
        {"index": 1, "entities": {"description": "x"}}, {"index": 2, "entities": {"description": "y"}}
    ], [])
    assert result["status"] == "combined_rfq_confirmation"


# Smaller handler state machines -------------------------------------------
@pytest.mark.asyncio
async def test_format_and_intent_switch_residual_branches(monkeypatch):
    h = FormatModificationHandler(AsyncMock()); h.session_manager = AsyncMock()
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "items"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _: 0)
    assert (await h.handle_format_modification("x", session(), user()))["status"] == "format_error"
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_a, **_k: 3)
    assert (await h._handle_format_error(session(), user(), "bad", "items"))["status"] == "max_retries_reached"
    s = session(incomplete_products=[1], complete_products=[2], delivery_details={"delivery_date": "d"})
    monkeypatch.setattr(format_mod.WorkflowManager, "get_delivery_details", lambda _: {"delivery_date": "d", "pincode": "p", "city": "c", "state": "s"})
    await h._replace_products_array(s, [{"description": "x"}])
    assert s.workflow_state["extracted_entities"][0]["city"] == "c"

    ih = bare(IntentSwitchHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), openai_service=AsyncMock())
    active = session(extracted_entities=[{"product_name": "Laptop"}]); active.workflow_type = SimpleNamespace(value="seller_rfq_view")
    assert await ih.should_handle_intent_switch(active, "buy_something", 100)
    active.workflow_state["pending_intent_switch"] = "corrupt"
    assert (await ih.handle_intent_switch_response(user(), active, "x"))["status"] == "corrupted_intent_switch_data"
    ih.response_helpers.generate_contextual_response.return_value = "choice"
    active.workflow_state = {"extracted_entities": [{"product_name": "Laptop"}]}
    assert (await ih.handle_intent_switch_choice(user(), active, "sell", "sell_something", {"confidence": 100}))["status"] == "intent_switch_choice_presented"
    ih.openai_service.analyze_intent_switch_response.side_effect = RuntimeError("ai")
    active.workflow_state["pending_intent_switch"] = {"new_intent": "sell_something", "new_intent_message": "sell", "intent_result": {}}
    assert (await ih.handle_intent_switch_response(user(), active, "continue current"))["status"] == "continue_current_workflow"
    assert ih._get_workflow_description(active) == "Laptop request"
    ih._abandon_current_workflow(active)
    assert active.outcome == ConversationOutcome.abandoned and active.workflow_type is None


@pytest.mark.asyncio
async def test_purchase_handlers_route_all_direct_paths(monkeypatch):
    entity, summaries, products, wa, manager = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    h = PurchaseIntentHandler(wa, AsyncMock(), entity, summaries, products, manager)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    summaries.load_user_context.return_value = [{"summary": "old"}]
    entity.extract_entities.return_value = {"products": [{"description": "x"}]}
    entity.extract_entities_with_summary_context.return_value = {"products": [{"description": "x"}]}
    products.handle_products_array.return_value = {"status": "products"}
    assert (await h.handle_purchase_intent(user(), session(session_archive={"old": 1}), {"text": {"body": "buy"}}, {"intent": "buy_something"}, lambda _: True))["status"] == "products"
    entity.extract_entities.return_value = {"quantity_limit": True, "error_type": "quantity_limit", "quantity_violations": [{"description": "x", "quantity": 100000001}, {"description": "y", "quantity": 100000002}]}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "quantity_limit_violation"
    entity.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await h.handle_purchase_intent(user(), session(), "change"))["status"] == "modification_clarification_sent"
    entity.extract_entities.return_value = {"entities": {"description": "x"}}
    products.handle_products_array.return_value = {"status": "single"}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "single"
    entity.extract_entities.return_value = {}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "no_new_entities"
    assert (await h._handle_single_product_entities(user(), session(workflow_type="modification_request"), "x", {}, []))["status"] == "modification_request_no_data"

    workflow = PurchaseWorkflowHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    workflow.entity_service.extract_entities = AsyncMock(return_value={})
    assert (await workflow.handle_purchase_intent(user(), session(), "x"))["status"] == "no_new_entities"
    workflow._handle_products_array = AsyncMock(return_value={"status": "products"})
    workflow.entity_service.extract_entities.return_value = {"products": [{"description": "x"}]}
    assert (await workflow.handle_purchase_intent(user(), session(), "x"))["status"] == "products"
    assert await PurchaseWorkflowHandler._handle_products_array(workflow, user(), session(), "x", []) is None
    assert await PurchaseWorkflowHandler._handle_single_entity(workflow, user(), session(), "x", {}) is None


# BFS, confirmation, seller, and sectioned residuals -----------------------
@pytest.mark.asyncio
async def test_bfs_search_buttons_bid_submission_and_error_paths(monkeypatch):
    h = bare(BFSSearchHandler, whatsapp_service=AsyncMock(), session_manager=AsyncMock(),
             openai_service=AsyncMock(), auto_categorization_service=AsyncMock(), bfs_api_service=AsyncMock())
    s = session(bfs_results=[{"id": "i", "description": "x", "sellPrice": 5}])
    h._clear_bid_state = AsyncMock()
    assert (await h.handle_button(user(), s, "search_bfs"))["status"] == "bfs_awaiting_product_description"
    assert (await h.handle_button(user(), s, "bfs_raise_rfq"))["status"] == "bfs_activate_rfq"
    assert (await h.handle_button(user(), s, "bfs_bid_cancel"))["status"] == "bfs_send_cancel_message"
    h._extract_entities = AsyncMock(return_value=[])
    assert (await h.handle_bfs_search(user(), session(), "?"))["status"] == "no_products_found"
    h._extract_entities = BFSSearchHandler._extract_entities.__get__(h)
    h.openai_service.extract_entities.return_value = {"success": False}
    assert await h._extract_entities("fallback") == [{"description": "fallback"}]
    h.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "Other"}
    assert (await h._build_api_payload([{"description": "x"}], "u", "s"))[0]["category"] == []
    await h._send_bfs_results(user(), s, [], suppress_raise_rfq_on_no_results=False)
    await h._send_bfs_results(user(), s, {"unexpected": True})

    redis = AsyncMock(); redis.retrieve.return_value = SimpleNamespace(org_id="o", id="u")
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    monkeypatch.setattr(bfs_mod, "generate_bid_summary", lambda _: "summary")
    h.bfs_api_service.request_bfs_item.side_effect = [{"success": True}, {"success": False, "error": "bad"}]
    s = session(bfs_bid_items=[
        {"price": 2, "quantity": 1, "original_item": {"id": "i", "sellPrice": 1}},
        {"price": 2, "quantity": 0, "original_item": {"id": "j"}},
        {"price": 2, "quantity": 1, "original_item": {}},
    ])
    assert (await h._submit_bids_to_api(user(), s))["status"] == "bfs_bid_submitted"
    s = session(bfs_bid_items=[{"price": 2, "quantity": 1, "original_item": {"id": "i"}}])
    h.bfs_api_service.request_bfs_item.return_value = {"success": False, "error": "bad"}
    assert (await h._submit_bids_to_api(user(), s))["status"] == "bfs_bid_api_error"
    assert (await h._submit_bids_to_api(user(), session()))["status"] == "bfs_bid_no_items"


@pytest.mark.asyncio
async def test_confirmation_and_seller_residual_paths(monkeypatch):
    h = bare(ConfirmationHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
             cancel_service=None, session_manager=None, confirmation_service=AsyncMock(), _bfs_search_handler=None)
    h._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"})
    h._handle_rfq_modification = AsyncMock(return_value={"status": "modified"})
    h._proceed_to_confirmation_from_optional = AsyncMock(return_value={"status": "continued"})
    h._handle_restart_workflow = AsyncMock(return_value={"status": "restart"})
    for button, expected in [("confirm_rfq", "accepted"), ("no_rfq", "modified"), ("continue_rfq", "continued"), ("confirm_no_changes", "accepted"), ("restart_rfq", "restart")]:
        assert (await h.handle_confirmation_button(user(), session(), button))["status"] == expected
    h.confirmation_service.parse_confirmation.return_value = "unclear"
    h._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarify"})
    assert (await h.handle_pending_confirmations(user(), session(), "hmm", {}))["status"] == "clarify"
    h.confirmation_service.parse_confirmation.return_value = "no"
    assert (await h.handle_pending_confirmations(user(), session(), "no", {}))["status"] == "modified"
    h._merge_optional_fields_and_confirm = AsyncMock(return_value={"status": "merged"})
    assert (await h.handle_optional_fields_response(user(), session(), "add specs"))["status"] == "merged"
    assert h._extract_product_descriptions_from_rfq([{"success": True, "rfq_data": {"items": [{"product_name": "Laptop"}]}}]) == ["Laptop"]
    assert h._extract_product_descriptions_from_rfq([]) == []

    mix = bare(SellerAuthMixin, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(),
               session_manager=AsyncMock(), otp_service=AsyncMock(), auth_redis_service=AsyncMock(), openai_service=AsyncMock())
    mix.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=False, org_id="seller", id="id")
    assert (await mix.check_seller_auth_state("+1", "seller"))["state"] == "correct_seller"
    assert (await mix.check_seller_auth_state("+1", "other"))["state"] == "wrong_account"
    current = SimpleNamespace(self_client=True, email="a@x", dict=lambda: {"email": "a@x"})
    assert (await mix.prompt_seller_account_switch("+1", session(), current))["status"] == "switch_prompt_sent"
    mix._parse_switch_choice = AsyncMock(return_value="stay")
    assert (await mix.handle_seller_switch_response("+1", session(target_seller_id="s"), "no"))["status"] == "switch_declined"
    mix._parse_switch_choice.return_value = "unclear"
    assert (await mix.handle_seller_switch_response("+1", session(), "?"))["status"] == "switch_response_unclear"
    mix._parse_switch_choice = SellerAuthMixin._parse_switch_choice.__get__(mix)
    mix.openai_service.generate_response.return_value = "stay"
    assert await mix._parse_switch_choice("perhaps") == "stay"
    mix.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await mix._parse_switch_choice("perhaps") == "unclear"


@pytest.mark.asyncio
async def test_seller_interest_and_sectioned_residual_paths(monkeypatch):
    wa, auth, manager = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(interest_mod, "get_auth_redis_service", lambda: AsyncMock())
    monkeypatch.setattr(interest_mod, "get_settings", lambda: SimpleNamespace(procucev_rfq_details_url="https://details"))
    h = SellerRFQInterestHandler(wa, auth, manager, AsyncMock())
    h._show_intermediate_buttons = AsyncMock(return_value={"status": "buttons"})
    h.check_seller_auth_state = AsyncMock(return_value={"state": "correct_seller"})
    assert (await h.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "buttons"
    h.check_seller_auth_state.return_value = {"state": "wrong_account", "current_user": user()}
    h.prompt_seller_account_switch = AsyncMock(return_value={"status": "switch"})
    assert (await h.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "switch"
    assert (await h.handle_check_details_click("+1", "r", "s", session()))["status"] == "check_details_sent"
    assert (await h.handle_otp_validated("+1", session()))["status"] == "error"

    sh = bare(SectionedRFQCreationHandler, entity_service=AsyncMock(), whatsapp_service=AsyncMock(),
              cancel_service=AsyncMock(), session_manager=AsyncMock(), confirmation_handler=AsyncMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda s: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda s, n, v: s.workflow_state.__setitem__(n, v))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_section_retry", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {"items": [{"description": "x", "quantity": 1}], "additional_text": ""})
    sh._display_items_confirmation = AsyncMock(return_value={"status": "items_confirmation"})
    assert (await sh._process_items_modification_direct(user(), session(), "items"))["status"] == "items_confirmation"
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {"error": "bad", "additional_text": ""})
    assert (await sh._process_items_modification_direct(user(), session(), "bad"))["status"] == "format_error"
    sh._cancel_after_max_retries = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    assert (await sh._process_items_modification_direct(user(), session(), "bad"))["status"] == "cancelled"
    sh._display_item_limit_exceeded = AsyncMock(return_value={"status": "limited"})
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_items_format", lambda _: {"items": [{"description": str(i)} for i in range(6)]})
    assert (await sh._process_items_modification_direct(user(), session(), "many"))["status"] == "limited"
    assert sectioned_mod._format_date_for_display("bad") == "bad"
    assert sh._get_next_section("attachments") == "final_confirmation"
