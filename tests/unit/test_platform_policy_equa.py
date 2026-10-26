"""BACKEND_PLATFORM switch: QUA stays the default, EQUA routes through its bot bridge."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.utils import platform_policy


BUTTONS = [
    {"id": "create_rfq", "title": "Create new RFQ"},
    {"id": "search_bfs", "title": "Search Ready Stocks"},
    {"id": "bfs_raise_rfq", "title": "Raise RFQ"},
]


# ── platform_policy ─────────────────────────────────────────────────────────

def test_normalize_platform_defaults_to_qua(caplog):
    assert platform_policy.normalize_platform(None) == "qua"
    assert platform_policy.normalize_platform(" EQUA ") == "equa"
    assert platform_policy.normalize_platform("other") == "qua"
    assert "Unknown BACKEND_PLATFORM" in caplog.text


def test_platform_default_reads_the_environment_when_not_given(monkeypatch):
    monkeypatch.delenv("BACKEND_PLATFORM", raising=False)
    assert platform_policy.platform_default("q", "e") == "q"
    monkeypatch.setenv("BACKEND_PLATFORM", "equa")
    assert platform_policy.platform_default("q", "e") == "e"
    assert platform_policy.platform_default("q", "e", "qua") == "q"


def test_platform_and_bfs_follow_settings_then_environment(monkeypatch):
    equa = SimpleNamespace(backend_platform="equa", enable_bfs=False, bot_api_key="k")
    assert platform_policy.get_backend_platform(equa) == "equa"
    assert platform_policy.is_equa(equa) is True
    assert platform_policy.is_bfs_enabled(equa) is False
    assert platform_policy.is_bfs_enabled(SimpleNamespace(enable_bfs=True)) is True

    assert platform_policy.get_backend_platform(SimpleNamespace(backend_platform=MagicMock())) == "qua"

    bare = SimpleNamespace()
    monkeypatch.setenv("BACKEND_PLATFORM", "equa")
    monkeypatch.delenv("ENABLE_BFS", raising=False)
    assert platform_policy.is_equa(bare) is True
    assert platform_policy.is_bfs_enabled(bare) is False
    monkeypatch.setenv("ENABLE_BFS", "true")
    assert platform_policy.is_bfs_enabled(bare) is True


def test_settings_that_fail_to_load_fall_back_to_environment(monkeypatch, caplog):
    import app.config as config_module

    def broken():
        raise RuntimeError("no settings")

    monkeypatch.setattr(config_module, "get_settings", broken)
    monkeypatch.setenv("BACKEND_PLATFORM", "equa")
    monkeypatch.setenv("BOT_API_KEY", "env-key")
    assert platform_policy.is_equa() is True
    assert platform_policy.bot_api_headers() == {"X-Bot-Api-Key": "env-key"}
    assert "Could not load settings" in caplog.text


def test_default_settings_resolve_through_get_settings():
    # The unit suite runs with the QUA default.
    assert platform_policy.get_backend_platform() == "qua"
    assert platform_policy.is_bfs_enabled() is True


def test_bfs_buttons_are_hidden_only_while_bfs_is_disabled():
    on = SimpleNamespace(enable_bfs=True)
    off = SimpleNamespace(enable_bfs=False)
    assert platform_policy.filter_bfs_buttons(BUTTONS, on) is BUTTONS
    assert platform_policy.filter_bfs_buttons(None, off) is None
    assert platform_policy.filter_bfs_buttons(BUTTONS, off) == [{"id": "create_rfq", "title": "Create new RFQ"}]
    unrelated = [{"id": "rfq_status", "title": "Check RFQ Status"}]
    assert platform_policy.filter_bfs_buttons(unrelated, off) is unrelated
    assert platform_policy.filter_bfs_buttons([{"id": "search_bfs"}], off) == platform_policy.FALLBACK_BUTTONS
    assert platform_policy.is_bfs_button(None) is False


def test_bot_api_headers_only_when_a_key_is_configured(monkeypatch):
    assert platform_policy.bot_api_headers(SimpleNamespace(bot_api_key="secret")) == {"X-Bot-Api-Key": "secret"}
    monkeypatch.delenv("BOT_API_KEY", raising=False)
    assert platform_policy.bot_api_headers(SimpleNamespace()) == {}
    assert platform_policy.bot_api_headers(SimpleNamespace(bot_api_key="")) == {}


# ── Settings ────────────────────────────────────────────────────────────────

def test_settings_default_to_qua_with_bfs_and_remote_schema(monkeypatch):
    for name in ("BACKEND_PLATFORM", "ENABLE_BFS", "ENABLE_REMOTE_CATEGORIZATION", "BOT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.backend_platform == "qua"
    assert settings.is_equa is False
    assert settings.enable_bfs is True
    assert settings.enable_remote_categorization is True
    settings.validate_config()


def test_settings_for_equa_switch_off_qua_only_features_and_require_the_key(monkeypatch):
    for name in ("ENABLE_BFS", "ENABLE_REMOTE_CATEGORIZATION", "BOT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKEND_PLATFORM", "equa")
    # Settings validate on construction, so a deployment without the key fails at boot.
    with pytest.raises(ValueError, match="BOT_API_KEY"):
        Settings()

    monkeypatch.setenv("BOT_API_KEY", "bridge-key")
    settings = Settings()
    assert settings.is_equa is True
    assert settings.bot_api_key == "bridge-key"
    assert settings.enable_bfs is False
    assert settings.enable_remote_categorization is False

    monkeypatch.setenv("ENABLE_BFS", "true")
    assert Settings().enable_bfs is True


# ── Celery schedule ─────────────────────────────────────────────────────────

def test_celery_disables_qua_schema_tasks_by_default_on_equa(monkeypatch):
    import app.celery_config as celery_config

    for name in ("ENABLE_VECTOR_STORE_SYNC", "ENABLE_SELLER_MATCHING", "ENABLE_DAILY_CATEGORY_REBUILD", "ENABLE_BFS_NOTIFICATION"):
        monkeypatch.delenv(name, raising=False)
    try:
        monkeypatch.setenv("BACKEND_PLATFORM", "equa")
        equa = importlib.reload(celery_config)
        assert not any(
            key in equa.beat_schedule
            for key in ("vector-store-sync-task", "seller-matching-task", "daily-category-vector-rebuild-task", "bfs-notification-task")
        )
        assert "log-cleanup-task" in equa.beat_schedule

        monkeypatch.setenv("ENABLE_SELLER_MATCHING", "true")
        assert "seller-matching-task" in importlib.reload(celery_config).beat_schedule

        monkeypatch.delenv("ENABLE_SELLER_MATCHING")
        monkeypatch.setenv("BACKEND_PLATFORM", "qua")
        qua = importlib.reload(celery_config)
        assert "seller-matching-task" in qua.beat_schedule
        assert "bfs-notification-task" in qua.beat_schedule
    finally:
        monkeypatch.delenv("BACKEND_PLATFORM", raising=False)
        importlib.reload(celery_config)


# ── API client ──────────────────────────────────────────────────────────────

class _FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body):
        self._body = body

    async def text(self):
        return "{}"

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    closed = False

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse({"ok": True})

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse({"access_token": "t"})


@pytest.mark.asyncio
async def test_api_client_sends_the_bridge_key_and_keeps_it_out_of_logs(monkeypatch):
    import app.procucev_apis.procucev_api_client as client_mod

    settings = SimpleNamespace(
        gmt_base_url="https://equa.example/api/bot", gmt_username="svc@x.com", gmt_password="p", gmt_phone="919876543210",
        gmt_client_id="c", gmt_client_secret="s", gmt_max_retries=1, gmt_retry_delay=0, bot_api_key="bridge-key",
    )
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(client_mod, "get_redis_service", lambda: SimpleNamespace())
    logged = []
    monkeypatch.setattr(client_mod, "manual_log_api_call", lambda title, url, input_data, *a: logged.append(input_data))

    client = client_mod.ProcucevAPIClient()
    client.session = _FakeSession()
    await client.send_request("GET", "/rest/gmt/getSubscriptionPlans", headers={"X-Trace": "1"})
    assert client.session.calls[0]["headers"] == {"X-Bot-Api-Key": "bridge-key", "X-Trace": "1"}
    assert logged[0]["headers"] == {"X-Trace": "1"}

    assert await client.authenticate_dynamic("buyer@x.com", "919876543210") == "t"
    assert client.session.calls[1]["headers"] == {"X-Bot-Api-Key": "bridge-key"}


# ── WhatsApp buttons ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_configurable_buttons_passes_buttons_through_the_bfs_filter(monkeypatch):
    import app.services.whatsapp_service as wa_mod

    seen = []

    def fake_filter(buttons):
        seen.append(buttons)
        return []

    monkeypatch.setattr(wa_mod.platform_policy, "filter_bfs_buttons", fake_filter)
    service = wa_mod.WhatsAppService.__new__(wa_mod.WhatsAppService)
    result = await service.send_configurable_buttons("919876543210", "Pick one", BUTTONS)
    assert seen == [BUTTONS]
    assert result.success is False


# ── BFS handler ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bfs_handler_declines_every_entry_point_while_bfs_is_disabled(monkeypatch):
    import app.services.handlers.bfs_search_handler as bfs_mod

    monkeypatch.setattr(bfs_mod, "OpenAIService", MagicMock)
    monkeypatch.setattr(bfs_mod, "get_bfs_api_service", lambda: MagicMock())
    monkeypatch.setattr(bfs_mod.platform_policy, "is_bfs_enabled", lambda settings=None: False)
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    handler = bfs_mod.BFSSearchHandler(whatsapp, SimpleNamespace(save_session=AsyncMock()))
    user = SimpleNamespace(phone_number="919876543210")

    session = SimpleNamespace(workflow_state={"bfs_search_pending": True, "bfs_bid_stage": "format_input", "keep": 1})
    assert await handler.handle_button(user, session, "search_bfs") == {"status": "bfs_unavailable"}
    assert session.workflow_state == {"keep": 1}

    empty = SimpleNamespace(workflow_state=None)
    assert (await handler.handle_bfs_search(user, empty, "steel pipes"))["status"] == "bfs_unavailable"
    assert (await handler.handle_bid_format_input(user, empty, "1,2"))["status"] == "bfs_unavailable"
    assert (await handler.handle_bid_otp_input(user, empty, "123456"))["status"] == "bfs_unavailable"
    assert whatsapp.send_message.await_count == 4
    assert whatsapp.send_message.await_args.args[1] == platform_policy.BFS_UNAVAILABLE_MESSAGE


# ── Opt-out ─────────────────────────────────────────────────────────────────

@pytest.fixture
def opt_out(monkeypatch):
    import app.services.opt_out_service as opt_mod

    monkeypatch.setattr(opt_mod, "get_db_session", lambda: MagicMock())
    monkeypatch.setattr(opt_mod, "WhatsAppService", MagicMock)
    monkeypatch.setattr(opt_mod, "OpenAIService", MagicMock)
    monkeypatch.setattr(opt_mod.platform_policy, "is_equa", lambda settings=None: True)
    return opt_mod, opt_mod.OptOutService()


@pytest.mark.asyncio
async def test_opt_out_goes_to_the_equa_bridge(monkeypatch, opt_out):
    opt_mod, service = opt_out
    import app.procucev_apis.procucev_api_client as client_mod

    client = SimpleNamespace(post=AsyncMock(side_effect=[{"success": True}, {"success": False, "message": "Seller not found"}, RuntimeError("down")]))
    monkeypatch.setattr(client_mod, "get_procucev_api_client", lambda: client)
    remote = MagicMock(side_effect=AssertionError("QUA MySQL must not be used on EQUA"))
    monkeypatch.setattr(opt_mod, "get_remote_db_session", remote)

    assert await service._update_remote_opt_out_status("v-1", True) is True
    assert client.post.await_args.kwargs["json_data"] == {"sellerId": "v-1", "optedOut": True}
    assert client.post.await_args.args[0] == "/whatsapp/seller-opt-out"
    assert await service._update_remote_opt_out_status("v-1", False) is False
    assert await service._update_remote_opt_out_status("v-1", False) is False
    remote.assert_not_called()


# ── Per-call timeout (used by the EQUA adapter for RFQ creation) ──────────

@pytest.mark.asyncio
async def test_send_request_honours_per_call_timeout_and_attempts(monkeypatch):
    import aiohttp
    import app.procucev_apis.procucev_api_client as client_mod

    settings = SimpleNamespace(
        gmt_base_url="https://equa.example/api/bot", gmt_username="svc@x.com", gmt_password="p", gmt_phone="919876543210",
        gmt_client_id="c", gmt_client_secret="s", gmt_max_retries=3, gmt_retry_delay=0, bot_api_key=None,
    )
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(client_mod, "get_redis_service", lambda: SimpleNamespace())
    monkeypatch.setattr(client_mod, "manual_log_api_call", lambda *a: None)
    monkeypatch.delenv("BOT_API_KEY", raising=False)
    client = client_mod.ProcucevAPIClient()
    client._handle_timeout_error = AsyncMock()

    class TimingOut(_FakeSession):
        def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            raise aiohttp.ClientConnectionError("down")

    client.session = TimingOut()
    result = await client.send_request("POST", "/rest/gmt/createRFQByClient", total_timeout=90, max_retries=1)
    assert result["success"] is False
    assert len(client.session.calls) == 1
    assert client.session.calls[0]["timeout"].total == 90

    client.session = _FakeSession()
    await client.send_request("GET", "/x")
    assert "timeout" not in client.session.calls[0]


# ── EQUA runs with QUA gone ─────────────────────────────────────────────────

QUA_VARIABLES = (
    "GMT_BASE_URL", "GMT_USERNAME", "GMT_PHONE", "GMT_PASSWORD", "GMT_CLIENT_ID", "GMT_CLIENT_SECRET",
    "REMOTE_DATABASE_URL", "PROCUCEV_DB_NAME", "PROCUCEV_PORTAL_URL", "EQUA_PORTAL_URL",
    "ENABLE_REMOTE_CATEGORIZATION", "ENABLE_BFS",
)


def _equa_without_qua(monkeypatch, **extra):
    for name in QUA_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKEND_PLATFORM", "equa")
    monkeypatch.setenv("BOT_API_KEY", "bridge-key")
    monkeypatch.setenv("EQUA_API_BASE_URL", "https://equa.example/api/bot")
    monkeypatch.setenv("EQUA_SERVICE_USERNAME", "bot@equa.example")
    monkeypatch.setenv("EQUA_SERVICE_PHONE", "919876543210")
    monkeypatch.setenv("DATABASE_MODE", "client")
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://bot:pw@bot-db.example:3306/aiprocprod")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_equa_settings_need_no_qua_variable_database_or_portal(monkeypatch):
    _equa_without_qua(monkeypatch)
    settings = Settings()  # validates on construction
    assert settings.gmt_base_url == "https://equa.example/api/bot"
    assert settings.gmt_username == "bot@equa.example"
    assert settings.equa_rfq_sourcing_mode == "mode_0"
    assert settings.gmt_phone == "919876543210"
    assert settings.gmt_password is None and settings.gmt_client_id is None
    # Never derived onto QUA's schema on the bot's MySQL server.
    assert settings.remote_database_url is None
    assert settings.enable_remote_categorization is False
    assert settings.PROCUCEV_PORTAL_URL == platform_policy.EQUA_PORTAL_URL
    assert settings.procucev_rfq_details_url == platform_policy.EQUA_PORTAL_URL
    assert "azurewebsites" not in settings.rfq_followup_note


def test_equa_settings_report_missing_equa_values_by_their_equa_names(monkeypatch):
    _equa_without_qua(monkeypatch)
    monkeypatch.delenv("EQUA_API_BASE_URL")
    monkeypatch.delenv("BOT_API_KEY")
    monkeypatch.delenv("EQUA_SERVICE_USERNAME")
    monkeypatch.delenv("EQUA_SERVICE_PHONE")
    with pytest.raises(ValueError) as error:
        Settings()
    assert str(error.value) == "Missing required environment variables: EQUA_API_BASE_URL, BOT_API_KEY"


def test_equa_names_win_and_gmt_names_remain_a_fallback(monkeypatch):
    _equa_without_qua(monkeypatch, GMT_BASE_URL="https://old/api/bot", GMT_USERNAME="old@x.com", EQUA_PORTAL_URL="https://portal.example/login")
    settings = Settings()
    assert settings.gmt_base_url == "https://equa.example/api/bot"
    assert settings.PROCUCEV_PORTAL_URL == "https://portal.example/login"

    monkeypatch.delenv("EQUA_API_BASE_URL")
    monkeypatch.delenv("EQUA_SERVICE_USERNAME")
    settings = Settings()
    assert settings.gmt_base_url == "https://old/api/bot"
    assert settings.gmt_username == "old@x.com"

    monkeypatch.setenv("PROCUCEV_PORTAL_URL", "https://explicit/login")
    assert Settings().PROCUCEV_PORTAL_URL == "https://explicit/login"


def test_equa_honours_an_explicit_remote_database_only(monkeypatch):
    _equa_without_qua(monkeypatch, REMOTE_DATABASE_URL="mysql+pymysql://u:p@other:3306/catalog")
    assert Settings().remote_database_url == "mysql+pymysql://u:p@other:3306/catalog"


def test_qua_settings_ignore_equa_names(monkeypatch):
    monkeypatch.delenv("BACKEND_PLATFORM", raising=False)
    monkeypatch.delenv("PROCUCEV_PORTAL_URL", raising=False)
    monkeypatch.setenv("EQUA_API_BASE_URL", "https://equa.example/api/bot")
    settings = Settings()
    assert settings.gmt_base_url == "https://gmt.invalid"
    assert settings.PROCUCEV_PORTAL_URL == platform_policy.QUA_PORTAL_URL


def test_api_logger_uses_the_resolved_backend_url(monkeypatch):
    import app.config as config_module
    from app.utils import procucev_api_logger as logger_mod

    monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(gmt_base_url="https://equa.example/api/bot"))
    assert logger_mod._api_base_url() == "https://equa.example/api/bot"

    def broken():
        raise RuntimeError("no settings")

    monkeypatch.setattr(config_module, "get_settings", broken)
    monkeypatch.setenv("GMT_BASE_URL", "https://gmt.example")
    assert logger_mod._api_base_url() == "https://gmt.example"
    monkeypatch.delenv("GMT_BASE_URL")
    assert logger_mod._api_base_url() == "https://api.procucev.com"
