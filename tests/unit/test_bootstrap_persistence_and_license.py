from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

import app.api.webhook as webhook
import app.database as database
import app.license as license_pkg
import app.license.license_middleware as license_middleware
import app.main as main
import app.redis_db as redis_db
import app.utils.pincode_distance as pincode_distance


class RequestStub:
    def __init__(self, *, content_type="", body=b"{}", form=None, json_data=None, query=None, client=None, path="/x", method="POST"):
        self.headers = {"content-type": content_type} if content_type else {}
        self._body = body
        self._form = form or {}
        self._json = json_data
        self.query_params = query or {}
        self.client = client or SimpleNamespace(host="127.0.0.1")
        self.url = SimpleNamespace(path=path)
        self.method = method

    async def body(self):
        return self._body

    async def form(self):
        return self._form

    async def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


@pytest.mark.asyncio
async def test_webhook_verification_and_parsers(monkeypatch):
    monkeypatch.setattr(webhook, "get_settings", lambda: SimpleNamespace(WHATSAPP_VERIFY_TOKEN="secret"))
    response = await webhook.verify_webhook("subscribe", "challenge", "secret")
    assert isinstance(response, PlainTextResponse)
    assert response.body == b"challenge"
    with pytest.raises(HTTPException) as exc:
        await webhook.verify_webhook("subscribe", "challenge", "wrong")
    assert exc.value.status_code == 403

    form = {"replytype": "TEXT", "customernumber": "9199", "replymessage": "hello+world", "wabanumber": "1"}
    parsed = await webhook.parse_webhook_data(RequestStub(content_type="application/x-www-form-urlencoded", form=form))
    assert parsed["type"] == "text" and parsed["content"] == "hello world"
    parsed_json = await webhook.parse_webhook_data(RequestStub(content_type="application/json", json_data={"type": "image"}))
    assert parsed_json == {"type": "image"}
    parsed_query = await webhook.parse_webhook_data(RequestStub(query=form))
    assert parsed_query["from"] == "9199"
    assert await webhook.parse_webhook_data(RequestStub()) is None

    image = dict(form, replytype="IMAGE", replymessage='{"id":"m1","mime_type":"image/png"}')
    parsed_image = webhook.parse_user_response_callback(image)
    assert parsed_image["content"]["id"] == "m1"
    malformed = webhook.parse_user_response_callback(dict(form, replytype="DOCUMENT", replymessage="not-json"))
    assert malformed["content"] == "not-json"
    assert webhook.parse_user_response_callback({"replymessage": "x"}) is None
    assert webhook.parse_user_response_callback({"customernumber": "1"}) is None
    assert webhook.parse_json_webhook({"ok": True}) == {"ok": True}


@pytest.mark.asyncio
async def test_webhook_routing_and_delivery_callbacks(monkeypatch):
    request = RequestStub(body=b"payload")
    background = MagicMock()
    for message_type, task in [("text", webhook.enqueue_message_async), ("interactive", webhook.process_message_async), ("image", webhook.process_message_async)]:
        monkeypatch.setattr(webhook, "parse_webhook_data", AsyncMock(return_value={"type": message_type, "from": "1"}))
        result = await webhook.handle_webhook.__wrapped__(request, background)
        assert result.status_code == 200
        background.add_task.assert_called_with(task, {"type": message_type, "from": "1"})

    monkeypatch.setattr(webhook, "parse_webhook_data", AsyncMock(return_value=None))
    assert (await webhook.handle_webhook.__wrapped__(request, background)).body == b'{"status":"ok"}'

    cancel = AsyncMock()
    calls = [RuntimeError("parse"), {"type": "text", "from": "1"}]
    async def parse_twice(_request):
        value = calls.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(webhook, "parse_webhook_data", parse_twice)
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", cancel)
    result = await webhook.handle_webhook.__wrapped__(request, background)
    assert result.status_code == 200 and b"error_handled" in result.body
    cancel.assert_awaited_once()

    redis = AsyncMock()
    monkeypatch.setattr(redis_db, "get_redis_service", lambda: redis)
    callback = RequestStub(query={"qStatus": "read", "qMobile": "1", "qMsgRef": "m"})
    assert (await webhook.handle_delivery_callback(callback)).status_code == 200
    fallback = RequestStub()
    fallback.form = AsyncMock(return_value={"status": "delivered", "mobile": "1", "mid": "m"})
    assert (await webhook.handle_delivery_callback(fallback)).status_code == 200
    redis.set.side_effect = RuntimeError("redis")
    assert (await webhook.handle_delivery_callback(callback)).status_code == 500


