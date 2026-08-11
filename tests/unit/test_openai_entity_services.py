from __future__ import annotations

import asyncio
import builtins
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.entity_service as entity_module
import app.services.openai_service as openai_module


class FakeFile:
    def __init__(self, text='{"name": "tool"}'):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.text


def function_response(arguments, output_type="function_call", output_text=""):
    return SimpleNamespace(
        output=[SimpleNamespace(type=output_type, arguments=json.dumps(arguments))],
        output_text=output_text,
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, input_tokens_details=SimpleNamespace(cached_tokens=2)),
    )


def empty_response(output_text=""):
    return SimpleNamespace(output=[], output_text=output_text, usage=None)


@pytest.fixture
def openai_service(monkeypatch):
    settings = SimpleNamespace(
        openai_model_default="default-model",
        openai_model_advanced="advanced-model",
        support_email="support@example.com",
        support_contact_info="call +91 999",
        PROCUCEV_PORTAL_URL="https://portal.example",
    )
    interaction_logger = MagicMock()
    monkeypatch.setattr(openai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_module, "get_interaction_logger", lambda: interaction_logger)
    service = openai_module.OpenAIService()
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock()),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
        close=AsyncMock(),
    )
    service._client = client
    return service, client, settings, interaction_logger


def install_tool_io(monkeypatch, service, prompt="system prompt"):
    monkeypatch.setattr(service, "_load_prompt", lambda *args, **kwargs: prompt)
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: FakeFile())


@pytest.mark.asyncio
async def test_openai_constructor_client_lifecycle_helpers_and_prompt_loading(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        openai_model_default="d", openai_model_advanced="a", support_email="s@example.com",
        support_contact_info="help", PROCUCEV_PORTAL_URL="https://portal",
    )
    logger = MagicMock()
    monkeypatch.setattr(openai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_module, "get_interaction_logger", lambda: logger)
    constructed = MagicMock()
    constructed.close = AsyncMock()
    monkeypatch.setattr(openai_module, "AsyncOpenAI", lambda **kwargs: constructed)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://endpoint")

    service = openai_module.OpenAIService()
    assert service.default_model == "d" and service.advanced_model == "a"
    assert service.client is constructed
    assert constructed.close.await_count == 0
    service._client_closed = True
    assert service.client is constructed
    await service.close()
    assert constructed.close.await_count == 1
    service.close_sync()
    await service.__aexit__(None, None, None)
    assert await service.__aenter__() is service

    service._client = None
    service.close_sync()  # no client is a safe no-op
    service._track_openai_call("intent", "9199")
    service._track_openai_call("intent", "9199")
    service._track_openai_call("entity")
    assert service.get_call_summary("9199") == {"intent": 2}
    assert service.get_call_summary() == {"entity": 1}
    assert service.get_call_summary("missing") == {}

    service.prompts_dir = tmp_path
    prompt_dir = tmp_path / "response_generation"
    prompt_dir.mkdir()
    (prompt_dir / "hello.txt").write_text("Hi {name} {support_email} {support_info_email} {portal_url}", encoding="utf-8")
    assert service._load_prompt("response_generation", "hello", name="Ada") == "Hi Ada s@example.com help https://portal"
    assert service._load_prompt("missing", "not_here").startswith("Generate an appropriate response")
    assert service._load_prompt("response_generation", "_get_seller_common_response_prompt", workflow_state="end_of_flow_reminder").startswith("Generate")

    notifier = MagicMock(notify_general_error=AsyncMock())
    service._error_notification_service = notifier
    await service._notify_openai_error("API", "down", "method")
    notifier.notify_general_error.assert_awaited_once()
    notifier.notify_general_error.side_effect = RuntimeError("notify")
    await service._notify_openai_error("API", "down", "method")


