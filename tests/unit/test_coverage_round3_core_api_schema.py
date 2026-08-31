"""Deterministic round-three coverage for core API, config, model, and RFQ paths."""

from __future__ import annotations

import logging
import logging.handlers
import os
import runpy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.webhook as webhook
import app.config as config_module
import app.database as database
import app.models as models
import app.procucev_apis.bfs_apis as bfs_module
import app.procucev_apis.procucev_api_client as client_module
from app.procucev_apis.bfs_apis import BFSAPIService
from app.procucev_apis.procucev_api_client import ProcucevAPIClient
from app.schemas.rfq import RFQCreateRequestSchema, RFQItemSchema, RFQValidationSchema


class RequestStub:
    """Small request double that never touches FastAPI, HTTP, or the network."""

    def __init__(self, *, body=b"payload", form=None, json_data=None, query=None):
        self.headers = {}
        self._body = body
        self._form = {} if form is None else form
        self._json = json_data
        self.query_params = {} if query is None else query

    async def body(self):
        return self._body

    async def form(self):
        return self._form

    async def json(self):
        if isinstance(self._json, BaseException):
            raise self._json
        return self._json


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Response:
    def __init__(self, data=None, status=200, text="ok"):
        self.status = status
        self._data = {} if data is None else data
        self._text = text
        self.headers = {"Content-Type": "application/json"}

    async def text(self):
        return self._text

    async def json(self):
        return self._data


class Session:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.request_calls = []

    def request(self, *args, **kwargs):
        self.request_calls.append((args, kwargs))
        return _ResponseContext(self.responses.pop(0))


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


def _client_settings():
    return SimpleNamespace(
        gmt_base_url="https://gmt.invalid",
        gmt_username="unit-user",
        gmt_password="unit-password",
        gmt_phone="9876543210",
        gmt_client_id="unit-client",
        gmt_client_secret="unit-secret",
        gmt_max_retries=1,
        gmt_retry_delay=0,
    )


def _make_client(monkeypatch):
    redis = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    monkeypatch.setattr(client_module, "get_settings", _client_settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: redis)
    return ProcucevAPIClient(), redis


def _db_context(value=None):
    @contextmanager
    def context():
        yield value if value is not None else object()

    return context


def test_webhook_import_creates_missing_log_directory_without_real_filesystem(monkeypatch):
    """Exercise import-time logging setup while replacing every side effect."""
    source_path = webhook.__file__
    payload_logger = MagicMock()
    payload_logger.handlers = []
    file_handler = MagicMock()
    real_get_logger = logging.getLogger

    def fake_get_logger(name=None):
        if name == "whatsapp_webhook":
            return payload_logger
        return real_get_logger(name)

    with (
        patch.object(os.path, "exists", return_value=False),
        patch.object(os, "makedirs") as makedirs,
        patch.object(logging, "getLogger", side_effect=fake_get_logger),
        patch.object(logging.handlers, "RotatingFileHandler", return_value=file_handler),
        patch("app.config.get_settings", return_value=SimpleNamespace()),
        patch(
            "app.services.message_queue_service.MessageQueueService",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.inactivity_timeout_service.get_timeout_service",
            return_value=MagicMock(),
        ),
    ):
        runpy.run_path(source_path, run_name="round3_webhook_import")

    makedirs.assert_called_once_with(os.path.join("logs", "whatsapp"))
    file_handler.setLevel.assert_called_once_with(logging.DEBUG)
    payload_logger.addHandler.assert_called_once_with(file_handler)


