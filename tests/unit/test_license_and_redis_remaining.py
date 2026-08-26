from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.responses import JSONResponse

import app.license as license_pkg
import app.license.license_middleware as license_middleware
import app.redis_db as redis_db


SECRET = "unit-license-secret"
FIXED_NOW = datetime(2025, 1, 15, 12, 0, 0)


def _encoded_license(secret: str = SECRET, *, expires_at: str = "2025-02-15T12:00:00", fields=None):
    values = fields or ["token", "machine", "2025-01-01", expires_at, "31", "v1"]
    plaintext = "|".join(values)
    key = hashlib.sha256(secret.encode()).digest()
    encrypted = bytes(ord(char) ^ key[index % len(key)] for index, char in enumerate(plaintext))
    encoded = base64.b64encode(encrypted).decode()
    signature = hashlib.sha256((encoded + secret).encode()).hexdigest()
    return f"{signature}::{encoded}", encoded


def _settings(**overrides):
    values = {
        "license_secret_key": SECRET,
        "redis_url": "redis://unit-test",
        "redis_expiry_seconds": 120,
        "workflow_timeout_seconds": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(path: str):
    return SimpleNamespace(url=SimpleNamespace(path=path))


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("not-a-license", "License file corrupted"),
        ("0" * 64 + "::%%%", "License data corrupted"),
        (_encoded_license(fields=["a", "b", "c", "2025-02-15T12:00:00", "1"])[0], "License data corrupted"),
        (_encoded_license(fields=["a", "b", "c", "not-a-date", "1", "v1"])[0], "License validation failed"),
    ],
)
def test_validate_license_malformed_and_invalid_data(tmp_path, monkeypatch, content, expected):
    monkeypatch.setattr("app.config.get_settings", lambda: _settings())
    if content == "0" * 64 + "::%%%":
        content = f"{hashlib.sha256(('%%%' + SECRET).encode()).hexdigest()}::%%%"
    license_file = tmp_path / "license.lic"
    license_file.write_text(content, encoding="utf-8")
    valid, message = license_pkg.validate_license(str(license_file))
    assert not valid
    assert expected in message


def test_validate_license_missing_default_and_signature_cases(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: _settings())
    missing = tmp_path / "missing.lic"
    assert license_pkg.validate_license(str(missing)) == (
        False,
        "License file not found. Please contact Admin.",
    )

    with patch.object(license_pkg.os.path, "join", return_value=str(missing)) as join:
        assert license_pkg.validate_license() [0] is False
        join.assert_called_once()

    content, _ = _encoded_license()
    signature, encoded = content.split("::", 1)
    bad_signature = tmp_path / "bad-signature.lic"
    bad_signature.write_text(f"{'f' * len(signature)}::{encoded}", encoding="utf-8")
    valid, message = license_pkg.validate_license(str(bad_signature))
    assert not valid and "signature invalid" in message