def test_openai_message_helpers_and_fallbacks(openai_service):
    service, _, settings, _ = openai_service
    history = [{"role": "user", "content": str(i)} for i in range(12)]
    assert len(service._build_messages_with_history({"conversation_history": {"openai_messages": history}}, "now")) == 11
    assert service._build_messages_with_history({"conversation_history": {"openai_messages": "bad"}}, "now")[-1]["content"] == "now"
    assert service._build_messages_with_history(None) == []
    image = '{"mime_type":"image/jpeg","data":"..."}'
    assert service._is_image_content(image)
    assert service._is_image_content("text", {"user_message": {"mime_type": "image/jpeg"}})
    assert not service._is_image_content("text", {"user_message": "text"})

    assert service.extract_text_from_message({"content": {"type": "button_reply", "button_reply": {"title": "Yes"}}}) == "Yes"
    assert service.extract_text_from_message({"content": {"type": "button_reply", "button_reply": {"id": "yes"}}}) == "yes"
    assert service.extract_text_from_message({"content": "hello"}) == "hello"
    assert service.extract_text_from_message({"content": {"text": "dict"}}) == "dict"
    assert service.extract_text_from_message({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "a b"
    assert service.extract_text_from_message({"content": [{"type": "image"}]}) == "[multimodal content]"
    assert service.extract_text_from_message({"content": 4}) == "[unknown content]"
    assert service._get_fallback_response({}, []) == pytest.approx(service._get_fallback_response({}, []))
    assert "support@example.com" in service._get_fallback_response({}, [])
    fallback = service._get_fallback_intent_response("broken")
    assert fallback["intent"] == "general_inquiry" and not fallback["success"]


@pytest.mark.asyncio
async def test_openai_classify_intent_structured_context_image_and_fallbacks(monkeypatch, openai_service):
    service, client, _, log = openai_service
    install_tool_io(monkeypatch, service)
    client.responses.create.return_value = function_response({
        "intent": "buy_something", "confidence": 92, "relevant_message": "yes",
        "irrelevant_message": None, "all_intent_scores": {"buy_something": 92},
        "context_analysis": {"x": 1}, "reasoning": "clear", "suggested_clarification": None,
    })
    context = {
        "user_role": "buyer", "workflow_type": "rfq_creation", "conversation_stage": "collecting",
        "conversation_history": {"openai_messages": [{"role": "user", "content": "old"}, {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}]},
        "workflow_state": {"sectioned_rfq": {"active": True, "current_section": "items", "awaiting_items_modification": True}, "pending_rfq": {}, "pending_optional_rfq": {}, "pending_attachment_decision": {}, "extracted_entities": [{"description": "bolt"}]},
    }
    result = await service.classify_intent({"text": "buy bolts"}, context)
    assert result["intent"] == "buy_something" and result["success"]
    sent = client.responses.create.await_args.kwargs
    assert sent["input"][0]["role"] == "developer" and "Sectioned RFQ Active" in sent["input"][0]["content"]
    log.log_intent_classification.assert_called_once()
    assert service.get_call_summary()["intent_classification"] == 1

    client.responses.create.return_value = function_response({"intent": "greeting", "confidence": 80})
    await service.classify_intent("{mime_type:image/png}", {"user_message": "{mime_type:image/png}"})
    assert client.responses.create.await_args.kwargs["input"] == [{"role": "user", "content": "User sent an image attachment"}]

    service._get_fallback_classification = AsyncMock(return_value={"fallback": True})
    client.responses.create.return_value = empty_response()
    assert await service.classify_intent("hello") == {"fallback": True}
    client.responses.create.side_effect = RuntimeError("boom")
    service._notify_openai_error = AsyncMock()
    assert await service.classify_intent("hello") == {"fallback": True}
    service._get_fallback_classification.assert_awaited()


@pytest.mark.asyncio
async def test_openai_classify_rate_limit_and_fallback_notification(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    class FakeRateLimit(Exception):
        pass
    monkeypatch.setattr(openai_module, "RateLimitError", FakeRateLimit)
    service._notify_openai_error = AsyncMock()
    service._get_fallback_classification = AsyncMock(return_value={"fallback": True})
    client.responses.create.side_effect = FakeRateLimit("rate")
    monkeypatch.setattr(openai_module, "get_user_phone_context", lambda: None)
    assert await service.classify_intent("hello") == {"fallback": True}
    service._handle_rate_limit_timeout = AsyncMock()
    monkeypatch.setattr(openai_module, "get_user_phone_context", lambda: "9199")
    assert (await service.classify_intent("hello"))["timeout_handled"]
    service._handle_rate_limit_timeout.assert_awaited_once_with("9199")


@pytest.mark.asyncio
async def test_openai_extract_entities_all_structured_formats_and_errors(monkeypatch, openai_service):
    service, client, _, log = openai_service
    install_tool_io(monkeypatch, service)
    cases = [
        ("modification_request", {"modifications": [{"operation_type": "modify"}], "has_new_values": True, "modification_intent": "quantity"}, "is_modification_extraction"),
        ("buy_something", {"products": [{"description": "bolt"}], "deliveryDate": "2030-01-01", "state": "MH", "city": "Pune", "pincode": "411005", "confidence": 90}, "products"),
        ("rfq_status_check", {"rfq_id": "RFQ-1", "confidence": 88}, "rfq_id"),
        ("registration_buyer", {"entities": {"email": "a@example.com"}, "completeness": 50, "missing_fields": ["phone"], "validation_errors": [], "confidence": 70}, "raw_extraction"),
        ("product_search", {"entities": {"description": "nut"}, "completeness": 20, "missing_fields": [], "confidence": 60, "next_questions": ["qty"]}, "entities"),
    ]
    for workflow, args, marker in cases:
        client.responses.create.return_value = function_response(args)
        result = await service.extract_entities("request", workflow)
        assert result["success"] and marker in result
    assert log.log_entity_extraction.call_count == len(cases)

    client.responses.create.return_value = empty_response()
    failed = await service.extract_entities("request", "bfs")
    assert failed["success"] is False and failed["products"] == []
    class FakeAPI(Exception):
        pass
    monkeypatch.setattr(openai_module, "APIError", FakeAPI)
    client.responses.create.side_effect = FakeAPI("api")
    service._notify_openai_error = AsyncMock()
    failed = await service.extract_entities("request", "product_search")
    assert failed["success"] is False
    client.responses.create.side_effect = RuntimeError("unexpected")
    failed = await service.extract_entities("request", "product_search")
    assert failed["success"] is False


@pytest.mark.asyncio
async def test_openai_reference_and_summary_methods(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    client.responses.create.return_value = function_response({"products": [{"description": "usual"}], "resolved_references": [{"reference_phrase": "usual"}], "confidence": 84})
    result = await service.extract_entities_with_summary_context("same as usual", [{"date": "today", "summary": "last", "entities": {"city": "Pune"}, "rfq_ids": ["R1"], "outcome": "done"}])
    assert result["products"] and result["resolved_references"]
    client.responses.create.return_value = function_response({"has_references": True, "confidence": 91, "reference_types": ["address"], "detected_phrases": ["usual"], "reasoning": "context"})
    assert (await service.analyze_reference_context("usual address"))["has_references"]
    client.responses.create.return_value = empty_response()
    assert not (await service.analyze_reference_context("none"))["success"]
    client.responses.create.side_effect = RuntimeError("reference")
    assert not (await service.analyze_reference_context("none"))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"chosen_action": "switch_to_new", "confidence": 90, "reasoning": "yes", "detected_keywords": ["new"], "ambiguity_level": "low"})
    switch = await service.analyze_intent_switch_response("new request", {"current_workflow": "rfq", "new_intent": "sell", "new_intent_message": "sell items"})
    assert switch["chosen_action"] == "switch_to_new"
    client.responses.create.return_value = empty_response()
    assert (await service.analyze_intent_switch_response("?", {}))["chosen_action"] == "continue_current"
    client.responses.create.side_effect = RuntimeError("switch")
    assert not (await service.analyze_intent_switch_response("?", {}))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"success": True, "updated_products": [{"city": "Pune"}], "merge_actions": ["city"]})
    merged = await service.merge_resolved_references_with_entities([{"city": "old"}], [{"resolved_value": "Pune"}], "usual")
    assert merged["updated_products"][0]["city"] == "Pune"
    client.responses.create.return_value = empty_response()
    assert not (await service.merge_resolved_references_with_entities([], [], "x"))["success"]
    client.responses.create.side_effect = RuntimeError("merge")
    assert "merge" in (await service.merge_resolved_references_with_entities([], [], "x"))["error"]


