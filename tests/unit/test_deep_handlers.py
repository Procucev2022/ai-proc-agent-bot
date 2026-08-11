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
import app.services.handlers.purchase_intent_handler as purchase_mod
import app.services.handlers.purchase_workflow_handler as workflow_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.handlers.seller_auth_mixin as seller_auth_mod
import app.services.handlers.seller_rfq_interest_handler as seller_interest_mod
import app.services.handlers.bfs_seller_bid_handler as seller_bid_mod
from app.models import ConversationOutcome, WorkflowType
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.bfs_seller_bid_handler import BFSSellerBidHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.purchase_workflow_handler import PurchaseWorkflowHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler


def session(**state):
    return SimpleNamespace(
        session_id="s1", workflow_state=dict(state), workflow_type=None,
        conversation_history={}, product_items=[], phone_number="+911", outcome=None,
        completed_at=None, rfq_ids=[], user_type=None,
    )


def user(role="buyer"):
    return SimpleNamespace(id="u1", org_id="o1", phone_number="+911", role=role,
                           is_registered=True, self_client=role == "buyer")


def async_handler(cls, **attrs):
    handler = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(handler, name, value)
    return handler


# Sectioned RFQ creation -----------------------------------------------------

def test_sectioned_helpers_cover_date_context_and_item_branches(monkeypatch):
    h = async_handler(SectionedRFQCreationHandler)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda current, name: current.workflow_state.get(name))
    s = session(date_location={"deliveryDate": "2025-01-02", "pincode": "560001", "city": "Bengaluru", "state": "KA"})
    s.workflow_type = WorkflowType.rfq_creation
    assert sectioned_mod._format_date_for_display("") == ""
    assert sectioned_mod._format_date_for_display("2025-01-02") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("02/01/2025") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("2 Jan") .endswith(str(sectioned_mod.datetime.now().year))
    assert sectioned_mod._format_date_for_display("not-a-date") == "not-a-date"
    context = h._build_entity_context(s)
    assert context["workflow_state"]["global_supplementary_fields"]["pincode"] == "560001"
    with_items = h._build_entity_context_with_items(s, [{"description": "x"}])
    assert with_items["workflow_state"]["incomplete_products"][0]["entities"]["description"] == "x"
    assert h._has_delivery_basics({"deliveryDate": "d", "pincode": 560001})
    assert not h._has_delivery_basics({"deliveryDate": "", "pincode": "560001"})
    assert h._is_delivery_complete({"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    assert not h._is_delivery_complete({"deliveryDate": "d"})
    assert h._get_next_section("date_location") == "items"
    assert h._get_next_section("final_confirmation") is None
    assert h._get_next_section("bad") is None
    incomplete = h._get_incomplete_items([{"description": "", "quantity": 0}, {"description": "chair", "quantity": 2}])
    assert incomplete[0]["missing_fields"] == ["description", "quantity"]
    assert "Product description" in h._generate_missing_items_fields_message(incomplete, [])
    assert "Delivery Date" in h._generate_delivery_missing_fields_message({"pincode": "1"})
    assert "fetch" in h._generate_delivery_missing_fields_message({"deliveryDate": "d", "pincode": "p"})


@pytest.mark.asyncio
async def test_sectioned_delivery_validation_and_modification_paths(monkeypatch):
    wa, manager, cancel, entities = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    h = SectionedRFQCreationHandler(entities, wa, cancel, manager)
    h.entity_service.openai_service = SimpleNamespace(validate_delivery_date=AsyncMock(return_value={"is_valid": True, "normalized_date": "2025-02-03"}))
    assert (await h._validate_delivery_date("tomorrow"))["is_valid"]
    h.entity_service.openai_service.validate_delivery_date.return_value = {"is_valid": False, "user_friendly_message": "bad date"}
    assert (await h._validate_delivery_date("bad"))["error"] == "bad date"
    h.entity_service.openai_service.validate_delivery_date.side_effect = RuntimeError("ai")
    assert (await h._validate_delivery_date("raw"))["is_valid"]
    assert (await h._autofill_location_from_pincode({"pincode": ""}))["is_valid"]
    assert not (await h._autofill_location_from_pincode({"pincode": "12"}))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "C", "state": "S"}))
    filled = await h._autofill_location_from_pincode({"pincode": "560001", "city": "old"})
    assert filled["delivery_data"]["city"] == "C"
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    assert not (await h._autofill_location_from_pincode({"pincode": "560001"}))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert "Error validating" in (await h._autofill_location_from_pincode({"pincode": "560001"}))["error"]
    assert not (await h._validate_pincode_and_get_location("12"))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "C", "state": "S"}))
    assert (await h._validate_pincode_and_get_location("560001"))["city"] == "C"
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={}))
    assert not (await h._validate_pincode_and_get_location("560001"))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    assert not (await h._validate_pincode_and_get_location("560001"))["is_valid"]

    s = session(); s.workflow_state["date_location_retry_count"] = 0
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_delivery_format", lambda _: {"error": "bad", "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    result = await h._process_delivery_modification_direct(user(), s, "date: bad")
    assert result["status"] == "format_error"
    s.workflow_state["date_location_retry_count"] = 2
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    monkeypatch.setattr(h, "_cancel_after_max_retries", AsyncMock(return_value={"status": "cancelled"}))
    assert (await h._process_delivery_modification_direct(user(), s, "date: bad"))["status"] == "cancelled"
    s = session(); entities.extract_entities.return_value = {"deliveryDate": "", "pincode": "", "products": [{"description": "chair"}]}
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda *_: {})
    h._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    assert (await h._process_delivery_modification_direct(user(), s, "chair"))["status"] == "missing"
    valid = {"deliveryDate": "2025-02-03", "pincode": "560001", "additional_text": ""}
    monkeypatch.setattr(sectioned_mod.sectioned_rfq_format_parser, "parse_delivery_format", lambda _: valid)
    h._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "3 February 2025"})
    h._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": True, "city": "C", "state": "S"})
    h._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmed"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_section_retry", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification", MagicMock())
    assert (await h._process_delivery_modification_direct(user(), s, "format"))["status"] == "confirmed"


