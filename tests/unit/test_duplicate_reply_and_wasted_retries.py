"""
Regression tests for the duplicate outbound WhatsApp message.

Traced from a production incident on 2026-08-14 where one button click produced
two messages: a "Currently, we are facing some technical issues" notice followed
by the correct reply. For a single `confirm_date_location` click the log showed

    openai_service  | ERROR | Intent classification failed: Unterminated string...
    cancel_service  | INFO  | Sending cancellation with buyer buttons to 9198...   <- message 1
    chat_service    | INFO  | [SECTIONED_RFQ] Button click detected: confirm_...
    whatsapp_service| DEBUG | Sending buttons ... 'RFQ Items (1)' ...              <- message 2

The cause is that `_get_fallback_classification` messaged the user as a side
effect of *classifying*, while the caller carried on and produced its own reply.

The same log also showed ~7s per failure burned on a health check that sent to
the literal recipient "test_number" and was retried four times, so those paths
are covered here too.

Every external collaborator is mocked; nothing here touches WhatsApp, OpenAI,
Redis or a database.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.webhook as webhook_module
import app.services.error_notification_service as error_module
import app.services.global_error_handler as geh_module
import app.services.intent_service as intent_module
import app.services.openai_service as openai_module
import app.services.whatsapp_service as wa_module
from app.services.whatsapp_service import MessageResponse, reply_sent_key
from app.tools.retry_service import RetryService


# ---------------------------------------------------------------------------
# The classification fallback must not message the user
# ---------------------------------------------------------------------------


@pytest.fixture
def sent_messages(monkeypatch):
    """Capture anything the fallback tries to push to the user."""
    sends = []

    async def record(*args, **kwargs):
        sends.append((args, kwargs))
        return True

    monkeypatch.setattr(
        "app.services.cancel_service.CancelService",
        lambda *a, **k: SimpleNamespace(_send_cancellation_message=record),
    )
    return sends


@pytest.fixture
def openai_service(monkeypatch):
    settings = SimpleNamespace(
        openai_model_default="model", openai_model_advanced="advanced",
        support_email="support@example.test", support_contact_info="help@example.test",
    )
    monkeypatch.setattr(openai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_module, "get_interaction_logger", lambda: MagicMock())
    return openai_module.OpenAIService()


@pytest.fixture
def intent_service(monkeypatch):
    monkeypatch.setattr(intent_module, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(
        intent_module, "get_settings",
        lambda: SimpleNamespace(support_contact_info="help@example.test"),
    )
    return intent_module.IntentService()


@pytest.mark.asyncio
async def test_openai_fallback_classifies_without_messaging_the_user(openai_service, sent_messages):
    result = await openai_service._get_fallback_classification(
        "Confirm", {"user_role": "buyer"}, error="truncated JSON", user_phone="919808494950"
    )

    assert sent_messages == [], "classifying must not produce an outbound message"
    assert result["success"] is False
    assert result["intent"] == "general_inquiry"


@pytest.mark.asyncio
async def test_intent_fallback_classifies_without_messaging_the_user(intent_service, sent_messages):
    result = await intent_service._get_fallback_classification(
        "Confirm", {"user_role": "buyer"}, error="OpenAI service returned None",
        user_phone="919808494950",
    )

    assert sent_messages == []
    assert result["success"] is False
    assert result["fallback_used"] is True


@pytest.mark.asyncio
async def test_one_failed_classification_yields_no_outbound_message(intent_service, sent_messages):
    """
    End to end for the incident: OpenAI returns None, and the classification
    layer emits nothing to the user. The caller remains the only sender, so the
    user sees exactly one reply per inbound message.
    """
    intent_service.openai_service.classify_intent = AsyncMock(return_value=None)

    result = await intent_service.classify_intent(
        "Confirm", {"user_role": "buyer"}, user_phone="919808494950"
    )

    assert sent_messages == []
    assert isinstance(result, dict)
    assert result["success"] is False


@pytest.mark.asyncio
async def test_fallback_stays_silent_for_every_role_and_context_shape(openai_service, intent_service, sent_messages):
    """The old code keyed the send off context['user_role']; no shape may send now."""
    for context in ({"user_role": "buyer"}, {"user_role": "seller"}, {}, None):
        await openai_service._get_fallback_classification("x", context, user_phone="+1")
        await intent_service._get_fallback_classification("x", context, user_phone="+1")

    assert sent_messages == []


# ---------------------------------------------------------------------------
# The WhatsApp health check must not send a probe message
# ---------------------------------------------------------------------------


def _error_service(*, mock_mode=False, configured=True):
    service = error_module.ErrorNotificationService.__new__(error_module.ErrorNotificationService)
    service.settings = SimpleNamespace(support_team_numbers=["1"], support_email="s@example.test")
    service.whatsapp_service = SimpleNamespace(
        mock_mode=mock_mode,
        base_url="https://wa.example.test" if configured else "",
        username="u", password="p", from_number="f",
        send_message=AsyncMock(
            return_value=MessageResponse(success=False, error="Invalid phone number format: test_number")
        ),
    )
    service.email_service = MagicMock()
    service._service_status_cache = {}
    service._cache_expiry = timedelta(minutes=2)
    return service


@pytest.mark.asyncio
async def test_whatsapp_health_check_sends_nothing():
    """
    The probe used to send to the literal recipient "test_number", which failed
    validation, was retried four times with 1+2+4s of backoff, and left the
    channel permanently marked unavailable.
    """
    service = _error_service()

    assert await service._check_whatsapp_health() is True
    service.whatsapp_service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_whatsapp_health_check_reports_misconfiguration(caplog):
    service = _error_service(configured=False)

    with caplog.at_level(logging.WARNING, logger="app.services.error_notification_service"):
        assert await service._check_whatsapp_health() is False

    assert "not fully configured" in caplog.text
    service.whatsapp_service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_whatsapp_health_check_passes_in_mock_mode_and_caches():
    service = _error_service(mock_mode=True, configured=False)

    assert await service._check_whatsapp_health() is True
    # Second call is served from cache.
    assert await service._check_whatsapp_health() is True
    assert service._service_status_cache["whatsapp_health"]["status"] is True


@pytest.mark.asyncio
async def test_whatsapp_health_check_survives_a_broken_client(caplog):
    service = _error_service()
    service.whatsapp_service = None  # attribute access raises inside the check

    with caplog.at_level(logging.ERROR, logger="app.services.error_notification_service"):
        assert await service._check_whatsapp_health() is False

    assert "WhatsApp health check failed" in caplog.text


# ---------------------------------------------------------------------------
# Permanent failures must not consume the retry budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(monkeypatch):
    """An invalid recipient burned 4 attempts and ~7s of backoff per send."""
    sleep = AsyncMock()
    monkeypatch.setattr("app.tools.retry_service.asyncio.sleep", sleep)
    service = RetryService(max_retries=3, initial_delay=1.0)

    attempt = AsyncMock(
        return_value=MessageResponse(
            success=False, error="Invalid phone number format: test_number", retryable=False
        )
    )
    result = await service.retry_with_backoff(attempt)

    assert result["success"] is False
    assert result["attempts"] == 1
    assert attempt.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_failure_still_uses_the_full_budget(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.tools.retry_service.asyncio.sleep", sleep)
    service = RetryService(max_retries=2, initial_delay=1.0)

    attempt = AsyncMock(return_value=MessageResponse(success=False, error="gateway timeout"))
    result = await service.retry_with_backoff(attempt)

    assert result["success"] is False
    assert result["attempts"] == 3
    assert attempt.await_count == 3


@pytest.mark.asyncio
async def test_dict_results_can_opt_out_of_retrying(monkeypatch):
    monkeypatch.setattr("app.tools.retry_service.asyncio.sleep", AsyncMock())
    service = RetryService(max_retries=3, initial_delay=1.0)

    permanent = AsyncMock(return_value={"success": False, "retryable": False})
    assert (await service.retry_with_backoff(permanent))["attempts"] == 1

    # Absent key keeps the existing retrying behaviour for current callers.
    silent = AsyncMock(return_value={"success": False})
    assert (await service.retry_with_backoff(silent))["attempts"] == 4


def test_message_response_defaults_to_retryable():
    assert MessageResponse(success=False, error="boom").retryable is True
    assert RetryService._is_retryable(SimpleNamespace(success=False)) is True


# ---------------------------------------------------------------------------
# An error notice must never follow a reply the user already received
# ---------------------------------------------------------------------------
#
# Second incident shape from the same 2026-08-14 logs. A handler sends its reply
# mid-flow, then the rest of process_message keeps running (session persistence,
# cache refresh, history writes, ExitService). An exception in that tail reached
# the outer `except`, which called handle_technical_failure unconditionally, so
# the user got the answer followed by "Currently, we are facing some technical
# issues". The webhook-level handler stacked a second notice on top of that.


class _FakeRedis:
    """Minimal async Redis stand-in shared across the services under test."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def get(self, key, as_json=False):
        return self.store.get(key)

    async def delete(self, key):
        return self.store.pop(key, None) is not None

    async def exists(self, key):
        return key in self.store


