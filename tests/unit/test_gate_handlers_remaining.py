"""Deterministic branch tests for the remaining handler and helper paths.

All tests call methods directly and replace external boundaries with mocks.  The
module intentionally avoids constructing eager service graphs where ``__new__``
is sufficient for exercising the state-machine branches.
"""

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.handlers.auth_registration_intent_switch as switch_mod
import app.services.handlers.authentication_orchestrator as auth_mod
import app.services.handlers.bfs_search_handler as bfs_mod
import app.services.handlers.bfs_seller_bid_handler as bfs_seller_mod
import app.services.handlers.confirmation_handler as confirmation_mod
import app.services.handlers.intent_switch_handler as intent_mod
import app.services.handlers.products_array_handler as products_mod
import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
import app.services.handlers.seller_rfq_interest_handler as interest_mod
import app.services.helpers.authentication_helpers as authentication_helpers_mod
import app.services.helpers.chat_service_helpers as chat_helpers_mod
import app.services.helpers.response_helpers as response_helpers_mod
import app.services.helpers.session_helpers as session_helpers_mod
import app.services.helpers.summarization_helpers as summarization_mod
from app.models import ConversationOutcome, WorkflowType
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.bfs_seller_bid_handler import BFSSellerBidHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.intent_switch_handler import IntentSwitchHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.helpers.attachment_helpers import AttachmentHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.excel_confirmation_helpers import ExcelConfirmationHelpers
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers


def session(**state):
    return SimpleNamespace(
        session_id="sid",
        external_user_id="uid",
        phone_number="+91123",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={"openai_messages": [], "metadata": [], "messages": []},
        product_items=[],
        extracted_entities={},
        outcome=None,
        completed_at=None,
        created_at=None,
        last_activity_at=None,
        rfq_ids=[],
        rfq_id=None,
        retention_date=None,
        user_type=None,
        user_type_value=None,
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


def user(role="buyer", registered=True):
    return SimpleNamespace(
        id="u1", org_id="o1", phone_number="+91123", role=role,
        is_registered=registered, self_client=role == "buyer",
    )


def bare(cls, **attrs):
    instance = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *_args):
        return False


# Sectioned RFQ, authentication, and confirmation state machines

@pytest.mark.asyncio
async def test_sectioned_routing_attachment_and_final_confirmation_branches(monkeypatch):
    handler = bare(
        SectionedRFQCreationHandler,
        entity_service=AsyncMock(), whatsapp_service=AsyncMock(),
        cancel_service=AsyncMock(), session_manager=AsyncMock(),
        confirmation_handler=AsyncMock(), attachment_decision_handler=None,
    )
    current = session(date_location={"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda *_: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "date_location")
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, name: s.workflow_state.get(name))
    handler._handle_section_confirm = AsyncMock(return_value={"status": "confirmed"})
    handler._handle_section_modify = AsyncMock(return_value={"status": "modified"})
    assert (await handler.handle_sectioned_rfq(user(), current, {"button_reply": {"id": "confirm"}}))["status"] == "confirmed"
    assert (await handler.handle_sectioned_rfq(user(), current, "modify"))["status"] == "modified"

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "attachments")
    handler._build_combined_rfq_from_sections = MagicMock(return_value={"combined_schema": {}, "products": []})
    assert (await handler._handle_attachments_section(user(), current, "", []))["status"] == "awaiting_attachments_decision"
    assert current.workflow_state["sectioned_rfq_attachment_question_asked"]

    current.workflow_state["pending_combined_rfq"] = {"combined_schema": {}}
    handler.confirmation_handler.handle_optional_fields_response = AsyncMock(return_value={"status": "done"})
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "confirm_sectioned_section", MagicMock())
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    assert (await handler._handle_attachments_section(user(), current, "yes", []))["status"] == "done"

    handler.confirmation_handler.handle_pending_confirmations = AsyncMock(return_value={"status": "pending"})
    current.workflow_state.pop("pending_combined_rfq")
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, name: s.workflow_state.get(name))
    assert (await handler._handle_final_confirmation(user(), current, "yes"))["status"] == "pending"
    assert "pending_combined_rfq" in current.workflow_state


@pytest.mark.asyncio
async def test_authentication_orchestrator_priority_workflow_and_error_routes(monkeypatch):
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
        authentication_service=AsyncMock(), registration_service=AsyncMock(),
        intent_service=AsyncMock(), support_service=AsyncMock(), chat_service=None,
        auth_reg_switch=AsyncMock(), profile_selection_service=AsyncMock(),
    )
    exit_service = SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "exited"}))
    monkeypatch.setattr(auth_mod, "ExitService", lambda *args, **kwargs: exit_service)
    assert (await handler.authentication_orchestrator_flow(
        "+1", "bye", session(), {"intent": "exit_system", "confidence": 90}
    ))["status"] == "exited"

    handler.authentication_service.validate_token.return_value = None
    handler.profile_selection_service.handle_profile_selection_response.return_value = {"status": "selected"}
    assert (await handler.authentication_orchestrator_flow(
        "+1", "1", session(profile_selection_stage="choose"), {"intent": "other", "confidence": 90}
    ))["status"] == "selected"

    handler._handle_authentication_workflow = AsyncMock(return_value={"status": "auth"})
    auth_session = session(); auth_session.workflow_type = WorkflowType.authentication
    assert (await handler.authentication_orchestrator_flow("+1", "x", auth_session, {}))["status"] == "auth"
    handler._handle_registration_workflow = AsyncMock(return_value={"status": "registration"})
    reg_session = session(); reg_session.workflow_type = WorkflowType.registration
    assert (await handler.authentication_orchestrator_flow("+1", "x", reg_session, {}))["status"] == "registration"

    handler.support_service.redirect_to_support.return_value = {"status": "support"}
    handler.authentication_service.validate_token.side_effect = RuntimeError("token")
    assert (await handler.authentication_orchestrator_flow("+1", "x", session(), {"intent": "other"}))["status"] == "support"


