"""
The ICS WhatsApp gateway posts 'mid=NA&smsgid=NA' on every message, so the
webhook hands MessageQueueService a literal message_id of 'NA' every time.

The deduplication guard claims '<phone>:message:<message_id>' with a 24 hour TTL
and drops the message when the claim already exists. With a constant 'NA' the
first message of the day claimed the key and every message after it was
discarded with "[ENQUEUE] Duplicate message_id='NA', ignored" - the bot accepted
the webhook, answered 200, and never replied.

These tests pin both halves of the contract: a placeholder ID must not collapse
distinct messages, and a genuine gateway retry must still be deduplicated.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.message_queue_service as queue_module


class _NxRedis:
    """Minimal async Redis double that honours SET NX, which the guard relies on."""

    def __init__(self):
        self.store = {}
        self.zadds = []

    async def set(self, key, value, nx=False, ex=None, **kwargs):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def zadd(self, key, mapping):
        self.zadds.append((key, mapping))
        return len(mapping)

    async def exists(self, key):
        return False

    async def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    @property
    def claimed_message_keys(self):
        return sorted(k for k in self.store if ":message:" in k)


def _service(redis):
    service = queue_module.MessageQueueService.__new__(queue_module.MessageQueueService)
    service.redis = redis
    service.batch_window = 5
    service._background_tasks = []
    service._running = True
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    return service


def _payload(content="Hi", timestamp="2026-08-15 16:57:44", message_id="NA"):
    return {
        "type": "text",
        "from": "919808494950",
        "to": "917996170801",
        "timestamp": timestamp,
        "content": content,
        "message_id": message_id,
        "sms_id": "NA",
    }


def test_a_real_gateway_id_is_used_verbatim():
    resolved = queue_module.MessageQueueService._resolve_message_id(
        "wamid.HBgMOTE5", "919808494950", 1.0, "Hi"
    )
    assert resolved == "wamid.HBgMOTE5"


@pytest.mark.parametrize(
    "placeholder", ["NA", "na", "N/A", "", "   ", "-", "NONE", "null", "nil", None]
)
def test_placeholder_ids_are_replaced_with_a_per_message_identity(placeholder):
    resolved = queue_module.MessageQueueService._resolve_message_id(
        placeholder, "919808494950", 1755264464.0, "Hi"
    )
    assert resolved.startswith("919808494950_1755264464.0_")
    assert resolved != str(placeholder)


def test_placeholder_identity_tracks_the_content():
    first = queue_module.MessageQueueService._resolve_message_id("NA", "91980", 1.0, "Hi")
    second = queue_module.MessageQueueService._resolve_message_id("NA", "91980", 1.0, "Hii")
    same = queue_module.MessageQueueService._resolve_message_id("NA", "91980", 1.0, "Hi")

    assert first != second, "different text must not collapse into one dedupe key"
    assert first == same, "identical text at the same instant is the same message"


@pytest.mark.asyncio
async def test_consecutive_messages_with_mid_na_are_all_queued():
    """The regression: three messages arrived, none reached the queue."""
    redis = _NxRedis()
    service = _service(redis)

    await service.enqueue_message(_payload("Hi", "2026-08-15 16:57:44"))
    await service.enqueue_message(_payload("Hi", "2026-08-15 16:58:46"))
    await service.enqueue_message(_payload("Hii", "2026-08-15 16:59:01"))

    assert len(redis.zadds) == 3, "every distinct message must be queued"
    assert len(set(redis.claimed_message_keys)) == 3
    queued = [
        json.loads(next(iter(mapping)))["content"] for _, mapping in redis.zadds
    ]
    assert queued == ["Hi", "Hi", "Hii"]


@pytest.mark.asyncio
async def test_a_reposted_webhook_is_still_deduplicated_without_a_gateway_id():
    redis = _NxRedis()
    service = _service(redis)
    payload = _payload("Hi", "2026-08-15 16:57:44")

    await service.enqueue_message(dict(payload))
    await service.enqueue_message(dict(payload))

    assert len(redis.zadds) == 1, "an identical re-post is a retry, not a new message"


@pytest.mark.asyncio
async def test_a_reposted_webhook_is_deduplicated_by_a_real_gateway_id():
    redis = _NxRedis()
    service = _service(redis)

    await service.enqueue_message(_payload("Hi", message_id="wamid.1"))
    await service.enqueue_message(_payload("edited later", message_id="wamid.1"))

    assert len(redis.zadds) == 1
    assert redis.claimed_message_keys == ["919808494950:message:wamid.1"]