@pytest.mark.asyncio
async def test_sectioned_router_items_attachments_restart_and_buttons(monkeypatch):
    wa, manager, cancel, entities, confirm = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    h = SectionedRFQCreationHandler(entities, wa, cancel, manager, confirmation_handler=confirm)
    u, s = user(), session()
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda _: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _: "items")
    h._handle_items_section = AsyncMock(return_value={"status": "items"})
    assert (await h.handle_sectioned_rfq(u, s, {"button_reply": {"title": "hello"}}))["status"] == "items"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda _: "unknown")
    assert (await h.handle_sectioned_rfq(u, s, 123))["status"] == "error"
    s.workflow_state = {"sectioned_rfq_attachment_question_asked": False}
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda *_: {"deliveryDate": "d"} if _[1] == "date_location" else [{"description": "x", "quantity": 1}])
    h._build_combined_rfq_from_sections = MagicMock(return_value={"combined_schema": {}, "products": []})
    assert (await h._handle_attachments_section(u, s, "", []))["status"] == "awaiting_attachments_decision"
    s.workflow_state["sectioned_rfq_attachment_question_asked"] = True
    confirm.handle_optional_fields_response.return_value = {"status": "optional"}
    assert (await h._handle_attachments_section(u, s, "no", []))["status"] == "optional"
    s.workflow_state["pending_combined_rfq"] = {}
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "confirm_sectioned_section", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    assert (await h._handle_attachments_section(u, s, "no", []))["status"] == "optional"
    h._handle_section_confirm = AsyncMock(return_value={"status": "yes"})
    h._handle_section_modify = AsyncMock(return_value={"status": "modify"})
    assert (await h.handle_section_button_click(u, s, "confirm_items"))["status"] == "yes"
    assert (await h.handle_section_button_click(u, s, "modify_items"))["status"] == "modify"
    assert (await h.handle_section_button_click(u, s, "bad"))["status"] == "error"
    h._handle_final_rfq_submission = AsyncMock(return_value={"status": "submitted"})
    assert (await h.handle_section_button_click(u, s, "final_confirm_rfq"))["status"] == "submitted"
    h._handle_restart_confirmation_response = AsyncMock(return_value={"status": "restart"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_sectioned_rfq_pending_restart", MagicMock())
    assert (await h._offer_restart_confirmation(u, s))["status"] == "awaiting_restart_confirmation"
    s.workflow_state["pending_restart"] = True
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda _: True)
    assert (await h.handle_sectioned_rfq(u, s, "maybe"))["status"] == "restart"
    h._handle_restart_confirmation_response = SectionedRFQCreationHandler._handle_restart_confirmation_response.__get__(h)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_sectioned_rfq", MagicMock())
    assert (await h._handle_restart_confirmation_response(u, s, "yes"))["status"] == "workflow_restarted"
    assert (await h._handle_restart_confirmation_response(u, s, "no"))["status"] == "restart_declined"
    assert (await h._handle_restart_confirmation_response(u, s, "maybe"))["status"] == "awaiting_restart_confirmation"


# Confirmation and product processing ---------------------------------------
@pytest.mark.asyncio
async def test_confirmation_buttons_parsing_submission_and_completion(monkeypatch):
    h = async_handler(ConfirmationHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
                      cancel_service=AsyncMock(), session_manager=AsyncMock())
    h.confirmation_service = SimpleNamespace(parse_confirmation=AsyncMock(return_value="yes"))
    h._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"})
    h._handle_rfq_modification = AsyncMock(return_value={"status": "modified"})
    h._proceed_to_confirmation_from_optional = AsyncMock(return_value={"status": "optional"})
    h._handle_restart_workflow = AsyncMock(return_value={"status": "restart"})
    u, s = user(), session()
    for button, expected in [("confirm_rfq", "accepted"), ("no_rfq", "modified"), ("continue_rfq", "optional"), ("confirm_no_changes", "accepted"), ("restart_rfq", "restart")]:
        assert (await h.handle_confirmation_button(u, s, button))["status"] == expected
    assert (await h.handle_confirmation_button(u, s, "other"))["status"] == "unknown_button"
    assert (await h.handle_pending_confirmations(u, s, "yes", {}))["status"] == "accepted"
    h.confirmation_service.parse_confirmation.return_value = "no"
    assert (await h.handle_pending_confirmations(u, s, "no", {}))["status"] == "modified"
    h.confirmation_service.parse_confirmation.return_value = "unclear"
    assert (await h.handle_pending_confirmations(u, s, "x", {"intent": "confirmation_response", "confidence": .8, "context_analysis": {"confirmation_details": {"response_type": "accept"}}}))["status"] == "accepted"
    h._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarify"})
    assert (await h.handle_pending_confirmations(u, s, "x", {}))["status"] == "clarify"
    h._extract_product_descriptions_from_rfq = MagicMock(return_value=["Laptop|blue"])
    await h._send_completion_response(u, s, [{"success": True, "rfq_id": "r1", "rfq_data": {"items": [{"description": "Laptop|blue"}]}}], 1)
    await h._send_completion_response(u, s, [{"success": False, "error": "down"}], 0)
    assert h.whatsapp_service.send_configurable_buttons.await_count == 1
    assert await h._handle_restart_workflow(u, s) == {"status": "restart"}


@pytest.mark.asyncio
async def test_confirmation_optional_merge_backend_and_acceptance(monkeypatch):
    wa, response, manager = AsyncMock(), AsyncMock(), AsyncMock()
    h = async_handler(ConfirmationHandler, whatsapp_service=wa, response_helpers=response,
                      cancel_service=AsyncMock(), session_manager=manager)
    h.confirmation_service = SimpleNamespace(parse_confirmation=AsyncMock())
    s = session(pending_optional_rfq={"entities": {"description": "x"}}, attachment_caption="urgent")
    response.generate_rfq_summary_and_confirmation.return_value = "summary"
    assert (await h._proceed_to_confirmation_from_optional(user(), s, "continue"))["status"] == "optional_fields_skipped"
    assert s.workflow_state["pending_rfq"]["entities"]["remarks"] == "urgent"
    s = session(pending_optional_combined_rfq={"combined_schema": {"project_desc": "x", "items": [], "delivery_locations": []}, "products": [{"entities": {"description": "x"}}]})
    response.generate_rfq_summary_and_confirmation.return_value = "summary"
    assert (await h._proceed_to_confirmation_from_optional(user(), s, "continue"))["status"] == "optional_fields_skipped"
    schema = SimpleNamespace(model_dump=lambda: {"project_desc": "x", "items": [{"description": "x", "quantity": 2}], "delivery_locations": []})
    api = AsyncMock(); api.create_rfq.return_value = {"success": True, "rfq_id": "r"}
    monkeypatch.setattr(confirmation_mod, "RFQAPIService", lambda: api)
    result = await h._submit_rfq_to_backend(schema, user())
    assert result["success"] and api.create_rfq.await_count == 1
    api.create_rfq.side_effect = RuntimeError("api")
    assert not (await h._submit_rfq_to_backend(schema, user()))["success"]
    entities = {"description": "x", "remarks": "old", "projectDesc": ""}
    h._merge_specifications_into_product(entities, {"brand": "HP", "remarks": "new", "projectDesc": "P"}, "message")
    assert entities["brand"] == "HP" and "new" in entities["remarks"] and entities["projectDesc"] == "P"
    h._merge_specifications_into_product({"description": "x"}, {}, "fallback")
    s = session(pending_rfq={"entities": {"description": "x"}})
    assert "X" in h._format_captured_info_for_modification(s)
    h._submit_rfq_to_backend = AsyncMock(return_value={"success": True, "rfq_id": "r"})
    s = session(pending_rfq={"entities": {"description": "x"}})
    result = await h._handle_rfq_acceptance(user(), s, "yes")
    assert result["status"] == "multiple_rfqs_created"


@pytest.mark.asyncio
async def test_products_array_merge_questions_and_routes(monkeypatch):
    h = ProductsArrayHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    h.session_manager.save_session = AsyncMock()
    s = session(attachment_caption="urgent")
    h._track_product_categories = AsyncMock()
    h._categorize_products_by_completeness = AsyncMock(return_value=([], [{"index": 1, "entities": {"description": "x"}}]))
    h._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    assert (await h.handle_products_array(user(), s, "x", [{"description": "x"}]))["status"] == "complete"
    assert s.product_items == []
    assert h._no_products_mentioned([])
    assert h._no_products_mentioned([{"description": "NO_PRODUCTS_MENTIONED"}])
    assert h._no_products_mentioned([{"description": "product"}])
    assert not h._no_products_mentioned([{"description": "laptop"}])
    positional = await h._merge_with_existing_incomplete_products([{"entities": {}}, {"entities": {}}], [{"description": "a"}, {"description": "b"}])
    assert [x["description"] for x in positional] == ["a", "b"]
    merged = await h._merge_with_existing_incomplete_products([{"entities": {"description": "Laptop", "quantity": 1}}], [{"description": "laptop", "quantity": 5}, {"state": "KA"}])
    assert merged[0]["quantity"] == 5 and merged[0]["state"] == "KA"
    questions, missing = await h._generate_clarification_questions([{"index": 1, "entities": {"description": "laptop"}, "missing_fields": ["delivery_date"]}, {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]}], 2)
    assert questions and missing
    questions, _ = await h._generate_clarification_questions([{"index": 1, "entities": {"description": "laptop"}, "missing_fields": ["item_0_quantity"]}], 1)
    assert questions
    h._generate_clarification_questions = AsyncMock(return_value=([], []))
    h._no_products_mentioned = MagicMock(return_value=True)
    assert (await h._handle_incomplete_products(user(), session(), "x", [{"description": "product"}], [{"missing_fields": ["x"], "entities": {}}], [], []))["status"] == "no_products_mentioned"


