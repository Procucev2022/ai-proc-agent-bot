"""Deep deterministic coverage for task, processor, tool, and utility branches."""

from __future__ import annotations

import asyncio
import json
import logging
import types
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app.services.processors import excel_message_processor as excel_processor
from app.services.processors import image_message_processor as image_processor
from app.services.processors import text_message_processor as text_processor
from app.tasks import bfs_notification_task as bfs_task
from app.tasks import category_name_sync_task as category_task
from app.tasks import daily_category_vector_rebuild_task as daily_task
from app.tasks import log_cleanup_task as cleanup_task
from app.tasks import seller_matching_task as seller_task
from app.tasks import task_utils
from app.tasks import taxonomy_build_task as taxonomy_task
from app.tasks import vector_store_sync_task as vector_task
from app.tasks import whatsapp_report_automation_task as report_task
from app.tools.confirmation_tool import ConfirmationTool
from app.tools.interaction_logger import InteractionLogger
from app.tools.user_selection_tool import UserSelectionTool
from app.utils import bfs_bid_format_parser as bid_parser
from app.utils import bfs_format_parser as bfs_parser
from app.utils import datetime_utils
from app.utils import excel_error_formatter
from app.utils import logging_utils
from app.utils import pincode_distance
from app.utils import pincode_lookup
from app.utils import sectioned_rfq_format_parser as rfq_parser


class ValueEnum(Enum):
    VALUE = "value"


def make_user(registered=True):
    return SimpleNamespace(phone_number="+919999", is_registered=registered)


def make_session(state=None, history=None):
    return SimpleNamespace(
        workflow_state=state if state is not None else {},
        conversation_history=history if history is not None else {},
        session_id="session-1",
    )


class FakeDB:
    def __init__(self, rows=(), rowcount=1):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return self

    def fetchall(self):
        return self.rows

    def add(self, entry):
        self.added = getattr(self, "added", []) + [entry]

    def add_all(self, entries):
        self.added_all = list(entries)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def test_datetime_utils_all_format_and_expiry_paths(monkeypatch):
    naive = datetime(2025, 11, 5, 12, 30)
    aware = datetime(2025, 11, 5, 18, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert [datetime_utils.get_ordinal_suffix(day) for day in (1, 2, 3, 4, 11, 12, 13, 21)] == ["st", "nd", "rd", "th", "th", "th", "th", "st"]
    assert datetime_utils.utc_from_naive(naive).tzinfo is datetime_utils.UTC
    assert datetime_utils.utc_from_naive(aware).hour == 12
    assert datetime_utils.utc_from_naive(None) is None
    assert datetime_utils.format_utc_display(None) == "N/A"
    assert datetime_utils.format_utc_display(naive).endswith("UTC")
    assert datetime_utils.format_utc_time_only(aware) == "18:00 UTC"
    assert datetime_utils.format_utc_short(naive) == "05/11 12:30 UTC"
    assert datetime_utils.format_date_display(naive) == "5th Nov 2025"
    assert datetime_utils.format_date_for_validation_error("") == "N/A"
    assert datetime_utils.format_date_for_validation_error("bad") == "bad"
    assert datetime_utils.add_business_days(None, 2) is None
    assert datetime_utils.add_business_days(naive, 0) == naive
    assert datetime_utils.add_business_days(datetime(2025, 11, 7), 1).date() == date(2025, 11, 10)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 11, 7)
    monkeypatch.setattr(datetime_utils, "datetime", FixedDatetime)
    assert datetime_utils.calculate_working_days_from_now(1) == "2025-11-10"
    monkeypatch.setattr(datetime_utils, "datetime", datetime)
    monkeypatch.setattr(datetime_utils, "utc_now", lambda: datetime(2025, 11, 5, tzinfo=datetime_utils.UTC))
    expired, last, threshold = datetime_utils.is_expired(datetime(2025, 11, 4), 1)
    assert expired and last.tzinfo is datetime_utils.UTC and threshold.day == 4
    assert datetime_utils.is_expired(None, 1)[0] is True


