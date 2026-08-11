from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import app.services.service_monitor as monitor_module
import app.utils.chroma_client as chroma_module
from app.services.service_monitor import ServiceMonitor, get_service_monitor
from app.services.verification_check_service import VerificationCheckService
from app.tools.retry_service import RetryService, get_retry_service
from app.utils.message_restore_utils import restore_last_bot_message
from app.utils.technical_failure_handler import handle_technical_failure


class InMemorySession:
    def __init__(self, state=None):
        self.workflow_state = state if state is not None else {}


@pytest.mark.asyncio
async def test_technical_failure_handler_builds_context_and_swallows_handler_errors(monkeypatch):
    handler = AsyncMock()
    monkeypatch.setattr("app.utils.technical_failure_handler.get_global_error_handler", lambda: handler)

    await handle_technical_failure("555", "database unavailable", "Database Error")

    context = handler.handle_error.await_args.args[0]
    assert context.error_type == "Database Error"
    assert context.error_message == "database unavailable"
    assert context.user_phone == "555"

    handler.handle_error.side_effect = RuntimeError("notification failed")
    await handle_technical_failure()


@pytest.mark.parametrize("server_mode", [True, False])
def test_chroma_client_server_and_persistent_success(monkeypatch, tmp_path, server_mode):
    settings = SimpleNamespace(
        chroma_use_server=server_mode,
        chroma_host="chroma.test",
        chroma_port=8123,
        chroma_persist_directory=str(tmp_path / "default"),
    )
    monkeypatch.setattr(chroma_module, "get_settings", lambda: settings)

    if server_mode:
        client = MagicMock()
        monkeypatch.setattr(chroma_module.chromadb, "HttpClient", MagicMock(return_value=client))
        result = chroma_module.get_chroma_client()
        assert result is client
        chroma_module.chromadb.HttpClient.assert_called_once_with(host="chroma.test", port=8123)
        client.heartbeat.assert_called_once_with()
    else:
        client = MagicMock()
        settings_factory = MagicMock(return_value=SimpleNamespace())
        monkeypatch.setattr(chroma_module, "Settings", settings_factory)
        monkeypatch.setattr(chroma_module.chromadb, "PersistentClient", MagicMock(return_value=client))
        mkdir = MagicMock()
        monkeypatch.setattr(chroma_module.Path, "mkdir", mkdir)

        custom_path = str(tmp_path / "custom")
        result = chroma_module.get_chroma_client(custom_path)

        assert result is client
        mkdir.assert_called_once_with(parents=True, exist_ok=True)
        settings_factory.assert_called_once_with(
            chroma_segment_cache_policy="LRU",
            chroma_memory_limit_bytes=8_000_000_000,
        )
        chroma_module.chromadb.PersistentClient.assert_called_once_with(
            path=custom_path, settings=settings_factory.return_value
        )


def test_chroma_client_uses_default_path_and_wraps_server_failure(monkeypatch):
    settings = SimpleNamespace(
        chroma_use_server=True,
        chroma_host="localhost",
        chroma_port=8000,
        chroma_persist_directory="default-path",
    )
    monkeypatch.setattr(chroma_module, "get_settings", lambda: settings)
    failed_client = MagicMock()
    failed_client.heartbeat.side_effect = ConnectionError("refused")
    monkeypatch.setattr(chroma_module.chromadb, "HttpClient", MagicMock(return_value=failed_client))

    with pytest.raises(RuntimeError, match="ChromaDB server not available") as error:
        chroma_module.get_chroma_client()
    assert "refused" in str(error.value)

    settings.chroma_use_server = False
    persistent = MagicMock()
    persistent_factory = MagicMock(return_value=persistent)
    settings_factory = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr(chroma_module.chromadb, "PersistentClient", persistent_factory)
    monkeypatch.setattr(chroma_module, "Settings", settings_factory)
    monkeypatch.setattr(chroma_module.Path, "mkdir", MagicMock())
    assert chroma_module.get_chroma_client() is persistent
    assert persistent_factory.call_args.kwargs["path"] == "default-path"