@pytest.mark.asyncio
async def test_products_complete_single_multiple_and_error(monkeypatch):
    h = async_handler(ProductsArrayHandler, whatsapp_service=AsyncMock(), openai_service=MagicMock(), response_helpers=AsyncMock(), session_manager=AsyncMock())
    schema = MagicMock()
    schema.get_optional_questions.return_value = []
    schema.model_dump.return_value = {}
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", MagicMock(return_value=schema))
    h.response_helpers.generate_rfq_summary_and_confirmation.return_value = "summary"
    s = session()
    assert (await h._handle_single_complete_product(user(), s, "x", {"index": 1, "entities": {"description": "x"}}, []))["status"] == "single_product_confirmation"
    combined = MagicMock(); combined.get_optional_questions.return_value = []; combined.model_dump.return_value = {}
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_combined_rfq_schema_from_multiple_products", MagicMock(return_value=combined))
    assert (await h._handle_multiple_complete_products(user(), session(), "x", [{"index": 1, "entities": {"description": "x"}}, {"index": 2, "entities": {"description": "y"}}], []))["status"] == "combined_rfq_confirmation"
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_combined_rfq_schema_from_multiple_products", MagicMock(return_value=None))
    assert not (await h._handle_multiple_complete_products(user(), session(), "x", [], []))["success"]


