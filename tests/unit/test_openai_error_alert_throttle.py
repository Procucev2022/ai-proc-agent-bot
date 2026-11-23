"""OpenAI error alerts reach support once per cooldown, not once per failed call."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import openai_service as module
from app.services.openai_service import OpenAIService


def make_service(**settings):
    service = OpenAIService.__new__(OpenAIService)
    defaults = {"enable_error_notifications": True, "openai_error_alert_cooldown_minutes": 60}
    service.settings = SimpleNamespace(**{**defaults, **settings})
    notifier = MagicMock(notify_general_error=AsyncMock())
    service._error_notification_service = notifier
    return service, notifier


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_repeats_within_cooldown_are_suppressed_then_counted(clock):
    service, notifier = make_service()
    for _ in range(5):
        await service._notify_openai_error("API Error", "Connection error.", "classify_intent")
    notifier.notify_general_error.assert_awaited_once()
    assert notifier.notify_general_error.await_args.args[0]["message"] == "classify_intent: Connection error."

    clock[0] += 3600
    await service._notify_openai_error("API Error", "Connection error.", "classify_intent")
    assert notifier.notify_general_error.await_count == 2
    message = notifier.notify_general_error.await_args.args[0]["message"]
    assert message == "classify_intent: Connection error. (4 more since the last alert)"


@pytest.mark.asyncio
async def test_each_method_and_error_type_alerts_separately(clock):
    service, notifier = make_service()
    await service._notify_openai_error("API Error", "down", "classify_intent")
    await service._notify_openai_error("API Error", "down", "extract_entities")
    await service._notify_openai_error("Unexpected Error", "down", "classify_intent")
    assert notifier.notify_general_error.await_count == 3


@pytest.mark.asyncio
async def test_disabled_notifications_send_nothing(clock):
    service, notifier = make_service(enable_error_notifications=False)
    await service._notify_openai_error("API Error", "down", "classify_intent")
    notifier.notify_general_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_notifier_failure_is_swallowed_and_still_throttles(clock):
    service, notifier = make_service()
    notifier.notify_general_error.side_effect = RuntimeError("mail quota")
    await service._notify_openai_error("API Error", "down", "classify_intent")
    await service._notify_openai_error("API Error", "down", "classify_intent")
    notifier.notify_general_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_without_settings_uses_defaults(clock):
    service, notifier = make_service()
    del service.settings
    await service._notify_openai_error("API Error", "down", "classify_intent")
    notifier.notify_general_error.assert_awaited_once()


def test_failure_alert_goes_to_configured_support_email():
    template = json.loads(Path("app/email_templates/api_service_failure.json").read_text(encoding="utf-8"))
    assert template["to"] == "{support_email}"