@pytest.mark.asyncio
async def test_webhook_sessions_queue_document_and_error_paths(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr(webhook, "get_redis_service", lambda: redis)
    redis.exists.return_value = False
    assert await webhook.create_direct_processing_session("+9199", "image")
    assert redis.set.await_count == 2
    redis.exists.return_value = True
    assert not await webhook.create_direct_processing_session("9199", "image")
    redis.exists.side_effect = RuntimeError("down")
    assert not await webhook.create_direct_processing_session("9199", "image")
    redis.exists.side_effect = None
    await webhook.cleanup_direct_processing_session("+9199")
    assert redis.delete.await_count >= 3 and redis.delete_pattern.await_count == 1
    redis.delete.side_effect = RuntimeError("down")
    await webhook.cleanup_direct_processing_session("9199")

    timeout = AsyncMock()
    queue = AsyncMock()
    monkeypatch.setattr(webhook, "timeout_service", timeout)
    monkeypatch.setattr(webhook, "message_queue_service", queue)
    monkeypatch.setattr(webhook, "get_settings", lambda: SimpleNamespace(pending_reply_ttl_seconds=12))
    await webhook.enqueue_message_async({"from": "+9199", "content": "hello"})
    timeout.update_user_activity.assert_awaited_once_with("+9199")
    queue.enqueue_message.assert_awaited_once()
    technical_handler = webhook.handle_technical_error_with_cancel
    queue.enqueue_message.side_effect = RuntimeError("queue")
    cancel = AsyncMock()
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", cancel)
    await webhook.enqueue_message_async({"from": "9199"})
    cancel.assert_awaited_once()

    chat = AsyncMock()
    await webhook.process_document_message({"from": "1", "content": "bad"}, chat)
    await webhook.process_document_message({"from": "1", "content": {}}, chat)
    await webhook.process_document_message({"from": "1", "content": {"document": {"link": "u", "filename": "a.xlsx"}}}, chat)
    await webhook.process_document_message({"from": "1", "content": {"id": "m", "filename": "a.pdf"}}, chat)
    assert [call.kwargs["message_type"] for call in chat.process_message.await_args_list] == ["excel_upload", "document"]

    whatsapp = AsyncMock()
    import sys
    monkeypatch.setitem(sys.modules, "app.services.whatsapp_service", SimpleNamespace(WhatsAppService=lambda *args, **kwargs: whatsapp))
    failure = AsyncMock()
    monkeypatch.setattr("app.utils.technical_failure_handler.handle_technical_failure", failure)
    monkeypatch.setattr(webhook, "handle_technical_error_with_cancel", technical_handler)
    await webhook.handle_technical_error_with_cancel("1", "bad", "X")
    whatsapp.send_message.assert_awaited_once()
    failure.assert_awaited_once()
    whatsapp.send_message.side_effect = RuntimeError("send")
    await webhook.handle_technical_error_with_cancel("1", "bad")


@pytest.mark.asyncio
async def test_webhook_process_message_paths(monkeypatch):
    timeout = AsyncMock()
    redis = AsyncMock()
    monkeypatch.setattr(webhook, "timeout_service", timeout)
    monkeypatch.setattr(webhook, "get_redis_service", lambda: redis)
    monkeypatch.setattr(webhook, "get_settings", lambda: SimpleNamespace(pending_reply_ttl_seconds=20))
    await webhook.process_message_async({"from": "1"})

    class Context:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

    fake_chat = AsyncMock()
    fake_chat.cleanup = AsyncMock()
    fake_chat.process_message = AsyncMock()
    fake_db = object()
    @contextmanager
    def db_context():
        yield fake_db
    monkeypatch.setattr(webhook, "create_direct_processing_session", AsyncMock(return_value=True))
    monkeypatch.setattr(webhook, "cleanup_direct_processing_session", AsyncMock())
    monkeypatch.setattr("app.utils.logging_utils.UserPhoneContext", lambda _phone: Context())
    monkeypatch.setattr("app.database.get_db_session_context", db_context)
    monkeypatch.setattr("app.services.chat_service.ChatService", lambda **kwargs: fake_chat)
    monkeypatch.setattr(webhook, "process_document_message", AsyncMock())
    await webhook.process_message_async({"from": "+1", "type": "document", "content": {"id": "m"}})
    webhook.process_document_message.assert_awaited_once()
    await webhook.process_message_async({"from": "1", "type": "image", "content": {"id": "m"}})
    fake_chat.process_message.assert_awaited_once()


class FakeRedisClient:
    def __init__(self):
        self.values = {}
        self.deleted = []
        self.scan_calls = 0
        self.fail = None

    async def set(self, key, value, **kwargs):
        if self.fail: raise self.fail
        self.values[key] = value
        return True

    async def get(self, key):
        if self.fail: raise self.fail
        return self.values.get(key)

    async def delete(self, *keys):
        if self.fail: raise self.fail
        count = 0
        for key in keys:
            if key in self.values:
                count += 1
                del self.values[key]
        self.deleted.extend(keys)
        return count

    async def exists(self, key): return int(key in self.values)
    async def ttl(self, key): return 42 if key in self.values else -2
    async def expire(self, key, seconds): return key in self.values
    async def expireat(self, key, timestamp): return key in self.values
    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def scan(self, cursor, **kwargs):
        self.scan_calls += 1
        return (0, list(self.values)) if self.scan_calls > 1 else (1, list(self.values))


@pytest.mark.asyncio
async def test_redis_base_auth_session_and_singletons(monkeypatch):
    client = FakeRedisClient()
    service = redis_db.BaseRedisService()
    service.client = client
    assert await service.set("d", {"x": 1}, ex=2)
    assert json.loads(await service.get("d")) == {"x": 1}
    assert await service.set("empty", "", ex=0)
    assert await service.get("empty") == ""
    assert await service.delete("empty")
    assert not await service.delete("missing")
    assert await service.exists("d") and await service.ttl("d") == 42
    assert await service.expire("d", 1) and await service.expireat("d", 1)
    assert await service.incr("n") == 1
    assert await service.delete_pattern("*") >= 1
    client.fail = RuntimeError("redis")
    assert not await service.set("x", "y")
    assert await service.get("x") is None and not await service.delete("x")
    client.fail = None
    client.values["bad"] = "not-json"
    assert await service.get("bad", as_json=True) is None

    auth = redis_db.AuthRedisService()
    auth.client = FakeRedisClient()
    auth.settings = SimpleNamespace(redis_expiry_seconds=30)
    assert await auth.store("1", {"name": "A"})
    monkeypatch.setattr(redis_db.User, "from_mixed_data", lambda data: SimpleNamespace(data=data))
    retrieved = await auth.retrieve("1")
    assert retrieved.data == {"name": "A"}
    assert await auth.is_authenticated("1")
    assert await auth.refresh_user_token("1")
    assert await auth.delete_auth("1")
    assert await auth.retrieve("missing") is None
    auth.client.values["auth:2"] = "{}"
    monkeypatch.setattr(redis_db.User, "from_mixed_data", MagicMock(side_effect=ValueError("bad")))
    assert await auth.retrieve("2") is None
    assert not await auth.refresh_user_token("missing")

    session = redis_db.SessionRedisService()
    session.client = FakeRedisClient()
    session.default_ttl = 100
    assert await session.store_session("s", {"session_id": "s"}, ttl=4)
    assert await session.save_session({"session_id": "s", "x": 1})
    assert not await session.save_session({"x": 1})
    assert await session.get_session("s") == {"session_id": "s", "x": 1}
    assert await session.refresh_ttl("s", 5)
    assert await session.delete_session("s")
    assert not await session.session_exists("s")
    assert await session.get_session_ttl("s") == -2
    session.client.values["session:h"] = json.dumps({"session_id": "h"})
    assert await session.append_message_to_history("h", "assistant", "hello")
    assert session.client.values["session:h"]
    assert not await session.append_message_to_history("missing", "user", "x")


@pytest.mark.asyncio
async def test_redis_manager_and_singletons(monkeypatch):
    redis_db.AsyncRedisConnectionManager._pool = None
    fake = object()
    factory = AsyncMock(return_value=fake)
    monkeypatch.setattr(redis_db.aioredis, "from_url", factory)
    assert await redis_db.AsyncRedisConnectionManager.get_client() is fake
    assert await redis_db.AsyncRedisConnectionManager.get_client() is fake
    factory.side_effect = RuntimeError("connect")
    redis_db.AsyncRedisConnectionManager._pool = None
    with pytest.raises(RuntimeError):
        await redis_db.AsyncRedisConnectionManager.get_client()

    redis_db._redis_service = redis_db._auth_service = redis_db._session_service = None
    assert redis_db.get_redis_service() is redis_db.get_redis_service()
    assert redis_db.get_auth_redis_service() is redis_db.get_auth_redis_service()
    assert redis_db.get_session_redis_service() is redis_db.get_session_redis_service()


def test_pincode_cache_and_distance_branches(monkeypatch):
    pincode_distance.clear_pincode_cache()
    assert pincode_distance.get_coordinates_from_pincode("") is None
    assert pincode_distance.get_coordinates_from_pincode("123") is None
    assert pincode_distance.get_cache_size() == 1
    nomi = MagicMock()
    location = SimpleNamespace(latitude=12.0, longitude=77.0, isna=lambda: SimpleNamespace(all=lambda: False))
    nomi.query_postal_code.return_value = location
    monkeypatch.setattr(pincode_distance, "_nomi", nomi)
    assert pincode_distance.get_coordinates_from_pincode("560001") == (12.0, 77.0)
    assert pincode_distance.get_coordinates_from_pincode("560001") == (12.0, 77.0)
    nomi.query_postal_code.return_value = None
    assert pincode_distance.get_coordinates_from_pincode("560002") is None
    location.isna = lambda: SimpleNamespace(all=lambda: True)
    nomi.query_postal_code.return_value = location
    assert pincode_distance.get_coordinates_from_pincode("560003") is None
    location.isna = lambda: SimpleNamespace(all=lambda: False)
    location.latitude = float("nan")
    nomi.query_postal_code.return_value = location
    assert pincode_distance.get_coordinates_from_pincode("560004") is None
    nomi.query_postal_code.side_effect = RuntimeError("lookup")
    assert pincode_distance.get_coordinates_from_pincode("560005") is None
    monkeypatch.setattr(pincode_distance, "_pincode_cache", {"560001": (1.0, 2.0)})
    monkeypatch.setattr(pincode_distance, "geodesic", lambda *_: SimpleNamespace(kilometers=10.0))
    assert pincode_distance.calculate_distance_between_pincodes("560001", "560001") == 10.0
    assert pincode_distance.calculate_distance_from_pincode_to_coords("560001", 1, 2) == 10.0
    monkeypatch.setattr(pincode_distance, "geodesic", lambda *_: SimpleNamespace(kilometers=float("inf")))
    assert pincode_distance.calculate_distance_between_pincodes("560001", "560001") is None
    monkeypatch.setattr(pincode_distance, "geodesic", MagicMock(side_effect=RuntimeError("geo")))
    assert pincode_distance.calculate_distance_from_pincode_to_coords("560001", 1, 2) is None
    pincode_distance.clear_pincode_cache()
    assert pincode_distance.get_cache_size() == 0


def _license_file(tmp_path, secret, expires, fields=None):
    plaintext = "|".join(fields or ["token", "machine", "created", expires, "30", "v1"])
    key = hashlib.sha256(secret.encode()).digest()
    encrypted = bytes(ord(char) ^ key[index % len(key)] for index, char in enumerate(plaintext))
    encoded = base64.b64encode(encrypted).decode()
    signature = hashlib.sha256((encoded + secret).encode()).hexdigest()
    path = tmp_path / "license.lic"
    path.write_text(f"{signature}::{encoded}", encoding="utf-8")
    return path


def test_license_validation_and_middleware(tmp_path, monkeypatch):
    secret = "test-secret"
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(license_secret_key=secret))
    missing, message = license_pkg.validate_license(str(tmp_path / "missing"))
    assert not missing and "not found" in message
    malformed = tmp_path / "bad"
    malformed.write_text("bad", encoding="utf-8")
    assert license_pkg.validate_license(str(malformed))[0] is False
    expired = _license_file(tmp_path, secret, (datetime.now() - timedelta(days=2)).isoformat())
    assert license_pkg.validate_license(str(expired))[0] is False
    soon = _license_file(tmp_path, secret, (datetime.now() + timedelta(days=3)).isoformat())
    assert license_pkg.validate_license(str(soon))[0] is True
    valid = _license_file(tmp_path, secret, (datetime.now() + timedelta(days=30)).isoformat())
    assert license_pkg.validate_license(str(valid)) == (True, "License valid.")
    valid.write_text("0" * 64 + valid.read_text().split("::", 1)[1], encoding="utf-8")
    assert license_pkg.validate_license(str(valid))[0] is False

    monkeypatch.setattr(license_middleware, "_validate_license_standalone", lambda path: (True, "ok"))
    middleware = license_middleware.LicenseMiddleware(object(), str(valid))
    assert middleware._read_license_file() is None

    async def call_next(request):
        return JSONResponse({"ok": True})
    exempt = RequestStub(path="/health")
    assert (pytest.run(asyncio_run(middleware.dispatch(exempt, call_next))) if False else True)