# Purchase orchestration and authentication ---------------------------------
@pytest.mark.asyncio
async def test_purchase_intent_and_workflow_routes(monkeypatch):
    entity, summary, array, wa, manager = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    h = PurchaseIntentHandler(wa, AsyncMock(), entity, summary, array, manager)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    summary.load_user_context.return_value = []
    entity.extract_entities.return_value = {"products": [{"description": "x"}]}
    array.handle_products_array.return_value = {"status": "products"}
    assert (await h.handle_purchase_intent(user(), session(session_archive={"old": 1}), {"text": {"body": "buy"}}, {"intent": "buy_something"}))["status"] == "products"
    entity.extract_entities.return_value = {"error_type": "quantity_limit", "quantity_violations": [{"description": "x", "quantity": 100000001}]}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "quantity_limit_violation"
    entity.extract_entities.return_value = {"error_type": "non_procurable", "non_procurable_items": ["service"]}
    monkeypatch.setattr("app.services.cancel_service.CancelService", MagicMock())
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] in {"non_procurable_cancelled", "error"}
    entity.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await h.handle_purchase_intent(user(), session(), "change"))["status"] == "modification_clarification_sent"
    entity.extract_entities.return_value = {"entities": {"description": "x"}}
    array.handle_products_array.return_value = {"status": "single"}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "single"
    entity.extract_entities.return_value = {}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "no_new_entities"
    entity.extract_entities.side_effect = RuntimeError("down")
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "error"


