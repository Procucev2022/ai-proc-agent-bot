"""Regression tests for the shared OpenAI client lifecycle.

Production bug: ChatService.cleanup() (run after every message) and the
categorization paths called close()/close_sync(), which closed the AsyncOpenAI
client every concurrent request shared. Other users' in-flight calls then failed
with APIConnectionError("Connection error.") and fell back to rule-based
handling. These tests pin the fix: requests never close the shared client, each
event loop gets its own client, and clients are closed only at shutdown.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import openai_service as svc


class FakeClient:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    async def close(self):
        self.close_calls += 1
        self.closed = True


def bare_service(client=None):
    service = svc.OpenAIService.__new__(svc.OpenAIService)
    service._client = client
    service._new_client = FakeClient
    return service


async def _get():
    return svc.get_shared_openai_client(FakeClient)


def test_same_loop_reuses_one_client():
    async def run():
        return await _get(), await _get()

    a, b = asyncio.run(run())
    assert a is b


def test_different_loops_get_different_clients():
    # Celery tasks use asyncio.run(); each run is a new loop and must not reuse
    # a client whose connection pool is bound to an earlier loop.
    assert asyncio.run(_get()) is not asyncio.run(_get())


def test_no_running_loop_client_is_reused_and_recreated_when_closed():
    a = svc.get_shared_openai_client(FakeClient)
    assert svc.get_shared_openai_client(FakeClient) is a
    a.closed = True
    assert svc.get_shared_openai_client(FakeClient) is not a


def test_closed_client_in_loop_is_replaced():
    async def run():
        a = await _get()
        a.closed = True
        return a, await _get()

    a, b = asyncio.run(run())
    assert b is not a


def test_client_without_is_closed_is_treated_as_open():
    plain = SimpleNamespace(close=AsyncMock())
    assert svc._is_closed(plain) is False
    assert svc._is_closed(MagicMock()) is False  # mock is_closed() is not literally True
    assert svc.get_shared_openai_client(lambda: plain) is plain
    assert svc.get_shared_openai_client(FakeClient) is plain


def test_service_property_uses_shared_client_and_injected_client_wins():
    async def run():
        first, second = bare_service(), bare_service()
        shared = first.client
        injected = FakeClient()
        return shared, second.client, bare_service(injected).client, injected

    shared, second, injected_result, injected = asyncio.run(run())
    assert second is shared
    assert injected_result is injected


def test_one_request_closing_does_not_break_another():
    """The original bug: one request's close closed another's live client."""

    async def run():
        request_a, request_b = bare_service(), bare_service()
        shared = request_a.client
        assert request_b.client is shared
        request_a.close_sync()
        await request_a.close()
        async with request_a:
            pass
        return shared, request_b.client

    shared, still_used = asyncio.run(run())
    assert shared.is_closed() is False and shared.close_calls == 0
    assert still_used is shared


@pytest.mark.asyncio
async def test_chat_service_cleanup_after_a_message_leaves_shared_client_open():
    from app.services.chat_service import ChatService

    in_flight = bare_service()
    shared = in_flight.client
    chat = ChatService.__new__(ChatService)
    chat._openai_service = bare_service()
    assert chat._openai_service.client is shared

    await chat.cleanup()

    assert shared.close_calls == 0
    assert in_flight.client is shared


def test_close_all_closes_clients_at_shutdown():
    async def run():
        client = await _get()
        await svc.close_all_openai_clients()
        return client

    client = asyncio.run(run())
    assert client.is_closed()
    assert svc._client_without_loop is None


def test_close_all_also_closes_no_loop_client_and_skips_closed_ones():
    no_loop = svc.get_shared_openai_client(FakeClient)
    asyncio.run(svc.close_all_openai_clients())
    assert no_loop.close_calls == 1

    already_closed = svc.get_shared_openai_client(FakeClient)
    already_closed.closed = True
    asyncio.run(svc.close_all_openai_clients())
    assert already_closed.close_calls == 0


def test_close_all_without_running_loop_closes_no_loop_client():
    no_loop = svc.get_shared_openai_client(FakeClient)
    coroutine = svc.close_all_openai_clients()
    # Drive the coroutine by hand so no event loop is running inside it.
    with pytest.raises(StopIteration):
        coroutine.send(None)
    assert no_loop.close_calls == 1


def test_close_all_logs_and_continues_when_close_fails(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(svc, "logger", logger)
    failing = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("boom")))
    svc._client_without_loop = failing

    asyncio.run(svc.close_all_openai_clients())

    failing.close.assert_awaited_once()
    logger.warning.assert_called_once()
    assert svc._client_without_loop is None


def test_new_client_is_built_through_shared_getter(monkeypatch):
    built = MagicMock()
    monkeypatch.setattr(svc, "AsyncOpenAI", lambda **kwargs: built)
    service = svc.OpenAIService.__new__(svc.OpenAIService)
    service._client = None

    async def run():
        return service.client, service.client

    first, second = asyncio.run(run())
    assert first is built and second is built


def _api_connection_error(cause):
    import httpx
    from openai import APIConnectionError

    request = httpx.Request("POST", "https://example.invalid/openai/v1/responses")
    try:
        try:
            raise cause
        except Exception as inner:
            raise APIConnectionError(request=request) from inner
    except APIConnectionError as exc:
        return exc


def test_describe_root_cause_names_the_underlying_error():
    exc = _api_connection_error(OSError("Name or service not known"))
    assert svc._describe_root_cause(exc) == " | cause=OSError: Name or service not known"
    assert svc._describe_root_cause(RuntimeError("plain")) == ""


def test_discard_shared_client_forgets_only_that_client():
    async def run():
        broken = await _get()
        svc._discard_shared_client(broken)
        fresh = await _get()
        svc._discard_shared_client(object())  # unknown client: nothing changes
        return broken, fresh, await _get()

    broken, fresh, again = asyncio.run(run())
    assert fresh is not broken and again is fresh
    assert broken.close_calls == 0  # never closed: in-flight calls may still use it

    no_loop = svc.get_shared_openai_client(FakeClient)
    svc._discard_shared_client(no_loop)
    assert svc._client_without_loop is None


def _instrumented_with(error):
    # A plain coroutine, not AsyncMock: a mock reports every attribute (including
    # the "already instrumented" marker) as truthy, so it would never be wrapped.
    async def create(**_kwargs):
        raise error

    raw = SimpleNamespace(responses=SimpleNamespace(create=create))
    return svc.OpenAIService._instrument_client(raw)


def test_connection_error_logs_cause_and_replaces_shared_client(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(svc, "logger", logger)
    error = _api_connection_error(ConnectionResetError("peer reset"))

    async def run():
        client = svc.get_shared_openai_client(
            lambda: _instrumented_with(error)
        )
        with pytest.raises(type(error)):
            await client.responses.create(model="m", input="x")
        return client, svc.get_shared_openai_client(FakeClient)

    failed, replacement = asyncio.run(run())
    assert replacement is not failed
    message = logger.error.call_args[0][0]
    assert "APIConnectionError" in message and "cause=ConnectionResetError: peer reset" in message


def test_non_connection_error_keeps_shared_client(monkeypatch):
    monkeypatch.setattr(svc, "logger", MagicMock())

    async def run():
        client = svc.get_shared_openai_client(
            lambda: _instrumented_with(ValueError("bad request"))
        )
        with pytest.raises(ValueError):
            await client.responses.create(model="m", input="x")
        return client, svc.get_shared_openai_client(FakeClient)

    client, same = asyncio.run(run())
    assert same is client
