"""Deterministic direct-call coverage for handler state-machine branches."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.handlers.auth_registration_intent_switch as switch_mod
import app.services.handlers.authentication_orchestrator as auth_mod
import app.services.handlers.confirmation_handler as confirmation_mod
import app.services.handlers.products_array_handler as products_mod
import app.services.handlers.purchase_intent_handler as purchase_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.handlers.seller_auth_mixin as seller_auth_mod
import app.services.handlers.seller_rfq_interest_handler as seller_interest_mod
from app.models import ConversationOutcome, WorkflowType
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler


def session(**state):
    return SimpleNamespace(
        session_id="sid", external_user_id="uid", phone_number="+91123",
        workflow_type=None, workflow_state=dict(state), conversation_history={},
        product_items=[], extracted_entities=[], outcome=None, completed_at=None,
        rfq_ids=[], user_type=None, rfq_id=None, created_at=None,
    )


def user(role="buyer", registered=True):
    return SimpleNamespace(
        id="u1", org_id="o1", phone_number="+91123", role=role,
        is_registered=registered, self_client=role == "buyer",
    )


def bare(cls, **attrs):
    obj = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(obj, name, value)
    return obj


def patch_sections(monkeypatch):
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, name: s.workflow_state.get(name))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda s, name, value: s.workflow_state.__setitem__(name, value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda s: bool(s.workflow_state.get("excel_source")))


def sectioned_handler():
    h = bare(
        SectionedRFQCreationHandler,
        entity_service=AsyncMock(), whatsapp_service=AsyncMock(),
        cancel_service=AsyncMock(), session_manager=AsyncMock(),
        confirmation_handler=AsyncMock(), attachment_decision_handler=None,
    )
    h.entity_service.openai_service = SimpleNamespace(validate_delivery_date=AsyncMock())
    return h


@pytest.mark.asyncio
async def test_sectioned_all_sections_payload_normalization_and_restart_states(monkeypatch):
    h = sectioned_handler()
    s = session()
    u = user()
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda *_: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "date_location")
    h._handle_date_location_section = AsyncMock(return_value={"status": "date"})
    h._handle_items_section = AsyncMock(return_value={"status": "items"})
    h._handle_attachments_section = AsyncMock(return_value={"status": "attachments"})
    h._handle_final_confirmation = AsyncMock(return_value={"status": "final"})

    for payload in (
        {"button_reply": {"title": "button"}},
        {"button_reply": {"id": "button-id"}},
        {"text": {"body": "text"}}, {"text": "plain"},
        {"content": "content"}, {"other": "value"}, None, 4,
    ):
        result = await h.handle_sectioned_rfq(u, s, payload, [])
        assert result["status"] == "date"
    for name, expected in (("items", "items"), ("attachments", "attachments"), ("final_confirmation", "final")):
        monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_s, name=name: name)
        assert (await h.handle_sectioned_rfq(u, s, "message", []))["status"] == expected
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "unknown")
    assert (await h.handle_sectioned_rfq(u, s, "message", []))["status"] == "error"

    h._handle_restart_confirmation_response = AsyncMock(return_value={"status": "restart"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda *_: True)
    assert (await h.handle_sectioned_rfq(u, s, {"text": {"body": "yes"}}, []))["status"] == "restart"


@pytest.mark.asyncio
async def test_sectioned_date_items_retry_validation_and_cancellation_branches(monkeypatch):
    h = sectioned_handler(); patch_sections(monkeypatch); u = user()
    h._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmation"})
    h._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    h._display_delivery_validation_error = AsyncMock(return_value={"status": "validation"})
    h._display_invalid_pincode_message = AsyncMock(return_value={"status": "pincode"})

    h.entity_service.extract_entities.return_value = {"deliveryDate": "tomorrow", "pincode": "560001", "products": []}
    h._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "2 January 2030"})
    h._autofill_location_from_pincode = AsyncMock(return_value={"is_valid": True, "delivery_data": {"deliveryDate": "2 January 2030", "pincode": "560001", "city": "Pune", "state": "MH"}})
    assert (await h._handle_date_location_section(u, session(), "details", []))["status"] == "confirmation"

    h.entity_service.extract_entities.return_value = {"deliveryDate": "bad", "pincode": "560001", "products": []}
    h._validate_delivery_date.return_value = {"is_valid": False, "error": "bad date"}
    assert (await h._handle_date_location_section(u, session(), "details", []))["status"] == "validation"

    h.entity_service.extract_entities.return_value = {"deliveryDate": "", "pincode": "", "products": []}
    assert (await h._handle_date_location_section(u, session(), "details", []))["status"] == "awaiting_delivery_details"
    # Delivery basics already stored, so the extraction block is skipped. The validation
    # error flags must still be bound; date+pincode without city/state means the pincode
    # lookup failed, so the user is asked for a valid pincode.
    stored_basics = session(date_location={"deliveryDate": "d", "pincode": "p"})
    assert (await h._handle_date_location_section(u, stored_basics, "details", []))["status"] == "pincode"
    # An empty message carries nothing to extract, so no OpenAI round trip is spent.
    h.entity_service.extract_entities.reset_mock()
    assert (await h._handle_date_location_section(u, session(), "", []))["status"] == "awaiting_delivery_details"
    h.entity_service.extract_entities.assert_not_called()

    parser = sectioned_mod.sectioned_rfq_format_parser
    monkeypatch.setattr(parser, "parse_delivery_format", lambda _: {"error": "bad format", "additional_text": ""})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 1)
    assert (await h._process_delivery_modification_direct(u, session(), "Date: bad"))["status"] == "format_error"
    h._cancel_after_max_retries = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 3)
    assert (await h._process_delivery_modification_direct(u, session(), "Pincode: bad"))["status"] == "cancelled"

    h.entity_service.extract_entities.return_value = {"deliveryDate": "", "pincode": "", "products": []}
    h._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry", lambda *_: 0)
    assert (await h._process_delivery_modification_direct(u, session(), "What is the format?"))["status"] == "missing"

    monkeypatch.setattr(parser, "parse_delivery_format", lambda _: {"deliveryDate": "2025-02-03", "pincode": "560001", "additional_text": ""})
    h._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "3 February 2025"})
    h._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": True, "city": "Pune", "state": "MH"})
    assert (await h._process_delivery_modification_direct(u, session(), "format"))["status"] == "confirmation"
    for date_ok, pin_ok in ((False, False), (False, True), (True, False)):
        h._validate_delivery_date.return_value = {"is_valid": date_ok, "error": "date"}
        h._validate_pincode_and_get_location.return_value = {"is_valid": pin_ok, "error": "pin"}
        assert (await h._process_delivery_modification_direct(u, session(), "format"))["status"] == "validation_error"

    h.entity_service.extract_entities.return_value = {"products": []}
    assert (await h._handle_items_section(u, session(), "x", []))["status"] == "awaiting_items"
    h.entity_service.extract_entities.return_value = {"products": [{"description": "x", "quantity": 1}]}
    h._display_items_confirmation = AsyncMock(return_value={"status": "items-confirmed"})
    assert (await h._handle_items_section(u, session(), "x", []))["status"] == "items-confirmed"
    h.entity_service.extract_entities.return_value = {"products": [{"description": "x"}]}
    h._display_items_missing_fields = AsyncMock(return_value={"status": "items-missing"})
    assert (await h._handle_items_section(u, session(), "x", []))["status"] == "items-missing"
    h.entity_service.extract_entities.return_value = {"products": [{"description": str(i), "quantity": 1} for i in range(6)]}
    h.cancel_service._clear_workflow_state = AsyncMock(); h.cancel_service._send_cancellation_message = AsyncMock()
    assert (await h._handle_items_section(u, session(), "x", []))["status"] == "item_limit_exceeded_cancelled"


@pytest.mark.asyncio
async def test_sectioned_attachments_final_buttons_and_restart(monkeypatch):
    h = sectioned_handler(); patch_sections(monkeypatch); u = user()
    h._build_combined_rfq_from_sections = MagicMock(return_value={"combined_schema": {}, "products": []})
    first = session(date_location={"deliveryDate": "d"}, items=[{"description": "x", "quantity": 1}])
    assert (await h._handle_attachments_section(u, first, "", []))["status"] == "awaiting_attachments_decision"
    h.confirmation_handler.handle_optional_fields_response.return_value = {"status": "optional"}
    second = session(sectioned_rfq_attachment_question_asked=True, pending_combined_rfq={"combined_schema": {}})
    assert (await h._handle_attachments_section(u, second, "continue", []))["status"] == "optional"
    assert second.workflow_state.get("sectioned_rfq") is not None or second.workflow_state.get("current_section") == "final_confirmation"

    h.confirmation_handler.handle_pending_confirmations.return_value = {"status": "pending"}
    final = session(date_location={"deliveryDate": "d"}, items=[])
    assert (await h._handle_final_confirmation(u, final, "yes"))["status"] == "pending"
    assert "pending_combined_rfq" in final.workflow_state

    h._handle_section_confirm = AsyncMock(return_value={"status": "confirmed"})
    h._handle_section_modify = AsyncMock(return_value={"status": "modified"})
    h._handle_restart_rfq = AsyncMock(return_value={"status": "restart"})
    h._handle_final_rfq_submission = AsyncMock(return_value={"status": "submitted"})
    h._handle_final_cancel = AsyncMock(return_value={"status": "cancelled"})
    h._handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    for button, expected in (("confirm_items", "confirmed"), ("modify_items", "modified"), ("restart_rfq", "restart"), ("confirm_cancel", "restart"), ("decline_cancel", "restart"), ("final_confirm_rfq", "submitted"), ("final_cancel_rfq", "cancelled"), ("attachments_yes", "attachment"), ("bad", "error")):
        assert (await h.handle_section_button_click(u, session(), button))["status"] == expected

    h._handle_final_rfq_submission = SectionedRFQCreationHandler._handle_final_rfq_submission.__get__(h)
    submitted = session()
    assert (await h._handle_final_rfq_submission(u, submitted))["status"] == "rfq_submitted"
    assert submitted.workflow_type == WorkflowType.rfq_submitted
    h._handle_restart_rfq = SectionedRFQCreationHandler._handle_restart_rfq.__get__(h)
    h.cancel_service.handle_cancel_intent = AsyncMock(return_value={"status": "cancelled_aborted"})
    h._display_delivery_confirmation = AsyncMock(return_value={"status": "redrawn"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "date_location")
    assert (await h._handle_restart_rfq(u, session(date_location={"deliveryDate": "d"})))["status"] == "redrawn"
    assert (await h._offer_restart_confirmation(u, session()))["status"] == "awaiting_restart_confirmation"
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_sectioned_rfq", MagicMock())
    assert (await h._handle_restart_confirmation_response(u, session(), "yes"))["status"] == "workflow_restarted"
    assert (await h._handle_restart_confirmation_response(u, session(), "no"))["status"] == "restart_declined"
    assert (await h._handle_restart_confirmation_response(u, session(), "maybe"))["status"] == "awaiting_restart_confirmation"


@pytest.mark.asyncio
async def test_confirmation_submission_completion_optional_and_bfs_fallbacks(monkeypatch):
    h = bare(ConfirmationHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), cancel_service=None, session_manager=None, confirmation_service=AsyncMock(), _bfs_search_handler=None)
    u = user()
    h._send_completion_response = AsyncMock()
    h._submit_rfq_to_backend = AsyncMock(return_value={"success": False, "error": "down"})
    monkeypatch.setattr(confirmation_mod, "RFQValidationSchema", lambda **data: SimpleNamespace(**data))
    failed = session(pending_combined_rfq={"combined_schema": {"project_desc": "pump"}})
    assert (await h._handle_rfq_acceptance(u, failed, "yes"))["successful_count"] == 0
    assert failed.outcome == ConversationOutcome.abandoned and failed.workflow_state == {}

    h._submit_rfq_to_backend.return_value = {"success": True, "rfq_id": "R1"}
    done = session(pending_combined_rfq={"combined_schema": {"project_desc": "pump"}})
    assert (await h._handle_rfq_acceptance(u, done, "yes"))["status"] == "multiple_rfqs_created"
    assert done.outcome == ConversationOutcome.completed

    h._send_completion_response = ConfirmationHandler._send_completion_response.__get__(h)
    await h._send_completion_response(u, session(), [{"success": False, "error": "no"}], 0)
    await h._send_completion_response(u, session(), [{"success": True, "rfq_id": "R1", "rfq_data": {"items": [{"description": "x"}]}}], 1)
    await h._send_completion_response(u, session(), [{"success": True, "rfq_id": "R1"}, {"success": True, "rfq_id": "R2"}], 2)
    await h._send_completion_response(u, session(), [{"success": True}], 1)
    assert h.whatsapp_service.send_message.await_count == 1
    assert h.whatsapp_service.send_configurable_buttons.await_count == 3

    h._bfs_search_handler = None
    import app.services.handlers.bfs_search_handler as bfs_mod
    fake_bfs = SimpleNamespace()
    monkeypatch.setattr(bfs_mod, "BFSSearchHandler", lambda *_args, **_kwargs: fake_bfs)
    assert h._extract_product_descriptions_from_rfq([]) == []
    assert h.bfs_search_handler is fake_bfs
    assert h.bfs_search_handler is fake_bfs
    h._bfs_search_handler = SimpleNamespace(handle_bfs_search=AsyncMock())
    await h._check_bfs_availability(u, session(), [])
    await h._check_bfs_availability(u, session(), [{"success": True, "rfq_data": {"items": [{"description": "pump"}]}}])
    h._bfs_search_handler.handle_bfs_search.side_effect = RuntimeError("search")
    await h._check_bfs_availability(u, session(), [{"success": True, "rfq_data": {"items": [{"product_name": "pump"}]}}])

    h.response_helpers = SimpleNamespace(generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"))
    h.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock())
    h._merge_specifications_into_product = MagicMock()
    monkeypatch.setattr(confirmation_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: SimpleNamespace())
    import app.services.openai_service as openai_mod
    fake_openai = SimpleNamespace(
        extract_entities=AsyncMock(return_value={"products": [{"remarks": "steel"}]})
    )
    monkeypatch.setattr(openai_mod, "OpenAIService", lambda: fake_openai)
    single = session(pending_optional_rfq={"entities": {"description": "pump"}}, attachment_caption="urgent")
    assert (await h._merge_optional_fields_and_confirm(u, single, "steel"))["status"] == "optional_fields_merged_confirmation_sent"
    combined = session(pending_optional_combined_rfq={"combined_schema": {}, "products": [{"entities": {}}]})
    assert (await h._merge_optional_fields_and_confirm(u, combined, "steel"))["status"] == "optional_fields_merged_confirmation_sent"
    monkeypatch.setattr(openai_mod, "OpenAIService", MagicMock(side_effect=RuntimeError("ai")))
    assert (await h._merge_optional_fields_and_confirm(u, session(), "x"))["status"] == "continue_with_purchase_intent"


@pytest.mark.asyncio
async def test_auth_switch_account_outcomes_and_exceptions(monkeypatch):
    h = bare(AuthRegistrationIntentSwitch, whatsapp_service=AsyncMock(), openai_service=MagicMock())
    u = user(); auth = AsyncMock()
    pending = {"target_role": "seller", "current_role": "buyer", "original_message": "sell"}
    for answer, expected in (("switch_existing", "existing"), ("register_new", "registered"), ("continue_current", "role_switch_declined"), ("unclear", "role_switch_clarification_requested")):
        h._ai_validate_three_option_response = AsyncMock(return_value=answer)
        s = session(pending_role_switch=pending.copy())
        h._handle_switch_to_existing_account = AsyncMock(return_value={"status": "existing"})
        h._handle_register_new_account = AsyncMock(return_value={"status": "registered"})
        assert (await h._handle_traditional_role_switch_response(u, s, "x", auth, pending))["status"] == expected

    h._ai_validate_three_option_response = AsyncMock(return_value="switch_existing")
    s = session(pending_account_switch=pending.copy())
    h._handle_switch_to_existing_account = AsyncMock(return_value={"status": "existing"})
    assert (await h.handle_account_switch_response(u, s, "1", auth))["status"] == "existing"
    h._ai_validate_three_option_response.return_value = "register_new"
    s = session(pending_account_switch=pending.copy())
    h._handle_register_new_account = AsyncMock(return_value={"status": "registered"})
    assert (await h.handle_account_switch_response(u, s, "2", auth))["status"] == "registered"
    h._ai_validate_three_option_response.return_value = "unclear"
    assert (await h.handle_account_switch_response(u, session(pending_account_switch=pending.copy()), "?", auth))["status"] == "account_switch_clarification_requested"

    h._handle_switch_to_specific_account = AsyncMock(return_value={"status": "specific"})
    options = {"formatted_options": [{"number": 1, "email": "seller@x", "account_data": {"id": "s"}}]}
    assert (await h._handle_enhanced_account_selection_response(u, session(), "1", auth, pending, options))["status"] == "specific"
    options["formatted_options"][0] = {"number": 1, "email": "seller@x", "text": "1 seller@x", "action": "continue_current"}
    assert (await h._handle_enhanced_account_selection_response(u, session(pending_role_switch=pending.copy()), "1", auth, pending, options))["status"] == "role_switch_declined"
    assert (await h._handle_enhanced_account_selection_response(u, session(), "bad", auth, pending, options))["status"] == "account_selection_clarification_requested"

    h.whatsapp_service.send_message.side_effect = RuntimeError("send")
    assert (await h.handle_role_switch_confirmation(u, session(), "x", "seller"))["status"] == "error"
    h.whatsapp_service.send_message.side_effect = None
    h.openai_service.generate_response = MagicMock(side_effect=RuntimeError("ai"))
    h._ai_validate_three_option_response = AuthRegistrationIntentSwitch._ai_validate_three_option_response.__get__(h, type(h))
    assert await h._ai_validate_role_confirmation_response("stay", "buyer", "seller") == "no"
    assert await h._ai_validate_three_option_response("3", "buyer", "seller", "switch") == "continue_current"


@pytest.mark.asyncio
async def test_auth_orchestrator_registration_stages_role_switch_and_fallbacks(monkeypatch):
    h = bare(AuthenticationOrchestrator, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), authentication_service=AsyncMock(), registration_service=AsyncMock(), intent_service=AsyncMock(), support_service=AsyncMock(), chat_service=None, auth_reg_switch=AsyncMock(), profile_selection_service=AsyncMock())
    h.support_service.redirect_to_support.return_value = {"status": "support"}
    h.registration_service.initiate_registration.return_value = {"ok": True}
    h.registration_service.handle_registration_data_collection.return_value = {"status": "data"}
    h.registration_service.handle_registration_otp_validation.return_value = {"status": "otp"}
    h.authentication_service.handle_email_confirmation.return_value = {"status": "email"}
    h.authentication_service.handle_domain_matching.return_value = {"status": "domain"}
    h.registration_service.handle_registration_confirmation.return_value = {"status": "confirm"}
    h._check_switch_response = AsyncMock(return_value=None)

    for stage, expected in (("data_collection", "data"), ("start", "data"), ("email_confirmation", "email"), ("email_otp", "otp"), ("domain_matching", "domain"), ("confirmation", "confirm")):
        s = session(registration_stage=stage, current_intent_result={"intent": "buy_something"})
        result = await h._handle_registration_workflow("+1", "message", s, {"intent": "buy_something"})
        assert result["status"] == expected
    assert (await h._handle_registration_workflow("+1", "message", session(), {}))["status"] == "redirected_to_registration"

    h._handle_intent_switch_during_registration = AsyncMock(return_value={"status": "switched"})
    assert (await h._handle_registration_workflow("+1", "sell", session(registration_stage="start", user_type="buyer"), {"intent": "sell_something", "confidence": 90}))["status"] == "switched"
    assert await h._should_handle_intent_switch("buy_something", "collecting")
    assert not await h._should_handle_intent_switch("buy_something", "email_otp")
    assert await h._should_handle_intent_switch_during_auth("buy_something", 90, "email_confirmation", session(intent_result={"intent": "sell_something"}))
    assert not await h._should_handle_intent_switch_during_auth("buy_something", 50, "email_confirmation", session())
    assert await h._should_handle_intent_switch_during_registration("buy_something", 90, "seller")
    assert await h._should_handle_intent_switch_during_registration("exit_system", 90, "buyer")

    h.authentication_service.validate_token.return_value = {"verification_required": True}
    h.profile_selection_service.handle_profile_selection.return_value = {"status": "profile"}
    assert (await h.authentication_orchestrator_flow("+1", "buy", session(), {"intent": "buy_something", "confidence": 90}))["status"] == "profile"
    h.authentication_service.validate_token.side_effect = RuntimeError("db")
    assert (await h.authentication_orchestrator_flow("+1", "x", session(), {"intent": "other"}))["status"] == "support"
    h.authentication_service.validate_token.side_effect = None
    h._handle_auth_fallback = AsyncMock(return_value={"status": "fallback"})
    assert (await h.authentication_orchestrator_flow("+1", "x", session(), {"intent": "other", "confidence": 90}))["status"] == "fallback"
    assert (await h._handle_auth_clarification_request("+1", "x"))["status"] == "clarification_sent"


@pytest.mark.asyncio
async def test_products_array_empty_merge_question_modes_and_exceptions(monkeypatch):
    h = bare(ProductsArrayHandler, whatsapp_service=AsyncMock(), openai_service=MagicMock(), response_helpers=AsyncMock(), session_manager=AsyncMock())
    u = user()
    h._track_product_categories = AsyncMock()
    h._categorize_products_by_completeness = AsyncMock(return_value=([], [{"index": 1, "entities": {"description": "pump"}}]))
    h._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    assert (await h.handle_products_array(u, session(), "x", [], global_supplementary_fields={"city": "Pune"}))["status"] == "need_product_description"
    h._categorize_products_by_completeness.return_value = ([{"index": 1, "entities": {"description": "pump"}, "missing_fields": ["quantity"]}], [])
    h._handle_incomplete_products = AsyncMock(return_value={"status": "incomplete"})
    assert (await h.handle_products_array(u, session(attachment_caption="urgent"), "x", [{"description": "pump"}]))["status"] == "incomplete"
    h._track_product_categories.side_effect = RuntimeError("track")
    assert (await h.handle_products_array(u, session(), "x", [{"description": "x"}]))["status"] == "error"

    merged = await h._merge_with_existing_incomplete_products([{"entities": {"description": "pump", "date_validation_error": "bad"}}], [{"description": "pump", "deliveryDate": "tomorrow", "date_validation_error": ""}])
    assert merged[0]["deliveryDate"] == "tomorrow" and "date_validation_error" not in merged[0]
    positional = await h._merge_with_existing_incomplete_products([{"entities": {}}, {"entities": {}}], [{"description": "pump"}, {"description": "bolt"}])
    assert [x["description"] for x in positional] == ["pump", "bolt"]
    assert await h._merge_with_existing_incomplete_products([{"entities": {"description": "pump"}}], [{"bad": object()}])

    schema = SimpleNamespace(get_combined_questions=lambda: {"mandatory": ["Delivery date?", "Quantity"], "optional": [], "has_mandatory": True, "has_optional": False}, get_missing_mandatory_fields=lambda: ["delivery_date"])
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: schema)
    questions, fields = await h._generate_clarification_questions([{"index": 1, "entities": {"description": "pump"}, "missing_fields": ["delivery_date"]}, {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]}], 2)
    assert "Delivery date?" in questions and fields
    schema.get_combined_questions = lambda: {"mandatory": ["How many quantity?"], "optional": [], "has_mandatory": True, "has_optional": False}
    questions, _ = await h._generate_clarification_questions([{"index": 1, "entities": {"description": "pump"}, "missing_fields": ["item_0_quantity"]}], 1)
    assert questions == ["How many quantity?"]


@pytest.mark.asyncio
async def test_purchase_intent_all_payload_paths_and_errors(monkeypatch):
    h = PurchaseIntentHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    u = user()
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "is_sectioned_rfq_active", lambda *_: False)
    h.chat_summary_service.load_user_context.return_value = ["summary"]
    h.entity_service.extract_entities_with_summary_context.return_value = {"products": [{"description": "pump"}], "global_supplementary_fields": {"city": "Pune"}}
    h.products_array_handler.handle_products_array.return_value = {"status": "products"}
    assert (await h.handle_purchase_intent(u, session(), {"text": {"body": "buy"}}, {"intent": "buy_something"}, lambda _: True))["status"] == "products"
    h.entity_service.extract_entities.return_value = {"error_type": "non_procurable", "non_procurable_items": ["service", "license"]}
    import app.services.cancel_service as cancel_mod
    monkeypatch.setattr(cancel_mod, "CancelService", MagicMock())
    cancel = cancel_mod.CancelService.return_value
    cancel._clear_workflow_state = AsyncMock(); cancel._send_cancellation_message = AsyncMock()
    assert (await h.handle_purchase_intent(u, session(), "x"))["status"] == "non_procurable_cancelled"
    h.entity_service.extract_entities.return_value = {"error_type": "quantity_limit", "quantity_violations": [{"description": "pump", "quantity": 100000001}, {"description": "bolt", "quantity": 100000002}]}
    assert (await h.handle_purchase_intent(u, session(), "x"))["status"] == "quantity_limit_violation"
    h.entity_service.extract_entities.return_value = {"entities": {}}
    assert (await h.handle_purchase_intent(u, session(), "x"))["status"] == "no_new_entities"
    h.entity_service.extract_entities.return_value = {"modification_intent_detected": True, "requires_clarification": True}
    assert (await h.handle_purchase_intent(u, session(), "change"))["status"] == "modification_clarification_sent"
    h.entity_service.extract_entities.side_effect = RuntimeError("extract")
    assert (await h.handle_purchase_intent(u, session(), "x"))["status"] == "error"

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=True))
    monkeypatch.setattr(purchase_mod.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(purchase_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    h.sectioned_rfq_handler = SimpleNamespace(handle_sectioned_rfq=AsyncMock(return_value={"status": "sectioned"}))
    assert (await h.handle_purchase_intent(u, session(), "buy"))["status"] == "sectioned"


@pytest.mark.asyncio
async def test_seller_auth_mixin_all_auth_lookup_otp_and_menu_outcomes(monkeypatch):
    h = bare(SellerAuthMixin, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), otp_service=AsyncMock(), auth_redis_service=AsyncMock(), openai_service=AsyncMock())
    h.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=False, org_id="other", id="other")
    assert (await h.check_seller_auth_state("+1", "target"))["account_type"] == "seller"
    h.auth_redis_service.retrieve.side_effect = RuntimeError("redis")
    assert (await h.check_seller_auth_state("+1", "target"))["state"] == "not_auth"
    h.auth_redis_service.retrieve.side_effect = None
    h.openai_service.generate_response.return_value = "stay"
    assert await h._parse_switch_choice("maybe") == "stay"
    h.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await h._parse_switch_choice("maybe") == "unclear"

    s = session(target_seller_id="target")
    h._parse_switch_choice = AsyncMock(return_value="stay")
    assert (await h.handle_seller_switch_response("+1", s, "2"))["status"] == "switch_declined"
    h._parse_switch_choice.return_value = "unclear"
    assert (await h.handle_seller_switch_response("+1", session(target_seller_id="target"), "?"))["status"] == "switch_response_unclear"
    h._parse_switch_choice.return_value = "switch"
    h.authentication_service.clear_user_token = AsyncMock()
    h.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await h.handle_seller_switch_response("+1", s, "1"))["status"] == "otp"

    h.initiate_seller_auth = SellerAuthMixin.initiate_seller_auth.__get__(h, type(h))
    h.get_seller_info = AsyncMock(return_value=(None, None))
    h._reset_workflow_with_menu = AsyncMock()
    assert (await h.initiate_seller_auth("+1", session(), "target"))["status"] == "seller_not_found"
    h.get_seller_info.return_value = ("seller@x", {"id": "s"})
    h.otp_service.send_otp.return_value = {"status": "otp_sent"}
    assert (await h.initiate_seller_auth("+1", session(), "target"))["status"] == "otp_sent"
    h.otp_service.send_otp.return_value = {"status": "failed", "error": "down"}
    assert (await h.initiate_seller_auth("+1", session(), "target"))["status"] == "otp_send_failed"
    h.otp_service = None
    assert (await h.initiate_seller_auth("+1", session(), "target"))["status"] == "otp_instruction_sent"

    h.get_seller_info = SellerAuthMixin.get_seller_info.__get__(h, type(h))
    h.authentication_service.user_authenticate.return_value = {"success": True, "response": [{"selfClient": True}]}
    assert await h.get_seller_info("+1", "target") == (None, None)
    h.authentication_service.user_authenticate.return_value = {"success": False}
    assert await h.get_seller_info("+1", "target") == (None, None)
    h.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=True)
    await h._reset_workflow_with_menu("+1", session(), "message")
    h.auth_redis_service.retrieve.return_value = None
    await h._reset_workflow_with_menu("+1", session(), "message")


@pytest.mark.asyncio
async def test_seller_interest_routes_failures_and_redirects(monkeypatch):
    h = bare(SellerRFQInterestHandler, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), otp_service=AsyncMock(), auth_redis_service=AsyncMock(), settings=SimpleNamespace(procucev_rfq_details_url="https://portal"))
    h.check_seller_auth_state = AsyncMock(return_value={"state": "not_auth"})
    h.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await h.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "otp"
    h.check_seller_auth_state.return_value = {"state": "wrong_account", "current_user": SimpleNamespace(self_client=True, email="a@x", dict=lambda: {})}
    h.prompt_seller_account_switch = AsyncMock(return_value={"status": "prompt"})
    assert (await h.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "prompt"
    h.check_seller_auth_state.return_value = {"state": "correct_seller"}
    h._show_intermediate_buttons = AsyncMock(return_value={"status": "intermediate"})
    assert (await h.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "intermediate"

    response = SimpleNamespace(success=False, message_id=None, error="send failed")
    h.whatsapp_service.send_configurable_buttons.return_value = response
    h._show_intermediate_buttons = SellerRFQInterestHandler._show_intermediate_buttons.__get__(h)
    import app.services.seller_notification_service as notification_mod
    monkeypatch.setattr(notification_mod, "SellerNotificationService", lambda: SimpleNamespace(get_intermediate_rfq_buttons=lambda *_: []))
    assert (await h._show_intermediate_buttons("+1", "r", "s", session()))["status"] == "error"
    assert (await h.handle_otp_validated("+1", session()))["status"] == "error"

    h.auth_redis_service.retrieve.return_value = None
    assert (await h._redirect_to_seller_flow("+1", "r", "s", session()))["status"] == "error"
    h.auth_redis_service.retrieve.return_value = SimpleNamespace(id="seller")
    fake_service = SimpleNamespace(handle_seller_workflow=AsyncMock(return_value={"message": "hello"}))
    import app.services.seller_service as seller_service_mod
    monkeypatch.setattr(seller_service_mod, "SellerService", lambda **_: fake_service)
    monkeypatch.setattr(seller_interest_mod.WorkflowManager, "set_workflow_type", MagicMock())
    assert (await h._redirect_to_seller_flow("+1", "r", "s", session()))["status"] == "redirected_to_seller_flow"
    assert h.whatsapp_service.send_message.await_count >= 1

    h.handle_seller_switch_response = AsyncMock(return_value={"status": "declined"})
    assert (await h.handle_switch_response("+1", session(), "2"))["status"] == "declined"