@pytest.mark.asyncio
async def test_openai_response_generation_and_validation_methods(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    context = {"conversation_history": {"openai_messages": [{"role": "user", "content": "old"}]}, "user_message": "hello", "user_role": "buyer"}
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="answer")
    assert await service.generate_response(context, [{"x": 1}]) == "answer"
    client.responses.create.side_effect = RuntimeError("response")
    assert "technical issue" in await service.generate_response(context)
    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="status")
    assert await service.generate_rfq_status_response(context) == "status"
    assert await service.generate_seller_rfq_overview_response(context) == "status"
    client.responses.create.return_value = function_response({"intent": "answer", "confidence": 80, "reasoning": "r"})
    assert (await service.generate_seller_intent({"workflow_state": {}, "message_type": "general"}))["intent"] == "answer"
    client.responses.create.return_value = empty_response()
    assert (await service.generate_seller_intent({}))["intent"] == "general_question"
    client.responses.create.side_effect = RuntimeError("seller")
    assert "technical issue" in await service.generate_seller_intent({})

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"is_valid": False, "validation_score": 45, "suggestions": ["fix"], "reason": "bad", "normalized_value": None, "severity": "warning"})
    assert (await service.validate_field_value("quantity", "x", {"item": "bolt"}))["validation_score"] == 45
    client.responses.create.return_value = empty_response()
    assert (await service.validate_field_value("quantity", "x", {}))["is_valid"]
    client.responses.create.side_effect = RuntimeError("validation")
    assert (await service.validate_field_value("quantity", "x", {}))["severity"] == "warning"

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"acknowledgment": "Thanks", "progress_update": "Half", "next_question": "Qty?"})
    assert await service.generate_contextual_response({"user_message": "x"}, ["What?"]) == "Thanks\n\nHalf\n\nQty?"
    client.responses.create.return_value = empty_response()
    assert (await service.generate_contextual_response({}, ["First?"])).endswith("First?")
    assert "details" in await service.generate_contextual_response({}, [])
    client.responses.create.side_effect = RuntimeError("context")
    assert "details" in await service.generate_contextual_response({}, [])

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"confirmation": "Done", "summary": "Summary", "next_steps": "Next", "reference_id": "R1"})
    completion = await service.generate_completion_response({}, {"user_message": "x"})
    assert "Summary:" in completion and "Reference: R1" in completion
    client.responses.create.return_value = empty_response()
    assert await service.generate_completion_response({}, {}) == "RFQ completed. Processing request."
    client.responses.create.side_effect = RuntimeError("complete")
    assert await service.generate_completion_response({}, {}) == "RFQ completed. Processing request."

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"progress_acknowledgment": "Ack", "questions": ["A?", "B?"]})
    clarification = await service.generate_clarification_response(["Fallback?"], 40, {"user_message": "x"})
    assert "• A?" in clarification and "• B?" in clarification
    client.responses.create.return_value = empty_response()
    assert "Fallback?" in await service.generate_clarification_response(["Fallback?"], 40, {})