@pytest.mark.asyncio
async def test_restore_last_bot_message_covers_fallback_text_and_button_formats():
    whatsapp = AsyncMock()
    empty_session = InMemorySession()
    await restore_last_bot_message(empty_session, whatsapp, "1", "fallback")
    whatsapp.send_message.assert_awaited_once_with("1", "fallback")

    whatsapp.reset_mock()
    text_session = InMemorySession({"saved": "plain text"})
    await restore_last_bot_message(text_session, whatsapp, "1", "unused", state_key="saved")
    whatsapp.send_message.assert_awaited_once_with("1", "plain text")
    assert text_session.workflow_state == {}

    whatsapp.reset_mock()
    structured = {
        "body": {"text": "Choose"},
        "header": {"text": "Header"},
        "footer": "Footer",
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "a", "title": "Alpha"}},
                {"id": "b", "title": "Beta"},
                {"type": "unsupported"},
                "not-a-button",
            ]
        },
    }
    structured_session = InMemorySession({"last": structured})
    await restore_last_bot_message(structured_session, whatsapp, "1", "unused", state_key="last")
    whatsapp.send_configurable_buttons.assert_awaited_once_with(
        recipient_id="1",
        body="Choose",
        buttons_config=[{"id": "a", "title": "Alpha"}, {"id": "b", "title": "Beta"}],
        header="Header",
        footer="Footer",
    )
    assert structured_session.workflow_state == {}

    whatsapp.reset_mock()
    no_button_session = InMemorySession(
        {"saved": {"body": "simple body", "header": {"text": "ignored"}, "action": {"buttons": []}}}
    )
    await restore_last_bot_message(no_button_session, whatsapp, "1", "unused", state_key="saved")
    whatsapp.send_message.assert_awaited_once_with("1", "simple body")