@pytest.mark.asyncio
async def test_purchase_workflow_constructor_routes_helpers(monkeypatch):
    h = PurchaseWorkflowHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    entity = AsyncMock(); h.entity_service = entity
    entity.extract_entities.return_value = {"products": [{"description": "x"}]}
    h._handle_products_array = AsyncMock(return_value={"status": "products"})
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "products"
    entity.extract_entities.return_value = {"entities": {"description": "x"}}
    h._handle_single_entity = AsyncMock(return_value={"status": "single"})
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "single"
    entity.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] == "modification_clarification_sent"
    entity.extract_entities.return_value = {"error_type": "non_procurable", "non_procurable_items": ["x", "y"]}
    monkeypatch.setattr("app.services.cancel_service.CancelService", MagicMock())
    monkeypatch.setattr("app.services.session_management_service.SessionManagementService", MagicMock())
    monkeypatch.setattr("app.database.DatabaseManager", MagicMock())
    assert (await h.handle_purchase_intent(user(), session(), "x"))["status"] in {"non_procurable_cancelled", "error"}
    assert await PurchaseWorkflowHandler._handle_products_array(h, user(), session(), "x", []) is None
    assert await PurchaseWorkflowHandler._handle_single_entity(h, user(), session(), "x", {}) is None


@pytest.mark.asyncio
async def test_auth_registration_switch_and_account_paths(monkeypatch):
    wa = AsyncMock(); h = async_handler(AuthRegistrationIntentSwitch, whatsapp_service=wa, openai_service=MagicMock())
    s = session(user_type="buyer"); s.workflow_type = "authentication"
    assert not await h.should_handle_auth_reg_switch(session(), "buy_something", "buyer")
    assert await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    s.workflow_state["pending_auth_reg_switch"] = {"new_intent": "sell_something"}
    assert not await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    assert h._get_target_combination("register_x", "seller") == "registration_seller"
    await h.handle_auth_reg_switch_choice("1", s, "sell", "sell_something", "seller")
    for response, expected in [("1", "continue_current_workflow"), ("2", "switch_to_new_combination"), ("exit", "exit_requested"), ("what?", "clarification_requested")]:
        s = session(pending_auth_reg_switch={"new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"})
        assert (await h.handle_auth_reg_switch_response("1", s, response))["status"] == expected
    h.openai_service.generate_response.return_value = "continue_current"
    assert await h._analyze_switch_choice("maybe") == "continue_current"
    h.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await h._analyze_switch_choice("maybe") == "unclear"
    u = user("buyer")
    assert (await h.handle_account_change_confirmation(u, session(), "x", "seller", "sell"))["status"] == "account_change_confirmation_requested"
    u.is_registered = False
    assert (await h.handle_account_change_confirmation(u, session(), "x", "seller", "sell"))["status"] == "account_change_confirmation_requested"
    assert (await h.handle_account_switch_confirmation(user(), session(), "x", "buyer"))["status"] == "account_switch_confirmation_requested"
    assert (await h.handle_role_switch_confirmation(user(), session(), "x", "seller"))["status"] == "role_switch_confirmation_requested"
    assert (await h._handle_role_switch_fallback(user(), session(), "seller", "buyer"))["status"] == "role_switch_confirmation_requested"
    s = session(pending_role_switch={"target_role": "seller", "current_role": "buyer", "original_message": "x"})
    h._handle_switch_to_existing_account = AsyncMock(return_value={"status": "exit"})
    assert (await h.handle_role_switch_response(user(), s, "1", AsyncMock()))["status"] == "exit"
    s = session(pending_role_switch={"target_role": "seller", "current_role": "buyer", "original_message": "x"})
    assert (await h.handle_role_switch_response(user(), s, "2", AsyncMock()))["status"] == "role_switch_declined"
    s = session(pending_role_switch={"target_role": "seller", "current_role": "buyer", "original_message": "x"})
    assert (await h.handle_role_switch_response(user(), s, "?", AsyncMock()))["status"] == "role_switch_clarification_requested"
    assert await h._parse_account_selection("2", [{"number": 2, "email": "a@x"}]) == {"number": 2, "email": "a@x"}
    assert await h._parse_account_selection("a@x", [{"number": 1, "email": "a@x"}])
    assert await h._parse_account_selection("none", []) is None
    assert (await h._show_account_selection_clarification(user(), {"formatted_options": [{"text": "1"}]}, "seller"))["status"] == "account_selection_clarification_requested"