@pytest.mark.asyncio
async def test_openai_excel_division_summary_mapping_and_seller_items(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    client.responses.create.return_value = function_response({"header_row_index": 2, "confidence": 90, "reasoning": "headers"})
    assert (await service.detect_excel_header_row([[None, "x"], ["Item", "Qty"]]))["header_row_index"] == 2
    client.responses.create.return_value = empty_response()
    assert (await service.detect_excel_header_row([]))["header_row_index"] == 0
    client.responses.create.side_effect = RuntimeError("excel")
    assert (await service.detect_excel_header_row([]))["success"] is False

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"selected_division": "IT", "confidence": 80, "reasoning": "fit", "alternative_divisions": [], "product_category_analysis": {}})
    assert (await service.select_division({"description": "laptop"}))["selected_division"] == "IT"
    client.responses.create.return_value = empty_response()
    assert (await service.select_division({}))["selected_division"] == "Admin & IT"
    client.responses.create.side_effect = RuntimeError("division")
    assert (await service.select_division({}))['confidence'] == 20

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="summary")
    assert await service.generate_session_summary({"user_id": "U", "conversation_messages": [{"sender": "u", "content": "hi"}]}) == "summary"
    client.responses.create.side_effect = RuntimeError("summary")
    assert await service.generate_session_summary({}) == "Session completed"

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"mapping_details": [{"excel_header": "Qty", "target_column": "Quantity"}], "confidence": 75, "unmapped_headers": []})
    assert (await service.map_excel_columns(["Qty"]))["column_mapping"] == {"Qty": "Quantity"}
    client.responses.create.return_value = empty_response()
    assert not (await service.map_excel_columns(["bad"]))["success"]
    client.responses.create.side_effect = RuntimeError("map")
    assert not (await service.map_excel_columns(["bad"]))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"items": ["tools", "parts"], "confidence": 95})
    parsed = await service.parse_seller_product_items("tools, parts")
    assert parsed["items"] == ["tools", "parts"]
    client.responses.create.return_value = empty_response()
    assert (await service.parse_seller_product_items("one"))["fallback_used"]
    client.responses.create.side_effect = RuntimeError("parse")
    assert (await service.parse_seller_product_items("one"))["fallback_used"]


@pytest.mark.asyncio
async def test_openai_categorization_learning_and_seller_selection(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    similar = [{"item": "bolt", "category": "Hardware", "similarity_score": 0.9}]
    client.responses.create.return_value = function_response({"category": "Hardware", "confidence_score": 0.9, "reasoning": "close"})
    assert (await service.categorize_with_similar_items("bolt", similar, ["Hardware"]))["category"] == "Hardware"
    client.responses.create.return_value = empty_response()
    assert (await service.categorize_with_similar_items("bolt", similar))["category"] == "Hardware"
    client.responses.create.side_effect = RuntimeError("cat")
    assert (await service.categorize_with_similar_items("bolt", similar))["category"] == "Hardware"
    assert (await service.categorize_with_similar_items("bolt", []))["category"] is None

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"level_1_category": "IT", "level_2_category": "Laptops", "level_3_category": "Business", "confidence_score": 0.8, "reasoning": "fit"})
    three = await service.generate_3_level_categorization("laptop", similar, "IT")
    assert three["categorization"]["level_2"] == "Laptops"
    client.responses.create.return_value = empty_response()
    assert not (await service.generate_3_level_categorization("x", []))["success"]
    client.responses.create.side_effect = RuntimeError("learn")
    assert not (await service.generate_3_level_categorization("x", []))["success"]
    service._client = client
    service._client_closed = False

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"is_valid": True, "confidence_score": 0.9, "validation_issues": [], "suggestions": []})
    assert (await service.validate_learning_category("a", "b", "c", "item"))["is_valid"]
    client.responses.create.return_value = empty_response()
    assert (await service.validate_learning_category("a", "b", "c", "item"))["is_valid"]
    client.responses.create.side_effect = RuntimeError("learn validate")
    assert not (await service.validate_learning_category("a", "b", "c", "item"))["is_valid"]

    cats = [
        {"id": 1, "level_1_category": "IT", "level_2_category": "Hardware", "level_3_category": "Laptop"},
        {"id": 2, "level_1_category": "IT", "level_2_category": "Hardware", "level_3_category": "Desktop"},
    ]
    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"selected_category_id": 1, "similarity_score": 0.9, "reasoning": "match"})
    mapped = await service.map_seller_category_to_existing_learning("Computers", cats, "Acme", {"city": "Pune", "state": "MH"})
    assert mapped["success"] and mapped["selected_category"]["id"] == 1
    client.responses.create.return_value = function_response({"selected_category_id": 999})
    assert not (await service.map_seller_category_to_existing_learning("x", cats))["success"]
    client.responses.create.side_effect = RuntimeError("seller map")
    assert not (await service.map_seller_category_to_existing_learning("x", cats))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"mappings": [{"seller_category": "Computers", "selected_category_id": 1, "similarity_score": 0.8, "reasoning": "fit"}, {"seller_category": "bad", "selected_category_id": 999}]})
    batch = await service.map_seller_categories_batch(["Computers", "bad"], cats)
    assert batch["Computers"]["success"] and not batch["bad"]["success"]
    client.responses.create.side_effect = RuntimeError("batch")
    assert not (await service.map_seller_categories_batch(["x"], cats))["x"]["success"]

    candidates = [{"seller_id": "S1", "seller_name": "Acme", "ranking": "gold", "category_match": {"original_category": "Hardware", "similarity_score": 0.8}, "distance_km": 2, "phone_number": "9", "location": {"city": "Pune", "state": "MH"}}]
    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"selected_seller_ids": ["S1", "missing"], "reasoning": "best", "confidence_score": 0.9})
    selected = await service.select_best_sellers("bolt", candidates, 1)
    assert selected["total_selected"] == 1
    assert (await service.select_best_sellers("bolt", []))["success"] is False
    client.responses.create.return_value = empty_response()
    assert not (await service.select_best_sellers("bolt", candidates))["success"]
    client.responses.create.side_effect = RuntimeError("select")
    assert not (await service.select_best_sellers("bolt", candidates))["success"]