@pytest.mark.asyncio
async def test_retry_service_covers_object_dict_failure_and_backoff_paths(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.tools.retry_service.asyncio.sleep", sleep)
    service = RetryService(max_retries=2, initial_delay=0.25)

    object_result = SimpleNamespace(success=True)
    first = await service.retry_with_backoff(AsyncMock(return_value=object_result), "arg", flag=True)
    assert first == {"success": True, "result": object_result, "attempts": 1}

    attempts = AsyncMock(side_effect=[{"success": False}, {"success": True, "id": "ok"}])
    second = await service.retry_with_backoff(attempts)
    assert second["attempts"] == 2
    assert sleep.await_args_list == [call(0.25)]

    unsuccessful_object = SimpleNamespace(success=False)
    always_bad = AsyncMock(return_value=unsuccessful_object)
    third = await service.retry_with_backoff(always_bad)
    assert third["success"] is False
    assert third["attempts"] == 3
    assert "unsuccessful result" in third["error"]
    assert sleep.await_args_list[-2:] == [call(0.25), call(0.5)]

    no_attempts = RetryService(max_retries=0, initial_delay=10)
    no_sleep = AsyncMock()
    monkeypatch.setattr("app.tools.retry_service.asyncio.sleep", no_sleep)
    result = await no_attempts.retry_with_backoff(AsyncMock(side_effect=ValueError("bad")))
    assert result == {"success": False, "error": "bad", "attempts": 1}
    no_sleep.assert_not_awaited()
    assert get_retry_service().max_retries == 3


@pytest.fixture
def monitor_dependencies(monkeypatch):
    settings = SimpleNamespace(enable_error_notifications=True, error_notification_cooldown_minutes=5)
    notification = MagicMock()
    notification.notify_whatsapp_down = AsyncMock()
    notification.notify_api_down = AsyncMock()
    notification.notify_error = AsyncMock(return_value={"sent": True})
    whatsapp = MagicMock()
    email = MagicMock()
    monkeypatch.setattr(monitor_module, "get_settings", lambda: settings)
    monkeypatch.setattr(monitor_module, "ErrorNotificationService", lambda: notification)
    monkeypatch.setattr(monitor_module, "WhatsAppService", lambda: whatsapp)
    monkeypatch.setattr(monitor_module, "EmailService", lambda: email)
    return settings, notification, whatsapp, email


@pytest.mark.asyncio
async def test_service_monitor_init_context_handlers_health_and_singleton(monkeypatch, monitor_dependencies):
    settings, notification, whatsapp, email = monitor_dependencies
    monitor = ServiceMonitor()
    assert monitor.settings is settings
    assert monitor.notification_service is notification
    assert monitor.whatsapp_service is whatsapp
    assert monitor.email_service is email

    async with monitor.monitor_whatsapp_operation("send"):
        pass
    with pytest.raises(RuntimeError, match="whatsapp failure"):
        async with monitor.monitor_whatsapp_operation("send"):
            raise RuntimeError("whatsapp failure")
    notification.notify_whatsapp_down.assert_awaited_once()
    details = notification.notify_whatsapp_down.await_args.args[0]
    assert details["operation"] == "send"
    assert details["message"] == "whatsapp failure"

    with pytest.raises(ValueError, match="api failure"):
        async with monitor.monitor_api_operation("fetch", "GMT"):
            raise ValueError("api failure")
    notification.notify_api_down.assert_awaited_once()
    assert notification.notify_api_down.await_args.args[0]["service"] == "GMT"

    monitor._last_notification["whatsapp_error"] = datetime.now()
    await monitor._handle_whatsapp_error("suppressed", "again")
    assert notification.notify_whatsapp_down.await_count == 1
    monitor._last_notification["api_error_GMT"] = datetime.now() - timedelta(minutes=6)
    assert monitor._should_notify("api_error_GMT")
    monitor._update_last_notification("api_error_GMT")
    assert not monitor._should_notify("api_error_GMT")
    settings.enable_error_notifications = False
    assert not monitor._should_notify("new")

    notification.notify_whatsapp_down.side_effect = RuntimeError("notify down")
    monitor._last_notification.pop("whatsapp_error", None)
    await monitor._handle_whatsapp_error("send", "ignored notification failure")
    settings.enable_error_notifications = True

    whatsapp.send_message = AsyncMock(return_value=SimpleNamespace(success=True, error=None))
    email.list_available_templates.return_value = ["template"]
    healthy = await monitor.test_services()
    assert healthy == {
        "whatsapp": {"status": "healthy", "details": "OK"},
        "email": {"status": "healthy", "details": "1 templates available"},
    }

    whatsapp.send_message.return_value = SimpleNamespace(success=False, error="down")
    email.list_available_templates.return_value = []
    unhealthy = await monitor.test_services()
    assert unhealthy["whatsapp"] == {"status": "unhealthy", "details": "down"}
    assert unhealthy["email"] == {"status": "unhealthy", "details": "0 templates available"}

    whatsapp.send_message.side_effect = RuntimeError("wa error")
    email.list_available_templates.side_effect = RuntimeError("email error")
    errors = await monitor.test_services()
    assert errors["whatsapp"] == {"status": "error", "details": "wa error"}
    assert errors["email"] == {"status": "error", "details": "email error"}

    notification.notify_error.return_value = {"ok": True}
    sent = await monitor.send_test_notification("api_error")
    assert sent == {"ok": True}
    assert notification.notify_error.await_args.args[0] == "api_error"

    monkeypatch.setattr(monitor_module, "_service_monitor", None)
    first = get_service_monitor()
    second = get_service_monitor()
    assert first is second
    assert isinstance(first, ServiceMonitor)


@pytest.mark.asyncio
async def test_verification_check_accepts_data_shapes_and_enforces_statuses(monkeypatch):
    otp = AsyncMock(return_value={"status": "otp_sent"})
    service = VerificationCheckService(AsyncMock(), SimpleNamespace(send_otp=otp), AsyncMock())

    class DictUser:
        def dict(self):
            return {"verificationStatus": "EMAIL_VERIFIED", "selfClient": True, "approved": True, "email": "buyer@x"}

    granted = await service.check_and_enforce_verification("1", DictUser(), InMemorySession())
    assert granted["access_granted"]
    assert granted["user_data"]["email"] == "buyer@x"

    attribute_user = SimpleNamespace(
        verification_status="EMAIL_VERIFIED", self_client=False, email="seller@x", approved=False
    )
    seller = await service.check_and_enforce_verification("1", attribute_user, InMemorySession())
    assert seller["redirect_info"]["reason"] == "seller_authentication"
    assert otp.await_args.args[3] is True

    class PairUser:
        __slots__ = ()

        def __iter__(self):
            return iter((("verificationStatus", "EMAIL_VERIFIED"), ("selfClient", True), ("approved", False), ("username", "buyer@x")))

    pending = await service.check_and_enforce_verification("1", PairUser(), InMemorySession())
    assert pending["redirect_to_support"]

    otp.reset_mock()
    pending_status = {"verificationStatus": "PENDING_EMAIL_VERIFICATION", "username": "buyer@x"}
    required = await service.check_and_enforce_verification("1", pending_status, InMemorySession())
    assert required["otp_sent"]
    otp.assert_awaited_once()

    invalid_email = {"verificationStatus": "EMAIL_VERIFICATION_FAILED", "email": None}
    failed = await service.check_and_enforce_verification("1", invalid_email, InMemorySession())
    assert failed["otp_sent"] is False
    assert failed["redirect_info"]["reason"] == "EMAIL_VERIFICATION_FAILED"

    unknown = await service.check_and_enforce_verification(
        "1", {"fullName": "A", "email": "a@x", "verificationStatus": "UNKNOWN"}, InMemorySession()
    )
    assert unknown["redirect_info"]["reason"] == "unknown_status"

    invalid = await service.check_and_enforce_verification("1", object(), InMemorySession())
    assert invalid["redirect_info"]["reason"] == "verification_check_error"


@pytest.mark.asyncio
async def test_verification_otp_refresh_and_domain_helpers(monkeypatch):
    auth = AsyncMock()
    otp = AsyncMock(return_value={"status": "otp_sent"})
    service = VerificationCheckService(auth, SimpleNamespace(send_otp=otp), AsyncMock())

    class FakeSession:
        def __init__(self):
            self.workflow_state = {}

    monkeypatch.setattr("app.models.ConversationSession", FakeSession)
    result = await service._send_verification_otp("1", "a@x", session=None, is_daily_verification=True)
    assert result["status"] == "otp_sent"
    assert otp.await_args.args[2].workflow_state == {}
    assert otp.await_args.args[3] is True
    assert await service._send_verification_otp("1", "your email") == {
        "status": "otp_send_failed",
        "reason": "invalid_email",
    }
    otp.side_effect = RuntimeError("provider")
    assert (await service._send_verification_otp("1", "a@x"))["reason"] == "provider"

    auth.authenticate_user.return_value = {"success": True}
    refreshed = await service.refresh_user_verification_status("1", max_retries=1)
    assert refreshed == {"success": True, "data": []}
    auth.authenticate_user.return_value = {"success": False}
    sleep = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", sleep)
    exhausted = await service.refresh_user_verification_status("1", max_retries=2)
    assert exhausted["success"] is False
    assert sleep.await_args_list == [call(0.5)]
    assert await service.refresh_user_verification_status("1", max_retries=0) == {
        "success": False,
        "message": "Max retries exceeded",
    }
    auth.authenticate_user.side_effect = RuntimeError("auth")
    assert (await service.refresh_user_verification_status("1"))["message"] == "auth"

    assert (await service._check_domain_approval("u", current_approved_status=True))["status"] == "already_approved"
    assert (await service._check_domain_approval("u", False))["status"] == "missing_user_data"
    assert (await service._check_domain_approval("u", False, {"email": "a@x"}))["status"] == "missing_domain_data"

    domain = SimpleNamespace(
        check_domain_match=AsyncMock(return_value={"approved": True, "method": "ai"}),
        user_approval_api_call=AsyncMock(return_value={"approved": True, "status": "approved"}),
    )
    monkeypatch.setattr("app.services.domain_check_service.DomainCheckService", lambda: domain)
    approved = await service._check_domain_approval(
        "u", False, {"username": "a@x", "company_name": "Acme"}
    )
    assert approved["approved"]
    domain.user_approval_api_call.assert_awaited_once_with("u")

    domain.check_domain_match.return_value = {"approved": False, "method": "fallback", "reasoning": "mismatch"}
    rejected = await service._check_domain_approval("u", False, {"email": "a@x", "companyName": "Acme"})
    assert rejected["status"] == "ai_domain_rejected"
    domain.check_domain_match.side_effect = RuntimeError("domain down")
    error = await service._check_domain_approval("u", False, {"email": "a@x", "companyName": "Acme"})
    assert error == {"approved": False, "error": "domain down"}
