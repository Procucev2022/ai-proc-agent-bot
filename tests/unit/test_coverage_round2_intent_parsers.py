"""Deterministic round-two coverage for intent and structured format paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.handlers.format_modification_handler as format_mod
import app.services.intent_service as intent_mod
import app.services.cancel_service as cancel_mod
import app.utils.bfs_bid_format_parser as bid_parser
import app.utils.bfs_format_parser as bfs_parser
import app.utils.sectioned_rfq_format_parser as sectioned_parser
from app.models import WorkflowType
from app.services.workflow_manager import WorkflowManager
from app.services.handlers.format_modification_handler import FormatModificationHandler


# IntentService -------------------------------------------------------------


def make_intent_service(monkeypatch, *, ai=None):
    """Construct IntentService without creating an OpenAI client or settings IO."""
    ai = ai or MagicMock()
    ai.classify_intent = AsyncMock()
    monkeypatch.setattr(intent_mod, "OpenAIService", lambda: ai)
    monkeypatch.setattr(
        intent_mod,
        "get_settings",
        lambda: SimpleNamespace(support_contact_info="support@example.com"),
    )
    return intent_mod.IntentService(), ai


@pytest.mark.asyncio
async def test_intent_missing_timeout_failure_exception_and_malformed_results(monkeypatch):
    service, ai = make_intent_service(monkeypatch)
    context = {"conversation_history": {"messages": []}}

    # The None-result branch awaits the fallback and forwards user_phone so the
    # user actually receives the outage notice.
    ai.classify_intent.return_value = None
    fallback = AsyncMock(return_value={"intent": "ambiguous", "success": False})
    service._get_fallback_classification = fallback
    result = await service.classify_intent("unmatched", context, user_phone="+1")
    assert result == {"intent": "ambiguous", "success": False}
    fallback.assert_awaited_once_with(
        "unmatched", context, error="OpenAI service returned None", user_phone="+1"
    )

    timeout_result = {
        "intent": "system_timeout",
        "confidence": 0,
        "success": False,
        "timeout_handled": True,
    }
    ai.classify_intent.return_value = timeout_result
    assert await service.classify_intent("please wait", context) is timeout_result

    fallback_async = AsyncMock(return_value={"intent": "fallback", "success": False})
    service._get_fallback_classification = fallback_async
    ai.classify_intent.return_value = {"success": False}
    failed = await service.classify_intent("failed", context, user_phone="+9199")
    assert failed["intent"] == "fallback"
    # user_phone must arrive by keyword: passing it positionally landed it in the
    # `error` parameter, leaving user_phone None so no notice was ever sent.
    fallback_async.assert_awaited_once_with(
        "failed", context, error="OpenAI classification unsuccessful", user_phone="+9199"
    )

    fallback_async.reset_mock()
    ai.classify_intent.side_effect = RuntimeError("OpenAI unavailable")
    errored = await service.classify_intent("exception", context, user_phone="+9199")
    assert errored["intent"] == "fallback"
    fallback_async.assert_awaited_once()
    assert fallback_async.await_args.kwargs["error"] == "OpenAI unavailable"
    assert fallback_async.await_args.kwargs["user_phone"] == "+9199"

    for malformed in (
        {"success": True, "confidence": 90},
        {
            "success": True,
            "intent": "general_inquiry",
            "confidence": 90,
            "context_analysis": None,
        },
    ):
        fallback_async.reset_mock()
        ai.classify_intent.side_effect = None
        ai.classify_intent.return_value = malformed
        result = await service.classify_intent("malformed", context)
        assert result["intent"] == "fallback"
        fallback_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_intent_contextual_variants_defaults_and_context_errors(monkeypatch):
    service, ai = make_intent_service(monkeypatch)
    ai.classify_intent = AsyncMock()
    ai.handle_contextual_interaction = AsyncMock(return_value={})
    context = {
        "conversation_history": {"messages": ["old"]},
        "workflow_state": {"stage": "collecting"},
        "extracted_entities": [{"description": "pump"}],
    }

    for intent in ("contextual_reference", "session_inquiry", "alternative_request"):
        classification = {
            "success": True,
            "intent": intent,
            "confidence": 61,
            "context_analysis": {},
        }
        ai.classify_intent.return_value = classification
        result = await service.classify_intent("that item", context)
        assert result["contextual_response"] == "How can I help you?"
        assert result["contextual_actions"] == []
        assert result["context_understanding"] == {}
        assert result["should_handle_directly"] is True
        assert result["should_update_entities"] is False
        assert result["should_change_workflow"] is False

    ai.handle_contextual_interaction.assert_awaited()
    ai.handle_contextual_interaction.assert_awaited_with(
        message="that item",
        conversation_history=context["conversation_history"],
        workflow_state=context["workflow_state"],
        extracted_entities=context["extracted_entities"],
    )

    # Confidence at the boundary does not enter contextual handling.
    ordinary = {
        "success": True,
        "intent": "contextual_reference",
        "confidence": 60,
        "context_analysis": {},
    }
    ai.classify_intent.return_value = ordinary
    assert await service.classify_intent("not enough confidence", {}) is ordinary

    # A contextual-service failure is converted to the local clarification
    # response rather than escaping to the outer fallback.
    ai.classify_intent.return_value = {
        "success": True,
        "intent": "session_inquiry",
        "confidence": 90,
        "context_analysis": {},
    }
    ai.handle_contextual_interaction.side_effect = RuntimeError("context service")
    result = await service.classify_intent("what did I ask", {})
    assert result["contextual_response"].startswith("I understand")
    assert result["contextual_actions"] == []
    assert result["context_understanding"]["confidence"] == 30
    assert result["should_handle_directly"] is True

    # A non-string message skips local shortcuts and still returns a valid
    # successful classification.
    ai.handle_contextual_interaction.side_effect = None
    non_string = {"success": True, "intent": "general_inquiry", "confidence": 70}
    ai.classify_intent.return_value = non_string
    assert await service.classify_intent({"text": "hello"}, None) is non_string


@pytest.mark.asyncio
async def test_intent_real_fallback_cancel_boundary_and_missing_context(monkeypatch):
    service, _ = make_intent_service(monkeypatch)
    cancel_service = SimpleNamespace(_send_cancellation_message=AsyncMock(return_value=True))
    monkeypatch.setattr(cancel_mod, "CancelService", lambda: cancel_service)

    result = await service._get_fallback_classification(
        "technical failure",
        {"user_role": "buyer"},
        error="down",
        user_phone="+9199",
    )
    # Returns a usable classification rather than None: callers dereference this
    # as a dict, and returning None surfaced as an unrelated AttributeError.
    assert result["success"] is False
    assert result["fallback_used"] is True
    assert "down" in result["reasoning"]

    # Classifying must not message the user. Sending here gave the user a
    # "technical issues" notice followed by the caller's real reply: two
    # outbound messages for one inbound message.
    cancel_service._send_cancellation_message.assert_not_awaited()

    assert (await service._get_fallback_classification("failure", {}))["success"] is False
    cancel_service._send_cancellation_message.assert_not_awaited()

    # A missing context is tolerated rather than raising inside the error path.
    assert (await service._get_fallback_classification("failure", None))["intent"] == "ambiguous"

    # The rule-based classifier drives the intent, so a real request no longer
    # degrades to a zero-confidence greeting.
    buying = await service._get_fallback_classification("I need to buy 10 laptops", {})
    assert buying["intent"] == "buy_something" and buying["confidence"] == 60

    # Non-string message shapes reach this path from multimodal payloads.
    assert (await service._get_fallback_classification({"text": "please cancel"}, {}))["intent"] == "cancel_workflow"
    assert (await service._get_fallback_classification([{"text": "bye"}], {}))["intent"] == "exit_system"


@pytest.mark.asyncio
async def test_intent_local_interruption_and_modification_priority(monkeypatch):
    service, ai = make_intent_service(monkeypatch)
    ai.classify_intent = AsyncMock(return_value={"success": True, "intent": "ai"})
    session = SimpleNamespace(workflow_state={})

    monkeypatch.setattr(WorkflowManager, "get_workflow_type", lambda _: WorkflowType.registration)
    assert service.detect_interruption_intent("hello", session) == {
        "is_interruption": False,
        "interruption_type": None,
    }

    monkeypatch.setattr(WorkflowManager, "get_workflow_type", lambda _: WorkflowType.rfq_creation)
    monkeypatch.setattr(WorkflowManager, "get_delivery_details", lambda _: None)
    assert not service.detect_interruption_intent("hello", session)["is_interruption"]

    session.workflow_state = {"extracted_entities": [{"description": "pump"}]}
    for message, expected in (
        ("hello", "greeting"),
        ("please help", "help"),
        ("why now", "faq"),
        ("unknown", None),
    ):
        result = service.detect_interruption_intent(message, session)
        assert result["interruption_type"] == expected

    monkeypatch.setattr(WorkflowManager, "is_awaiting_modification", lambda _: (True, "items"))
    assert service.detect_format_modification_intent("formatted", session)
    monkeypatch.setattr(WorkflowManager, "is_awaiting_modification", lambda _: (False, None))
    assert not service.detect_format_modification_intent("formatted", session)

    service.detect_interruption_intent = MagicMock(
        return_value={"is_interruption": True, "interruption_type": "faq"}
    )
    interrupted = await service.classify_intent("hello", {"session": session})
    assert interrupted["intent"] == "faq"
    ai.classify_intent.assert_not_awaited()

    service.detect_interruption_intent.return_value = {
        "is_interruption": False,
        "interruption_type": None,
    }
    service.detect_format_modification_intent = MagicMock(return_value=True)
    modified = await service.classify_intent("formatted response", {"session": session})
    assert modified["intent"] == "format_modification"
    ai.classify_intent.assert_not_awaited()

    assert service.detect_exit_keywords("reset the workflow")
    assert not service.detect_exit_keywords("continue")
    assert service.should_allow_exit(session)


# BFS product parser --------------------------------------------------------


def test_bfs_product_parser_malformed_empty_and_display_completeness(monkeypatch):
    assert bfs_parser.parse_bfs_format("   ") == {"error": "No products found"}
    assert bfs_parser.parse_bfs_format(None) == {"error": "Invalid format"}
    assert "Product name" in bfs_parser.parse_bfs_format("header only")["error"]
    assert "Product name" in bfs_parser.parse_bfs_format("Product 1:")["error"]

    original_split = bfs_parser.re.split

    def return_empty_blocks(pattern, text, *args, **kwargs):
        if pattern == r"\n\s*\n":
            return ["", ""]
        return original_split(pattern, text, *args, **kwargs)

    monkeypatch.setattr(bfs_parser.re, "split", return_empty_blocks)
    assert bfs_parser.parse_bfs_format("synthetic blocks") == {
        "error": "No valid products found"
    }

    products = [{"description": "Laptop"}, {"description": "Monitor"}]
    display = bfs_parser.generate_bfs_display(products)
    assert "Product 1: Laptop" in display and "Product 2: Monitor" in display
    missing_display, labels = bfs_parser.generate_bfs_display_with_missing(
        [{"description": ""}, {"description": ""}]
    )
    assert missing_display.count("Please provide product name") == 2
    assert labels == ["Product name"]
    assert bfs_parser.generate_bfs_display([]) == "No products"
    assert bfs_parser.generate_bfs_display_with_missing([]) == ("No products", [])
    assert bfs_parser.is_bfs_complete(products)
    assert not bfs_parser.is_bfs_complete([{}, {"description": "Monitor"}])
    assert not bfs_parser.is_bfs_complete([])


# BFS bid parser ------------------------------------------------------------


def bid_item(**overrides):
    item = {
        "description": "Dell XPS",
        "specification": "i7",
        "sellPrice": 85000,
        "availableQuantity": 5,
        "ageOfAsset": "2yr",
        "location": "Pune",
    }
    item.update(overrides)
    return item


def bid_text(item, *, price="80000", quantity="2"):
    generated = bid_parser.generate_bid_format([item])
    return generated.replace(
        "a. Your Price:", f"a. Your Price: {price}"
    ).replace("b. Your Qty:", f"b. Your Qty: {quantity}")


def test_bfs_bid_normalization_extraction_generation_and_summary_edges():
    assert bid_parser._normalize_bid_text_newlines("already\nformatted") == "already\nformatted"
    normalized = bid_parser._normalize_bid_text_newlines(
        "header Seller Price: 1 a. Price: 2 b. Qty: 1 2. next"
    )
    assert normalized.count("\n") >= 3

    raw = (
        "Place your bids:\n*Place Bid*\n*modify the prices\n_copy the format\n"
        "_remove any items\n*stock available\n*verify your bids\n_footnote\n"
        "Dell: 10\nPlease confirm"
    )
    section, remaining = bid_parser._extract_bid_format_section(raw)
    assert section == "Dell: 10"
    assert remaining == "Please confirm"
    assert bid_parser._extract_bid_format_section("instructions only") == (
        "instructions only",
        "",
    )

    assert bid_parser.generate_bid_format([]) == ""
    sparse = bid_parser.generate_bid_format([{"description": "A"}])
    assert "Seller Price: 0" in sparse and "Available Qty: 1" in sparse
    long_item = bid_item(description="D" * 70, specification="S" * 40)
    assert len(bid_parser._build_item_key(long_item).split(" -- ")[0]) == 50
    assert len(bid_parser._build_item_key(long_item).split(" -- ")[1]) == 20

    valid_summary = bid_parser.generate_bid_summary(
        [{"key": "Dell", "price": 10, "quantity": 1, "original_item": bid_item()}]
    )
    assert "Seller Price" in valid_summary and "Your Qty: 1" in valid_summary
    assert bid_parser.generate_bid_summary(
        [{"key": "ignored", "price": 1, "quantity": 0}]
    ) == ""
    assert bid_parser.generate_bid_summary([]) == ""


def test_bfs_bid_parse_empty_alternative_header_and_validation_errors(monkeypatch):
    item = bid_item()
    assert "No bid items" in bid_parser.parse_bid_format("", [item])["error"]

    alternative = (
        "1. Dell XPS -- i7:\n"
        "a. Price: 2,500.50\n"
        "b. Qty: 2"
    )
    parsed = bid_parser.parse_bid_format(alternative, [item])
    assert parsed["bids"][0]["price"] == 2500.50
    assert parsed["bids"][0]["quantity"] == 2

    generated = bid_parser.generate_bid_format([item])
    cases = (
        (generated.replace("a. Your Price:", "a. Other:"), "Missing price"),
        (
            generated.replace("a. Your Price:", "a. Your Price: 1")
            .replace("b. Your Qty:", "b. Other:"),
            "Missing quantity",
        ),
        (
            generated.replace("a. Your Price:", "a. Your Price: 0")
            .replace("b. Your Qty:", "b. Your Qty: 1"),
            "greater than 0",
        ),
        (
            generated.replace("a. Your Price:", "a. Your Price: 1")
            .replace("b. Your Qty:", "b. Your Qty: 0"),
            "greater than 0",
        ),
        (
            generated.replace("Dell XPS -- i7", "Other -- i7")
            .replace("a. Your Price:", "a. Your Price: 1")
            .replace("b. Your Qty:", "b. Your Qty: 1"),
            "not found",
        ),
        (
            generated.replace("a. Your Price:", "a. Your Price: 1")
            .replace("b. Your Qty:", "b. Your Qty: 6"),
            "exceeds available",
        ),
    )
    for text, expected in cases:
        assert expected in bid_parser.parse_bid_format(text, [item])["error"]

    assert "Invalid header" in bid_parser.parse_bid_format("not a bid", [item])["error"]

    original_split = bid_parser.re.split
    monkeypatch.setattr(bid_parser.re, "split", lambda *args, **kwargs: [])
    assert "No bid items found" in bid_parser.parse_bid_format("some bid", [item])["error"]
    monkeypatch.setattr(bid_parser.re, "split", original_split)


# Sectioned RFQ parser ------------------------------------------------------


def test_sectioned_delivery_extraction_validation_display_and_completeness(monkeypatch):
    three_fields = "Intro\nDate: 12 Nov 2025\nPincode: 411005\nCity: Pune\nWhat next?"
    extracted, remaining = sectioned_parser._extract_delivery_format_section(three_fields)
    assert extracted.endswith("City: Pune")
    assert remaining == "What next?"
    assert sectioned_parser._extract_delivery_format_section("nothing") == ("nothing", "")

    success = sectioned_parser.parse_delivery_format(
        "Date: 12 Nov 2025\nPincode: 411005\nQuestion?"
    )
    assert success["deliveryDate"] == "12 Nov 2025"
    assert success["additional_text"] == "Question?"
    assert "Delivery Date" in sectioned_parser.parse_delivery_format("Pincode: 411005")["error"]
    assert "Delivery Pincode" in sectioned_parser.parse_delivery_format("Date: tomorrow")["error"]
    assert "6-digit" in sectioned_parser.parse_delivery_format(
        "Date: tomorrow\nPincode: 123"
    )["error"]

    monkeypatch.setattr(
        sectioned_parser,
        "_extract_delivery_format_section",
        MagicMock(side_effect=RuntimeError("extract")),
    )
    assert sectioned_parser.parse_delivery_format("anything") == {
        "error": "Invalid format. Please follow the exact format shown."
    }

    display, missing = sectioned_parser.generate_delivery_display_with_missing(
        {"pincode": "411005"}
    )
    assert "Please provide delivery date" in display
    assert missing == ["Delivery Date"]
    display, missing = sectioned_parser.generate_delivery_display_with_missing(
        {"deliveryDate": "today"}
    )
    assert "Please provide 6-digit pincode" in display
    assert missing == ["Delivery Pincode"]
    assert sectioned_parser.generate_delivery_display({"deliveryDate": "today"}).endswith(
        "Delivery Pincode: "
    )
    assert "Valid 6-digit" in sectioned_parser.generate_delivery_display_with_invalid_pincode({})

    complete = {"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"}
    assert sectioned_parser.validate_delivery_completeness(complete)
    for field in complete:
        incomplete = complete.copy()
        incomplete[field] = ""
        assert not sectioned_parser.validate_delivery_completeness(incomplete)
    assert not sectioned_parser.validate_delivery_completeness({})


def test_sectioned_items_extraction_validation_display_and_completeness(monkeypatch):
    text = (
        "Items (1):\nItem 1: Widget\nQty: 2\nUoM: box\nBrand: Acme\n"
        "Specification: steel\nPlease confirm"
    )
    extracted, remaining = sectioned_parser._extract_items_format_section(text)
    assert "Item 1: Widget" in extracted and remaining == "Please confirm"
    assert sectioned_parser._extract_items_format_section("no fields") == ("no fields", "")

    parsed = sectioned_parser.parse_items_format(text)
    assert parsed["items"][0]["unitofMeasures"] == "box"
    assert parsed["items"][0]["brand"] == "Acme"
    assert parsed["items"][0]["remarks"] == "steel"
    assert parsed["additional_text"] == "Please confirm"

    embedded_uom = sectioned_parser.parse_items_format(
        "Item 1: Widget\nQty: 2\nSpecification: UoM: each"
    )
    assert embedded_uom["items"][0]["unitofMeasures"] == "each"
    assert embedded_uom["items"][0]["remarks"] == ""
    both_missing = sectioned_parser.parse_items_format("Item 1:\nQty: 2")
    assert "Item name/description" in both_missing["error"]
    assert "Qty" not in both_missing["error"]
    assert "Qty" in sectioned_parser.parse_items_format("Item 1: Widget")["error"]
    assert "No valid" in sectioned_parser.parse_items_format("RFQ Items (1):")["error"]
    assert "No items" in sectioned_parser.parse_items_format("\n")["error"]
    assert sectioned_parser.parse_items_format(None) == {
        "error": "Invalid format. Please follow the exact format shown."
    }

    monkeypatch.setattr(
        sectioned_parser,
        "_extract_items_format_section",
        MagicMock(side_effect=RuntimeError("extract")),
    )
    assert sectioned_parser.parse_items_format("anything") == {
        "error": "Invalid format. Please follow the exact format shown."
    }

    assert sectioned_parser._format_quantity("") == ""
    assert sectioned_parser._format_quantity(2.5) == "2.5"
    assert sectioned_parser._format_quantity("not numeric") == "not numeric"
    assert sectioned_parser._sanitize_text("a\x00b\x01c\n") == "abc\n"
    assert sectioned_parser._sanitize_text(None) == ""
    assert sectioned_parser._truncate_text("abcdefghijk", 8) == "abcde..."

    products = [
        {"description": "A", "quantity": 1},
        {"description": "B", "quantity": 2},
        {"description": "C", "quantity": 3},
        {"description": "D", "quantity": 4},
        {"description": "E", "quantity": 5},
        {"description": "F", "quantity": 6},
    ]
    assert "+1 more item" in sectioned_parser.generate_items_display(products)
    assert sectioned_parser.generate_items_display([]) == "No items"

    missing_display, labels = sectioned_parser.generate_items_display_with_missing(
        [{"description": "A", "quantity": 1}, {"description": "", "quantity": None}],
        [
            {"index": 1, "missing_fields": ["description", "quantity"]},
            {"index": 2, "missing_fields": ["description", "quantity"]},
        ],
    )
    assert "Please provide product name" in missing_display
    assert labels == ["Product Name", "Quantity"]
    assert sectioned_parser.generate_items_display_with_missing([], []) == ("No items", [])

    original_limit = sectioned_parser.MAX_MESSAGE_LENGTH
    try:
        sectioned_parser.MAX_MESSAGE_LENGTH = 1
        assert sectioned_parser.generate_items_display(products).startswith("6 items")
        assert sectioned_parser.generate_items_display_with_missing(products, []) == (
            "6 items (details too long to display)",
            [],
        )
    finally:
        sectioned_parser.MAX_MESSAGE_LENGTH = original_limit

    assert sectioned_parser.validate_items_completeness(
        [{"description": "", "quantity": 1}, {"description": "ok", "quantity": 2}]
    )
    assert not sectioned_parser.validate_items_completeness(
        [{"description": "ok", "quantity": 0}, {"description": "", "quantity": 3}]
    )
    assert not sectioned_parser.validate_items_completeness([])


# Legacy format modification handler ---------------------------------------


def handler_session():
    return SimpleNamespace(session_id="format-session", workflow_state={})


def handler_user():
    return SimpleNamespace(phone_number="+919999999999")


@pytest.mark.asyncio
async def test_format_handler_reachable_retry_and_max_retry_paths(monkeypatch):
    whatsapp = AsyncMock()
    handler = FormatModificationHandler(whatsapp)
    handler.session_manager = SimpleNamespace(save_session=AsyncMock())
    session = handler_session()
    user = handler_user()

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (False, None))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "unknown"))
    assert (await handler.handle_format_modification("x", session, user))["status"] == "error"

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "delivery"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_a, **_k: 1)
    retry = await handler.handle_format_modification("bad delivery", session, user)
    assert retry == {
        "status": "format_error",
        "message": "Format validation failed",
        "retry_count": 1,
    }
    whatsapp.send_message.assert_awaited()
    assert "Attempt 1 of 3" in whatsapp.send_message.await_args.args[1]
    handler.session_manager.save_session.assert_awaited_once_with(
        session, persist_to_db=False
    )

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "items"))
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_a, **_k: 2)
    retry_items = await handler.handle_format_modification("bad items", session, user)
    assert retry_items["retry_count"] == 2
    assert handler.session_manager.save_session.await_count == 2

    clear_awaiting = MagicMock()
    monkeypatch.setattr(format_mod.WorkflowManager, "clear_awaiting_modification", clear_awaiting)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_a, **_k: 3)
    handler.session_manager.save_session.reset_mock()
    maxed = await handler.handle_format_modification("still bad", session, user)
    assert maxed == {
        "status": "max_retries_reached",
        "message": "Max retries exceeded",
        "retry_count": 3,
    }
    clear_awaiting.assert_called_once_with(session, caller="format_mod_handler")
    assert "Maximum retry attempts (3) reached." in whatsapp.send_message.await_args.args[1]
    handler.session_manager.save_session.assert_not_awaited()

    # Counts above the limit take the same terminal path.
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_a, **_k: 4)
    over_limit = await handler._handle_format_error(session, user, "bad", "items")
    assert over_limit["status"] == "max_retries_reached"


@pytest.mark.asyncio
async def test_format_handler_converts_reachable_state_and_session_errors(monkeypatch):
    # The public wrapper converts state-manager failures to its generic error.
    handler = FormatModificationHandler(AsyncMock())
    session = handler_session()
    user = handler_user()

    def state_failure(_session):
        raise RuntimeError("state unavailable")

    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", state_failure)
    result = await handler.handle_format_modification("x", session, user)
    assert result == {
        "status": "error",
        "message": "An error occurred while processing your modification. Please try again.",
    }

    # The placeholder parser result is always an error.  Without a session
    # manager, the reachable non-max retry path fails after sending and is
    # likewise converted by the public wrapper.
    handler = FormatModificationHandler(AsyncMock())
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _: (True, "delivery"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_a, **_k: 1)
    result = await handler.handle_format_modification("bad", session, user)
    assert result["status"] == "error"
    assert "processing your modification" in result["message"]