@pytest.mark.asyncio
async def test_openai_confirmation_opt_in_dates_registration_and_misc(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    data = {"items": [{"description": "x", "brand": "b\x00" + "z" * 100, "remarks": "r"}] * 6, "delivery_date": "2030-01-01"}
    long_summary = "line1\\nline2 " * 250
    client.responses.create.return_value = function_response({"summary": long_summary})
    confirmation = await service.generate_rfq_confirmation(data, {"user_message": "buy"})
    assert "line1\nline2" in confirmation and "+1 more items" in confirmation
    client.responses.create.side_effect = RuntimeError("confirm")
    assert await service.generate_rfq_confirmation({}, {}) == "Here's a summary of your RFQ."

    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="generated")
    assert await service.generate_opt_out_confirmation("Acme") == "generated"
    assert await service.generate_opt_in_confirmation("Acme", ["Tools"]) == "generated"
    assert await service.generate_permission_request("Acme", []) == "generated"
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="")
    assert "opted out" in await service.generate_opt_out_confirmation("Acme")
    client.responses.create.side_effect = RuntimeError("message")
    assert "welcome back" in await service.generate_opt_in_confirmation("Acme", [])

    future = (date.today() + timedelta(days=10)).isoformat()
    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"is_valid": True, "normalized_date": future, "validation_issues": [], "user_friendly_message": "", "confidence": 90, "reasoning": "future"})
    assert (await service.validate_delivery_date("next week", future))["is_valid"]
    past = (date.today() - timedelta(days=1)).isoformat()
    client.responses.create.return_value = function_response({"is_valid": True, "normalized_date": past, "validation_issues": [], "confidence": 90})
    assert not (await service.validate_delivery_date("yesterday", past))["is_valid"]
    client.responses.create.return_value = empty_response()
    assert not (await service.validate_delivery_date("bad"))["success"]
    client.responses.create.side_effect = RuntimeError("date")
    assert (await service.validate_delivery_date("bad"))["parsing_method"] == "error"

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"intent": "opt_out", "confidence": 95, "reasoning": "stop", "detected_phrases": ["stop"]})
    assert (await service.detect_opt_out_intent("stop"))["intent"] == "opt_out"
    client.responses.create.return_value = empty_response()
    assert (await service.detect_opt_out_intent("?"))["intent"] == "none"
    client.responses.create.side_effect = RuntimeError("opt")
    assert (await service.detect_opt_out_intent("?"))["confidence"] == 20

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"entities": {"email": "a@x.com"}, "completeness": 80, "missing_fields": [], "validation_errors": [], "confidence": 90})
    reg = await service.extract_registration_entities("email a@x.com", "old", "buyer", {"name": "A"})
    assert reg["entities"]["email"] == "a@x.com" and "email" in reg["extracted_fields"]
    client.responses.create.return_value = empty_response()
    assert not (await service.extract_registration_entities("x"))["success"]
    client.responses.create.side_effect = RuntimeError("reg")
    assert not (await service.extract_registration_entities("x"))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"status": "confirmed", "selected_email": "a@x.com"})
    assert (await service.parse_email_confirmation("yes", ["a@x.com"]))["status"] == "confirmed"
    client.responses.create.return_value = empty_response()
    assert not (await service.parse_email_confirmation("?", []))["success"]
    client.responses.create.side_effect = RuntimeError("email")
    assert not (await service.parse_email_confirmation("?", []))["success"]

    service.classify_intent = MagicMock(return_value={"intent": "sell_something", "confidence": 70, "success": True})
    assert service.classify_auth_intent("sell")["intent"] == "sell"
    service.classify_intent.side_effect = RuntimeError("auth")
    assert service.classify_auth_intent("?")["intent"] == "unclear"

    client.responses.create.side_effect = None
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="completion"))])
    assert await service.get_completion("prompt") == "completion"
    client.chat.completions.create.side_effect = RuntimeError("chat")
    with pytest.raises(RuntimeError, match="chat"):
        await service.get_completion("prompt")

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"rfq_ids": ["R1"], "confidence": 80, "reasoning": "found"})
    assert (await service.extract_rfq_ids_from_message("R1", "which RFQ?"))["rfq_ids"] == ["R1"]
    client.responses.create.return_value = empty_response()
    assert not (await service.extract_rfq_ids_from_message("?", "x"))["success"]
    client.responses.create.side_effect = RuntimeError("ids")
    assert not (await service.extract_rfq_ids_from_message("?", "x"))["success"]

    client.responses.create.side_effect = None
    client.responses.create.return_value = function_response({"rfqs": [{"items": []}], "processing_summary": {"count": 1}, "confidence": 70})
    assert (await service.process_excel_to_rfqs("rows", "book.xlsx"))["rfqs"]
    client.responses.create.return_value = empty_response()
    assert not (await service.process_excel_to_rfqs("rows", "book.xlsx"))["success"]
    client.responses.create.side_effect = RuntimeError("rfq excel")
    assert not (await service.process_excel_to_rfqs("rows", "book.xlsx"))["success"]