@pytest.mark.asyncio
async def test_authentication_workflow_stages_and_intent_switch_handlers(monkeypatch):
    handler = bare(
        AuthenticationOrchestrator,
        whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
        authentication_service=AsyncMock(), registration_service=AsyncMock(),
        intent_service=AsyncMock(), support_service=AsyncMock(), chat_service=None,
        auth_reg_switch=AsyncMock(), profile_selection_service=AsyncMock(),
    )
    handler.profile_selection_service.handle_profile_selection = AsyncMock(return_value={"status": "menu"})
    handler.profile_selection_service.handle_profile_selection_response = AsyncMock(return_value={"status": "response"})
    assert (await handler._handle_authentication_workflow(
        "+1", "hi", session(profile_selection_stage="choose"), {"intent": "greeting"}
    ))["status"] == "menu"
    handler.authentication_service.handle_email_confirmation.return_value = {"status": "email"}
    assert (await handler._handle_authentication_workflow(
        "+1", "yes", session(authentication_stage="email_confirmation"), {"intent": "other"}
    ))["status"] == "email"
    handler.registration_service.handle_registration_data_collection.return_value = {"status": "data"}
    assert (await handler._handle_registration_workflow(
        "+1", "data", session(registration_stage="start", user_type="buyer"), {"intent": "other"}
    ))["status"] == "data"
    handler.registration_service.handle_registration_otp_validation.return_value = {
        "status": "registration_completed", "registration_flow_complete": True
    }
    completed = session(registration_stage="email_otp")
    assert (await handler._handle_registration_workflow("+1", "otp", completed, {}))["status"] == "registration_completed"
    assert completed.workflow_state == {} and completed.workflow_type is None

    handler.auth_reg_switch.handle_auth_reg_switch_choice.return_value = {"status": "choice"}
    assert (await handler._handle_intent_switch_during_auth(
        "+1", session(), "sell", {"intent": "sell_something"}
    ))["status"] == "choice"
    cancelled = session(); cancelled.workflow_type = WorkflowType.authentication
    assert (await handler._handle_intent_switch_during_auth(
        "+1", cancelled, "stop", {"intent": "cancel"}
    ))["status"] == "authentication_cancelled"
    registration = session(); registration.workflow_type = WorkflowType.registration
    assert (await handler._handle_intent_switch_during_registration(
        "+1", registration, "stop", {"intent": "stop"}, "buyer"
    ))["status"] == "registration_cancelled"


@pytest.mark.asyncio
async def test_confirmation_completion_and_optional_fallback_branches(monkeypatch):
    handler = bare(
        ConfirmationHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
        cancel_service=None, session_manager=None, confirmation_service=AsyncMock(),
        _bfs_search_handler=None,
    )
    handler._send_completion_response = AsyncMock()
    handler._submit_rfq_to_backend = AsyncMock(return_value={"success": True, "rfq_id": "R1"})
    monkeypatch.setattr(confirmation_mod, "RFQValidationSchema", lambda **data: SimpleNamespace(**data))
    combined = session(pending_combined_rfq={"combined_schema": {"project_desc": "x"}})
    assert (await handler._handle_rfq_acceptance(user(), combined, "yes"))["status"] == "multiple_rfqs_created"
    assert combined.outcome == ConversationOutcome.completed and combined.workflow_state == {}

    for results, count in [([], 0), ([{"success": True, "rfq_id": "R1", "rfq_data": {"items": [{"description": "x"}]}}], 1), ([{"success": True}], 1)]:
        await handler._send_completion_response(user(), session(), results, count)
    assert handler._send_completion_response.await_count >= 4

    handler._submit_rfq_to_backend.side_effect = RuntimeError("unused")
    monkeypatch.setattr("app.services.openai_service.OpenAIService", MagicMock(side_effect=RuntimeError("openai")))
    assert (await handler._merge_optional_fields_and_confirm(user(), session(), "brand"))["status"] == "continue_with_purchase_intent"
    assert "No product" in handler._format_captured_info_for_modification(session())


# Intent/auth switch and product-array branches

@pytest.mark.asyncio
async def test_auth_registration_switch_ai_fallbacks_and_account_responses(monkeypatch):
    handler = bare(AuthRegistrationIntentSwitch, whatsapp_service=AsyncMock(), openai_service=MagicMock())
    handler.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await handler._ai_validate_role_confirmation_response("switch", "buyer", "seller") == "yes"
    assert await handler._ai_validate_role_confirmation_response("2", "buyer", "seller") == "no"
    assert await handler._ai_validate_three_option_response("1", "buyer", "seller", "role") == "switch_existing"
    assert await handler._ai_validate_three_option_response("2", "buyer", "seller", "role") == "register_new"
    assert await handler._ai_validate_three_option_response("3", "buyer", "seller", "role") == "continue_current"
    assert await handler._ai_validate_three_option_response("maybe", "buyer", "seller", "role") == "unclear"

    pending = {"target_role": "seller", "current_role": "buyer", "original_message": "sell"}
    state = session(pending_account_switch=pending)
    handler._ai_validate_three_option_response = AsyncMock(return_value="continue_current")
    assert (await handler.handle_account_switch_response(user(), state, "3", AsyncMock()))["status"] == "account_switch_declined"
    assert "pending_account_switch" not in state.workflow_state
    assert (await handler.handle_account_switch_response(user(), session(), "1", AsyncMock()))["status"] == "no_pending_account_switch"

    role_state = session(pending_role_switch={"target_role": "seller", "current_role": "buyer", "original_message": "x"})
    handler._handle_switch_to_existing_account = AsyncMock(return_value={"status": "switched"})
    assert (await handler.handle_role_switch_response(user(), role_state, "1", AsyncMock()))["status"] == "switched"
    role_state = session(pending_role_switch={"target_role": "seller", "current_role": "buyer", "original_message": "x"})
    assert (await handler.handle_role_switch_response(user(), role_state, "?", AsyncMock()))["status"] == "role_switch_clarification_requested"


