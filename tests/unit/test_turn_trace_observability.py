"""
Unit tests for app.utils.turn_trace.

The module exists to answer one operational question: when a WhatsApp reply is
slow, which part was slow, and did the delay happen before or after the message
reached this process. The tests below pin that behaviour down, including the
degraded paths, because tracing must never be the reason a reply fails.
"""

import logging
import time
from datetime import datetime, timedelta

import pytest

from app.utils import turn_trace as tt


@pytest.fixture(autouse=True)
def _clear_turn():
    """Every test starts and ends with no ambient turn."""
    tt.set_current_turn(None)
    yield
    tt.set_current_turn(None)


# ---------------------------------------------------------------- timestamps

def test_parse_gateway_timestamp_applies_the_gateway_offset():
    # The gateway stamps local (IST) time with no offset while the container
    # logs UTC, so the parsed value must be shifted back by the offset.
    parsed = tt.parse_gateway_timestamp("2026-08-22 12:53:54")
    assert parsed == datetime(2026, 8, 22, 12, 53, 54) - timedelta(
        minutes=tt.GATEWAY_UTC_OFFSET_MINUTES
    )


@pytest.mark.parametrize(
    "raw",
    ["2026-08-22 12:53:54", "2026-08-22 12:53:54.125300", "2026-08-22T12:53:54"],
)
def test_parse_gateway_timestamp_accepts_every_observed_format(raw):
    assert tt.parse_gateway_timestamp(raw) is not None


def test_parse_gateway_timestamp_passes_datetime_through():
    moment = datetime(2026, 8, 22, 1, 2, 3)
    assert tt.parse_gateway_timestamp(moment) is moment


def test_parse_gateway_timestamp_accepts_epoch_numbers():
    assert tt.parse_gateway_timestamp(0) == datetime(1970, 1, 1)
    assert tt.parse_gateway_timestamp(1.5) == datetime(1970, 1, 1, 0, 0, 1, 500000)


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-timestamp", 10 ** 30])
def test_parse_gateway_timestamp_returns_none_rather_than_raising(raw):
    # A malformed timestamp is worth losing the lag metric over, never the message.
    assert tt.parse_gateway_timestamp(raw) is None


def test_gateway_lag_seconds_measures_delay_before_the_app_saw_the_message():
    sent_local = datetime(2026, 8, 22, 12, 53, 54)
    arrived_utc = sent_local - timedelta(minutes=tt.GATEWAY_UTC_OFFSET_MINUTES) + timedelta(seconds=43)
    assert tt.gateway_lag_seconds("2026-08-22 12:53:54", now=arrived_utc) == pytest.approx(43.0)


def test_gateway_lag_seconds_is_none_without_a_usable_timestamp():
    assert tt.gateway_lag_seconds(None) is None


def test_gateway_lag_seconds_defaults_to_now():
    assert tt.gateway_lag_seconds(tt._utcnow()) == pytest.approx(0, abs=5)


# ------------------------------------------------------------------ TurnTrace

def _trace(**kwargs):
    defaults = {"turn_id": "abc123", "user_phone": "919", "source": "test"}
    defaults.update(kwargs)
    return tt.TurnTrace(**defaults)


def test_elapsed_and_user_perceived_include_the_gateway_lag():
    trace = _trace(gateway_lag=40.0)
    trace.started_perf = time.perf_counter() - 2.0
    assert trace.elapsed == pytest.approx(2.0, abs=0.5)
    # The user starts waiting when they press send, not when the webhook lands.
    assert trace.user_perceived == pytest.approx(42.0, abs=0.5)


def test_user_perceived_is_unknown_without_a_gateway_lag():
    assert _trace().user_perceived is None


def test_timeline_renders_recorded_stages_in_milliseconds():
    trace = _trace()
    assert trace.timeline() == "no-stages"
    trace.record("classify_intent", 0.25)
    trace.record("send", 0.1)
    assert trace.timeline() == "classify_intent=250ms send=100ms"


def test_mark_records_an_offset_and_annotate_reaches_the_summary(caplog):
    trace = _trace()
    with caplog.at_level(logging.DEBUG, logger="app.utils.turn_trace"):
        trace.mark("queued", depth=2)
    assert trace.marks[0][0] == "queued"
    assert "mark=queued" in caplog.text and "depth=2" in caplog.text

    trace.annotate(intent="greeting")
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        trace.finish()
    assert "intent=greeting" in caplog.text


