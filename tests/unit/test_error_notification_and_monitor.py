from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.error_notification_service import ErrorNotificationService
from app.services.service_monitor import ServiceMonitor


def make_error_service():
    service = ErrorNotificationService.__new__(ErrorNotificationService)
    service.settings = SimpleNamespace(support_team_numbers=["1"], support_email="s@example.com")
    service.whatsapp_service = AsyncMock()
    service.email_service = MagicMock()
    service._service_status_cache = {}
    from datetime import timedelta
    service._cache_expiry = timedelta(minutes=2)
    return service


@pytest.mark.asyncio
async def test_error_notification_routing_and_helpers():
    service = make_error_service()
    service.whatsapp_service.mock_mode = False
    service.whatsapp_service.send_message.return_value = SimpleNamespace(success=True, message_id="m", error=None)
    service.email_service.list_available_templates.return_value = ["x"]
    service.email_service.send_email_by_template = AsyncMock(return_value={"status": "Success"})
    details = {"error_type": "API", "message": "bad", "service": "GMT", "timestamp": "now"}
    assert "API" in service._format_whatsapp_message(details)
    assert service._get_default_recipients()["email"] == ["s@example.com"]
    assert await service._check_whatsapp_health()
    assert await service._check_whatsapp_health()
    assert await service._check_email_health()
    assert await service._check_email_health()
    assert (await service.notify_error("whatsapp_down", details))["email"]["success"]
    assert (await service.notify_error("api_down", details))["whatsapp"]["success"]
    both = await service.notify_error("general_error", details, {"whatsapp": ["1"], "email": ["e"]})
    assert "whatsapp" in both and "email" in both
    assert await service._send_whatsapp_notification(details, ["1"])
    assert await service._send_email_notification("t", details, ["e"])
    assert await service.notify_whatsapp_down(details)
    assert await service.notify_api_down(details)
    assert await service.notify_general_error(details)

    # Missing credentials mark the channel unusable. The health check reaches this
    # verdict from configuration alone; it never sends a probe message.
    service._service_status_cache = {}
    service.whatsapp_service.base_url = ""
    assert not await service._check_whatsapp_health()
    service.whatsapp_service.base_url = "https://wa.example.test"
    service.email_service.list_available_templates.side_effect = RuntimeError("mail")
    service._service_status_cache = {}
    assert not await service._check_email_health()
    service._service_status_cache = {}
    service._check_whatsapp_health = AsyncMock(return_value=False)
    service._check_email_health = AsyncMock(return_value=False)
    assert "error" in await service.notify_error("general_error", details)


@pytest.mark.asyncio
async def test_service_monitor_contexts_and_health(monkeypatch):
    monitor = ServiceMonitor.__new__(ServiceMonitor)
    monitor.settings = SimpleNamespace(enable_error_notifications=True, error_notification_cooldown_minutes=5)
    monitor.notification_service = MagicMock()
    monitor.notification_service.notify_whatsapp_down = AsyncMock()
    monitor.notification_service.notify_api_down = AsyncMock()
    monitor.notification_service.notify_error = AsyncMock(return_value={"ok": True})
    monitor.whatsapp_service = AsyncMock()
    monitor.email_service = MagicMock()
    monitor._last_notification = {}
    from datetime import timedelta
    monitor._cooldown_period = timedelta(minutes=5)
    assert monitor._should_notify("x")
    monitor._update_last_notification("x")
    assert not monitor._should_notify("x")
    monitor.settings.enable_error_notifications = False
    assert not monitor._should_notify("new")
    monitor.settings.enable_error_notifications = True
    async with monitor.monitor_whatsapp_operation("ok"):
        pass
    with pytest.raises(RuntimeError):
        async with monitor.monitor_whatsapp_operation("bad"):
            raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        async with monitor.monitor_api_operation("bad", "GMT"):
            raise RuntimeError("x")
    monitor.whatsapp_service.send_message.return_value = SimpleNamespace(success=True, error=None)
    monitor.email_service.list_available_templates.return_value = ["a"]
    result = await monitor.test_services()
    assert result["whatsapp"]["status"] == "healthy" and result["email"]["status"] == "healthy"
    assert (await monitor.send_test_notification())["ok"]