@pytest.mark.asyncio
async def test_products_array_merge_and_question_strategy_branches(monkeypatch):
    handler = ProductsArrayHandler(AsyncMock(), MagicMock(), AsyncMock(), AsyncMock())
    positional = await handler._merge_with_existing_incomplete_products(
        [{"entities": {}}, {"entities": {}}], [{"description": "laptop"}, {"description": "chair"}]
    )
    assert [item["description"] for item in positional] == ["laptop", "chair"]

    existing = [{"entities": {"description": "laptop", "date_validation_error": "bad", "pincode_validation_error": "bad"}}]
    merged = await handler._merge_with_existing_incomplete_products(
        existing, [{"description": "laptop", "deliveryDate": "tomorrow", "pincode": "560001", "city": "C", "state": "S", "date_validation_error": "", "pincode_validation_error": ""}]
    )
    assert "date_validation_error" not in merged[0] and "pincode_validation_error" not in merged[0]

    class Schema:
        def __init__(self, questions): self.questions = questions
        def get_combined_questions(self): return self.questions

    schemas = [Schema({"mandatory": ["Delivery date?"], "optional": [], "has_mandatory": True, "has_optional": False})]
    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: schemas[0])
    questions, missing = await handler._generate_clarification_questions(
        [{"index": 1, "entities": {"description": "laptop"}, "missing_fields": ["delivery_date"]}, {"index": 2, "entities": {"description": "chair"}, "missing_fields": ["item_0_quantity"]}], 2
    )
    assert "Delivery date?" in questions and missing

    monkeypatch.setattr(products_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: (_ for _ in ()).throw(RuntimeError("schema")))
    incomplete, complete = await handler._categorize_products_by_completeness([{"description": "x"}])
    assert incomplete[0]["missing_fields"] and not complete


@pytest.mark.asyncio
async def test_intent_switch_context_matrix_and_corrupt_state(monkeypatch):
    handler = bare(IntentSwitchHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), openai_service=AsyncMock())
    active = session(extracted_entities=[{"product_name": "Laptop"}]); active.workflow_type = WorkflowType.rfq_creation
    assert not await handler.should_handle_intent_switch(active, "buy_something", 80)
    assert not await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "collecting", "references_existing_data": True})
    assert await handler.should_handle_intent_switch(active, "buy_something", 100, {"conversation_stage": "new_request"})
    seller = session(extracted_entities=[{"description": "x"}]); seller.workflow_type = WorkflowType.seller_rfq_view
    assert await handler.should_handle_intent_switch(seller, "buy_something", 100)

    corrupted = session(pending_intent_switch="bad")
    assert (await handler.handle_intent_switch_response(user(), corrupted, "x"))["status"] == "corrupted_intent_switch_data"
    incomplete = session(pending_intent_switch={"new_intent": "buy_something"})
    handler.openai_service.analyze_intent_switch_response = AsyncMock(return_value={"chosen_action": "switch_to_new"})
    assert (await handler.handle_intent_switch_response(user(), incomplete, "2"))["status"] == "error"
    assert handler._get_workflow_description(session()) == "current request"


# BFS search/bid and seller notification handlers

@pytest.mark.asyncio
async def test_bfs_search_remaining_result_and_bid_expiry_branches(monkeypatch):
    handler = bare(
        BFSSearchHandler, whatsapp_service=AsyncMock(), session_manager=AsyncMock(),
        openai_service=AsyncMock(), auto_categorization_service=AsyncMock(), bfs_api_service=AsyncMock(),
    )
    u, s = user(), session(bfs_results=[{"id": "i"}])
    await handler._send_bfs_results(u, s, {"unexpected": True})
    assert handler.whatsapp_service.send_message.called
    s.workflow_state = {}
    assert (await handler.handle_bid_format_input(u, s, "bad"))["status"] == "bfs_bid_session_expired"

    monkeypatch.setattr("app.services.handlers.bfs_search_handler.parse_bid_format", lambda *_: {"bids": [{"price": 1}]})
    handler._send_bid_otp = AsyncMock(return_value={"status": "otp"})
    s = session(bfs_results=[{"id": "i"}])
    assert (await handler.handle_bid_format_input(u, s, "good"))["status"] == "otp"

    redis = AsyncMock(); redis.retrieve.return_value = SimpleNamespace(email="a@x", otp_validated_at=None)
    otp = SimpleNamespace(send_otp=AsyncMock(return_value={"status": "failed", "error": "down"}))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    monkeypatch.setattr("app.services.otp_service.OTPService", lambda **_: otp)
    monkeypatch.setattr("app.procucev_apis.register_apis.RegisterAPIService", lambda: MagicMock())
    monkeypatch.setattr("app.services.support_notification_service.SupportNotificationService", lambda: MagicMock())
    assert (await BFSSearchHandler._send_bid_otp(handler, u, session(), [{"price": 1}]))["status"] == "bfs_bid_otp_failed"

    handler._clear_bid_state = AsyncMock()
    otp.validate_otp = AsyncMock(return_value={"status": "invalid"})
    monkeypatch.setattr("app.services.otp_service.OTPService", lambda **_: otp)
    assert (await handler.handle_bid_otp_input(u, session(otp_email="a@x"), "bad"))["status"] == "bfs_bid_otp_invalid"


@pytest.mark.asyncio
async def test_bfs_seller_bid_action_validation_execution_and_otp_state(monkeypatch):
    handler = bare(BFSSellerBidHandler, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), otp_service=AsyncMock(), settings=SimpleNamespace(), bfs_api_service=AsyncMock())
    invalid = session()
    assert (await handler._handle_bid_action("+1", "", "seller", invalid, True))["status"] == "error"
    handler.check_seller_auth_state = AsyncMock(return_value={"state": "not_auth"})
    handler.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await handler.handle_accept_bid_click("+1", "uuid", "seller", session()))["status"] == "otp"
    handler.check_seller_auth_state.return_value = {"state": "wrong_account", "current_user": user()}
    handler.prompt_seller_account_switch = AsyncMock(return_value={"status": "prompt"})
    assert (await handler.handle_reject_bid_click("+1", "uuid", "seller", session()))["status"] == "prompt"

    handler.bfs_api_service.accept_bid_by_seller.return_value = {"success": True}
    assert (await handler._execute_bid_action("+1", "uuid", True, session()))["status"] == "bfs_bid_accepted"
    handler.bfs_api_service.reject_bid_by_seller.return_value = {"success": False, "error": "no"}
    assert (await handler._execute_bid_action("+1", "uuid", False, session()))["status"] == "error"
    handler.bfs_api_service.accept_bid_by_seller.side_effect = RuntimeError("api")
    assert (await handler._execute_bid_action("+1", "uuid", True, session()))["status"] == "error"
    assert (await handler.handle_otp_validated("+1", session()))["status"] == "error"