def test_logging_utils_context_formatter_helpers_and_decorators(monkeypatch):
    record = logging.LogRecord("source", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    logging_utils.clear_user_phone_context()
    assert "N/A | source | INFO | hello world" in logging_utils.CustomFormatter().format(record)
    logging_utils.set_user_phone_context("111")
    assert logging_utils.get_user_phone_context() == "111"
    record = logging.LogRecord("source", logging.INFO, __file__, 1, "context", (), None)
    assert "111 | source" in logging_utils.CustomFormatter().format(record)
    record = logging.LogRecord("source", logging.INFO, __file__, 1, "explicit", (), None)
    record.phone_number = "222"
    assert "222 | source" in logging_utils.CustomFormatter().format(record)
    with logging_utils.UserPhoneContext("333"):
        assert logging_utils.get_user_phone_context() == "333"
    assert logging_utils.get_user_phone_context() == "111"
    logging_utils.clear_user_phone_context()

    logger = Mock()
    logging_utils.log_info(logger, "info", user_id="u", phone_number="p", role="buyer")
    logging_utils.log_debug(logger, "debug", phone_number="p")
    logging_utils.log_error(logger, "error", error=RuntimeError("bad"), user_id="u")
    logging_utils.log_error(logger, "plain")
    assert logger.info.called and logger.debug.called and logger.error.call_count == 2

    database = __import__("app.database", fromlist=["get_db_session"])
    models = __import__("app.models", fromlist=["SystemLog"])
    db = FakeDB()
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(models, "SystemLog", lambda **kwargs: SimpleNamespace(**kwargs))
    logging_utils.log_to_database("INFO", "message", service="svc", context={"x": 1})
    assert db.commits == 1 and db.closed
    monkeypatch.setattr(database, "get_db_session", Mock(side_effect=RuntimeError("db")))
    logging_utils.log_to_database("ERROR", "message")

    @logging_utils.log_service_method("demo")
    def sync(value, **kwargs):
        return value

    @logging_utils.log_service_method("demo")
    async def async_fn(value, **kwargs):
        return value

    assert sync(3, user_id="u") == 3
    assert asyncio.run(async_fn(4, user_phone="p")) == 4
    with pytest.raises(ValueError):
        @logging_utils.log_service_method("demo")
        def bad():
            raise ValueError("x")
        bad()
    logging_utils.clear_user_phone_context()


def test_logging_utils_setup_basic_logging(monkeypatch, tmp_path):
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    fake_handler = Mock()
    monkeypatch.setattr(logging_utils, "ConcurrentTimedRotatingFileHandler", Mock(return_value=fake_handler))
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_BACKUP_COUNT", "2")
    logging_utils.setup_basic_logging("DEBUG")
    assert root.level == logging.DEBUG and root.handlers
    root.handlers.clear()
    root.handlers.extend(old_handlers)


def test_pincode_distance_success_cache_invalid_and_errors(monkeypatch):
    pincode_distance.clear_pincode_cache()
    pincode_distance._nomi = None
    assert pincode_distance.get_coordinates_from_pincode(None) is None
    assert pincode_distance.get_coordinates_from_pincode("123") is None
    assert pincode_distance.get_cache_size() == 1

    class Location:
        latitude = 18.5
        longitude = 73.8
        def isna(self):
            return SimpleNamespace(all=lambda: False)

    nomi = SimpleNamespace(query_postal_code=Mock(return_value=Location()))
    monkeypatch.setattr(pincode_distance.pgeocode, "Nominatim", Mock(return_value=nomi))
    assert pincode_distance.get_coordinates_from_pincode("411005") == (18.5, 73.8)
    assert pincode_distance.get_coordinates_from_pincode(" 411005 ") == (18.5, 73.8)
    assert pincode_distance.get_cache_size() == 2

    pincode_distance.clear_pincode_cache()
    nomi.query_postal_code.return_value = SimpleNamespace(latitude=float("nan"), longitude=73.8, isna=lambda: SimpleNamespace(all=lambda: False))
    assert pincode_distance.get_coordinates_from_pincode("411005") is None
    nomi.query_postal_code.side_effect = RuntimeError("lookup")
    assert pincode_distance.get_coordinates_from_pincode("411006") is None
    pincode_distance._nomi = None
    monkeypatch.setattr(pincode_distance.pgeocode, "Nominatim", Mock(side_effect=RuntimeError("init")))
    assert pincode_distance.get_coordinates_from_pincode("411007") is None

    pincode_distance.clear_pincode_cache()
    monkeypatch.setattr(pincode_distance, "get_coordinates_from_pincode", lambda pin: (1.0, 2.0) if pin == "a" else None)
    assert pincode_distance.calculate_distance_between_pincodes("a", "b") is None
    class Distance:
        kilometers = 12.5
    monkeypatch.setattr(pincode_distance, "geodesic", Mock(return_value=Distance()))
    assert pincode_distance.calculate_distance_between_pincodes("a", "a") == 12.5
    monkeypatch.setattr(pincode_distance, "geodesic", Mock(side_effect=RuntimeError("geo")))
    assert pincode_distance.calculate_distance_between_pincodes("a", "a") is None
    monkeypatch.setattr(pincode_distance, "geodesic", Mock(return_value=SimpleNamespace(kilometers=float("inf"))))
    assert pincode_distance.calculate_distance_between_pincodes("a", "a") is None
    monkeypatch.setattr(pincode_distance, "geodesic", Mock(return_value=Distance()))
    assert pincode_distance.calculate_distance_from_pincode_to_coords("b", 1, 2) is None
    assert pincode_distance.calculate_distance_from_pincode_to_coords("a", 1, 2) == 12.5
    monkeypatch.setattr(pincode_distance, "geodesic", Mock(side_effect=RuntimeError("geo")))
    assert pincode_distance.calculate_distance_from_pincode_to_coords("a", 1, 2) is None


def test_pincode_lookup_retry_timeout_and_async_paths(monkeypatch):
    response = Mock()
    response.json.return_value = [{"Status": "Success"}]
    calls = {"count": 0}
    def get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.exceptions.ConnectionError("offline")
        return response
    monkeypatch.setattr(pincode_lookup.requests, "get", get)
    monkeypatch.setattr(pincode_lookup.time, "sleep", lambda _: None)
    assert pincode_lookup.get_pincode_details("411005", max_retries=2)[0]["Status"] == "Success"
    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(side_effect=requests.exceptions.Timeout()))
    assert pincode_lookup.get_pincode_details("411005", max_retries=1) is None
    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(side_effect=requests.exceptions.RequestException("bad")))
    assert pincode_lookup.get_pincode_details("411005") is None
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("bad")) is None
    monkeypatch.setattr(pincode_lookup, "get_pincode_details", lambda _: [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "MH"}]}])
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005"))["city"] == "Pune"
    monkeypatch.setattr(pincode_lookup, "get_pincode_details", lambda _: [{"Status": "Error", "PostOffice": []}])
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005")) is None
    monkeypatch.setattr(pincode_lookup, "get_pincode_details", Mock(side_effect=RuntimeError("api")))
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005")) is None