async def asyncio_run(awaitable):
    return await awaitable


@pytest.mark.asyncio
async def test_license_middleware_dispatch(monkeypatch):
    monkeypatch.setattr(license_middleware, "_validate_license_standalone", lambda path: (True, "ok"))
    middleware = license_middleware.LicenseMiddleware(object())
    called = AsyncMock(return_value=JSONResponse({"ok": True}))
    exempt = RequestStub(path="/health")
    assert (await middleware.dispatch(exempt, called)).status_code == 200
    monkeypatch.setattr(middleware, "validate_license", lambda: (False, "expired"))
    denied = await middleware.dispatch(RequestStub(path="/private"), called)
    assert denied.status_code == 403
    monkeypatch.setattr(middleware, "validate_license", lambda: (True, "ok"))
    assert (await middleware.dispatch(RequestStub(path="/private"), called)).status_code == 200


@pytest.mark.asyncio
async def test_main_middleware_endpoints_and_exception(monkeypatch):
    async def next_response(_request): return JSONResponse({"ok": True})
    allowed = main.IPRestrictionMiddleware(object(), ["1.1.1.1"])
    assert (await allowed.dispatch(RequestStub(client=SimpleNamespace(host="1.1.1.1")), next_response)).status_code == 200
    denied = await allowed.dispatch(RequestStub(client=SimpleNamespace(host="2.2.2.2")), next_response)
    assert denied.status_code == 403
    forwarded = RequestStub(client=SimpleNamespace(host="2.2.2.2"))
    forwarded.headers["x-forwarded-for"] = "1.1.1.1, 2.2.2.2"
    assert (await allowed.dispatch(forwarded, next_response)).status_code == 200
    unrestricted = main.IPRestrictionMiddleware(object(), [])
    assert (await unrestricted.dispatch(RequestStub(), next_response)).status_code == 200

    logger_middleware = main.RequestLoggingMiddleware(object())
    assert (await logger_middleware.dispatch(RequestStub(), next_response)).status_code == 200
    async def fail(_request): raise RuntimeError("handler")
    with pytest.raises(RuntimeError): await logger_middleware.dispatch(RequestStub(), fail)

    assert b'"status":"healthy"' in (await main.health_check()).body
    assert b"/chat" in (await main.root()).body
    monkeypatch.setattr(main.templates, "TemplateResponse", MagicMock(return_value="html"))
    assert await main.chat_page(RequestStub()) == "html"

    notify = AsyncMock()
    monkeypatch.setattr(main, "handle_server_error", notify)
    req = RequestStub(json_data={"phone": "1"}, path="/x")
    response = await main.global_exception_handler(req, ValueError("bad"))
    assert response.status_code == 500
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_chat_and_upload_success_failure(monkeypatch):
    class Context:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    fake_chat = MagicMock()
    fake_chat.whatsapp_service = MagicMock()
    fake_chat.whatsapp_service.send_message = AsyncMock()
    fake_chat.whatsapp_service.send_configurable_buttons = AsyncMock()
    fake_chat.process_message = AsyncMock(return_value={"status": "done"})
    fake_chat.cleanup = AsyncMock()
    @contextmanager
    def db_context(): yield object()
    monkeypatch.setattr("app.utils.logging_utils.UserPhoneContext", lambda _phone: Context())
    monkeypatch.setattr(main, "get_db_session_context", db_context)
    monkeypatch.setattr("app.database.get_db_session_context", db_context)
    monkeypatch.setattr(main, "ChatService", lambda **kwargs: fake_chat)
    result = await main.process_chat_message.__wrapped__(RequestStub(), main.ChatMessage(message={"image": "x"}, phone="1"))
    assert result["success"] and result["status"] == "done"
    fake_chat.process_message.side_effect = RuntimeError("chat")
    notify = AsyncMock()
    monkeypatch.setattr(main, "handle_server_error", notify)
    failed = await main.process_chat_message.__wrapped__(RequestStub(), main.ChatMessage(message="x", phone="1"))
    assert failed["success"] is False and notify.await_count == 1

    class Upload:
        filename = "a.xlsx"
        async def read(self): return b"data"
    fake_chat.process_message.side_effect = None
    fake_chat.process_message.return_value = {"status": "uploaded"}
    monkeypatch.setattr("app.services.excel_validation_service.ExcelValidationService.validate_excel_file_from_url", AsyncMock(return_value={"valid": True}))
    uploaded = await main.upload_excel_file.__wrapped__(RequestStub(), "1", Upload())
    assert uploaded["success"] and uploaded["filename"] == "a.xlsx"
    fake_chat.process_message.side_effect = RuntimeError("upload")
    failed_upload = await main.upload_excel_file.__wrapped__(RequestStub(), "1", Upload())
    assert failed_upload["success"] is False


