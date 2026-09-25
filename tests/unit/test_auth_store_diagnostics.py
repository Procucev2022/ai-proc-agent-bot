"""
AuthRedisService.store must log why a login could not be stored. A verified
profile selection otherwise fails with no reply and no visible cause.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import redis_db


def _auth_service(monkeypatch, enabled):
    settings = SimpleNamespace(redis_session_storage_enabled=enabled, redis_expiry_seconds=60)
    monkeypatch.setattr(redis_db, "get_settings", lambda: settings)
    return redis_db.AuthRedisService()


@pytest.mark.asyncio
async def test_store_logs_when_session_storage_is_disabled(monkeypatch, caplog):
    auth = _auth_service(monkeypatch, enabled=False)
    auth.set = AsyncMock()

    with caplog.at_level(logging.ERROR, logger=redis_db.logger.name):
        assert await auth.store("919511876403", {"id": "u"}) is False

    auth.set.assert_not_awaited()
    assert "REDIS_SESSION_STORAGE_ENABLED=false" in caplog.text
    assert "919511876403" in caplog.text


@pytest.mark.asyncio
async def test_store_logs_when_redis_rejects_the_write(monkeypatch, caplog):
    auth = _auth_service(monkeypatch, enabled=True)
    auth.set = AsyncMock(return_value=False)

    with caplog.at_level(logging.ERROR, logger=redis_db.logger.name):
        assert await auth.store("919511876403", {"id": "u"}) is False

    auth.set.assert_awaited_once_with("auth:919511876403", {"id": "u"}, 60)
    assert "Redis did not store login for 919511876403" in caplog.text
    assert "client initialized: False" in caplog.text


@pytest.mark.asyncio
async def test_store_success_logs_nothing(monkeypatch, caplog):
    auth = _auth_service(monkeypatch, enabled=True)
    auth.set = AsyncMock(return_value=True)

    with caplog.at_level(logging.ERROR, logger=redis_db.logger.name):
        assert await auth.store("919511876403", {"id": "u"}, expiry_seconds=7) is True

    assert "[AUTH-STORE]" not in caplog.text
