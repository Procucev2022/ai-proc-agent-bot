"""
Unit tests for the event-loop stall detector and the batch-flush fast path.

Both exist because of the same production finding: replies were slow or missing
and nothing in the logs said why. The monitor makes a blocked event loop visible,
and the flush removes the shared poller tick from the latency path while the
poller stays behind as the safety net.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import message_queue_service as queue_mod
from app.utils import loop_monitor as lm
from app.utils import turn_trace as tt


# ============================================================== loop monitor

def _monitor(**kwargs):
    defaults = {"interval": 0.01, "warn_threshold": 0.05, "error_threshold": 0.5}
    defaults.update(kwargs)
    return lm.EventLoopMonitor(**defaults)


def test_a_new_monitor_reports_a_clean_idle_state():
    monitor = _monitor()
    assert monitor.running is False
    stats = monitor.stats()
    assert stats["samples"] == 0 and stats["stalls"] == 0
    assert stats["worst_stall_seconds"] == 0.0
    assert stats["warn_threshold_seconds"] == 0.05
    assert stats["worker_pid"] == monitor.worker_pid


def test_normal_scheduling_jitter_is_counted_but_not_reported(caplog):
    monitor = _monitor()
    with caplog.at_level(logging.WARNING, logger="app.utils.loop_monitor"):
        monitor._record(0.001)
    assert monitor.samples == 1
    assert monitor.stalls == 0
    assert caplog.text == ""


def test_a_stall_is_reported_with_the_blast_radius_spelled_out(caplog):
    monitor = _monitor()
    with caplog.at_level(logging.WARNING, logger="app.utils.loop_monitor"):
        monitor._record(0.2)
    assert monitor.stalls == 1
    assert monitor.worst_stall == pytest.approx(0.2)
    assert monitor.total_stalled == pytest.approx(0.2)
    # The message has to say that *other* users were stalled too, because that
    # is the non-obvious part of the failure mode.
    assert "Event loop blocked" in caplog.text
    assert "batch poller" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_a_long_stall_is_escalated_to_error(caplog):
    monitor = _monitor()
    with caplog.at_level(logging.WARNING, logger="app.utils.loop_monitor"):
        monitor._record(5.0)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_the_worst_stall_is_retained_across_samples():
    monitor = _monitor()
    monitor._record(0.3)
    monitor._record(0.1)
    assert monitor.worst_stall == pytest.approx(0.3)
    assert monitor.stalls == 2


async def test_run_samples_the_loop_then_exits_when_stopped():
    monitor = _monitor(interval=0.01)
    task = monitor.start()
    await asyncio.sleep(0.06)
    assert monitor.running is True
    monitor._running = False
    await asyncio.sleep(0.03)
    await monitor.stop()
    assert task.done()
    assert monitor.samples >= 1
    assert monitor.running is False


async def test_run_measures_the_overshoot_not_the_sleep(monkeypatch):
    # An asyncio.sleep that returns late is exactly what a blocked loop looks
    # like from inside the monitor, so the recorded value must be the excess.
    monitor = _monitor(interval=1.0, warn_threshold=0.05)
    ticks = {"n": 0}
    perf = {"t": 0.0}

    async def fake_sleep(_seconds):
        ticks["n"] += 1
        perf["t"] += 1.4  # 1.0s requested, 1.4s actual -> 0.4s overshoot
        if ticks["n"] >= 2:
            monitor._running = False

    monkeypatch.setattr(lm.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(lm.time, "perf_counter", lambda: perf["t"])

    await monitor.run()
    assert monitor.stalls >= 1
    assert monitor.worst_stall == pytest.approx(0.4, abs=0.01)


async def test_run_re_raises_cancellation(caplog):
    monitor = _monitor(interval=5)
    task = monitor.start()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert monitor.running is False


async def test_start_reuses_a_live_task():
    monitor = _monitor(interval=5)
    first = monitor.start()
    assert monitor.start() is first
    await monitor.stop()


async def test_stop_is_safe_when_nothing_is_running():
    monitor = _monitor()
    await monitor.stop()  # no task at all
    monitor._task = MagicMock(done=MagicMock(return_value=True))
    await monitor.stop()  # task already finished
    assert monitor.running is False


def test_get_loop_monitor_is_a_singleton_configured_from_the_environment(monkeypatch):
    monkeypatch.setitem(lm._singleton, "monitor", None)
    monkeypatch.setenv("LOOP_MONITOR_INTERVAL_SECONDS", "3")
    monkeypatch.setenv("LOOP_MONITOR_WARN_SECONDS", "0.75")
    monkeypatch.setenv("LOOP_MONITOR_ERROR_SECONDS", "4")

    monitor = lm.get_loop_monitor()
    assert monitor.interval == 3.0
    assert monitor.warn_threshold == 0.75
    assert monitor.error_threshold == 4.0
    assert lm.get_loop_monitor() is monitor


# ========================================================= batch flush timing

class FlushRedis:
    """Redis stub exposing only what the flush path touches."""

    def __init__(self, pttl_values):
        self.pttl_values = list(pttl_values)
        self.pttl_calls = 0

    async def pttl(self, _key):
        self.pttl_calls += 1
        if self.pttl_values:
            return self.pttl_values.pop(0)
        return -2


def _service(redis=None, **attrs):
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = redis or FlushRedis([])
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    for key, value in attrs.items():
        setattr(service, key, value)
    return service


def test_private_attributes_are_never_delegated_to_whatsapp_service():
    # The regression this guards: __getattr__ forwarded a missing `_running` to
    # WhatsAppService, so the batch poller died with a misleading AttributeError
    # and that worker stopped answering text messages for the rest of its life.
    service = _service()
    with pytest.raises(AttributeError, match="private attributes are never delegated"):
        service._not_a_real_attribute


def test_class_defaults_keep_a_hand_built_instance_usable():
    service = _service()
    assert service.poll_interval > 0
    assert service.max_flush_waits >= 1
    assert service._running is True


def test_flush_tasks_is_per_instance_and_never_hits_getattr():
    first, second = _service(), _service()
    first.flush_tasks["919"] = "sentinel"
    assert second.flush_tasks == {}


async def test_flush_waits_out_the_window_then_creates_the_batch():
    # 40ms left on the window, then the key is gone: one wait, then the batch.
    redis = FlushRedis([40, -2])
    service = _service(redis, max_flush_waits=5)
    service._create_batch = AsyncMock()

    await service._flush_after_window("919")

    service._create_batch.assert_awaited_once_with("919")
    assert redis.pttl_calls == 2


async def test_flush_creates_the_batch_immediately_when_the_window_is_closed():
    service = _service(FlushRedis([-2]), max_flush_waits=5)
    service._create_batch = AsyncMock()
    await service._flush_after_window("919")
    service._create_batch.assert_awaited_once()


async def test_flush_honours_a_window_extended_by_a_follow_up_message():
    # Each pttl read returns a fresh deadline, mimicking a user still typing.
    redis = FlushRedis([10, 10, 10, -2])
    service = _service(redis, max_flush_waits=10)
    service._create_batch = AsyncMock()
    await service._flush_after_window("919")
    assert redis.pttl_calls == 4
    service._create_batch.assert_awaited_once()


async def test_flush_stops_re_arming_so_a_user_cannot_defer_their_own_reply(caplog):
    # A user typing every window would otherwise wait forever.
    redis = FlushRedis([5] * 50)
    service = _service(redis, max_flush_waits=3)
    service._create_batch = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="app.services.message_queue_service"):
        await service._flush_after_window("919")

    assert redis.pttl_calls == 3
    assert "answering now to bound the wait" in caplog.text
    service._create_batch.assert_awaited_once()


async def test_a_failed_flush_falls_back_to_the_poller_instead_of_raising(caplog):
    service = _service(FlushRedis([-2]), max_flush_waits=2)
    service._create_batch = AsyncMock(side_effect=RuntimeError("redis down"))

    with caplog.at_level(logging.ERROR, logger="app.services.message_queue_service"):
        await service._flush_after_window("919")  # must not raise

    assert "falling back to the" in caplog.text


async def test_flush_cancellation_propagates_for_clean_shutdown():
    service = _service(FlushRedis([1000]), max_flush_waits=5)
    service._create_batch = AsyncMock()
    task = asyncio.create_task(service._flush_after_window("919"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service._create_batch.assert_not_awaited()


async def test_scheduling_is_idempotent_per_user():
    service = _service(FlushRedis([1000] * 5), max_flush_waits=5)
    service._create_batch = AsyncMock()

    service._schedule_batch_flush("919")
    first = service.flush_tasks["919"]
    service._schedule_batch_flush("919")
    # A second message extends the window in Redis; it must not arm a second task.
    assert service.flush_tasks["919"] is first

    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


async def test_a_finished_flush_task_is_forgotten():
    service = _service(FlushRedis([-2]), max_flush_waits=2)
    service._create_batch = AsyncMock()
    service._schedule_batch_flush("919")
    await asyncio.gather(*service.flush_tasks.values(), return_exceptions=True)
    await asyncio.sleep(0)
    assert "919" not in service.flush_tasks


def test_scheduling_without_a_running_loop_defers_to_the_poller(caplog):
    service = _service(FlushRedis([]))
    with caplog.at_level(logging.WARNING, logger="app.services.message_queue_service"):
        service._schedule_batch_flush("919")
    assert "batch poller will pick it up" in caplog.text
    assert service.flush_tasks == {}


# ==================================================== poller resilience

class PollerRedis:
    def __init__(self, incoming=(), outgoing=(), processing=(), trigger=()):
        self.incoming = list(incoming)
        self.outgoing = list(outgoing)
        self.processing = set(processing)
        self.trigger = set(trigger)
        self.scanned = []

    async def scan(self, cursor=0, match=None, count=100):
        self.scanned.append(match)
        keys = self.incoming if match == "*:incoming" else self.outgoing
        return 0, list(keys)

    async def exists(self, key):
        return 1 if key in self.processing or key in self.trigger else 0

    async def zcard(self, _key):
        return 1

    async def llen(self, _key):
        return 2


async def test_a_poll_cycle_creates_batches_for_users_whose_window_closed():
    redis = PollerRedis(incoming=["919:incoming"])
    service = _service(redis)
    service._create_batch = AsyncMock()
    service._try_start_processing = AsyncMock()

    assert await service._poll_once() == 1
    service._create_batch.assert_awaited_once_with("919")


async def test_a_poll_cycle_skips_users_still_inside_their_window():
    redis = PollerRedis(incoming=["919:incoming"], trigger={"919:batch_trigger"})
    service = _service(redis)
    service._create_batch = AsyncMock()
    service._try_start_processing = AsyncMock()

    assert await service._poll_once() == 0
    service._create_batch.assert_not_awaited()


async def test_a_poll_cycle_recovers_batches_that_were_built_but_never_answered(caplog):
    # Nothing used to scan {phone}:outgoing, so a batch stranded there was lost
    # for good and that user simply never got a reply.
    redis = PollerRedis(outgoing=["918:outgoing"])
    service = _service(redis)
    service._create_batch = AsyncMock()
    service._try_start_processing = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="app.services.message_queue_service"):
        assert await service._poll_once() == 1

    assert "*:outgoing" in redis.scanned
    service._try_start_processing.assert_awaited_once_with("918")
    assert "stranded batch" in caplog.text


async def test_a_poll_cycle_leaves_a_user_alone_while_their_batch_is_in_flight():
    redis = PollerRedis(outgoing=["918:outgoing"], processing={"918:processing"})
    service = _service(redis)
    service._create_batch = AsyncMock()
    service._try_start_processing = AsyncMock()

    assert await service._poll_once() == 0
    service._try_start_processing.assert_not_awaited()


async def test_the_poller_survives_a_redis_outage_and_keeps_polling(caplog):
    # The production failure: one escaped exception killed the loop permanently
    # and that worker never answered another text message.
    service = _service(MagicMock())
    cycles = {"n": 0}

    async def failing_poll():
        cycles["n"] += 1
        if cycles["n"] >= 3:
            service._running = False
        raise ConnectionError("Error 111 connecting to redis-dev:6379")

    service._poll_once = failing_poll

    lock = SimpleNamespace(acquire=AsyncMock(return_value=True), release=AsyncMock())
    service.redis.lock = MagicMock(return_value=lock)
    service.poll_interval = 0.001

    with caplog.at_level(logging.ERROR, logger="app.services.message_queue_service"):
        await service.run_batch_poller()

    assert cycles["n"] == 3, "the loop must keep running after a failed cycle"
    assert "poller still running" in caplog.text


async def test_the_poller_yields_the_cycle_to_whichever_worker_holds_the_lock():
    service = _service(MagicMock())
    service.poll_interval = 0.001
    service._poll_once = AsyncMock()

    calls = {"n": 0}

    async def acquire():
        calls["n"] += 1
        if calls["n"] >= 2:
            service._running = False
        return False

    lock = SimpleNamespace(acquire=acquire, release=AsyncMock())
    service.redis.lock = MagicMock(return_value=lock)

    await service.run_batch_poller()
    service._poll_once.assert_not_awaited()


async def test_the_poller_releases_the_lock_even_when_a_cycle_fails():
    service = _service(MagicMock())
    service.poll_interval = 0.001
    release = AsyncMock()

    async def poll_then_stop():
        service._running = False
        raise RuntimeError("boom")

    service._poll_once = poll_then_stop
    lock = SimpleNamespace(acquire=AsyncMock(return_value=True), release=release)
    service.redis.lock = MagicMock(return_value=lock)

    await service.run_batch_poller()
    release.assert_awaited()


async def test_the_poller_exits_quietly_when_asked_to_stop():
    service = _service(MagicMock())
    service._running = False
    await service.run_batch_poller()  # returns without touching Redis


async def test_scan_keys_walks_the_cursor_to_completion():
    class TwoPageRedis:
        def __init__(self):
            self.calls = 0

        async def scan(self, cursor=0, match=None, count=100):
            self.calls += 1
            if self.calls == 1:
                return 7, ["a:incoming"]
            return 0, ["b:incoming"]

    service = _service(TwoPageRedis())
    assert await service._scan_keys("*:incoming") == ["a:incoming", "b:incoming"]


# =============================================== correlation across the queue

def test_a_message_exposes_the_turn_id_the_webhook_stamped():
    message = queue_mod.Message(
        message_id="m1",
        user_phone="919",
        content="hi",
        message_type="text",
        timestamp=1.0,
        webhook_data={tt.TURN_ID_FIELD: "turn0001", tt.RECEIVED_AT_FIELD: 123.5},
    )
    assert message.turn_id == "turn0001"
    assert message.received_at == 123.5


def test_a_message_without_correlation_keys_degrades_to_placeholders():
    message = queue_mod.Message("m", "919", "hi", "text", 1.0, {})
    assert message.turn_id == "-"
    assert message.received_at is None


def test_a_batch_carries_the_correlation_forward_for_tracing():
    batch = queue_mod.Batch(
        batch_id="b1",
        user_phone="919",
        concatenated_content="hi",
        message_type="text",
        message_count=1,
        created_at=2.0,
        turn_ids=["turn0001", "turn0002"],
        received_at=1.0,
    )
    assert batch.turn_id == "turn0001"
    payload = batch.as_trace_payload()
    assert payload[tt.TURN_ID_FIELD] == "turn0001"
    assert payload[tt.RECEIVED_AT_FIELD] == 1.0
    assert payload["from"] == "919"


def test_a_batch_from_an_older_revision_still_loads():
    batch = queue_mod.Batch.from_dict(
        {
            "batch_id": "b1",
            "user_phone": "919",
            "concatenated_content": "hi",
            "message_type": "text",
            "message_count": 1,
            "created_at": 2.0,
        }
    )
    assert batch.turn_ids == [] and batch.turn_id == "-"


@pytest.mark.parametrize(
    "cls,payload",
    [
        (
            queue_mod.Batch,
            {
                "batch_id": "b",
                "user_phone": "9",
                "concatenated_content": "c",
                "message_type": "text",
                "message_count": 1,
                "created_at": 1.0,
                "a_field_from_the_future": True,
            },
        ),
        (
            queue_mod.Message,
            {
                "message_id": "m",
                "user_phone": "9",
                "content": "c",
                "message_type": "text",
                "timestamp": 1.0,
                "webhook_data": {},
                "a_field_from_the_future": True,
            },
        ),
    ],
)
def test_unknown_fields_are_dropped_so_a_rolling_deploy_cannot_strand_messages(cls, payload):
    # Both records round-trip through Redis, so one revision reads what another
    # wrote. Without this an added field would raise TypeError on every queued
    # message the older revision picked up, and those replies would be lost.
    assert cls.from_dict(payload) is not None


# ============================================ outbound WhatsApp HTTP transport

from app.services import whatsapp_service as wa_mod  # noqa: E402  (grouped with its tests)


@pytest.fixture
def _reset_gateway():
    """Leave the shared session table untouched for other tests."""
    previous = dict(wa_mod._gateway)
    wa_mod._gateway.update({"session": None, "loop": None})
    yield
    wa_mod._gateway.update(previous)


def test_gateway_response_parses_its_body_like_the_old_requests_object():
    response = wa_mod.GatewayResponse(status_code=200, text='{"mid": "abc"}')
    assert response.json() == {"mid": "abc"}
    assert response.status_code == 200


class _FakeClientSession:
    """Stands in for aiohttp.ClientSession without opening a socket."""

    def __init__(self, status=200, body='{"mid": "m1"}'):
        self.closed = False
        self.status = status
        self.body = body
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        session = self

        class _Ctx:
            async def __aenter__(self):
                return SimpleNamespace(
                    status=session.status,
                    text=AsyncMock(return_value=session.body),
                )

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()

    async def close(self):
        self.closed = True


async def test_the_gateway_session_is_created_once_and_then_reused(_reset_gateway, monkeypatch):
    created = []

    def factory(**kwargs):
        session = _FakeClientSession()
        created.append(session)
        return session

    monkeypatch.setattr(wa_mod.aiohttp, "ClientSession", factory)

    first = await wa_mod.get_gateway_session()
    second = await wa_mod.get_gateway_session()
    # Reuse is the point: a fresh session per message would add a TCP and TLS
    # handshake to every reply.
    assert first is second
    assert len(created) == 1


async def test_a_session_bound_to_a_dead_loop_is_replaced(_reset_gateway, monkeypatch):
    monkeypatch.setattr(wa_mod.aiohttp, "ClientSession", lambda **kw: _FakeClientSession())
    first = await wa_mod.get_gateway_session()
    # Simulate the session having been built on a previous event loop.
    wa_mod._gateway["loop"] = object()
    second = await wa_mod.get_gateway_session()
    assert second is not first


async def test_a_closed_session_is_replaced(_reset_gateway, monkeypatch):
    monkeypatch.setattr(wa_mod.aiohttp, "ClientSession", lambda **kw: _FakeClientSession())
    first = await wa_mod.get_gateway_session()
    first.closed = True
    assert await wa_mod.get_gateway_session() is not first


async def test_closing_the_session_clears_the_shared_slot(_reset_gateway, monkeypatch, caplog):
    monkeypatch.setattr(wa_mod.aiohttp, "ClientSession", lambda **kw: _FakeClientSession())
    session = await wa_mod.get_gateway_session()

    with caplog.at_level(logging.INFO, logger="app.services.whatsapp_service"):
        await wa_mod.close_gateway_session()

    assert session.closed is True
    assert wa_mod._gateway["session"] is None
    assert "session closed" in caplog.text


async def test_closing_is_a_no_op_when_there_is_no_session(_reset_gateway):
    await wa_mod.close_gateway_session()
    assert wa_mod._gateway["session"] is None


async def test_a_close_failure_is_logged_and_swallowed(_reset_gateway, monkeypatch, caplog):
    session = _FakeClientSession()
    session.close = AsyncMock(side_effect=RuntimeError("already gone"))
    wa_mod._gateway.update({"session": session, "loop": asyncio.get_running_loop()})

    with caplog.at_level(logging.WARNING, logger="app.services.whatsapp_service"):
        await wa_mod.close_gateway_session()  # shutdown must not raise

    assert "Error closing pooled HTTP session" in caplog.text


async def test_post_to_gateway_returns_the_body_and_logs_its_timing(_reset_gateway, monkeypatch, caplog):
    session = _FakeClientSession(status=200, body='{"mid": "m9"}')
    monkeypatch.setattr(wa_mod, "get_gateway_session", AsyncMock(return_value=session))

    with caplog.at_level(logging.INFO, logger="app.services.whatsapp_service"):
        response = await wa_mod.post_to_gateway("http://wa/sessioncomm", {"user": "u"})

    assert isinstance(response, wa_mod.GatewayResponse)
    assert response.status_code == 200
    assert response.json() == {"mid": "m9"}
    assert session.calls[0][0] == "http://wa/sessioncomm"
    assert session.calls[0][1] == {"user": "u"}
    assert session.calls[0][2] == {"Content-Type": "application/json"}
    # This is the last hop before the user sees the reply, so it must be timed.
    assert "POST http://wa/sessioncomm -> 200" in caplog.text


async def test_post_to_gateway_reports_a_gateway_error_status(_reset_gateway, monkeypatch):
    session = _FakeClientSession(status=503, body="unavailable")
    monkeypatch.setattr(wa_mod, "get_gateway_session", AsyncMock(return_value=session))
    response = await wa_mod.post_to_gateway("http://wa/sessioncomm", {})
    assert response.status_code == 503
    assert response.text == "unavailable"