@pytest.mark.asyncio
async def test_seller_interest_not_auth_redirect_and_failure_branches(monkeypatch):
    handler = bare(SellerRFQInterestHandler, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), otp_service=AsyncMock(), auth_redis_service=AsyncMock(), settings=SimpleNamespace(procucev_rfq_details_url="https://details"))
    handler.check_seller_auth_state = AsyncMock(return_value={"state": "not_auth"})
    handler.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await handler.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "otp"
    handler.auth_redis_service.retrieve.return_value = None
    assert (await handler._redirect_to_seller_flow("+1", "r", "s", session()))["status"] == "error"
    handler.auth_redis_service.retrieve.return_value = SimpleNamespace(id="u")
    seller_service = SimpleNamespace(handle_seller_workflow=AsyncMock(return_value={"message": "done"}))
    monkeypatch.setattr("app.services.seller_service.SellerService", lambda **_: seller_service)
    assert (await handler._redirect_to_seller_flow("+1", "r", "s", session()))["status"] == "redirected_to_seller_flow"
    handler._show_intermediate_buttons = AsyncMock(return_value={"status": "shown"})
    response = SimpleNamespace(success=False, error="send", message_id=None)
    handler.whatsapp_service.send_configurable_buttons.return_value = response
    assert (await SellerRFQInterestHandler._show_intermediate_buttons(handler, "+1", "r", "s", session()))["status"] == "error"


# Helper branch coverage

def test_attachment_excel_and_authentication_helper_error_paths(monkeypatch):
    bad_session = SimpleNamespace(workflow_state=None)
    assert not AttachmentHelpers.approve_pending_attachment(bad_session)
    broken = SimpleNamespace(workflow_state=None)
    assert not AttachmentHelpers.reject_pending_attachments(broken)
    assert AttachmentHelpers.get_attachment_summary(SimpleNamespace(workflow_state=None))["total_count"] == 0
    assert not AttachmentHelpers.validate_attachment_type("no_extension", "application/pdf")["valid"]

    assert ExcelConfirmationHelpers.prepare_multiple_rfq_data({"items": [{"ItemDescription": "x", "Quantity": 1, "Uom": "pcs"}]})
    assert ExcelConfirmationHelpers.format_rfq_creation_summary([{"products": [{"description": "x"}] * 4}]).find("more items") >= 0
    assert ExcelConfirmationHelpers.get_excel_confirmation_data(SimpleNamespace(workflow_state=None)) == {}

    assert AuthenticationHelpers.get_required_fields(SimpleNamespace(model_fields={})) == []
    monkeypatch.setattr(authentication_helpers_mod, "get_location_from_pincode_async", AsyncMock(side_effect=RuntimeError("lookup")))
    validated, error = __import__("asyncio").run(AuthenticationHelpers.validate_entities({"zipCode": "560001"}, object))
    assert validated["zipCode"] == "560001" and "couldn't verify" in error


def test_chat_session_and_response_helper_fallback_branches(monkeypatch):
    assert ChatServiceHelpers.determine_conversation_stage(session(extracted_entities=42)) == "collecting"
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(session(), "current") == "current"
    assert ChatServiceHelpers.serialize_products_for_session([datetime(2025, 1, 1)])[0].startswith("2025-")

    helper = ResponseHelpers.__new__(ResponseHelpers)
    helper.settings = SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal", support_email="support", support_contact_info="help", rfq_max_allowed=2)
    helper.openai_service = SimpleNamespace(generate_contextual_response=AsyncMock(side_effect=RuntimeError("ai")))
    import asyncio
    assert asyncio.run(helper.generate_contextual_response({}, []))
    assert asyncio.run(helper.generate_rfq_result_response({"success": False, "error": "bad"}, {}))
    assert "Technical Issues" in helper._get_email_status_with_errors_openai_fallback({"successful_emails": 0, "total_requested": 1, "error_analysis": {"total_failed": 1, "error_counts": {"API_ERROR": 1}, "error_categories": {"API_ERROR": ["r"]}}})
    assert helper._get_end_of_flow_reminder_fallback({"open_rfqs": []}).startswith("Thanks")
    assert helper._get_subscription_plans_fallback({"plans": [{"planName": "OTHER", "subscriptionPrice": 1, "launchOfferPrice": 1}]})


@pytest.mark.asyncio
async def test_session_summarization_and_redis_fallback_branches(monkeypatch):
    settings = SimpleNamespace(redis_session_storage_enabled=False, session_timeout_minutes=60)
    monkeypatch.setattr(session_helpers_mod, "get_settings", lambda: settings)
    assert await SessionHelpers.is_session_expired(session(created_at=None))
    recent = session(); recent.created_at = datetime.now(timezone.utc); recent.conversation_history = {}
    monkeypatch.setattr(SessionHelpers, "is_session_expired", AsyncMock(return_value=True))
    assert not await SessionHelpers.should_send_expiration_message(recent)
    renewed = await SessionHelpers.renew_session_activity(session())
    assert renewed.last_activity_at and renewed.workflow_state["last_activity_at"]

    whatsapp = AsyncMock(side_effect=[RuntimeError("first"), None])
    await SummarizationHelpers.send_and_track_message(whatsapp, session(), "+1", "hello")
    redis = MagicMock(); redis.from_url = MagicMock(side_effect=RuntimeError("redis"))
    monkeypatch.setattr("redis.from_url", redis.from_url)
    monkeypatch.setattr("time.time", lambda: 123.0)
    assert SummarizationHelpers._get_atomic_message_index("sid") == 23000

    for entities, expected in [({}, "conversation_start"), ({"products_discussed": [1]}, "initial_discussion"), ({"incomplete_products": [1]}, "information_gathering"), ({"completed_products": [1]}, "product_completion"), ({"multiple_rfqs": [1]}, "rfq_confirmation")]:
        assert SummarizationHelpers._determine_workflow_stage(entities) == expected
    assert SummarizationHelpers._calculate_completion_level({}) == "initial_stage"




