from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.welcome_message_service import WelcomeMessageService
from app.utils.procucev_api_logger import ProcucevAPILogger, log_procucev_api_call, manual_log_api_call


@pytest.mark.asyncio
async def test_welcome_message_redis_and_send_paths(monkeypatch, tmp_path):
    redis = AsyncMock()
    service = WelcomeMessageService.__new__(WelcomeMessageService)
    service.redis_service = redis
    service.welcome_flag_prefix = "welcome_msg"
    assert service._get_welcome_key("+91 (987)-10") == "welcome_msg:9198710"
    redis.exists.return_value = True
    assert not await service.should_send_welcome("1")
    redis.exists.return_value = False
    assert await service.should_send_welcome("1")
    redis.exists.side_effect = RuntimeError("redis")
    assert await service.should_send_welcome("1")
    redis.exists.side_effect = None
    redis.expireat.return_value = True
    assert await service.mark_welcome_sent("1")
    redis.expireat.return_value = False
    assert not await service.mark_welcome_sent("1")
    redis.set.side_effect = RuntimeError("set")
    assert not await service.mark_welcome_sent("1")
    redis.set.side_effect = None
    redis.delete.return_value = True
    assert await service.reset_welcome_flag("1")
    redis.delete.return_value = False
    assert not await service.reset_welcome_flag("1")
    redis.delete.side_effect = RuntimeError("delete")
    assert not await service.reset_welcome_flag("1")

    redis.delete.side_effect = None
    service.should_send_welcome = AsyncMock(return_value=True)
    service.mark_welcome_sent = AsyncMock()
    whatsapp = AsyncMock()
    whatsapp.send_message.return_value = SimpleNamespace(success=True)
    assert await service.check_and_send_welcome("1", whatsapp)
    whatsapp.send_message.return_value = SimpleNamespace(success=False)
    assert not await service.check_and_send_welcome("1", whatsapp)
    service.should_send_welcome.return_value = False
    assert not await service.check_and_send_welcome("1", whatsapp)
    service.should_send_welcome.side_effect = RuntimeError("send")
    assert not await service.check_and_send_welcome("1", whatsapp)


def test_api_logger_file_and_decorators(tmp_path, monkeypatch):
    logger = ProcucevAPILogger(str(tmp_path / "logs"))
    logger.log_api_call("title", "/path", {"a": 1}, {"ok": True}, 0.123, phone_number="1")
    files = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(files) == 1 and "title" in files[0].read_text(encoding="utf-8")
    failing_open = MagicMock(side_effect=OSError("disk"))
    monkeypatch.setattr("builtins.open", failing_open)
    logger.log_api_call("title", "/path", {}, {}, 0)

    decorated_logger = MagicMock()
    monkeypatch.setattr("app.utils.procucev_api_logger.procucev_api_logger", decorated_logger)

    @log_procucev_api_call("async", "/a")
    async def async_ok(value, **kwargs):
        return value

    @log_procucev_api_call()
    def sync_ok(value):
        return value

    @log_procucev_api_call()
    async def async_bad():
        raise ValueError("bad")

    @log_procucev_api_call()
    def sync_bad():
        raise ValueError("bad")

    import asyncio
    assert asyncio.run(async_ok("ok", user_phone="1")) == "ok"
    assert sync_ok("ok") == "ok"
    with pytest.raises(ValueError):
        asyncio.run(async_bad())
    with pytest.raises(ValueError):
        sync_bad()
    manual_log_api_call("manual", "/m", {}, {}, 0)
    assert decorated_logger.log_api_call.call_count >= 5
