"""Deterministic residual edge coverage for task, tool, and parser modules."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
import requests

from app.tasks import bfs_notification_task as bfs
from app.tasks import category_name_sync_task as category_sync
from app.tasks import daily_category_vector_rebuild_task as daily_rebuild
from app.tasks import log_cleanup_task as cleanup
from app.tasks import seller_matching_task as seller
from app.tasks import task_utils
from app.tasks import taxonomy_build_task as taxonomy
from app.tasks import vector_store_sync_task as vector_sync
from app.tasks import whatsapp_report_automation_task as whatsapp
from app.tools.confirmation_tool import ConfirmationTool
from app.tools.interaction_logger import InteractionLogger
import app.tools.interaction_logger as interaction_logger_module
from app.tools.user_selection_tool import UserSelectionTool
from app.utils import bfs_bid_format_parser as bid_parser
from app.utils import bfs_format_parser as bfs_parser
from app.utils import datetime_utils
from app.utils import excel_error_formatter
from app.utils import logging_utils
from app.utils import pincode_distance
from app.utils import pincode_lookup
from app.utils import sectioned_rfq_format_parser as sectioned


class Session:
    def __init__(self, rows=(), rowcount=1):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.executed = []
        self.added = []
        self.added_all = []
        self.query_result = SimpleNamespace(all=lambda: [])

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return SimpleNamespace(
            keys=lambda: ["value"],
            fetchall=lambda: self.rows,
            rowcount=self.rowcount,
        )

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added_all.append(list(values))

    def query(self, *_args):
        return self.query_result


# Task utilities and BFS notifications -------------------------------------


def test_task_utils_falsey_phone_rows_and_bfs_transaction_edges(monkeypatch):
    db = Session(rows=[("123",), (None,), ("",)])
    monkeypatch.setattr(task_utils, "get_db_session", lambda: db)
    assert task_utils.get_users_active_in_last_24hrs(["", None]) == set()
    assert db.closed

    commit_failure = Session()
    commit_failure.commit = Mock(side_effect=RuntimeError("commit"))
    monkeypatch.setattr(bfs, "get_remote_db_session", lambda: commit_failure)
    assert bfs.mark_notification_sent("bfs-1") is False
    assert commit_failure.rollbacks == 1 and commit_failure.closed

    batch_failure = Session(rowcount=3)
    batch_failure.commit = Mock(side_effect=RuntimeError("batch commit"))
    monkeypatch.setattr(bfs, "get_remote_db_session", lambda: batch_failure)
    assert bfs.mark_notifications_sent_batch(["one", "two"]) == 0
    assert batch_failure.rollbacks == 1


@pytest.mark.asyncio
async def test_bfs_no_successful_results_and_wrapper_success(monkeypatch):
    monkeypatch.setattr(bfs, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs, "get_pending_bfs_notifications", lambda limit: [{"seller_phone": "9199", "bfs_user_uuid": "b1"}])
    monkeypatch.setattr(bfs, "get_users_active_in_last_24hrs", lambda phones: set())
    monkeypatch.setattr(bfs, "normalize_phone_for_comparison", lambda phone: phone.lstrip("+"))
    service = SimpleNamespace(send_bfs_bid_notifications_batch=AsyncMock(return_value={"results": [], "sent": 0, "failed": 1, "skipped": 0}))
    monkeypatch.setattr(bfs, "get_seller_notification_service", lambda: service)
    marked = Mock()
    monkeypatch.setattr(bfs, "mark_notifications_sent_batch", marked)
    result = await bfs.process_bfs_notifications()
    assert result["failed"] == 1
    marked.assert_not_called()

    monkeypatch.setattr(bfs, "process_bfs_notifications", AsyncMock(return_value={"status": "completed"}))
    monkeypatch.setattr(bfs.asyncio, "run", lambda coroutine: (coroutine.close() or {"status": "completed"}))
    assert bfs.process_bfs_seller_notifications.run() == {"status": "completed"}


# Category and daily vector rebuild tasks ----------------------------------


def test_category_sync_exception_orchestration_and_project_path(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_vector_search", True)
    original_path = list(category_sync.sys.path)
    project_path = category_sync.os.path.abspath(category_sync.os.path.join(category_sync.os.path.dirname(category_sync.__file__), "../.."))
    category_sync.sys.path[:] = [p for p in category_sync.sys.path if p != project_path]
    category_sync._add_project_path()
    assert project_path in category_sync.sys.path
    category_sync.sys.path[:] = original_path

    monkeypatch.setattr(category_sync, "_load_categories_from_remote", Mock(side_effect=RuntimeError("remote")))
    monkeypatch.setattr(category_sync, "_load_categories_from_local_db", lambda: {"Local": 1})
    monkeypatch.setattr(category_sync, "_create_category_embeddings", lambda categories, clear_existing=False: {"success": True, "total_categories": 1, "collection_count": 1})
    result = category_sync.sync_category_names.run()
    assert result["status"] == "completed"
    assert result["steps_failed"][0]["step"] == "remote_database_load"

    monkeypatch.setattr(category_sync, "_load_categories_from_remote", lambda: {"Remote": 1})
    monkeypatch.setattr(category_sync, "_load_categories_from_local_db", Mock(side_effect=RuntimeError("local")))
    monkeypatch.setattr(category_sync, "_create_category_embeddings", Mock(side_effect=RuntimeError("chroma")))
    result = category_sync.sync_category_names.run()
    assert result["status"] == "failed"
    assert result["steps_failed"][-1]["step"] == "embedding_creation"


def test_daily_mapping_batch_commit_and_rollback(monkeypatch):
    database = __import__("app.database", fromlist=["get_db_session"])
    models = __import__("app.models", fromlist=["CategoryMapping"])
    monkeypatch.setattr(daily_rebuild, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(database, "test_remote_connection", lambda: True)
    monkeypatch.setattr(database, "get_remote_item_categories", lambda: [{"category": "A", "item": f"Item {i}"} for i in range(501)])

    class Mapping:
        category = "category"
        item = "item"
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(models, "CategoryMapping", Mapping)
    db = Session()
    db.query_result = SimpleNamespace(all=lambda: [])
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    result = daily_rebuild._sync_category_mappings()
    assert result["inserted_count"] == 501
    assert len(db.added_all) == 2 and db.commits == 2 and db.closed

    failed = Session()
    failed.query_result = SimpleNamespace(all=Mock(side_effect=RuntimeError("query")))
    monkeypatch.setattr(database, "get_db_session", lambda: failed)
    result = daily_rebuild._sync_category_mappings()
    assert result["success"] is False and failed.rollbacks == 1 and failed.closed


# Log cleanup and seller matching ------------------------------------------


def test_log_cleanup_subdirectories_existing_archive_and_unlink_error(monkeypatch, tmp_path):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 1, 10)

    monkeypatch.setattr(cleanup, "datetime", FixedDateTime)
    log_dir = tmp_path / "logs"
    (log_dir / "app").mkdir(parents=True)
    (log_dir / "openai_interactions").mkdir()
    nested = log_dir / "app" / "app.log.2024-01-01"
    jsonl = log_dir / "openai_interactions" / "interactions_2024-01-01.jsonl"
    nested.write_text("nested", encoding="utf-8")
    jsonl.write_text("jsonl", encoding="utf-8")
    manager = cleanup.LogCleanupManager(str(log_dir), 7, 7)
    old_archive = manager.archive_dir / "logs_2020-01-01.tar.gz"
    old_archive.parent.mkdir()
    old_archive.write_bytes(b"old")
    assert nested in manager._find_old_logs()["2024-01-01"]
    assert jsonl in manager._find_old_logs()["2024-01-01"]
    assert old_archive in manager._find_old_archives()

    existing = manager.archive_dir / "logs_2024-01-01.tar.gz"
    existing.write_bytes(b"already")
    manager._archive_logs({"2024-01-01": [nested]})
    assert nested.exists()

    fresh = log_dir / "app_2024-01-02.log"
    fresh.write_text("fresh", encoding="utf-8")
    monkeypatch.setattr(cleanup.tarfile, "open", Mock())
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=RuntimeError("unlink")))
    manager._archive_logs({"2024-01-02": [fresh]})
    assert manager.stats["errors"] == 1


@pytest.mark.asyncio
async def test_seller_enhanced_candidate_success_exception_and_recording(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "enable_vector_search", True)
    monkeypatch.setattr(seller, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller, "get_rfq_item_categories", lambda _: ["Tools"])
    monkeypatch.setattr(seller, "get_users_active_in_last_24hrs", lambda phones: {"111"})
    monkeypatch.setattr(seller, "normalize_phone_for_comparison", lambda value: value.lstrip("+"))
    enhanced = SimpleNamespace(find_sellers_for_item=AsyncMock(return_value={"success": True, "sellers": [{"seller_id": "candidate"}]}))
    monkeypatch.setattr("app.services.enhanced_seller_matching_service.EnhancedSellerMatchingService", lambda: enhanced)
    standard = SimpleNamespace(select_sellers_for_rfq=AsyncMock(return_value={"subscribed_sellers": [{"seller_id": "s1", "phone_number": "+111"}], "unsubscribed_sellers": []}))
    monkeypatch.setattr(seller, "log_selected_sellers_to_remote", Mock(return_value=True))
    notification = SimpleNamespace(send_rfq_notifications=AsyncMock(return_value={"sent": 1, "failed": 0, "results": [{"seller_id": "s1", "success": True}]}))
    monkeypatch.setattr(seller, "SellerNotificationService", lambda: notification)
    recorded = Mock()
    record_notifications = seller.record_rfq_seller_notifications
    monkeypatch.setattr(seller, "record_rfq_seller_notifications", recorded)
    result = await seller.process_single_rfq_matching({"rfq_id": "r", "rfq_uuid": "u", "description": "pump"}, standard, set())
    assert result["success"] and result["notifications_sent"] == 1
    assert standard.select_sellers_for_rfq.await_args.kwargs["candidate_seller_ids"] == ["candidate"]

    enhanced.find_sellers_for_item.side_effect = RuntimeError("semantic")
    standard.select_sellers_for_rfq.side_effect = RuntimeError("standard")
    result = await seller.process_single_rfq_matching({"rfq_id": "r", "rfq_uuid": "u"}, standard, set())
    assert result["success"] is False and "standard" in result["error"]

    seller._fk_dropped = True
    monkeypatch.setattr(seller, "RFQSellerNotification", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(seller, "NotificationType", SimpleNamespace(initial_notification="initial"))
    db = Session()
    monkeypatch.setattr(seller, "get_db_session", lambda: db)
    record_notifications("r", [{"seller_id": "s", "success": True}, {"seller_id": "x", "success": False}])
    assert len(db.added) == 1 and db.commits == 1 and db.closed


# Taxonomy and vector store tasks ------------------------------------------


def _patch_taxonomy_loader(monkeypatch, process):
    module = SimpleNamespace(process_category_mappings=process)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda target: target.__dict__.update(module.__dict__)))
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr("importlib.util.module_from_spec", lambda _spec: SimpleNamespace())


@pytest.mark.asyncio
async def test_taxonomy_sequential_retries_and_checkpoint_failure(monkeypatch):
    monkeypatch.setattr(taxonomy, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1))
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"category": str(i)} for i in range(3)])
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({"next_batch": 0})), set=AsyncMock(), delete=AsyncMock(side_effect=RuntimeError("delete")))
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())
    process = AsyncMock(side_effect=[
        {"success": True, "processed_count": 2, "created_categories": 1, "existing_categories": 0, "errors": []},
        {"success": False, "error": "bad"},
        {"success": False, "error": "bad"},
        {"success": False, "error": "bad"},
    ])
    _patch_taxonomy_loader(monkeypatch, process)
    result = await taxonomy.build_taxonomy_async(None, batch_size=2, process_all=True, parallel=False, resume=True)
    assert result["success"] is False
    assert result["total_errors"] == 1
    assert result["batches_processed"] == 1
    assert process.await_count == 4


def test_vector_timeout_cleanup_error_and_embedding_exception(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    monkeypatch.setattr(vector_sync, "get_settings", Mock(side_effect=SoftTimeLimitExceeded()))
    assert vector_sync.sync_vector_store.run()["status"] == "timeout"

    database = __import__("app.database", fromlist=["get_db_session"])
    models = __import__("app.models", fromlist=["SellerLearningMapping"])
    db = Session()
    db.query_result = SimpleNamespace(filter=Mock(side_effect=RuntimeError("delete")))
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(database, "execute_remote_query", lambda *_: [{"uuid": "buyer"}])
    monkeypatch.setattr(models, "SellerLearningMapping", SimpleNamespace(seller_id=SimpleNamespace(in_=lambda ids: ids)))
    result = vector_sync._cleanup_buyer_mappings()
    assert result["success"] is False and db.rollbacks == 1 and db.closed

    monkeypatch.setattr(vector_sync, "sys", SimpleNamespace(path=[]))
    monkeypatch.setitem(__import__("sys").modules, "create_category_embeddings", SimpleNamespace(create_unified_vector_store=Mock(side_effect=RuntimeError("chroma"))))
    result = vector_sync._run_vector_embedding_creation()
    assert result["success"] is False and "chroma" in result["error"]


@pytest.mark.asyncio
async def test_vector_mapping_no_learning_categories_and_missing_selected_category(monkeypatch):
    database = __import__("app.database", fromlist=["get_db_session"])
    models = __import__("app.models", fromlist=["LearningCategory", "SellerLearningMapping"])
    openai_module = __import__("app.services.openai_service", fromlist=["OpenAIService"])
    adapter_module = __import__("app.services.seller_data_adapter", fromlist=["SellerDataAdapter"])
    db = Session()
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: SimpleNamespace(map_seller_categories_batch=AsyncMock()))
    monkeypatch.setattr(models, "LearningCategory", SimpleNamespace())
    monkeypatch.setattr(models, "SellerLearningMapping", SimpleNamespace())
    monkeypatch.setattr(adapter_module, "SellerDataAdapter", lambda: SimpleNamespace(test_connection=lambda: True, get_sellers_from_remote=lambda: [SimpleNamespace(seller_id="s", categories=["tools"])]))
    db.query_result = SimpleNamespace(all=lambda: [], filter=lambda *_: db.query_result, first=lambda: None)
    assert (await vector_sync._async_map_sellers_to_categories())["error"] == "No learning categories found"


# WhatsApp reports, tools, and utility edges -------------------------------


def _patch_report(monkeypatch, tmp_path, sessions):
    analytics = SimpleNamespace(analyze_daily_conversations=AsyncMock(return_value={"success": True, "total_sessions": len(sessions), "sessions_df": sessions}))
    excel_path = str(tmp_path / "report.xlsx")
    monkeypatch.setattr(whatsapp, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(whatsapp.os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(whatsapp, "ConversationAnalyticsService", lambda: analytics)
    monkeypatch.setattr(whatsapp, "EnhancedExcelReportService", lambda: SimpleNamespace(generate_report=lambda **kwargs: excel_path))
    monkeypatch.setattr(whatsapp, "_send_excel_reports_with_api_session", AsyncMock(return_value={"status": "Success"}))
    return analytics, excel_path


@pytest.mark.asyncio
async def test_whatsapp_nonempty_csv_excel_only_and_removal_error(monkeypatch, tmp_path):
    sessions = pd.DataFrame([{"created_at": "2025-01-01", "phone_number": "1", "conversation_history": "hi"}])
    analytics, excel_path = _patch_report(monkeypatch, tmp_path, sessions)
    monkeypatch.setattr(whatsapp.os.path, "exists", lambda path: path.endswith(".xlsx"))
    monkeypatch.setattr(whatsapp.os, "remove", Mock(side_effect=RuntimeError("locked")))
    result = await whatsapp.run_whatsapp_report_automation_async(None, "2025-01-01")
    assert result["status"] == "completed"
    assert whatsapp._send_excel_reports_with_api_session.await_args.args[0] == [excel_path]
    csv_files = list((tmp_path / "app" / "reportStore").glob("*.csv"))
    assert csv_files and "phone_number" in csv_files[0].read_text(encoding="utf-8")
    analytics.analyze_daily_conversations.assert_awaited_once_with(date(2025, 1, 1))


@pytest.mark.asyncio
async def test_tools_serializer_selection_and_confirmation_edges(tmp_path, monkeypatch):
    logger = InteractionLogger(str(tmp_path))
    assert logger._json_serializer(object()).startswith("<")
    interaction_logger_module._interaction_logger = None
    first = interaction_logger_module.get_interaction_logger()
    assert interaction_logger_module.get_interaction_logger() is first

    service = SimpleNamespace(default_model="model", client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))
    tool = UserSelectionTool(service)
    options = [{"number": 1, "profile": {"email": "buyer@example.com", "role": "buyer"}}, {"number": 2, "action": "Register new", "display": "Register"}]
    assert tool._rule_based_analysis("example.com", options)["selected_option"] == 1
    assert tool._rule_based_analysis("register me as seller", options)["register"]["type"] == "seller"
    (tmp_path / "profile_selection").mkdir()
    monkeypatch.setattr(tool, "prompts_dir", tmp_path)
    (tmp_path / "profile_selection" / "user_selection_analysis.txt").write_text("system", encoding="utf-8")
    (tmp_path / "user_selection_analysis.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool, "tools_dir", tmp_path)
    service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="message", arguments="{}")])
    assert (await tool._ai_based_analysis("unclear", options))["confidence"] == 0.2
    service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="function_call", arguments="{")])
    assert (await tool._ai_based_analysis("broken", options))["confidence"] == 0.1

    confirmation = ConfirmationTool(SimpleNamespace(parse_confirmation_response=AsyncMock(return_value="no")))
    assert await confirmation.parse_confirmation("not okay") == "no"
    assert await confirmation.parse_confirmation("ambiguous") == "no"


def test_logging_pincode_lookup_and_datetime_edges(monkeypatch):
    logging_utils.clear_user_phone_context()
    logging_utils._user_phone_context.set(None)
    logging_utils._thread_local.phone_number = "thread-phone"
    assert logging_utils.get_user_phone_context() == "thread-phone"
    logging_utils.clear_user_phone_context()

    record = logging.LogRecord("module", logging.INFO, __file__, 1, "message", (), None)
    assert "N/A | module" in logging_utils.CustomFormatter().format(record)
    assert datetime_utils.format_date_for_validation_error("2025-11-11") == "11th Nov 2025"

    pincode_distance.clear_pincode_cache()
    location = SimpleNamespace(latitude=18.5, longitude=73.8, isna=lambda: SimpleNamespace(all=lambda: True))
    nomi = SimpleNamespace(query_postal_code=Mock(return_value=location))
    monkeypatch.setattr(pincode_distance, "_nomi", nomi)
    assert pincode_distance.get_coordinates_from_pincode("411010") is None
    assert pincode_distance.get_coordinates_from_pincode("411010") is None
    nomi.query_postal_code.assert_called_once_with("411010")

    response = Mock()
    response.raise_for_status.side_effect = requests.exceptions.HTTPError("http")
    monkeypatch.setattr(pincode_lookup.requests, "get", lambda *args, **kwargs: response)
    assert pincode_lookup.get_pincode_details("411005", max_retries=2) is None
    monkeypatch.setattr(pincode_lookup, "get_pincode_details", lambda _: [])
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("411005")) is None


def test_parser_and_formatter_residual_edges(monkeypatch):
    assert bfs_parser.parse_bfs_format("Product 1: Laptop\nnot a second product") ["products"]
    assert bfs_parser.parse_bfs_format("header only") ["error"]
    normalized = bid_parser._normalize_bid_text_newlines("x Seller Price: 1 a. Price: 2 b. Qty: 1 2.")
    assert normalized.count("\n") >= 3
    assert bid_parser._extract_bid_format_section("instructions only") == ("instructions only", "")
    item = {"description": "A" * 60, "specification": "S" * 30, "sellPrice": 1, "availableQuantity": 1}
    generated = bid_parser.generate_bid_format([item])
    assert len(bid_parser._build_item_key(item).split(" -- ")[0]) == 50
    assert bid_parser.parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 1.5").replace("b. Your Qty:", "b. Your Qty: 1"), [item])["bids"]

    delivery = "Delivery Date: 1 Jan 2025\nDelivery Pincode: 411005\nDelivery City: Pune\nDelivery State: MH\nrandom note"
    assert sectioned.parse_delivery_format(delivery)["additional_text"] == "random note"
    assert sectioned.parse_items_format("Items (1):\nItem 1:\nQty: 1\nPlease confirm")["error"]
    assert sectioned._sanitize_text("a\x00b\x01c") == "abc"
    assert sectioned._format_quantity(2.0) == "2"
    assert sectioned._format_quantity("bad") == "bad"
    monkeypatch.setattr(sectioned, "MAX_MESSAGE_LENGTH", 10)
    assert sectioned.generate_items_display([{"description": "x" * 100, "quantity": 1, "remarks": "y" * 100}] * 20).startswith("20 items")

    class BadDetails(dict):
        def get(self, key, default=None):
            if key == "actual_rows":
                raise RuntimeError("format")
            return super().get(key, "fallback")

    assert excel_error_formatter.ExcelErrorFormatter.format_error("row_limit", BadDetails(message="fallback")) == "File Processing Failed: fallback"