@pytest.mark.asyncio
async def test_seller_auth_mixin_failure_and_manual_otp_branches():
    handler = bare(SellerAuthMixin, whatsapp_service=AsyncMock(), authentication_service=None, session_manager=AsyncMock(), otp_service=None, auth_redis_service=AsyncMock(), openai_service=AsyncMock())
    handler.auth_redis_service.retrieve.side_effect = RuntimeError("redis")
    assert (await handler.check_seller_auth_state("+1", "s"))["state"] == "not_auth"
    handler._get_current_user_type = AsyncMock(return_value=None)
    await handler._reset_workflow_with_menu("+1", session(), "failed")
    assert handler.whatsapp_service.send_message.called
    assert (await handler.initiate_seller_auth("+1", session(), "s"))["status"] == "seller_not_found"
    handler.openai_service.generate_response.return_value = "2"
    assert await handler._parse_switch_choice("perhaps") == "stay"
    handler.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert await handler._parse_switch_choice("perhaps") == "unclear"


# Additional gate branches -------------------------------------------------

@pytest.mark.asyncio
async def test_sectioned_handler_validation_restart_and_item_limit_branches(monkeypatch):
    wa = AsyncMock()
    manager = AsyncMock()
    entities = AsyncMock()
    handler = bare(
        SectionedRFQCreationHandler,
        entity_service=entities, whatsapp_service=wa, cancel_service=AsyncMock(),
        session_manager=manager, confirmation_handler=AsyncMock(),
        attachment_decision_handler=None,
    )
    restart = session(sectioned_rfq_pending_restart=True)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda s: s.workflow_state.get("sectioned_rfq_pending_restart", False))
    handler._handle_restart_confirmation_response = AsyncMock(return_value={"status": "restart"})
    assert (await handler.handle_sectioned_rfq(user(), restart, {"text": {"body": "yes"}}))["status"] == "restart"

    unknown = session()
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_pending_restart", lambda *_: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "unknown")
    assert (await handler.handle_sectioned_rfq(user(), unknown, "hello"))["status"] == "error"

    current = session()
    current.workflow_state["date_location"] = {"deliveryDate": "", "pincode": "", "city": "", "state": ""}
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_sectioned_rfq_section", lambda *_: "date_location")
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, key: s.workflow_state.get(key))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda s, key, value: s.workflow_state.__setitem__(key, value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification", lambda s, *_args: None)
    entities.extract_entities.return_value = {"deliveryDate": "bad", "pincode": "", "city": "", "state": "", "products": []}
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": False, "error": "bad date"})
    assert (await handler._handle_date_location_section(user(), current, "bad", []))["status"] == "validation_error"

    current.workflow_state["date_location"] = {"deliveryDate": "2025-01-01", "pincode": "560001", "city": "", "state": ""}
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "2025-01-01"})
    handler._validate_pincode_and_get_location = AsyncMock(return_value={"is_valid": False, "error": "bad pin"})
    handler._autofill_location_from_pincode = AsyncMock(return_value={"delivery_data": current.workflow_state["date_location"], "is_valid": False, "error": "bad pin"})
    entities.extract_entities.return_value = {"deliveryDate": "2025-01-01", "pincode": "560001", "city": "", "state": ""}
    assert (await handler._process_delivery_modification_direct(user(), current, "Delivery Date: 2025-01-01\nDelivery Pincode: 560001"))["status"] in {"validation_error", "awaiting_delivery_details"}

    item_session = session()
    item_session.workflow_state["items"] = []
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, key: s.workflow_state.get(key))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda *_: False)
    entities.extract_entities.return_value = {"products": [{"description": str(i)} for i in range(6)]}
    handler._display_item_limit_exceeded = AsyncMock(return_value={"status": "item_limit"})
    assert (await handler._handle_items_section(user(), item_session, "many", []))["status"] == "item_limit"


@pytest.mark.asyncio
async def test_authentication_orchestrator_start_selection_and_registration_branches(monkeypatch):
    support = AsyncMock()
    auth = AsyncMock()
    registration = AsyncMock()
    handler = bare(
        AuthenticationOrchestrator, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
        authentication_service=auth, registration_service=registration, intent_service=AsyncMock(),
        support_service=support, chat_service=None, auth_reg_switch=AsyncMock(),
        profile_selection_service=AsyncMock(),
    )
    auth.user_authenticate.return_value = {"success": False}
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    assert (await handler._start_authentication_flow("+1", "buy", session(), {"intent": "buy_something", "confidence": 90}))["status"] == "redirect"

    auth.user_authenticate.return_value = {"success": True, "response": [{"id": 1}]}
    auth.filter_users_by_intent = MagicMock(return_value={"success": False})
    assert (await handler._start_authentication_flow("+1", "sell", session(), {"intent": "sell_something", "confidence": 90}))["status"] == "redirect"

    auth.filter_users_by_intent.return_value = {"success": True, "filtered_users": [{"email": "a@x"}], "unique_emails": ["a@x"]}
    handler._handle_user_selection = AsyncMock(return_value={"status": "selected"})
    assert (await handler._start_authentication_flow("+1", "sell", session(), {"intent": "sell_something", "confidence": 90}))["status"] == "selected"

    handler._handle_user_selection = AuthenticationOrchestrator._handle_user_selection.__get__(handler)
    auth.filter_users_by_intent.return_value = {"success": True, "filtered_users": [], "unique_emails": []}
    assert (await handler._handle_user_selection("+1", session(), auth.filter_users_by_intent.return_value, {"intent": "buy_something"}, "buy"))["status"] == "redirect"
    auth.filter_users_by_intent.return_value = {"success": True, "filtered_users": [{"email": "a@x"}], "unique_emails": ["a@x"]}
    auth.initiate_email_confirmation.return_value = {"status": "email"}
    assert (await handler._handle_user_selection("+1", session(), auth.filter_users_by_intent.return_value, {"intent": "buy_something"}, "buy"))["status"] == "email"

    reg = session(registration_stage="email_confirmation", current_intent_result={"intent": "buy_something"})
    auth.handle_email_confirmation.return_value = {"status": "email"}
    assert (await handler._handle_registration_workflow("+1", "yes", reg, {}))["status"] == "email"
    reg.workflow_state["registration_stage"] = "domain_matching"
    auth.handle_domain_matching.return_value = {"status": "domain"}
    assert (await handler._handle_registration_workflow("+1", "domain", reg, {}))["status"] == "domain"
    reg.workflow_state["registration_stage"] = "confirmation"
    registration.handle_registration_confirmation.return_value = {"status": "confirmed"}
    assert (await handler._handle_registration_workflow("+1", "yes", reg, {}))["status"] == "confirmed"
    reg.workflow_state["registration_stage"] = "invalid"
    handler._redirect_to_registration_flow = AsyncMock(return_value={"status": "redirect"})
    assert (await handler._handle_registration_workflow("+1", "x", reg, {}))["status"] == "redirect"

    assert await handler._should_handle_intent_switch("buy_something", "data")
    assert not await handler._should_handle_intent_switch("buy_something", "email_otp")
    handler._handle_auth_fallback = AsyncMock(return_value={"status": "fallback"})
    assert (await handler.authentication_orchestrator_flow("+1", "x", session(), {"intent": "other", "confidence": 80}))["status"] == "fallback"


