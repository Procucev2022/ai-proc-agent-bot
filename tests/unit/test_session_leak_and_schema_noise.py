"""
Regression tests for three faults found in the 2026-08 production log analysis.

1. `DatabaseManager` checked a connection out of the pool in `__init__`, so the
   twenty-five call sites that build one speculatively -- `ExitService(...,
   db_manager=None)` and `CancelService(..., db_manager=None)` construct one per
   turn -- each took a connection and leaked it. 205 "garbage collected with
   unclosed session" warnings, still occurring in the newest deployed revision.

2. A missing `conversation_sessions` table produced three stacked SQLAlchemy
   ERROR tracebacks plus a wasted retry for every single message, 217 in total.
   The table is absent because the database user is read-only, so this is a
   permanent environment fact rather than an event worth re-reporting.

3. `enqueue_message` parsed the gateway timestamp with one hard-coded format.
   Anything else -- including the microsecond form in the gateway's own
   documentation -- raised out of the enqueue and the message was dropped, so the
   user received no reply at all.

Every collaborator is mocked; nothing here opens a database, Redis or a socket.
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import database
from app.services import message_queue_service as queue_mod


# ============================================================ 1. session leak

@pytest.fixture
def pooled_session(monkeypatch):
    """A stand-in for a pooled session, plus a count of how many were handed out."""
    handed_out = []

    def factory():
        session = MagicMock(name=f"session-{len(handed_out)}")
        handed_out.append(session)
        return session

    monkeypatch.setattr(database, "get_db_session", factory)
    return handed_out


def test_constructing_a_manager_takes_nothing_out_of_the_pool(pooled_session):
    # The leak was structural: every speculative construction cost a connection.
    manager = database.DatabaseManager()
    assert pooled_session == []
    assert manager.has_session is False


def test_the_connection_is_taken_only_when_the_session_is_actually_used(pooled_session):
    manager = database.DatabaseManager()
    first = manager.session
    assert len(pooled_session) == 1
    # Repeated access reuses it rather than checking out another.
    assert manager.session is first
    assert len(pooled_session) == 1
    assert manager.has_session is True


def test_an_unused_manager_is_collected_without_a_leak_warning(pooled_session, caplog):
    manager = database.DatabaseManager()
    with caplog.at_level(logging.WARNING, logger="app.database"):
        database.DatabaseManager.__del__(manager)
    assert "unclosed session" not in caplog.text


def test_a_used_manager_that_was_not_closed_still_warns(pooled_session, caplog):
    manager = database.DatabaseManager()
    session = manager.session  # force the checkout
    with caplog.at_level(logging.WARNING, logger="app.database"):
        database.DatabaseManager.__del__(manager)
    # The warning must survive: it is the only signal that a real connection leaked.
    assert "unclosed session" in caplog.text
    session.close.assert_called_once()


def test_a_provided_session_is_never_closed_by_the_manager(pooled_session):
    provided = MagicMock()
    manager = database.DatabaseManager(session=provided)
    assert manager.has_session is True
    assert pooled_session == []  # nothing taken from the pool
    manager.close()
    provided.close.assert_not_called()


def test_the_context_manager_closes_a_session_it_created(pooled_session):
    with database.DatabaseManager() as manager:
        session = manager.session
    session.close.assert_called_once()
    assert manager.has_session is False


def test_the_context_manager_does_not_suppress_exceptions(pooled_session):
    with pytest.raises(ValueError):
        with database.DatabaseManager() as manager:
            manager.session
            raise ValueError("propagate me")


def test_the_session_can_still_be_replaced_directly(pooled_session):
    manager = database.DatabaseManager()
    replacement = MagicMock()
    manager.session = replacement
    assert manager.session is replacement
    assert pooled_session == []


# =================================================== 2. missing-table reporting

@pytest.mark.parametrize(
    "message",
    [
        "(pymysql.err.ProgrammingError) (1146, \"Table 'dev.conversation_sessions' doesn't exist\")",
        "no such table: conversation_sessions",
        "Error 1146 while executing query",
    ],
)
def test_a_missing_table_is_recognised_across_drivers(message):
    assert database._is_missing_table_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    ["Lost connection to MySQL server", "Duplicate entry 'x' for key 'PRIMARY'", ""],
)
def test_other_database_errors_are_not_mistaken_for_a_missing_table(message):
    assert database._is_missing_table_error(Exception(message)) is False


def test_a_missing_table_is_reported_once_per_operation_then_quietened(caplog, monkeypatch):
    monkeypatch.setattr(database, "_missing_table_reported", set())
    exc = Exception("Table 'dev.conversation_sessions' doesn't exist")

    with caplog.at_level(logging.DEBUG, logger="app.database"):
        database._report_missing_table("save_conversation_session", exc)
        database._report_missing_table("save_conversation_session", exc)
        database._report_missing_table("get_conversation_session", exc)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # One per operation, not one per message: this replaced 217 stacked errors.
    assert len(warnings) == 2
    # The first report has to say what to do about it.
    assert "session history is not being persisted" in warnings[0].message
    assert "grant" in warnings[0].message
    assert any(r.levelno == logging.DEBUG and "still absent" in r.message for r in caplog.records)


def _manager_with_failing_query(monkeypatch, error_message):
    """A manager whose first query raises the given database error."""
    monkeypatch.setattr(database, "_missing_table_reported", set())
    session = MagicMock()
    session.query.side_effect = SQLAlchemyError(error_message)
    manager = database.DatabaseManager(session=session)
    return manager, session


MISSING = "(pymysql.err.ProgrammingError) (1146, \"Table 'dev.conversation_sessions' doesn't exist\")"


def test_get_conversation_session_returns_none_without_an_error_traceback(monkeypatch, caplog):
    manager, session = _manager_with_failing_query(monkeypatch, MISSING)
    with caplog.at_level(logging.DEBUG, logger="app.database"):
        assert manager.get_conversation_session("whatsapp_919_20260824") is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    session.rollback.assert_called_once()


def test_a_real_database_error_on_read_is_still_reported_as_an_error(monkeypatch, caplog):
    manager, _ = _manager_with_failing_query(monkeypatch, "Lost connection to MySQL server")
    with caplog.at_level(logging.ERROR, logger="app.database"):
        assert manager.get_conversation_session("sid") is None
    assert "Database error in get_conversation_session" in caplog.text


def test_save_falls_back_to_an_unsaved_object_without_an_error_traceback(monkeypatch, caplog):
    manager, session = _manager_with_failing_query(monkeypatch, MISSING)
    with caplog.at_level(logging.DEBUG, logger="app.database"):
        result = manager.save_conversation_session(
            {"session_id": "whatsapp_919_20260824", "external_user_id": "919"}
        )
    # The conversation must continue from Redis, so an object is still returned.
    assert result.session_id == "whatsapp_919_20260824"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    session.rollback.assert_called_once()


def test_append_does_not_retry_through_save_when_the_table_is_absent(monkeypatch, caplog):
    manager, _ = _manager_with_failing_query(monkeypatch, MISSING)
    manager.save_conversation_session = MagicMock(name="save")

    with caplog.at_level(logging.DEBUG, logger="app.database"):
        result = manager.append_session_data(
            {"session_id": "whatsapp_919_20260824", "external_user_id": "919"}
        )

    # Saving instead of appending cannot conjure the table, and the old order
    # produced an error here, another from save, and a third from save's retry.
    manager.save_conversation_session.assert_not_called()
    assert result.session_id == "whatsapp_919_20260824"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ================================================= 3. gateway timestamp parsing

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-18 18:38:15", datetime(2026, 8, 18, 18, 38, 15)),
        # The form in the gateway's own documentation, which used to raise.
        ("2025-09-13 13:54:22.125300", datetime(2025, 9, 13, 13, 54, 22, 125300)),
        ("2026-08-18T18:38:15", datetime(2026, 8, 18, 18, 38, 15)),
        ("2026-08-18T18:38:15.500000", datetime(2026, 8, 18, 18, 38, 15, 500000)),
    ],
)
def test_every_gateway_timestamp_shape_is_understood(raw, expected):
    assert queue_mod.MessageQueueService._parse_timestamp(raw) == expected.timestamp()


def test_sub_second_precision_is_preserved():
    # The dedupe key is built from this value. Truncating to whole seconds made
    # two genuinely different messages sent in the same second collide, and the
    # second was then silently dropped for 24 hours.
    a = queue_mod.MessageQueueService._parse_timestamp("2026-08-18 18:38:15.100000")
    b = queue_mod.MessageQueueService._parse_timestamp("2026-08-18 18:38:15.900000")
    assert a != b


@pytest.mark.parametrize("raw", [1787403234.5, 1787403234])
def test_a_numeric_timestamp_passes_straight_through(raw):
    assert queue_mod.MessageQueueService._parse_timestamp(raw) == float(raw)


@pytest.mark.parametrize("raw", ["garbage", "", "   ", None])
def test_an_unreadable_timestamp_never_costs_the_message(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.message_queue_service"):
        value = queue_mod.MessageQueueService._parse_timestamp(raw)
    # A timestamp is metadata. Raising here dropped the message and left the user
    # with no reply, which is far worse than an approximate arrival time.
    assert isinstance(value, float) and value > 0
    assert "Unrecognised gateway timestamp" in caplog.text


async def test_a_message_with_a_microsecond_timestamp_is_still_queued():
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        zadd=AsyncMock(),
        exists=AsyncMock(return_value=False),
        setex=AsyncMock(),
    )
    service.whatsapp_service = SimpleNamespace()
    service._refresh_batch_timer = AsyncMock()
    service._schedule_batch_flush = MagicMock()

    await service.enqueue_message({
        "from": "+919808494950",
        "type": "text",
        "content": "hi",
        "timestamp": "2025-09-13 13:54:22.125300",
        "message_id": "NA",
    })

    service.redis.zadd.assert_awaited_once()
    service._schedule_batch_flush.assert_called_once_with("919808494950")