@pytest.mark.asyncio
async def test_openai_contextual_interaction_confirmation_registration_type_and_helpers(monkeypatch, openai_service):
    service, client, _, _ = openai_service
    install_tool_io(monkeypatch, service)
    prompt = service._build_contextual_analysis_prompt(
        "change it", {"messages": [{"role": "user", "content": "old"}]},
        {"workflow_type": "rfq", "stage": "collecting", "pending_rfq": {"x": 1}},
        [{"description": "bolt", "quantity": 2, "specifications": "steel"}],
    )
    assert "USER MESSAGE" in prompt and "CURRENT EXTRACTED ENTITIES" in prompt and "RECENT CONVERSATION" in prompt
    client.responses.create.return_value = function_response({"response": "done", "actions": [{"type": "modify"}], "context_understanding": {"user_intent": "modify", "confidence": 80}})
    interaction = await service.handle_contextual_interaction("change", {"messages": []}, {}, [])
    assert interaction["response"] == "done"
    client.responses.create.return_value = empty_response()
    assert not (await service.handle_contextual_interaction("?", {}, {}, []))["success"]
    client.responses.create.side_effect = RuntimeError("interaction")
    assert (await service.handle_contextual_interaction("?", {}, {}, []))["context_understanding"]["user_intent"] == "error"

    confirmation_file = MagicMock()
    confirmation_file.__enter__.return_value = confirmation_file
    confirmation_file.read.return_value = "Classify"
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: confirmation_file)
    client.responses.create.side_effect = None
    client.responses.create.return_value = SimpleNamespace(output=[], output_text=" YES ")
    assert await service.parse_confirmation_response("yes") == "yes"
    client.responses.create.return_value = SimpleNamespace(output=[], output_text="maybe")
    assert await service.parse_confirmation_response("maybe") == "unclear"
    client.responses.create.side_effect = RuntimeError("confirm parse")
    assert await service.parse_confirmation_response("?") == "unclear"

    client.responses.create.side_effect = None
    confirmation_file.read.return_value = '{"name": "tool"}'
    client.responses.create.return_value = function_response({"registration_type": "buyer", "confidence": 90})
    assert (await service.detect_registration_type("buy"))["registration_type"] == "buyer"
    client.responses.create.return_value = empty_response()
    assert not (await service.detect_registration_type("?"))["success"]
    client.responses.create.side_effect = RuntimeError("profile")
    assert not (await service.detect_registration_type("?"))["success"]

    obj = {"d": datetime(2024, 1, 2), "e": SimpleNamespace(value="x"), "f": 10.0, "g": {"bad": object()}, "a": [{"attachments": [{"file_content": "secret", "name": "x"}]}]}
    cleaned = service._clean_for_json_serialization(obj)
    assert cleaned["d"] == "2024-01-02T00:00:00" and cleaned["f"] == 10 and isinstance(cleaned["g"]["bad"], str)
    stripped = service._strip_base64_from_entities(obj)
    assert stripped["a"][0]["attachments"][0]["file_content"] == "[base64_data]"


@pytest.fixture
def entity_service():
    api = MagicMock()
    api.extract_entities = AsyncMock()
    api.extract_registration_entities = AsyncMock()
    api.validate_delivery_date = AsyncMock()
    api.extract_entities_with_summary_context = AsyncMock()
    api.merge_resolved_references_with_entities = AsyncMock()
    api.extract_historical_options = MagicMock()
    return entity_module.EntityService(api), api


@pytest.mark.asyncio
async def test_entity_constructor_routing_registration_and_error(entity_service):
    service, api = entity_service
    assert service.openai_service is api
    api.extract_entities.return_value = {"entities": {"description": "bolt"}, "confidence": 80, "success": True}
    result = await service.extract_entities("bolt")
    assert result["entities"]["description"] == "bolt"
    api.extract_registration_entities.return_value = {"entities": {"email": "a@x.com"}, "confidence": 70, "success": True, "extracted_fields": ["email"], "reasoning": "clear"}
    registration = await service._handle_registration_extraction("a@x.com", {"conversation_history": "old", "registration_entities": {"name": "A"}}, "buyer_registration")
    assert registration["entities"]["email"] == "a@x.com"
    api.extract_entities.side_effect = RuntimeError("extract")
    assert (await service.extract_entities("x"))["success"] is False
    api.extract_entities.side_effect = None
    service._handle_reference_extraction = AsyncMock(return_value={"reference": True})
    reference_context = {"intent_result": {"intent": "reference_request", "confidence": 90, "reasoning": "history", "context_analysis": {"reference_details": {"reference_type": "address", "has_history": True}}}}
    assert await service.extract_entities("usual", reference_context) == {"reference": True}


