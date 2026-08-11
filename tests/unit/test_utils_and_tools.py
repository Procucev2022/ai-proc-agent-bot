from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from unittest.mock import AsyncMock

import pytest

from app.tools.confirmation_tool import ConfirmationTool
from app.tools.interaction_logger import InteractionLogger
from app.tools.retry_service import RetryService, get_retry_service
from app.tools.user_selection_tool import UserSelectionTool
from app.utils.bfs_bid_format_parser import (
    _build_item_key,
    _extract_bid_format_section,
    _normalize_bid_text_newlines,
    generate_bid_format,
    generate_bid_summary,
    parse_bid_format,
)
from app.utils.bfs_format_parser import (
    generate_bfs_display,
    generate_bfs_display_with_missing,
    is_bfs_complete,
    parse_bfs_format,
)
from app.utils.datetime_utils import *
from app.utils.excel_error_formatter import ExcelErrorFormatter, format_excel_error
from app.utils.message_restore_utils import restore_last_bot_message
from app.utils.pincode_distance import calculate_distance_between_pincodes
from app.utils.technical_failure_handler import handle_technical_failure


def test_datetime_helpers_cover_empty_and_weekend_paths():
    assert get_ordinal_suffix(11) == "th"
    assert get_ordinal_suffix(1) == "st"
    assert get_ordinal_suffix(22) == "nd"
    assert get_ordinal_suffix(23) == "rd"
    naive = datetime(2025, 11, 5, 12, 0)
    assert utc_from_naive(naive).tzinfo is not None
    assert utc_from_naive(None) is None
    assert format_utc_display(None) == "N/A"
    assert format_utc_time_only(naive) == "12:00 UTC"
    assert format_utc_short(naive) == "05/11 12:00 UTC"
    assert format_date_display(naive) == "5th Nov 2025"
    assert format_date_for_validation_error("2025-11-12") == "12th Nov 2025"
    assert format_date_for_validation_error("not-a-date") == "not-a-date"
    assert add_business_days(None, 2) is None
    assert add_business_days(naive, 0) == naive
    assert add_business_days(datetime(2025, 11, 7), 1).weekday() == 0
    assert is_expired(None, 1)[0] is True
    assert is_expired(datetime.now() - timedelta(hours=8), 1)[0] is True


def test_bfs_parser_and_display_paths():
    assert parse_bfs_format("Product 1: Laptop\n\nProduct 2: Monitor")["products"]
    assert parse_bfs_format("Product 1:")["error"]
    assert parse_bfs_format("")["error"]
    products = [{"description": "Laptop"}, {"description": ""}]
    assert "Product 1" in generate_bfs_display(products)
    assert generate_bfs_display([]) == "No products"
    display, missing = generate_bfs_display_with_missing(products)
    assert "Please provide" in display and missing == ["Product name"]
    assert is_bfs_complete([{"description": "Laptop"}])
    assert not is_bfs_complete(products)
    assert not is_bfs_complete([])


def test_bid_format_roundtrip_and_validation_errors():
    item = {"description": "Dell XPS", "specification": "i7", "sellPrice": 85000, "availableQuantity": 5, "ageOfAsset": "2yr", "location": "Pune"}
    assert _build_item_key(item) == "Dell XPS -- i7"
    assert _build_item_key({"description": "Dell"}) == "Dell"
    raw = "Place your bids:\nDell XPS -- i7: 85000\nfooter"
    section, remaining = _extract_bid_format_section(raw)
    assert "Dell XPS" in section and "footer" in remaining
    assert "Seller Price" in _normalize_bid_text_newlines("x Seller Price: 1 a. Your Price: 2 b. Your Qty: 1")
    generated = generate_bid_format([item])
    assert "Dell XPS -- i7" in generated
    valid = parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 80000").replace("b. Your Qty:", "b. Your Qty: 2"), [item])
    assert valid["bids"][0]["quantity"] == 2
    assert "No bid items" in parse_bid_format("", [item])["error"]
    assert "Missing price" in parse_bid_format(generated.replace("a. Your Price:", "a. Other:"), [item])["error"]
    missing_quantity = generated.replace("a. Your Price:", "a. Your Price: 1").replace("b. Your Qty:", "b. Other:")
    assert "Missing quantity" in parse_bid_format(missing_quantity, [item])["error"]
    assert "greater than 0" in parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 0").replace("b. Your Qty:", "b. Your Qty: 1"), [item])["error"]
    assert "exceeds available" in parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 1").replace("b. Your Qty:", "b. Your Qty: 9"), [item])["error"]
    assert "not found" in parse_bid_format(generated.replace("Dell XPS -- i7", "Other -- i7").replace("a. Your Price:", "a. Your Price: 1").replace("b. Your Qty:", "b. Your Qty: 1"), [item])["error"]
    assert "₹" in generate_bid_summary([{"key": "Dell", "price": 10, "quantity": 1, "original_item": item}])
    assert generate_bid_summary([{"key": "ignored", "price": 1}]) == ""


