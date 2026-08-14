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

import app.services.error_notification_service as error_module
import app.services.intent_service as intent_module
import app.services.openai_service as openai_module
from app.services.whatsapp_service import MessageResponse
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