@pytest.mark.asyncio
async def test_entity_standard_extraction_multi_legacy_validation_and_guards(entity_service, monkeypatch):
    service, api = entity_service
    future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    api.extract_entities.return_value = {"products": [{"description": "bolt", "quantity": 2, "pincode": "411005"}], "deliveryDate": future, "state": "old", "city": "old", "pincode": "411005", "confidence": 88, "success": True}
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": future}
    monkeypatch.setattr(entity_module, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "MH"}))
    context = {"workflow_state": {"global_supplementary_fields": {"state": "old"}, "incomplete_products": [{"entities": {"description": "bolt", "quantity": 1}}]}}
    result = await service.extract_entities("more bolts", context)
    assert result["products"][0]["city"] == "Pune" and result["state"] == "MH"
    api.extract_entities.return_value = {"entities": {"description": "bolt", "deliveryDate": future}, "confidence": 50, "success": True}
    assert (await service.extract_entities("old format"))["entities"]["deliveryDate"] == future
    api.validate_delivery_date.return_value = {"is_valid": False, "user_friendly_message": "invalid"}
    legacy = await service.extract_entities("bad date")
    assert legacy["date_validation_error"] and legacy["entities"]["deliveryDate"] is None

    api.extract_entities.return_value = {"products": [{"description": "NON_PROCURABLE", "remarks": "weapon"}], "confidence": 50}
    non = await service.extract_entities("weapon")
    assert non["error_type"] == "non_procurable"
    api.extract_entities.return_value = {"products": [{"description": "bulk", "quantity": 10000000001}], "confidence": 50}
    quantity = await service.extract_entities("bulk")
    assert quantity["error_type"] == "quantity_limit"


@pytest.mark.asyncio
async def test_entity_modification_operations_and_formatting(entity_service, monkeypatch):
    service, api = entity_service
    existing = [{"entities": {"description": "bolt", "quantity": 2, "city": "Pune", "state": "MH", "unitofMeasures": "kg", "date_validation_error": "old"}}, {"entities": {"description": "nut", "quantity": 1, "city": "Pune", "state": "MH", "unitofMeasures": "kg"}}]
    formatted = service._format_existing_products_for_prompt(existing)
    assert "Index 0: bolt" in formatted and "Location: Pune, MH" in formatted
    modified = service._apply_modifications_to_existing_products(existing, [
        {"operation_type": "modify", "target_product_index": 0, "new_quantity": 5, "delivery_date": "2030-01-01"},
        {"operation_type": "add", "target_product_description": "washer", "new_description": "washer", "new_quantity": 3},
        {"operation_type": "remove", "target_product_index": 1},
        {"operation_type": "unknown", "target_product_index": 0},
        {"operation_type": "modify", "target_product_index": 99, "new_quantity": 7},
    ], "change", {"city": "Mumbai"})
    assert len(modified) == 2 and modified[0]["quantity"] == 5 and modified[0]["city"] == "Mumbai"
    assert modified[1]["description"] == "washer" and modified[1]["unitofMeasures"] == "kg"

    api.extract_entities.return_value = {"is_modification_extraction": True, "has_new_values": True, "modifications": [{"operation_type": "modify", "target_product_index": 0, "new_quantity": 4}], "confidence": 90}
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": None}
    service._auto_fill_location_from_pincode = AsyncMock(side_effect=lambda products: products)
    result = await service.extract_entities("change quantity", {"workflow_state": {"pending_rfq": existing[0]}}, "modification_request")
    assert result["is_modification"] and result["products"][0]["quantity"] == 4

    api.extract_entities.return_value = {"is_modification_extraction": True, "has_new_values": False, "modifications": [], "modification_intent": "city", "confidence": 60}
    clarification = await service.extract_entities("change city", {"workflow_state": {"pending_rfq": existing[0]}}, "modification_request")
    assert clarification["requires_clarification"]
    api.extract_entities.return_value = {"products": [{"quantity": 4}], "confidence": 70}
    old_format = await service.extract_entities("qty", {"workflow_state": {"pending_rfq": existing[0]}}, "modification_request")
    assert old_format["is_modification"]
    api.extract_entities.return_value = {"products": [], "confidence": 70}
    assert (await service.extract_entities("none", {"workflow_state": {"pending_rfq": existing[0]}}, "modification_request"))["requires_clarification"]

    api.extract_entities.return_value = {"entities": {"description": "fallback"}, "confidence": 50}
    assert (await service.extract_entities("no pending", {"workflow_state": {}}, "modification_request"))["entities"]["description"] == "fallback"