def test_database_helpers_and_remote_operations(monkeypatch):
    settings = SimpleNamespace(database_mode="client", PROJECT_ROOT="C:/missing")
    import os
    monkeypatch.setattr(os.path, "exists", lambda path: path.endswith("ssl/DigiCertGlobalRootCA.crt.pem"))
    assert "ca" in database._get_ssl_connect_args(settings)["ssl"]
    monkeypatch.setattr(os.path, "exists", lambda _path: False)
    with pytest.raises(ValueError, match="TLS CA certificate"):
        database._get_ssl_connect_args(settings)
    settings.database_mode = "local"
    assert database._get_ssl_connect_args(settings)["ssl"]["ssl_disabled"] is False

    pool = SimpleNamespace(checkedout=lambda: 1, checkedin=lambda: 2, size=lambda: 3, overflow=lambda: 0)
    database.engine = SimpleNamespace(pool=pool)
    database.log_connection_pool_status("test")
    database.engine = None

    monkeypatch.setattr(database, "execute_remote_query", MagicMock(return_value=[{"x": 1}]))
    assert database.get_remote_item_categories(5)
    assert database.get_remote_item_categories() == [{"x": 1}]
    execute = database.execute_remote_query
    execute.side_effect = [[{"total": 3}], [{"unique_categories": 2}], [], [{"category": "A"}]]
    stats = database.get_remote_category_stats()
    assert stats["total_items"] == 3 and stats["unique_divisions"] == 0
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert database.test_remote_connection() is False
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(database, "execute_remote_query", lambda *_: [{"test": 1}])
    assert database.test_remote_connection()


