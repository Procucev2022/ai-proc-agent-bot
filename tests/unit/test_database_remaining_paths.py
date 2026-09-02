"""Deterministic unit coverage for the remaining database module paths."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import app.database as database
from app.models import ConversationOutcome


@pytest.fixture
def main_settings():
    return SimpleNamespace(
        database_mode="local",
        PROJECT_ROOT="C:/project",
        sql_debug=False,
        session_timeout_hours=4,
        cleanup_completed_sessions=True,
        get_database_url=lambda: "mysql://unit-test",
        get_remote_database_url=lambda: "mysql://remote-unit-test",
        enable_remote_categorization=True,
    )


def _patch_error_handler(monkeypatch):
    scheduled = MagicMock()
    monkeypatch.setattr(database.asyncio, "create_task", scheduled)
    monkeypatch.setattr(
        "app.services.global_error_handler.handle_database_error",
        lambda message: message,
    )
    return scheduled


def _save_data():
    return {
        "session_id": "session-1",
        "external_user_id": "user-1",
        "workflow_state": {"stage": "done"},
        "conversation_history": {"messages": []},
        "retention_date": datetime(2025, 1, 1).date(),
        "extracted_entities": {"item": "pump"},
    }


def test_pool_helpers_cover_no_engine_and_pool_failure(monkeypatch):
    monkeypatch.setattr(database, "engine", None)
    database._log_pool_status("without engine")

    monkeypatch.setattr(database, "engine", SimpleNamespace())
    database._log_pool_status("without pool")

    failing_pool = SimpleNamespace(checkedout=lambda: (_ for _ in ()).throw(RuntimeError("pool")))
    monkeypatch.setattr(database, "engine", SimpleNamespace(pool=failing_pool))
    debug = MagicMock()
    monkeypatch.setattr(database.logger, "debug", debug)
    database.log_connection_pool_status("failing pool")
    debug.assert_called_once()


def test_init_database_seeds_when_empty_and_uses_expected_configuration(monkeypatch, main_settings):
    engine = SimpleNamespace(pool=SimpleNamespace())
    db = MagicMock()
    db.query.return_value.count.return_value = 0
    factory = MagicMock(return_value=db)
    create_engine = MagicMock(return_value=engine)
    sessionmaker = MagicMock(return_value=factory)
    create_all = MagicMock()

    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "create_engine", create_engine)
    monkeypatch.setattr(database, "sessionmaker", sessionmaker)
    monkeypatch.setattr(database.Base.metadata, "create_all", create_all)
    monkeypatch.setattr(database, "_log_pool_status", MagicMock())
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)

    database.init_database()

    assert database.engine is engine
    create_engine.assert_called_once_with(
        "mysql://unit-test",
        # Outside client TLS mode the driver gets no ssl_* overrides at all:
        # _get_ssl_connect_args returns {} rather than disabling verification.
        connect_args={},
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=10,
        max_overflow=20,
        pool_timeout=15,
        echo_pool=False,
        isolation_level="READ COMMITTED",
    )
    sessionmaker.assert_called_once_with(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    create_all.assert_called_once_with(bind=engine)
    assert db.add.call_count == 13
    assert sum(type(item).__name__ == "ProductCategory" for item in (c.args[0] for c in db.add.call_args_list)) == 8
    assert sum(type(item).__name__ == "Vendor" for item in (c.args[0] for c in db.add.call_args_list)) == 5
    db.commit.assert_called_once()
    db.close.assert_called_once()


def test_init_database_skips_seed_when_populated_and_ignores_schema_and_seed_errors(
    monkeypatch, main_settings
):
    engine = SimpleNamespace()
    db = MagicMock()
    db.query.return_value.count.return_value = 2
    factory = MagicMock(return_value=db)
    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(database, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(database.Base.metadata, "create_all", MagicMock(side_effect=RuntimeError("ddl")))
    monkeypatch.setattr(database, "_log_pool_status", MagicMock())
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)

    database.init_database()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once()

    db.query.return_value.count.return_value = 0
    db.commit.side_effect = RuntimeError("read-only")
    database.init_database()
    assert db.rollback.call_count == 1
    assert db.close.call_count == 2


def test_init_database_ignores_failure_to_open_seed_session(monkeypatch, main_settings):
    factory = MagicMock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "create_engine", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(database, "sessionmaker", lambda **kwargs: factory)
    monkeypatch.setattr(database.Base.metadata, "create_all", MagicMock())
    monkeypatch.setattr(database, "_log_pool_status", MagicMock())
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)

    database.init_database()
    factory.assert_called_once_with()


def test_get_db_session_lazily_initializes_and_reuses_factory(monkeypatch, main_settings):
    engine = object()
    session = object()
    factory = MagicMock(return_value=session)
    create_engine = MagicMock(return_value=engine)
    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "create_engine", create_engine)
    monkeypatch.setattr(database, "sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(database, "_log_pool_status", MagicMock())
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)

    assert database.get_db_session() is session
    assert database.get_db_session() is session
    create_engine.assert_called_once()
    assert factory.call_count == 2


def test_get_db_session_reports_engine_and_session_errors(monkeypatch, main_settings):
    handler = _patch_error_handler(monkeypatch)
    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "_get_ssl_connect_args", lambda _: {})
    monkeypatch.setattr(database, "SessionLocal", None)
    monkeypatch.setattr(database, "create_engine", MagicMock(side_effect=RuntimeError("engine")))

    with pytest.raises(RuntimeError, match="engine"):
        database.get_db_session()
    assert handler.call_count == 1

    sql_error = SQLAlchemyError("session")
    monkeypatch.setattr(database, "SessionLocal", MagicMock(side_effect=sql_error))
    with pytest.raises(SQLAlchemyError, match="session"):
        database.get_db_session()
    assert handler.call_count == 2

    monkeypatch.setattr(database, "SessionLocal", MagicMock(side_effect=RuntimeError("session")))
    with pytest.raises(RuntimeError, match="session"):
        database.get_db_session()
    assert handler.call_count == 3


def test_remote_session_lazy_initialization_and_error_paths(monkeypatch, main_settings):
    engine = object()
    session = MagicMock()
    session.execute.return_value = object()
    factory = MagicMock(return_value=session)
    create_engine = MagicMock(return_value=engine)
    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "create_engine", create_engine)
    monkeypatch.setattr(database, "sessionmaker", MagicMock(return_value=factory))
    monkeypatch.setattr(database, "remote_engine", None)
    monkeypatch.setattr(database, "RemoteSessionLocal", None)

    assert database.get_remote_db_session() is session
    create_engine.assert_called_once_with(
        "mysql://remote-unit-test",
        connect_args={},
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
        echo=False,
    )
    assert session.execute.call_count == 1

    handler = _patch_error_handler(monkeypatch)
    monkeypatch.setattr(database, "RemoteSessionLocal", None)
    monkeypatch.setattr(database, "create_engine", MagicMock(side_effect=RuntimeError("remote engine")))
    with pytest.raises(RuntimeError, match="remote engine"):
        database.get_remote_db_session()
    assert handler.call_count == 1

    monkeypatch.setattr(database, "RemoteSessionLocal", MagicMock(side_effect=SQLAlchemyError("remote session")))
    monkeypatch.setattr(database, "create_engine", MagicMock(return_value=engine))
    with pytest.raises(SQLAlchemyError, match="remote session"):
        database.get_remote_db_session()
    assert handler.call_count == 2

    bad_session = MagicMock()
    bad_session.execute.side_effect = RuntimeError("remote query")
    monkeypatch.setattr(database, "RemoteSessionLocal", lambda: bad_session)
    with pytest.raises(RuntimeError, match="remote query"):
        database.get_remote_db_session()
    assert handler.call_count == 3


def test_remote_session_configuration_and_sqlalchemy_connection_error(monkeypatch, main_settings):
    monkeypatch.setattr(database, "RemoteSessionLocal", None)
    disabled = SimpleNamespace(enable_remote_categorization=False)
    monkeypatch.setattr(database, "get_settings", lambda: disabled)
    with pytest.raises(ValueError, match="not enabled"):
        database.get_remote_db_session()

    missing_url = SimpleNamespace(enable_remote_categorization=True, get_remote_database_url=lambda: None)
    monkeypatch.setattr(database, "get_settings", lambda: missing_url)
    with pytest.raises(ValueError, match="not configured"):
        database.get_remote_db_session()

    handler = _patch_error_handler(monkeypatch)
    sql_session = MagicMock()
    sql_session.execute.side_effect = SQLAlchemyError("connection")
    monkeypatch.setattr(database, "get_settings", lambda: main_settings)
    monkeypatch.setattr(database, "RemoteSessionLocal", lambda: sql_session)
    with pytest.raises(SQLAlchemyError, match="connection"):
        database.get_remote_db_session()
    assert handler.call_count == 1


def test_execute_remote_query_converts_rows_and_closes_session(monkeypatch):
    session = MagicMock()
    result = MagicMock()
    result.keys.return_value = ["id", "name"]
    result.fetchall.return_value = [(1, "one"), (2, "two")]
    session.execute.return_value = result
    monkeypatch.setattr(database, "get_remote_db_session", lambda: session)

    assert database.execute_remote_query("SELECT :id", {"id": 1}) == [
        {"id": 1, "name": "one"},
        {"id": 2, "name": "two"},
    ]
    session.execute.assert_called_once()
    assert session.close.call_count == 1


def test_execute_remote_query_reports_sqlalchemy_and_generic_errors(monkeypatch):
    handler = _patch_error_handler(monkeypatch)
    for error in (SQLAlchemyError("sql"), RuntimeError("generic")):
        session = MagicMock()
        session.execute.side_effect = error
        monkeypatch.setattr(database, "get_remote_db_session", lambda session=session: session)
        with pytest.raises(type(error)):
            database.execute_remote_query("bad")
        session.close.assert_called_once()
    assert handler.call_count == 2


def test_remote_helpers_build_queries_and_stats_handle_empty_results(monkeypatch):
    execute = MagicMock(return_value=[])
    monkeypatch.setattr(database, "execute_remote_query", execute)

    assert database.get_remote_item_categories(0) == []
    query, params = execute.call_args.args
    assert "LIMIT" not in query and params == {}

    assert database.get_remote_item_categories(3) == []
    query, params = execute.call_args.args
    assert "LIMIT :limit" in query and params == {"limit": 3}

    execute.side_effect = [[], [], [], []]
    assert database.get_remote_category_stats() == {
        "total_items": 0,
        "unique_categories": 0,
        "unique_divisions": 0,
        "top_categories": [],
    }


def test_remote_connection_false_results_and_exceptions(monkeypatch):
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    for result in ([], [{"test": 0}], [{"other": 1}]):
        monkeypatch.setattr(database, "execute_remote_query", lambda *_args, result=result: result)
        assert database.test_remote_connection() is False

    monkeypatch.setattr(database, "execute_remote_query", MagicMock(side_effect=RuntimeError("offline")))
    assert database.test_remote_connection() is False


def test_manager_lifecycle_pool_status_and_noops(monkeypatch):
    main_pool = SimpleNamespace(
        size=lambda: 10, checkedin=lambda: 2, checkedout=lambda: 8, overflow=lambda: 1, invalid=lambda: 0
    )
    remote_pool = SimpleNamespace(
        size=lambda: 5, checkedin=lambda: 0, checkedout=lambda: 5, overflow=lambda: 2, invalid=lambda: 1
    )
    monkeypatch.setattr(database, "engine", SimpleNamespace(pool=main_pool))
    monkeypatch.setattr(database, "remote_engine", SimpleNamespace(pool=remote_pool))
    provided = MagicMock()
    manager = database.DatabaseManager(session=provided)
    assert manager.get_connection_pool_status() == {
        "main_db": {
            "size": 10,
            "checked_in": 2,
            "checked_out": 8,
            "overflow": 1,
            "invalid": 0,
            "pool_status": "healthy",
        },
        "remote_db": {
            "size": 5,
            "checked_in": 0,
            "checked_out": 5,
            "overflow": 2,
            "invalid": 1,
            "pool_status": "depleted",
        },
    }
    assert manager.execute_health_check() is None
    assert manager.backup_learning_data() is None
    manager.close()
    provided.close.assert_not_called()

    owned_session = MagicMock()
    monkeypatch.setattr(database, "get_db_session", lambda: owned_session)

    # A manager that is never used takes nothing out of the pool, so there is
    # nothing to close and nothing that can leak.
    unused = database.DatabaseManager()
    assert unused.has_session is False
    unused.close()
    owned_session.close.assert_not_called()

    owned = database.DatabaseManager()
    assert owned.__enter__() is owned
    assert owned.session is owned_session  # first access checks the session out
    assert owned.has_session is True
    assert owned.__exit__(ValueError, ValueError("x"), None) is False
    owned_session.close.assert_called_once()


def test_manager_close_and_destructor_swallow_close_errors(monkeypatch):
    session = MagicMock()
    session.close.side_effect = RuntimeError("close")
    monkeypatch.setattr(database, "get_db_session", lambda: session)
    manager = database.DatabaseManager()
    assert manager.session is session  # force the lazy checkout
    manager.close()
    # A close() whose underlying close() raised must still drop the reference.
    assert manager.has_session is False

    destructor_session = MagicMock()
    destructor = database.DatabaseManager.__new__(database.DatabaseManager)
    destructor._owns_session = True
    destructor.session = destructor_session
    warning = MagicMock()
    monkeypatch.setattr(database.logger, "warning", warning)
    database.DatabaseManager.__del__(destructor)
    warning.assert_called_once()
    destructor_session.close.assert_called_once()


def test_cleanup_expired_sessions_updates_rows_and_uses_configured_timeout(monkeypatch):
    now = datetime(2025, 2, 3, 12, 0, 0)
    expired = SimpleNamespace(
        outcome=None,
        completed_at=None,
        workflow_state={"large": True},
        conversation_history={"messages": ["old"]},
        extracted_entities={"x": 1},
    )
    query = MagicMock()
    query.filter.return_value.all.return_value = [expired]
    db = MagicMock()
    db.query.return_value = query
    manager = database.DatabaseManager(session=db)
    settings = SimpleNamespace(session_timeout_hours=4)
    monkeypatch.setattr(database, "utc_now", lambda: now)
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    assert manager.cleanup_expired_sessions() == 1
    assert expired.outcome is ConversationOutcome.timeout
    assert expired.completed_at == now
    assert expired.workflow_state == {"extracted_entities": {}}
    assert expired.conversation_history == {"messages": []}
    assert expired.extracted_entities == {}
    db.commit.assert_called_once()
    query.filter.assert_called_once()


def test_cleanup_completed_sessions_disabled_and_enabled(monkeypatch):
    db = MagicMock()
    query = db.query.return_value
    completed = SimpleNamespace(workflow_state={"x": 1}, conversation_history={"messages": ["x"]}, extracted_entities={"x": 1})
    query.filter.return_value.all.return_value = [completed]
    manager = database.DatabaseManager(session=db)
    settings = SimpleNamespace(cleanup_completed_sessions=False)
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    assert manager.cleanup_completed_sessions() == 0
    db.commit.assert_not_called()

    settings.cleanup_completed_sessions = True
    flags = MagicMock()
    monkeypatch.setattr(database, "flag_modified", flags)
    assert manager.cleanup_completed_sessions() == 1
    assert completed.workflow_state == {"extracted_entities": {}, "cleaned_up": True}
    assert completed.conversation_history == {"messages": [], "cleaned_up": True}
    assert completed.extracted_entities == {}
    assert flags.call_args_list == [
        call(completed, "workflow_state"),
        call(completed, "conversation_history"),
    ]
    db.commit.assert_called_once()


def test_append_session_data_merges_all_supported_fields(monkeypatch):
    existing = SimpleNamespace(
        conversation_history={
            "messages": [{"timestamp": "1", "content": {"a": 1}, "role": "user"}],
            "metadata": [{"timestamp": "m", "content": "same", "role": "system"}],
            "openai_messages": [{"content": {"prompt": "x"}, "role": "user"}],
        },
        rfq_ids=["r1"],
        product_items=["old-product"],
        bfs_products_searched=["old-search"],
        bfs_price_accepted=["old-price"],
        bfs_counter_offers=["old-counter"],
        products_bid_for=["old-bid"],
        bids_received=["old-response"],
        bids_accepted=["old-accepted"],
        counter_offers_made=["old-made"],
        counter_offers_accepted=["old-counter-accepted"],
        rfqs_with_response=["r1"],
        seller_responses=["old-seller"],
        workflow_state={"old": 1},
        extracted_entities={"old": 1},
        rfq_metadata={"old": 1},
        interaction_metrics={"old": 1},
        whatsapp_context={"old": 1},
        workflow_type="old",
        outcome="old",
        completed_at=None,
        last_activity_at=None,
        user_type="old",
        session_state="old",
        bfs_search_count=2,
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database, "flag_modified", MagicMock())
    new_data = {
        "session_id": "session-1",
        "conversation_history": {
            "messages": [
                {"timestamp": "1", "content": {"a": 1}, "role": "user"},
                {"timestamp": "2", "content": {"a": 2}, "role": "assistant"},
                {"timestamp": "2", "content": {"a": 2}, "role": "assistant"},
            ],
            "metadata": [{"timestamp": "m", "content": "same", "role": "system"}, {"timestamp": "n", "content": "new", "role": "system"}],
            "openai_messages": [{"content": {"prompt": "x"}, "role": "user"}, {"content": {"prompt": "y"}, "role": "assistant"}],
        },
        "rfq_ids": ["r1", "r2"],
        "product_items": "new-product",
        "bfs_products_searched": ["new-search"],
        "bfs_price_accepted": ["new-price"],
        "bfs_counter_offers": ["new-counter"],
        "products_bid_for": ["new-bid"],
        "bids_received": ["new-response"],
        "bids_accepted": ["new-accepted"],
        "counter_offers_made": ["new-made"],
        "counter_offers_accepted": ["new-counter-accepted"],
        "rfqs_with_response": ["r1", "r2"],
        "seller_responses": ["new-seller"],
        "workflow_state": {"new": 2},
        "extracted_entities": {"new": 2},
        "rfq_metadata": {"new": 2},
        "interaction_metrics": {"new": 2},
        "whatsapp_context": {"new": 2},
        "workflow_type": "new-type",
        "outcome": "completed",
        "completed_at": datetime(2025, 1, 2),
        "last_activity_at": datetime(2025, 1, 3),
        "user_type": "buyer",
        "session_state": "completed",
        "bfs_search_count": 3,
    }

    assert manager.append_session_data(new_data) is existing
    assert len(existing.conversation_history["messages"]) == 2
    assert len(existing.conversation_history["metadata"]) == 2
    assert len(existing.conversation_history["openai_messages"]) == 2
    assert existing.rfq_ids == ["r1", "r2"]
    assert existing.product_items == ["old-product", "new-product"]
    assert existing.rfqs_with_response == ["r1", "r2"]
    assert existing.workflow_state == {"old": 1, "new": 2}
    assert existing.extracted_entities == {"old": 1, "new": 2}
    assert existing.outcome == "completed"
    assert existing.bfs_search_count == 5
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(existing)


def test_append_session_data_falls_back_after_database_error(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = SQLAlchemyError("lookup")
    manager = database.DatabaseManager(session=db)
    fallback = MagicMock(return_value="saved")
    monkeypatch.setattr(manager, "save_conversation_session", fallback)
    data = {"session_id": "failed"}

    assert manager.append_session_data(data) == "saved"
    db.rollback.assert_called_once()
    fallback.assert_called_once_with(data)


def test_save_conversation_session_updates_existing_and_creates_new(monkeypatch):
    data = _save_data()
    existing = SimpleNamespace(session_id="session-1", external_user_id="old")
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing
    manager = database.DatabaseManager(session=db)
    flags = MagicMock()
    monkeypatch.setattr(database, "flag_modified", flags)

    assert manager.save_conversation_session(data) is existing
    assert existing.session_id == "session-1"
    assert existing.external_user_id == "user-1"
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(existing)
    assert flags.called

    db.reset_mock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    assert manager.save_conversation_session(data).session_id == "session-1"
    db.add.assert_called_once()
    db.commit.assert_called_once()
    db.refresh.assert_called_once()


def test_save_conversation_session_integrity_error_retries_existing_or_returns_object(monkeypatch):
    data = _save_data()
    retry = SimpleNamespace(session_id="session-1")
    db = MagicMock()
    first = db.query.return_value.filter_by.return_value.first
    first.side_effect = [IntegrityError("insert", {}, RuntimeError("duplicate")), retry]
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database, "flag_modified", MagicMock())

    assert manager.save_conversation_session(data) is retry
    db.rollback.assert_called_once()
    assert db.commit.call_count == 1
    db.refresh.assert_called_once_with(retry)

    db = MagicMock()
    first = db.query.return_value.filter_by.return_value.first
    first.side_effect = [IntegrityError("insert", {}, RuntimeError("duplicate")), None]
    manager = database.DatabaseManager(session=db)
    result = manager.save_conversation_session(data)
    assert result.session_id == "session-1"
    db.rollback.assert_called_once()


def test_save_conversation_session_generic_error_retries_or_constructs_fallback(monkeypatch):
    data = _save_data()
    retry = SimpleNamespace(session_id="session-1")
    db = MagicMock()
    first = db.query.return_value.filter_by.return_value.first
    first.side_effect = [None, retry]
    db.commit.side_effect = [SQLAlchemyError("write"), None]
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database, "flag_modified", MagicMock())
    assert manager.save_conversation_session(data) is retry
    db.rollback.assert_called_once()
    db.refresh.assert_called_once_with(retry)

    db = MagicMock()
    first = db.query.return_value.filter_by.return_value.first
    first.side_effect = [None, RuntimeError("retry lookup")]
    db.commit.side_effect = SQLAlchemyError("write")
    manager = database.DatabaseManager(session=db)
    result = manager.save_conversation_session(data)
    assert result.session_id == "session-1"
    db.rollback.assert_called_once()


def test_get_conversation_session_normal_optional_empty_and_missing(monkeypatch):
    db = MagicMock()
    optional = SimpleNamespace(
        extracted_entities={"x": 1},
        workflow_state={"pending_optional_combined_rfq": {"id": 1}},
    )
    db.query.return_value.filter_by.return_value.first.side_effect = [optional, None]
    manager = database.DatabaseManager(session=db)

    assert manager.get_conversation_session("one") is optional
    assert manager.get_conversation_session("missing") is None
    db.expire_all.assert_called_with()
    assert db.refresh.call_count == 1


def test_get_conversation_session_handles_empty_and_malformed_json_state(monkeypatch):
    empty = SimpleNamespace(extracted_entities=None, workflow_state=None)

    class BrokenState(str):
        def keys(self):
            return []

    malformed = SimpleNamespace(extracted_entities={}, workflow_state=BrokenState("not-json"))
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [empty, malformed]
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(
        database.json,
        "loads",
        MagicMock(side_effect=database.json.JSONDecodeError("bad json", "not-json", 0)),
    )

    assert manager.get_conversation_session("empty") is empty
    assert manager.get_conversation_session("bad") is malformed
    assert malformed.workflow_state == {"extracted_entities": []}


def test_get_conversation_session_returns_none_on_database_error():
    db = MagicMock()
    db.expire_all.side_effect = SQLAlchemyError("expired")
    manager = database.DatabaseManager(session=db)
    assert manager.get_conversation_session("bad") is None
    db.rollback.assert_called_once()


def test_ssl_certificate_search_and_session_context_paths(monkeypatch, main_settings):
    client = SimpleNamespace(database_mode="client", PROJECT_ROOT="C:/project")
    monkeypatch.setattr(
        "os.path.exists",
        lambda path: "C:/project" in path and path.endswith("DigiCertGlobalRootCA.crt.pem"),
    )
    ssl_args = database._get_ssl_connect_args(client)
    assert ssl_args["ssl"]["ca"].endswith("ssl/DigiCertGlobalRootCA.crt.pem")
    assert "C:/project" in ssl_args["ssl"]["ca"]

    class Session:
        def __init__(self):
            self.committed = 0
            self.rolled_back = 0
            self.closed = 0

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rolled_back += 1

        def close(self):
            self.closed += 1

    success = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: success)
    monkeypatch.setattr(database, "_log_pool_status", MagicMock())
    with database.get_db_session_context() as current:
        assert current is success
    assert (success.committed, success.rolled_back, success.closed) == (1, 0, 1)

    failure = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: failure)
    with pytest.raises(RuntimeError, match="context"):
        with database.get_db_session_context():
            raise RuntimeError("context")
    assert (failure.committed, failure.rolled_back, failure.closed) == (0, 1, 1)


def test_remote_connection_success_and_manager_uninitialized_pool_status(monkeypatch):
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(database, "execute_remote_query", lambda query: [{"test": 1}])
    assert database.test_remote_connection() is True

    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "remote_engine", None)
    manager = database.DatabaseManager(session=MagicMock())
    assert manager.get_connection_pool_status() == {
        "main_db": {"status": "not_initialized"},
        "remote_db": {"status": "not_initialized"},
    }


def test_append_session_data_noop_fields_and_new_record_fallback(monkeypatch):
    existing = SimpleNamespace(conversation_history={})
    db = MagicMock()
    first = db.query.return_value.filter_by.return_value.first
    first.side_effect = [existing, None]
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database, "flag_modified", MagicMock())

    assert manager.append_session_data({"session_id": "same", "outcome": None, "bfs_search_count": 0}) is existing
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(existing)

    fallback = MagicMock(return_value="created")
    monkeypatch.setattr(manager, "save_conversation_session", fallback)
    assert manager.append_session_data({"session_id": "new", "conversation_history": {}}) == "created"
    fallback.assert_called_once_with({"session_id": "new", "conversation_history": {}})


def test_get_conversation_session_plain_and_deserialized_workflow_state(monkeypatch):
    class JsonState(str):
        def keys(self):
            return []

    plain = SimpleNamespace(extracted_entities={}, workflow_state={"stage": "plain"})
    encoded = SimpleNamespace(extracted_entities={}, workflow_state=JsonState('{"stage": "decoded"}'))
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [plain, encoded]
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database.json, "loads", lambda value: {"stage": "decoded"})

    assert manager.get_conversation_session("plain") is plain
    assert manager.get_conversation_session("encoded") is encoded
    assert encoded.workflow_state == {"stage": "decoded"}


def test_remaining_database_branches(monkeypatch, main_settings):
    client = SimpleNamespace(database_mode="client", PROJECT_ROOT="C:/missing")
    monkeypatch.setattr("os.path.exists", lambda _path: False)
    with pytest.raises(ValueError, match="TLS CA certificate"):
        database._get_ssl_connect_args(client)

    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert database.test_remote_connection() is False

    now = datetime(2025, 2, 3, 12, 0, 0)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database, "utc_now", lambda: now)
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(session_timeout_hours=4))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(session_timeout_hours=4))
    assert manager.cleanup_expired_sessions(hours=2) == 0

    data = _save_data()
    failing_db = MagicMock()
    first = failing_db.query.return_value.filter_by.return_value.first
    first.side_effect = [None, None]
    failing_db.commit.side_effect = SQLAlchemyError("write")
    manager = database.DatabaseManager(session=failing_db)
    result = manager.save_conversation_session(data)
    assert result.session_id == "session-1"


def test_append_session_data_handles_non_dict_openai_content(monkeypatch):
    existing = SimpleNamespace(
        conversation_history={
            "messages": [],
            "metadata": [],
            "openai_messages": [{"content": "existing", "role": "system"}],
        }
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing
    manager = database.DatabaseManager(session=db)
    monkeypatch.setattr(database, "flag_modified", MagicMock())

    manager.append_session_data(
        {
            "session_id": "session-1",
            "conversation_history": {
                "messages": [],
                "metadata": [],
                "openai_messages": [
                    {"content": "existing", "role": "system"},
                    {"content": "new", "role": "system"},
                ],
            },
        }
    )
    assert existing.conversation_history["openai_messages"] == [
        {"content": "existing", "role": "system"},
        {"content": "new", "role": "system"},
    ]


def test_get_conversation_session_optional_single_field(monkeypatch):
    single = SimpleNamespace(
        extracted_entities={},
        workflow_state={"pending_optional_rfq": {"id": 1}},
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = single
    manager = database.DatabaseManager(session=db)
    assert manager.get_conversation_session("single") is single
