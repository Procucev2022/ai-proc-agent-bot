"""Deterministic residual coverage for tasks, utilities, tools, and services.

Every external boundary in this module is replaced with a local fake or mock.
The cases focus on residual empty, retry, failure, and fallback branches rather
than live database, Redis, OpenAI, WhatsApp, HTTP, or email calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pandas as pd
import pytest
import requests

from app.models import ConversationOutcome, UserType
from app.tasks import bfs_notification_task as bfs_task
from app.tasks import log_cleanup_task as cleanup_task
from app.tasks import seller_matching_task as seller_task
from app.tasks import taxonomy_build_task as taxonomy_task
from app.tasks import vector_store_sync_task as vector_task
from app.tasks import whatsapp_report_automation_task as report_task
from app.tools import interaction_logger as interaction_tool
from app.tools import user_selection_tool as selection_tool
from app.utils import bfs_bid_format_parser as bid_parser
from app.utils import bfs_format_parser as bfs_parser
from app.utils import logging_utils
from app.utils import pincode_lookup
from app.utils import sectioned_rfq_format_parser as sectioned_parser
from app.services import daily_aggregation_service as aggregation_module
from app.services import daily_summary_service as summary_module
from app.services import email_service as email_module
from app.services import entity_service as entity_module
from app.services import excel_processing_service as excel_processing_module
from app.services import excel_validation_service as excel_validation_module
from app.services import exit_service as exit_module
from app.services import inactivity_timeout_service as timeout_module
from app.services import intent_service as intent_module
from app.services import learning_categorization_service as learning_module
from app.services import message_queue_service as queue_module
from app.services import openai_service as openai_module
from app.services import opt_out_service as opt_out_module
from app.services import profile_selection_service as profile_module
from app.services import registration_service as registration_module
from app.services import rfq_background_service as background_module
from app.services import rfq_intimation_service as intimation_module
from app.services import seller_categorization_service as seller_cat_module
from app.services import seller_recommendation_service as recommendation_module
from app.services import session_management_service as session_module
from app.services import user_cache_service as cache_module
from app.services import vendor_service as vendor_module
from app.services import webhook_health_monitor_service as health_module
from app.services import welcome_message_service as welcome_module
from app.services import whatsapp_service as whatsapp_module


class QueryFake:
    """Small chainable query fake shared by database-facing unit cases."""

    def __init__(self, values=(), first_value=None, scalar_value=0, delete_value=0):
        self.values = list(values)
        self.first_value = first_value
        self.scalar_value = scalar_value
        self.delete_value = delete_value

    def filter(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.first_value

    def scalar(self):
        return self.scalar_value

    def delete(self, **kwargs):
        return self.delete_value


class ContextDB:
    def __init__(self, queries=(), default_query=None):
        self.queries = list(queries)
        self.default_query = default_query or QueryFake()
        self.added = []
        self.commits = 0
        self.refreshes = 0
        self.closed = False
        self.rollbacks = 0
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def query(self, *args, **kwargs):
        return self.queries.pop(0) if self.queries else self.default_query

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def refresh(self, value):
        self.refreshes += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return SimpleNamespace(rowcount=0)


class RedisLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = 0

    async def acquire(self):
        return self.acquired

    async def release(self):
        self.released += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RedisFake:
    def __init__(self):
        self.deleted = []
        self.setex_calls = []
        self.values = {}
        self.scanned = []
        self.lock_obj = RedisLock()

    def lock(self, *args, **kwargs):
        return self.lock_obj

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.values[key] = value
        return True

    async def get(self, key, *args, **kwargs):
        return self.values.get(key)

    async def exists(self, key):
        return bool(self.values.get(key))

    async def delete(self, key, *args):
        keys = (key,) + args
        self.deleted.extend(keys)
        return len(keys)

    async def scan(self, *args, **kwargs):
        if self.scanned:
            return self.scanned.pop(0)
        return 0, []


# ---------------------------------------------------------------------------
# Task residuals
# ---------------------------------------------------------------------------


def test_residual_bfs_cleanup_and_seller_helpers(monkeypatch, tmp_path):
    class Session:
        def __init__(self, rowcount=0, fail_commit=False):
            self.rowcount = rowcount
            self.fail_commit = fail_commit
            self.closed = False
            self.rollbacks = 0

        def execute(self, *args):
            if self.fail_commit:
                raise RuntimeError("execute failed")
            return SimpleNamespace(keys=lambda: ["x"], fetchall=lambda: [(1,)], rowcount=self.rowcount)

        def commit(self):
            if self.fail_commit:
                raise RuntimeError("commit failed")

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    failed = Session(rowcount=1, fail_commit=True)
    monkeypatch.setattr(bfs_task, "get_remote_db_session", lambda: failed)
    assert bfs_task.mark_notification_sent("bfs-1") is False
    assert failed.rollbacks == 1 and failed.closed

    empty_result = Session(rowcount=0)
    monkeypatch.setattr(bfs_task, "get_remote_db_session", lambda: empty_result)
    assert bfs_task.get_pending_bfs_notifications(2) == [{"x": 1}]

    monkeypatch.setattr(seller_task, "execute_remote_query", lambda *_: [])
    assert seller_task.get_rfq_notification_progress("rfq") == {
        "subscribed_notified": 0,
        "unsubscribed_notified": 0,
    }
    assert seller_task.get_sellers_already_notified_for_rfq("rfq") == set()
    assert seller_task.extract_delivery_location({"delivery_city": "Pune", "delivery_pincode": 411005}) == {
        "city": "Pune",
        "pincode": 411005,
    }

    manager = Mock()
    manager.run.return_value = {"errors": 1, "logs_archived": 0}
    monkeypatch.setattr(cleanup_task, "get_settings", lambda: SimpleNamespace(
        PROJECT_ROOT=str(tmp_path), log_retention_days=7, archive_retention_days=14
    ))
    monkeypatch.setattr(cleanup_task, "LogCleanupManager", lambda **kwargs: manager)
    assert cleanup_task.cleanup_logs.run()["status"] == "completed_with_errors"
    manager.run.return_value = {"errors": 0, "logs_archived": 1}
    assert cleanup_task.cleanup_logs.run()["status"] == "completed"


def test_residual_cleanup_date_and_seller_recording_edges(monkeypatch, tmp_path):
    manager = cleanup_task.LogCleanupManager(str(tmp_path), 7, 7, [r"keep"])
    assert manager._extract_date_from_filename("bad-name.log") is None
    assert manager._extract_date_from_filename("app_2024-02-30.log") is None
    assert manager._should_exclude("app_keep_2024-01-01.log") is True
    assert manager._should_exclude("app_2024-01-01.log") is False

    monkeypatch.setattr(seller_task, "get_remote_db_session", lambda: SimpleNamespace(
        execute=Mock(side_effect=RuntimeError("write")), rollback=Mock(), close=Mock()
    ))
    assert seller_task.log_selected_sellers_to_remote("u", "r", [{"seller_id": "s"}]) is False

    # A notification list with no successful entries is intentionally a no-op.
    seller_task.record_rfq_seller_notifications("r", [{"seller_id": "s", "success": False}])

    class Inspector:
        def get_foreign_keys(self, _table):
            raise RuntimeError("inspect failed")

    db = SimpleNamespace(bind=object(), rollback=Mock())
    monkeypatch.setattr(seller_task, "get_db_session", lambda: db)
    monkeypatch.setattr("sqlalchemy.inspect", lambda *_: Inspector())
    seller_task._fk_dropped = False
    seller_task._ensure_seller_id_fk_dropped(db)
    assert db.rollback.called


@pytest.mark.asyncio
async def test_residual_bfs_notification_template_branch(monkeypatch):
    monkeypatch.setattr(bfs_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs_task, "get_pending_bfs_notifications", lambda limit: [{
        "bfs_user_uuid": "u1", "seller_phone": "9199", "buy_price": None,
        "listed_price": 10, "item_description": "pump"
    }])
    monkeypatch.setattr(bfs_task, "get_users_active_in_last_24hrs", lambda phones: set())
    monkeypatch.setattr(bfs_task, "normalize_phone_for_comparison", lambda p: p.lstrip("+"))
    service = SimpleNamespace(send_bfs_bid_notifications_batch=AsyncMock(return_value={
        "results": [], "sent": 0, "failed": 0, "skipped": 1
    }))
    monkeypatch.setattr(bfs_task, "get_seller_notification_service", lambda: service)
    mark = Mock()
    monkeypatch.setattr(bfs_task, "mark_notifications_sent_batch", mark)
    result = await bfs_task.process_bfs_notifications()
    assert result["template_messages"] == 1 and result["interactive_messages"] == 0
    mark.assert_not_called()


@pytest.mark.asyncio
async def test_residual_taxonomy_single_batch_and_disabled(monkeypatch):
    redis = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock(), delete=AsyncMock())
    monkeypatch.setattr(taxonomy_task.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy_task, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=False))
    monkeypatch.setattr(taxonomy_task, "get_remote_item_categories", Mock(side_effect=AssertionError("not called")))
    loader_module = SimpleNamespace(process_category_mappings=AsyncMock(return_value={
        "success": True, "processed_count": 2, "created_categories": 1, "existing_categories": 1
    }))
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda target: target.__dict__.update(loader_module.__dict__)))
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr("importlib.util.module_from_spec", lambda _: SimpleNamespace())
    result = await taxonomy_task.build_taxonomy_async(None, batch_size=2, process_all=False, parallel=False, resume=False)
    assert result["success"] is True and result["processed_count"] == 2
    loader_module.process_category_mappings.return_value = {"success": False, "error": "rejected"}
    result = await taxonomy_task.build_taxonomy_async(None, batch_size=2, process_all=False, parallel=False, resume=False)
    assert result["success"] is False and result["error"] == "rejected"


def test_residual_vector_sync_disabled_mapping_failure_and_trigger(monkeypatch):
    monkeypatch.setattr(vector_task, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=False))
    assert vector_task.sync_vector_store.run()["status"] == "skipped"

    monkeypatch.setattr(vector_task, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=True))
    monkeypatch.setattr(vector_task, "_run_seller_category_mapping", lambda: {"success": False, "error": "mapping"})
    assert vector_task.sync_vector_store.run()["status"] == "partial_failure"

    queued = SimpleNamespace(id="vector-1")
    monkeypatch.setattr(vector_task.sync_vector_store, "apply_async", Mock(return_value=queued))
    result = vector_task.trigger_vector_store_sync(True)
    assert result["task_id"] == "vector-1" and result["cleanup_buyer_mappings"] is True


@pytest.mark.asyncio
async def test_residual_report_empty_csv_and_email_failure(monkeypatch, tmp_path):
    analytics = SimpleNamespace(analyze_daily_conversations=AsyncMock(return_value={
        "success": True, "total_sessions": 0, "sessions_df": pd.DataFrame()
    }))
    monkeypatch.setattr(report_task, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(report_task.os, "getcwd", lambda: str(tmp_path))
    (tmp_path / "app" / "reportStore").mkdir(parents=True)
    monkeypatch.setattr(report_task, "ConversationAnalyticsService", lambda: analytics)
    report_path = str(tmp_path / "report.xlsx")
    monkeypatch.setattr(report_task, "EnhancedExcelReportService", lambda: SimpleNamespace(
        generate_report=lambda **kwargs: report_path
    ))
    monkeypatch.setattr(report_task.os.path, "exists", lambda path: True)
    monkeypatch.setattr(report_task, "_send_excel_reports_with_api_session", AsyncMock(return_value={
        "status": "Failure", "message": "mail unavailable"
    }))
    result = await report_task.run_whatsapp_report_automation_async(None, "2025-01-02")
    assert result["status"] == "failed" and result["step"] == "email_sending"
    csv_path = tmp_path / "app" / "reportStore" / "Daily_Chats_2025-01-02.csv"
    assert csv_path.exists() and "created_at" in csv_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsers, logging, pincode lookup, and tools
# ---------------------------------------------------------------------------


def test_residual_parser_and_display_edges():
    assert bfs_parser.parse_bfs_format("Product 1: Laptop\n\nnot a product")["error"]
    assert bfs_parser.generate_bfs_display_with_missing([{}])[1] == ["Product name"]
    assert bid_parser.parse_bid_format("1. Dell -- Age: 1yr:\n a. Your Price: 5\n b. Your Qty: 1", [
        {"description": "Dell", "availableQuantity": 1}
    ])["bids"][0]["quantity"] == 1
    assert bid_parser.parse_bid_format("not a header", [])["error"]
    assert bid_parser.generate_bid_summary([{"key": "x", "price": 1, "quantity": 0}]) == ""

    delivery = sectioned_parser.parse_delivery_format(
        "Intro\nDelivery Date: 12 Nov 2025\nDelivery Pincode: 411005\nDelivery City: Pune\nDelivery State: MH\nPlease confirm"
    )
    assert delivery["pincode"] == "411005" and delivery["additional_text"] == "Please confirm"
    assert "6-digit" in sectioned_parser.parse_delivery_format("Delivery Date: tomorrow\nDelivery Pincode: 123")["error"]
    items = sectioned_parser.parse_items_format(
        "RFQ Items (1):\nItem 1: Pump\nQty: 2\nSpecification: UoM: pcs, steel\nPlease confirm"
    )
    assert items["items"][0]["unitofMeasures"] == "pcs" and items["additional_text"] == "Please confirm"
    assert "missing" in sectioned_parser.parse_items_format("Item 1: Pump")["error"]
    assert sectioned_parser._format_quantity(2.5) == "2.5"
    assert sectioned_parser._format_quantity("bad") == "bad"
    assert sectioned_parser._sanitize_text("a\x00b") == "ab"
    assert sectioned_parser._truncate_text("abcdef", 5) == "ab..."
    assert sectioned_parser.validate_delivery_completeness({}) is False
    assert sectioned_parser.validate_items_completeness([{"description": "x", "quantity": 1}]) is True
    assert sectioned_parser.generate_delivery_display_with_invalid_pincode({"deliveryDate": "today"}).endswith("[Valid 6-digit pin-code]")


def test_residual_logging_decorators_and_database_failure(monkeypatch, caplog):
    logger = logging.getLogger("residual-logger")
    logging_utils.log_info(logger, "hello", user_id="u", phone_number="p", source="unit")
    logging_utils.log_debug(logger, "debug", user_id="u")
    logging_utils.log_error(logger, "failed", error=RuntimeError("boom"), phone_number="p")
    logging_utils.log_error(logger, "plain")

    db = ContextDB()
    db.add = Mock(side_effect=RuntimeError("db add"))
    monkeypatch.setattr("app.database.get_db_session", lambda: db)
    logging_utils.log_to_database("ERROR", "message", service="unit")
    assert "Failed to log to database" in caplog.text

    @logging_utils.log_service_method("residual")
    def sync_ok(value):
        return value + 1

    @logging_utils.log_service_method("residual")
    async def async_ok(value):
        return value + 1

    @logging_utils.log_service_method("residual")
    def sync_fail():
        raise ValueError("sync")

    assert sync_ok(1) == 2
    assert asyncio.run(async_ok(1)) == 2
    with pytest.raises(ValueError):
        sync_fail()


def test_residual_pincode_retry_exhaustion(monkeypatch):
    sleeps = Mock()
    monkeypatch.setattr(pincode_lookup.time, "sleep", sleeps)
    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(side_effect=requests.exceptions.ConnectionError("offline")))
    assert pincode_lookup.get_pincode_details("411005", max_retries=2) is None
    assert sleeps.call_count == 1

    monkeypatch.setattr(pincode_lookup.requests, "get", Mock(side_effect=requests.exceptions.Timeout()))
    assert pincode_lookup.get_pincode_details("411005", max_retries=1) is None
    sleeps.assert_called_once()
    assert asyncio.run(pincode_lookup.get_location_from_pincode_async("bad")) is None


def test_residual_interaction_logger_write_error_and_selection_fallback(tmp_path, monkeypatch):
    logger = interaction_tool.InteractionLogger(str(tmp_path))
    monkeypatch.setattr("builtins.open", Mock(side_effect=OSError("disk full")))
    logger.log_error("intent", "hello", "disk")

    tool = selection_tool.UserSelectionTool(SimpleNamespace())
    options = [{"number": 1, "profile": {"email": "buyer@example.com", "role": "buyer"}}]
    assert tool._rule_based_analysis("buyer@example.com", options)["selected_option"] == 1
    assert tool._rule_based_analysis("unclear", options)["selected_option"] is None
    assert tool._detect_registration_intent("register me as seller") == "seller"
    monkeypatch.setattr(tool, "_rule_based_analysis", Mock(side_effect=RuntimeError("rules")))
    result = asyncio.run(tool.analyze_user_selection("unclear", options))
    assert result["requires_clarification"] is True


@pytest.mark.asyncio
async def test_residual_selection_ai_success_and_openai_exception(tmp_path):
    service = SimpleNamespace(default_model="model", client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))
    tool = selection_tool.UserSelectionTool(service)
    service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(
        type="function_call", arguments=json.dumps({
            "selected_option": 1, "confidence": 0.9, "reasoning": "email", "alternative_matches": [],
            "requires_clarification": False, "register": {"type": None}
        })
    )])
    assert (await tool._ai_based_analysis("buyer", [{"number": 1, "profile": {"email": "buyer@example.com", "role": "buyer"}}]))["confidence"] == 0.9
    service.client.responses.create.side_effect = RuntimeError("openai")
    assert (await tool._ai_based_analysis("buyer", []))["confidence"] == 0.1


# ---------------------------------------------------------------------------
# Low-branch services
# ---------------------------------------------------------------------------


def _buyer_session(**overrides):
    values = {
        "user_type": UserType.buyer,
        "external_user_id": "buyer-1",
        "rfq_ids": ["rfq-1"],
        "rfq_id": None,
        "product_items": [{"description": "pump", "category": "Tools"}],
        "products_searched_count": 1,
        "total_rfq_responses_received": 2,
        "bfs_search_count": 1,
        "products_bid_for": {"products": [1]},
        "bids_accepted": {"count": 1},
        "rfqs_with_response": ["rfq-1"],
        "seller_responses": [],
        "counter_offers_accepted": [],
        "counter_offers_made": [],
        "extracted_entities": {"category": "Tools"},
        "created_at": datetime(2025, 1, 1),
        "completed_at": datetime(2025, 1, 1, 0, 30),
        "last_activity_at": datetime(2025, 1, 1, 0, 45),
        "session_state": SimpleNamespace(value="completed"),
        "parent_session_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_residual_daily_aggregation_commit_failure_and_metric_update(monkeypatch):
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    session = _buyer_session()
    db = ContextDB(default_query=QueryFake(values=[session]))
    db.commit = Mock(side_effect=RuntimeError("commit"))
    service._calculate_buyer_summary_metrics = Mock(return_value={"x": 1})
    service._calculate_seller_summary_metrics = Mock(return_value={"y": 1})
    service._calculate_category_summary_metrics = Mock(return_value={"z": 1})
    service._update_rolling_windows = Mock()
    monkeypatch.setattr(aggregation_module, "get_db_session", lambda: db)
    assert service.run_daily_aggregation(date(2025, 1, 1)) is False

    existing = SimpleNamespace(metric_data={}, is_complete=False)
    db2 = ContextDB(default_query=QueryFake(first_value=existing))
    service._store_metrics(db2, date(2025, 1, 1), "buyer_summary", {"count": 2})
    assert existing.metric_data == {"count": 2} and existing.is_complete is True


def test_residual_daily_metrics_empty_categories_and_rolling_defaults():
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    empty = service._calculate_buyer_summary_metrics([], date(2025, 1, 1))
    assert empty["avg_products_per_rfq"] == 0
    assert service._aggregate_buyer_rolling_metrics([]) == {}
    assert service._aggregate_seller_rolling_metrics([]) == {}
    assert service._aggregate_category_rolling_metrics([]) == {}
    session = _buyer_session(product_items=None, extracted_entities=[{"description": "Pump"}, {"category": "Tools"}])
    assert set(service._extract_categories_from_session(session)) == {"Pump", "Tools"}


@pytest.mark.asyncio
async def test_residual_daily_summary_existing_record_and_duration_error(monkeypatch):
    service = summary_module.DailySummaryService.__new__(summary_module.DailySummaryService)
    service.settings = SimpleNamespace(enable_daily_summarization=True)
    session = _buyer_session(seller_responses=["response"], parent_session_id="parent")
    existing = SimpleNamespace()
    db = ContextDB(queries=[QueryFake(values=[session]), QueryFake(values=[]), QueryFake(first_value=existing)])
    monkeypatch.setattr(summary_module, "get_db_session", lambda: db)
    result = await service.generate_daily_summary("buyer-1", date(2025, 1, 1))
    assert result is existing and existing.sessions_count == 1 and db.refreshes == 1
    assert service._calculate_duration(SimpleNamespace(created_at="bad", completed_at="bad", last_activity_at=None)) is None


@pytest.mark.asyncio
async def test_residual_email_missing_recipient_and_api_exception(monkeypatch):
    service = email_module.EmailService.__new__(email_module.EmailService)
    service.settings = SimpleNamespace(support_email="support@test", email_signature="sig", email_templates_path=".")
    service.templates_cache = {}
    service.email_api = SimpleNamespace(send_email=AsyncMock(side_effect=RuntimeError("smtp")))
    service._load_template = Mock(return_value={"to": "", "subject": "Hi", "body": "Body"})
    result = await service.send_email_by_template("missing-recipient", {})
    assert result["status"] == "Failure" and result["statusCode"] == "400"
    service._load_template.return_value = {"to": "a@test", "subject": "Hi", "body": "Body"}
    result = await service.send_email_by_template("api-error", {})
    assert result["statusCode"] == "500"
    assert service._process_template({"to": "support@procucev.com", "body": "{name}"}, {"name": "Ada"})["to"] == ["support@test"]


@pytest.mark.asyncio
async def test_residual_entity_quantity_limit_and_pincode_fallback(monkeypatch):
    service = entity_module.EntityService.__new__(entity_module.EntityService)
    violations = service._check_quantity_limits([
        {"description": "bulk", "quantity": "10000000001"},
        {"description": "bad", "quantity": "n/a"},
    ])
    assert violations[0]["description"] == "bulk"
    assert service._clean_invalid_descriptions([{"description": "item"}, {"description": "Pump"}])[0]["description"] is None
    monkeypatch.setattr(entity_module, "get_location_from_pincode_async", AsyncMock(return_value=None))
    result = await service._auto_fill_location_from_pincode([{"description": "pump", "pincode": "123"}])
    assert result[0]["pincode"] is None and "pincode_validation_error" in result[0]
    monkeypatch.setattr(entity_module, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "MH"}))
    result = await service._auto_fill_location_from_pincode([{"description": "pump", "pincode": "411005", "city": "Old"}])
    assert result[0]["city"] == "Pune" and result[0]["state"] == "MH"


@pytest.mark.asyncio
async def test_residual_excel_processing_skipped_rows_and_validation_skip(monkeypatch):
    service = excel_processing_module.ExcelProcessingService.__new__(excel_processing_module.ExcelProcessingService)
    service.openai_service = SimpleNamespace()
    service.target_columns = ["S.No", "ItemDescription", "Specification", "Uom", "Quantity", "Remarks"]
    monkeypatch.setattr(service, "_is_valid_excel_file", Mock(return_value=True))
    monkeypatch.setattr(service, "_validate_excel_structure", AsyncMock(return_value={"valid": True}))
    class ExcelFile:
        sheet_names = ["Sheet1"]
        def close(self):
            pass
    monkeypatch.setattr(excel_processing_module.pd, "ExcelFile", lambda _: ExcelFile())
    monkeypatch.setattr(excel_processing_module.pd, "read_excel", lambda *args, **kwargs: pd.DataFrame([["header"], ["value"]]))
    monkeypatch.setattr(service, "_process_excel_with_openai", AsyncMock(return_value={
        "success": True, "rfqs": [{"products": [{"description": "pump", "quantity": 1}]}],
        "processing_summary": {"total_rows_processed": 2, "total_products": 2, "total_products_extracted": 1,
                               "skipped_rows": 1, "skipped_items_summary": "missing quantity"}
    }))
    result = await service.process_excel_file(b"PK\x03\x04data", "items.xlsx")
    assert result["success"] is False and result["should_skip_rfq_creation"] is True

    validation = excel_validation_module.ExcelValidationService()
    validation._download_file_with_retry = AsyncMock(return_value=b"content")
    validation._validate_file_integrity = Mock(return_value={"valid": True})
    validation._validate_excel_content = Mock(return_value={"valid": True, "format": "xlsx"})
    validation._validate_excel_readability = AsyncMock(return_value={"valid": True})
    result = await validation.validate_excel_file_from_url("url", "items.xlsx", skip_content_validation=True)
    assert result["valid"] is True and result["validation_summary"]["structure_check"] == "skipped"


@pytest.mark.asyncio
async def test_residual_exit_declined_and_goodbye_error(monkeypatch):
    service = exit_module.ExitService.__new__(exit_module.ExitService)
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("send")))
    service.authentication_service = None
    service.session_manager = SimpleNamespace(save_session=AsyncMock())
    service.db_manager = SimpleNamespace()
    service.settings = SimpleNamespace(procucev_link="https://example")
    session = SimpleNamespace(workflow_state={"exit_pending": True}, conversation_history={"messages": []}, workflow_type=None)
    monkeypatch.setattr(exit_module, "restore_last_bot_message", AsyncMock())
    result = await service.handle_exit_confirmation("+1", session, False)
    assert result["status"] == "exit_aborted"
    assert await service._send_goodbye_message("+1") is False


@pytest.mark.asyncio
async def test_residual_timeout_activity_lock_and_completed_session_cleanup(monkeypatch):
    service = timeout_module.InactivityTimeoutService.__new__(timeout_module.InactivityTimeoutService)
    service.activity_key_ttl = 420
    service.timeout_seconds = 300
    service.worker_timeout_threshold = 135
    service.enabled = True
    redis = RedisFake()
    busy = RedisLock(acquired=False)
    redis.lock_obj = busy
    service.redis = redis
    await service.update_user_activity("+9199")
    assert redis.setex_calls

    redis.scanned = [(0, ["9199:last_activity"])]
    redis.values["9199:last_activity"] = str(0)
    redis_session = SimpleNamespace(get_session=AsyncMock(return_value={"workflow_type": "buy", "outcome": "completed"}))
    service.redis_session = redis_session
    monkeypatch.setattr(timeout_module.time, "time", lambda: 1000)
    await service._check_inactive_users()
    assert "9199:last_activity" in redis.deleted


@pytest.mark.asyncio
async def test_residual_intent_timeout_passthrough_and_learning_fuzzy_match():
    service = intent_module.IntentService.__new__(intent_module.IntentService)
    service.openai_service = SimpleNamespace(classify_intent=AsyncMock(return_value={"timeout_handled": True, "success": False}))
    assert (await service.classify_intent("anything"))["timeout_handled"] is True
    assert service._get_general_fallback_intent("please cancel") == ("cancel_workflow", 85)

    learning = learning_module.LearningCategorizationService.__new__(learning_module.LearningCategorizationService)
    assert learning._find_similar_l2(
        "Valves", {"Valves & Fittings", "Unrelated"}, {"Valves & Fittings": ("Hardware", 1)}, "Hardware"
    ) == "Valves & Fittings"
    assert learning._find_similar_l2("No match", set(), {}, "Hardware") is None


@pytest.mark.asyncio
async def test_residual_message_queue_malformed_batch_requeues(monkeypatch):
    service = queue_module.MessageQueueService.__new__(queue_module.MessageQueueService)
    redis = RedisFake()
    redis.values["1:outgoing"] = "not-json"
    redis.exists = AsyncMock(return_value=False)
    redis.lpop = AsyncMock(return_value="not-json")
    redis.lpush = AsyncMock()
    service.redis = redis
    monkeypatch.setattr(queue_module.asyncio, "create_task", Mock())
    await service._try_start_processing("1")
    redis.lpush.assert_awaited_once_with("1:outgoing", "not-json")


@pytest.mark.asyncio
async def test_residual_openai_field_validation_fallback_and_success(monkeypatch):
    service = openai_module.OpenAIService.__new__(openai_module.OpenAIService)
    service.tools_dir = Path(openai_module.__file__).parent.parent / "tools"
    service.default_model = "model"
    service._client_closed = False
    service._load_prompt = Mock(return_value="system")
    response = SimpleNamespace(output=[SimpleNamespace(type="message", arguments="{}")] )
    service._client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    result = await service.validate_field_value("quantity", "2", {})
    assert result["is_valid"] is True and result["validation_score"] == 80
    service._client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(
        type="function_call", arguments=json.dumps({"is_valid": False, "validation_score": 20, "reason": "bad"})
    )])
    result = await service.validate_field_value("quantity", "bad", {})
    assert result["is_valid"] is False and result["reason"] == "bad"
    service._client.responses.create.side_effect = RuntimeError("api")
    assert (await service.validate_field_value("quantity", "bad", {}))["is_valid"] is True


@pytest.mark.asyncio
async def test_residual_opt_out_remote_zero_rows_and_profile_registration_helpers(monkeypatch):
    service = opt_out_module.OptOutService.__new__(opt_out_module.OptOutService)
    db = ContextDB()
    db.execute = Mock(return_value=SimpleNamespace(rowcount=0))
    monkeypatch.setattr(opt_out_module, "get_remote_db_session", lambda: db)
    assert await service._update_remote_opt_out_status("seller", False) is False
    assert db.closed

    profile = profile_module.ProfileSelectionService.__new__(profile_module.ProfileSelectionService)
    assert profile._extract_user_name([{"user_data": {"fullName": "  Ada Lovelace "}}]) == "Ada"
    assert profile._extract_user_name([]) is None

    registration = registration_module.RegistrationService.__new__(registration_module.RegistrationService)
    assert registration._parse_button_response({"button_reply": {"id": "confirm_registration"}}) == "yes"
    assert registration._parse_button_response({"button_reply": {"id": "unknown"}}) is None
    assert registration._parse_button_response(4) is None


@pytest.mark.asyncio
async def test_residual_rfq_background_permission_and_seller_categorization(monkeypatch):
    background = object.__new__(background_module.RFQBackgroundService)
    background._active_jobs = {}
    background._job_stats = {"rfqs_processed": 0, "sellers_notified": 0, "notifications_sent": 0, "errors_occurred": 0}
    background.recommendation_service = SimpleNamespace(select_sellers_for_rfq=AsyncMock(return_value={"total_selected": 0}))
    background._fetch_rfq_data = AsyncMock(return_value={"rfq_id": "r"})
    assert (await background.process_approved_rfq("r"))["reason"] == "No qualifying sellers found"

    intimation = intimation_module.RFQIntimationService.__new__(intimation_module.RFQIntimationService)
    intimation.opt_out_service = SimpleNamespace(
        check_seller_notification_eligibility=AsyncMock(return_value={"eligible": False, "action": "send_permission_request"}),
        send_permission_request=AsyncMock(return_value={"success": True}),
    )
    intimation._get_seller_details = AsyncMock(return_value=SimpleNamespace(seller_id="s"))
    result = await intimation.send_rfq_notification("s", {"rfq_id": "r"})
    assert result["action"] == "permission_request_sent"

    seller_cat = seller_cat_module.SellerCategorizationService.__new__(seller_cat_module.SellerCategorizationService)
    seller_cat._processing_stats = {"sellers_processed": 0, "categories_mapped": 0, "openai_calls_made": 0, "errors_encountered": 0, "processing_time_total": 0}
    seller_cat._get_seller_details = AsyncMock(return_value=SimpleNamespace(seller_id="s", categories=["Tools"]))
    seller_cat._create_categorization_job = AsyncMock(return_value=SimpleNamespace(job_id="job"))
    seller_cat._generate_3_level_mapping_for_category = AsyncMock(return_value={"success": False, "error": "ai"})
    seller_cat._update_categorization_job = AsyncMock()
    result = await seller_cat.categorize_seller_categories("s")
    assert result["success"] is False and "No categories" in result["error"]


@pytest.mark.asyncio
async def test_residual_seller_recommendation_location_and_empty_selection(monkeypatch):
    service = recommendation_module.SellerRecommendationService.__new__(recommendation_module.SellerRecommendationService)
    service.default_config = {"MAX_SUBSCRIBED_SELLERS_PER_RFQ": 1, "MAX_UNSUBSCRIBED_SELLERS_PER_RFQ": 1,
                              "MAX_TIME_SINCE_LAST_MESSAGE_HOURS": 24, "MAX_TIME_SINCE_LAST_ACTIVE_HOURS": 24}
    service._load_system_config = AsyncMock(return_value=service.default_config)
    service._filter_sellers_by_category = AsyncMock(return_value=[])
    empty = await service.select_sellers_for_rfq({"rfq_id": "r", "categories": ["Tools"]})
    assert empty["total_selected"] == 0 and "reason" in empty["selection_metadata"]

    service.db_session = ContextDB(default_query=QueryFake(first_value=None))
    monkeypatch.setattr(recommendation_module.pincode_distance, "calculate_distance_between_pincodes", lambda *_: None)
    sellers = [SimpleNamespace(seller_id="s1", seller_name="One", location={"pincode": "411005"}),
               SimpleNamespace(seller_id="s2", seller_name="Two", location={})]
    result = await service._filter_sellers_by_location(sellers, {"pincode": "560001"})
    assert result == sellers


def test_residual_session_reset_cache_options_vendor_partial_and_whatsapp_retry(monkeypatch):
    session_service = session_module.SessionManagementService.__new__(session_module.SessionManagementService)
    session_service._validate_license = Mock(return_value=(True, "ok"))
    session_service.redis_enabled = False
    completed = SimpleNamespace(
        session_id="old-session", external_user_id="+1", retention_date=None,
        outcome=ConversationOutcome.completed, workflow_state={"old": 1}, conversation_history={"old": 1},
        extracted_entities={"old": 1}, completed_at=datetime.now(), workflow_type="old"
    )
    db_manager = SimpleNamespace(
        get_conversation_session=Mock(return_value=completed),
        save_conversation_session=Mock(return_value=completed)
    )
    session_service.db_manager = db_manager
    session_service.redis_session = SimpleNamespace()
    session_service._session_to_dict = Mock(return_value={"fresh": True})
    monkeypatch.setattr(session_module.SessionHelpers, "generate_session_id", lambda *_: "sid")
    result = asyncio.run(session_service.get_conversation_context("+1"))
    assert result is completed and completed.outcome is None and completed.workflow_state["extracted_entities"] == []
    db_manager.save_conversation_session.assert_called_once()

    cache = cache_module.UserCacheService.__new__(cache_module.UserCacheService)
    cache.get_user_data = AsyncMock(return_value=[{"id": "1", "username": "seller@test", "selfClient": False}])
    result = asyncio.run(cache.get_account_options_for_intent_switch("+1", "buy_something"))
    assert result["has_target_accounts"] is False and len(result["formatted_options"]) == 2

    vendor = vendor_module.VendorService.__new__(vendor_module.VendorService)
    vendor_obj = SimpleNamespace(vendor_services=["Tools"], geographic_coverage=["New York City"])
    assert vendor._calculate_relevance(vendor_obj, {"category": "Tools", "location": "York"}) == 50 + 15 + 2 + 1

    wa = whatsapp_module.WhatsAppService.__new__(whatsapp_module.WhatsAppService)
    wa.mock_mode = False
    wa.retry_service = SimpleNamespace(retry_with_backoff=AsyncMock(return_value={
        "success": False, "attempts": 3, "error": "exhausted"
    }))
    wa._track_message_in_history = AsyncMock()
    result = asyncio.run(wa.send_message("+1", "hello", session_id="sid"))
    assert result.success is False and result.error == "exhausted"
    wa._track_message_in_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_residual_health_network_welcome_failure_and_user_cache_error(monkeypatch):
    monitor = health_module.WebhookHealthMonitorService.__new__(health_module.WebhookHealthMonitorService)
    monitor.settings = SimpleNamespace(WHATSAPP_BASE_URL="https://api", WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p")
    monitor.response_threshold = 1
    monitor.api_timeout = 10
    monitor._get_session = AsyncMock(side_effect=aiohttp.ClientConnectionError("offline"))
    status, latency, error = await monitor._check_api_health()
    assert status is health_module.HealthStatus.CRITICAL and "Connection error" in error

    welcome = welcome_module.WelcomeMessageService.__new__(welcome_module.WelcomeMessageService)
    welcome.redis_service = AsyncMock()
    welcome.should_send_welcome = AsyncMock(return_value=True)
    welcome.redis_service.set = AsyncMock()
    welcome.redis_service.expireat = AsyncMock(return_value=True)
    response = await welcome.check_and_send_welcome("+1", SimpleNamespace(
        send_message=AsyncMock(return_value=whatsapp_module.MessageResponse(False, error="failed"))
    ))
    assert response is False

    cache = cache_module.UserCacheService.__new__(cache_module.UserCacheService)
    cache.redis_service = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("redis")))
    assert await cache.get_user_data("+1") is None


@pytest.mark.asyncio
async def test_residual_daily_summary_disabled_and_email_template_missing(monkeypatch):
    service = summary_module.DailySummaryService.__new__(summary_module.DailySummaryService)
    service.settings = SimpleNamespace(enable_daily_summarization=False)
    assert await service.generate_daily_summary("u") is None

    email = email_module.EmailService.__new__(email_module.EmailService)
    email.settings = SimpleNamespace(email_templates_path=".")
    email.templates_cache = {}
    monkeypatch.setattr(email_module.os.path, "exists", Mock(return_value=False))
    assert email._load_template("not-there") is None
