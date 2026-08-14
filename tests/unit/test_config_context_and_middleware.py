from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.context.context_controller import param_context, session_context, user_context
from app.context.context_manager import ContextManager, context_manager
from app.context.middleware import ContextMiddleware, get_request_id


def test_settings_accessors_and_singleton_contract(monkeypatch):
    # Settings reads these straight from the process environment and conftest only
    # supplies them via setdefault, so whatever the runner exports wins. Asserting
    # one workflow's placeholder made this test pass under pr-quality-gate.yml
    # (AZURE_OPENAI_API_KEY=unit-test-key) and fail under the deploy workflows
    # (test-key). Pin the values the assertions below depend on instead.
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "configured-openai-key")
    monkeypatch.setenv("LOCAL_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REMOTE_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("DATABASE_MODE", "local")
    settings = Settings()
    assert settings.get_database_url() == "sqlite:///:memory:"
    assert settings.get_remote_database_url() == "sqlite:///:memory:"
    assert settings.is_ssl_enabled() is False
    assert settings.get_openai_config()["api_key"] == "configured-openai-key"
    assert settings.get_whatsapp_config()["verify_token"]
    assert settings.get_logging_config()["handlers"] == ["console"]
    assert settings.get_rfq_status_config()["max_allowed"] == 5
    assert settings.get_api_timeout_config()["read_timeout"] == 30
    assert settings.get_retry_config()["max_attempts"] == 3
    assert settings.get_celery_config()["worker_concurrency"] == 2
    assert settings._parse_webhook_recipients(" a, ,b ") == ["a", "b"]
    assert settings._parse_webhook_recipients("") == []


def test_settings_database_and_remote_error_branches(monkeypatch):
    settings = Settings()
    settings.database_mode = "client"
    settings.client_database_url = None
    with pytest.raises(ValueError, match="CLIENT_DATABASE_URL"):
        settings.get_database_url()
    settings.client_database_url = "postgresql://bad"
    with pytest.raises(ValueError, match="PostgreSQL"):
        settings.get_database_url()
    settings.enable_remote_categorization = False
    assert settings.get_remote_database_url() is None
    settings.enable_remote_categorization = True
    settings.remote_database_url = None
    with pytest.raises(ValueError, match="REMOTE_DATABASE_URL"):
        settings.get_remote_database_url()
    settings.openai_api_key = None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        settings.get_openai_config()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AZURE_OPENAI_API_KEY", "", "AZURE_OPENAI_API_KEY"),
        ("GMT_USERNAME", "", "GMT_USERNAME"),
        ("GMT_PHONE", "", "GMT_PHONE"),
        ("LOCAL_DATABASE_URL", "", "LOCAL_DATABASE_URL"),
    ],
)
def test_settings_required_environment_validation(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings()


def test_settings_numeric_and_webhook_validation(monkeypatch):
    monkeypatch.setenv("SESSION_TIMEOUT_MINUTES", "0")
    with pytest.raises(ValueError, match="SESSION_TIMEOUT_MINUTES"):
        Settings()
    monkeypatch.setenv("SESSION_TIMEOUT_MINUTES", "30")
    monkeypatch.setenv("WEBHOOK_HEALTH_MONITORING_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("WEBHOOK_API_TIMEOUT_SECONDS", "10")
    with pytest.raises(ValueError, match="greater than"):
        Settings()


def test_context_manager_and_controller_lifecycle():
    manager = ContextManager()
    manager.clear("request")
    manager.set("request", "payload", {"a": 1})
    manager.update("request", "payload", {"b": 2})
    assert manager.get("request", "payload") == {"a": 1, "b": 2}
    manager.update("missing", "payload", {"ignored": True})
    manager.set("request", "scalar", "value")
    manager.update("request", "scalar", {"ignored": True})
    manager.delete("request", "scalar")
    manager.delete("missing", "scalar")

    session_context.set("r1", {"state": "new"})
    session_context.update("r1", {"step": 1})
    assert session_context.get("r1")["step"] == 1
    session_context.delete("r1")
    user_context.set("r2", {"name": "Ada"})
    assert user_context.get("r2") == {"name": "Ada"}
    user_context.delete("r2")
    param_context.set("r3", "value")
    assert param_context.get("r3") == "value"
    param_context.delete("r3")


@pytest.mark.asyncio
async def test_context_middleware_success_and_error_paths():
    request = SimpleNamespace(
        state=SimpleNamespace(),
        method="GET",
        url=SimpleNamespace(path="/health"),
    )
    call_next = AsyncMock(return_value=SimpleNamespace(status_code=200))
    middleware = ContextMiddleware.__new__(ContextMiddleware)
    response = await middleware.dispatch(request, call_next)
    assert response.status_code == 200
    assert get_request_id(request) is not None
    call_next.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await middleware.dispatch(request, call_next)