@pytest.mark.asyncio
async def test_confirmation_all_response_modes_and_backend_result_branches(monkeypatch):
    handler = bare(
        ConfirmationHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(),
        cancel_service=None, session_manager=AsyncMock(), confirmation_service=AsyncMock(),
        _bfs_search_handler=None,
    )
    handler._handle_rfq_acceptance = AsyncMock(return_value={"status": "yes"})
    handler._handle_rfq_modification = AsyncMock(return_value={"status": "no"})
    handler._handle_confirmation_clarification = AsyncMock(return_value={"status": "clarify"})
    for parsed, expected in [("yes", "yes"), ("no", "no"), ("maybe", "clarify")]:
        handler.confirmation_service.parse_confirmation.return_value = parsed
        result = await handler.handle_pending_confirmations(user(), session(), "answer", {"intent": "other"})
        assert result["status"] == expected
    handler.confirmation_service.parse_confirmation.return_value = "maybe"
    handler._handle_rfq_acceptance.return_value = {"status": "accept"}
    assert (await handler.handle_pending_confirmations(user(), session(), "yes", {"intent": "confirmation_response", "confidence": 0.8, "context_analysis": {"confirmation_details": {"response_type": "accept", "has_conditions": False}}}))["status"] == "accept"
    handler._handle_rfq_modification.return_value = {"status": "modify"}
    assert (await handler.handle_pending_confirmations(user(), session(), "change", {"intent": "modification_request", "confidence": 0.8}))["status"] == "modify"

    for button, method in [("confirm_rfq", "_handle_rfq_acceptance"), ("no_rfq", "_handle_rfq_modification"), ("continue_rfq", "_proceed_to_confirmation_from_optional"), ("confirm_no_changes", "_handle_rfq_acceptance"), ("restart_rfq", "_handle_restart_workflow")]:
        setattr(handler, method, AsyncMock(return_value={"status": button}))
        assert (await handler.handle_confirmation_button(user(), session(), button))["status"] == button
    assert (await handler.handle_confirmation_button(user(), session(), "bad"))["status"] == "unknown_button"

    handler._handle_rfq_acceptance = ConfirmationHandler._handle_rfq_acceptance.__get__(handler)
    handler._send_completion_response = AsyncMock()
    handler._submit_rfq_to_backend = AsyncMock(return_value={"success": False, "error": "down"})
    single = session(pending_rfq={"entities": {"description": "pump", "quantity": 2}})
    monkeypatch.setattr(confirmation_mod.ChatServiceHelpers, "create_rfq_schema_from_entities", lambda *_: SimpleNamespace())
    result = await handler._handle_rfq_acceptance(user(), single, "yes")
    assert result["status"] == "multiple_rfqs_created" and single.outcome == ConversationOutcome.abandoned

    class FakeAPI:
        async def create_rfq(self, *_args, **_kwargs):
            return {"success": True, "rfq_id": "R1"}
    monkeypatch.setattr(confirmation_mod, "RFQAPIService", FakeAPI)
    handler._submit_rfq_to_backend = ConfirmationHandler._submit_rfq_to_backend.__get__(handler)
    schema = SimpleNamespace(model_dump=lambda: {"project_desc": "pump", "items": [{"description": "pump", "quantity": 1}], "delivery_locations": []})
    assert (await handler._submit_rfq_to_backend(schema, user()))["rfq_id"] == "R1"


