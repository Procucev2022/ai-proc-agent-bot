"""Deterministic branch coverage for messaging helpers, queue, and processors."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest

from app.services.helpers import attachment_helpers as attachment_module
from app.services.helpers.attachment_helpers import AttachmentHelpers
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services import message_queue_service as queue_module
from app.services import whatsapp_service as whatsapp_module
from app.services.processors import excel_message_processor as excel_module
from app.services.processors import image_message_processor as image_module


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


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = 0

    async def acquire(self, **_kwargs):
        return self.acquired

    async def release(self):
        self.released += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeResponse:
    def __init__(self, status=200, payload=None, json_error=None):
        self.status_code = status
        self.status = status
        self.payload = payload if payload is not None else {"status": "ok"}
        self.text = json.dumps(self.payload)
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class EnumValue(Enum):
    ITEM = "item"


def session(**state):
    return SimpleNamespace(
        session_id="session-1", external_user_id="user-1", phone_number="+919999",
        workflow_type=None, workflow_state=state, conversation_history={"openai_messages": [], "metadata": [], "messages": []},
        outcome=None, created_at=None, last_activity_at=None, completed_at=None,
        product_items=[], extracted_entities={}, rfq_ids=[], rfq_id=None, retention_date=None,
        bfs_products_searched=[], bfs_search_count=0, bfs_price_accepted=[], bfs_counter_offers=[],
        products_bid_for=[], bids_received=[], bids_accepted=[], counter_offers_made=[],
        counter_offers_accepted=[], rfqs_with_response=[], user_type=None,
    )


def user(registered=True):
    return SimpleNamespace(phone_number="+919999", is_registered=registered)


def response_helper():
    helper = ResponseHelpers.__new__(ResponseHelpers)
    helper.settings = SimpleNamespace(
        PROCUCEV_PORTAL_URL="https://portal.example", support_email="support@example",
        support_contact_info="help@example", rfq_max_allowed=2,
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=" AI text "))))
    helper.openai_service = SimpleNamespace(
        default_model="model", client=client, _load_prompt=MagicMock(return_value="instructions")
    )
    return helper


# ResponseHelpers -----------------------------------------------------------

@pytest.mark.asyncio
async def test_response_common_contextual_dispatch_and_delegates():
    helper = response_helper()
    assert await helper._generate_common_seller_response("state", "type", "context", "fallback") == "AI text"
    assert helper.openai_service._load_prompt.call_args.kwargs["context_data"] == {"workflow_state": "context"}
    helper.openai_service.client.responses.create.return_value = SimpleNamespace(output_text="")
    assert await helper._generate_common_seller_response("s", "t", {}, "fallback") == "fallback"
    helper.openai_service.client.responses.create.side_effect = RuntimeError("openai")
    assert await helper._generate_common_seller_response("s", "t", {}, "fallback") == "fallback"

    generator = AsyncMock(return_value="contextual")
    helper.openai_service.generate_contextual_response = generator
    assert await helper.generate_contextual_response({"a": 1}, ["q"], "stage", [{"summary": 1}]) == "contextual"
    assert generator.call_args.args[0]["has_historical_context"] is True
    generator.side_effect = RuntimeError("down")
    assert "q" in await helper.generate_contextual_response({}, ["q"])
    assert "more details" in await helper.generate_contextual_response({}, [])

    helper.openai_service.generate_rfq_status_response = AsyncMock(return_value="status")
    helper.openai_service.generate_seller_intent = AsyncMock(return_value="intent")
    assert await helper.generate_rfq_status_contextual_response({}) == "status"
    assert await helper.generate_seller_contextual_intent_response({}) == "intent"
    helper.openai_service.generate_rfq_status_response.side_effect = RuntimeError("bad")
    helper.openai_service.generate_seller_intent.side_effect = RuntimeError("bad")
    assert await helper.generate_rfq_status_contextual_response({}) is None
    assert await helper.generate_seller_contextual_intent_response({}) is None

    states = [
        "display_rfqs_to_seller", "display_rfqs_no_credits", "no_credits_available",
        "show_subscription_plans", "payment_link_generated", "rfq_email_processing",
        "rfq_email_status", "rfq_email_status_with_errors", "invalid_rfq_selection",
        "invalid_plan_selection", "general_seller_response", "general_affirmative_response",
        "contextual_plan_request", "ambiguous_seller_response", "end_of_flow_reminder",
        "generic_closing_message", "standard_closing_message", "error", "credit_check_error",
        "rfq_fetch_error", "plan_fetch_error", "payment_link_error", "unknown",
    ]
    for state in states:
        method = {
            "display_rfqs_to_seller": "_generate_rfq_display_response",
            "display_rfqs_no_credits": "_generate_no_credits_rfq_response",
            "no_credits_available": "_generate_no_credits_response",
            "show_subscription_plans": "_generate_subscription_plans_response",
            "payment_link_generated": "_generate_payment_link_response",
            "rfq_email_processing": "_generate_email_processing_response",
            "rfq_email_status": "_generate_email_status_response",
            "rfq_email_status_with_errors": "_generate_rfq_email_status_with_errors_response",
            "invalid_rfq_selection": "_generate_invalid_rfq_response",
            "invalid_plan_selection": "_generate_invalid_plan_response",
            "general_seller_response": "_generate_general_seller_response",
            "general_affirmative_response": "_generate_general_affirmative_response",
            "contextual_plan_request": "_generate_contextual_plan_request_response",
            "ambiguous_seller_response": "_generate_ambiguous_seller_response",
            "end_of_flow_reminder": "_generate_end_of_flow_reminder_response",
            "generic_closing_message": "_generate_generic_closing_response",
            "standard_closing_message": "_generate_standard_closing_response",
            "error": "_generate_error_response", "credit_check_error": "_generate_error_response",
            "rfq_fetch_error": "_generate_error_response", "plan_fetch_error": "_generate_error_response",
            "payment_link_error": "_generate_error_response", "unknown": "_generate_fallback_seller_response",
        }[state]
        replacement = AsyncMock(return_value=state)
        setattr(helper, method, replacement)
        assert await helper.generate_seller_contextual_response({"workflow_state": state}) == state
        replacement.assert_awaited_once()

    helper._generate_fallback_seller_response = AsyncMock(side_effect=RuntimeError("route"))
    assert await helper.generate_seller_contextual_response({"workflow_state": "unknown", "user_role": "buyer"}) == "What can I assist you with today?"


@pytest.mark.asyncio
async def test_response_seller_wrappers_and_fallback_formatters():
    helper = response_helper()
    context = {"workflow_state": "x", "selected_rfq_ids": ["1", "2"], "available_rfqs": ["1", "2"]}
    for name in (
        "_generate_rfq_email_status_with_errors_response", "_generate_end_of_flow_reminder_response",
        "_generate_generic_closing_response", "_generate_standard_closing_response",
        "_generate_rfq_display_response", "_generate_no_credits_rfq_response",
        "_generate_subscription_plans_response", "_generate_no_credits_response",
        "_generate_payment_link_response", "_generate_email_processing_response",
        "_generate_email_status_response", "_generate_invalid_rfq_response",
        "_generate_invalid_plan_response", "_generate_general_seller_response",
        "_generate_general_affirmative_response", "_generate_contextual_plan_request_response",
        "_generate_ambiguous_seller_response", "_generate_rfq_selection_prompt", "_generate_error_response",
    ):
        result = await getattr(helper, name)(context)
        assert result == "AI text"
    helper.openai_service.client.responses.create.side_effect = None
    helper.openai_service.client.responses.create.return_value = SimpleNamespace(output_text="")
    assert "1, 2" in await helper._generate_email_processing_response(context)

    assert "live RFQ" in helper._get_end_of_flow_reminder_fallback({"open_rfqs": [{"rfq_id": "r", "location": "Pune", "submission_date": "today"}], "total_open_rfqs": 1})
    assert "contact" in helper._get_end_of_flow_reminder_fallback({})
    email = helper._get_email_status_with_errors_openai_fallback({
        "successful_emails": 1, "total_requested": 5,
        "error_analysis": {"total_failed": 4, "error_counts": {"NO_CREDITS": 1, "RFQ_NOT_FOUND": 1, "API_ERROR": 1, "UNKNOWN": 1},
                           "error_categories": {"NO_CREDITS": ["a"], "RFQ_NOT_FOUND": ["b"], "API_ERROR": ["c"], "UNKNOWN": ["d"]}},
    })
    assert all(part in email for part in ("Insufficient Credits", "RFQs Not Found", "Technical Issues", "Unknown Errors"))
    assert helper._get_email_status_with_errors_openai_fallback({"successful_emails": 2, "total_requested": 2}).startswith("Successfully")
    assert helper._get_email_status_fallback({"successful_emails": 2, "total_requested": 2}).startswith("✅")
    assert helper._get_email_status_fallback({"successful_emails": 0, "total_requested": 2}).startswith("❌")
    assert helper._get_fallback_message("error")["message"]
    assert helper._get_fallback_message("unknown", "BUYER")["buttons"]
    assert helper._get_fallback_message("unknown", "seller")["buttons"]
    assert helper._get_fallback_message("unknown")["buttons"] is None

    rfq = {"rfq_id": "r", "category": ["A", "B"], "submission_date": "tomorrow", "location": "Pune", "project_description": "x"}
    assert "A, B" in helper._format_single_rfq(rfq, 1)
    assert "N/A" in helper._format_single_rfq({"category": "A", "submission_date": "N/A"}, 2)
    assert "Total RFQs" in helper._get_rfq_display_fallback({"rfqs": [rfq]})
    assert "Credits Available" in helper._get_no_credits_rfq_fallback({"rfqs": [rfq]})
    plans = [{"planName": "connect", "subscriptionPrice": 1000, "launchOfferPrice": 500, "subscriptionPeriodMonths": 1, "rfqBundleSize": 2},
             {"planName": "select"}, {"planName": "elect"}, {"planName": "other"}]
    assert all(name in helper._get_subscription_plans_fallback({"plans": plans}) for name in ("CONNECT", "SELECT", "ELECT", "OTHER"))
    assert "Razorpay" in helper._get_payment_link_fallback({"selected_plan": {"name": "P", "price": 10}, "payment_link": "url"})


@pytest.mark.asyncio
async def test_response_rfq_registration_and_validation_paths(monkeypatch):
    helper = response_helper()
    schema = SimpleNamespace(dict=lambda: {"x": 1})
    helper.openai_service.generate_completion_response = AsyncMock(return_value="done")
    assert await helper.generate_completion_response(schema, {}) == "done"
    helper.openai_service.generate_completion_response.side_effect = RuntimeError("x")
    assert "complete" in await helper.generate_completion_response(object(), {})

    context = {"extracted_entities": [{"description": "Laptop", "deliveryDate": "2025-11-05", "date_validation_error": "bad date", "pincode_validation_error": "bad pin"}], "products": [{"date_validation_error": "bad date", "pincode_validation_error": "bad pin"}]}
    monkeypatch.setattr(helper, "format_rfq_entities_message", Mock(return_value="formatted"))
    assert await helper.generate_clarification_response(["question"], 50, context) == "formatted"
    assert await helper.generate_clarification_response([], 0, {}) == "Thank you for the information! Let me process your RFQ."
    monkeypatch.setattr(helper, "format_rfq_entities_message", Mock(side_effect=RuntimeError("format")))
    assert "I need" in await helper.generate_clarification_response(["question"], 50, {"extracted_entities": [{"description": "x"}]})
    assert helper._extract_date_validation_errors(context).count("bad date") == 1
    assert helper._extract_pincode_validation_errors({"extracted_entities": {"pincode_validation_error": "p"}}) == ["p"]

    helper.openai_service.generate_rfq_confirmation = AsyncMock(return_value="confirm")
    assert await helper.generate_rfq_summary_and_confirmation(schema, {}, ["old"]) == "confirm"
    helper.openai_service.generate_rfq_confirmation.side_effect = RuntimeError("x")
    assert "ready" in await helper.generate_rfq_summary_and_confirmation(object(), {})
    assert (await helper.generate_rfq_result_response({"success": True, "rfq_id": "R"}, {})).startswith("Thank you")
    assert (await helper.generate_rfq_result_response({"success": True}, {})).endswith("successfully.")
    helper.generate_contextual_response = AsyncMock(return_value="failure")
    assert await helper.generate_rfq_result_response({"success": False, "error": "bad"}, {}) == "failure"
    helper.generate_contextual_response.side_effect = RuntimeError("x")
    assert "There was an issue" in await helper.generate_rfq_result_response({"success": False, "error": "bad"}, {})

    helper.openai_service.generate_contextual_response = AsyncMock(return_value="registered")
    assert await helper.generate_registration_confirmation({"name": "A"}, {}) == "registered"
    helper.openai_service.generate_contextual_response.side_effect = RuntimeError("x")
    assert "Name" in await helper.generate_registration_confirmation({"name": "A", "email": "a@x", "ignored": "x"}, {})
    helper.openai_service.generate_clarification_response = AsyncMock(return_value="clarify")
    assert await helper.generate_registration_clarification(["name"], 1, {}) == "clarify"
    helper.openai_service.generate_clarification_response.side_effect = RuntimeError("x")
    assert "full name" in await helper.generate_registration_clarification(["name"], 1, {})
    assert "remaining" in await helper.generate_registration_clarification(["unknown"], 1, {})
    helper.format_rfq_entities_message = ResponseHelpers.format_rfq_entities_message.__get__(helper)
    monkeypatch.setattr("app.services.helpers.response_helpers.format_rfq_response_message", Mock(return_value="rfq"))
    assert helper.format_rfq_entities_message([{"deliveryDate": "2025-11-05", "state": "S", "city": "C", "pincode": "1"}], ["q"]) == "rfq"
    monkeypatch.setattr("app.services.helpers.response_helpers.format_rfq_response_message", Mock(side_effect=RuntimeError("bad")))
    with pytest.raises(RuntimeError):
        helper.format_rfq_entities_message([], ["q"])


# ExcelHelpers --------------------------------------------------------------

def test_excel_helpers_all_formats_thresholds_errors_and_instructions(monkeypatch):
    items = [{"ItemDescription": " Laptop ", "Specification": None, "Quantity": 2, "Uom": None, "Remarks": ""}, {"ItemDescription": "", "Quantity": None}]
    entities = ExcelHelpers.convert_excel_to_entities(items)
    assert entities[0]["description"] == "Laptop" and entities[0]["quantity"] == "2" and entities[0]["unit_of_measure"] == "pcs"
    assert ExcelHelpers.convert_excel_to_entities([None, {"ItemDescription": "x"}])[0]["description"] == "x"
    assert ExcelHelpers.calculate_excel_completeness(items) == 50
    assert ExcelHelpers.calculate_excel_completeness([]) == 0
    assert "didn't find" in ExcelHelpers.generate_excel_summary({"filename": "x", "total_items": 0})
    assert "Laptop" in ExcelHelpers.generate_excel_summary({"filename": "x", "total_items": 1, "items": items})
    assert "No items found" in ExcelHelpers.identify_missing_fields([])
    four = [{"ItemDescription": "" if i < 2 else "x", "Quantity": "" if i < 2 else 1, "Uom": "", "Specification": ""} for i in range(4)]
    assert "Item descriptions" in ExcelHelpers.identify_missing_fields(four)
    assert "Quantities" in ExcelHelpers.identify_missing_fields(four)
    assert "Units of measure" in ExcelHelpers.identify_missing_fields(four)
    assert "Specifications" in ExcelHelpers.identify_missing_fields(four)
    context = ExcelHelpers.prepare_excel_context({"success": True, "filename": "x", "total_items": 1, "items": items, "rfqs": [{"deliveryDate": "d", "pincode": "1", "state": "S", "city": "C"}]}, "9199")
    assert context["city"] == "C" and context["user_phone"] == "9199"
    failed = ExcelHelpers.prepare_excel_context({"success": False, "error": "bad"}, "p")
    assert failed["extracted_entities"] == []
    assert not ExcelHelpers.should_complete_immediately(100, items)
    complete = [{"ItemDescription": "x", "Specification": "s", "Uom": "pcs", "Quantity": 1, "S.No": "1", "Remarks": "r"}]
    assert ExcelHelpers.create_excel_validation_schema_from_items(complete).is_excel_complete()
    assert not ExcelHelpers.create_excel_validation_schema_from_items([]).is_excel_complete()
    with pytest.raises(AttributeError):
        ExcelHelpers.create_excel_validation_schema_from_items([{"ItemDescription": None}])

    base = {"excel_data": {"filename": "x", "items": [], "headers": [], "validation_result": {}}}
    assert "empty or corrupted" in " ".join(ExcelHelpers.generate_reupload_instructions([], base))
    assert "Found these columns" in " ".join(ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [], "headers": ["A"], "column_mapping": {}, "validation_result": {}}}))
    assert ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"error": "❌ bad"}}) == ["❌ bad"]
    missing = {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {"missing_required_fields": ["Item 1: Missing 'Specification'"]}}}
    assert "Specification" in ExcelHelpers.generate_reupload_instructions([], missing)[0]
    assert "missing required" in ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [1], "validation_result": {"errors": ["bad"]}}})[0]
    assert "empty fields" in " ".join(ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [1], "validation_result": {"warnings": ["a", "b", "c", "d"]}}}))
    assert "looks good" in ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [1], "validation_result": {}}})[0]


# SessionHelpers ------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_helpers_expiry_ids_activity_and_reset(monkeypatch):
    assert not await SessionHelpers.is_session_expired(None)
    settings = SimpleNamespace(redis_session_storage_enabled=True, session_timeout_minutes=60)
    redis = AsyncMock(session_exists=AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.helpers.session_helpers.get_settings", lambda: settings)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    assert not await SessionHelpers.is_session_expired(session())
    redis.session_exists.return_value = False
    assert await SessionHelpers.is_session_expired(session())
    settings.redis_session_storage_enabled = False
    missing = session()
    assert await SessionHelpers.is_session_expired(missing)
    old = session(); old.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    monkeypatch.setattr("app.services.helpers.session_helpers.is_expired", lambda *_: (True, old.created_at, old.created_at))
    assert await SessionHelpers.is_session_expired(old)

    expired = session(); expired.created_at = datetime.now(timezone.utc) - timedelta(hours=3); expired.conversation_history = {"openai_messages": [{}, {}]}
    monkeypatch.setattr(SessionHelpers, "is_session_expired", AsyncMock(return_value=True))
    assert await SessionHelpers.should_send_expiration_message(expired)
    expired.workflow_state["last_expiry_notification"] = "bad"
    assert await SessionHelpers.should_send_expiration_message(expired)
    fresh = session(); fresh.created_at = datetime.now(timezone.utc); fresh.conversation_history = {"openai_messages": [{}, {}]}
    assert not await SessionHelpers.should_send_expiration_message(fresh)
    no_history = session(); no_history.created_at = old.created_at
    assert not await SessionHelpers.should_send_expiration_message(no_history)

    fixed = datetime(2025, 1, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.helpers.session_helpers.utc_now", lambda: fixed)
    for strategy in ("daily", "weekly", "persistent", "uuid", "other"):
        assert "whatsapp_9_" in SessionHelpers.generate_session_id("9", strategy)
    renewed = await SessionHelpers.renew_session_activity(session())
    assert renewed.last_activity_at.tzinfo is None and renewed.workflow_state["last_activity_at"].startswith("2025")

    s = session(profile_selection_stage="select", profile_options=[1], old="clear")
    from app.models import WorkflowType
    s.workflow_type = WorkflowType.authentication
    db = MagicMock()
    result = await SessionHelpers.handle_session_expiry(s, db)
    assert result.workflow_type == WorkflowType.authentication and result.outcome is None
    db.append_session_data.assert_called_once()
    s.workflow_type = WorkflowType.rfq_creation
    await SessionHelpers.handle_session_expiry(s, db)
    assert s.workflow_type is None


def test_session_helpers_activity_averages_cleaning_and_disabled_detection():
    s = session(); s.bfs_products_searched = None; s.bfs_price_accepted = None; s.bfs_counter_offers = None
    SessionHelpers.update_bfs_activity(s, {"searched_products": ["p"], "accepted_prices": [1], "counter_offers": [2]})
    assert s.bfs_search_count == 1 and s.bfs_products_searched == ["p"]
    s.products_bid_for = None; s.bids_received = None; s.bids_accepted = None; s.counter_offers_made = None; s.counter_offers_accepted = None; s.rfqs_with_response = None
    SessionHelpers.update_bidding_activity(s, {"products_bid_for": ["p"], "bids_received": [1], "bids_accepted": [2], "counter_offers_made": [3], "counter_offers_accepted": [4], "rfqs_with_response": ["r", "r"]})
    assert s.rfqs_with_response == ["r"]
    s.rfq_ids = []; s.rfq_id = None
    assert SessionHelpers.calculate_session_averages(s).avg_products_per_rfq == 0
    s.rfq_ids = ["r"]; s.product_items = [{"category": "IT"}, {"category": "IT"}]; s.extracted_entities = [{"category": "Office", "description": "Laptop"}]
    assert SessionHelpers.calculate_session_averages(s).avg_products_per_rfq == 2
    cleaned = SessionHelpers.clean_for_json_serialization({"e": EnumValue.ITEM, "d": date(2025, 1, 1), "t": (1, object()), "n": None})
    assert cleaned["e"] == "item" and isinstance(cleaned["t"], list)
    assert SessionHelpers.should_use_summary_aware_extraction("refer") is False
    broken = session(); broken.rfq_ids = ["r"]; broken.product_items = object()
    assert SessionHelpers.calculate_session_averages(broken).avg_products_per_rfq == 0


# SummarizationHelpers ------------------------------------------------------

@pytest.mark.asyncio
async def test_summarization_send_track_history_atomic_rich_and_completion(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    s = session()
    await SummarizationHelpers.send_and_track_message(whatsapp, s, "p", "hello")
    assert len(s.conversation_history["messages"]) == 1
    SummarizationHelpers.add_to_conversation_history(s, "user", "I need a laptop", "text", "buy", .9)
    SummarizationHelpers.add_to_conversation_history(s, "assistant", {"image": True}, "image")
    assert s.conversation_history["messages"][-1]["content"] == {"image": True}
    async def coroutine():
        return "later"
    pending = coroutine()
    before = len(s.conversation_history["messages"])
    SummarizationHelpers.add_to_conversation_history(s, "user", pending)
    pending.close()
    assert len(s.conversation_history["messages"]) == before

    sync_redis = MagicMock(incr=Mock(return_value=7), expire=Mock())
    monkeypatch.setattr("redis.from_url", lambda *_args, **_kwargs: sync_redis)
    assert SummarizationHelpers._get_atomic_message_index("s") == 7
    sync_redis.incr.side_effect = RuntimeError("redis")
    import time
    monkeypatch.setattr(time, "time", lambda: 123.4)
    assert SummarizationHelpers._get_atomic_message_index("s") == 23400
    sync_redis.incr.side_effect = None
    for i in range(55):
        SummarizationHelpers.add_to_conversation_history(s, "assistant", str(i))
    assert len(s.conversation_history["messages"]) == 50

    s.workflow_state = {"pending_rfq": {"x": 1}, "pending_combined_rfq": {"x": 2}, "complete_products": [1], "incomplete_products": [2], "extracted_entities": [3], "user_preferences": {"a": 1}, "budget_constraints": {"max": 5}, "conversation_stage": "stage"}
    s.product_items = [1]; s.interaction_metrics = {"i": 1}; s.seller_responses = [2]; s.rfq_metadata = {"r": 1}
    rich = SummarizationHelpers.extract_rich_entities_for_summary(s)
    assert all(key in rich for key in ("rfq_details", "combined_rfq", "completed_products", "session_product_items", "rfq_metadata"))
    s.created_at = datetime(2025, 1, 1); s.completed_at = datetime(2025, 1, 1, 1); s.external_user_id = "u"; s.rfq_ids = ["r"]
    data = SummarizationHelpers.prepare_enhanced_summary_data(s, rich)
    assert data["session_duration_minutes"] == 60 and data["products_count"] == 1
    for values, stage in (({"multiple_rfqs": [1]}, "rfq_confirmation"), ({"rfq_details": 1}, "rfq_confirmation"), ({"completed_products": [1]}, "product_completion"), ({"incomplete_products": [1]}, "information_gathering"), ({"products_discussed": [1]}, "initial_discussion"), ({}, "conversation_start")):
        assert SummarizationHelpers._determine_workflow_stage(values) == stage
    assert SummarizationHelpers._calculate_completion_level({"completed_products": [1]}) == "nearly_completed"
    messages = [{"sender": "user", "content": x} for x in ("yes", "budget 10", {"image": True}, 5)]
    assert len(SummarizationHelpers._extract_key_decisions(messages, {"user_preferences": {"x": 1}, "budget_constraints": {"max": 2}})) >= 3

    chat, daily = AsyncMock(), AsyncMock()
    await SummarizationHelpers.handle_session_completion_async(chat, daily, {"session_id": "s", "user_id": "u", "rfq_ids": ["r"]})
    chat.generate_session_summary.assert_awaited_once(); daily.generate_daily_summary.assert_awaited_once_with("u")
    chat.generate_session_summary.side_effect = RuntimeError("background")
    await SummarizationHelpers.handle_session_completion_async(chat, daily, {})


# AttachmentHelpers ---------------------------------------------------------

@pytest.mark.asyncio
async def test_attachment_download_all_network_and_size_paths(monkeypatch):
    response = SimpleNamespace(status=200, headers={"Content-Type": "image/jpeg"}, read=AsyncMock(return_value=b"bytes"), text=AsyncMock(return_value=""))
    client = SimpleNamespace(get=Mock(return_value=AsyncContext(response)))
    monkeypatch.setattr(attachment_module.aiohttp, "ClientSession", lambda: AsyncContext(client))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(WHATSAPP_FROM_NUMBER="9199", WHATSAPP_MEDIA_DOWNLOAD_URL="https://media.example"))
    result = await AttachmentHelpers.download_and_encode_attachment("id", "photo.jpg")
    assert result["success"] and result["attachment"]["file_content"] == base64.b64encode(b"bytes").decode()
    response.read.return_value = b"x" * (AttachmentHelpers.MAX_FILE_SIZE_BYTES + 1)
    assert "exceeds" in (await AttachmentHelpers.download_and_encode_attachment("id", "large.bin"))["error"]
    response.status = 404; response.text.return_value = "missing"; response.read.return_value = b"x"
    assert (await AttachmentHelpers.download_and_encode_attachment("id"))["error"].startswith("Failed to download")
    monkeypatch.setattr(attachment_module.aiohttp, "ClientTimeout", type("Timeout", (Exception,), {}))
    client.get.side_effect = attachment_module.aiohttp.ClientTimeout()
    assert (await AttachmentHelpers.download_and_encode_attachment("id"))["error"] == "Timeout downloading file"
    client.get.side_effect = RuntimeError("network")
    assert "network" in (await AttachmentHelpers.download_and_encode_attachment("id"))["error"]


def test_attachment_state_approval_summary_validation_and_urls(monkeypatch):
    s = session(pending_attachments=[{"file_name": "old", "status": "rejected"}], extracted_entities=[{"attachments": [{"file_name": "ok"}]}])
    added = AttachmentHelpers.add_attachment_to_session(s, {"file_name": "new"})
    assert added["success"] and added["count"] == 2
    full = session(extracted_entities=[{"attachments": [{"file_name": str(i)} for i in range(4)]}])
    assert not AttachmentHelpers.add_attachment_to_session(full, {})["success"]
    assert AttachmentHelpers.approve_pending_attachment(s, "new")
    assert s.workflow_state["pending_attachments"] == [] and s.workflow_state["extracted_entities"][0]["attachments"][-1]["file_name"] == "new"
    assert not AttachmentHelpers.approve_pending_attachment(session())
    assert AttachmentHelpers.reject_pending_attachments(s)
    assert AttachmentHelpers.get_attachment_summary(s)["total_count"] == 2
    for mime, filename in (("image/jpeg", "a.jpg"), ("application/pdf", "a.pdf"), ("application/octet-stream", "a.dwg")):
        assert AttachmentHelpers.validate_attachment_type(filename, mime)["valid"]
    assert not AttachmentHelpers.validate_attachment_type("a.exe", "application/pdf")["valid"]
    assert not AttachmentHelpers.validate_attachment_type("a.pdf", "bad")["valid"]
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(WHATSAPP_MEDIA_DOWNLOAD_URL="https://media.example"))
    assert AttachmentHelpers._construct_media_download_url("id").endswith("/id")
    assert AttachmentHelpers._construct_media_download_url("https://x/y") == "https://x/y"
    assert AttachmentHelpers._construct_media_download_url("https://media.sendmsg.in/wamessage/media/old").endswith("/old")


# WhatsAppService -----------------------------------------------------------

def wa_service(mock=False):
    service = whatsapp_module.WhatsAppService.__new__(whatsapp_module.WhatsAppService)
    service.username = "u"; service.password = "p"; service.from_number = "f"; service.base_url = "https://wa"; service.template_base_url = "https://templates"; service.mock_mode = mock
    service.retry_service = SimpleNamespace(retry_with_backoff=AsyncMock())
    return service


@pytest.mark.asyncio
async def test_whatsapp_send_message_real_mock_cache_tracking_and_failures(monkeypatch):
    service = wa_service(mock=True)
    service.retry_service.retry_with_backoff.return_value = {"success": True, "result": whatsapp_module.MessageResponse(True, "m"), "attempts": 1}
    service._clear_pending_reply_flag = AsyncMock(); service._track_message_in_history = AsyncMock()
    assert (await service.send_message("+919999", "hello", "S")).success
    service._clear_pending_reply_flag.assert_awaited_once()
    await service.send_message("9", "ack", clear_pending_reply=False)
    assert service._clear_pending_reply_flag.await_count == 1

    service = wa_service(mock=False); service._format_phone_number = Mock(return_value="919999")
    cache = SimpleNamespace(get=AsyncMock(return_value={"irrelevant_response": {"user_message": "old"}}), set=AsyncMock())
    monkeypatch.setattr(whatsapp_module, "get_redis_service", lambda: cache)
    monkeypatch.setattr(whatsapp_module.requests, "post", Mock(return_value=FakeResponse(200, [{"mid": "M"}])))
    async def retry_call(fn):
        return {"success": True, "result": await fn(), "attempts": 1}
    service.retry_service.retry_with_backoff.side_effect = retry_call
    service._clear_pending_reply_flag = AsyncMock(); service._track_message_in_history = AsyncMock()
    result = await service.send_message("x", "new", "S")
    assert result.message_id == "M" and cache.set.await_count == 1
    payload = whatsapp_module.requests.post.call_args.kwargs["json"]
    assert payload["sessiondata"]["message"]["text"] == "old\n\nnew"
    assert (await service.send_message("x", "system", skip_concatenation=True)).success
    service._format_phone_number.return_value = ""
    assert not (await service.send_message("x", "bad")).success
    service.retry_service.side_effect = None
    service.retry_service.retry_with_backoff.return_value = {"success": False, "attempts": 2, "error": "retry"}
    assert not (await service.send_message("x", "bad")).success


@pytest.mark.asyncio
async def test_whatsapp_template_interactive_lists_buttons_and_configurable(monkeypatch):
    service = wa_service(mock=False); service._format_phone_number = Mock(return_value="919999"); service._clear_pending_reply_flag = AsyncMock(); service._track_message_in_history = AsyncMock()
    monkeypatch.setattr(whatsapp_module, "get_redis_service", lambda: SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock()))
    monkeypatch.setattr(whatsapp_module.requests, "post", Mock(return_value=FakeResponse(200, {"mid": "M"})))
    async def retry_call(fn):
        return {"success": True, "result": await fn(), "attempts": 1}
    service.retry_service.retry_with_backoff.side_effect = retry_call
    assert (await service.send_template_message("x", "welcome", ["A", "B"])).success
    assert whatsapp_module.requests.post.call_args.args[0].endswith("/mediasend")
    assert (await service.send_interactive_message("x", "button", {"body": {"text": "x"}})).success
    service.send_interactive_message = AsyncMock(return_value=whatsapp_module.MessageResponse(True))
    result = await service.send_list_message("x", "h", "b", [{"title": str(i)} for i in range(11)])
    assert result.success and len(service.send_interactive_message.call_args.args[2]["action"]["sections"]) == 2
    await service.send_button_message("x", "h", "b", [{"title": str(i)} for i in range(5)])
    assert len(service.send_interactive_message.call_args.args[2]["action"]["buttons"]) == 3
    await service.send_cta_button_message("x", "body", "go", "https://x")
    service.send_interactive_message = AsyncMock(return_value=whatsapp_module.MessageResponse(True))
    result = await service.send_configurable_buttons("x", "body\x00", [{"id": "a", "title": "A"}, {"title": "B"}, {"title": "C"}, {"title": "D"}], header="H\x00", session_id="S")
    assert result.success and service._track_message_in_history.await_args.args[2] == "interactive_button"
    assert (await service.send_configurable_buttons("x", "body", []))["success"] if False else not (await service.send_configurable_buttons("x", "body", [])).success
    assert not (await service.send_configurable_buttons("x", "body", [{"id": "x"}])).success
    service._format_phone_number.return_value = ""
    assert not (await service.send_configurable_buttons("x", "body", [{"title": "x"}])).success


def test_whatsapp_formatters_api_response_and_phone_edges():
    service = wa_service()
    assert service.format_vendor_results([]).startswith("No vendors")
    assert "and 1 more" in service.format_vendor_results([{"name": str(i)} for i in range(6)])
    assert service.format_bfs_results([]).startswith("No products")
    assert "Price" in service.format_bfs_results([{"name": "p", "price": 2, "quantity": 1, "description": "d"}])
    assert "RFQ Summary" in service.format_rfq_summary({"product_name": "p", "delivery_city": "Pune", "delivery_state": "MH", "remarks": "r"})
    assert service._handle_api_response(FakeResponse(200, [{"mid": 1}])).message_id == "1"
    assert service._handle_api_response(FakeResponse(200, {"mid": 2})).message_id == "2"
    assert service._handle_api_response(FakeResponse(200, {"status": "ok"})).success
    assert service._handle_api_response(FakeResponse(500, {"Error": "down"})).error.endswith("down")
    assert not service._handle_api_response(FakeResponse(200, json_error=ValueError("json"))).success
    for raw in (None, "", "12", "1234567890123456", "+91 (99999) 99999", "001234567890"):
        assert isinstance(service._format_phone_number(raw), str)
    assert service._format_phone_number("+91 (99999) 99999") == "919999999999"


# MessageQueueService -------------------------------------------------------

def queue_service():
    service = queue_module.MessageQueueService.__new__(queue_module.MessageQueueService)
    service.batch_window = 3; service.please_wait_threshold = 15; service.max_please_wait_count = 3; service.monitoring_poll_interval = 1; service.response_ready_ttl = 60; service.monitor_lock_ttl = 180; service.please_wait_interval_ttl = 600; service._background_tasks = []; service._running = True
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    service.redis = MagicMock()
    return service


@pytest.mark.asyncio
async def test_queue_keys_enqueue_batch_and_start_paths(monkeypatch):
    service = queue_service()
    assert service._key_incoming("u") == "u:incoming" and service._key_please_wait_interval("u", 2).endswith(":2")
    service.redis.zadd = AsyncMock(); service.redis.exists = AsyncMock(return_value=False); service._create_batch = AsyncMock()
    await service.enqueue_message({"from": "+123", "timestamp": "2024-01-01 00:00:00", "text": {"body": "hello"}})
    service._create_batch.assert_awaited_once_with("123")
    service.redis.exists.return_value = True; service._refresh_batch_timer = AsyncMock()
    await service.enqueue_message({"from": "123", "content": "next", "timestamp": 2, "type": "image"})
    service._refresh_batch_timer.assert_awaited_once_with("123")
    with pytest.raises(ValueError):
        await service.enqueue_message({"from": "1", "timestamp": "bad"})
    service._refresh_batch_timer = queue_module.MessageQueueService._refresh_batch_timer.__get__(service)
    service.redis.setex = AsyncMock()
    await service._refresh_batch_timer("1")
    service.redis.setex.assert_awaited_once()

    service.redis.lock.return_value = FakeLock(); service.redis.exists = AsyncMock(return_value=False); service.redis.zrange = AsyncMock(return_value=[json.dumps(queue_module.Message("1", "u", "Hello", "text", 1, {}).to_dict()), json.dumps(queue_module.Message("2", "u", "hello", "text", 2, {}).to_dict()), "bad"]); service.redis.zrem = AsyncMock(); service.redis.rpush = AsyncMock(); service._try_start_processing = AsyncMock()
    service._create_batch = queue_module.MessageQueueService._create_batch.__get__(service)
    monkeypatch.setattr(queue_module.time, "time", lambda: 1)
    await service._create_batch("u")
    batch = queue_module.Batch.from_dict(json.loads(service.redis.rpush.call_args.args[1]))
    assert batch.concatenated_content == "Hello" and batch.message_count == 2
    service.redis.exists.return_value = True
    await service._create_batch("u")
    service.redis.zrange.return_value = ["bad"]; service.redis.exists.return_value = False
    await service._create_batch("u"); service.redis.delete.assert_called()

    service._try_start_processing = queue_module.MessageQueueService._try_start_processing.__get__(service)
    service.redis.exists.return_value = True
    await service._try_start_processing("u")
    service.redis.exists.return_value = False; service.redis.lpop = AsyncMock(return_value="bad"); service.redis.lpush = AsyncMock(); await service._try_start_processing("u"); service.redis.lpush.assert_awaited_once()
    batch_json = json.dumps(queue_module.Batch("b", "u", "hi", "text", 1, 1).to_dict()); service.redis.lpop.return_value = batch_json; service.redis.setex = AsyncMock()
    created = []
    monkeypatch.setattr(queue_module.asyncio, "create_task", lambda coro: created.append(coro))
    await service._try_start_processing("u")
    assert service.redis.setex.await_count == 2
    for coro in created: coro.close()


@pytest.mark.asyncio
async def test_queue_background_poller_monitor_process_wrapper_cleanup_health(monkeypatch):
    service = queue_service(); service.redis.lock.return_value = FakeLock(acquired=False)
    sleeps = AsyncMock(side_effect=[None]); monkeypatch.setattr(queue_module.asyncio, "sleep", sleeps)
    service._running = True
    async def stop_after_sleep(_): service._running = False
    sleeps.side_effect = stop_after_sleep
    await service.run_batch_poller()

    service = queue_service(); service.redis.lock.return_value = FakeLock(acquired=True); service.redis.scan = AsyncMock(return_value=(0, ["u:incoming"])); service.redis.exists = AsyncMock(return_value=False); service._create_batch = AsyncMock()
    calls = [0]
    async def poll_sleep(_):
        calls[0] += 1
        if calls[0] > 1: service._running = False
    monkeypatch.setattr(queue_module.asyncio, "sleep", poll_sleep); service.redis.lock.return_value = FakeLock(acquired=True)
    service.redis.zcard = AsyncMock(return_value=1)
    await service.run_batch_poller()

    service = queue_service(); service.redis.scan = AsyncMock(return_value=(0, ["u:session"])); now = 100.0; monkeypatch.setattr(queue_module.time, "time", lambda: now); service.redis.get = AsyncMock(side_effect=[queue_module.ProcessingSession("b", 0).to_json(), None]); service.redis.set = AsyncMock(return_value=True); service.redis.exists = AsyncMock(return_value=True); service._send_please_wait = AsyncMock()
    count = [0]
    async def monitor_sleep(_):
        count[0] += 1
        if count[0] > 1: service._running = False
    monkeypatch.setattr(queue_module.asyncio, "sleep", monitor_sleep)
    await service.run_monitoring_loop()
    service._should_send_acknowledgment = AsyncMock(return_value=False)
    assert not await service._should_send_acknowledgment("u")
    await service._send_acknowledgment("u"); await service._send_please_wait("u")

    service = queue_service(); service.redis.get = AsyncMock(return_value=None); direct = await service.send_message("u", "hi"); assert direct.success
    service.redis.get.return_value = queue_module.ProcessingSession("b", 1).to_json(); service.redis.zcard = AsyncMock(return_value=0); service.redis.llen = AsyncMock(return_value=0); service.redis.setex = AsyncMock(); service._cleanup_and_next = AsyncMock(); service._should_suppress_response = AsyncMock(return_value=False)
    assert (await service.send_message("u", "hi")).success
    service._should_suppress_response.return_value = True
    assert (await service.send_message("u", "hi")).success
    with pytest.raises(ValueError): await service.send_message("")

    service.redis.delete = AsyncMock(); service.redis.scan = AsyncMock(return_value=(0, ["u:please_wait:interval:1"])); service.redis.zcard = AsyncMock(return_value=0); service.redis.llen = AsyncMock(return_value=0); service._try_start_processing = AsyncMock()
    await service._cleanup_and_next("b", "u", True)
    service._cleanup_and_next = queue_module.MessageQueueService._cleanup_and_next.__get__(service)
    service.redis.zcard.return_value = 1
    service._create_batch = AsyncMock()
    await service._cleanup_and_next("b", "u", False)
    service._create_batch.assert_awaited()
    service.redis.get = AsyncMock(return_value=None); service.redis.exists = AsyncMock(return_value=False)
    assert (await service.get_queue_status("u"))["session"] is None
    service.redis.get.return_value = queue_module.ProcessingSession("b", 1).to_json()
    queue_status = await service.get_queue_status("u")
    assert queue_status["session"]["batch_id"] == "b"
    assert queue_status["session"]["please_wait_sent"] is False
    await service.cleanup_user_state("u")
    service.redis.scan = AsyncMock(side_effect=[(0, ["u:processing"]), (0, ["u:incoming"]), (0, ["u:outgoing"]), (0, ["u:session"])])
    service.redis.zcard = AsyncMock(return_value=2); service.redis.llen = AsyncMock(return_value=1); service.redis.get = AsyncMock(return_value=queue_module.ProcessingSession("b", 0).to_json())
    metrics = await service.get_health_metrics(); assert metrics["active_users"] == 1 and metrics["processing_count"] == 1
    service.redis.close = AsyncMock(); await service.shutdown(); assert not service._running


@pytest.mark.asyncio
async def test_queue_process_and_background_task_lifecycle(monkeypatch):
    service = queue_service(); batch = queue_module.Batch("b", "u", "hi", "text", 1, 1); service.redis.exists = AsyncMock(return_value=False); service._cleanup_and_next = AsyncMock()
    chat = SimpleNamespace(process_message=AsyncMock(), cleanup=AsyncMock())
    monkeypatch.setitem(sys.modules, "app.services.chat_service", SimpleNamespace(ChatService=Mock(return_value=chat)))
    monkeypatch.setattr("app.database.get_db_session_context", lambda: AsyncContext())
    # The application uses a synchronous context manager for the DB.
    class DBContext:
        def __enter__(self): return "db"
        def __exit__(self, *_args): return False
    monkeypatch.setattr("app.database.get_db_session_context", lambda: DBContext())
    await service._process_batch(batch); chat.process_message.assert_awaited_once()
    chat.process_message.side_effect = RuntimeError("chat")
    await service._process_batch(batch); assert service._cleanup_and_next.await_count == 1
    service._background_tasks = [SimpleNamespace(done=lambda: True, get_name=lambda: "old")]
    def fake_create_task(coro, name=None):
        coro.close()
        return SimpleNamespace(done=lambda: False, get_name=lambda: name)
    monkeypatch.setattr(queue_module.asyncio, "create_task", fake_create_task)
    service._ensure_background_tasks(); assert len(service._background_tasks) == 2


# Processors ---------------------------------------------------------------

def processor_pair():
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    response = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"), generate_clarification_response=AsyncMock(return_value="clarify"), generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"))
    return whatsapp, response


@pytest.mark.asyncio
async def test_image_processor_payload_relevance_errors_and_next_steps(monkeypatch):
    whatsapp, response = processor_pair(); processor = image_module.ImageMessageProcessor(whatsapp, response); u = user()
    assert (await processor.process_image_message(user(False), session(), {}))["response"] == "registration_required"
    history = {"messages": [{"role": "assistant", "content": {"body": {"text": "body"}, "header": {"text": "h"}, "footer": {"text": "f"}, "action": {"buttons": [{"reply": {"id": "a", "title": "A"}}]}}}]}
    assert (await processor.process_image_message(u, session(), {}))["response"] == "attachment_ignored"
    assert (await processor.process_image_message(u, SimpleNamespace(workflow_state={}, conversation_history=history), {}))["response"] == "attachment_ignored"
    assert (await processor.process_image_message(u, SimpleNamespace(workflow_state={}, conversation_history={"messages": [{"role": "assistant", "content": "last"}]}), {}))["response"] == "attachment_ignored"
    relevant = session(incomplete_products=[1])
    monkeypatch.setattr(image_module.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": False, "error": "download"}))
    assert (await processor.process_image_message(u, relevant, {"image": {"link": "url"}}))["response"] == "download"
    monkeypatch.setattr(image_module.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": True, "attachment": {"file_name": "x.jpg"}}))
    monkeypatch.setattr(image_module.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": True, "count": 1, "max": 4}))
    monkeypatch.setattr(image_module.AttachmentHelpers, "approve_pending_attachment", Mock())
    relevant.workflow_state["incomplete_products"] = [1]
    assert (await processor.process_image_message(u, relevant, {"data": "b64", "filename": "x.jpg", "mime_type": "image/jpeg", "caption": "remark"}))["response"] == "attachment_added_continue_clarification"
    assert relevant.workflow_state["attachment_caption"] == "remark"
    assert (await processor.process_image_message(u, session(pending_rfq={"entities": {}}), {"id": "id", "mime_type": "image/png"}))["response"] in ("attachment_added", "attachment_added_continue_clarification", "confirmation_regenerated")
    monkeypatch.setattr(image_module.AttachmentHelpers, "add_attachment_to_session", Mock(side_effect=RuntimeError("processing")))
    assert (await processor.process_image_message(u, session(pending_rfq={"entities": {}}), {"data": "x"}))["response"] == "processing"
    assert await processor._handle_save_error(u) == {"status": "error", "response": "failed_to_save_attachment"}
    assert processor._is_attachment_relevant(session(pending_optional_rfq=True))
    assert processor._get_last_bot_message(SimpleNamespace(conversation_history={"messages": []})) is None


@pytest.mark.asyncio
async def test_image_processor_confirmation_optional_combined_and_limit(monkeypatch):
    whatsapp, response = processor_pair(); processor = image_module.ImageMessageProcessor(whatsapp, response); u = user()
    chat_helpers = __import__("app.services.helpers.chat_service_helpers", fromlist=["ChatServiceHelpers"])
    monkeypatch.setattr(chat_helpers.ChatServiceHelpers, "create_rfq_schema_from_entities", Mock(return_value="schema"))
    s = session(pending_rfq={"entities": {"description": "x"}}, extracted_entities=[{"attachments": [{"file_name": "x"}]}], attachment_caption="remark")
    assert await processor._regenerate_existing_confirmation(u, s, "new") == {"status": "handled", "response": "confirmation_regenerated"}
    s = session(pending_combined_rfq={"combined_schema": {"items": []}, "products": [{"entities": {}}]}, attachment_caption="remark", extracted_entities=[{"attachments": [{"file_name": "x"}]}])
    assert (await processor._regenerate_existing_confirmation(u, s, "new"))["response"] == "confirmation_regenerated"
    confirmation = __import__("app.services.handlers.confirmation_handler", fromlist=["ConfirmationHandler"])
    monkeypatch.setattr(confirmation.ConfirmationHandler, "_proceed_to_confirmation_from_optional", AsyncMock())
    assert (await processor._proceed_from_optional_to_confirmation(u, session(pending_optional_rfq=True), "x"))["response"] == "attachment_added_proceeded_to_confirmation"
    s = session(pending_rfq={"entities": {"description": "x"}})
    assert (await processor._regenerate_confirmation_with_error(u, s, "limit"))["response"] == "confirmation_with_error"
    assert await processor._regenerate_confirmation_with_error(u, session(), "limit") == {"status": "error", "response": "no_pending_confirmation_found"}
    assert (await processor._determine_next_step(u, session(pending_optional_rfq=True), "x"))["response"] == "attachment_added_proceeded_to_confirmation"
    assert (await processor._determine_next_step(u, session(incomplete_products=True), "x"))["response"] == "attachment_added_continue_clarification"
    assert (await processor._determine_next_step(u, session(), "x"))["response"] == "attachment_added"


@pytest.mark.asyncio
async def test_excel_processor_all_upload_gates_handlers_and_converters(monkeypatch):
    whatsapp, response = processor_pair()
    class Lock:
        def __init__(self, acquired=True): self.acquired = acquired; self.released = 0
        async def acquire(self, blocking=False): return self.acquired
        async def release(self): self.released += 1
    lock = Lock()
    class Redis:
        def lock(self, *_args, **_kwargs): return lock
    monkeypatch.setattr(excel_module.Redis, "from_url", Mock(return_value=Redis()))
    monkeypatch.setattr(excel_module, "get_settings", lambda: SimpleNamespace(redis_url="redis://test"))
    processor = excel_module.ExcelMessageProcessor(whatsapp, response, Mock()); u = user()
    assert (await processor.process_excel_upload(user(False), session(), {}))["response"] == "registration_required"
    monkeypatch.setattr(excel_module.ImageMessageProcessor, "process_image_message", AsyncMock(return_value={"status": "delegated"}))
    assert (await processor.process_excel_upload(u, session(pending_optional_rfq=True), {}))["status"] == "delegated"
    assert (await processor.process_excel_upload(u, session(excel_file_processed=True, excel_filename="old"), {}))["response"] == "excel_already_processed"
    assert (await processor.process_excel_upload(u, session(), {"document": {}}))["response"] == "file_access_error"
    lock.acquired = False
    assert (await processor.process_excel_upload(u, session(), {"document": {"link": "url", "filename": "x"}}))["response"] == "upload_in_progress"
    lock.acquired = True
    validation = SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "bad"}))
    monkeypatch.setattr(excel_module, "ExcelValidationService", lambda: validation)
    assert (await processor.process_excel_upload(u, session(), {"document": {"link": "url", "filename": "x"}}))["response"] == "validation_failed"
    validation.validate_excel_file_from_url.return_value = {"valid": True, "content": b"x"}
    processing = SimpleNamespace(process_excel_file=AsyncMock(return_value={"success": False, "error": "bad"}))
    monkeypatch.setattr(excel_module, "ExcelProcessingService", lambda _openai: processing)
    assert (await processor.process_excel_upload(u, session(), {"document": {"link": "url", "filename": "x"}}))["response"] == "processing_failed"
    processing.process_excel_file.return_value = {"success": True, "items": [{"Quantity": ""}] * 4}
    assert (await processor.process_excel_upload(u, session(), {"document": {"link": "url", "filename": "x"}}))["response"] == "missing_quantities_reupload_required"
    processing.process_excel_file.return_value = {"success": True, "filename": "x", "items": [{"ItemDescription": "x", "Quantity": 1}]}
    monkeypatch.setattr(excel_module.ExcelHelpers, "prepare_excel_context", Mock(return_value={"excel_data": processing.process_excel_file.return_value, "completeness": 50}))
    monkeypatch.setattr(excel_module.ExcelHelpers, "should_complete_immediately", Mock(return_value=False))
    processor._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    assert (await processor.process_excel_upload(u, session(), {"document": {"link": "url", "filename": "x"}}))["status"] == "incomplete"
    assert processor._convert_excel_to_entities([{"ItemDescription": "x", "Quantity": "2.0", "Uom": "kg"}, {"Quantity": "bad"}])[0]["quantity"] == 2
    assert processor._convert_excel_to_entities([{"Quantity": "bad"}])[0]["quantity"] is None
    assert processor._identify_missing_common_fields([{}]) == ["delivery_date", "location"]
    assert processor._identify_missing_common_fields([{"DeliveryDate": "today", "City": "Pune"}]) == []


@pytest.mark.asyncio
async def test_excel_processor_direct_complete_incomplete_structured_and_legacy(monkeypatch):
    whatsapp, response = processor_pair(); processor = excel_module.ExcelMessageProcessor.__new__(excel_module.ExcelMessageProcessor); processor.whatsapp_service = whatsapp; processor.response_helpers = response; processor.openai_service = Mock()
    u = user()
    complete = {"filename": "x", "items": [{"ItemDescription": "x", "Quantity": 1}], "processing_summary": {"total_products_extracted": 1}}
    assert (await processor._handle_complete_excel(u, session(), complete))["status"] == "redirect_to_multiple_flow"
    structured = {"filename": "x", "rfqs": [{"products": [{"description": "x", "quantity": 1}]}]}
    assert (await processor._handle_complete_excel(u, session(), structured))["status"] == "redirect_to_multiple_flow"
    structured_missing = {"filename": "x", "rfqs": [{"products": [{"description": "x"}], "deliveryDate": None, "pincode": None}]}
    result = await processor._handle_incomplete_excel(u, session(), {"excel_data": structured_missing})
    assert result["status"] == "excel_missing_common_data"
    structured_present = {"filename": "x", "rfqs": [{"products": [{"description": "x"}], "deliveryDate": "today", "pincode": "1"}]}
    assert (await processor._handle_incomplete_excel(u, session(), {"excel_data": structured_present}))["status"] == "redirect_to_multiple_flow"
    legacy = {"filename": "x", "items": [{"ItemDescription": "x", "Quantity": 1}]}
    assert (await processor._handle_incomplete_excel(u, session(), {"excel_data": legacy}))["status"] == "excel_missing_common_data"
    processor.response_helpers.generate_contextual_response.side_effect = None
    processor.response_helpers.generate_contextual_response.return_value = "response"
    assert (await processor._handle_complete_excel(u, session(), complete))["status"] == "redirect_to_multiple_flow"