def test_finish_logs_one_summary_and_is_idempotent(caplog):
    trace = _trace(gateway_lag=1.0)
    trace.record("send", 0.05)
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        trace.finish("complete")
        trace.finish("complete")
    summaries = [r for r in caplog.records if "SUMMARY" in r.message]
    # Several exit paths converge on the same turn; it must be summarised once.
    assert len(summaries) == 1
    assert trace.finished is True
    assert "gateway_lag=1.000s" in summaries[0].message
    assert "send=50ms" in summaries[0].message


def test_finish_reports_unknown_lag_when_the_timestamp_was_unusable(caplog):
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        _trace().finish()
    assert "gateway_lag=unknown" in caplog.text
    assert "user_perceived=unknown" in caplog.text


@pytest.mark.parametrize("outcome", ["error", "dropped", "no_reply"])
def test_finish_escalates_outcomes_where_the_user_got_nothing(caplog, outcome):
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        _trace().finish(outcome)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_finish_warns_when_the_user_waited_too_long(caplog):
    trace = _trace(gateway_lag=tt.SLOW_TURN_SECONDS + 1)
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        trace.finish("complete")
    assert "SLOW_TURN" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_finish_warns_on_slow_in_app_time_even_without_a_lag(caplog):
    trace = _trace()
    trace.started_perf = time.perf_counter() - (tt.SLOW_TURN_SECONDS + 1)
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        trace.finish("complete")
    assert "SLOW_TURN" in caplog.text


# ------------------------------------------------------- start / stamp / resume

def test_new_turn_id_is_short_and_unique():
    first, second = tt.new_turn_id(), tt.new_turn_id()
    assert len(first) == 8 and first != second


def test_start_turn_becomes_the_current_turn_and_truncates_the_preview():
    trace = tt.start_turn("+919808494950", source="webhook:text", message_preview="x" * 200)
    assert tt.current_turn() is trace
    assert tt.current_turn_id() == trace.turn_id
    assert len(trace.message_preview) == 80


