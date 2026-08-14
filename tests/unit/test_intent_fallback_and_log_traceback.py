"""
Regression tests for the intent-classification failure chain and for traceback
preservation in the log formatter.

Traced from a production incident where the message
"2 Laptop,  delivery tomorrow, pincode: 251002" was classified as a
zero-confidence greeting and the user's RFQ was dropped:

1. openai_service.classify_intent capped output at 100 tokens, so the
   function-call JSON was truncated and json.loads() raised.
2. Both _get_fallback_classification implementations fell off the end of the
   function and returned None.
3. intent_service returned the fallback coroutine without awaiting it.
4. chat_service caught the resulting AttributeError and rebound the intent to
   "greeting", which routes into the irrelevant-message flow.

Every external collaborator is mocked; nothing here touches OpenAI, Redis or a
database.
"""

from __future__ import annotations

import builtins
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_module
import app.services.intent_service as intent_module
import app.services.openai_service as openai_module
from app.utils import logging_utils


# ---------------------------------------------------------------------------
# CustomFormatter: exception and stack text must survive formatting
# ---------------------------------------------------------------------------


def _record(message: str = "boom", *, exc_info=None, stack_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        "app.api.webhook", logging.ERROR, __file__, 1, message, (), exc_info,
        sinfo=stack_info,
    )


def test_formatter_appends_exception_traceback():
    """logger.error(..., exc_info=True) must not lose its traceback."""
    logging_utils.clear_user_phone_context()
    try:
        raise ValueError("unterminated string")
    except ValueError:
        record = _record("Critical error processing webhook: ", exc_info=sys.exc_info())

    formatted = logging_utils.CustomFormatter().format(record)

    assert "Critical error processing webhook:" in formatted
    assert "Traceback (most recent call last):" in formatted
    assert "ValueError: unterminated string" in formatted
    # The single-line prefix contract is unchanged.
    assert formatted.splitlines()[0].endswith("Critical error processing webhook: ")
    assert "app.api.webhook | ERROR" in formatted.splitlines()[0]


def test_formatter_reuses_cached_exception_text_and_appends_stack():
    """exc_text is computed once, and stack_info is appended like the stdlib does."""
    logging_utils.clear_user_phone_context()
    formatter = logging_utils.CustomFormatter()

    try:
        raise KeyError("from")
    except KeyError:
        record = _record(exc_info=sys.exc_info())

    first = formatter.format(record)
    assert record.exc_text  # cached on the record by the first pass
    record.exc_text = "CACHED-EXCEPTION-TEXT"
    second = formatter.format(record)

    assert "KeyError" in first
    assert "CACHED-EXCEPTION-TEXT" in second
    assert "Traceback" not in second

    stacked = formatter.format(_record("with stack", stack_info="Stack (most recent call last):\n  frame"))
    assert "Stack (most recent call last):" in stacked


def test_formatter_without_exception_is_a_single_line():
    logging_utils.clear_user_phone_context()
    formatted = logging_utils.CustomFormatter().format(_record("plain message"))
    assert formatted.count("\n") == 0
    assert formatted.endswith("plain message")


# ---------------------------------------------------------------------------
# openai_service: token ceiling, truncation reporting, real fallback payload
# ---------------------------------------------------------------------------