@pytest.mark.asyncio
async def test_webhook_error_recovery_without_processable_payload(monkeypatch):
    background = MagicMock()
    parse = AsyncMock(side_effect=[RuntimeError("initial parse"), None])
    cancel = AsyncMock()
    monkeypatch.setattr(webhook, "parse_webhook_data", parse)
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", cancel)

    response = await webhook.handle_webhook.__wrapped__(RequestStub(), background)

    assert response.status_code == 200
    assert b"error_handled" in response.body
    cancel.assert_not_awaited()
    background.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_delivery_callback_form_and_json_fallback_branches(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr("app.redis_db.get_redis_service", lambda: redis)

    # Empty form data takes the nested JSON-dictionary fallback.
    request = RequestStub(
        form={},
        json_data={"status": "sent", "mobile": "1", "msg_ref": "m", "timestamp": "now"},
    )
    assert (await webhook.handle_delivery_callback(request)).status_code == 200

    # A non-empty form skips JSON parsing and still returns the normal response.
    request = RequestStub(form={"status": "delivered", "mobile": "1", "mid": "m"})
    request.json = AsyncMock(return_value=["not", "a", "mapping"])
    assert (await webhook.handle_delivery_callback(request)).status_code == 200

    # A form parsing failure falls back to a non-dictionary JSON body.
    request = RequestStub(json_data=["not", "a", "mapping"])
    request.form = AsyncMock(side_effect=RuntimeError("form unavailable"))
    assert (await webhook.handle_delivery_callback(request)).status_code == 200


@pytest.mark.asyncio
async def test_webhook_process_guards_cover_missing_phone_and_uncreated_session(monkeypatch):
    timeout = AsyncMock()
    redis = AsyncMock()
    monkeypatch.setattr(webhook, "timeout_service", timeout)
    monkeypatch.setattr(webhook, "get_redis_service", lambda: redis)
    monkeypatch.setattr(
        webhook,
        "get_settings",
        lambda: SimpleNamespace(pending_reply_ttl_seconds=20),
    )

    # No phone means the initial activity/pending-reply block is skipped.
    await webhook.process_message_async({"type": "image", "content": {"id": "image"}})
    timeout.update_user_activity.assert_not_awaited()
    redis.set.assert_not_awaited()

    # A conflicting interactive session returns before constructing a chat service.
    monkeypatch.setattr(webhook, "create_direct_processing_session", AsyncMock(return_value=False))
    monkeypatch.setattr("app.utils.logging_utils.UserPhoneContext", lambda _phone: AsyncContext())
    chat_constructor = MagicMock()
    monkeypatch.setattr("app.services.chat_service.ChatService", chat_constructor)

    await webhook.process_message_async(
        {"from": "9199", "type": "interactive", "content": {"id": "button"}}
    )

    chat_constructor.assert_not_called()


@pytest.mark.asyncio
async def test_procucev_authentication_failure_and_dynamic_token_fallbacks(monkeypatch):
    client, redis = _make_client(monkeypatch)

    # No cached token plus failed authentication reaches the post-lock failure log path.
    client.authenticate = AsyncMock(return_value=False)
    assert await client._ensure_authenticated() is False
    client.authenticate.assert_awaited_once()
    redis.get.assert_awaited_once_with(client.token_cache_key, as_json=True)

    # A still-valid in-memory token returns before Redis or re-authentication.
    client.auth_token = "cached-token"
    client.token_expiry = client_module.datetime.now(client_module.UTC) + client_module.timedelta(seconds=60)
    client.authenticate.reset_mock()
    redis.get.reset_mock()
    assert await client._ensure_authenticated() is True
    client.authenticate.assert_not_awaited()
    redis.get.assert_not_awaited()

    # A token populated while waiting on the refresh lock takes the double-check path.
    client.auth_token = None
    client.token_expiry = None

    async def populate_token(*_args, **_kwargs):
        client.auth_token = "race-token"
        client.token_expiry = client_module.datetime.now(client_module.UTC) + client_module.timedelta(seconds=60)
        return None

    redis.get = AsyncMock(side_effect=populate_token)
    client.authenticate.reset_mock()
    assert await client._ensure_authenticated() is True
    client.authenticate.assert_not_awaited()
    redis.get.assert_awaited_once_with(client.token_cache_key, as_json=True)

    monkeypatch.setattr(client_module, "manual_log_api_call", MagicMock())
    client.session = Session([Response(data={"ok": "fallback"})])
    client.authenticate_dynamic = AsyncMock(return_value=None)
    result = await client.send_request(
        "GET",
        "/dynamic-fallback",
        dynamic_token={"username": "dynamic-user", "phone": "9876543210"},
    )
    assert result["ok"] == "fallback"
    client.authenticate_dynamic.assert_awaited_once_with("dynamic-user", "9876543210")
    assert "Authorization" not in client.session.request_calls[0][1]["headers"]

    # Incomplete credentials do not call dynamic authentication and use the ordinary request path.
    client.session = Session([Response(data={"ok": "incomplete"})])
    client.authenticate_dynamic.reset_mock()
    result = await client.send_request(
        "GET", "/incomplete", dynamic_token={"username": "dynamic-user"}
    )
    assert result["ok"] == "incomplete"
    client.authenticate_dynamic.assert_not_awaited()

    # A truthy unsupported token type falls through without adding authorization.
    client.session = Session([Response(data={"ok": "unsupported"})])
    result = await client.send_request("GET", "/unsupported", dynamic_token=object())
    assert result["ok"] == "unsupported"
    client.authenticate_dynamic.assert_not_awaited()
    assert "Authorization" not in client.session.request_calls[0][1]["headers"]


def test_config_disabled_health_monitoring_validation(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    settings = config_module.Settings()
    settings.webhook_health_monitoring_enabled = False

    # Required values come from the unit-safe environment in tests/conftest.py.
    settings.validate_config()


def test_config_enabled_health_monitoring_validation(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    settings = config_module.Settings()
    settings.webhook_health_monitoring_enabled = True
    settings.webhook_health_check_interval_seconds = 20
    settings.webhook_api_timeout_seconds = 10
    settings.webhook_failure_grace_period_seconds = 60
    settings.webhook_api_response_threshold_seconds = 5
    settings.webhook_recovery_confirmations = 1
    settings.webhook_warning_consecutive_threshold = 1

    settings.validate_config()


def test_model_invalid_enum_fallbacks_are_deterministic():
    assert models.ConversationSession.validate_user_type(None, "user_type", "invalid") is models.UserType.unknown
    assert models.ConversationSession.validate_session_state(None, "session_state", "invalid") is models.SessionState.active

    # Enum instances are non-string values and pass through unchanged.
    assert models.ConversationSession.validate_user_type(None, "user_type", models.UserType.buyer) is models.UserType.buyer
    assert models.ConversationSession.validate_session_state(None, "session_state", models.SessionState.active) is models.SessionState.active


def test_rfq_direct_validator_edges_and_truthy_optional_fields():
    assert RFQItemSchema.validate_quantity("2.5") == 2.5
    with pytest.raises(ValueError, match="valid number"):
        RFQItemSchema.validate_quantity("not-a-number")
    with pytest.raises(ValueError, match="greater than 0"):
        RFQItemSchema.validate_quantity(0)

    assert RFQCreateRequestSchema.validate_rfq_items([{"description": "bolt"}]) == [{"description": "bolt"}]
    with pytest.raises(ValueError, match="At least one RFQ item"):
        RFQCreateRequestSchema.validate_rfq_items([])
    assert RFQCreateRequestSchema.validate_delivery_locations([{"state": "MH"}]) == [{"state": "MH"}]
    with pytest.raises(ValueError, match="At least one delivery location"):
        RFQCreateRequestSchema.validate_delivery_locations([])

    complete_optional = RFQValidationSchema(
        preferred_brand="Acme", remarks="urgent", attachments=[{"name": "spec.pdf"}]
    )
    assert complete_optional.get_missing_optional_fields() == []


def test_rfq_mandatory_questions_cover_standalone_dynamic_and_duplicate_paths(monkeypatch):
    schema = RFQValidationSchema()
    missing = [
        "division_confirmation",
        "division",
        "item_0_description",
        "item_0_quantity",
        "item_2_description",
        "item_2_quantity",
        "delivery_location_0_state",
        "delivery_location_0_city",
        "delivery_location_2_state",
        "delivery_location_2_city",
        "delivery_location_2_pincode",
    ]
    monkeypatch.setattr(
        RFQValidationSchema,
        "get_missing_mandatory_fields",
        lambda _self: missing,
    )

    questions = schema.get_mandatory_questions()

    assert "Please confirm the division for this request" in questions
    assert "Which division or department is this for?" in questions
    assert "What are the item details? Please provide the description and quantity needed" in questions
    assert "Where should the items be delivered? Please provide the complete address (State, City, and Pincode)" in questions
    assert len(questions) == len(set(questions))
    assert questions.count("What is the item description?") == 0
    assert questions.count("How many items do you need (quantity)?") == 0
    assert questions.count("What is the delivery state?") == 0
    assert questions.count("What is the delivery city?") == 0
    assert questions.count("What is the delivery pincode?") == 0


@pytest.mark.asyncio
async def test_database_ssl_configuration_and_bfs_http_boundary(monkeypatch):
    local_settings = SimpleNamespace(database_mode="local")
    assert database._get_ssl_connect_args(local_settings) == {"ssl_disabled": True}

    client_settings = SimpleNamespace(database_mode="client", PROJECT_ROOT="C:/unit")
    with patch.object(os.path, "exists", return_value=False):
        with pytest.raises(ValueError, match="TLS CA certificate"):
            database._get_ssl_connect_args(client_settings)

    api_client = AsyncMock()
    monkeypatch.setattr(bfs_module, "get_procucev_api_client", lambda: api_client)
    api_client.post.return_value = {"success": True, "data": [{"id": "item-1"}]}
    service = BFSAPIService()

    result = await service.search_bfs_items([{"category": ["tools"]}])
    assert result == {
        "success": True,
        "data": [{"id": "item-1"}],
        "raw_response": api_client.post.return_value,
    }
    api_client.post.assert_awaited_once()