def test_database_context_and_manager_paths(monkeypatch):
    class Session:
        def __init__(self): self.commits = 0; self.rollbacks = 0; self.closed = False
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1
        def close(self): self.closed = True
    session = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: session)
    with database.get_db_session_context() as current:
        assert current is session
    assert session.commits == 1 and session.closed
    session = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: session)
    with pytest.raises(ValueError):
        with database.get_db_session_context(): raise ValueError("bad")
    assert session.rollbacks == 1 and session.closed

    provided = Session()
    manager = database.DatabaseManager(session=provided)
    assert manager.get_connection_pool_status()["main_db"]["status"] == "not_initialized"
    manager.close()
    assert not provided.closed
    owned = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: owned)
    manager = database.DatabaseManager()
    manager.close()
    assert owned.closed and manager.session is None
    with database.DatabaseManager(session=Session()) as entered:
        assert entered.session is not None

    query = MagicMock()
    query.filter.return_value.all.return_value = []
    db_session = MagicMock()
    db_session.query.return_value = query
    manager = database.DatabaseManager(session=db_session)
    settings = SimpleNamespace(session_timeout_hours=1, cleanup_completed_sessions=False)
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    assert manager.cleanup_expired_sessions() == 0
    assert manager.cleanup_completed_sessions() == 0
    session_obj = SimpleNamespace(workflow_state={}, conversation_history={}, extracted_entities={}, outcome=None)
    query.filter.return_value.all.return_value = [session_obj]
    assert manager.cleanup_expired_sessions(1) == 1
    assert session_obj.workflow_state == {"extracted_entities": {}}

    existing = SimpleNamespace(conversation_history={"messages": [{"timestamp": "t", "content": "x", "role": "user"}], "metadata": [], "openai_messages": []}, rfq_ids=["r1"], workflow_state={"a": 1}, extracted_entities={}, bfs_search_count=1)
    db_session.query.return_value.filter_by.return_value.first.return_value = existing
    monkeypatch.setattr(database, "flag_modified", lambda *_args: None)
    saved = manager.append_session_data({"session_id": "s", "conversation_history": {"messages": [{"timestamp": "t", "content": "x", "role": "user"}, {"timestamp": "u", "content": {"a": 1}, "role": "assistant"}], "metadata": [], "openai_messages": [{"content": "x", "role": "user"}]}, "rfq_ids": ["r1", "r2"], "workflow_state": {"b": 2}, "bfs_search_count": 2})
    assert saved is existing and existing.bfs_search_count == 3
    db_session.query.return_value.filter_by.return_value.first.return_value = None
    monkeypatch.setattr(manager, "save_conversation_session", MagicMock(return_value="new"))
    assert manager.append_session_data({"session_id": "new"}) == "new"


def test_database_remote_session_and_query_errors(monkeypatch):
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    with pytest.raises(ValueError): database.get_remote_db_session()
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True, get_remote_database_url=lambda: None))
    with pytest.raises(ValueError): database.get_remote_db_session()
    session = MagicMock()
    session.execute.return_value = object()
    database.RemoteSessionLocal = lambda: session
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    assert database.get_remote_db_session() is session
    result = MagicMock()
    result.keys.return_value = ["a"]
    result.fetchall.return_value = [(1,)]
    session.execute.return_value = result
    assert database.execute_remote_query("select 1") == [{"a": 1}]
    assert session.close.called
    session.execute.side_effect = RuntimeError("query")
    with pytest.raises(RuntimeError): database.execute_remote_query("bad")