@pytest.mark.asyncio
async def test_retry_confirmation_selection_and_restore_helpers(tmp_path, monkeypatch):
    service = RetryService(max_retries=1, initial_delay=0)
    monkeypatch.setattr("app.tools.retry_service.asyncio.sleep", AsyncMock())
    result = await service.retry_with_backoff(AsyncMock(return_value={"success": True}))
    assert result["success"] and result["attempts"] == 1
    failed = await service.retry_with_backoff(AsyncMock(side_effect=RuntimeError("no")))
    assert failed["success"] is False
    assert get_retry_service().max_retries == 3

    ai = AsyncMock()
    ai.parse_confirmation_response.return_value = "yes"
    confirmation = ConfirmationTool(ai)
    assert await confirmation.parse_confirmation("YES") == "yes"
    assert await confirmation.parse_confirmation("no") == "no"
    assert await confirmation.parse_confirmation("maybe") == "yes"
    ai.parse_confirmation_response.return_value = "unclear"
    assert await confirmation.parse_confirmation("perhaps") is None
    ai.parse_confirmation_response.side_effect = RuntimeError("offline")
    assert await confirmation.parse_confirmation("ambiguous") is None

    selection = UserSelectionTool(AsyncMock())
    options = [{"number": 1, "profile": {"email": "a@example.com", "role": "buyer"}}, {"number": 2, "action": "Register new" , "display": "Register"}]
    assert (await selection.analyze_user_selection("1", options))["selected_option"] == 1
    assert (await selection.analyze_user_selection({"button_reply": {"title": "2"}}, options))["selected_option"] == 2
    assert selection._rule_based_analysis("register me as buyer", options)["register"]["type"] == "buyer"
    assert selection._fuzzy_email_match("a@ex", "a@example.com")["confidence"] > 0
    assert selection._string_similarity("abc", "abc") == 1
    assert selection._string_similarity("", "abc") == 0
    assert selection._rule_based_analysis("unknown", options)["requires_clarification"]

    class Session:
        workflow_state = {"last_bot_message_before_cancel": {"body": {"text": "Hi"}, "action": {"buttons": [{"reply": {"id": "x", "title": "Go"}}]}}}
    whatsapp = AsyncMock()
    session = Session()
    await restore_last_bot_message(session, whatsapp, "123", "fallback")
    whatsapp.send_configurable_buttons.assert_awaited_once()
    assert "last_bot_message_before_cancel" not in session.workflow_state


@pytest.mark.asyncio
async def test_interaction_logger_and_failure_handler(tmp_path, monkeypatch):
    logger = InteractionLogger(str(tmp_path))
    logger.log_intent_classification("hi", "greet", 0.9, "reason", "model", openai_input={"x": 1})
    logger.log_entity_extraction("buy", {"item": "x"}, 1, "rfq", "model")
    logger.log_error("intent", "x", "bad")
    logger.log_response_generation({}, "ok", "stage", "model")
    assert logger.log_file.read_text(encoding="utf-8").count("\n") == 4
    assert logger._json_serializer(datetime(2025, 1, 1)).startswith("2025")
    assert logger._json_serializer(EnumValue("x")) == "x"

    handler = AsyncMock()
    monkeypatch.setattr("app.utils.technical_failure_handler.get_global_error_handler", lambda: handler)
    await handle_technical_failure("123", "bad", "type")
    handler.handle_error.assert_awaited_once()


class EnumValue(Enum):
    VALUE = "x"


def test_excel_errors_and_distance():
    assert "3 rows" in ExcelErrorFormatter.format_error("row_limit", {"actual_rows": 3})
    assert "Unknown" in format_excel_error("unknown")
    assert "1.0MB" in format_excel_error("file_too_large", {"file_size_mb": 1})
    assert calculate_distance_between_pincodes("123", "456") is None