def test_format_parsers_success_missing_and_display_limits():
    assert bfs_parser.parse_bfs_format("Product 1: Laptop\n\nProduct 2: Monitor")["products"]
    assert bfs_parser.parse_bfs_format("Product 1:")["error"]
    assert bfs_parser.parse_bfs_format("   ")["error"]
    assert bfs_parser.generate_bfs_display([]) == "No products"
    assert bfs_parser.generate_bfs_display_with_missing([{"description": ""}])[1] == ["Product name"]
    assert not bfs_parser.is_bfs_complete([])

    item = {"description": "Dell XPS", "specification": "i7", "sellPrice": 85000, "availableQuantity": 5, "ageOfAsset": "2yr", "location": "Pune"}
    generated = bid_parser.generate_bid_format([item])
    assert "Seller Price" in generated
    valid_text = generated.replace("a. Your Price:", "a. Your Price: 80000").replace("b. Your Qty:", "b. Your Qty: 2")
    assert bid_parser.parse_bid_format(valid_text, [item])["bids"][0]["quantity"] == 2
    assert bid_parser.parse_bid_format("", [item])["error"]
    assert "Missing price" in bid_parser.parse_bid_format(generated.replace("a. Your Price:", "a. Other:"), [item])["error"]
    assert "Missing quantity" in bid_parser.parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 1").replace("b. Your Qty:", "b. Other:"), [item])["error"]
    assert "greater than 0" in bid_parser.parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 0").replace("b. Your Qty:", "b. Your Qty: 1"), [item])["error"]
    assert "exceeds available" in bid_parser.parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 1").replace("b. Your Qty:", "b. Your Qty: 9"), [item])["error"]
    assert "not found" in bid_parser.parse_bid_format(generated.replace("Dell XPS -- i7", "Other -- i7").replace("a. Your Price:", "a. Your Price: 1").replace("b. Your Qty:", "b. Your Qty: 1"), [item])["error"]
    assert "Dell XPS" in bid_parser._extract_bid_format_section("Place your bids:\nDell XPS: 1\nfooter")[0]
    assert "Seller Price" in bid_parser._normalize_bid_text_newlines("x Seller Price: 1 a. Your Price: 2 b. Your Qty: 1")
    assert bid_parser.generate_bid_summary([{"key": "x", "price": 2}]) == ""

    delivery = "Intro\nDelivery Date: 12 Nov 2025\nDelivery Pincode: 411005\nDelivery City: Pune\nDelivery State: MH\nPlease confirm"
    parsed = rfq_parser.parse_delivery_format(delivery)
    assert parsed["pincode"] == "411005" and parsed["additional_text"] == "Please confirm"
    assert "Missing required" in rfq_parser.parse_delivery_format("Delivery City: Pune")["error"]
    assert "6-digit" in rfq_parser.parse_delivery_format("Date: tomorrow\nPincode: 123")["error"]
    items = rfq_parser.parse_items_format("RFQ Items (1):\nItem 1: Bottle\nQty: 2\nBrand: Acme\nSpecification: UoM: each, blue\nPlease confirm")
    assert items["items"][0]["remarks"] == "blue" and items["additional_text"] == "Please confirm"
    assert "missing required" in rfq_parser.parse_items_format("Item 1: Bottle")["error"]
    assert rfq_parser.generate_delivery_display({"deliveryDate": "today", "pincode": "411005"}).startswith("Delivery Date")
    display, missing = rfq_parser.generate_delivery_display_with_missing({})
    assert "Please provide" in display and len(missing) == 2
    assert rfq_parser.generate_items_display([]) == "No items"
    assert "+1 more item" in rfq_parser.generate_items_display([{"description": "x", "quantity": 1}] * 6)
    missing_display, labels = rfq_parser.generate_items_display_with_missing([{"description": "", "quantity": None}], [{"index": 1, "missing_fields": ["description", "quantity"]}])
    assert "Please provide" in missing_display and labels == ["Product Name", "Quantity"]
    assert rfq_parser.generate_delivery_display_with_invalid_pincode({"deliveryDate": "today"}).endswith("[Valid 6-digit pin-code]")
    assert not rfq_parser.validate_delivery_completeness({})
    assert rfq_parser.validate_delivery_completeness({"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    assert not rfq_parser.validate_items_completeness([{"description": "", "quantity": 1}])
    assert rfq_parser.validate_items_completeness([{"description": "x", "quantity": 1}])


def test_excel_error_formatter_known_fallback_and_convenience():
    details = {
        "actual_rows": 3, "max_rows": 2, "example_values": "x", "file_size_mb": 1,
        "worksheet_count": 2, "column_name": "Qty", "format": "csv", "skipped_rows": 1,
        "total_rows": 2, "extracted_rows": 1, "example_header": "A/B"
    }
    for error_type in ["row_limit", "invalid_quantity", "merged_cells", "empty_file", "multiple_worksheets", "file_too_large", "password_protected", "invalid_format", "corrupted_file", "no_headers", "mixed_data_types", "unsupported_format", "download_failed", "validation_error", "date_validation", "pincode_validation", "processing_incomplete", "date_location_inconsistency", "special_characters_in_headers"]:
        assert excel_error_formatter.ExcelErrorFormatter.format_error(error_type, details).startswith("File Processing Failed:")
    assert "Unknown" in excel_error_formatter.format_excel_error("unknown")
    assert "fallback" in excel_error_formatter.ExcelErrorFormatter.format_error("merged_cells", {"message": object()}).lower() or "File Processing Failed" in excel_error_formatter.ExcelErrorFormatter.format_error("merged_cells", {"message": object()})


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_tool_exact_partial_ai_and_error():
    service = SimpleNamespace(parse_confirmation_response=AsyncMock(return_value="yes"))
    tool = ConfirmationTool(service)
    assert await tool.parse_confirmation(" YES ") == "yes"
    assert await tool.parse_confirmation("please confirm this") == "yes"
    assert await tool.parse_confirmation("no") == "no"
    assert await tool.parse_confirmation("maybe") == "yes"
    service.parse_confirmation_response.return_value = "unclear"
    assert await tool.parse_confirmation("perhaps") is None
    service.parse_confirmation_response.side_effect = RuntimeError("offline")
    assert await tool.parse_confirmation("ambiguous") is None


@pytest.mark.asyncio
async def test_user_selection_rule_fuzzy_and_ai_paths(monkeypatch, tmp_path):
    service = SimpleNamespace(
        default_model="model",
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()))
    )
    tool = UserSelectionTool(service)
    options = [
        {"number": 1, "profile": {"email": "buyer@example.com", "role": "buyer"}},
        {"number": 2, "action": "Register new", "display": "Register"},
    ]
    assert (await tool.analyze_user_selection({"button_reply": {"title": "1"}}, options))["selected_option"] == 1
    assert tool._rule_based_analysis("register me as buyer", options)["register"]["type"] == "buyer"
    assert tool._rule_based_analysis("register me as seller", options)["register"]["type"] == "seller"
    assert tool._rule_based_analysis("buyer@example.com", options)["selected_option"] == 1
    assert tool._rule_based_analysis("first", options)["selected_option"] == 1
    assert tool._rule_based_analysis("unknown", options)["requires_clarification"]
    assert tool._fuzzy_email_match("buyer@exampl.com", "buyer@example.com")["confidence"] > 0
    assert tool._fuzzy_email_match("x", "buyer@example.com")["confidence"] == 0
    assert tool._string_similarity("abc", "abc") == 1
    assert tool._string_similarity("", "abc") == 0

    # Redirect both directories into tmp_path first. Writing the stub prompt and tool
    # straight into app/prompts and app/tools overwrote the shipped files, which is how
    # user_selection_analysis.json ended up empty and returned HTTP 400 in production.
    tool.prompts_dir = tmp_path / "prompts"
    tool.tools_dir = tmp_path / "tools"
    prompt_dir = tool.prompts_dir / "profile_selection"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    tool.tools_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "user_selection_analysis.txt").write_text("system", encoding="utf-8")
    (tool.tools_dir / "user_selection_analysis.json").write_text("{}", encoding="utf-8")
    service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="function_call", arguments=json.dumps({"selected_option": 2, "confidence": 0.9, "reasoning": "AI"}))])
    result = await tool._ai_based_analysis("something unclear", options)
    assert result["selected_option"] == 2 and result["reasoning"].startswith("AI analysis")
    service.client.responses.create.return_value = SimpleNamespace(output=[])
    assert (await tool._ai_based_analysis("unclear", options))["confidence"] == 0.2
    service.client.responses.create.side_effect = RuntimeError("ai")
    assert (await tool._ai_based_analysis("unclear", options))["confidence"] == 0.1
    service.client.responses.create.side_effect = None
    service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="function_call", arguments=json.dumps({"selected_option": 2, "confidence": 0.9, "reasoning": "AI"}))])
    assert (await tool.analyze_user_selection("unclear", options))["selected_option"] == 2
    monkeypatch.setattr(tool, "_rule_based_analysis", Mock(side_effect=RuntimeError("rule")))
    assert (await tool.analyze_user_selection("x", options))["requires_clarification"]