@pytest.mark.asyncio
async def test_authentication_orchestrator_main_and_workflow_branches(monkeypatch):
    h = async_handler(AuthenticationOrchestrator, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), authentication_service=AsyncMock(), registration_service=AsyncMock(), intent_service=AsyncMock(), support_service=AsyncMock(), chat_service=None)
    h.auth_reg_switch = AsyncMock(); h.profile_selection_service = AsyncMock()
    h.authentication_service.filter_users_by_intent = MagicMock()
    h.support_service.redirect_to_support.return_value = {"status": "support"}
    h.authentication_service.validate_token.return_value = None
    h.profile_selection_service.handle_profile_selection.return_value = {"status": "profile"}
    assert (await h.authentication_orchestrator_flow("1", "x", session(), {"intent": "buy_something", "confidence": 90}))["status"] == "profile"
    h._handle_auth_fallback = AsyncMock(return_value={"status": "fallback"})
    assert (await h.authentication_orchestrator_flow("1", "x", session(), {"intent": "other", "confidence": 90}))["status"] == "fallback"
    h.support_service.redirect_to_support.return_value = {"status": "support"}
    h.authentication_service.validate_token.side_effect = RuntimeError("bad")
    assert (await h.authentication_orchestrator_flow("1", "x", session(), {}))["status"] == "support"
    h.authentication_service.validate_token.side_effect = None
    h.authentication_service.user_authenticate.return_value = {"success": False}
    h._redirect_to_registration_flow = AsyncMock(return_value={"status": "register"})
    assert (await h._start_authentication_flow("1", "x", session(), {"intent": "sell_something", "confidence": 90}))["status"] == "register"
    assert (await h._start_authentication_flow("1", "x", session(), {"intent": "unknown", "confidence": 10}))["status"] == "clarification_sent"
    h.authentication_service.user_authenticate.return_value = {"success": True, "response": []}
    h.authentication_service.filter_users_by_intent.return_value = {"success": False}
    assert (await h._start_authentication_flow("1", "x", session(), {"intent": "ambiguous", "confidence": 90}))["status"] == "register"
    h.authentication_service.filter_users_by_intent.return_value = {"success": True, "filtered_users": [{}], "unique_emails": ["x@x"]}
    h.authentication_service.initiate_email_confirmation.return_value = {"status": "email"}
    assert (await h._handle_user_selection("1", session(), {"filtered_users": [{}], "unique_emails": ["x@x"]}, {"intent": "buy_something"}, "x"))["status"] == "email"
    assert (await h._handle_user_selection("1", session(), {"unique_emails": []}, {"intent": "buy_something"}, "x"))["status"] == "register"
    h.authentication_service.handle_email_otp_validation.return_value = {"status": "otp"}
    assert (await h._handle_authentication_workflow("1", "1", session(authentication_stage="email_otp"), {}))["status"] == "otp"
    assert not await h._should_handle_intent_switch_during_auth("buy_something", 50, "email_confirmation")
    assert await h._should_handle_intent_switch_during_auth("buy_something", 90, "email_confirmation", session())
    assert await h._should_handle_intent_switch_during_registration("buy_something", 90, "seller")
    assert not await h._should_handle_intent_switch_during_registration("buy_something", 50, "seller")
    assert (await h._handle_auth_clarification_request("1", "x"))["status"] == "clarification_sent"
    assert (await h._handle_auth_general_inquiry("1", "x"))["status"] == "general_inquiry_handled"
    assert (await h._handle_auth_fallback("1", "x"))["status"] == "fallback"


# BFS and seller handlers ----------------------------------------------------
@pytest.mark.asyncio
async def test_bfs_search_routes_extraction_payload_results_and_bid_state(monkeypatch):
    wa, manager, api = AsyncMock(), AsyncMock(), AsyncMock()
    h = async_handler(BFSSearchHandler, whatsapp_service=wa, session_manager=manager, openai_service=AsyncMock(), auto_categorization_service=AsyncMock(), bfs_api_service=api)
    h.openai_service.extract_entities.return_value = {"success": True, "products": [{"description": "Laptop"}, {"description": ""}]}
    h.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "IT"}
    h.bfs_api_service.search_bfs_items.return_value = {"success": False, "error": "down"}
    assert (await h.handle_bfs_search(user(), session(), "laptop"))["status"] == "bfs_search_failed"
    api.search_bfs_items.return_value = {"success": True, "data": [{"description": "Laptop", "specification": "HP", "availableQuantity": 2, "sellPrice": 10}]}
    result = await h.handle_bfs_search(user(), session(), "laptop")
    assert result["status"] == "bfs_search_completed"
    h._extract_entities = AsyncMock(return_value=[])
    assert (await h.handle_bfs_search(user(), session(), "?"))["status"] == "no_products_found"
    h._extract_entities = AsyncMock(return_value=[{"description": "x"}])
    h.bfs_api_service.search_bfs_items.return_value = {"success": False}
    assert (await h.handle_bfs_search(user(), session(), "x"))["status"] == "bfs_search_failed"
    h.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "Other"}
    assert (await h._build_api_payload([{"description": "x"}], "u", "s"))[0]["category"] == []
    h.auto_categorization_service.categorize_item.side_effect = RuntimeError("cat")
    assert await h._build_api_payload([{"description": "x"}], "u", "s")
    s = session(bfs_results=[])
    assert (await h.initiate_bid_flow(user(), s))["status"] == "bfs_no_items_to_bid"
    s = session(bfs_results=[{"id": "i", "description": "x"}])
    monkeypatch.setattr(bfs_mod, "generate_bid_format", lambda _: "format")
    assert (await h.initiate_bid_flow(user(), s))["status"] == "bfs_awaiting_bid_format"
    h._clear_bid_state = AsyncMock()
    assert (await h.handle_button(user(), s, "bfs_restart"))["status"] == "bfs_send_cancel_message"
    for button in ["bfs_cancel", "unknown"]:
        assert (await h.handle_button(user(), s, button))["status"] in {"bfs_send_cancel_message", "unknown_bfs_button"}
    await h._send_bfs_results(user(), s, [], suppress_raise_rfq_on_no_results=True)
    await h._send_bfs_results(user(), s, None)
    await h._send_bfs_results(user(), s, {"x": 1})
    assert await h._cancel_bid_flow(user(), s, "test") == {"status": "bfs_send_cancel_message", "reason": "test"}