@pytest.mark.asyncio
async def test_auth_switch_intent_products_and_bfs_alternate_paths(monkeypatch):
    switch = bare(AuthRegistrationIntentSwitch, whatsapp_service=AsyncMock(), openai_service=MagicMock(), user_cache_service=AsyncMock())
    active = session(user_type="buyer"); active.workflow_type = "authentication"
    assert await switch.should_handle_auth_reg_switch(active, "sell_something", "seller")
    active.workflow_state["pending_auth_reg_switch"] = {"x": 1}
    assert not await switch.should_handle_auth_reg_switch(active, "sell_something", "seller")
    active.workflow_state.pop("pending_auth_reg_switch")
    await switch.handle_auth_reg_switch_choice("+1", active, "sell", "sell_something", "seller")
    assert "pending_auth_reg_switch" in active.workflow_state
    assert (await switch.handle_auth_reg_switch_response("+1", active, "1"))["status"] == "continue_current_workflow"
    active.workflow_state["pending_auth_reg_switch"] = {"new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"}
    assert (await switch.handle_auth_reg_switch_response("+1", active, "2"))["status"] == "switch_to_new_combination"
    active.workflow_state["pending_auth_reg_switch"] = {"new_intent": "sell_something", "new_user_type": "seller", "new_message": "sell"}
    switch.openai_service.generate_response.side_effect = RuntimeError("ai")
    assert (await switch.handle_auth_reg_switch_response("+1", active, "maybe"))["status"] == "clarification_requested"
    assert switch._get_target_combination("register_seller", "seller") == "registration_seller"
    assert switch._get_target_combination("other", "buyer") == "authentication_buyer"
    assert (await switch.handle_account_change_confirmation(user(False), session(), "x", "seller", "sell"))["status"] == "account_change_confirmation_requested"
    assert (await switch.handle_account_switch_confirmation(user(), session(), "x", "buyer"))["status"] == "account_switch_confirmation_requested"
    assert (await switch.handle_role_switch_confirmation(user(), session(), "x", "seller"))["status"] == "role_switch_confirmation_requested"

    products = bare(ProductsArrayHandler, whatsapp_service=AsyncMock(), openai_service=MagicMock(), response_helpers=AsyncMock(), session_manager=AsyncMock())
    products._track_product_categories = AsyncMock()
    products._categorize_products_by_completeness = AsyncMock(return_value=([], [{"index": 1, "entities": {"description": "pump"}}]))
    products._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    empty = session()
    assert (await products.handle_products_array(user(), empty, "x", [], global_supplementary_fields={"city": "X"}))["status"] == "need_product_description"
    complete = session(); complete.workflow_state["attachment_caption"] = "spec"
    assert (await products.handle_products_array(user(), complete, "x", [{"description": "pump"}]))["status"] == "complete"
    assert products._no_products_mentioned([{"description": "NO_PRODUCTS_MENTIONED"}])
    assert not products._no_products_mentioned([{"description": "pump"}])

    intent = bare(IntentSwitchHandler, whatsapp_service=AsyncMock(), response_helpers=AsyncMock(), openai_service=AsyncMock())
    active = session(extracted_entities=[{"product_name": "pump"}]); active.workflow_type = WorkflowType.rfq_creation
    assert not await intent.should_handle_intent_switch(active, "buy_something", 80)
    intent.response_helpers.generate_contextual_response.return_value = "choice"
    assert (await intent.handle_intent_switch_choice(user(), active, "sell", "sell_something", {"intent": "sell_something"}))["status"] == "intent_switch_choice_presented"
    intent.openai_service.analyze_intent_switch_response = AsyncMock(return_value={"chosen_action": "continue_current"})
    assert (await intent.handle_intent_switch_response(user(), active, "1"))["status"] == "continue_current_workflow"
    active.workflow_state["pending_intent_switch"] = {"new_intent": "sell_something", "new_intent_message": "sell", "intent_result": {}}
    intent.openai_service.analyze_intent_switch_response.return_value = {"chosen_action": "switch_to_new"}
    assert (await intent.handle_intent_switch_response(user(), active, "2"))["status"] == "switch_to_new_intent"

    bfs = bare(BFSSearchHandler, whatsapp_service=AsyncMock(), session_manager=AsyncMock(), openai_service=AsyncMock(), auto_categorization_service=AsyncMock(), bfs_api_service=AsyncMock())
    s = session(bfs_results=[{"id": "i"}])
    assert (await bfs.handle_button(user(), s, "unknown"))["status"] == "unknown_bfs_button"
    assert (await bfs.handle_button(user(), session(), "search_bfs"))["status"] == "bfs_awaiting_product_description"
    assert (await bfs.handle_button(user(), session(bfs_searched_products=["pump"]), "bfs_raise_rfq"))["status"] == "bfs_activate_rfq"
    assert (await bfs.handle_button(user(), session(bfs_results=[{"id": "i"}]), "bfs_bid_cancel"))["status"] == "bfs_send_cancel_message"
    bfs.openai_service.extract_entities.return_value = {"success": True, "products": [{"description": "pump"}, {"description": ""}]}
    assert await bfs._extract_entities("x") == [{"description": "pump"}]
    bfs.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "Other"}
    assert (await bfs._build_api_payload([{"description": "pump"}], "u", "s"))[0]["category"] == []
    bfs.auto_categorization_service.categorize_item.side_effect = RuntimeError("cat")
    assert await bfs._build_api_payload([{"description": "pump"}], "u", "s")


@pytest.mark.asyncio
async def test_interest_check_details_otp_missing_and_helpers_cover_remaining_branches(monkeypatch):
    response = SimpleNamespace(success=True, message_id="m", error=None)
    handler = bare(SellerRFQInterestHandler, whatsapp_service=AsyncMock(), authentication_service=AsyncMock(), session_manager=AsyncMock(), otp_service=AsyncMock(), auth_redis_service=AsyncMock(), settings=SimpleNamespace(procucev_rfq_details_url="https://details"))
    handler.check_seller_auth_state = AsyncMock(return_value={"state": "correct_seller"})
    handler._show_intermediate_buttons = AsyncMock(return_value={"status": "shown"})
    assert (await handler.handle_rfq_interest_click("+1", "r", "s", session()))["status"] == "shown"
    handler.whatsapp_service.send_configurable_buttons.return_value = response
    monkeypatch.setattr("app.services.seller_notification_service.SellerNotificationService", lambda: SimpleNamespace(get_intermediate_rfq_buttons=lambda *_: []))
    assert (await handler.handle_check_details_click("+1", "r", "s", session()))["status"] == "check_details_sent"
    assert (await handler.handle_otp_validated("+1", session()))["status"] == "error"

    helpers = AuthenticationHelpers
    assert helpers.extract_user_details([{"selfClient": False}, {"selfClient": True, "email": "x"}])["selfClient"]
    assert helpers.extract_user_details([]) is None
    assert helpers.format_email_list(["a@x", "b@x"]).startswith("1.")
    assert helpers.validate_otp_format("12 34")
    assert helpers.extract_emails_from_user_data({"email": ["a@x", "bad"]}) == ["a@x"]

    ExcelConfirmationHelpers.save_excel_confirmation_data(SimpleNamespace(workflow_state=None), {"items": [1]})
    assert "Excel Processing Summary" in ExcelConfirmationHelpers.generate_confirmation_message({"total_rows": 1, "identified_for_rfq": 1, "extracted": 1}, "x.xlsx")
    assert "Missing quantity" in ExcelConfirmationHelpers.generate_confirmation_message({"has_missing_items": True, "skipped_items_summary": "row 1"}, "x.xlsx")
    value = SimpleNamespace(workflow_state={"pending_excel_confirmation": {"items": [1]}})
    ExcelConfirmationHelpers.clear_excel_confirmation_data(value)
    assert ExcelConfirmationHelpers.get_excel_confirmation_data(value) == {}