def test_interaction_logger_serializers_global_and_write_error(tmp_path, monkeypatch):
    logger = InteractionLogger(str(tmp_path))
    logger.log_intent_classification("hi", "greet", .9, "reason", "model", all_scores={"greet": .9}, phone_number="1", openai_input={"x": 1})
    logger.log_entity_extraction("buy", {"item": "x"}, 1, "rfq", "model", missing_fields=["date"])
    logger.log_error("intent", "x", "bad")
    logger.log_response_generation({}, "ok", "stage", "model")
    assert logger.log_file.read_text(encoding="utf-8").count("\n") == 4
    assert logger._json_serializer(datetime(2025, 1, 1)).startswith("2025")
    assert logger._json_serializer(ValueEnum.VALUE) == "value"
    model = SimpleNamespace(model_dump=lambda: {"x": 1})
    assert logger._json_serializer(model) == {"x": 1}
    broken = SimpleNamespace(model_dump=lambda: (_ for _ in ()).throw(RuntimeError("bad")), dict=lambda: {"ok": 1})
    assert logger._json_serializer(broken) == {"ok": 1}
    monkeypatch.setattr("builtins.open", Mock(side_effect=OSError("disk")))
    logger._write_log_entry({"interaction_type": "error"})


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_processor_routes_all_intents_and_handlers(monkeypatch):
    intent = Mock(return_value={"intent": "buy_something", "confidence": .9})
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    response = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"), generate_clarification_response=AsyncMock(return_value="clarify"))
    confirmation = SimpleNamespace(handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}), handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}))
    rfq = SimpleNamespace(process_rfq_status_request=AsyncMock(return_value={"status": "ok", "response_message": "status", "rfq_ids": [1], "rfq_statuses": ["open"]}))
    processor = text_processor.TextMessageProcessor(Mock(), Mock(), whatsapp, response, confirmation, Mock(), rfq, Mock())
    user = make_user()
    assert (await processor.process_text_message(make_user(False), make_session(), "x"))["status"] == "registration_needed"
    session = make_session()
    processor.intent_service.classify_intent = intent
    assert (await processor.process_text_message(user, session, "buy"))["status"] == "delegate_to_chat_service"
    intent.return_value = {"intent": "modification_request", "confidence": .8}
    assert (await processor.process_text_message(user, make_session(), "modify"))["status"] == "delegate_to_chat_service"
    intent.return_value = {"intent": "x", "confidence": .1}
    assert (await processor.process_text_message(user, make_session(), "unclear"))["status"] == "clarification_sent"
    intent.return_value = {"intent": "rfq_status_check", "confidence": .9}
    result = await processor.process_text_message(user, make_session(), "status")
    assert result["status"] == "ok"
    intent.return_value = {"intent": "general_inquiry", "confidence": .5}
    assert (await processor.process_text_message(user, make_session(), "hello"))["status"] == "general_inquiry_handled"
    intent.return_value = {"intent": "other", "confidence": .8}
    assert (await processor.process_text_message(user, make_session(), "other"))["status"] == "fallback_handled"
    intent.return_value = {"intent": "confirmation_response", "confidence": .8}
    assert (await processor.process_text_message(user, make_session(), "yes"))["status"] == "delegate_to_chat_service"
    state = {"awaiting_attachment_decision": True}
    assert (await processor.process_text_message(user, make_session(state), "attach"))["method"] == "_handle_attachment_decision"
    state = {"pending_optional_rfq": True}
    assert (await processor.process_text_message(user, make_session(state), "optional"))["status"] == "optional"
    state = {"pending_rfq": True}
    assert (await processor.process_text_message(user, make_session(state), "confirm"))["status"] == "pending"
    state = {"extracted_entities": [{"description": "x"}]}
    assert (await processor.process_text_message(user, make_session(state), "continue"))["status"] == "delegate_to_chat_service"

    whatsapp.send_message.side_effect = RuntimeError("send")
    assert (await processor._handle_general_inquiry(user, "x"))["status"] == "error"
    assert (await processor._handle_rfq_status_inquiry(user, "x"))["status"] == "error"
    assert (await processor._handle_fallback(user, "x"))["status"] == "error"
    whatsapp.send_message.side_effect = None
    session = make_session({"clarification_retry_count": 2, "last_activity_at": "t", "other": 1})
    assert (await processor._handle_clarification_request(user, "x", session))["status"] == "clarification_limit_reached_exit"
    assert session.workflow_state == {"last_activity_at": "t"}