def test_start_turn_warns_when_the_delay_happened_upstream(caplog):
    # Formatted the way the gateway actually sends it: its own local clock.
    stamped_45s_ago = (
        tt._utcnow()
        + timedelta(minutes=tt.GATEWAY_UTC_OFFSET_MINUTES)
        - timedelta(seconds=45)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        tt.start_turn("919", source="webhook:text", gateway_timestamp=stamped_45s_ago)
    # This is the cold-start signature: the message was already ~45s old on arrival.
    assert "HIGH_GATEWAY_LAG" in caplog.text


def test_start_turn_accepts_an_explicit_id_and_a_blank_phone():
    trace = tt.start_turn("", source="s", turn_id="fixed123")
    assert trace.turn_id == "fixed123"
    assert trace.user_phone == "unknown"


def test_current_turn_id_is_a_dash_when_no_turn_is_active():
    assert tt.current_turn_id() == "-"
    assert tt.current_turn() is None


def test_stamp_turn_writes_the_correlation_keys_onto_the_payload():
    trace = tt.start_turn("919", source="webhook:text")
    payload = {"from": "919", "content": "hi"}
    assert tt.stamp_turn(payload) == trace.turn_id
    assert payload[tt.TURN_ID_FIELD] == trace.turn_id
    assert payload[tt.RECEIVED_AT_FIELD] == trace.started_at


def test_stamp_turn_keeps_an_id_the_payload_already_carries():
    payload = {tt.TURN_ID_FIELD: "existing1"}
    # A webhook retried by the gateway must stay on one turn id, not fragment.
    assert tt.stamp_turn(payload, _trace()) == "existing1"
    assert isinstance(payload[tt.RECEIVED_AT_FIELD], float)


def test_stamp_turn_invents_an_id_when_there_is_no_turn():
    payload = {}
    stamped = tt.stamp_turn(payload)
    assert stamped and payload[tt.TURN_ID_FIELD] == stamped


def test_resume_turn_rewinds_the_clock_to_when_the_webhook_arrived():
    payload = {
        tt.TURN_ID_FIELD: "carried1",
        tt.RECEIVED_AT_FIELD: time.time() - 5.0,
        "from": "+919808494950",
        "content": "hi",
    }
    trace = tt.resume_turn(payload, source="batch")
    assert trace.turn_id == "carried1"
    assert trace.user_phone == "919808494950"
    # in_app latency must span the queue hop, not restart at zero after it.
    assert trace.elapsed == pytest.approx(5.0, abs=0.5)
    assert tt.current_turn() is trace


def test_resume_turn_starts_fresh_when_the_payload_was_never_stamped():
    trace = tt.resume_turn({"from": "919"}, source="batch")
    assert trace.turn_id and trace.elapsed == pytest.approx(0, abs=0.5)


def test_resume_turn_prefers_an_explicit_phone_and_tolerates_a_bad_receipt_time():
    trace = tt.resume_turn(
        {tt.RECEIVED_AT_FIELD: "not-a-number"}, source="batch", user_phone="918"
    )
    assert trace.user_phone == "918"
    assert trace.elapsed == pytest.approx(0, abs=0.5)


# ------------------------------------------------------------ context handling

def test_set_and_reset_restore_the_previous_turn():
    outer = tt.start_turn("919", source="outer")
    token = tt.set_current_turn(_trace(turn_id="inner"))
    assert tt.current_turn_id() == "inner"
    tt.reset_current_turn(token)
    assert tt.current_turn() is outer


def test_reset_current_turn_clears_when_the_token_is_foreign():
    import contextvars

    tt.start_turn("919", source="outer")
    # A token minted in another context cannot be reset here; clearing is the
    # safe outcome rather than propagating a ValueError into a live turn.
    foreign = contextvars.copy_context().run(lambda: tt.set_current_turn(_trace()))
    tt.reset_current_turn(foreign)
    assert tt.current_turn() is None


def test_bind_turn_reattaches_across_a_task_boundary():
    trace = _trace(turn_id="rebound1")
    assert tt.current_turn() is None
    with tt.bind_turn(trace) as bound:
        assert bound is trace and tt.current_turn_id() == "rebound1"
    assert tt.current_turn() is None


# -------------------------------------------------------------------- stage()

def test_stage_records_its_duration_on_the_current_turn():
    trace = tt.start_turn("919", source="s")
    with tt.stage("classify_intent", model="gpt"):
        pass
    assert trace.stages[0][0] == "classify_intent"


async def test_stage_measures_awaited_work():
    import asyncio

    trace = tt.start_turn("919", source="s")
    with tt.stage("slow_await"):
        await asyncio.sleep(0.05)
    name, duration = trace.stages[0]
    assert name == "slow_await" and duration >= 0.04


def test_stage_warns_when_a_step_is_slow(caplog, monkeypatch):
    monkeypatch.setattr(tt, "SLOW_STAGE_SECONDS", 0.0)
    tt.start_turn("919", source="s")
    with caplog.at_level(logging.DEBUG, logger="app.utils.turn_trace"):
        with tt.stage("upstream_api"):
            pass
    assert "SLOW" in caplog.text


def test_stage_records_a_failure_and_re_raises_untouched(caplog):
    trace = tt.start_turn("919", source="s")
    with caplog.at_level(logging.ERROR, logger="app.utils.turn_trace"):
        with pytest.raises(ValueError, match="boom"):
            with tt.stage("send", to="919"):
                raise ValueError("boom")
    # The timeline must show where the turn died, and tracing must not swallow it.
    assert trace.stages[0][0] == "send!"
    assert "failed after" in caplog.text and "ValueError" in caplog.text


def test_stage_is_harmless_without_an_active_turn(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.utils.turn_trace"):
        with tt.stage("orphan"):
            pass
    assert "[-]" in caplog.text


# ------------------------------------------------------- module-level helpers

def test_module_helpers_apply_to_the_current_turn(caplog):
    trace = tt.start_turn("919", source="s")
    tt.mark("batched", size=3)
    tt.annotate(intent="greeting")
    with caplog.at_level(logging.INFO, logger="app.utils.turn_trace"):
        tt.finish_turn("complete")
    assert trace.marks[0][0] == "batched"
    assert "intent=greeting" in caplog.text
    assert trace.finished is True


def test_module_helpers_are_no_ops_without_a_turn(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.utils.turn_trace"):
        tt.mark("orphan", a=1)
        tt.annotate(b=2)
        tt.finish_turn("complete")
    assert "mark=orphan" in caplog.text
    assert not any("SUMMARY" in r.message for r in caplog.records)


def test_format_fields_renders_nothing_for_empty_metadata():
    assert tt._format_fields({}) == ""
    assert tt._format_fields({"a": 1, "b": "x"}) == " a=1 b=x"