class _Tool:
    """Minimal context-manager stand-in for the tool definition file."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return '{"name": "classify_intent"}'


@pytest.fixture
def openai_service(monkeypatch):
    settings = SimpleNamespace(
        openai_model_default="model", openai_model_advanced="advanced",
        support_email="support@example.test", support_contact_info="help@example.test",
    )
    monkeypatch.setattr(openai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_module, "get_interaction_logger", lambda: MagicMock())
    service = openai_module.OpenAIService()
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()))
    service._client = client
    monkeypatch.setattr(service, "_load_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(builtins, "open", lambda *a, **k: _Tool())
    return service, client


def _api_response(args, *, status=None, incomplete_details=None):
    payload = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", arguments=json.dumps(args))],
        usage=SimpleNamespace(
            input_tokens=7265, output_tokens=100,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )
    if status is not None:
        payload.status = status
    if incomplete_details is not None:
        payload.incomplete_details = incomplete_details
    return payload


@pytest.mark.asyncio
async def test_classify_intent_requests_enough_tokens_for_the_full_schema(openai_service):
    """
    The 100-token cap truncated the function-call arguments mid-string. The
    schema marks all_intent_scores, context_analysis and reasoning required, so
    a complete payload needs several hundred tokens.
    """
    service, client = openai_service
    client.responses.create.return_value = _api_response(
        {"intent": "buy_something", "confidence": 95, "reasoning": "purchase request"}
    )

    result = await service.classify_intent("2 Laptop,  delivery tomorrow, pincode: 251002")

    assert result["success"] is True
    assert result["intent"] == "buy_something"
    requested = client.responses.create.await_args.kwargs["max_output_tokens"]
    assert requested == openai_module.INTENT_CLASSIFICATION_MAX_OUTPUT_TOKENS
    assert requested >= 400, "must leave room for all_intent_scores plus context_analysis"


@pytest.mark.asyncio
async def test_truncated_json_no_longer_resolves_to_none(openai_service, monkeypatch):
    """
    A truncated payload still fails to parse, but the fallback now yields a
    usable classification instead of None.
    """
    service, client = openai_service
    service._notify_openai_error = AsyncMock()
    client.responses.create.return_value = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", arguments='{"intent": "buy_someth')],
        usage=None,
    )

    result = await service.classify_intent("2 Laptop, pincode: 251002", {})

    assert result is not None
    assert result["success"] is False
    assert isinstance(result["intent"], str)


def test_log_incomplete_response_reports_token_ceiling(caplog):
    """Truncation is logged explicitly instead of only surfacing as a parse error."""
    truncated = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.openai_service"):
        assert openai_module.OpenAIService._log_incomplete_response(truncated, "intent classification") is True
    assert "truncated" in caplog.text
    assert "max_output_tokens" in caplog.text


def test_log_incomplete_response_handles_missing_and_complete_shapes(caplog):
    """A complete response, and one predating these fields, must stay silent."""
    complete = SimpleNamespace(status="completed", incomplete_details=None)
    bare = SimpleNamespace()

    with caplog.at_level(logging.WARNING, logger="app.services.openai_service"):
        assert openai_module.OpenAIService._log_incomplete_response(complete, "intent classification") is False
        assert openai_module.OpenAIService._log_incomplete_response(bare, "intent classification") is False
    assert caplog.text == ""

    # incomplete_details present without a nested reason still reports.
    with caplog.at_level(logging.WARNING, logger="app.services.openai_service"):
        assert openai_module.OpenAIService._log_incomplete_response(
            SimpleNamespace(incomplete_details="content_filter"), "entity extraction"
        ) is True
    assert "content_filter" in caplog.text


@pytest.mark.asyncio
async def test_classify_intent_logs_truncation_from_live_response(openai_service, caplog):
    service, client = openai_service
    client.responses.create.return_value = _api_response(
        {"intent": "buy_something", "confidence": 90},
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    with caplog.at_level(logging.WARNING, logger="app.services.openai_service"):
        await service.classify_intent("buy laptops")
    assert "was truncated" in caplog.text


# ---------------------------------------------------------------------------
# intent_service: the fallback is awaited and returns a real classification
# ---------------------------------------------------------------------------


def _intent_service(monkeypatch, classify_return):
    ai = MagicMock()
    ai.classify_intent = AsyncMock(return_value=classify_return)
    monkeypatch.setattr(intent_module, "OpenAIService", lambda: ai)
    monkeypatch.setattr(
        intent_module, "get_settings",
        lambda: SimpleNamespace(support_contact_info="support@example.test"),
    )
    return intent_module.IntentService(), ai


@pytest.mark.asyncio
async def test_none_from_openai_yields_a_dict_not_a_coroutine(monkeypatch):
    """
    The missing await returned a coroutine object, so chat_service's
    `.get("timeout_handled")` raised 'coroutine' object has no attribute 'get'.
    """
    service, _ = _intent_service(monkeypatch, None)

    result = await service.classify_intent(
        "2 Laptop,  delivery tomorrow, pincode: 251002",
        {"user_role": "buyer"},
        user_phone="919808494950",
    )

    assert isinstance(result, dict), "must not leak an un-awaited coroutine"
    # The exact call chat_service makes, which used to raise.
    assert result.get("timeout_handled") is None
    assert result["success"] is False
    assert result["fallback_used"] is True


@pytest.mark.asyncio
async def test_fallback_receives_the_error_description_not_the_phone_number(monkeypatch):
    """
    user_phone used to be passed positionally, landing in the `error` parameter:
    the recorded cause became a phone number and user_phone stayed None. Assert
    each value reaches the parameter it belongs to.
    """
    service, _ = _intent_service(monkeypatch, {"success": False})
    captured = {}

    async def capture(message, context=None, error=None, user_phone=None):
        captured.update(message=message, error=error, user_phone=user_phone)
        return {"intent": "ambiguous", "confidence": 30, "success": False}

    service._get_fallback_classification = capture

    await service.classify_intent("buy 2 laptops", {"user_role": "buyer"}, user_phone="919808494950")

    assert captured["user_phone"] == "919808494950"
    assert captured["error"] == "OpenAI classification unsuccessful"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("plain text", "plain text"),
        ({"text": "from text key"}, "from text key"),
        ({"content": "from content key"}, "from content key"),
        ({"other": 1}, ""),
        ([{"text": "a"}, {"text": "b"}, "skipped"], "a b"),
        ([], ""),
        (None, ""),
        (42, "42"),
    ],
)
def test_message_to_text_handles_every_supported_shape(message, expected):
    assert intent_module.IntentService._message_to_text(message) == expected


# ---------------------------------------------------------------------------
# chat_service: a failed classification must not become a greeting
# ---------------------------------------------------------------------------


def _chat_service(intent_service) -> chat_module.ChatService:
    """Build a ChatService shell: only the lazy intent_service property is needed."""
    service = chat_module.ChatService.__new__(chat_module.ChatService)
    service._intent_service = intent_service
    return service


def test_classification_fallback_uses_keywords_not_greeting():
    """
    "greeting" is in the irrelevant-message tuple, so defaulting to it answered
    real requests with the canned out-of-scope reply and dropped them.
    """
    service = _chat_service(intent_module.IntentService.__new__(intent_module.IntentService))

    result = service._build_classification_fallback("I need to buy 10 laptops")

    assert result["intent"] == "buy_something"
    assert result["intent"] != "greeting"
    assert result["success"] is False
    assert result["fallback_used"] is True


def test_classification_fallback_defaults_to_ambiguous_for_empty_and_broken_input():
    service = _chat_service(intent_module.IntentService.__new__(intent_module.IntentService))

    assert service._build_classification_fallback("   ")["intent"] == "ambiguous"
    assert service._build_classification_fallback("")["confidence"] == 0

    # A mocked-out intent service (or any collaborator returning the wrong shape)
    # must degrade rather than raise inside the error path.
    broken = MagicMock()
    broken._message_to_text.side_effect = RuntimeError("no classifier")
    assert _chat_service(broken)._build_classification_fallback("anything")["intent"] == "ambiguous"


def test_classification_fallback_never_lands_in_the_irrelevant_message_tuple():
    """Guard the routing contract that made this bug user-visible."""
    service = _chat_service(intent_module.IntentService.__new__(intent_module.IntentService))
    irrelevant = ("general_inquiry", "greeting", "support")

    rfq_like = [
        "2 Laptop,  delivery tomorrow, pincode: 251002",
        "I want to buy 5 pumps",
        "need 100 water bottles",
        "purchase order for cables",
    ]
    for message in rfq_like:
        assert service._build_classification_fallback(message)["intent"] not in irrelevant
