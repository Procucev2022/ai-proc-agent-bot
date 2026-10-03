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
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "configured-verify-token")
    settings = Settings()
    assert settings.get_database_url() == "sqlite:///:memory:"
    assert settings.get_remote_database_url() == "sqlite:///:memory:"
    assert settings.is_ssl_enabled() is False
    assert settings.get_openai_config()["api_key"] == "configured-openai-key"
    assert settings.get_whatsapp_config()["verify_token"] == "configured-verify-token"
    assert settings.get_logging_config()["handlers"] == ["console"]
    assert settings.get_rfq_status_config()["max_allowed"] == 5
    assert settings.get_api_timeout_config()["read_timeout"] == 30
    assert settings.get_retry_config()["max_attempts"] == 3
    assert settings.get_celery_config()["worker_concurrency"] == 2
    assert settings._parse_webhook_recipients(" a, ,b ") == ["a", "b"]
    assert settings._parse_webhook_recipients("") == []


def test_settings_database_url_prefers_new_name_and_falls_back_to_legacy(monkeypatch):
    # Stub load_dotenv so the developer's real .env cannot repopulate deleted vars.
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_MODE", "client")

    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://new")
    monkeypatch.setenv("CLIENT_DATABASE_URL", "mysql+pymysql://legacy")
    assert Settings().get_database_url() == "mysql+pymysql://new"

    monkeypatch.delenv("DATABASE_URL")
    assert Settings().get_database_url() == "mysql+pymysql://legacy"

    monkeypatch.delenv("CLIENT_DATABASE_URL")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings().get_database_url()


def _production_env_only(monkeypatch):
    """Leave only the variables production sets; nothing from the developer's .env."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    for name in (
        "DATABASE_MODE", "LOCAL_DATABASE_URL", "CLIENT_DATABASE_URL", "REMOTE_DATABASE_URL",
        "PROCUCEV_DB_NAME", "WHATSAPP_DB", "ENABLE_REMOTE_CATEGORIZATION",
        "WHATSAPP_TEMPLATE_RFQ_NOTIFICATION", "WHATSAPP_TEMPLATE_BFS_BID_NOTIFICATION",
        "PROCUCEV_PORTAL_URL", "RFQ_FOLLOWUP_NOTE", "PROCUCEV_RFQ_DETAILS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://user:p%40ss@db.example.com:3306/aiprocprod")


def test_settings_derive_database_settings_from_database_url_alone(monkeypatch):
    _production_env_only(monkeypatch)

    settings = Settings()

    assert settings.database_mode == "client"
    assert settings.is_ssl_enabled() is True
    assert settings.get_database_url().endswith("/aiprocprod")
    assert settings.whatsapp_db == "aiprocprod"
    assert settings.procucev_db_name == "quaproduction"
    assert settings.enable_remote_categorization is True
    assert settings.get_remote_database_url() == (
        "mysql+pymysql://user:p%40ss@db.example.com:3306/quaproduction"
    )
    assert settings.WHATSAPP_TEMPLATE_RFQ_NOTIFICATION
    assert settings.WHATSAPP_TEMPLATE_BFS_BID_NOTIFICATION
    assert settings.PROCUCEV_PORTAL_URL
    assert settings.rfq_followup_note == settings.PROCUCEV_PORTAL_URL
    assert settings.procucev_rfq_details_url == settings.PROCUCEV_PORTAL_URL


def test_settings_explicit_database_overrides_win(monkeypatch):
    _production_env_only(monkeypatch)
    monkeypatch.setenv("REMOTE_DATABASE_URL", "mysql+pymysql://u:p@remote:3306/other_schema")
    monkeypatch.setenv("WHATSAPP_DB", "bot_db")
    monkeypatch.setenv("ENABLE_REMOTE_CATEGORIZATION", "false")
    monkeypatch.setenv("PROCUCEV_PORTAL_URL", "https://portal.example.com")

    settings = Settings()

    assert settings.procucev_db_name == "other_schema"
    assert settings.remote_database_url == "mysql+pymysql://u:p@remote:3306/other_schema"
    assert settings.whatsapp_db == "bot_db"
    assert settings.get_remote_database_url() is None
    assert settings.rfq_followup_note == "https://portal.example.com"

    monkeypatch.setenv("PROCUCEV_DB_NAME", "named_schema")
    assert Settings().procucev_db_name == "named_schema"


def test_settings_local_mode_inferred_without_database_url(monkeypatch):
    _production_env_only(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("LOCAL_DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/procurement_db")

    settings = Settings()

    assert settings.database_mode == "local"
    assert settings.is_ssl_enabled() is False
    assert settings.whatsapp_db == "procurement_db"
    assert settings.remote_database_url == "mysql+pymysql://u:p@localhost:3306/quaproduction"

    monkeypatch.setenv("LOCAL_DATABASE_URL", "mysql+pymysql://u:p@localhost:3306")
    assert Settings().whatsapp_db == "procurement_db"


def test_database_url_helpers_tolerate_missing_and_invalid_urls():
    from app.config import _database_name, _with_database

    assert _database_name(None) is None
    assert _database_name("not a url") is None
    assert _database_name("mysql+pymysql://h") is None
    assert _with_database(None, "x") is None
    assert _with_database("not a url", "x") is None


def test_settings_database_and_remote_error_branches(monkeypatch):
    settings = Settings()
    settings.database_mode = "client"
    settings.client_database_url = None
    with pytest.raises(ValueError, match="DATABASE_URL"):
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