def test_validate_license_expired_soon_and_valid(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.get_settings", lambda: _settings())
    monkeypatch.setattr(license_pkg, "datetime", Mock(
        now=Mock(return_value=FIXED_NOW),
        fromisoformat=datetime.fromisoformat,
    ))

    expired, _ = _encoded_license(expires_at="2025-01-13T12:00:00")
    expired_file = tmp_path / "expired.lic"
    expired_file.write_text(expired, encoding="utf-8")
    valid, message = license_pkg.validate_license(str(expired_file))
    assert not valid and "2 days ago" in message

    soon, _ = _encoded_license(expires_at="2025-01-20T12:00:00")
    soon_file = tmp_path / "soon.lic"
    soon_file.write_text(soon, encoding="utf-8")
    assert license_pkg.validate_license(str(soon_file)) == (
        True,
        "License valid but expires soon in 5 days. Please renew.",
    )

    normal, _ = _encoded_license(expires_at="2025-02-15T12:00:00")
    normal_file = tmp_path / "normal.lic"
    normal_file.write_text(normal, encoding="utf-8")
    assert license_pkg.validate_license(str(normal_file)) == (True, "License valid.")


def test_validate_license_settings_and_file_errors(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.get_settings", Mock(side_effect=RuntimeError("settings unavailable")))
    valid, message = license_pkg.validate_license(str(tmp_path / "license.lic"))
    assert not valid and "settings unavailable" in message

    monkeypatch.setattr("app.config.get_settings", lambda: _settings())
    license_file = tmp_path / "license.lic"
    license_file.write_text("0" * 64 + "::payload", encoding="utf-8")
    with patch.object(license_pkg, "open", side_effect=OSError("read failed")):
        valid, message = license_pkg.validate_license(str(license_file))
    assert not valid and "read failed" in message


@pytest.mark.asyncio
async def test_license_middleware_helpers_file_cases_and_dispatch(tmp_path, monkeypatch):
    content, encoded = _encoded_license(fields=["token", "machine", "created", "2025-02-15T12:00:00", "31", "v1"])
    monkeypatch.setattr(license_middleware, "get_settings", Mock(return_value=_settings()))
    standalone = Mock(return_value=(False, "startup invalid"))
    monkeypatch.setattr(license_middleware, "_validate_license_standalone", standalone)
    middleware = license_middleware.LicenseMiddleware(object(), str(tmp_path / "license.lic"))
    standalone.assert_called_once_with(str(tmp_path / "license.lic"))
    with patch.object(license_middleware, "_validate_license_standalone", return_value=(True, "startup valid")) as valid_startup:
        license_middleware.LicenseMiddleware(object(), str(tmp_path / "valid.lic"))
    valid_startup.assert_called_once_with(str(tmp_path / "valid.lic"))

    assert middleware._decrypt_license_data(encoded) == "token|machine|created|2025-02-15T12:00:00|31|v1"
    with patch.object(license_middleware.base64, "b64decode", side_effect=ValueError("bad crypto")):
        with pytest.raises(ValueError, match="bad crypto"):
            middleware._decrypt_license_data(encoded)

    monkeypatch.setattr(license_middleware.os.path, "exists", Mock(return_value=False))
    assert middleware._read_license_file() is None

    monkeypatch.setattr(license_middleware.os.path, "exists", Mock(return_value=True))
    monkeypatch.setattr(license_middleware, "open", MagicMock(), raising=False)
    license_middleware.open.return_value.__enter__.return_value.read.return_value = content
    parsed = middleware._read_license_file()
    assert parsed == {
        "token": "token",
        "machine_id": "machine",
        "created_at": "created",
        "expires_at": "2025-02-15T12:00:00",
        "validity_days": 31,
        "version": "v1",
    }

    for malformed in [
        "malformed",
        "f" * 64 + "::" + encoded,
    ]:
        license_middleware.open.return_value.__enter__.return_value.read.return_value = malformed
        assert middleware._read_license_file() is None

    bad_parts, _ = _encoded_license(fields=["a", "b", "c", "d", "e"])
    license_middleware.open.return_value.__enter__.return_value.read.return_value = bad_parts
    assert middleware._read_license_file() is None
    bad_days, _ = _encoded_license(fields=["a", "b", "c", "d", "not-int", "v1"])
    license_middleware.open.return_value.__enter__.return_value.read.return_value = bad_days
    assert middleware._read_license_file() is None
    license_middleware.open.side_effect = OSError("file read failed")
    assert middleware._read_license_file() is None
    license_middleware.open.side_effect = None
    with patch.object(middleware, "_decrypt_license_data", side_effect=RuntimeError("decrypt failed")):
        license_middleware.open.return_value.__enter__.return_value.read.return_value = content
        assert middleware._read_license_file() is None

    delegated = Mock(return_value=(True, "delegated"))
    monkeypatch.setattr(license_middleware, "_validate_license_standalone", delegated)
    assert middleware.validate_license() == (True, "delegated")
    delegated.assert_called_with(str(tmp_path / "license.lic"))

    call_next = AsyncMock(return_value=JSONResponse({"ok": True}))
    assert (await middleware.dispatch(_request("/docs/anything"), call_next)).status_code == 200
    call_next.assert_awaited_once()

    monkeypatch.setattr(middleware, "validate_license", Mock(return_value=(False, "expired")))
    denied = await middleware.dispatch(_request("/private"), call_next)
    assert denied.status_code == 403
    assert json.loads(denied.body) == {
        "error": "License Expired",
        "message": "expired",
        "action": "Please contact Admin to renew your license",
    }
    assert call_next.await_count == 1

    monkeypatch.setattr(middleware, "validate_license", Mock(return_value=(True, "valid")))
    assert (await middleware.dispatch(_request("/private"), call_next)).status_code == 200
    assert call_next.await_count == 2


class RedisClient:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.scan_results = [(0, [])]
        self.fail_method = None

    async def set(self, key, value, **kwargs):
        self.calls.append(("set", key, value, kwargs))
        if self.fail_method == "set":
            raise RuntimeError("set failed")
        self.values[key] = value
        return True

    async def get(self, key):
        self.calls.append(("get", key))
        if self.fail_method == "get":
            raise RuntimeError("get failed")
        return self.values.get(key)

    async def delete(self, *keys):
        self.calls.append(("delete", keys))
        if self.fail_method == "delete":
            raise RuntimeError("delete failed")
        count = 0
        for key in keys:
            count += key in self.values
            self.values.pop(key, None)
        return count

    async def exists(self, key):
        if self.fail_method == "exists":
            raise RuntimeError("exists failed")
        return int(key in self.values)

    async def ttl(self, key):
        if self.fail_method == "ttl":
            raise RuntimeError("ttl failed")
        return 45 if key in self.values else -2

    async def expire(self, key, seconds):
        if self.fail_method == "expire":
            raise RuntimeError("expire failed")
        self.calls.append(("expire", key, seconds))
        return key in self.values

    async def expireat(self, key, timestamp):
        if self.fail_method == "expireat":
            raise RuntimeError("expireat failed")
        self.calls.append(("expireat", key, timestamp))
        return key in self.values

    async def incr(self, key):
        if self.fail_method == "incr":
            raise RuntimeError("incr failed")
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def scan(self, cursor, **kwargs):
        if self.fail_method == "scan":
            raise RuntimeError("scan failed")
        self.calls.append(("scan", cursor, kwargs))
        return self.scan_results.pop(0)


@pytest.mark.asyncio
async def test_redis_manager_pool_creation_reuse_and_failure(monkeypatch):
    redis_db.AsyncRedisConnectionManager._pool = None
    factory = AsyncMock(return_value=object())
    monkeypatch.setattr(redis_db, "get_settings", Mock(return_value=_settings()))
    monkeypatch.setattr(redis_db.aioredis, "from_url", factory)

    client = await redis_db.AsyncRedisConnectionManager.get_client()
    assert await redis_db.AsyncRedisConnectionManager.get_client() is client
    factory.assert_awaited_once_with(
        "redis://unit-test",
        decode_responses=True,
        max_connections=20,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        health_check_interval=30,
        socket_keepalive=True,
        retry_on_timeout=True,
    )

    redis_db.AsyncRedisConnectionManager._pool = None
    factory.side_effect = RuntimeError("connection failed")
    with pytest.raises(RuntimeError, match="connection failed"):
        await redis_db.AsyncRedisConnectionManager.get_client()


@pytest.mark.asyncio
async def test_base_redis_operations_success_serialization_and_scan():
    service = redis_db.BaseRedisService()
    client = RedisClient()
    service.client = client

    assert await service.set("dict", {"x": 1}, ex=30)
    assert json.loads(client.values["dict"]) == {"x": 1}
    assert await service.set("zero", "value", ex=0)
    assert client.calls[-1] == ("set", "zero", "value", {})
    assert await service.get("dict", as_json=True) == {"x": 1}
    assert await service.get("missing", as_json=True) is None
    client.values["empty"] = ""
    assert await service.get("empty", as_json=True) == ""

    assert await service.delete("dict")
    assert not await service.delete("missing")
    client.values["exists"] = "1"
    assert await service.exists("exists")
    assert await service.ttl("exists") == 45
    assert await service.expire("exists", 5)
    assert await service.expireat("exists", 123)
    assert await service.incr("counter") == 1

    client.values.update({"a:1": "1", "a:2": "2"})
    client.scan_results = [(1, ["a:1"]), (0, ["a:2"])]
    assert await service.delete_pattern("a:*") == 2
    client.scan_results = [(0, [])]
    assert await service.delete_pattern("none:*") == 0
    assert [call[0] for call in client.calls].count("scan") >= 3


@pytest.mark.asyncio
async def test_base_redis_operations_errors_and_lazy_initialization(monkeypatch):
    service = redis_db.BaseRedisService()
    client = RedisClient()
    service.client = client

    client.fail_method = "set"
    assert not await service.set("x", "y")
    client.fail_method = "get"
    assert await service.get("x") is None
    client.fail_method = "delete"
    assert not await service.delete("x")
    client.fail_method = "exists"
    assert not await service.exists("x")
    client.fail_method = "ttl"
    assert await service.ttl("x") is None
    client.fail_method = "expire"
    assert not await service.expire("x", 1)
    client.fail_method = "expireat"
    assert not await service.expireat("x", 1)
    client.fail_method = "incr"
    assert await service.incr("x") is None
    client.fail_method = "scan"
    assert await service.delete_pattern("*") == 0

    failing_init = redis_db.BaseRedisService()
    monkeypatch.setattr(redis_db.AsyncRedisConnectionManager, "get_client", AsyncMock(side_effect=RuntimeError("pool")))
    with pytest.raises(RuntimeError, match="pool"):
        await failing_init.init_client()
    assert failing_init.client is None

    bad_json = redis_db.BaseRedisService()
    bad_json.client = RedisClient()
    bad_json.client.values["bad"] = "not json"
    assert await bad_json.get("bad", as_json=True) is None
    bad_value = redis_db.BaseRedisService()
    bad_value.client = RedisClient()
    assert not await bad_value.set("bad", {"bad": {1, 2}})


@pytest.mark.asyncio
async def test_base_redis_operations_disabled_storage_and_none_client(monkeypatch):
    disabled = redis_db.BaseRedisService()
    disabled.settings = SimpleNamespace(redis_session_storage_enabled=False)
    await disabled.init_client()
    assert not await disabled.set("k", "v")
    assert await disabled.get("k") is None
    assert not await disabled.delete("k")
    assert not await disabled.exists("k")
    assert await disabled.ttl("k") is None
    assert not await disabled.expire("k", 10)
    assert not await disabled.expireat("k", 100)
    assert await disabled.incr("k") is None
    assert await disabled.delete_pattern("k*") == 0

    no_client = redis_db.BaseRedisService()
    no_client.settings = SimpleNamespace(redis_session_storage_enabled=True)
    monkeypatch.setattr(redis_db.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=None))
    assert not await no_client.set("k", "v")
    assert await no_client.get("k") is None
    assert not await no_client.delete("k")
    assert not await no_client.exists("k")
    assert await no_client.ttl("k") is None
    assert not await no_client.expire("k", 10)
    assert not await no_client.expireat("k", 100)
    assert await no_client.incr("k") is None
    assert await no_client.delete_pattern("k*") == 0


@pytest.mark.asyncio
async def test_auth_service_store_retrieve_refresh_and_errors(monkeypatch):
    monkeypatch.setattr(redis_db, "get_settings", Mock(return_value=_settings(redis_expiry_seconds=99)))
    auth = redis_db.AuthRedisService()
    auth.client = RedisClient()

    assert await auth.store("+1", {"id": "u"})
    assert auth.client.calls[-1][3] == {"ex": 99}
    assert await auth.store("+1", {"id": "u"}, expiry_seconds=7)
    assert await auth.delete_auth("+1")
    assert not await auth.is_authenticated("+1")
    assert await auth.retrieve("missing") is None

    auth.client.values["auth:+1"] = json.dumps({"id": "u"})
    user = SimpleNamespace(id="u")
    monkeypatch.setattr(redis_db.User, "from_mixed_data", Mock(return_value=user))
    refresh = AsyncMock(return_value=True)
    real_refresh = redis_db.AuthRedisService.refresh_user_token.__get__(auth, redis_db.AuthRedisService)
    monkeypatch.setattr(auth, "refresh_user_token", refresh)
    assert await auth.retrieve("+1") is user
    refresh.assert_awaited_once_with("+1")

    monkeypatch.setattr(redis_db.User, "from_mixed_data", Mock(side_effect=ValueError("bad user")))
    monkeypatch.setattr(auth, "refresh_user_token", real_refresh)
    assert await auth.retrieve("+1") is None
    assert not await auth.refresh_user_token("missing")

    auth.client.fail_method = "expire"
    assert not await auth.refresh_user_token("+1")
    auth.client.fail_method = "exists"
    assert not await auth.refresh_user_token("+1")


@pytest.mark.asyncio
async def test_session_service_wrappers_ttl_selection_and_history(monkeypatch):
    monkeypatch.setattr(redis_db, "get_settings", Mock(return_value=_settings(workflow_timeout_seconds=300)))
    session = redis_db.SessionRedisService()
    session.client = RedisClient()
    assert session.default_ttl == 900

    assert await session.store_session("s", {"session_id": "s"}, ttl=0)
    assert session.client.calls[-1][3] == {"ex": 900}
    assert await session.save_session({"session_id": "s", "value": 1}, ttl=4)
    assert not await session.save_session(None)
    assert not await session.save_session({})
    assert not await session.save_session("not-a-dict")
    assert await session.get_session("s") == {"session_id": "s", "value": 1}
    assert await session.refresh_ttl("s", ttl=0)
    assert await session.delete_session("s")
    assert not await session.session_exists("s")
    assert await session.get_session_ttl("s") == -2

    session.client.values["session:h"] = json.dumps({"session_id": "h"})
    fixed = datetime(2025, 1, 15, 12, 30)
    with patch("app.utils.datetime_utils.utc_now", return_value=fixed):
        assert await session.append_message_to_history("h", "user", "hello", "text")
    saved = json.loads(session.client.values["session:h"])
    history = saved["conversation_history"]
    assert history["messages"][0] == {
        "role": "user",
        "content": "hello",
        "timestamp": fixed.isoformat(),
        "message_type": "text",
    }
    assert history["metadata"][0] == {
        "timestamp": fixed.isoformat(),
        "message_type": "text",
        "role": "user",
    }
    assert history["openai_messages"] == [{"role": "user", "content": "hello"}]
    assert saved["last_activity_at"] == fixed.isoformat()

    session.client.values["session:existing"] = json.dumps({
        "session_id": "existing",
        "conversation_history": {"messages": [], "metadata": [], "openai_messages": []},
    })
    session.client.scan_results = [(0, [])]
    with patch("app.utils.datetime_utils.utc_now", return_value=fixed):
        assert await session.append_message_to_history("existing", "assistant", "reply")
    assert not await session.append_message_to_history("unknown", "user", "x")

    session.client.values["session:partial"] = json.dumps({
        "session_id": "partial",
        "conversation_history": {},
    })
    with patch("app.utils.datetime_utils.utc_now", return_value=fixed):
        assert await session.append_message_to_history("partial", "assistant", "reply")
    partial_history = json.loads(session.client.values["session:partial"])["conversation_history"]
    assert partial_history["metadata"] and partial_history["openai_messages"]


@pytest.mark.asyncio
async def test_session_history_failure_and_ttl_fallback(monkeypatch):
    monkeypatch.setattr(redis_db, "get_settings", Mock(return_value=_settings()))
    session = redis_db.SessionRedisService()
    session.client = RedisClient()
    session.client.values["session:bad"] = json.dumps({"session_id": "bad", "conversation_history": None})
    assert not await session.append_message_to_history("bad", "user", "x")

    session.client.values["session:fallback"] = json.dumps({"session_id": "fallback"})
    session.client.fail_method = "ttl"
    assert await session.append_message_to_history("fallback", "user", "x")
    assert session.client.calls[-1][3] == {"ex": session.default_ttl}

    session.client.fail_method = None
    session.client.values["session:write-fail"] = json.dumps({"session_id": "write-fail"})
    original_set = session.set
    monkeypatch.setattr(session, "set", AsyncMock(return_value=False))
    with patch("app.utils.datetime_utils.utc_now", return_value=datetime(2025, 1, 15, 12, 0)):
        assert not await session.append_message_to_history("write-fail", "user", "x")
    monkeypatch.setattr(session, "set", original_set)
    assert await session.store_session("negative", {}, ttl=-1)


@pytest.mark.asyncio
async def test_redis_singletons_are_lazy_and_independent(monkeypatch):
    redis_db._redis_service = None
    redis_db._auth_service = None
    redis_db._session_service = None
    fake_base = object()
    fake_auth = object()
    fake_session = object()
    with patch.object(redis_db, "BaseRedisService", return_value=fake_base) as base_cls, \
            patch.object(redis_db, "AuthRedisService", return_value=fake_auth) as auth_cls, \
            patch.object(redis_db, "SessionRedisService", return_value=fake_session) as session_cls:
        assert redis_db.get_redis_service() is fake_base
        assert redis_db.get_redis_service() is fake_base
        assert redis_db.get_auth_redis_service() is fake_auth
        assert redis_db.get_auth_redis_service() is fake_auth
        assert redis_db.get_session_redis_service() is fake_session
        assert redis_db.get_session_redis_service() is fake_session
    base_cls.assert_called_once_with()
    auth_cls.assert_called_once_with()
    session_cls.assert_called_once_with()