@pytest.mark.asyncio
async def test_entity_merge_date_reference_and_location_helpers(entity_service, monkeypatch):
    service, api = entity_service
    today = datetime.now().date()
    future = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": future}
    products, error = await service._validate_dates_in_products([{"description": "a", "deliveryDate": future}, {"description": "b", "deliveryDate": future}], "x")
    assert not error and api.validate_delivery_date.await_count == 1 and products[0]["deliveryDate"] == future
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": (today - timedelta(days=1)).strftime("%Y-%m-%d")}
    products, error = await service._validate_dates_in_products([{"deliveryDate": future}], "x")
    assert error and products[0]["deliveryDate"] is None
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": "not-a-date"}
    products, error = await service._validate_dates_in_products([{"deliveryDate": future}], "x")
    assert error
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": future, "validation_issues": ["warning"]}
    one, error = await service._validate_date_in_entity({"deliveryDate": future, "description": "x"}, "x")
    assert error and one["deliveryDate"] is None

    assert service._extract_common_fields_from_products([]) == {}
    assert service._extract_common_fields_from_products([{"city": "Pune"}, {"city": "Pune"}]) == {"city": "Pune"}
    assert service._has_meaningful_modification_values([{"remarks": "change", "quantity": None}], "x") is False
    assert service._has_meaningful_modification_values([{"remarks": "change", "quantity": "4"}], "x") is True
    assert service._is_date_future_or_today(future) and not service._is_date_future_or_today("bad")

    base = [{"description": "bolt", "quantity": 1, "unitofMeasures": "kg"}]
    assert service._merge_global_fields_into_products(base, {"city": "Pune", "description": "new"})[0]["city"] == "Pune"
    merged = service._merge_new_extraction_with_existing_products(base, [{"description": "bolt", "quantity": 2, "unitofMeasures": "unit(s)"}], "x")
    assert merged[0]["quantity"] == 2 and merged[0]["unitofMeasures"] == "kg"
    merged = service._merge_new_extraction_with_existing_products(base, [{"description": None, "city": "Pune"}], "x")
    assert merged[0]["city"] == "Pune"
    assert len(service._merge_new_extraction_with_existing_products(base, [{"description": "washer"}], "x")) == 2
    applied = service._apply_supplementary_data_to_existing_products([{ "description": "x", "city": "old"}], {"city": "new", "state": "MH"})
    assert applied[0]["city"] == "old" and applied[0]["state"] == "MH"

    monkeypatch.setattr(entity_module, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "MH"}))
    located = await service._auto_fill_location_from_pincode([{ "pincode": "411005", "city": "old"}, {"pincode": "411005"}])
    assert all(p["city"] == "Pune" for p in located)
    invalid = await service._auto_fill_location_from_pincode([{ "pincode": "bad"}])
    assert invalid[0]["pincode"] is None
    monkeypatch.setattr(entity_module, "get_location_from_pincode_async", AsyncMock(return_value=None))
    missing = await service._auto_fill_location_from_pincode([{ "pincode": "411005"}])
    assert missing[0]["pincode"] is None
    assert await service._auto_fill_location_from_pincode([]) == []

    assert service._detect_non_procurable_items([{ "description": "NON_PROCURABLE", "remarks": "item"}]) == ["item"]
    assert service._check_quantity_limits([{ "description": "x", "quantity": "bad"}, {"description": "x", "quantity": 2}]) == []
    assert service._check_quantity_limits([{ "description": "x", "quantity": 10000000001}])[0]["max_allowed"] == 10000000000
    assert service._clean_invalid_descriptions([{ "description": "kg"}, {"description": "123"}, {"description": "bolt"}])[0]["description"] is None

    schema = {"type": "object"}
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: FakeFile(json.dumps(schema)))
    assert service._get_schema("buy_something") == schema and service._get_schema("other") == {}
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    assert service._get_schema("buy_something") == {}


@pytest.mark.asyncio
async def test_entity_reference_summary_and_reference_merge_fallbacks(entity_service):
    service, api = entity_service
    context = {"user_context": {"chat_history": [{"summary": "old"}]}, "workflow_state": {}}
    api.extract_historical_options.return_value = {"success": True, "options": [{"city": "Pune"}], "summary": "found", "recommendations": {}}
    result = await service._handle_reference_extraction("usual", context, {"reference_type": "address", "confidence": 80, "detected_phrases": ["usual"]})
    assert result["requires_user_selection"] and result["historical_options"]
    api.extract_historical_options.return_value = {"success": False}
    service._handle_standard_extraction = AsyncMock(return_value={"standard": True})
    assert await service._handle_reference_extraction("usual", context, {"reference_type": "address"}) == {"standard": True}
    assert await service._handle_reference_extraction("usual", {"user_context": {}}, {"reference_type": "address"}) == {"standard": True}

    api.extract_entities_with_summary_context.return_value = {"products": [{"description": "x"}], "resolved_references": [{"reference_phrase": "usual", "resolved_field": "city", "resolved_value": "Pune"}], "confidence": 80}
    api.merge_resolved_references_with_entities.return_value = {"success": True, "updated_products": [{"description": "x", "city": "Pune"}]}
    api.validate_delivery_date.return_value = {"is_valid": True, "normalized_date": None}
    service._auto_fill_location_from_pincode = AsyncMock(side_effect=lambda p: p)
    summary = await service.extract_entities_with_summary_context("usual", {"chat_summaries": [{"summary": "old"}]})
    assert summary["products"][0]["city"] == "Pune"
    assert await service.extract_entities_with_summary_context("x", {}) == await service._handle_standard_extraction("x", {}, "buy_something")
    service._handle_standard_extraction = AsyncMock(return_value={"standard": True})
    api.extract_entities_with_summary_context.side_effect = RuntimeError("summary")
    assert await service.extract_entities_with_summary_context("x", {"chat_summaries": [{"summary": "old"}]}) == {"standard": True}

    assert await service._apply_resolved_references_intelligently([], [], "x") == []
    api.merge_resolved_references_with_entities.return_value = {"success": False}
    products = [{"description": "x"}]
    assert await service._apply_resolved_references_intelligently(products, [{"x": 1}], "x") == products
    api.merge_resolved_references_with_entities.side_effect = RuntimeError("merge")
    assert await service._apply_resolved_references_intelligently(products, [{"x": 1}], "x") == products