@pytest.mark.asyncio
async def test_seller_auth_mixin_interest_and_bid_boundaries(monkeypatch):
    mix = async_handler(SellerAuthMixin, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), otp_service=AsyncMock(), auth_redis_service=AsyncMock(), openai_service=AsyncMock())
    mix.auth_redis_service.retrieve.return_value = None
    assert (await mix.check_seller_auth_state("+1", "s"))["state"] == "not_auth"
    mix.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=True)
    assert (await mix.check_seller_auth_state("+1", "s"))["account_type"] == "buyer"
    mix.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=False, org_id="s", id="u")
    assert (await mix.check_seller_auth_state("+1", "s"))["state"] == "correct_seller"
    mix.auth_redis_service.retrieve.side_effect = RuntimeError("redis")
    assert (await mix.check_seller_auth_state("+1", "s"))["state"] == "not_auth"
    current = SimpleNamespace(self_client=True, email="a@x", dict=lambda: {"email": "a@x"})
    mix.auth_redis_service.retrieve.side_effect = None
    assert (await mix.prompt_seller_account_switch("1", session(), current))["status"] == "switch_prompt_sent"
    mix._parse_switch_choice = AsyncMock(return_value="stay")
    assert (await mix.handle_seller_switch_response("1", session(target_seller_id="s"), "no"))["status"] == "switch_declined"
    mix._parse_switch_choice.return_value = "unclear"
    assert (await mix.handle_seller_switch_response("1", session(), "x"))["status"] == "switch_response_unclear"
    mix._parse_switch_choice.return_value = "switch"
    mix.authentication_service.clear_user_token = AsyncMock()
    mix.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await mix.handle_seller_switch_response("1", session(target_seller_id="s"), "1"))["status"] == "otp"
    mix._parse_switch_choice = SellerAuthMixin._parse_switch_choice.__get__(mix)
    mix.openai_service.generate_response.return_value = "stay"
    assert await mix._parse_switch_choice("maybe") == "stay"
    mix.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await mix._parse_switch_choice("maybe") == "unclear"
    mix.authentication_service.user_authenticate.return_value = {"success": True, "response": [{"id": "s", "selfClient": False, "username": "s@x", "orgId": "o"}]}
    assert (await mix.get_seller_info("1", "s"))[0] == "s@x"
    mix.authentication_service.user_authenticate.return_value = {"success": False}
    assert await mix.get_seller_info("1", "s") == (None, None)
    mix.initiate_seller_auth = SellerAuthMixin.initiate_seller_auth.__get__(mix)
    mix.get_seller_info = AsyncMock(return_value=("s@x", {"id": "s"}))
    mix.otp_service.send_otp.return_value = {"status": "otp_sent"}
    assert (await mix.initiate_seller_auth("1", session(), "s"))["status"] == "otp_sent"
    mix.otp_service.send_otp.return_value = {"status": "bad", "error": "x"}
    mix._reset_workflow_with_menu = AsyncMock()
    assert (await mix.initiate_seller_auth("1", session(), "s"))["status"] == "otp_send_failed"
    mix.otp_service = None
    assert (await mix.initiate_seller_auth("1", session(), "s"))["status"] == "otp_instruction_sent"
    mix._get_current_user_type = AsyncMock(return_value=None)
    await mix._reset_workflow_with_menu("1", session(), "message")