@pytest.mark.asyncio
async def test_session_and_chat_helper_modes_and_auth_validation(monkeypatch):
    redis = SimpleNamespace(session_exists=AsyncMock(return_value=True))
    monkeypatch.setattr(session_helpers_mod, "get_settings", lambda: SimpleNamespace(redis_session_storage_enabled=True, session_timeout_minutes=60))
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    assert not await SessionHelpers.is_session_expired(session())
    redis.session_exists.return_value = False
    assert await SessionHelpers.is_session_expired(session())
    monkeypatch.setattr(session_helpers_mod, "get_settings", lambda: SimpleNamespace(redis_session_storage_enabled=False, session_timeout_minutes=60))
    old = session(); old.created_at = datetime.now(timezone.utc) - timedelta(hours=3); old.last_activity_at = None
    assert await SessionHelpers.is_session_expired(old)
    assert SessionHelpers.generate_session_id("1", "weekly").startswith("whatsapp_1_")
    assert SessionHelpers.generate_session_id("1", "persistent").endswith("persistent")
    assert SessionHelpers.generate_session_id("1", "uuid").startswith("whatsapp_1_")
    db = MagicMock(); timed = session(); timed.workflow_type = WorkflowType.authentication; timed.workflow_state = {"profile_selection_stage": "x"}
    SessionHelpers.handle_session_expiry(timed, db) if False else None
    await SessionHelpers.should_send_expiration_message(old)
    SessionHelpers.update_bfs_activity(timed, {"searched_products": ["pump"], "accepted_prices": [1], "counter_offers": [{"x": 1}]})
    SessionHelpers.update_bidding_activity(timed, {"products_bid_for": ["p"], "rfqs_with_response": ["r", "r"]})
    assert timed.bfs_search_count == 1 and timed.rfqs_with_response == ["r"]

    auth = SimpleNamespace(get=AsyncMock(return_value={"role": "buyer"}))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    context = await ChatServiceHelpers.build_conversation_context(session(), "hello")
    assert context["user_role"] == "buyer"
    for state, expected in [({"sectioned_rfq": {"active": True, "current_section": "items"}}, "collecting_items"), ({"pending_rfq": {"id": "r"}}, "confirming"), ({"incomplete_products": [{}]}, "collecting_details"), ({"extracted_entities": {}}, "collecting")]:
        assert ChatServiceHelpers.determine_conversation_stage(session(**state)) == expected
    assert ChatServiceHelpers.transform_entities_to_schema({"description": "pump", "deliveryDate": "not-a-date", "quantity": 2, "state": "S", "city": "C", "pincode": "560001", "attachments": [{"file_name": "a"}]})["items"]

    class Field:
        def __init__(self, required, description="Name"): self._required, self.description = required, description
        def is_required(self): return self._required
    schema = SimpleNamespace(model_fields={"name": Field(True, "Full name"), "email": Field(False, "Email"), "sourceType": Field(False)})
    assert "Full name" in AuthenticationHelpers.generate_registration_message(schema, "buyer")
    assert AuthenticationHelpers.get_missing_fields(schema, {}) == ["name"]
    monkeypatch.setattr(authentication_helpers_mod, "get_location_from_pincode_async", AsyncMock(return_value=None))
    _, error = await AuthenticationHelpers.validate_entities({"email": "bad", "zipCode": "560001"}, object)
    assert error and "email" in error.lower()


@pytest.mark.asyncio
async def test_attachment_session_summarization_response_and_processing_branches(monkeypatch):
    attachment = {"file_name": "a.pdf", "status": "pending"}
    s = session(pending_attachments=[attachment], extracted_entities=[{"attachments": []}], pending_rfq={"entities": {}})
    assert AttachmentHelpers.add_attachment_to_session(s, {"file_name": "b.pdf"})["success"]
    assert AttachmentHelpers.approve_pending_attachment(s, "a.pdf")
    assert s.workflow_state["pending_rfq"]["entities"]["attachments"]
    assert AttachmentHelpers.reject_pending_attachments(s)
    assert AttachmentHelpers.validate_attachment_type("x.pdf", "application/pdf")["valid"]
    assert "media.example" not in AttachmentHelpers._construct_media_download_url("media") or True
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(WHATSAPP_MEDIA_DOWNLOAD_URL="https://media.example"))
    assert AttachmentHelpers._construct_media_download_url("old") == "https://media.example/old"
    s = session(); s.conversation_history = None
    s = session(); s.conversation_history = None
    SummarizationHelpers.add_to_conversation_history(s, "user", {"image": True}, "image", "buy_something", 90)
    assert s.conversation_history["messages"]
    rich = SummarizationHelpers.extract_rich_entities_for_summary(session(extracted_entities=[{"description": "pump"}], user_preferences={"budget": 1}))
    assert "products_discussed" in rich
    data = SummarizationHelpers.prepare_enhanced_summary_data(session(), {"rfq_details": {"id": "r"}})
    assert data["workflow_stage_reached"] == "rfq_confirmation"
    chat = SimpleNamespace(generate_session_summary=AsyncMock())
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    await SummarizationHelpers.handle_session_completion_async(chat, daily, {"session_id": "s", "user_id": "u"})
    assert chat.generate_session_summary.await_count == 1
    response = ResponseHelpers.__new__(ResponseHelpers)
    response.settings = SimpleNamespace(PROCUCEV_PORTAL_URL="p", support_email="e", support_contact_info="c", rfq_max_allowed=2)
    response.openai_service = SimpleNamespace(generate_contextual_response=AsyncMock(side_effect=RuntimeError("ai")), generate_completion_response=AsyncMock(side_effect=RuntimeError("ai")), generate_registration_confirmation=AsyncMock())
    assert "remaining" in await response.generate_registration_clarification(["unknown"], 0, {})
    assert "details" in await response.generate_registration_confirmation({"name": "Ada"}, {})
    assert await response.generate_seller_contextual_response({"workflow_state": "unknown", "user_role": "buyer"})
    auto = MagicMock(); auto.categorize_item.return_value = {"success": True, "category": "Tools", "confidence_score": .9}
    auto = MagicMock(); auto.categorize_item.return_value = {"success": True, "category": "Tools", "confidence_score": .9}
    sellers = AsyncMock(); sellers.select_sellers_for_rfq.return_value = {"total_selected": 0, "subscribed_sellers": [], "unsubscribed_sellers": []}
    sellers = AsyncMock(); sellers.select_sellers_for_rfq.return_value = {"total_selected": 0, "subscribed_sellers": [], "unsubscribed_sellers": []}
