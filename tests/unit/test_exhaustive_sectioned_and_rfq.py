"""Deterministic direct-method coverage for sectioned RFQ and related services.

Every network, database, Redis, Chroma, OpenAI, WhatsApp, email, and timer
boundary in this module is replaced with an in-memory mock.  The tests are
intentionally method-oriented so state-machine branches remain visible.
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.handlers.authentication_orchestrator as auth_mod
import app.services.handlers.auth_registration_intent_switch as switch_mod
import app.services.handlers.confirmation_handler as confirmation_mod
import app.services.handlers.bfs_search_handler as bfs_mod
import app.services.handlers.purchase_intent_handler as purchase_mod
import app.services.handlers.purchase_workflow_handler as workflow_mod
import app.services.rfq_background_service as background_mod
import app.services.rfq_intimation_service as intimation_mod
import app.services.webhook_health_monitor_service as health_mod
import app.services.enhanced_auto_categorization_service as categorization_mod
import app.services.enhanced_excel_report_service as report_mod
from app.services.helpers.chat_service_helpers import ChatServiceHelpers

from app.models import ConversationOutcome, InteractionType, RFQStatus, ResponseType, WorkflowType
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.purchase_workflow_handler import PurchaseWorkflowHandler


# ---------------------------------------------------------------------------
# Shared test objects


def make_session(**state):
    return SimpleNamespace(
        session_id="sid", external_user_id="uid", phone_number="+91123",
        workflow_type=None, workflow_state=dict(state), conversation_history={},
        product_items=[], outcome=None, completed_at=None, rfq_ids=[], user_type=None,
        extracted_entities={}, rfq_id=None,
    )


def make_user(role="buyer", registered=True):
    return SimpleNamespace(id="u1", org_id="o1", phone_number="+91123", role=role,
                           is_registered=registered, self_client=role == "buyer")


def bare(cls, **attrs):
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


class Query:
    def __init__(self, first=None, all_values=None, scalar_value=0, delete_value=0):
        self.first_value = first
        self.all_values = all_values or []
        self.scalar_value = scalar_value
        self.delete_value = delete_value

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self.first_value

    def all(self):
        return self.all_values

    def scalar(self):
        return self.scalar_value

    def count(self):
        return self.scalar_value

    def delete(self):
        return self.delete_value


class DB:
    def __init__(self, query=None):
        self.query_obj = query or Query()
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.bind = "bind"

    def query(self, *args, **kwargs):
        return self.query_obj

    def add(self, value):
        self.added.append(value)


class DBContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        return False


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *args):
        return False


# ---------------------------------------------------------------------------
# Sectioned RFQ handler


def sectioned_handler():
    h = bare(SectionedRFQCreationHandler, entity_service=AsyncMock(),
             whatsapp_service=AsyncMock(), cancel_service=AsyncMock(),
             session_manager=AsyncMock(), confirmation_handler=AsyncMock(),
             attachment_decision_handler=None)
    h.entity_service.openai_service = SimpleNamespace(validate_delivery_date=AsyncMock())
    return h


def sectioned_manager(monkeypatch):
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data",
                        lambda s, name: s.workflow_state.get(name))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data",
                        lambda s, name, value: s.workflow_state.__setitem__(name, value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel",
                        lambda s: bool(s.workflow_state.get("excel_source")))


def test_sectioned_private_helpers_and_combined_schema(monkeypatch):
    h = sectioned_handler()
    sectioned_manager(monkeypatch)
    s = make_session(date_location={"deliveryDate": "2 Jan", "pincode": "560001", "city": "C", "state": "S"})
    s.workflow_type = WorkflowType.rfq_creation
    assert sectioned_mod._format_date_for_display("") == ""
    assert sectioned_mod._format_date_for_display("2025-01-02") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("02-01-2025") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("02/01/2025") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("02 Jan 2025") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("02 January 2025") == "2 January 2025"
    assert sectioned_mod._format_date_for_display("2 Jan").endswith(str(sectioned_mod.datetime.now().year))
    assert sectioned_mod._format_date_for_display("bad") == "bad"
    assert h._build_entity_context(s)["workflow_state"]["global_supplementary_fields"]["city"] == "C"
    assert h._build_entity_context_with_items(s, [{"description": "bolt"}])["workflow_state"]["incomplete_products"]
    assert h._has_delivery_basics({"deliveryDate": "d", "pincode": 560001})
    assert not h._has_delivery_basics({"deliveryDate": "", "pincode": "560001"})
    assert h._is_delivery_complete({"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    assert not h._is_delivery_complete({"deliveryDate": "d"})
    assert [h._get_next_section(x) for x in ["date_location", "items", "attachments"]] == ["items", "attachments", "final_confirmation"]
    assert h._get_next_section("final_confirmation") is None and h._get_next_section("bad") is None
    missing = h._get_incomplete_items([{"description": "", "quantity": 0}, {"description": "x", "quantity": 1}])
    assert missing[0]["missing_fields"] == ["description", "quantity"]
    assert "Product description" in h._generate_missing_items_fields_message(missing, [])
    assert "Delivery Date" in h._generate_delivery_missing_fields_message({"pincode": "1"})
    assert "fetch" in h._generate_delivery_missing_fields_message({"deliveryDate": "d", "pincode": "p"})

    schema = SimpleNamespace(model_dump=lambda: {"project_desc": "x"})
    monkeypatch.setattr(ChatServiceHelpers, "create_combined_rfq_schema_from_multiple_products", lambda products: schema)
    monkeypatch.setattr(ChatServiceHelpers, "serialize_products_for_session", lambda products: [{"serialized": True}])
    combined = h._build_combined_rfq_from_sections({"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"}, [{"description": "x", "quantity": 2}])
    assert combined == {"combined_schema": {"project_desc": "x"}, "products": [{"serialized": True}]}


@pytest.mark.asyncio
async def test_sectioned_location_validation_and_direct_format_branches(monkeypatch):
    h = sectioned_handler()
    sectioned_manager(monkeypatch)
    u, s = make_user(), make_session()
    h.entity_service.openai_service.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": "2025-02-03"}
    assert (await h._validate_delivery_date("tomorrow"))["normalized_date"] == "3 February 2025"
    h.entity_service.openai_service.validate_delivery_date.return_value = {"is_valid": False, "user_friendly_message": "invalid"}
    assert (await h._validate_delivery_date("bad"))["error"] == "invalid"
    h.entity_service.openai_service.validate_delivery_date.side_effect = RuntimeError("ai")
    assert (await h._validate_delivery_date("raw"))["is_valid"]
    assert (await h._autofill_location_from_pincode({"pincode": ""}))["is_valid"]
    assert not (await h._autofill_location_from_pincode({"pincode": "123"}))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "New", "state": "State"}))
    value = await h._autofill_location_from_pincode({"pincode": "560001", "city": "Old"})
    assert value["delivery_data"]["city"] == "New"
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    assert not (await h._autofill_location_from_pincode({"pincode": "560001"}))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("down")))
    assert "Error validating" in (await h._autofill_location_from_pincode({"pincode": "560001"}))["error"]
    assert not (await h._validate_pincode_and_get_location("bad"))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "C", "state": "S"}))
    assert (await h._validate_pincode_and_get_location("560001"))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(return_value={}))
    assert not (await h._validate_pincode_and_get_location("560001"))["is_valid"]
    monkeypatch.setattr(sectioned_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("down")))
    assert not (await h._validate_pincode_and_get_location("560001"))["is_valid"]

    h._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmation"})
    h._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    h._display_delivery_validation_error = AsyncMock(return_value={"status": "validation"})
    h._display_invalid_pincode_message = AsyncMock(return_value={"status": "pincode"})
    h._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "d"})
    h._autofill_location_from_pincode = AsyncMock(return_value={"is_valid": True, "delivery_data": {"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"}})
    h.entity_service.extract_entities.return_value = {"deliveryDate": "d", "pincode": "p", "products": [{"description": "x", "quantity": 1}]}
    h.session_manager.save_session = AsyncMock()
    result = await h._handle_date_location_section(u, s, "details", [])
    assert result["status"] == "confirmation"
    s.workflow_state["date_location"] = {"deliveryDate": "d", "pincode": "p"}
    h._display_invalid_pincode_message = AsyncMock(return_value={"status": "pincode"})
    # Basics already stored, so the extraction block is skipped; the validation flags it
    # would have set must still be bound. Date+pincode without city/state means the
    # pincode lookup failed, so the user is asked for a valid one.
    assert (await h._handle_date_location_section(u, s, "plain", []))["status"] == "pincode"

    parser = sectioned_mod.sectioned_rfq_format_parser
    monkeypatch.setattr(parser, "parse_delivery_format", lambda _: {"error": "bad", "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    assert (await h._process_delivery_modification_direct(u, make_session(), "date: bad"))["status"] == "format_error"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    h._cancel_after_max_retries = AsyncMock(return_value={"status": "cancelled"})
    assert (await h._process_delivery_modification_direct(u, make_session(), "date: bad"))["status"] == "cancelled"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    h.entity_service.extract_entities.return_value = {"deliveryDate": "", "pincode": "", "products": []}
    h._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    assert (await h._process_delivery_modification_direct(u, make_session(), "just a question"))["status"] == "missing"

    valid = {"deliveryDate": "2025-02-03", "pincode": "560001", "additional_text": ""}
    monkeypatch.setattr(parser, "parse_delivery_format", lambda _: valid)
    h._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "3 February 2025"})
    h._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": True, "city": "C", "state": "S"})
    h._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmed"})
    assert (await h._process_delivery_modification_direct(u, make_session(), "format"))["status"] == "confirmed"
    for dv, pv in [(False, False), (False, True), (True, False)]:
        h._validate_delivery_date = AsyncMock(return_value={"is_valid": dv, "error": "date"})
        h._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": pv, "error": "pin"})
        assert (await h._process_delivery_modification_direct(u, make_session(), "format"))["status"] == "validation_error"


@pytest.mark.asyncio
async def test_sectioned_routing_items_buttons_restart_and_attachment_paths(monkeypatch):
    h = sectioned_handler(); sectioned_manager(monkeypatch); u, s = make_user(), make_session()
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_awaiting_section_modification", lambda *_: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda *_: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "items")
    h._handle_items_section = AsyncMock(return_value={"status": "items"})
    assert (await h.handle_sectioned_rfq(u, s, {"button_reply": {"title": "x"}}))["status"] == "items"
    assert (await h.handle_sectioned_rfq(u, s, {"text": {"body": "x"}}))["status"] == "items"
    assert (await h.handle_sectioned_rfq(u, s, {"content": "x"}))["status"] == "items"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "unknown")
    assert (await h.handle_sectioned_rfq(u, s, 4))["status"] == "error"

    parser = sectioned_mod.sectioned_rfq_format_parser
    monkeypatch.setattr(parser, "parse_items_format", lambda _: {"error": "bad", "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    assert (await h._process_items_modification_direct(u, make_session(), "bad"))["status"] == "format_error"
    h._cancel_after_max_retries = AsyncMock(return_value={"status": "cancel"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    assert (await h._process_items_modification_direct(u, make_session(), "bad"))["status"] == "cancel"
    monkeypatch.setattr(parser, "parse_items_format", lambda _: {"items": [{"description": "x", "quantity": 1}], "additional_text": ""})
    h._display_items_confirmation = AsyncMock(return_value={"status": "confirmed"})
    assert (await h._process_items_modification_direct(u, make_session(), "ok"))["status"] == "confirmed"

    h._handle_items_section = SectionedRFQCreationHandler._handle_items_section.__get__(h)
    h._display_items_confirmation = AsyncMock(return_value={"status": "confirmed"})
    h._display_items_missing_fields = AsyncMock(return_value={"status": "missing"})
    h.entity_service.extract_entities.return_value = {"products": []}
    assert (await h._handle_items_section(u, make_session(), "x", []))["status"] == "awaiting_items"
    h.entity_service.extract_entities.return_value = {"products": [{"description": "x", "quantity": 1}]}
    assert (await h._handle_items_section(u, make_session(), "x", []))["status"] == "confirmed"
    h.entity_service.extract_entities.return_value = {"products": [{"description": "x"}]}
    assert (await h._handle_items_section(u, make_session(), "x", []))["status"] == "missing"
    many = [{"description": str(i), "quantity": 1} for i in range(6)]
    h.entity_service.extract_entities.return_value = {"products": many}
    h.cancel_service._clear_workflow_state = AsyncMock(); h.cancel_service._send_cancellation_message = AsyncMock()
    assert (await h._handle_items_section(u, make_session(), "x", []))["status"] == "item_limit_exceeded_cancelled"
    excel = make_session(excel_source=True)
    assert (await SectionedRFQCreationHandler._display_items_confirmation(h, u, excel, [{"description": "x", "quantity": 1}]))["status"] == "awaiting_items_confirmation"
    assert (await SectionedRFQCreationHandler._display_items_missing_fields(h, u, excel, [{"description": "x"}], [{"index": 1, "item": {"description": "x"}, "missing_fields": ["quantity"]}]))["status"] == "awaiting_missing_item_fields"

    h._handle_section_confirm = AsyncMock(return_value={"status": "confirmed"})
    h._handle_section_modify = AsyncMock(return_value={"status": "modified"})
    h._handle_restart_rfq = AsyncMock(return_value={"status": "restart"})
    h._handle_final_rfq_submission = AsyncMock(return_value={"status": "submitted"})
    h._handle_final_cancel = AsyncMock(return_value={"status": "final-cancel"})
    h._handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    for button, expected in [("confirm_items", "confirmed"), ("modify_items", "modified"), ("restart_rfq", "restart"), ("confirm_cancel", "restart"), ("decline_cancel", "restart"), ("final_confirm_rfq", "submitted"), ("final_cancel_rfq", "final-cancel"), ("attachments_yes", "attachment")]:
        assert (await h.handle_section_button_click(u, s, button))["status"] == expected
    assert (await h.handle_section_button_click(u, s, "unknown"))["status"] == "error"

    h._handle_items_section = AsyncMock(return_value={"status": "display"})
    h._handle_restart_rfq = SectionedRFQCreationHandler._handle_restart_rfq.__get__(h)
    h._handle_attachment_decision = SectionedRFQCreationHandler._handle_attachment_decision.__get__(h)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, n: s.workflow_state.get(n))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda s: "date_location")
    h.cancel_service.handle_cancel_intent = AsyncMock(return_value={"status": "cancelled_aborted"})
    h._display_delivery_confirmation = AsyncMock(return_value={"status": "redrawn"})
    assert (await h._handle_restart_rfq(u, make_session(date_location={"deliveryDate": "d"})))["status"] == "redrawn"
    h.cancel_service.handle_cancel_intent.return_value = {"status": "cancelled"}
    assert (await h._handle_restart_rfq(u, make_session()))["status"] == "cancelled"
    assert (await h._handle_attachment_decision(u, s, "attachments_yes"))["status"] == "awaiting_attachments"
    h._handle_final_confirmation = AsyncMock(return_value={"status": "final"})
    assert (await h._handle_attachment_decision(u, s, "attachments_no"))["status"] == "final"
    h._handle_final_rfq_submission = SectionedRFQCreationHandler._handle_final_rfq_submission.__get__(h)
    assert (await h._handle_final_rfq_submission(u, s))["status"] == "rfq_submitted"
    assert s.workflow_type == WorkflowType.rfq_submitted
    assert (await h._offer_restart_confirmation(u, s))["status"] == "awaiting_restart_confirmation"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_sectioned_rfq", MagicMock())
    assert (await h._handle_restart_confirmation_response(u, s, "yes"))["status"] == "workflow_restarted"
    assert (await h._handle_restart_confirmation_response(u, s, "no"))["status"] == "restart_declined"
    assert (await h._handle_restart_confirmation_response(u, s, "maybe"))["status"] == "awaiting_restart_confirmation"


@pytest.mark.asyncio
async def test_sectioned_next_section_and_final_delegation(monkeypatch):
    h = sectioned_handler(); sectioned_manager(monkeypatch); u = make_user()
    h._handle_items_section = AsyncMock(return_value={"status": "items"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, n: s.workflow_state.get(n))
    s = make_session(bfs_rfq_products=["bolt", "nut"])
    assert (await h._initiate_next_section(u, s, "items"))["status"] == "items"
    assert "bfs_rfq_products" not in s.workflow_state
    h._handle_attachments_section = AsyncMock(return_value={"status": "attachments"})
    assert (await h._initiate_next_section(u, make_session(), "attachments"))["status"] == "attachments"
    h._handle_final_confirmation = AsyncMock(return_value={"status": "final"})
    assert (await h._initiate_next_section(u, make_session(), "final_confirmation"))["status"] == "final"
    assert (await h._initiate_next_section(u, make_session(), "other"))["status"] == "section_initiated"
    h._handle_final_confirmation = SectionedRFQCreationHandler._handle_final_confirmation.__get__(h)
    h.confirmation_handler.handle_pending_confirmations.return_value = {"status": "handled"}
    s = make_session(date_location={"deliveryDate": "d"}, items=[])
    h._build_combined_rfq_from_sections = MagicMock(return_value={"combined_schema": {}, "products": []})
    assert (await h._handle_final_confirmation(u, s, "yes"))["status"] == "handled"
    assert (await h._cancel_after_max_retries(u, s, "items"))["status"] == "max_retries_cancelled"


# ---------------------------------------------------------------------------
# Authentication intent switch and orchestrator


def switch_handler():
    return bare(AuthRegistrationIntentSwitch, whatsapp_service=AsyncMock(), openai_service=MagicMock())


@pytest.mark.asyncio
async def test_auth_registration_switch_all_direct_choices(monkeypatch):
    h = switch_handler(); u = make_user(); s = make_session(user_type="buyer"); s.workflow_type = "authentication"
    assert await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    assert not await h.should_handle_auth_reg_switch(make_session(), "sell_something", "seller")
    s.workflow_state["pending_auth_reg_switch"] = {"new_intent": "sell_something"}
    assert not await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    assert [h._get_target_combination(x, "seller") for x in ["sell_something", "buy_something", "register_seller", "register_buyer", "register_x", "other"]] == ["authentication_seller", "authentication_buyer", "registration_seller", "registration_buyer", "registration_seller", "authentication_seller"]
    assert h._get_new_combination_desc("sell_something", "seller") == "seller login"
    assert h._get_new_combination_desc("buy_something", "buyer") == "buyer login"
    assert h._get_new_combination_desc("register_seller", "seller") == "seller registration"
    assert h._get_current_combination_desc(s) == "buyer login"
    assert h._get_target_workflow("register_buyer") == "registration"
    h.whatsapp_service.send_message = AsyncMock()
    s.workflow_state.pop("pending_auth_reg_switch")
    assert (await h.handle_auth_reg_switch_choice("+1", s, "sell", "sell_something", "seller"))["status"] == "auth_reg_switch_choice_presented"
    for message, expected in [("continue", "continue_current_workflow"), ("2", "switch_to_new_combination"), ("exit", "exit_requested"), ("huh", "clarification_requested")]:
        s = make_session(pending_auth_reg_switch={"new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"})
        assert (await h.handle_auth_reg_switch_response("+1", s, message))["status"] == expected
    assert (await h.handle_auth_reg_switch_response("+1", make_session(), "1"))["status"] == "no_pending_switch"
    h.openai_service.generate_response.return_value = "continue_current"
    assert await h._analyze_switch_choice("maybe") == "continue_current"
    h.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await h._analyze_switch_choice("maybe") == "unclear"
    s = make_session(); h._clear_current_workflow(s); assert s.outcome == "abandoned" and s.workflow_type is None
    assert (await h.handle_account_change_confirmation(u, s, "x", "seller", "sell_something"))["status"] == "account_change_confirmation_requested"
    assert (await h.handle_account_change_confirmation(make_user(registered=False), make_session(), "x", "seller", "sell_something"))["status"] == "account_change_confirmation_requested"
    assert (await h.handle_account_switch_confirmation(u, make_session(), "x", "buyer"))["status"] == "account_switch_confirmation_requested"
    assert (await h.handle_role_switch_confirmation(u, make_session(), "x", "seller"))["status"] == "role_switch_confirmation_requested"
    assert (await h._handle_role_switch_fallback(u, make_session(), "seller", "buyer"))["status"] == "role_switch_confirmation_requested"


@pytest.mark.asyncio
async def test_auth_switch_account_selection_and_ai_branches(monkeypatch):
    h = switch_handler(); u = make_user(); auth = AsyncMock()
    pending = {"target_role": "seller", "current_role": "buyer", "original_message": "sell"}
    s = make_session(pending_role_switch=pending)
    h._handle_switch_to_existing_account = AsyncMock(return_value={"status": "existing"})
    assert (await h.handle_role_switch_response(u, s, "1", auth))["status"] == "existing"
    s = make_session(pending_role_switch=pending)
    assert (await h.handle_role_switch_response(u, s, "2", auth))["status"] == "role_switch_declined"
    s = make_session(pending_role_switch=pending)
    assert (await h.handle_role_switch_response(u, s, "?", auth))["status"] == "role_switch_clarification_requested"
    assert (await h.handle_role_switch_response(u, make_session(), "1", auth))["status"] == "no_pending_role_switch"
    options = [{"number": 1, "email": "a@x", "text": "1 a@x"}, {"number": 2, "email": "b@x", "text": "2 b@x"}]
    assert (await h._parse_account_selection("2", options))["email"] == "b@x"
    assert (await h._parse_account_selection("a@x please", options))["number"] == 1
    assert await h._parse_account_selection("none", options) is None
    assert (await h._show_account_selection_clarification(u, {"formatted_options": options}, "seller"))["status"] == "account_selection_clarification_requested"
    h._handle_register_new_account = AsyncMock(return_value={"status": "register"})
    account_options = {"formatted_options": [{"number": 1, "email": "a@x", "action": "register_new"}]}
    assert (await h._handle_enhanced_account_selection_response(u, s, "1", auth, pending, account_options))["status"] == "register"
    h._ai_validate_three_option_response = AsyncMock(return_value="continue_current")
    s = make_session(pending_role_switch=pending)
    assert (await h._handle_traditional_role_switch_response(u, s, "3", auth, pending))["status"] == "role_switch_declined"
    h._ai_validate_three_option_response.return_value = "unclear"
    s = make_session(pending_role_switch=pending)
    assert (await h._handle_traditional_role_switch_response(u, s, "?", auth, pending))["status"] == "role_switch_clarification_requested"
    h._ai_validate_three_option_response = AuthRegistrationIntentSwitch._ai_validate_three_option_response.__get__(h)
    h.openai_service.generate_response.return_value = "yes"
    assert await h._ai_validate_role_confirmation_response("yes", "buyer", "seller") == "yes"
    h.openai_service.generate_response = MagicMock(side_effect=RuntimeError("down"))
    assert await h._ai_validate_role_confirmation_response("no", "buyer", "seller") == "no"
    assert await h._ai_validate_three_option_response("2", "buyer", "seller", "role_switch") == "register_new"
    h._handle_switch_to_existing_account = AuthRegistrationIntentSwitch._handle_switch_to_existing_account.__get__(h)
    auth.clear_user_token = AsyncMock()
    h.whatsapp_service.send_message = AsyncMock()
    exit_service = MagicMock(); exit_service.handle_exit_intent = AsyncMock(return_value={"status": "exited"})
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda **_: exit_service)
    assert (await h._handle_switch_to_existing_account(u, make_session(), "seller", "sell", auth))["status"] == "exit_completed"


@pytest.mark.asyncio
async def test_auth_orchestrator_predicates_and_workflow_routes(monkeypatch):
    h = bare(AuthenticationOrchestrator, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
             authentication_service=AsyncMock(), registration_service=AsyncMock(), intent_service=AsyncMock(),
             support_service=AsyncMock(), chat_service=None, auth_reg_switch=AsyncMock(), profile_selection_service=AsyncMock())
    h.support_service.redirect_to_support.return_value = {"status": "support"}
    h.profile_selection_service.handle_profile_selection.return_value = {"status": "profile"}
    h.profile_selection_service.handle_profile_selection_response.return_value = {"status": "selected"}
    s = make_session(); h.authentication_service.validate_token.return_value = None
    assert (await h.authentication_orchestrator_flow("+1", "buy", s, {"intent": "buy_something", "confidence": 90}))["status"] == "profile"
    h.authentication_service.validate_token.return_value = {"is_registered": True, "id": "u", "name": "A", "email": "a@x"}
    assert (await h.authentication_orchestrator_flow("+1", "buy", s, {"intent": "buy_something", "confidence": 90})).id == "u"
    h.authentication_service.validate_token.side_effect = RuntimeError("bad")
    assert (await h.authentication_orchestrator_flow("+1", "x", s, {}))["status"] == "support"
    h.authentication_service.validate_token.side_effect = None
    h.authentication_service.validate_token.return_value = None
    h._handle_auth_fallback = AsyncMock(return_value={"status": "fallback"})
    assert (await h.authentication_orchestrator_flow("+1", "x", make_session(), {"intent": "other", "confidence": 90}))["status"] == "fallback"
    for intent, confidence in [("ambiguous", 90), ("other", 10), ("greeting", 90), ("register_account", 90)]:
        assert (await h.authentication_orchestrator_flow("+1", "x", make_session(), {"intent": intent, "confidence": confidence}))["status"] == "profile"
    assert (await h._handle_auth_clarification_request("+1", "x"))["status"] == "clarification_sent"
    assert (await h._handle_auth_general_inquiry("+1", "x"))["status"] == "general_inquiry_handled"
    assert (await h._handle_auth_fallback("+1", "x"))["status"] == "fallback"
    assert await h._should_handle_intent_switch("otp", "email") is False
    assert await h._should_handle_intent_switch("sell_something", "email") is True
    assert not await h._should_handle_intent_switch_during_auth("buy_something", 50, "email_confirmation")
    assert await h._should_handle_intent_switch_during_auth("sell_something", 90, "email_confirmation", make_session())
    assert not await h._should_handle_intent_switch_during_registration("buy_something", 50, "seller")
    assert await h._should_handle_intent_switch_during_registration("buy_something", 90, "seller")


@pytest.mark.asyncio
async def test_auth_orchestrator_start_and_registration_stages(monkeypatch):
    h = bare(AuthenticationOrchestrator, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
             authentication_service=AsyncMock(), registration_service=AsyncMock(), intent_service=AsyncMock(),
             support_service=AsyncMock(), chat_service=None, auth_reg_switch=AsyncMock(), profile_selection_service=AsyncMock())
    h.support_service.redirect_to_support.return_value = {"status": "support"}
    h._redirect_to_registration_flow = AsyncMock(return_value={"status": "register"})
    assert (await h._start_authentication_flow("+1", "x", make_session(), {"intent": "unknown", "confidence": 10}))["status"] == "clarification_sent"
    h.authentication_service.filter_users_by_intent = MagicMock()
    h.authentication_service.user_authenticate.return_value = {"success": False}
    assert (await h._start_authentication_flow("+1", "x", make_session(), {"intent": "sell_something", "confidence": 90}))["status"] == "register"
    h.authentication_service.user_authenticate.return_value = {"success": True, "response": [{}]}
    h.authentication_service.filter_users_by_intent.return_value = {"success": True, "filtered_users": [{}], "unique_emails": ["a@x"]}
    h.authentication_service.initiate_email_confirmation.return_value = {"status": "email"}
    assert (await h._start_authentication_flow("+1", "x", make_session(), {"intent": "buy_something", "confidence": 90}))["status"] == "email"
    assert (await h._handle_user_selection("+1", make_session(), {"unique_emails": []}, {"intent": "buy_something"}, "x"))["status"] == "register"
    assert (await h._handle_user_selection("+1", make_session(), {"filtered_users": [{}], "unique_emails": ["a@x"]}, {"intent": "buy_something"}, "x"))["status"] == "email"
    h.authentication_service.handle_email_otp_validation.return_value = {"status": "otp"}
    assert (await h._handle_authentication_workflow("+1", "1", make_session(authentication_stage="email_otp"), {}))["status"] == "otp"
    h._check_switch_response = AsyncMock(return_value=None)
    h._start_authentication_flow = AsyncMock(return_value={"status": "started"})
    assert (await h._handle_authentication_workflow("+1", "x", make_session(), {"intent": "buy_something"}))["status"] == "started"
    h.registration_service.handle_registration_data_collection.return_value = {"status": "data"}
    assert (await h._handle_registration_workflow("+1", "x", make_session(registration_stage="data_collection", user_type="buyer"), {"intent": "buy_something"}))["status"] == "data"
    h.authentication_service.handle_email_confirmation.return_value = {"status": "email"}
    assert (await h._handle_registration_workflow("+1", "x", make_session(registration_stage="email_confirmation", current_intent_result={"intent": "buy_something"}), {}))["status"] == "email"
    h.registration_service.handle_registration_otp_validation.return_value = {"status": "registration_completed", "registration_flow_complete": True}
    s = make_session(registration_stage="email_otp")
    assert (await h._handle_registration_workflow("+1", "x", s, {}))["status"] == "registration_completed"
    assert s.workflow_type is None and s.workflow_state == {}
    h.authentication_service.handle_domain_matching.return_value = {"status": "domain"}
    assert (await h._handle_registration_workflow("+1", "x", make_session(registration_stage="domain_matching"), {}))["status"] == "domain"
    h.registration_service.handle_registration_confirmation.return_value = {"status": "confirm"}
    assert (await h._handle_registration_workflow("+1", "x", make_session(registration_stage="confirmation"), {}))["status"] == "confirm"
    assert (await h._handle_registration_workflow("+1", "x", make_session(), {}))["status"] == "register"
    h._redirect_to_registration_flow = AuthenticationOrchestrator._redirect_to_registration_flow.__get__(h)
    h.registration_service.initiate_registration.return_value = {"ok": True}
    result = await h._redirect_to_registration_flow("+1", make_session(), "seller")
    assert result["status"] == "redirected_to_registration"


# ---------------------------------------------------------------------------
# Confirmation handler


def confirmation_handler():
    return bare(ConfirmationHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
                 cancel_service=AsyncMock(), session_manager=AsyncMock(), confirmation_service=AsyncMock(),
                 _bfs_search_handler=None)


@pytest.mark.asyncio
async def test_confirmation_routes_optional_submission_and_helpers(monkeypatch):
    h = confirmation_handler(); u, s = make_user(), make_session()
    h._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"})
    h._handle_rfq_modification = AsyncMock(return_value={"status": "modified"})
    h._proceed_to_confirmation_from_optional = AsyncMock(return_value={"status": "optional"})
    h._handle_restart_workflow = AsyncMock(return_value={"status": "restart"})
    for button, status in [("confirm_rfq", "accepted"), ("no_rfq", "modified"), ("continue_rfq", "optional"), ("confirm_no_changes", "accepted"), ("restart_rfq", "restart")]:
        assert (await h.handle_confirmation_button(u, s, button))["status"] == status
    assert (await h.handle_confirmation_button(u, s, "other"))["status"] == "unknown_button"
    h.confirmation_service.parse_confirmation.return_value = "yes"
    assert (await h.handle_pending_confirmations(u, s, "yes", {}))["status"] == "accepted"
    h.confirmation_service.parse_confirmation.return_value = "no"
    assert (await h.handle_pending_confirmations(u, s, "no", {}))["status"] == "modified"
    h.confirmation_service.parse_confirmation.return_value = "unclear"
    assert (await h.handle_pending_confirmations(u, s, "x", {"intent": "confirmation_response", "confidence": .8, "context_analysis": {"confirmation_details": {"response_type": "accept"}}}))["status"] == "accepted"
    h._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarify"})
    assert (await h.handle_pending_confirmations(u, s, "x", {}))["status"] == "clarify"
    h._merge_optional_fields_and_confirm = AsyncMock(return_value={"status": "merged"})
    assert (await h.handle_optional_fields_response(u, s, "add brand"))["status"] == "merged"
    assert (await h.handle_optional_fields_response(u, s, "skip"))["status"] == "optional"
    h.response_helpers.generate_contextual_response.return_value = "please clarify"
    h._handle_confirmation_clarification = ConfirmationHandler._handle_confirmation_clarification.__get__(h)
    assert (await h._handle_confirmation_clarification(u, s, "?"))["status"] == "confirmation_clarification_requested"

    schema = SimpleNamespace(model_dump=lambda: {"project_desc": "Laptop", "items": [{"description": "Laptop", "quantity": 2}], "delivery_locations": []})
    api = AsyncMock(); api.create_rfq.return_value = {"success": True, "rfq_id": "R1"}
    monkeypatch.setattr(confirmation_mod, "RFQAPIService", lambda: api)
    result = await h._submit_rfq_to_backend(schema, u)
    assert result["success"] and result["rfq_id"] == "R1"
    api.create_rfq.side_effect = RuntimeError("api")
    assert not (await h._submit_rfq_to_backend(schema, u))["success"]
    h._extract_product_descriptions_from_rfq = ConfirmationHandler._extract_product_descriptions_from_rfq.__get__(h)
    assert h._extract_product_descriptions_from_rfq([{ "success": True, "rfq_data": {"items": [{"product_name": "Laptop"}]}}]) == ["Laptop"]
    assert h._extract_product_descriptions_from_rfq([{ "success": True, "rfq_data": {"items": [{"description": "Laptop|blue"}]}}]) == ["Laptop|blue"]
    h._merge_specifications_into_product({"description": "x", "remarks": "old", "projectDesc": ""}, {"brand": "HP", "remarks": "new", "projectDesc": "P"}, "msg")
    h._merge_specifications_into_product({"description": "x"}, {}, "fallback")


@pytest.mark.asyncio
async def test_confirmation_acceptance_optional_bfs_and_restart(monkeypatch):
    h = confirmation_handler(); u = make_user(); h.response_helpers.generate_rfq_summary_and_confirmation.return_value = "summary"
    monkeypatch.setattr(confirmation_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda e, _: SimpleNamespace(model_dump=lambda: e))
    h._submit_rfq_to_backend = AsyncMock(return_value={"success": True, "rfq_id": "R"})
    s = make_session(pending_optional_rfq={"entities": {"description": "x"}}, attachment_caption="urgent")
    assert (await h._proceed_to_confirmation_from_optional(u, s, "continue"))["status"] == "optional_fields_skipped"
    s = make_session(pending_rfq={"entities": {"description": "x"}})
    assert (await h._handle_rfq_acceptance(u, s, "yes"))["status"] == "multiple_rfqs_created"
    s = make_session()
    h._send_completion_response = AsyncMock()
    assert (await h._handle_rfq_acceptance(u, s, "yes"))["status"] == "multiple_rfqs_created"
    h.cancel_service.handle_cancel_intent = AsyncMock(return_value={"status": "restart"})
    assert (await h._handle_restart_workflow(u, s))["status"] == "restart"
    h.cancel_service = None
    assert (await h._handle_restart_workflow(u, s))["status"] == "error"
    h._bfs_search_handler = "bfs"
    assert h.bfs_search_handler == "bfs"
    h._extract_product_descriptions_from_rfq = MagicMock(return_value=[])
    await h._check_bfs_availability(u, s, [{"success": True}])
    h._extract_product_descriptions_from_rfq.side_effect = RuntimeError("noncritical")
    await h._check_bfs_availability(u, s, [{"success": True}])


# ---------------------------------------------------------------------------
# BFS handler


def bfs_handler():
    return bare(BFSSearchHandler, whatsapp_service=AsyncMock(), session_manager=AsyncMock(),
                 openai_service=AsyncMock(), auto_categorization_service=AsyncMock(), bfs_api_service=AsyncMock())


@pytest.mark.asyncio
async def test_bfs_search_payload_results_and_buttons(monkeypatch):
    h = bfs_handler(); u, s = make_user(), make_session()
    h.openai_service.extract_entities.return_value = {"success": True, "products": [{"description": "Laptop"}, {"description": ""}]}
    assert await h._extract_entities("laptop") == [{"description": "Laptop"}]
    h.openai_service.extract_entities.return_value = {"success": False}
    assert await h._extract_entities("raw") == [{"description": "raw"}]
    h.openai_service.extract_entities.side_effect = RuntimeError("ai")
    assert await h._extract_entities("raw") == [{"description": "raw"}]
    h.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "IT"}
    assert (await h._build_api_payload([{ "description": "x"}], "u", "s"))[0]["category"] == ["IT"]
    h.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "Other"}
    assert (await h._build_api_payload([{ "description": "x"}], "u", "s"))[0]["category"] == []
    h.auto_categorization_service.categorize_item.side_effect = RuntimeError("cat")
    assert await h._build_api_payload([{ "description": "x"}], "u", "s")
    h.bfs_api_service.search_bfs_items.return_value = {"success": True, "data": [{"description": "Laptop", "specification": "HP", "availableQuantity": 2, "sellPrice": 3}]}
    assert (await h.handle_bfs_search(u, s, "laptop"))["status"] == "bfs_search_completed"
    h.bfs_api_service.search_bfs_items.return_value = {"success": False, "error": "down"}
    assert (await h.handle_bfs_search(u, s, "laptop"))["status"] == "bfs_search_failed"
    h._extract_entities = AsyncMock(return_value=[])
    assert (await h.handle_bfs_search(u, s, "?"))["status"] == "no_products_found"
    await h._send_bfs_results(u, s, [], suppress_raise_rfq_on_no_results=True)
    await h._send_bfs_results(u, s, [])
    await h._send_bfs_results(u, s, {"not": "list"})
    h.whatsapp_service.send_configurable_buttons.side_effect = RuntimeError("send")
    await h._send_bfs_results(u, s, [{"description": "x", "availableQuantity": "bad"}])
    h.whatsapp_service.send_configurable_buttons.side_effect = None
    s = make_session(bfs_results=[])
    assert (await h.initiate_bid_flow(u, s))["status"] == "bfs_no_items_to_bid"
    monkeypatch.setattr(bfs_mod, "generate_bid_format", lambda _: "format")
    s.workflow_state["bfs_results"] = [{"id": "i", "description": "x"}]
    assert (await h.initiate_bid_flow(u, s))["status"] == "bfs_awaiting_bid_format"
    for button in ["search_bfs", "bfs_cancel", "bfs_bid_cancel", "bfs_restart", "unknown"]:
        result = await h.handle_button(u, make_session(bfs_results=[{"id": "i"}], bfs_searched_products=["x"]), button)
        assert result["status"] in {"bfs_awaiting_product_description", "bfs_send_cancel_message", "unknown_bfs_button"}
    h._clear_bid_state = AsyncMock()
    s = make_session(bfs_searched_products=["x"], bfs_results=[{"id": "i"}])
    assert (await h.handle_button(u, s, "bfs_raise_rfq"))["status"] == "bfs_activate_rfq"


@pytest.mark.asyncio
async def test_bfs_bid_otp_submission_and_cleanup(monkeypatch):
    h = bfs_handler(); u = make_user(); s = make_session(bfs_results=[{"id": "i", "sellPrice": 2, "description": "x"}])
    monkeypatch.setattr(bfs_mod, "parse_bid_format", lambda *_: {"error": "bad"})
    assert (await h.handle_bid_format_input(u, s, "bad"))["status"] == "bfs_bid_format_invalid"
    s.workflow_state["bfs_bid_retry_count"] = 2
    h._cancel_bid_after_max_retries = AsyncMock(return_value={"status": "max"})
    assert (await h.handle_bid_format_input(u, s, "bad"))["status"] == "max"
    s = make_session(bfs_results=[{"id": "i"}]); h._clear_bid_state = AsyncMock()
    assert (await h.handle_bid_format_input(u, s, "bad"))["status"] == "bfs_bid_format_invalid"
    monkeypatch.setattr(bfs_mod, "parse_bid_format", lambda *_: {"bids": [{"price": 1, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}}]})
    h._send_bid_otp = AsyncMock(return_value={"status": "otp"})
    assert (await h.handle_bid_format_input(u, s, "good"))["status"] == "otp"
    h._send_bid_otp = BFSSearchHandler._send_bid_otp.__get__(h)
    redis = AsyncMock(); redis.retrieve.return_value = None
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    assert (await h._send_bid_otp(u, s, [{"price": 1}]))["status"] == "bfs_bid_no_email"
    redis.retrieve.return_value = SimpleNamespace(email="a@x", otp_validated_at=9999999999, org_id="o", id="i")
    monkeypatch.setattr("time.time", lambda: 10000000000)
    h._submit_bids_to_api = AsyncMock(return_value={"status": "submitted"})
    assert (await h._send_bid_otp(u, s, [{"price": 1}]))["status"] == "submitted"

    redis.retrieve.return_value = SimpleNamespace(email="a@x", otp_validated_at=None, org_id="o", id="i", model_dump=lambda: {})
    otp = MagicMock(); otp.send_otp = AsyncMock(return_value={"status": "otp_sent"}); otp.validate_otp = AsyncMock(return_value={"status": "otp_valid"})
    monkeypatch.setattr("app.services.otp_service.OTPService", lambda **_: otp)
    monkeypatch.setattr("app.procucev_apis.register_apis.RegisterAPIService", lambda: MagicMock())
    monkeypatch.setattr("app.services.support_notification_service.SupportNotificationService", lambda: MagicMock())
    monkeypatch.setattr(bfs_mod, "generate_bid_summary", lambda _: "summary")
    assert (await h._send_bid_otp(u, s, [{"price": 1}]))["status"] == "bfs_bid_otp_sent"
    assert (await h.handle_bid_otp_input(u, s, "RESEND"))["status"] == "otp_sent"
    h._submit_bids_to_api = AsyncMock(return_value={"status": "submitted"})
    assert (await h.handle_bid_otp_input(u, s, "123456"))["status"] == "submitted"
    redis.retrieve.return_value = SimpleNamespace(email="a@x", otp_validated_at=None, org_id="o", id="i", model_dump=lambda: {})
    otp.validate_otp.return_value = {"status": "max_otp_exceeded"}
    assert (await h.handle_bid_otp_input(u, s, "bad"))["status"] == "bfs_bid_max_otp_retries"
    otp.validate_otp.return_value = {"status": "invalid"}
    assert (await h.handle_bid_otp_input(u, s, "bad"))["status"] == "bfs_bid_otp_invalid"

    h._submit_bids_to_api = BFSSearchHandler._submit_bids_to_api.__get__(h)
    s = make_session(bfs_bid_items=[])
    assert (await h._submit_bids_to_api(u, s))["status"] == "bfs_bid_no_items"
    s.workflow_state["bfs_bid_items"] = [{"price": 1, "quantity": 0, "original_item": {"id": "i"}}, {"price": 1, "quantity": 1, "original_item": {}}, {"price": 1, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}}]
    h.bfs_api_service.request_bfs_item.return_value = {"success": True}
    assert (await h._submit_bids_to_api(u, s))["status"] in {"bfs_bid_submitted", "bfs_bids_submitted", "bfs_bids_partial_success"}
    await h._clear_bid_state(s, clear_bfs_search=True)
    assert (await h._cancel_bid_flow(u, s, "reason"))["status"] == "bfs_send_cancel_message"
    h._cancel_bid_after_max_retries = BFSSearchHandler._cancel_bid_after_max_retries.__get__(h)
    assert (await h._cancel_bid_after_max_retries(u, s))["status"] == "bfs_bid_max_retries"
    await h._send_post_bid_menu(u, s, "buyer")
    await h._send_post_bid_menu(u, s, "seller")


# ---------------------------------------------------------------------------
# Purchase handlers

@pytest.mark.asyncio
async def test_purchase_intent_all_priority_routes(monkeypatch):
    h = PurchaseIntentHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    h.chat_summary_service.load_user_context.return_value = ["summary"]
    h.entity_service.extract_entities.return_value = {"products": [{"description": "x"}]}
    h.entity_service.extract_entities_with_summary_context.return_value = {"products": [{"description": "x"}]}
    h.products_array_handler.handle_products_array.return_value = {"status": "products"}
    assert (await h.handle_purchase_intent(make_user(), make_session(session_archive={"old": 1}), {"text": {"body": "buy"}}, {"intent": "buy_something"}, lambda _: True))["status"] == "products"
    h.entity_service.extract_entities_with_summary_context.return_value = {"products": [{"description": "x"}]}
    h.products_array_handler.handle_products_array.return_value = {"status": "single"}
    assert (await h.handle_purchase_intent(make_user(), make_session(), "x", {"intent": "modification_request"}, lambda _: True))["status"] == "single"
    h.entity_service.extract_entities.return_value = {"error_type": "quantity_limit", "quantity_violations": [{"description": "x", "quantity": 100000001}]}
    assert (await h.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "quantity_limit_violation"
    h.entity_service.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await h.handle_purchase_intent(make_user(), make_session(), "change"))["status"] == "modification_clarification_sent"
    h.entity_service.extract_entities.return_value = {"error_type": "non_procurable", "non_procurable_items": ["service", "advice"]}
    monkeypatch.setattr("app.services.cancel_service.CancelService", MagicMock())
    assert (await h.handle_purchase_intent(make_user(), make_session(), "x"))["status"] in {"non_procurable_cancelled", "error"}
    h.entity_service.extract_entities.return_value = {}
    assert (await h.handle_purchase_intent(make_user(), make_session(workflow_type="modification_request"), "x"))["status"] == "modification_request_no_data"
    h.entity_service.extract_entities.side_effect = RuntimeError("down")
    assert (await h.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "error"
    assert (await h._handle_quantity_limit_violations(make_user(), make_session(), {"quantity_violations": [{"description": "a", "quantity": 2}, {"description": "b", "quantity": 3}]}))["status"] == "quantity_limit_violation"
    assert (await h._handle_error_response(RuntimeError("x"), "+1"))["status"] == "error"
    assert "No product" in h._format_captured_info_for_modification(make_session())


@pytest.mark.asyncio
async def test_purchase_sectioned_lazy_and_workflow_stubs(monkeypatch):
    h = PurchaseIntentHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    monkeypatch.setattr(purchase_mod.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(purchase_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    fake_sectioned = MagicMock(); fake_sectioned.handle_sectioned_rfq = AsyncMock(return_value={"status": "sectioned"})
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", lambda **_: fake_sectioned)
    monkeypatch.setattr("app.services.cancel_service.CancelService", MagicMock())
    assert (await h.handle_purchase_intent(make_user(), make_session(), "buy"))["status"] == "sectioned"
    p = PurchaseWorkflowHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    p.entity_service.extract_entities.return_value = {"products": [{"description": "x"}]}
    p._handle_products_array = AsyncMock(return_value={"status": "products"})
    assert (await p.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "products"
    p.entity_service.extract_entities.return_value = {"entities": {"description": "x"}}
    p._handle_single_entity = AsyncMock(return_value={"status": "single"})
    assert (await p.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "single"
    p.entity_service.extract_entities.return_value = {}
    assert (await p.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "no_new_entities"
    assert await PurchaseWorkflowHandler._handle_products_array(p, make_user(), make_session(), "x", []) is None
    assert await PurchaseWorkflowHandler._handle_single_entity(p, make_user(), make_session(), "x", {}) is None
    p.entity_service.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await p.handle_purchase_intent(make_user(), make_session(), "x"))["status"] == "modification_clarification_sent"


# ---------------------------------------------------------------------------
# RFQ background and intimation services


def background_service(monkeypatch):
    background_mod.RFQBackgroundService._instance = None
    background_mod.RFQBackgroundService._initialized = False
    db = DB()
    monkeypatch.setattr(background_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(background_mod, "SellerRecommendationService", lambda *_: MagicMock())
    monkeypatch.setattr(background_mod, "RFQIntimationService", lambda *_: MagicMock())
    return background_mod.RFQBackgroundService(db), db


@pytest.mark.asyncio
async def test_background_processing_cleanup_priority_and_stats(monkeypatch):
    service, db = background_service(monkeypatch)
    service._fetch_rfq_data = AsyncMock(return_value=None)
    assert not (await service.process_approved_rfq("missing"))["success"]
    service._fetch_rfq_data = AsyncMock(return_value={"rfq_id": "R", "rfq_title": "R"})
    service.recommendation_service.select_sellers_for_rfq = AsyncMock(return_value={"total_selected": 0, "subscribed_sellers": [], "unsubscribed_sellers": []})
    assert (await service.process_approved_rfq("R"))["sellers_selected"] == 0
    service.recommendation_service.select_sellers_for_rfq.return_value = {"total_selected": 1, "subscribed_sellers": [{"seller_id": "s", "seller_name": "S"}], "unsubscribed_sellers": [], "selection_metadata": {}}
    service._send_batch_notifications = AsyncMock(return_value={"successful": 1, "failed": 0, "details": []})
    service._update_rfq_status = AsyncMock()
    assert (await service.process_approved_rfq("R"))["success"]
    service.process_approved_rfq = AsyncMock(side_effect=[{"success": True, "notifications_sent": 1}, RuntimeError("bad")])
    assert (await service.process_multiple_rfqs(["R1", "R2"]))["failed_rfqs"] == 1
    service.intimation_service.send_rfq_notification = AsyncMock(side_effect=[{"success": True, "message_id": "m"}, RuntimeError("bad")])
    batch = await service._send_batch_notifications({"rfq_id": "R"}, [{"seller_id": "s", "seller_name": "S"}, {"seller_id": "t", "seller_name": "T"}], "job")
    assert batch.get("successful", 0) == 1 or batch.get("success") is False
    service._fetch_rfq_data = background_mod.RFQBackgroundService._fetch_rfq_data.__get__(service)
    rfq = SimpleNamespace(rfq_id="R", api_payload={"rfq_title": "Title", "categories": ["Medical Equipment"], "deadline": (date.today() + timedelta(days=3)).isoformat()}, status=RFQStatus.ready, created_at=datetime.utcnow() - timedelta(days=2), external_user_id="u")
    db.query_obj = Query(first=rfq)
    assert (await service._fetch_rfq_data("R"))["rfq_title"] == "Title"
    db.query_obj = Query(first=None)
    assert await service._fetch_rfq_data("R") is None
    db.query_obj = Query(scalar_value=0)
    assert (await service.cleanup_old_notifications())["notifications_deleted"] == 0
    db.query_obj = Query(scalar_value=1, delete_value=1)
    assert (await service.cleanup_old_notifications())["success"]
    db.query_obj = Query(first=rfq, all_values=[rfq], scalar_value=0)
    assert (await service.get_pending_rfqs())[0]["priority"] >= 100
    assert service._calculate_rfq_priority(rfq) > 100
    assert service._calculate_rfq_priority(SimpleNamespace(api_payload={"deadline": "bad", "categories": []}, created_at=datetime.utcnow())) >= 100
    await service._update_rfq_status("R", RFQStatus.submitted)
    db.query_obj = Query(first=None)
    await service._update_rfq_status("R", RFQStatus.submitted)
    db.query_obj = Query(first=rfq); db.query_obj.first_value = rfq
    db.commit.side_effect = RuntimeError("commit")
    await service._update_rfq_status("R", RFQStatus.submitted)
    assert service.get_job_statistics()["total_jobs_tracked"] >= 1
    assert isinstance(service.get_active_jobs(), list)


@pytest.mark.asyncio
async def test_intimation_all_seller_payment_email_timeout_helpers(monkeypatch):
    db = DB(); wa = AsyncMock(); opt = AsyncMock()
    monkeypatch.setattr(intimation_mod, "get_settings", lambda: SimpleNamespace(support_contact_info="support", contact_email="email"))
    monkeypatch.setattr(intimation_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(intimation_mod, "OptOutService", lambda *_: opt)
    service = intimation_mod.RFQIntimationService(db); seller = SimpleNamespace(seller_id="s", seller_name="Seller", phone_number="+1", email="s@x", subscription_credits=2)
    service._get_seller_details = AsyncMock(return_value=None)
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["error"] == "Seller not found"
    service._get_seller_details.return_value = seller
    opt.check_seller_notification_eligibility.return_value = {"eligible": False, "reason": "opted out"}
    assert not (await service.send_rfq_notification("s", {"rfq_id": "R"}))["success"]
    opt.check_seller_notification_eligibility.return_value = {"eligible": False, "action": "send_permission_request"}
    opt.send_permission_request.return_value = {"sent": True}
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["action"] == "permission_request_sent"
    opt.check_seller_notification_eligibility.return_value = {"eligible": True}
    wa.send_message.return_value = SimpleNamespace(success=True, message_id="m", error=None)
    service._record_notification = AsyncMock(); service._record_interaction = AsyncMock(); service._start_conversation_timeout = AsyncMock()
    assert (await service.send_rfq_notification("s", {"rfq_id": "R", "rfq_title": "T", "categories": ["A"], "quantity_info": "2"}))["success"]
    seller.subscription_credits = 0
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["next_step"] == "subscription_selection"
    assert (await service.handle_subscription_selection("s", "R", "bad"))["error"] == "Invalid subscription plan"
    service.mock_procurev = MagicMock(); service.mock_procurev.generate_payment_link = AsyncMock(return_value={"success": False, "error": "down"})
    assert not (await service.handle_subscription_selection("s", "R", "basic"))["success"]
    service.mock_procurev.generate_payment_link.return_value = {"success": True, "payment_link": "pay", "payment_id": "p"}
    assert (await service.handle_subscription_selection("s", "R", "basic"))["success"]
    service._validate_rfq_id = AsyncMock(return_value=False)
    assert (await service.handle_rfq_id_request("s", "bad", "bad"))["error"] == "Invalid RFQ ID"
    service._validate_rfq_id.return_value = True; seller.subscription_credits = 0
    assert (await service.handle_rfq_id_request("s", "R", "R"))["error"] == "Insufficient credits"
    seller.subscription_credits = 2; service.mock_procurev.send_rfq_email = AsyncMock(return_value={"success": False, "error": "mail"})
    assert not (await service.handle_rfq_id_request("s", "R", "R"))["success"]
    service.mock_procurev.send_rfq_email.return_value = {"success": True, "email_id": "e"}; service._deduct_seller_credit = AsyncMock(); service._update_notification_response = AsyncMock()
    assert (await service.handle_rfq_id_request("s", "R", "R"))["success"]
    service.mock_procurev.get_seller_pending_bids = AsyncMock(return_value={"success": True, "pending_bids": [{"rfq_title": "T", "rfq_id": "R", "deadline": "d"}] * 4})
    service._record_interaction = AsyncMock(); service._update_notification_response = AsyncMock()
    assert (await service.handle_conversation_timeout("s", "R"))["pending_bids_count"] == 4
    service.mock_procurev.get_seller_pending_bids.return_value = {"success": True, "pending_bids": []}
    assert (await service.handle_conversation_timeout("s", "R"))["pending_bids_count"] == 0
    service.mock_procurev.get_seller_pending_bids.return_value = {"success": False}
    assert (await service.handle_conversation_timeout("s", "R"))["success"]
    assert "A" in await service._generate_rfq_brief({"rfq_title": "T", "categories": ["A"], "quantity_info": "2"})
    assert "Credits" in await service._create_credited_seller_message(seller, "brief", {"rfq_id": "R"})
    assert "Basic" in await service._create_uncredited_seller_message(seller, "brief")
    monkeypatch.setattr(intimation_mod.asyncio, "create_task", MagicMock())
    await service._start_conversation_timeout("s", "R")
    await service._record_notification("s", "R", "m"); await service._record_interaction("s", "R", InteractionType.notification_sent, {})
    service._validate_rfq_id = intimation_mod.RFQIntimationService._validate_rfq_id.__get__(service)
    db.query_obj = Query(first=SimpleNamespace()); assert await service._validate_rfq_id("R")
    db.query_obj = Query(first=None); assert not await service._validate_rfq_id("R")
    seller.subscription_credits = 1; db.query_obj = Query(first=seller); await service._deduct_seller_credit("s")
    service._update_notification_response = intimation_mod.RFQIntimationService._update_notification_response.__get__(service)
    notification = SimpleNamespace(response_type=None, responded_at=None); db.query_obj = Query(first=notification); await service._update_notification_response("s", "R", ResponseType.ignored); assert notification.response_type == ResponseType.ignored


# ---------------------------------------------------------------------------
# Webhook health monitor


def health_service():
    h = bare(health_mod.WebhookHealthMonitorService,
             settings=SimpleNamespace(WHATSAPP_BASE_URL="http://wa", WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p", webhook_alert_state_ttl_seconds=60, webhook_health_monitoring_enabled=True),
             redis=AsyncMock(), email_service=AsyncMock(), response_threshold=1, api_timeout=3, grace_period=0,
             recovery_confirmations=2, warning_threshold=2, alert_recipients=[], worker_id="worker",
             is_leader=False, _running=False, _session=None, check_interval=1)
    return h


@pytest.mark.asyncio
async def test_health_http_lock_state_history_and_alerts(monkeypatch):
    h = health_service()
    response = SimpleNamespace(status=200)
    session = MagicMock(); session.closed = False; session.post.return_value = AsyncContext(response); h._session = session
    assert (await h._check_api_health())[0] == health_mod.HealthStatus.OK
    session.post.return_value = AsyncContext(SimpleNamespace(status=500)); assert (await h._check_api_health())[0] == health_mod.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(SimpleNamespace(status=400)); assert (await h._check_api_health())[0] == health_mod.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(SimpleNamespace(status=201)); assert (await h._check_api_health())[0] == health_mod.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(error=asyncio.TimeoutError()); assert (await h._check_api_health())[0] == health_mod.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(error=RuntimeError("bad")); assert (await h._check_api_health())[0] == health_mod.HealthStatus.CRITICAL
    h.redis.init_client = AsyncMock(); h.redis.client = SimpleNamespace(set=AsyncMock(return_value=True)); h.redis.get = AsyncMock(return_value="other"); h.redis.expire = AsyncMock(); h.redis.delete = AsyncMock()
    assert await h._try_acquire_leader_lock(); h.redis.client.set.return_value = False; assert not await h._try_acquire_leader_lock()
    h.redis.get.return_value = "worker"; assert await h._try_acquire_leader_lock(); assert await h._renew_leader_lock(); h.redis.get.return_value = "other"; assert not await h._renew_leader_lock(); await h._release_leader_lock()
    h.redis.get = AsyncMock(return_value=None); assert (await h._get_state())["current_state"] == "HEALTHY"
    h.redis.get.return_value = "{bad"; assert (await h._get_state())["current_state"] == "HEALTHY"
    h.redis.set = AsyncMock(); await h._save_state({"x": 1}); assert h.redis.set.await_count
    h.redis.set.side_effect = RuntimeError("redis"); await h._save_state({"x": 1})
    h.redis.get.return_value = json.dumps([{"old": i} for i in range(100)]); h.redis.set.side_effect = None
    await h._add_to_history(health_mod.HealthStatus.OK, 1.2, None)
    state = {"current_state": "HEALTHY", "is_alerting": False, "last_alert_time": None, "failure_start_time": None, "consecutive_failures": 0, "consecutive_successes": 0, "consecutive_warnings": 0, "last_severity": "OK", "last_check_time": None, "last_latency_ms": None, "last_error": None}
    h._save_state = AsyncMock(); h._send_critical_alert = AsyncMock(); h._send_warning_alert = AsyncMock(); h._send_relapse_alert = AsyncMock(); h._send_recovery_notification = AsyncMock()
    await h._process_check_result(state, health_mod.HealthStatus.CRITICAL, 2, "down"); assert state["current_state"] == "FAILING"
    state["failure_start_time"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat(); await h._process_check_result(state, health_mod.HealthStatus.CRITICAL, 2, "down"); assert state["current_state"] == "ALERTING"
    await h._handle_ok_status(state, health_mod.MonitorState.ALERTING); await h._handle_ok_status(state, health_mod.MonitorState.RECOVERED)
    state["current_state"] = "HEALTHY"; await h._handle_warning_status(state, health_mod.MonitorState.HEALTHY); state["failure_start_time"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat(); state["consecutive_warnings"] = 1; await h._handle_warning_status(state, health_mod.MonitorState.FAILING)
    await h._handle_critical_status(state, health_mod.MonitorState.RECOVERED)
    await h._send_critical_alert(state); await h._send_warning_alert(state); await h._send_recovery_notification(state); await h._send_relapse_alert(state)
    assert health_mod.WebhookHealthMonitorService._format_duration(timedelta(hours=1, minutes=2, seconds=3)) == "1h 2m 3s"
    assert health_mod.WebhookHealthMonitorService._format_duration(timedelta()) == "0s"


@pytest.mark.asyncio
async def test_health_monitor_loops_sessions_and_email_branches(monkeypatch):
    h = health_service(); h.settings.webhook_health_monitoring_enabled = False
    await h.start_monitoring(); h._running = True; await h.start_monitoring(); await h.stop_monitoring()
    h = health_service(); h._running = True; h._try_acquire_leader_lock = AsyncMock(return_value=False); h._interruptible_sleep = AsyncMock(side_effect=lambda _: setattr(h, "_running", False)); await h.start_monitoring()
    h = health_service(); h._renew_leader_lock = AsyncMock(return_value=False); h._running = True; await h._run_as_leader(); assert not h.is_leader
    h = health_service(); h.alert_recipients = ["a@x"]; h.email_service.send_email_by_template.return_value = {"status": "Success"}
    state = {"failure_start_time": datetime.utcnow().isoformat(), "consecutive_failures": 2, "last_error": "down", "last_check_time": "now", "last_latency_ms": 2, "consecutive_warnings": 2}
    await h._send_critical_alert(state); await h._send_warning_alert(state); await h._send_recovery_notification(state); await h._send_relapse_alert(state)
    h.email_service.send_email_by_template.side_effect = RuntimeError("mail"); await h._send_critical_alert(state)
    fake = MagicMock(); fake.closed = False; fake.close = AsyncMock(); h._session = fake; await h._close_session(); assert h._session is None
    h._running = True; await h._interruptible_sleep(0)


# ---------------------------------------------------------------------------
# Enhanced categorization


def categorization_service():
    c = bare(categorization_mod.EnhancedAutoCategorizationService, collection=MagicMock(), category_collection=MagicMock(), fallback_service=MagicMock(), openai_service=MagicMock())
    return c


def test_categorization_keyword_cross_validation_hybrid_hierarchy(monkeypatch):
    c = categorization_service()
    assert c._build_enhanced_description("Battery", {"level_3_category": "Battery", "level_2_category": "Power"}) == "Battery Power"
    monkeypatch.setattr(categorization_mod, "execute_remote_query", lambda *_: [{"category": "Power", "freq": 2}])
    assert c._keyword_lookup_source_of_truth("power")["success"]
    monkeypatch.setattr(categorization_mod, "execute_remote_query", lambda *_: [])
    assert not c._keyword_lookup_source_of_truth("unknown")["success"]
    c._keyword_lookup_source_of_truth = MagicMock(return_value={"success": True, "category": "Other", "consensus": .5})
    assert c._cross_validate_with_fallback("x", "Tools", .5)["recommended_category"] == "Other"
    c._keyword_lookup_source_of_truth.return_value = {"success": False}; c.fallback_service.collection.query.return_value = {"metadatas": [[]], "distances": [[]]}
    assert c._cross_validate_with_fallback("x", "Tools", .5)["use_learning"]
    c.fallback_service.collection.query.return_value = {"metadatas": [[{"category": "Tools"}, {"category": "Other"}]], "distances": [[.2, .8]]}
    assert c._cross_validate_with_fallback("x", "Tools", .5)["validated"]
    c.category_collection.count.return_value = 0; assert not c._search_by_category_name("x")["success"]
    c.category_collection.count.return_value = 1; c.category_collection.query.return_value = {"documents": [["Tools"]], "metadatas": [[{"item_count": 2}]], "distances": [[.2]]}
    assert c._search_by_category_name("x")["best_match"]["category_name"] == "Tools"
    assert c._hybrid_category_selection("Tools", .8, {"success": True, "matches": [{"category_name": "Tools", "similarity": .8}]})["agreement"]
    assert c._hybrid_category_selection("Tools", .9, {"success": True, "matches": [{"category_name": "Other", "similarity": .9}]})["method"] == "hybrid_item_trusted"
    assert c._hybrid_category_selection("Tools", .3, {"success": True, "matches": [{"category_name": "Other", "similarity": .8}]})["method"] in {"hybrid_category_override", "hybrid_item_preferred"}
    meta = {"level_3_category": "L3", "level_2_category": "L2", "level_1_category": "L1", "client_category_name": "Tools"}
    c.collection.query.return_value = {"documents": [["x"]], "metadatas": [[meta]], "distances": [[.2]]}
    assert c._search_hierarchical_levels("x")["success"]
    c.collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert not c._search_hierarchical_levels("x")["success"]


@pytest.mark.asyncio
async def test_categorization_pipeline_learning_logging_health_stats(monkeypatch):
    c = categorization_service(); c.chroma_path = "mock"
    c._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    c._search_hierarchical_levels = MagicMock(return_value={"success": True, "similarity_score": .95, "best_match": {"client_category_name": "Tools"}, "all_level_matches": [{"metadata": {"client_category_name": "Tools", "item_description": "bolt"}, "similarity_score": .95, "matched_level": "level_3"}]})
    c._log_categorization = MagicMock()
    assert (await c.categorize_item("bolt", "u"))["method"] == "enhanced_taxonomy_high_similarity"
    c._search_hierarchical_levels.return_value = {"success": False}; c.fallback_service._get_similar_items.return_value = []
    c._log_fallback_categorization = MagicMock(); assert (await c.categorize_item("x", "u"))["client_category"] == "Other"
    c.fallback_service._get_similar_items.return_value = [{"category": "Tools", "similarity_score": .7}]
    c.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Tools", "confidence": .8})
    c._update_learning_taxonomy = AsyncMock(return_value=True)
    assert (await c.categorize_item("x", "u"))["method"] == "enhanced_fallback_openai"
    c._keyword_lookup_source_of_truth.side_effect = RuntimeError("bad"); assert (await c.categorize_item("x", "u"))["method"] == "enhanced_error"
    c.collection.count.return_value = 1; c.category_collection.count.return_value = 2; c.fallback_service.get_collection_stats.return_value = {"n": 1}; assert c.health_check()["overall_status"] == "healthy"
    c.collection.count.side_effect = RuntimeError("chroma"); assert c.health_check()["overall_status"] == "unhealthy"
    c.collection.count.side_effect = None; c.collection.count.return_value = 2; c.category_collection.count.return_value = 3; c.fallback_service.get_collection_stats.return_value = {"n": 1}; assert c.get_stats()["unified_vector_store"]["total_items"] == 2
    c.collection.count.side_effect = RuntimeError("db"); assert "error" in c.get_stats()
    c._update_learning_taxonomy = categorization_mod.EnhancedAutoCategorizationService._update_learning_taxonomy.__get__(c)
    monkeypatch.setattr("app.services.learning_categorization_service.LearningCategorizationService", lambda: SimpleNamespace(create_3_level_category=AsyncMock(return_value={"success": True})))
    assert await c._update_learning_taxonomy("x", "Tools", "u")
    db = DB(); monkeypatch.setattr(categorization_mod, "get_db_session", lambda: db)
    c._log_categorization = categorization_mod.EnhancedAutoCategorizationService._log_categorization.__get__(c)
    c._log_categorization("x", "u", None, None, "Tools", .8, .5, "manual", 1)
    assert db.added
    c._log_fallback_categorization = categorization_mod.EnhancedAutoCategorizationService._log_fallback_categorization.__get__(c)
    c._log_fallback_categorization("x", "u", None, None, "Tools", .5, "fallback", 1, "no_match")


# ---------------------------------------------------------------------------
# Excel report service


def report_service():
    return bare(report_mod.EnhancedExcelReportService, settings=SimpleNamespace())


class Writer:
    def __enter__(self): return self
    def __exit__(self, *args): return False



def test_excel_helpers_queries_metrics_formatting_and_generation(monkeypatch):
    r = report_service(); assert r.sanitize_for_excel("a\x00b\x7fc") == "abc" and r.sanitize_for_excel(None) == ""
    assert r._get_empty_metrics()["total_rfqs"] == 0 and r._get_metric_value("Total RFQs Submitted", {"total_rfqs": 3}) == 3
    assert r._get_seller_metric_value("RFQs Requested", {"rfqs_requested": 2}) == 2
    monkeypatch.setattr(report_mod, "get_db_session_context", lambda: DBContext(DB()))
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Seller Chats Initiated", 4]], columns=["metric_name", "total_value"])))
    assert r._calculate_seller_metrics(DB(), date(2024, 1, 1), date(2024, 1, 2))["seller_chats_initiated"] == 4
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(side_effect=RuntimeError("db")))
    assert r._calculate_seller_metrics(DB(), date(2024, 1, 1), date(2024, 1, 2))["rfqs_requested"] == 0
    assert r._generate_category_details_fallback(DB(), date(2024, 1, 1), date(2024, 1, 2)).shape[0] == 3
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([[{"total_rfqs": 2}]])))
    assert r._get_rolling_window_metrics(DB(), date(2024, 1, 1), 7) is None
    assert r._get_rolling_window_metrics(DB(), date(2024, 1, 1), 2) is None
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Total RFQs Submitted", 2], ["Total Items in All RFQs", 4]], columns=["metric_name", "value"])))
    assert r._calculate_buyer_metrics(DB(), date(2024, 1, 1), date(2024, 1, 2))["avg_products_per_rfq"] == 2
    monkeypatch.setattr(report_mod.pd, "ExcelWriter", MagicMock(return_value=Writer()))
    for name in ["_generate_buyer_details_sheet", "_generate_seller_details_sheet", "_generate_category_details_sheet", "_generate_buyer_summary_sheet", "_generate_seller_summary_sheet", "_generate_category_summary_sheet", "_generate_aggregate_sheet_90d", "_format_excel_sheets"]:
        setattr(r, name, MagicMock())
    assert r.generate_report(date(2024, 1, 1), "report.xlsx") == "report.xlsx"
    for name in ["_generate_buyer_details_sheet", "_generate_seller_details_sheet", "_generate_category_details_sheet", "_generate_buyer_summary_sheet", "_generate_seller_summary_sheet", "_generate_category_summary_sheet", "_generate_aggregate_sheet_90d", "_format_excel_sheets"]:
        assert getattr(r, name).called


@pytest.mark.asyncio
async def test_excel_email_and_formatting_error_paths(monkeypatch):
    r = report_service()
    worksheet = MagicMock()
    worksheet.__iter__.return_value = iter([])
    book = MagicMock()
    book.sheetnames = ["Sheet1"]
    book.__getitem__.return_value = worksheet
    writer = SimpleNamespace(book=book)
    r._format_excel_sheets(writer)
    r._get_rolling_window_metrics = MagicMock(return_value=None); r._calculate_buyer_metrics = MagicMock(return_value=None)
    monkeypatch.setattr(report_mod, "get_db_session_context", lambda: DBContext(DB()))
    monkeypatch.setattr(report_mod.pd.DataFrame, "to_excel", MagicMock())
    r._generate_buyer_summary_sheet(Writer(), date(2024, 1, 1))
    api = AsyncMock(); api.send_email_by_template = AsyncMock(return_value={"status": "Success"})
    monkeypatch.setattr(report_mod, "EmailService", lambda: api)
    monkeypatch.setattr("app.procucev_apis.procucev_api_client.init_procucev_api_client", AsyncMock())
    monkeypatch.setattr("app.procucev_apis.procucev_api_client.close_procucev_api_client", AsyncMock())
    fake = MagicMock(); fake.__enter__.return_value = fake; fake.__exit__.return_value = False; fake.read.return_value = b"xlsx"
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: fake)
    await r._send_report_email("report.xlsx", date(2024, 1, 1))
    api.send_report_email.side_effect = RuntimeError("mail"); await r._send_report_email("report.xlsx", date(2024, 1, 1))