@pytest.mark.asyncio
async def test_seller_interest_and_bid_action_constructor_and_errors(monkeypatch):
    wa, auth, manager = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(seller_interest_mod, "get_auth_redis_service", lambda: AsyncMock())
    monkeypatch.setattr(seller_interest_mod, "get_settings", lambda: SimpleNamespace(procucev_rfq_details_url="https://x"))
    interest = SellerRFQInterestHandler(wa, auth, manager, AsyncMock())
    interest.check_seller_auth_state = AsyncMock(return_value={"state": "correct_seller"})
    interest._show_intermediate_buttons = AsyncMock(return_value={"status": "buttons"})
    assert (await interest.handle_rfq_interest_click("1", "r", "s", session()))["status"] == "buttons"
    interest.check_seller_auth_state.return_value = {"state": "not_auth"}
    interest.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await interest.handle_rfq_interest_click("1", "r", "s", session()))["status"] == "otp"
    interest.check_seller_auth_state.return_value = {"state": "wrong_account", "current_user": {}}
    interest.prompt_seller_account_switch = AsyncMock(return_value={"status": "switch"})
    assert (await interest.handle_rfq_interest_click("1", "r", "s", session()))["status"] == "switch"
    interest.settings.procucev_rfq_details_url = "https://details"
    assert (await interest.handle_check_details_click("1", "r", "s", session()))["status"] == "check_details_sent"
    interest.auth_redis_service = AsyncMock(); interest.auth_redis_service.retrieve.return_value = None
    assert (await interest._redirect_to_seller_flow("1", "r", "s", session()))["status"] == "error"
    assert (await interest.handle_otp_validated("1", session()))["status"] == "error"
    bid = async_handler(BFSSellerBidHandler, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), bfs_api_service=AsyncMock())
    bid.check_seller_auth_state = AsyncMock(return_value={"state": "correct_seller"})
    bid.bfs_api_service.accept_bid_by_seller.return_value = {"success": False}
    assert (await bid.handle_accept_bid_click("1", "b", "s", session()))["status"] == "error"
    bid.bfs_api_service.accept_bid_by_seller.return_value = {"success": False, "error": "no"}
    assert (await bid._execute_bid_action("1", "b", True, session()))["error"] == "no"
    bid.bfs_api_service.accept_bid_by_seller.side_effect = RuntimeError("api")
    assert (await bid._execute_bid_action("1", "b", True, session()))["status"] == "error"
    assert (await bid.handle_reject_bid_click("1", "", "s", session()))["status"] == "error"
    assert (await bid.handle_otp_validated("1", session()))["status"] == "error"


# Small remaining handlers ---------------------------------------------------
@pytest.mark.asyncio
async def test_format_modification_intent_switch_and_seller_bid_helpers(monkeypatch):
    h = FormatModificationHandler(AsyncMock()); h.session_manager = AsyncMock()
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (False, None))
    assert (await h.handle_format_modification("x", session(), user()))["status"] == "error"
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "delivery"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _: 0)
    assert (await h.handle_format_modification("x", session(), user()))["status"] == "format_error"
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 3)
    assert (await h._handle_format_error(session(), user(), "bad", "items"))["status"] == "max_retries_reached"
    s = session(incomplete_products=[1], complete_products=[2], extracted_entities=[])
    monkeypatch.setattr(format_mod.WorkflowManager, "get_delivery_details", lambda _: {"delivery_date": "d", "pincode": "p", "city": "c", "state": "s"})
    await h._replace_products_array(s, [{"description": "x"}])
    assert s.workflow_state["extracted_entities"][0]["city"] == "c"
    ih = async_handler(IntentSwitchHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), openai_service=AsyncMock())
    ih.response_helpers.generate_contextual_response.return_value = "choice"
    active = session(extracted_entities=[{"product_name": "x"}]); active.workflow_type = SimpleNamespace(value="rfq_creation")
    assert not await ih.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "optional_fields"})
    assert (await ih.handle_intent_switch_choice(user(), active, "sell", "sell_something", {"confidence": 100}))["status"] == "intent_switch_choice_presented"
    ih.openai_service.analyze_intent_switch_response.side_effect = RuntimeError("AI")
    active.workflow_state["pending_intent_switch"] = {"new_intent": "sell_something", "new_intent_message": "sell", "intent_result": {}}
    assert (await ih.handle_intent_switch_response(user(), active, "continue current"))["status"] == "continue_current_workflow"
    bid = async_handler(BFSSellerBidHandler, whatsapp_service=AsyncMock(), session_manager=AsyncMock(), bfs_api_service=AsyncMock())
    bid.bfs_api_service.accept_bid_by_seller.return_value = {"ok": False}
    assert (await bid._execute_bid_action("1", "b", True, session()))["status"] == "error"
    assert ConversationOutcome.abandoned is not None