@pytest.mark.asyncio
async def test_image_processor_attachment_formats_errors_and_confirmation(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    response = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"), generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"))
    processor = image_processor.ImageMessageProcessor(whatsapp, response)
    user = make_user()
    assert (await processor.process_image_message(make_user(False), make_session(), {}))["response"] == "registration_required"
    history = {"messages": [{"role": "assistant", "content": {"body": {"text": "body"}, "header": {"text": "head"}, "footer": {"text": "foot"}, "action": {"buttons": [{"reply": {"id": "a", "title": "A"}}]}}}]}
    assert (await processor.process_image_message(user, make_session(history=history), {}))["response"] == "attachment_ignored"
    assert whatsapp.send_configurable_buttons.await_count == 1
    assert (await processor.process_image_message(user, make_session(history={"messages": [{"role": "assistant", "content": "last"}]}), {}))["response"] == "attachment_ignored"
    assert (await processor.process_image_message(user, make_session(), {}))["response"] == "attachment_ignored"
    assert (await processor.process_image_message(user, make_session({"pending_rfq": True}), {}))["response"] == "no_file_data"

    monkeypatch.setattr(image_processor.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": False, "error": "download"}))
    assert (await processor.process_image_message(user, make_session({"pending_rfq": True}), {"image": {"link": "url"}}))["response"] == "download"
    monkeypatch.setattr(image_processor.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": True, "attachment": {"file_name": "x.jpg"}}))
    monkeypatch.setattr(image_processor.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": True, "count": 1, "max": 4}))
    monkeypatch.setattr(image_processor.AttachmentHelpers, "approve_pending_attachment", Mock())
    session = make_session({"incomplete_products": True})
    result = await processor.process_image_message(user, session, {"data": "base64", "filename": "x.jpg", "mime_type": "image/jpeg", "caption": "remark"})
    assert result["response"] == "attachment_added_continue_clarification" and session.workflow_state["attachment_caption"] == "remark"
    confirmation_module = __import__("app.services.handlers.confirmation_handler", fromlist=["ConfirmationHandler"])
    monkeypatch.setattr(confirmation_module.ConfirmationHandler, "_proceed_to_confirmation_from_optional", AsyncMock())
    chat_helpers = __import__("app.services.helpers.chat_service_helpers", fromlist=["ChatServiceHelpers"])
    session = make_session({"pending_optional_rfq": True})
    assert (await processor.process_image_message(user, session, {"data": "x"}))["response"] == "attachment_added_proceeded_to_confirmation"
    session = make_session({"pending_rfq": {"entities": {"description": "x"}}, "extracted_entities": [{"attachments": [{"file_name": "x"}]}], "attachment_caption": "r"})
    monkeypatch.setattr(chat_helpers.ChatServiceHelpers, "create_rfq_schema_from_entities", Mock(return_value="schema"))
    assert (await processor.process_image_message(user, session, {"data": "x"}))["response"] == "confirmation_regenerated"

    monkeypatch.setattr(image_processor.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": False, "error": "Maximum attachments reached"}))
    session = make_session({"pending_rfq": {"entities": {"description": "x"}}})
    monkeypatch.setattr(chat_helpers.ChatServiceHelpers, "create_rfq_schema_from_entities", Mock(return_value="schema"))
    assert (await processor.process_image_message(user, session, {"data": "x"}))['response'] == "confirmation_with_error"
    monkeypatch.setattr(image_processor.AttachmentHelpers, "add_attachment_to_session", Mock(side_effect=RuntimeError("processing")))
    assert (await processor.process_image_message(user, make_session({"pending_rfq": True}), {"data": "x"}))["response"] == "processing"
    assert processor._get_last_bot_message(make_session(history={"messages": [{"role": "user", "content": "u"}]})) is None
    assert await processor._handle_save_error(user) == {"status": "error", "response": "failed_to_save_attachment"}
    assert await processor._regenerate_confirmation_with_error(user, make_session(), "bad") == {"status": "error", "response": "no_pending_confirmation_found"}


@pytest.mark.asyncio
async def test_excel_processor_lock_validation_and_conversion_branches(monkeypatch):
    class Lock:
        def __init__(self, acquired=True): self.acquired = acquired; self.released = 0
        async def acquire(self, blocking=False): return self.acquired
        async def release(self): self.released += 1
    class Redis:
        def __init__(self, lock): self._lock = lock
        def lock(self, *_args, **_kwargs): return self._lock
    lock = Lock()
    monkeypatch.setattr(excel_processor.Redis, "from_url", Mock(return_value=Redis(lock)))
    monkeypatch.setattr(excel_processor, "get_settings", lambda: SimpleNamespace(redis_url="redis://test"))
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    response = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"))
    processor = excel_processor.ExcelMessageProcessor(whatsapp, response, Mock())
    user = make_user()
    assert (await processor.process_excel_upload(make_user(False), make_session(), {}))["response"] == "registration_required"
    session = make_session({"pending_optional_rfq": True})
    delegated = AsyncMock(return_value={"status": "delegated"})
    monkeypatch.setattr(excel_processor.ImageMessageProcessor, "process_image_message", delegated)
    assert (await processor.process_excel_upload(user, session, {"data": "x"}))["status"] == "delegated"
    assert (await processor.process_excel_upload(user, make_session({"excel_file_processed": True, "excel_filename": "old.xlsx"}), {}))["response"] == "excel_already_processed"
    assert (await processor.process_excel_upload(user, make_session(), {"document": {}}))["response"] == "file_access_error"

    lock.acquired = False
    assert (await processor.process_excel_upload(user, make_session(), {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "upload_in_progress"
    lock.acquired = True
    validation = SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "invalid"}))
    monkeypatch.setattr(excel_processor, "ExcelValidationService", lambda: validation)
    assert (await processor.process_excel_upload(user, make_session(), {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "validation_failed"
    assert lock.released > 0
    validation.validate_excel_file_from_url.return_value = {"valid": True, "content": b"x"}
    processing = SimpleNamespace(process_excel_file=AsyncMock(return_value={"success": False, "error": "bad"}))
    monkeypatch.setattr(excel_processor, "ExcelProcessingService", lambda _: processing)
    assert (await processor.process_excel_upload(user, make_session(), {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "processing_failed"
    processing.process_excel_file.return_value = {"success": True, "items": [{"Quantity": ""}] * 4}
    monkeypatch.setattr(excel_processor, "format_rfq_response_message", lambda **_: "missing") if hasattr(excel_processor, "format_rfq_response_message") else None
    assert (await processor.process_excel_upload(user, make_session(), {"document": {"link": "url", "filename": "x.xlsx"}}))["response"] == "missing_quantities_reupload_required"
    entities = processor._convert_excel_to_entities([{"ItemDescription": "x", "Quantity": "2.0", "Uom": "kg", "Specification": "brand"}, {"Quantity": "bad"}])
    assert entities[0]["quantity"] == 2 and entities[1]["quantity"] is None
    assert processor._identify_missing_common_fields([{}]) == ["delivery_date", "location"]
    assert processor._identify_missing_common_fields([{"DeliveryDate": "today", "City": "Pune"}]) == []


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def test_task_utils_database_and_error_branches(monkeypatch):
    assert task_utils.normalize_phone_for_comparison(None) == ""
    assert task_utils.normalize_phone_for_comparison("++123") == "123"
    assert task_utils.get_users_active_in_last_24hrs([]) == set()
    db = FakeDB(rows=[("123",), (None,), ("456",)])
    monkeypatch.setattr(task_utils, "get_db_session", lambda: db)
    assert task_utils.get_users_active_in_last_24hrs(["+123", "456"]) == {"123", "456"}
    assert db.closed
    monkeypatch.setattr(task_utils, "get_db_session", Mock(side_effect=RuntimeError("db")))
    assert task_utils.get_users_active_in_last_24hrs(["+1"]) == set()
    assert not task_utils.is_user_active_in_last_24hrs("")
    monkeypatch.setattr(task_utils, "get_users_active_in_last_24hrs", lambda values: {"123"})
    assert task_utils.is_user_active_in_last_24hrs("+123")


def test_bfs_notification_transactions_and_empty_paths(monkeypatch):
    result = SimpleNamespace(keys=lambda: ["a"], fetchall=lambda: [(1,)], rowcount=2)
    db = FakeDB()
    db.execute = Mock(return_value=result)
    monkeypatch.setattr(bfs_task, "get_remote_db_session", lambda: db)
    assert bfs_task.get_pending_bfs_notifications(2) == [{"a": 1}]
    assert bfs_task.mark_notification_sent("u")
    assert bfs_task.mark_notifications_sent_batch([]) == 0
    assert bfs_task.mark_notifications_sent_batch(["u1", "u2"]) == 2
    failing = FakeDB()
    failing.execute = Mock(side_effect=RuntimeError("db"))
    monkeypatch.setattr(bfs_task, "get_remote_db_session", lambda: failing)
    assert not bfs_task.mark_notification_sent("u")
    assert bfs_task.mark_notifications_sent_batch(["u"]) == 0
    monkeypatch.setattr(bfs_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs_task, "get_pending_bfs_notifications", lambda limit: [{"seller_phone": None}])
    assert asyncio.run(bfs_task.process_bfs_notifications())["processed"] == 0


def test_category_daily_and_vector_task_helpers(monkeypatch):
    database = __import__("app.database", fromlist=["get_db_session"])
    models = __import__("app.models", fromlist=["CategoryMapping"])
    monkeypatch.setattr(category_task, "get_settings", lambda: SimpleNamespace(chroma_host="h", chroma_port=1))
    collection = SimpleNamespace(add=Mock(), count=Mock(return_value=2))
    client = SimpleNamespace(heartbeat=Mock(), get_or_create_collection=Mock(return_value=collection), delete_collection=Mock(side_effect=RuntimeError("missing")))
    monkeypatch.setattr(category_task.chromadb, "HttpClient", lambda **_: client)
    monkeypatch.setattr(category_task.embedding_functions, "SentenceTransformerEmbeddingFunction", lambda **_: "embed")
    assert category_task._create_category_embeddings({"A": 1, "B": 2}, True)["success"]
    monkeypatch.setattr(category_task.chromadb, "HttpClient", Mock(side_effect=RuntimeError("offline")))
    assert not category_task._create_category_embeddings({"A": 1})["success"]

    monkeypatch.setattr(daily_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(database, "test_remote_connection", lambda: True)
    monkeypatch.setattr(database, "get_remote_item_categories", lambda: [{"category": "A", "item": "X"}, {"category": "", "item": "Y"}])
    class Mapping:
        category = "category"
        item = "item"
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    monkeypatch.setattr(models, "CategoryMapping", Mapping)
    db = FakeDB()
    db.query = lambda *_: SimpleNamespace(all=lambda: [])
    db.add_all = Mock()
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    result = daily_task._sync_category_mappings()
    assert result["inserted_count"] == 1 and db.commits == 1
    monkeypatch.setattr(daily_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert not daily_task._sync_category_mappings()["success"]

    monkeypatch.setattr(vector_task, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=True))
    monkeypatch.setattr(vector_task, "_run_seller_category_mapping", lambda: {"success": True, "processed_count": 1})
    monkeypatch.setattr(vector_task, "_run_vector_embedding_creation", lambda **_: {"success": True, "total_items": 1})
    assert vector_task.sync_vector_store.run()["status"] == "completed"
    monkeypatch.setattr(vector_task, "_run_seller_category_mapping", lambda: {"success": False, "error": "map"})
    assert vector_task.sync_vector_store.run()["status"] == "partial_failure"
    monkeypatch.setattr(vector_task, "_run_seller_category_mapping", Mock(side_effect=RuntimeError("map")))
    assert vector_task.sync_vector_store.run()["status"] == "failed"


def test_seller_task_helpers_and_remote_log(monkeypatch):
    monkeypatch.setattr(seller_task, "execute_remote_query", lambda *_: [{"vendor_uuid": "s"}])
    assert seller_task.get_sellers_already_notified_for_rfq("r") == {"s"}
    assert seller_task.extract_delivery_location({"delivery_city": "Pune", "delivery_state": "MH", "delivery_pincode": "411005"}) == {"city": "Pune", "state": "MH", "pincode": "411005"}
    assert seller_task._build_item_description_for_rfq({"categories": ["A"], "description": " x ", "special_instruction": " y "}) == "Categories: A | Description: x | Requirements: y"
    assert seller_task._build_item_description_for_rfq({}) == ""
    assert seller_task.log_selected_sellers_to_remote("r", "id", [])
    db = FakeDB()
    monkeypatch.setattr(seller_task, "get_remote_db_session", lambda: db)
    assert seller_task.log_selected_sellers_to_remote("r", "id", [{"seller_id": "s"}])
    assert db.commits == 1
    failing = FakeDB()
    failing.execute = Mock(side_effect=RuntimeError("insert"))
    monkeypatch.setattr(seller_task, "get_remote_db_session", lambda: failing)
    assert not seller_task.log_selected_sellers_to_remote("r", "id", [{"seller_id": "s"}])


@pytest.mark.asyncio
async def test_taxonomy_single_batch_and_parallel_retry(monkeypatch):
    class Redis:
        async def get(self, key): return None
        async def set(self, key, value): pass
        async def delete(self, key): pass
    monkeypatch.setattr(taxonomy_task.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=Redis()))
    monkeypatch.setattr(taxonomy_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1))
    monkeypatch.setattr(taxonomy_task, "get_remote_item_categories", lambda: [{"x": 1}, {"x": 2}])
    process = AsyncMock(side_effect=[RuntimeError("retry"), {"success": True, "processed_count": 2, "created_categories": 1, "existing_categories": 1}])
    module = types.SimpleNamespace(process_category_mappings=process)
    spec = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda target: target.__dict__.update(module.__dict__)))
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *_: spec)
    monkeypatch.setattr("importlib.util.module_from_spec", lambda _: types.SimpleNamespace())
    monkeypatch.setattr(taxonomy_task.asyncio, "sleep", AsyncMock())
    result = await taxonomy_task.build_taxonomy_async(None, batch_size=2, process_all=True, parallel=True, resume=False)
    assert result["success"] and result["checkpoint_cleared"]
    process.side_effect = None
    process.return_value = {"success": False, "error": "bad"}
    result = await taxonomy_task.build_taxonomy_async(None, batch_size=2, process_all=False, parallel=False, resume=False)
    assert result["success"] is False


def test_cleanup_manager_and_report_failure_branches(monkeypatch, tmp_path):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return datetime(2025, 1, 10)
    monkeypatch.setattr(cleanup_task, "datetime", FixedDateTime)
    missing = cleanup_task.LogCleanupManager(str(tmp_path / "missing"), 7, 7)
    assert missing.run()["errors"] == 1
    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "app_2024-01-01.log"
    old.write_text("old", encoding="utf-8")
    manager = cleanup_task.LogCleanupManager(str(logs), 7, 7)
    assert manager.run()["logs_archived"] == 1 and not old.exists()
    archive = manager.archive_dir / "logs_2020-01-01.tar.gz"
    archive.write_bytes(b"x")
    assert archive in manager._find_old_archives()

    monkeypatch.setattr(report_task, "get_settings", lambda: SimpleNamespace())
    result = asyncio.run(report_task.run_whatsapp_report_automation_async(None, "bad"))
    assert result["status"] == "failed"
    monkeypatch.setattr(report_task, "get_settings", Mock(side_effect=RuntimeError("outer")))
    with pytest.raises(RuntimeError):
        asyncio.run(report_task.run_whatsapp_report_automation_async(None, "2024-01-01"))