def _whatsapp_service(monkeypatch, redis):
    monkeypatch.setattr(wa_module, "get_redis_service", lambda: redis)
    monkeypatch.setattr(
        wa_module, "get_settings",
        lambda: SimpleNamespace(pending_reply_ttl_seconds=180),
    )
    return wa_module.WhatsAppService.__new__(wa_module.WhatsAppService)


def _error_handler(monkeypatch, redis):
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis)
    handler = geh_module.GlobalErrorHandler.__new__(geh_module.GlobalErrorHandler)
    handler.whatsapp_service = AsyncMock()
    handler.user_error_message = "Currently, we are facing some technical issues."
    return handler


def test_reply_marker_key_matches_the_webhook_phone_format():
    assert reply_sent_key("+919808494950") == "919808494950:reply_sent"
    assert reply_sent_key("919808494950") == "919808494950:reply_sent"


@pytest.mark.asyncio
async def test_a_delivered_reply_silences_the_late_error_notice(monkeypatch):
    """The incident end to end: one reply out, then the tail fails."""
    redis = _FakeRedis()
    service = _whatsapp_service(monkeypatch, redis)
    handler = _error_handler(monkeypatch, redis)

    await service._mark_reply_sent("+919808494950")
    await handler._send_user_response("+919808494950")

    handler.whatsapp_service.send_message.assert_not_awaited()

    # A second failure in the same turn stays quiet too, which is what stops the
    # webhook-level handler from stacking a notice on the chat-level one.
    await handler._send_user_response("919808494950")
    handler.whatsapp_service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failure_before_any_reply_still_notifies(monkeypatch):
    redis = _FakeRedis()
    handler = _error_handler(monkeypatch, redis)

    await handler._send_user_response("+919808494950")

    handler.whatsapp_service.send_message.assert_awaited_once()
    assert handler.whatsapp_service.send_message.await_args.kwargs["skip_concatenation"] is True


