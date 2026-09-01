"""Deterministic unit tests for the scheduled task modules.

All service, database, queue, filesystem, and API boundaries are replaced with
small fakes so these tests exercise task control flow without worker startup.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pandas as pd
import pytest

from app.tasks import auto_categorization_task as auto
from app.tasks import bfs_notification_task as bfs
from app.tasks import category_name_sync_task as category_sync
from app.tasks import daily_category_vector_rebuild_task as daily_rebuild
from app.tasks import export_excel_task as export_excel
from app.tasks import log_cleanup_task as cleanup
from app.tasks import seller_matching_task as seller
from app.tasks import taxonomy_build_task as taxonomy
from app.tasks import vector_store_sync_task as vector_sync
from app.tasks import whatsapp_report_automation_task as whatsapp


FIXED_NOW = datetime(2024, 6, 15, 12, 0, 0)


class FixedDateTime(datetime):
    @classmethod
    def utcnow(cls):
        return FIXED_NOW

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW


class FakeSession:
    def __init__(self, result=None, rowcount=1, fail_execute=False):
        self.result = result or SimpleNamespace(rowcount=rowcount)
        self.fail_execute = fail_execute
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.executed = []

    def execute(self, *args):
        self.executed.append(args)
        if self.fail_execute:
            raise RuntimeError("execute failed")
        return self.result

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeQuery:
    def __init__(self, values=None, first_value=None, delete_count=0):
        self.values = values or []
        self.first_value = first_value
        self.delete_count = delete_count

    def all(self):
        return self.values

    def first(self):
        return self.first_value

    def filter(self, *args, **kwargs):
        return self

    def delete(self, **kwargs):
        return self.delete_count


# ---------------------------------------------------------------------------
# auto_categorization_task
# ---------------------------------------------------------------------------


def test_auto_task_disabled_and_empty(monkeypatch):
    monkeypatch.setattr(auto, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert auto.process_uncategorized_rfqs.run()["status"] == "skipped"
    monkeypatch.setattr(auto, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(auto, "get_uncategorized_items", lambda: [])
    assert auto.process_uncategorized_rfqs.run() == {
        "status": "completed", "processed": 0, "message": "No items to process"
    }


def test_auto_task_mixed_results_and_outer_error(monkeypatch):
    monkeypatch.setattr(auto, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    items = [{"uuid": "i1", "rfq_id": "r1"}, {"uuid": "i2", "rfq_id": "r2"}, {"uuid": "i3"}]
    monkeypatch.setattr(auto, "get_uncategorized_items", lambda: items)
    monkeypatch.setattr(auto, "EnhancedAutoCategorizationService", lambda: object())
    results = iter([
        {"success": True, "item_uuid": "i1"},
        {"success": False, "item_uuid": "i2"},
    ])
    monkeypatch.setattr(auto.asyncio, "run", lambda *args: next(results))
    result = auto.process_uncategorized_rfqs.run()
    assert result["processed"] == 1 and result["failed"] == 2
    assert result["results"][-1]["error"] == ""
    monkeypatch.setattr(auto, "get_uncategorized_items", Mock(side_effect=RuntimeError("query")))
    with pytest.raises(RuntimeError, match="query"):
        auto.process_uncategorized_rfqs.run()


def test_auto_single_item_success_missing_category_update_failure_and_exception(monkeypatch):
    service = SimpleNamespace(categorize_item=AsyncMock(return_value={"success": True, "client_category": "Tools"}))
    monkeypatch.setattr(auto, "update_rfq_item_category", lambda *_: True)
    result = asyncio.run(auto.process_single_item({"uuid": "i", "rfq_id": "r", "description": "x"}, service))
    assert result["success"] and result["category"] == "Tools"
    service.categorize_item = AsyncMock(return_value={"success": False})
    assert asyncio.run(auto.process_single_item({"uuid": "i", "rfq_id": "r"}, service))["error"] == "Categorization failed"
    service.categorize_item = AsyncMock(return_value={"success": True, "client_category": "Tools"})
    monkeypatch.setattr(auto, "update_rfq_item_category", lambda *_: False)
    assert asyncio.run(auto.process_single_item({"uuid": "i", "rfq_id": "r"}, service))["error"] == "Database update failed"
    service.categorize_item = AsyncMock(side_effect=RuntimeError("ai"))
    assert asyncio.run(auto.process_single_item({"uuid": "i", "rfq_id": "r"}, service))["error"] == "ai"


def test_auto_database_helpers_success_and_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(auto, "execute_remote_query", lambda query, params: calls.append((query, params)) or [{"uuid": "i"}])
    assert auto.get_uncategorized_items(7) == [{"uuid": "i"}]
    assert calls[-1][1] == {"limit": 7}
    monkeypatch.setattr(auto, "execute_remote_query", Mock(side_effect=RuntimeError("read")))
    assert auto.get_rfq_items("r") == []
    db = FakeSession(rowcount=1)
    monkeypatch.setattr(auto, "get_remote_db_session", lambda: db)
    assert auto.update_rfq_item_category("i", "c") is True
    assert db.commits == 1 and db.closed
    db = FakeSession(rowcount=0)
    monkeypatch.setattr(auto, "get_remote_db_session", lambda: db)
    assert auto.update_rfq_item_category("i", "c") is False
    db = FakeSession(fail_execute=True)
    monkeypatch.setattr(auto, "get_remote_db_session", lambda: db)
    assert auto.update_rfq_item_category("i", "c") is False
    assert db.rollbacks == 1 and db.closed
    monkeypatch.setattr(auto, "get_remote_db_session", Mock(side_effect=RuntimeError("connect")))
    assert auto.update_rfq_item_category("i", "c") is False


# ---------------------------------------------------------------------------
# bfs_notification_task
# ---------------------------------------------------------------------------


def test_bfs_database_helpers_cover_rows_and_transactions(monkeypatch):
    result = SimpleNamespace(keys=lambda: ["a", "b"], fetchall=lambda: [(1, 2)], rowcount=2)
    db = FakeSession(result=result, rowcount=2)
    monkeypatch.setattr(bfs, "get_remote_db_session", lambda: db)
    assert bfs.get_pending_bfs_notifications(3) == [{"a": 1, "b": 2}]
    assert db.closed
    assert bfs.mark_notification_sent("u") is True
    assert bfs.mark_notifications_sent_batch([]) == 0
    assert bfs.mark_notifications_sent_batch(["u1", "u2"]) == 2
    failing = FakeSession(fail_execute=True)
    monkeypatch.setattr(bfs, "get_remote_db_session", lambda: failing)
    assert bfs.mark_notification_sent("u") is False
    assert bfs.mark_notifications_sent_batch(["u"]) == 0
    monkeypatch.setattr(bfs, "get_remote_db_session", Mock(side_effect=RuntimeError("connect")))
    assert bfs.get_pending_bfs_notifications() == []


def test_bfs_async_disabled_empty_missing_phone_and_mixed_notifications(monkeypatch):
    monkeypatch.setattr(bfs, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert asyncio.run(bfs.process_bfs_notifications())["status"] == "skipped"
    monkeypatch.setattr(bfs, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs, "get_pending_bfs_notifications", lambda limit: [])
    assert asyncio.run(bfs.process_bfs_notifications())["processed"] == 0
    records = [
        {"bfs_user_uuid": "u1", "seller_phone": "9199", "buy_price": None, "listed_price": 12,
         "item_description": "item", "item_category": "cat", "ask_price": 4, "quantity": 2,
         "buyer_name": "buyer", "seller_name": "seller", "seller_org_uuid": "s1"},
        {"bfs_user_uuid": "u2", "seller_phone": None},
    ]
    monkeypatch.setattr(bfs, "get_pending_bfs_notifications", lambda limit: records)
    monkeypatch.setattr(bfs, "get_users_active_in_last_24hrs", lambda phones: {"9199"})
    monkeypatch.setattr(bfs, "normalize_phone_for_comparison", lambda phone: phone.lstrip("+"))
    service = SimpleNamespace(send_bfs_bid_notifications_batch=AsyncMock(return_value={
        "results": [{"bfs_user_uuid": "u1", "success": True}], "sent": 1, "failed": 0, "skipped": 0
    }))
    monkeypatch.setattr(bfs, "get_seller_notification_service", lambda: service)
    marked = Mock()
    monkeypatch.setattr(bfs, "mark_notifications_sent_batch", marked)
    result = asyncio.run(bfs.process_bfs_notifications())
    assert result["notifications_prepared"] == 1 and result["interactive_messages"] == 1
    assert service.send_bfs_bid_notifications_batch.await_args.kwargs["notifications"][0]["bid_data"]["buy_price"] == 12
    marked.assert_called_once_with(["u1"])
    monkeypatch.setattr(bfs, "get_pending_bfs_notifications", lambda limit: [{"seller_phone": None}])
    assert asyncio.run(bfs.process_bfs_notifications())["message"] == "No valid seller phone numbers"


def test_bfs_wrapper_propagates_async_errors(monkeypatch):
    monkeypatch.setattr(bfs, "process_bfs_notifications", AsyncMock(side_effect=RuntimeError("worker")))
    with pytest.raises(RuntimeError, match="worker"):
        bfs.process_bfs_seller_notifications.run()


# ---------------------------------------------------------------------------
# category_name_sync_task
# ---------------------------------------------------------------------------


def test_category_source_loaders_cover_disabled_empty_and_local_error(monkeypatch):
    monkeypatch.setattr(category_sync, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert category_sync._load_categories_from_remote() == {}
    monkeypatch.setattr(category_sync, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    database = importlib.import_module("app.database")
    monkeypatch.setattr(database, "test_remote_connection", lambda: False)
    assert category_sync._load_categories_from_remote() == {}
    monkeypatch.setattr(database, "test_remote_connection", lambda: True)
    monkeypatch.setattr(database, "get_remote_item_categories", lambda: [{"category": " A "}, {"category": ""}, {"category": None}, {"category": "A"}])
    assert category_sync._load_categories_from_remote() == {"A": 2}
    db = FakeQuery([SimpleNamespace(client_category_name=" L "), SimpleNamespace(client_category_name=None)])
    session = SimpleNamespace(query=lambda *_: db, close=Mock())
    monkeypatch.setattr(database, "get_db_session", lambda: session)
    assert category_sync._load_categories_from_local_db() == {"L": 1}
    session.query = Mock(side_effect=RuntimeError("local"))
    assert category_sync._load_categories_from_local_db() == {}
    session.close.assert_called()


def test_category_sync_orchestration_success_failure_empty_and_trigger(monkeypatch):
    monkeypatch.setattr(category_sync, "_add_project_path", Mock())
    monkeypatch.setattr(category_sync, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(category_sync, "_load_categories_from_remote", lambda: {"A": 2})
    monkeypatch.setattr(category_sync, "_load_categories_from_local_db", lambda: {"A": 1, "B": 1})
    embed = Mock(return_value={"success": True, "total_categories": 2, "collection_count": 2})
    monkeypatch.setattr(category_sync, "_create_category_embeddings", embed)
    result = category_sync.sync_category_names.run(clear_existing=True)
    assert result["status"] == "completed" and result["total_unique_categories"] == 2
    assert embed.call_args.args[0] == {"A": 3, "B": 1}
    monkeypatch.setattr(category_sync, "_create_category_embeddings", lambda *_args, **_kwargs: {"success": False, "error": "chroma"})
    assert category_sync.sync_category_names.run()["status"] == "partial_failure"
    monkeypatch.setattr(category_sync, "_load_categories_from_remote", lambda: {})
    monkeypatch.setattr(category_sync, "_load_categories_from_local_db", lambda: {})
    assert category_sync.sync_category_names.run()["status"] == "failed"
    queued = SimpleNamespace(id="cat-task")
    monkeypatch.setattr(category_sync.sync_category_names, "apply_async", Mock(return_value=queued))
    assert category_sync.trigger_category_name_sync(True)["task_id"] == "cat-task"


def test_category_embedding_client_batches_and_errors(monkeypatch):
    class Collection:
        def __init__(self):
            self.added = []
        def add(self, **kwargs):
            self.added.append(kwargs)
        def count(self):
            return sum(len(x["ids"]) for x in self.added)

    collection = Collection()
    client = SimpleNamespace(
        heartbeat=Mock(),
        delete_collection=Mock(side_effect=RuntimeError("missing")),
        get_or_create_collection=Mock(return_value=collection),
    )
    monkeypatch.setattr(category_sync, "get_settings", lambda: SimpleNamespace(chroma_host="h", chroma_port=8000))
    monkeypatch.setattr(category_sync.chromadb, "HttpClient", Mock(return_value=client))
    monkeypatch.setattr(category_sync.embedding_functions, "SentenceTransformerEmbeddingFunction", Mock(return_value="embed"))
    categories = {f"Cat {i:03d}": i for i in range(101)}
    result = category_sync._create_category_embeddings(categories, clear_existing=True)
    assert result == {"success": True, "total_categories": 101, "collection_count": 101}
    assert [len(x["ids"]) for x in collection.added] == [100, 1]
    client.heartbeat.side_effect = RuntimeError("offline")
    assert category_sync._create_category_embeddings({"A": 1})["success"] is False


# ---------------------------------------------------------------------------
# daily_category_vector_rebuild_task
# ---------------------------------------------------------------------------


def test_daily_rebuild_wrapper_statuses_and_trigger(monkeypatch):
    monkeypatch.setattr(daily_rebuild, "get_settings", lambda: SimpleNamespace(enable_daily_category_rebuild=False))
    assert daily_rebuild.rebuild_category_vector_store.run()["status"] == "skipped"
    settings = SimpleNamespace(enable_daily_category_rebuild=True, enable_remote_categorization=True)
    monkeypatch.setattr(daily_rebuild, "get_settings", lambda: settings)
    monkeypatch.setattr(daily_rebuild, "_sync_category_mappings", lambda: {"success": False, "error": "remote"})
    fake_service = SimpleNamespace(get_collection_stats=Mock(side_effect=[{"n": 1}, {"n": 2}]), populate_embeddings_from_db=Mock(return_value=4))
    service_module = importlib.import_module("app.services.auto_categorization_service")
    monkeypatch.setattr(service_module, "AutoCategorizationService", lambda: fake_service)
    result = daily_rebuild.rebuild_category_vector_store.run()
    assert result["status"] == "completed" and result["items_count"] == 4
    monkeypatch.setattr(service_module, "AutoCategorizationService", Mock(side_effect=RuntimeError("service")))
    assert daily_rebuild.rebuild_category_vector_store.run()["status"] == "failed"
    monkeypatch.setattr(daily_rebuild, "_sync_category_mappings", Mock(side_effect=SoftTimeLimitError()))
    # A real Celery timeout is tested through the task's explicit handler below.
    monkeypatch.setattr(daily_rebuild, "_sync_category_mappings", lambda: {"success": True, "inserted_count": 0, "total_remote_items": 0, "unique_categories": 0})
    monkeypatch.setattr(daily_rebuild.rebuild_category_vector_store, "apply_async", Mock(return_value=SimpleNamespace(id="daily-task")))
    assert daily_rebuild.trigger_category_vector_rebuild()["task_id"] == "daily-task"


class SoftTimeLimitError(Exception):
    """Placeholder used only to keep the status test setup explicit."""


def test_daily_rebuild_timeout_and_mapping_helper(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded
    real_sync = daily_rebuild._sync_category_mappings

    monkeypatch.setattr(daily_rebuild, "get_settings", lambda: SimpleNamespace(enable_daily_category_rebuild=True))
    monkeypatch.setattr(daily_rebuild, "_sync_category_mappings", Mock(side_effect=SoftTimeLimitExceeded()))
    result = daily_rebuild.rebuild_category_vector_store.run()
    assert result["status"] == "timeout"
    monkeypatch.setattr(daily_rebuild, "_sync_category_mappings", real_sync)
    database = importlib.import_module("app.database")
    models = importlib.import_module("app.models")
    monkeypatch.setattr(daily_rebuild, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert daily_rebuild._sync_category_mappings()["success"] is False
    monkeypatch.setattr(daily_rebuild, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(database, "test_remote_connection", lambda: False)
    assert daily_rebuild._sync_category_mappings()["error"] == "Remote database connection failed"
    monkeypatch.setattr(database, "test_remote_connection", lambda: True)
    monkeypatch.setattr(database, "get_remote_item_categories", lambda: [])
    assert daily_rebuild._sync_category_mappings()["error"] == "No items returned from remote database"
    monkeypatch.setattr(database, "get_remote_item_categories", lambda: [{"category": " A ", "item": " X "}, {"category": "", "item": "Y"}])
    class Mapping:
        category = "category"
        item = "item"
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    monkeypatch.setattr(models, "CategoryMapping", Mapping)
    db = SimpleNamespace(query=lambda *_: FakeQuery([("A", "X")]), add_all=Mock(), commit=Mock(), rollback=Mock(), close=Mock())
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    result = daily_rebuild._sync_category_mappings()
    assert result["inserted_count"] == 0 and db.close.called


# ---------------------------------------------------------------------------
# export_excel_task
# ---------------------------------------------------------------------------


def test_export_email_reports_attachments_variables_and_empty(monkeypatch, tmp_path):
    first = tmp_path / "one.xlsx"
    first.write_bytes(b"excel")
    settings = SimpleNamespace(email_report_sender="sender", support_email="support", environment="test")
    monkeypatch.setattr(export_excel, "get_settings", lambda: settings)
    service = SimpleNamespace(send_email_by_template=AsyncMock(return_value={"status": "Success"}))
    monkeypatch.setattr(export_excel, "EmailService", lambda: service)
    result = asyncio.run(export_excel._send_excel_email_reports([str(first), str(tmp_path / "missing.xlsx")], date(2024, 1, 2)))
    assert result["status"] == "Success"
    kwargs = service.send_email_by_template.await_args.kwargs
    assert kwargs["attachments"][0]["fileData"] == base64.b64encode(b"excel").decode()
    assert kwargs["variables"]["parsed_Date"] == "2024-01-02"
    service.send_email_by_template = AsyncMock(side_effect=RuntimeError("mail"))
    assert asyncio.run(export_excel._send_excel_email_reports([], date(2024, 1, 2)))["status"] == "Failure"


def test_export_api_session_init_delegate_and_close_errors(monkeypatch):
    api = importlib.import_module("app.procucev_apis.procucev_api_client")
    init = AsyncMock()
    close = AsyncMock(side_effect=RuntimeError("close"))
    monkeypatch.setattr(api, "init_procucev_api_client", init)
    monkeypatch.setattr(api, "close_procucev_api_client", close)
    monkeypatch.setattr(export_excel, "_send_excel_email_reports", AsyncMock(return_value={"status": "Success"}))
    assert asyncio.run(export_excel._send_excel_reports_with_api_session([], date(2024, 1, 2)))["status"] == "Success"
    init.side_effect = RuntimeError("init")
    assert asyncio.run(export_excel._send_excel_reports_with_api_session([], date(2024, 1, 2)))["status"] == "Failure"


# ---------------------------------------------------------------------------
# log_cleanup_task
# ---------------------------------------------------------------------------


def test_log_cleanup_manager_missing_and_archives_old_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "datetime", FixedDateTime)
    missing = cleanup.LogCleanupManager(str(tmp_path / "missing"), 7, 7)
    assert missing.run()["errors"] == 1
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "app_2024-05-01.log"
    old.write_text("old")
    new = log_dir / "app_2024-06-14.log"
    new.write_text("new")
    excluded = log_dir / "app_2024-05-01.log.keep"
    excluded.write_text("keep")
    manager = cleanup.LogCleanupManager(str(log_dir), 7, 7, ["keep"])
    stats = manager.run()
    assert stats["logs_archived"] == 1 and stats["archives_created"] == 1
    assert not old.exists() and new.exists()
    assert manager._extract_date_from_filename("bad_2024-99-01.log") is None
    assert manager._extract_date_from_filename("no-date.log") is None
    assert manager._should_exclude("anything.keep")


def test_log_cleanup_archive_and_purge_error_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(cleanup, "datetime", FixedDateTime)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "app_2024-05-01.log"
    old.write_text("x")
    manager = cleanup.LogCleanupManager(str(log_dir), 7, 7)
    monkeypatch.setattr(cleanup.tarfile, "open", Mock(side_effect=RuntimeError("tar")))
    manager.run()
    assert manager.stats["errors"] == 1
    archive = manager.archive_dir / "logs_2024-05-01.tar.gz"
    archive.parent.mkdir(exist_ok=True)
    archive.write_bytes(b"x")
    old_archive = manager.archive_dir / "logs_2020-01-01.tar.gz"
    old_archive.write_bytes(b"old")
    bad_archive = manager.archive_dir / "logs_2024-99-99.tar.gz"
    bad_archive.write_bytes(b"bad")
    assert old_archive in manager._find_old_archives()
    manager._purge_archives([old_archive])
    assert manager.stats["archives_deleted"] == 1
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=RuntimeError("unlink")))
    manager._purge_archives([archive])
    assert manager.stats["errors"] == 2


def test_cleanup_wrapper_statuses_and_error(monkeypatch):
    settings = SimpleNamespace(PROJECT_ROOT="/project", log_retention_days=7, archive_retention_days=30)
    monkeypatch.setattr(cleanup, "get_settings", lambda: settings)
    manager = Mock()
    manager.run.return_value = {"errors": 0, "logs_archived": 1}
    monkeypatch.setattr(cleanup, "LogCleanupManager", lambda **kwargs: manager)
    assert cleanup.cleanup_logs.run()["status"] == "completed"
    manager.run.return_value = {"errors": 1}
    assert cleanup.cleanup_logs.run()["status"] == "completed_with_errors"
    monkeypatch.setattr(cleanup, "get_settings", Mock(side_effect=RuntimeError("settings")))
    with pytest.raises(RuntimeError, match="settings"):
        cleanup.cleanup_logs.run()


# ---------------------------------------------------------------------------
# seller_matching_task
# ---------------------------------------------------------------------------


def test_seller_query_helpers_and_simple_helpers(monkeypatch):
    monkeypatch.setattr(seller, "execute_remote_query", lambda _q, _p: [{"subscribed_notified": 2, "unsubscribed_notified": 3}])
    assert seller.get_rfq_notification_progress("r") == {"subscribed_notified": 2, "unsubscribed_notified": 3}
    monkeypatch.setattr(seller, "execute_remote_query", lambda _q, _p: [])
    assert seller.get_rfq_notification_progress("r")["subscribed_notified"] == 0
    monkeypatch.setattr(seller, "execute_remote_query", lambda _q, _p: [{"vendor_uuid": "s1"}, {"vendor_uuid": None}])
    assert seller.get_sellers_already_notified_for_rfq("r") == {"s1"}
    monkeypatch.setattr(seller, "execute_remote_query", lambda _q, _p: [])
    assert seller.get_rfqs_needing_seller_matching(4) == []
    assert seller.get_rfq_item_categories("r") == []
    assert seller.get_sellers_notified_in_last_24hrs() == set()
    assert seller.extract_delivery_location({"delivery_city": "Pune", "delivery_state": "MH", "delivery_pincode": "1"}) == {"city": "Pune", "state": "MH", "pincode": "1"}
    assert seller._build_item_description_for_rfq({"categories": ["A"], "description": " d ", "special_instruction": " s "}) == "Categories: A | Description: d | Requirements: s"
    assert seller._build_item_description_for_rfq({}) == ""


def test_seller_wrapper_disabled_empty_mixed_and_outer_error(monkeypatch):
    monkeypatch.setattr(seller, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    assert seller.process_seller_matching.run()["status"] == "skipped"
    monkeypatch.setattr(seller, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(seller, "get_rfqs_needing_seller_matching", lambda: [])
    assert seller.process_seller_matching.run()["processed"] == 0
    rfqs = [{"rfq_id": "r1"}, {"rfq_id": "r2"}]
    monkeypatch.setattr(seller, "get_rfqs_needing_seller_matching", lambda: rfqs)
    monkeypatch.setattr(seller, "get_sellers_notified_in_last_24hrs", lambda: {"old"})
    monkeypatch.setattr(seller, "SellerRecommendationService", lambda: object())
    monkeypatch.setattr(seller.asyncio, "run", Mock(side_effect=[{"success": True, "seller_ids_notified": ["new"]}, RuntimeError("bad")]))
    result = seller.process_seller_matching.run()
    assert result["processed"] == 1 and result["failed"] == 1
    assert result["results"][1]["error"] == "bad"
    monkeypatch.setattr(seller, "get_rfqs_needing_seller_matching", Mock(side_effect=RuntimeError("outer")))
    with pytest.raises(RuntimeError, match="outer"):
        seller.process_seller_matching.run()


@pytest.mark.asyncio
async def test_seller_single_target_empty_categories_and_no_eligible(monkeypatch):
    monkeypatch.setattr(
        "app.services.enhanced_seller_matching_service.EnhancedSellerMatchingService",
        lambda: SimpleNamespace(find_sellers_for_item=AsyncMock(return_value={"matched_sellers": []}))
    )
    reached = {"rfq_id": "r", "subscribed_notified": 10, "unsubscribed_notified": 25}
    assert (await seller.process_single_rfq_matching(reached, object(), set()))["sellers_matched"] == 0
    monkeypatch.setattr(seller, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller, "get_rfq_item_categories", lambda _: [])
    assert (await seller.process_single_rfq_matching({"rfq_id": "r", "rfq_uuid": "u"}, object(), set()))["categories_used"] == []
    monkeypatch.setattr(seller, "get_rfq_item_categories", lambda _: ["A"])
    service = SimpleNamespace(select_sellers_for_rfq=AsyncMock(return_value={"subscribed_sellers": [{"seller_id": "s1"}], "unsubscribed_sellers": []}))
    monkeypatch.setattr(seller, "get_sellers_already_notified_for_rfq", lambda _: {"s1"})
    result = await seller.process_single_rfq_matching({"rfq_id": "r", "rfq_uuid": "u"}, service, set())
    assert result["message"] == "No eligible sellers after filtering"


@pytest.mark.asyncio
async def test_seller_single_success_deduplicates_splits_messages_and_records(monkeypatch):
    monkeypatch.setattr(
        "app.services.enhanced_seller_matching_service.EnhancedSellerMatchingService",
        lambda: SimpleNamespace(find_sellers_for_item=AsyncMock(return_value={"matched_sellers": []}))
    )
    monkeypatch.setattr(seller, "get_sellers_already_notified_for_rfq", lambda _: {"rfq-old"})
    monkeypatch.setattr(seller, "get_rfq_item_categories", lambda _: ["A", "B"])
    subscribed = {"seller_id": "s1", "phone_number": "+111", "seller_name": "S1", "categories": ["A"]}
    unsubscribed = {"seller_id": "u1", "phone_number": "222", "seller_name": "U1", "categories": ["B"]}
    service = SimpleNamespace(select_sellers_for_rfq=AsyncMock(side_effect=[
        {"subscribed_sellers": [subscribed], "unsubscribed_sellers": [unsubscribed]},
        {"subscribed_sellers": [subscribed], "unsubscribed_sellers": []},
    ]))
    monkeypatch.setattr(seller, "get_users_active_in_last_24hrs", lambda phones: {"111"})
    monkeypatch.setattr(seller, "normalize_phone_for_comparison", lambda p: p.lstrip("+"))
    monkeypatch.setattr(seller, "log_selected_sellers_to_remote", Mock(return_value=False))
    recorded = Mock()
    monkeypatch.setattr(seller, "record_rfq_seller_notifications", recorded)
    notification = SimpleNamespace(send_rfq_notifications=AsyncMock(return_value={
        "sent": 1, "failed": 1, "results": [{"seller_id": "s1", "success": True}]
    }))
    monkeypatch.setattr(seller, "SellerNotificationService", lambda: notification)
    result = await seller.process_single_rfq_matching(
        {"rfq_id": "r", "rfq_uuid": "u", "description": "desc", "special_instruction": "inst"},
        service, {"none"}
    )
    assert result["sellers_matched"] == 2
    assert result["interactive_messages"] == 1 and result["template_messages"] == 1
    assert result["notifications_sent"] == 1
    assert recorded.called


def test_seller_remote_logging_and_recording(monkeypatch):
    assert seller.log_selected_sellers_to_remote("r", "id", []) is True
    db = FakeSession()
    monkeypatch.setattr(seller, "get_remote_db_session", lambda: db)
    assert seller.log_selected_sellers_to_remote("r", "id", [{"seller_id": "s"}]) is True
    assert db.commits == 1
    failing = FakeSession(fail_execute=True)
    monkeypatch.setattr(seller, "get_remote_db_session", lambda: failing)
    assert seller.log_selected_sellers_to_remote("r", "id", [{"seller_id": "s"}]) is False
    monkeypatch.setattr(seller, "get_db_session", Mock(side_effect=RuntimeError("db")))
    seller.record_rfq_seller_notifications("r", [])
    assert seller.record_rfq_seller_notifications("r", [{"success": False}]) is None


# ---------------------------------------------------------------------------
# taxonomy_build_task
# ---------------------------------------------------------------------------


def test_taxonomy_wrapper_delegates_without_worker(monkeypatch):
    called = {}
    def fake_run(coro):
        called["coro"] = coro
        coro.close()
        return {"success": True}
    monkeypatch.setattr(taxonomy.asyncio, "run", fake_run)
    result = taxonomy.build_taxonomy.run(batch_size=3, process_all=False, parallel=False, resume=False)
    assert result["success"]


class FakeRedis:
    def __init__(self, checkpoint=None):
        self.checkpoint = checkpoint
        self.set_calls = []
        self.deleted = []
    async def get(self, key): return self.checkpoint
    async def set(self, key, value): self.set_calls.append((key, value))
    async def delete(self, key): self.deleted.append(key)


def _fake_taxonomy_loader(monkeypatch, process):
    module = types.SimpleNamespace(process_category_mappings=process)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda target: target.__dict__.update(module.__dict__)))
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr("importlib.util.module_from_spec", lambda _spec: types.SimpleNamespace())


@pytest.mark.asyncio
async def test_taxonomy_single_batch_success_failure_and_checkpoint_error(monkeypatch):
    settings = SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1)
    monkeypatch.setattr(taxonomy, "get_settings", lambda: settings)
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"category": "x"}])
    redis = FakeRedis(checkpoint="not-json")
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())
    process = AsyncMock(return_value={"success": True, "processed_count": 2, "created_categories": 1, "existing_categories": 1})
    _fake_taxonomy_loader(monkeypatch, process)
    result = await taxonomy.build_taxonomy_async(None, batch_size=5, process_all=False, parallel=False, resume=True)
    assert result["success"] and result["processed_count"] == 2
    process.return_value = {"success": False, "error": "bad"}
    result = await taxonomy.build_taxonomy_async(None, batch_size=5, process_all=False, parallel=False, resume=False)
    assert result["success"] is False


@pytest.mark.asyncio
async def test_taxonomy_parallel_retry_and_outer_import_failure(monkeypatch):
    settings = SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1)
    monkeypatch.setattr(taxonomy, "get_settings", lambda: settings)
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"x": i} for i in range(2)])
    redis = FakeRedis()
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())
    process = AsyncMock(side_effect=[RuntimeError("retry"), {"success": True, "processed_count": 2, "created_categories": 1, "existing_categories": 1}])
    _fake_taxonomy_loader(monkeypatch, process)
    result = await taxonomy.build_taxonomy_async(None, batch_size=5, process_all=True, parallel=True, resume=False)
    assert result["success"] and result["checkpoint_cleared"]
    monkeypatch.setattr("importlib.util.spec_from_file_location", Mock(side_effect=ImportError("loader")))
    assert (await taxonomy.build_taxonomy_async(None, resume=False))["success"] is False


# ---------------------------------------------------------------------------
# vector_store_sync_task
# ---------------------------------------------------------------------------


def test_vector_sync_orchestration_statuses_and_trigger(monkeypatch):
    monkeypatch.setattr(vector_sync, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=False))
    assert vector_sync.sync_vector_store.run()["status"] == "skipped"
    settings = SimpleNamespace(enable_vector_store_sync=True)
    monkeypatch.setattr(vector_sync, "get_settings", lambda: settings)
    monkeypatch.setattr(vector_sync, "_cleanup_buyer_mappings", lambda: {"success": True, "deleted_count": 2})
    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", lambda: {"success": True, "processed_count": 2, "created_mappings": 1, "existing_mappings": 1, "errors": []})
    monkeypatch.setattr(vector_sync, "_run_vector_embedding_creation", lambda clear_existing: {"success": True, "total_items": 2})
    result = vector_sync.sync_vector_store.run(clear_existing=True, cleanup_buyer_mappings=True)
    assert result["status"] == "completed" and len(result["steps_completed"]) == 3
    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", lambda: {"success": False, "error": "map"})
    assert vector_sync.sync_vector_store.run()["status"] == "partial_failure"
    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", Mock(side_effect=RuntimeError("map-ex")))
    assert vector_sync.sync_vector_store.run()["status"] == "failed"
    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", lambda: {"success": True})
    monkeypatch.setattr(vector_sync, "_run_vector_embedding_creation", lambda **_: {"success": False, "error": "embed"})
    assert vector_sync.sync_vector_store.run()["status"] == "partial_failure"
    monkeypatch.setattr(vector_sync.sync_vector_store, "apply_async", Mock(return_value=SimpleNamespace(id="vector-task")))
    assert vector_sync.trigger_vector_store_sync(True, True)["task_id"] == "vector-task"


def test_vector_cleanup_mapping_and_embedding_helpers(monkeypatch):
    database = importlib.import_module("app.database")
    models = importlib.import_module("app.models")
    monkeypatch.setattr(database, "execute_remote_query", lambda *_: [])
    db = SimpleNamespace(close=Mock(), rollback=Mock(), commit=Mock())
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    assert vector_sync._cleanup_buyer_mappings()["deleted_count"] == 0
    monkeypatch.setattr(database, "execute_remote_query", lambda *_: [{"uuid": "buyer"}])
    query = FakeQuery(delete_count=2)
    db.query = lambda *_: query
    monkeypatch.setattr(models, "SellerLearningMapping", SimpleNamespace(seller_id=SimpleNamespace(in_=lambda ids: ids)))
    assert vector_sync._cleanup_buyer_mappings()["deleted_count"] == 2
    def fake_run(coro):
        coro.close()
        return {"success": True}
    monkeypatch.setattr(asyncio, "run", fake_run)
    assert vector_sync._run_seller_category_mapping()["success"]
    def fake_import(coro):
        coro.close()
        raise ImportError("missing")
    monkeypatch.setattr(asyncio, "run", fake_import)
    assert vector_sync._run_seller_category_mapping()["success"] is False
    module = types.ModuleType("create_category_embeddings")
    module.create_unified_vector_store = lambda clear_existing: clear_existing
    monkeypatch.setitem(sys.modules, "create_category_embeddings", module)
    assert vector_sync._run_vector_embedding_creation(True)["success"]
    module.create_unified_vector_store = lambda **_: False
    assert vector_sync._run_vector_embedding_creation()["success"] is False


@pytest.mark.asyncio
async def test_vector_async_mapping_empty_connection_categories_and_success(monkeypatch):
    database = importlib.import_module("app.database")
    models = importlib.import_module("app.models")
    openai_module = importlib.import_module("app.services.openai_service")
    adapter_module = importlib.import_module("app.services.seller_data_adapter")
    db = SimpleNamespace(close=Mock(), rollback=Mock(), commit=Mock())
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: object())
    monkeypatch.setattr(adapter_module, "SellerDataAdapter", lambda: SimpleNamespace(test_connection=lambda: False))
    assert (await vector_sync._async_map_sellers_to_categories())["error"] == "Remote database connection failed"
    monkeypatch.setattr(adapter_module, "SellerDataAdapter", lambda: SimpleNamespace(test_connection=lambda: True, get_sellers_from_remote=lambda: []))
    assert (await vector_sync._async_map_sellers_to_categories())["error"] == "No sellers found"
    cat = SimpleNamespace(id="c1", level_1_category="L1", level_2_category="L2", level_3_category="L3")
    seller_obj = SimpleNamespace(seller_id="s1", categories=["tools"])
    class LearningCategory:
        id = "id"
    class Mapping:
        seller_id = SimpleNamespace(__eq__=lambda self, value: True)
        original_category = SimpleNamespace(__eq__=lambda self, value: True)
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    monkeypatch.setattr(models, "LearningCategory", LearningCategory)
    monkeypatch.setattr(models, "SellerLearningMapping", Mapping)
    db.query = lambda model: FakeQuery([cat] if model is LearningCategory else [], first_value=None)
    adapter = SimpleNamespace(test_connection=lambda: True, get_sellers_from_remote=lambda: [seller_obj])
    monkeypatch.setattr(adapter_module, "SellerDataAdapter", lambda: adapter)
    openai = SimpleNamespace(map_seller_categories_batch=AsyncMock(return_value={"tools": {"success": True, "selected_category": {"id": "c1"}, "token_usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}}))
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: openai)
    result = await vector_sync._async_map_sellers_to_categories()
    assert result["success"] and result["openai_calls"] == 1


# ---------------------------------------------------------------------------
# whatsapp_report_automation_task
# ---------------------------------------------------------------------------


def _patch_whatsapp_common(monkeypatch, tmp_path, analytics_result, email_result=None, exists=True):
    monkeypatch.setattr(whatsapp, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(whatsapp.os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(whatsapp.os, "makedirs", lambda path, exist_ok=True: Path(path).mkdir(parents=True, exist_ok=True))
    analytics = SimpleNamespace(analyze_daily_conversations=AsyncMock(return_value=analytics_result))
    monkeypatch.setattr(whatsapp, "ConversationAnalyticsService", lambda: analytics)
    excel_path = str(tmp_path / "report.xlsx")
    monkeypatch.setattr(whatsapp, "EnhancedExcelReportService", lambda: SimpleNamespace(generate_report=lambda **_: excel_path))
    monkeypatch.setattr(whatsapp.os.path, "exists", lambda path: exists)
    monkeypatch.setattr(whatsapp, "_send_excel_reports_with_api_session", AsyncMock(return_value=email_result or {"status": "Success"}))
    return excel_path


def test_whatsapp_wrapper_invalid_analytics_fail_and_default_date(monkeypatch, tmp_path):
    monkeypatch.setattr(whatsapp, "get_settings", lambda: SimpleNamespace())
    result = asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "bad-date"))
    assert result["status"] == "failed" and "step" not in result
    _patch_whatsapp_common(monkeypatch, tmp_path, {"success": False, "error": "analytics"})
    result = asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))
    assert result["step"] == "conversation_analytics"
    monkeypatch.setattr(whatsapp, "ConversationAnalyticsService", lambda: SimpleNamespace(analyze_daily_conversations=AsyncMock(side_effect=RuntimeError("analytics"))))
    result = asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))
    assert result["step"] == "conversation_analytics"


def test_whatsapp_excel_email_timeout_outer_and_success_cleanup(monkeypatch, tmp_path):
    sessions = pd.DataFrame(columns=[])
    excel_path = _patch_whatsapp_common(monkeypatch, tmp_path, {"success": True, "total_sessions": 2, "sessions_df": sessions})
    monkeypatch.setattr(whatsapp.os, "remove", Mock())
    result = asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))
    assert result["status"] == "completed"
    assert whatsapp._send_excel_reports_with_api_session.await_args.args[0] == [excel_path, str(tmp_path / "app" / "reportStore" / "Daily_Chats_2024-06-01.csv")]
    monkeypatch.setattr(whatsapp.os.path, "exists", lambda path: False)
    result = asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))
    assert result["step"] == "excel_generation"
    monkeypatch.setattr(whatsapp.os.path, "exists", lambda path: True)
    monkeypatch.setattr(whatsapp, "_send_excel_reports_with_api_session", AsyncMock(return_value={"status": "Failure", "message": "mail"}))
    # A fresh fake analytics/excel setup is reused; email failure is reached after Excel.
    result = asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))
    assert result["step"] == "email_sending"
    from celery.exceptions import SoftTimeLimitExceeded
    monkeypatch.setattr(whatsapp, "get_settings", Mock(side_effect=SoftTimeLimitExceeded()))
    assert asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))["status"] == "timeout"
    monkeypatch.setattr(whatsapp, "get_settings", Mock(side_effect=RuntimeError("outer")))
    with pytest.raises(RuntimeError, match="outer"):
        asyncio.run(whatsapp.run_whatsapp_report_automation_async(None, "2024-06-01"))


def test_whatsapp_sync_wrapper_delegates(monkeypatch):
    expected = {"status": "completed"}
    def fake_run(coro):
        coro.close()
        return expected
    monkeypatch.setattr(whatsapp.asyncio, "run", fake_run)
    assert whatsapp.run_whatsapp_report_automation.run(target_date="2024-01-01") == expected
