"""Data cached from one GMT API must never be served once the app points at another.

Redis outlives deployments, so buyers and logins cached while the app talked to
the dev backend kept being served after it was pointed at production.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.main as main
import app.redis_db as redis_db
import app.services.user_cache_service as ucs
from app.config import api_source_tag

PROD = "https://quaservicesp.procucev.com"
DEV = "https://p2pv1servicesdev.example.net"


def test_api_source_tag_uses_the_host():
    assert api_source_tag(PROD + "/rest/x") == "quaservicesp.procucev.com"
    assert api_source_tag("HTTPS://Host.Example") == "host.example"
    assert api_source_tag("not-a-url") == "not-a-url"
    assert api_source_tag(None) == "unconfigured"
    assert api_source_tag("") == "unconfigured"


def _cache_service(monkeypatch, cached):
    monkeypatch.setattr(ucs, "get_settings", lambda: SimpleNamespace(gmt_base_url=PROD))
    redis = SimpleNamespace(get=AsyncMock(return_value=cached), set=AsyncMock(return_value=True))
    service = ucs.UserCacheService.__new__(ucs.UserCacheService)
    service.redis_service = redis
    return service, redis


@pytest.mark.asyncio
async def test_user_cache_tags_entries_with_the_current_api(monkeypatch):
    service, redis = _cache_service(monkeypatch, None)

    assert await service.store_user_data("+91 99", [{"username": "buyer"}])

    assert redis.set.await_args.args[1]["source_api"] == "quaservicesp.procucev.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_api", [api_source_tag(DEV), None])
async def test_user_cache_ignores_entries_from_another_or_unknown_api(monkeypatch, source_api):
    cached = {"user_data": [{"username": "dev-buyer"}]}
    if source_api:
        cached["source_api"] = source_api
    service, _ = _cache_service(monkeypatch, cached)

    assert await service.get_user_data("9199") is None


@pytest.mark.asyncio
async def test_user_cache_serves_entries_from_the_current_api(monkeypatch):
    cached = {"user_data": [{"username": "buyer"}], "source_api": api_source_tag(PROD)}
    service, _ = _cache_service(monkeypatch, cached)

    assert await service.get_user_data("9199") == [{"username": "buyer"}]


class _FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    async def exists(self, key):
        return int(key in self.values)

    async def expire(self, key, seconds):
        return key in self.values


def _auth_service(base_url):
    auth = redis_db.AuthRedisService()
    auth.client = _FakeRedis()
    auth.settings = SimpleNamespace(redis_expiry_seconds=30, gmt_base_url=base_url)
    return auth


@pytest.mark.asyncio
async def test_login_from_the_current_api_is_kept(monkeypatch):
    monkeypatch.setattr(redis_db.User, "from_mixed_data", lambda data: SimpleNamespace(data=data))
    auth = _auth_service(PROD)

    assert await auth.store("9199", {"id": "u"})

    assert await auth.is_authenticated("9199")
    assert (await auth.retrieve("9199")).data["id"] == "u"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [{"id": "u", "source_api": api_source_tag(DEV)}, {"id": "u"}])
async def test_login_from_another_or_unknown_api_is_discarded(stored):
    auth = _auth_service(PROD)
    auth.client.values["auth:9199"] = json.dumps(stored)

    assert await auth.retrieve("9199") is None
    assert "auth:9199" not in auth.client.values
    auth.client.values["auth:9199"] = json.dumps(stored)
    assert not await auth.is_authenticated("9199")


def test_api_token_cache_key_is_scoped_to_the_api_host(monkeypatch):
    import app.procucev_apis.procucev_api_client as client_mod

    for base_url, host in ((PROD, "quaservicesp.procucev.com"), (DEV, "p2pv1servicesdev.example.net")):
        settings = SimpleNamespace(
            gmt_base_url=base_url, gmt_username="u", gmt_password="p", gmt_phone="1",
            gmt_client_id="c", gmt_client_secret="s", gmt_max_retries=1, gmt_retry_delay=0,
        )
        monkeypatch.setattr(client_mod, "get_settings", lambda s=settings: s)
        monkeypatch.setattr(client_mod, "get_redis_service", lambda: SimpleNamespace())
        assert client_mod.ProcucevAPIClient().token_cache_key == f"procucev_api:auth_token:{host}"


def test_chat_debug_info_reports_the_backends_in_use(monkeypatch):
    settings = SimpleNamespace(
        gmt_base_url=PROD,
        database_mode="client",
        client_database_url="mysql+pymysql://user:secret@db.example.com:3306/aiprocprod",
        local_database_url=None,
        procucev_db_name="quaproduction",
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    assert main._data_source() == {
        "gmt_api": PROD,
        "database": "db.example.com/aiprocprod",
        "procucev_schema": "quaproduction",
    }

    settings.gmt_base_url = None
    settings.database_mode = "local"
    settings.local_database_url = "mysql+pymysql://u:p@localhost:3306/procurement_db"
    described = main._data_source()
    assert described["gmt_api"] == "not configured"
    assert described["database"] == "localhost/procurement_db"
    assert "secret" not in json.dumps(described)