@pytest.mark.asyncio
async def test_the_next_inbound_message_reopens_the_turn(monkeypatch):
    """Clearing on ingress keeps genuine failures on later turns visible."""
    redis = _FakeRedis()
    redis.store["919808494950:reply_sent"] = "1"
    monkeypatch.setattr(webhook_module, "get_redis_service", lambda: redis)
    monkeypatch.setattr(
        webhook_module, "get_settings",
        lambda: SimpleNamespace(pending_reply_ttl_seconds=180),
    )
    monkeypatch.setattr(webhook_module, "timeout_service", AsyncMock())
    monkeypatch.setattr(webhook_module, "message_queue_service", AsyncMock())

    await webhook_module.enqueue_message_async({"from": "+919808494950", "content": "hi"})

    assert "919808494950:reply_sent" not in redis.store
    assert redis.store["919808494950:pending_reply"] == "1"

    handler = _error_handler(monkeypatch, redis)
    await handler._send_user_response("+919808494950")
    handler.whatsapp_service.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreachable_redis_keeps_the_notice_rather_than_going_silent(monkeypatch):
    """Fail open: a duplicate beats silence when something is actually broken."""
    redis = _FakeRedis()
    handler = _error_handler(monkeypatch, redis)

    async def boom(_key):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "exists", boom)

    assert await handler._reply_already_delivered("+1") is False
    await handler._send_user_response("+1")
    handler.whatsapp_service.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_marking_a_reply_never_breaks_the_send(monkeypatch):
    redis = _FakeRedis()
    service = _whatsapp_service(monkeypatch, redis)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "set", boom)

    await service._mark_reply_sent("+919808494950")  # must not raise


@pytest.mark.asyncio
async def test_successful_send_records_the_marker_with_a_ttl(monkeypatch):
    redis = AsyncMock()
    service = _whatsapp_service(monkeypatch, redis)
    service._format_phone_number = MagicMock(return_value="919808494950")
    service._track_message_in_history = AsyncMock()
    service.retry_service = SimpleNamespace(
        retry_with_backoff=AsyncMock(
            return_value={"success": True, "result": MessageResponse(True, "m"), "attempts": 1, "error": None}
        )
    )

    assert (await service.send_message("+919808494950", "your RFQ is live")).success

    redis.set.assert_awaited_once_with("919808494950:reply_sent", "1", ex=180)
    redis.delete.assert_awaited_once_with("919808494950:pending_reply")


@pytest.mark.asyncio
async def test_acknowledgements_do_not_close_the_turn(monkeypatch):
    """clear_pending_reply=False means "not the real answer", so no marker."""
    redis = AsyncMock()
    service = _whatsapp_service(monkeypatch, redis)
    service._format_phone_number = MagicMock(return_value="919808494950")
    service._track_message_in_history = AsyncMock()
    service.retry_service = SimpleNamespace(
        retry_with_backoff=AsyncMock(
            return_value={"success": True, "result": MessageResponse(True, "m"), "attempts": 1, "error": None}
        )
    )

    await service.send_message("+919808494950", "please wait", clear_pending_reply=False)

    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_technical_error_notifies_the_user_once(monkeypatch):
    """
    handle_technical_error_with_cancel used to send its own notice *and* call
    handle_technical_failure, so one failure produced two bubbles. The first also
    claimed "your current session has been cleared" while clearing nothing.
    """
    failure = AsyncMock()
    monkeypatch.setattr("app.utils.technical_failure_handler.handle_technical_failure", failure)
    constructed = []
    monkeypatch.setattr(
        wa_module, "WhatsAppService",
        lambda *a, **k: constructed.append(1) or MagicMock(),
    )

    await webhook_module.handle_technical_error_with_cancel("919808494950", "boom", "Critical Webhook Error")

    failure.assert_awaited_once()
    assert failure.await_args.kwargs["user_phone"] == "919808494950"
    assert constructed == [], "the webhook must not send a second error message of its own"


@pytest.mark.asyncio
async def test_webhook_technical_error_survives_a_failing_notifier(monkeypatch):
    monkeypatch.setattr(
        "app.utils.technical_failure_handler.handle_technical_failure",
        AsyncMock(side_effect=RuntimeError("notifier down")),
    )

    await webhook_module.handle_technical_error_with_cancel("919808494950", "boom")  # must not raise
