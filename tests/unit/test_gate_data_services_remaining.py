"""Deterministic branch coverage for the remaining data-oriented services.

External systems are represented by in-memory fakes or mocks.  No test in this
module opens a network connection, talks to Redis/Chroma, or uses a live DB.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.auto_categorization_service as auto_mod
import app.services.conversation_analytics_service as analytics_mod
import app.services.daily_aggregation_service as aggregation_mod
import app.services.daily_summary_service as summary_mod
import app.services.enhanced_auto_categorization_service as enhanced_cat_mod
import app.services.enhanced_excel_report_service as report_mod
import app.services.enhanced_seller_matching_service as matching_mod
import app.services.excel_processing_service as processing_mod
import app.services.excel_validation_service as validation_mod
import app.services.rfq_background_service as background_mod
import app.services.rfq_intimation_service as intimation_mod
import app.services.seller_categorization_service as seller_cat_mod
import app.services.seller_notification_service as notification_mod
import app.services.seller_recommendation_service as recommendation_mod
import app.services.seller_service as seller_mod
import app.services.webhook_health_monitor_service as health_mod
from app.models import RFQStatus, SessionState, UserType
from app.services.whatsapp_service import MessageResponse


class Query:
    def __init__(self, *, all_values=None, first=None, scalar=0, count=0, delete=0):
        self.all_value = [] if all_values is None else all_values
        self.first_value = first
        self.scalar_value = scalar
        self.count_value = count
        self.delete_value = delete

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def distinct(self, *args, **kwargs):
        return self

    def all(self):
        return self.all_value

    def first(self):
        return self.first_value

    def scalar(self):
        return self.scalar_value

    def count(self):
        return self.count_value

    def delete(self):
        return self.delete_value


class DB:
    def __init__(self, query=None):
        self.query_value = query or Query()
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.refresh = MagicMock()
        self.bind = "mock-bind"

    def query(self, *args, **kwargs):
        return self.query_value

    def add(self, value):
        self.added.append(value)


class DbContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        return False


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *args):
        return False


def session(**overrides):
    values = dict(
        session_id="S1", external_user_id="U1", created_at=datetime(2024, 1, 1),
        completed_at=datetime(2024, 1, 1, 1), last_activity_at=None,
        user_type=UserType.buyer, rfq_ids=["R1", None], rfq_id=None,
        product_items=[{"category": "Tools"}], products_bid_for=["p"],
        bids_accepted={"count": 1}, rfqs_with_response=["R1"], bfs_search_count=1,
        bfs_price_accepted=["p"], counter_offers_accepted={"count": 1},
        counter_offers_made=["offer"], seller_responses=["response"],
        extracted_entities={"category": "Tools", "description": "Drill"},
        parent_session_id=None, session_state=SessionState.completed,
        conversation_history={"messages": []},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def analytics_service():
    service = analytics_mod.ConversationAnalyticsService.__new__(analytics_mod.ConversationAnalyticsService)
    service.batch_size = 2
    service.settings = SimpleNamespace(procucev_db_name="proc", whatsapp_db="wa")
    service.openai_service = SimpleNamespace(
        tools_dir=".", default_model="model", _load_prompt=MagicMock(return_value="prompt"),
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())),
    )
    return service


def health_service(recipients=None):
    service = health_mod.WebhookHealthMonitorService.__new__(health_mod.WebhookHealthMonitorService)
    service.settings = SimpleNamespace(
        WHATSAPP_BASE_URL="http://wa", WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p",
        webhook_alert_state_ttl_seconds=60, webhook_health_monitoring_enabled=True,
    )
    service.response_threshold = 1
    service.api_timeout = 3
    service.grace_period = 0
    service.recovery_confirmations = 2
    service.warning_threshold = 2
    service.check_interval = 0
    service.alert_recipients = [] if recipients is None else recipients
    service.redis = MagicMock()
    service.email_service = MagicMock()
    service.worker_id = "worker"
    service.is_leader = False
    service._running = False
    service._session = None
    return service


# Analytics, daily aggregation, and daily summary ---------------------------


def test_analytics_remaining_shapes_dump_paths_and_remote_failures(monkeypatch):
    service = analytics_service()
    rows = [
        {"session_id": "B", "phone_number": "1", "user_type": "buyer", "buyer_identities": []},
        {"session_id": "S", "phone_number": "2", "user_type": "seller", "seller_identities": [{}]},
        {"session_id": "U", "phone_number": "3", "user_type": "unknown", "unknown_user_metrics": {}},
    ]
    buyer, seller, unknown = service._create_dataframes_from_sessions(rows, "2024-01-01")
    assert buyer.empty and not seller.empty and not unknown.empty
    assert service._extract_chat_sequence({"messages": [{"role": "user", "content": "plain"}, {"role": "assistant", "content": {}}]}) == "User: plain\nAssistant: {}"
    assert service._safe_json_value({}) is None

    db = DB(Query(first=None))
    service._dump_joined_buyer_df_to_db(pd.DataFrame(), db)
    service._dump_joined_seller_df_to_db(pd.DataFrame(), db)
    service._dump_unknown_df_to_db(pd.DataFrame(), db)
    service._dump_joined_seller_interest_to_fact_table(pd.DataFrame(), db)
    service._dump_bfs_search_df_to_db(pd.DataFrame(), db, date(2024, 1, 1))
    assert not db.added

    monkeypatch.setattr(analytics_mod, "get_remote_db_session", lambda: (_ for _ in ()).throw(RuntimeError("remote")))
    assert service._get_product_category_mapping(["x"]) == {}
    assert service._get_bfs_category_counts_combined(date(2024, 1, 1), db) == ({}, {})

    q = MagicMock()
    q.filter.return_value = q
    q.all.return_value = []
    q.first.return_value = None
    empty_db = MagicMock()
    empty_db.query.return_value = q
    service._calculate_rfqs_with_response = MagicMock(return_value=0)
    service._calculate_total_rfq_responses = MagicMock(return_value=0)
    service.calculate_and_store_daily_aggregates(date(2024, 1, 1), empty_db)
    service.calculate_and_store_seller_daily_aggregates(date(2024, 1, 1), empty_db)
    service.calculate_and_store_unknown_daily_aggregates(date(2024, 1, 1), empty_db)
    assert empty_db.query.called


@pytest.mark.asyncio
async def test_analytics_daily_full_branch_with_remote_frames(monkeypatch):
    service = analytics_service()
    query = MagicMock()
    query.with_entities.return_value = query
    query.filter.return_value = query
    query.all.return_value = [session()]
    db = MagicMock()
    db.query.return_value = query
    monkeypatch.setattr(analytics_mod, "get_db_session", lambda: DbContext(db))
    service._process_sessions_in_batches = AsyncMock(return_value={
        "date": "2024-01-01", "total_sessions": 1,
        "sessions": [{"session_id": "S", "phone": "1", "phone_number": "1", "user_type": "buyer",
                       "buyer_identities": [{"buyer_email": "b", "buyer_metrics": {}}]}],
    })
    service._query_remote_users = MagicMock(return_value=pd.DataFrame({"username": ["u"], "phone": ["1"], "date": ["2024-01-01"]}))
    service._query_remote_seller_rfqs = MagicMock(return_value=pd.DataFrame({"rfq_id": ["r"], "phone": ["2"], "username": ["s"]}))
    service._query_remote_counter_seller_bids = MagicMock(return_value=pd.DataFrame({"rfq_id": ["r"], "phone_number": ["2"], "username": ["s"], "date": ["2024-01-01"], "phone_clean": ["2"]}))
    service._query_remote_rfq_categories = MagicMock(return_value=pd.DataFrame({"rfq_id": ["r"]}))
    for name in ("_dump_joined_buyer_df_to_db", "_dump_joined_seller_df_to_db", "_dump_unknown_df_to_db",
                 "_dump_joined_seller_interest_to_fact_table", "calculate_and_store_daily_aggregates",
                 "calculate_and_store_category_aggregates", "calculate_and_store_seller_daily_aggregates",
                 "calculate_and_store_unknown_daily_aggregates"):
        setattr(service, name, MagicMock())
    result = await service.analyze_daily_conversations(date(2024, 1, 1))
    assert result["success"] and result["sessions_df"].shape[0] == 1


def test_daily_aggregation_alternate_shapes_and_seller_rolling(monkeypatch):
    service = aggregation_mod.DailyAggregationService.__new__(aggregation_mod.DailyAggregationService)
    buyer = session(
        products_searched_count=2, total_rfq_responses_received=3,
        rfq_ids=None, rfq_id="R2", product_items={"bad": True},
        products_bid_for={"products": ["a", "b"]}, bids_accepted=["a"],
        rfqs_with_response={"unique_rfqs": 2}, bfs_search_count=0,
    )
    result = service._calculate_buyer_summary_metrics([buyer], date.today())
    assert result["total_rfqs_submitted"] == 1 and result["products_bid_for"] == 2
    seller = session(user_type=UserType.seller, rfq_ids=None, rfq_id="R", seller_responses={"count": 2},
                     bids_accepted=["b"], counter_offers_accepted=["c"])
    assert service._calculate_seller_summary_metrics([seller], date.today())["counter_offers_accepted"] == 1
    category = session(user_type=UserType.seller, product_items=[{"category": "A"}],
                      extracted_entities={"category": "B"}, rfqs_with_response={"count": 2},
                      bfs_price_accepted={"count": 2}, counter_offers_accepted=[1], counter_offers_made={"count": 3})
    metrics = service._calculate_category_summary_metrics([category], date.today())
    assert metrics["A"]["counter_offers_accepted_by_sellers"] == 1
    assert set(service._extract_categories_from_session(session(product_items=None, extracted_entities=[{"description": "D"}, "bad"]))) == {"D"}
    rolling = service._aggregate_seller_rolling_metrics([{"seller_chats_initiated": 2, "rfqs_requested": 1}, {}])
    assert rolling["seller_chats_initiated"] == 2
    assert service._aggregate_category_rolling_metrics([{"A": {"rfqs_uploaded": 1}}, {"A": {"rfqs_uploaded": 2}}])["A"]["rfqs_uploaded"] == 3


@pytest.mark.asyncio
async def test_daily_summary_create_list_entities_and_exception(monkeypatch):
    service = summary_mod.DailySummaryService.__new__(summary_mod.DailySummaryService)
    service.settings = SimpleNamespace(enable_daily_summarization=True)
    first_query = Query(all_values=[session(parent_session_id="P", seller_responses={"x": 1},
                                           extracted_entities=[{"description": "D", "category": "C"}])], first=None)
    db = DB(first_query)
    monkeypatch.setattr(summary_mod, "get_db_session", lambda: DbContext(db))
    result = await service.generate_daily_summary("U1", date(2024, 1, 1))
    assert result is not None and db.added
    first_query.all_value = []
    assert await service.generate_daily_summary("U1", date(2024, 1, 1)) is None
    first_query.all_value = [session()]
    first_query.first_value = None
    first_query.all = MagicMock(return_value=[session()])
    assert await service.generate_daily_summary("U1", date(2024, 1, 1)) is not None
    failing_query = MagicMock()
    failing_query.filter.return_value = failing_query
    failing_query.first.side_effect = RuntimeError("db")
    db.query_value = failing_query
    assert await service.get_user_daily_summary("U1", date(2024, 1, 1)) is None


# Excel report, processing, and validation ---------------------------------


def test_report_remaining_sheets_metrics_and_format_errors(monkeypatch):
    service = report_mod.EnhancedExcelReportService.__new__(report_mod.EnhancedExcelReportService)
    db = SimpleNamespace(bind="bind")
    writer = SimpleNamespace(book=SimpleNamespace(sheetnames=[]))
    monkeypatch.setattr(report_mod, "get_db_session_context", lambda: DbContext(db))
    monkeypatch.setattr(report_mod.pd.DataFrame, "to_excel", MagicMock())
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([
        ["Seller Chats Initiated", 2], ["Total RFQs Requested", 3], ["Unique Sellers", 1],
        ["Subscription Plans Requested", 1], ["Zero Credit RFQ Attempt", 1],
        ["Unregistered Sellers Requested for RFQ", 2]], columns=["metric_name", "total_value"])))
    service._generate_seller_summary_sheet(writer, date(2024, 1, 1))
    assert service._calculate_seller_metrics(db, date(2024, 1, 1), date(2024, 1, 1))["rfqs_requested"] == 3
    service._generate_category_summary_sheet(writer, date(2024, 1, 1))
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(side_effect=RuntimeError("query")))
    with pytest.raises(UnboundLocalError):
        service._generate_category_summary_sheet(writer, date(2024, 1, 1))
    service._generate_aggregate_sheet_90d(writer, date(2024, 1, 1))
    assert service._calculate_buyer_metrics(db, date(2024, 1, 1), date(2024, 1, 1)) is None
    assert service._get_rolling_window_metrics(db, date(2024, 1, 1), 2) is None

    bad_writer = SimpleNamespace(book=SimpleNamespace(sheetnames=None))
    service._format_excel_sheets(bad_writer)
    service._generate_buyer_details_fallback = MagicMock()


def test_report_generate_email_and_fallback_detail_queries(monkeypatch, tmp_path):
    service = report_mod.EnhancedExcelReportService.__new__(report_mod.EnhancedExcelReportService)
    writer = MagicMock()
    writer.__enter__.return_value = writer
    writer.__exit__.return_value = False
    monkeypatch.setattr(report_mod.pd, "ExcelWriter", MagicMock(return_value=writer))
    for name in ("_generate_buyer_details_sheet", "_generate_seller_details_sheet", "_generate_category_details_sheet",
                 "_generate_buyer_summary_sheet", "_generate_seller_summary_sheet", "_generate_category_summary_sheet",
                 "_generate_aggregate_sheet_90d", "_format_excel_sheets"):
        setattr(service, name, MagicMock())
    def consume(awaitable):
        awaitable.close()

    monkeypatch.setattr(report_mod.asyncio, "run", consume)
    assert service.generate_report(date(2024, 1, 1), "report.xlsx", send_email=True) == "report.xlsx"
    path = tmp_path / "r.xlsx"
    path.write_bytes(b"excel")
    fake_email = SimpleNamespace(send_email_by_template=AsyncMock(return_value={"status": "Failed"}))
    monkeypatch.setattr(report_mod, "EmailService", lambda: fake_email)
    monkeypatch.setattr(report_mod, "init_procucev_api_client", AsyncMock(), raising=False)
    monkeypatch.setattr(report_mod, "close_procucev_api_client", AsyncMock(), raising=False)
    awaitable = service._send_report_email(str(path), date(2024, 1, 1))
    asyncio.run(awaitable)


def processing_service():
    service = processing_mod.ExcelProcessingService.__new__(processing_mod.ExcelProcessingService)
    service.target_columns = ["S.No", "ItemDescription", "Specification", "Uom", "Quantity", "Remarks"]
    service.openai_service = SimpleNamespace(process_excel_to_rfqs=AsyncMock())
    return service


@pytest.mark.asyncio
async def test_excel_processing_remaining_error_and_validation_paths(monkeypatch):
    service = processing_service()
    assert service._extract_items_with_mapping(pd.DataFrame([["x", None]], columns=["A", "B"]), ["A", "B"], {})["items"] == []
    assert service._validate_business_rules([
        {"ItemDescription": "x", "Quantity": "five", "Uom": "each"},
        {"ItemDescription": "x", "Quantity": 20001, "Uom": "item"},
    ])["valid"] is False
    assert service._validate_data_types([{"ItemDescription": "x" * 201, "Quantity": -1, "Uom": "u" * 21}])["valid"] is False
    monkeypatch.setattr(processing_mod.pd, "ExcelFile", MagicMock(return_value=SimpleNamespace(close=MagicMock())))
    assert service._is_valid_excel_file(b"\xd0\xcf\x11\xe0" + b"x" * 10, "x.xls")
    monkeypatch.setattr(processing_mod.pd, "ExcelFile", MagicMock(side_effect=RuntimeError("bad")))
    assert not service._is_valid_excel_file(b"PK\x03\x04" + b"x" * 10, "x.xlsx")
    monkeypatch.setattr(processing_mod, "load_workbook", MagicMock(side_effect=RuntimeError("not available")), raising=False)
    monkeypatch.setattr(processing_mod.pd, "read_excel", MagicMock(side_effect=RuntimeError("bad")))
    assert (await service._validate_excel_structure(b"bytes"))["valid"] is False
    assert service._validate_rfqs_consistency([{"city": "A", "state": "S", "pincode": "1"},
                                               {"city": "B", "state": "T", "pincode": "2"},
                                               {"city": "C", "state": "U", "pincode": "3"}])["valid"] is False
    service.openai_service.process_excel_to_rfqs.side_effect = RuntimeError("ai")
    assert "OpenAI" in (await service._process_excel_with_openai(pd.DataFrame({"A": [1]}), "x"))["error"]
    service._is_valid_excel_file = MagicMock(side_effect=RuntimeError("validator"))
    assert (await service.process_excel_file(b"bytes", "x.xlsx"))["success"] is False


@pytest.mark.asyncio
async def test_excel_validation_remaining_quality_and_retry_branches(monkeypatch):
    service = validation_mod.ExcelValidationService()
    assert service._validate_excel_content(b"plain text") ["error_type"] == "invalid_format"
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(side_effect=RuntimeError("bad")))
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(side_effect=validation_mod.InvalidFileException("bad")))
    monkeypatch.setattr(validation_mod.xlrd, "open_workbook", MagicMock(side_effect=RuntimeError("bad")))
    result = await service._validate_excel_readability(b"x", "x.xlsx")
    assert result["error_type"] in {"unreadable_file", "readability_error"}
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame()))
    assert (await service._validate_data_quality(b"x"))["error_type"] == "no_data_found"
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"Quantity": ["@bad"], "Name": ["x"]})))
    result = await service._validate_data_quality(b"x")
    assert result["valid"] is False

    class Response:
        status = 500
        async def read(self):
            return b"x"

    class HttpSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def get(self, _url): return AsyncContext(Response())

    monkeypatch.setattr(validation_mod.aiohttp, "ClientSession", lambda **_: HttpSession())
    assert await service._download_file_with_retry("url", max_retries=2) is None
    service._download_file_with_retry = AsyncMock(return_value=b"data")
    service._validate_file_integrity = MagicMock(return_value={"valid": True})
    service._validate_excel_content = MagicMock(return_value={"valid": False, "error_type": "bad"})
    result = await service.validate_excel_file_from_url("url", "x.xlsx")
    assert result["error_type"] == "bad"


# Chroma categorization and matching ---------------------------------------


def test_auto_categorization_population_sources_logging_and_health(monkeypatch):
    service = auto_mod.AutoCategorizationService.__new__(auto_mod.AutoCategorizationService)
    service.embedding_function = "embedding"
    service.collection = MagicMock()
    service.collection.name = "category_items"
    service.chroma_client = MagicMock()
    service.openai_service = MagicMock()
    service.learning_service = MagicMock()
    service.collection.count.return_value = 0
    service.collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    monkeypatch.setattr(auto_mod, "get_settings", lambda: SimpleNamespace(
        enable_remote_categorization=False, chroma_host="localhost", chroma_port=8000))
    service._get_local_category_data = MagicMock(return_value=[{"id": "1", "item": "bolt", "category": "Tools"}, {"id": "2"}])
    assert service.populate_embeddings_from_db() == 2
    service._get_local_category_data.return_value = []
    assert service.populate_embeddings_from_db() == 0
    monkeypatch.setattr(auto_mod, "get_settings", lambda: SimpleNamespace(
        enable_remote_categorization=True, chroma_host="localhost", chroma_port=8000))
    service._get_remote_category_data = MagicMock(side_effect=RuntimeError("remote"))
    service._get_local_category_data = MagicMock(return_value=[])
    with pytest.raises(RuntimeError):
        service.populate_embeddings_from_db()
    db = DB()
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    service._log_categorization("x", "u", None, None, "Tools", .8, .7, "test", 1)
    service._log_categorization("x", "u", None, None, "Tools", .8, .7, "test", 1)
    db.commit.side_effect = RuntimeError("commit")
    service._log_categorization("x", "u", None, None, "Tools", .8, .7, "test", 1)
    service.collection.count.return_value = 3
    service.openai_service.client = object()
    assert service.get_collection_stats()["total_items"] == 3
    service.collection.count.side_effect = RuntimeError("chroma")
    assert "error" in service.get_collection_stats()
    service.collection.count.side_effect = None
    service.collection.count.return_value = 1
    service.learning_service = MagicMock()
    service.learning_service.create_3_level_category = AsyncMock(return_value={"success": False})
    assert service.health_check()["status"] in {"healthy", "unhealthy"}


@pytest.mark.asyncio
async def test_enhanced_categorization_all_fallback_and_logging_paths(monkeypatch):
    service = enhanced_cat_mod.EnhancedAutoCategorizationService.__new__(enhanced_cat_mod.EnhancedAutoCategorizationService)
    service.collection = MagicMock(); service.category_collection = MagicMock(); service.fallback_service = MagicMock(); service.openai_service = MagicMock()
    service.chroma_path = "mock"
    monkeypatch.setattr(enhanced_cat_mod, "execute_remote_query", MagicMock(return_value=[]), raising=False)
    assert service._keyword_lookup_source_of_truth("x") ["success"] is False
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service.fallback_service.collection.query.return_value = {"metadatas": [[{"category": "Other"}]], "distances": [[1.0]]}
    result = service._cross_validate_with_fallback("x", "Tools", .9)
    assert result["validated"] is False
    service.fallback_service.collection.query.return_value = {"metadatas": [[]], "distances": [[]]}
    assert service._cross_validate_with_fallback("x", "Tools", .5)["use_learning"]
    service.collection.query.return_value = {"documents": [["x"]], "metadatas": [[{"client_category_name": "Tools", "level_1_category": "A", "level_2_category": "B", "level_3_category": "C", "category_path": "A/B/C", "confidence_score": .8, "item_description": "x"}]], "distances": [[1.2]]}
    assert service.get_category_suggestions("x") == []
    service.collection.query.side_effect = RuntimeError("query")
    assert service.get_category_suggestions("x") == []
    service.collection.query.side_effect = None
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._search_hierarchical_levels = MagicMock(return_value={"success": False})
    service.fallback_service._get_similar_items.return_value = []
    service._log_fallback_categorization = MagicMock()
    assert (await service.categorize_item("unknown", "u"))["method"] == "enhanced_no_match"
    service.fallback_service._get_similar_items.return_value = [{"category": "Tools", "similarity_score": .8}]
    service.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": False})
    assert (await service.categorize_item("x", "u"))["method"] == "enhanced_no_match"
    service.collection.count.return_value = 1
    service.fallback_service.get_collection_stats.return_value = {"error": "bad"}
    assert service.health_check()["overall_status"] == "unhealthy"
    service.collection.count.side_effect = RuntimeError("chroma")
    service.fallback_service.get_collection_stats.side_effect = RuntimeError("fallback")
    assert service.health_check()["overall_status"] == "unhealthy"
    assert "error" in service.get_stats()


@pytest.mark.asyncio
async def test_enhanced_matching_distance_filters_and_stats(monkeypatch):
    collection = MagicMock()
    service = matching_mod.EnhancedSellerMatchingService.__new__(matching_mod.EnhancedSellerMatchingService)
    service.collection = collection; service.chroma_path = "mock"; service.openai_service = MagicMock()
    metadata = {"seller_id": "s", "seller_name": "S", "phone_number": "1", "email": "", "original_category": "A",
                "level_1_category": "A", "level_2_category": "B", "level_3_category": "C", "category_path": "A/B/C",
                "confidence_score": .8, "ranking": "Gold", "location": "bad-json"}
    collection.query.return_value = {"documents": [["x", "y"]], "metadatas": [[metadata, metadata]], "distances": [[.1, .2]]}
    service.openai_service.select_best_sellers = AsyncMock(return_value={"success": False})
    result = await service.find_sellers_for_item("x", similarity_threshold=.95, ranking_priority=False)
    assert result["success"] and not result["sellers"]
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service.find_sellers_by_category_path("A")["success"] is False
    collection.count.return_value = 2
    collection.query.return_value = {"documents": [["x"]]}
    assert service.health_check()["overall_status"] == "healthy"
    service.collection.query.side_effect = RuntimeError("down")
    assert service.health_check()["overall_status"].startswith("unhealthy")
    db = DB(Query(count=4)); monkeypatch.setattr(matching_mod, "get_db_session", lambda: db)
    service.collection.query.side_effect = None; service.collection.count.return_value = 2
    assert service.get_stats()["database_sellers"]["total_sellers"] == 4


# RFQ, seller notification/recommendation/categorization -------------------

@pytest.mark.asyncio
async def test_rfq_background_remaining_fetch_cleanup_priority_and_notifications(monkeypatch):
    # Isolate the singleton from constructor-based RFQ background tests.
    monkeypatch.setattr(background_mod.RFQBackgroundService, "_instance", None)
    monkeypatch.setattr(background_mod.RFQBackgroundService, "_initialized", False)
    service = background_mod.RFQBackgroundService.__new__(background_mod.RFQBackgroundService)
    service.db_session = DB(Query(all_values=[])); service.max_concurrent_notifications = 2
    async def send_notification(*, seller_id, rfq_data):
        if seller_id == "s2":
            raise RuntimeError("down")
        return {"success": True, "message_id": "m"}

    service.intimation_service = SimpleNamespace(send_rfq_notification=send_notification)
    result = await service._send_batch_notifications({"rfq_id": "R"}, [{"seller_id": "s1", "seller_name": "S1"}, {"seller_id": "s2", "seller_name": "S2"}], "job")
    assert result["successful"] == 1 and result["failed"] == 1
    service.db_session.query_value = Query(count=0)
    assert (await service.cleanup_old_notifications())["notifications_deleted"] == 0
    service.db_session.query_value = Query(count=2, delete=2)
    cleaned = await service.cleanup_old_notifications()
    assert cleaned["success"]
    rfq = SimpleNamespace(rfq_id="R", api_payload={"deadline": (date.today() + timedelta(days=3)).isoformat(), "categories": ["Medical Equipment"]}, created_at=datetime.utcnow() - timedelta(days=24), status=RFQStatus.ready)
    assert service._calculate_rfq_priority(rfq) >= 180
    service.db_session.query_value = Query(first=rfq)
    await service._update_rfq_status("R", RFQStatus.submitted)
    assert rfq.status == RFQStatus.submitted
    service.db_session.query_value = Query(first=None)
    assert await service._fetch_rfq_data("missing") is None
    service.db_session.query_value = Query(all_values=[])
    assert await service.get_pending_rfqs() == []


@pytest.mark.asyncio
async def test_rfq_intimation_credit_email_timeout_and_helpers(monkeypatch):
    db = DB(Query(first=None)); wa = SimpleNamespace(send_message=AsyncMock(return_value=MessageResponse(success=True, message_id="m")))
    service = intimation_mod.RFQIntimationService(db)
    service.whatsapp_service = wa
    service._get_seller_details = AsyncMock(return_value=SimpleNamespace(seller_name="S", phone_number="1", email="s@x", subscription_credits=1))
    service.opt_out_service.check_seller_notification_eligibility = AsyncMock(return_value={"eligible": False, "action": "send_permission_request"})
    service.opt_out_service.send_permission_request = AsyncMock(return_value={"sent": True})
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["action"] == "permission_request_sent"
    service._get_seller_details.return_value.subscription_credits = 0
    service.opt_out_service.check_seller_notification_eligibility.return_value = {"eligible": True}
    service._generate_rfq_brief = AsyncMock(return_value="brief"); service._create_uncredited_seller_message = AsyncMock(return_value="msg")
    service._record_notification = AsyncMock(); service._record_interaction = AsyncMock(); service._start_conversation_timeout = AsyncMock()
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["next_step"] == "subscription_selection"
    service._get_seller_details.return_value.subscription_credits = 1
    service._get_mock_procurev = MagicMock(return_value=SimpleNamespace(generate_payment_link=AsyncMock(return_value={"success": False, "error": "no"})))
    assert not (await service.handle_subscription_selection("s", "R", "basic"))["success"]
    service._validate_rfq_id = AsyncMock(return_value=False)
    assert (await service.handle_rfq_id_request("s", "R", "bad"))["error"] == "Invalid RFQ ID"
    service._validate_rfq_id.return_value = True
    service._get_mock_procurev.return_value.send_rfq_email = AsyncMock(return_value={"success": False, "error": "mail"})
    assert "Email sending failed" in (await service.handle_rfq_id_request("s", "R", "R"))["error"]
    service._get_mock_procurev.return_value.get_seller_pending_bids = AsyncMock(return_value={"success": True, "pending_bids": []})
    service._record_interaction = AsyncMock(); service._update_notification_response = AsyncMock()
    assert (await service.handle_conversation_timeout("s", "R"))["success"]
    service._get_mock_procurev.return_value.get_seller_pending_bids.return_value = {"success": False}
    assert (await service.handle_conversation_timeout("s", "R"))["success"]
    service.db_session.query_value = Query(first=SimpleNamespace(subscription_credits=1))
    await service._deduct_seller_credit("s")
    assert service.db_session.commit.called


def recommendation_service(monkeypatch):
    recommendation_mod.SellerRecommendationService._instance = None
    recommendation_mod.SellerRecommendationService._initialized = False
    db = DB(Query(first=None, count=0))
    monkeypatch.setattr(recommendation_mod, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(recommendation_mod, "LocationService", lambda: MagicMock())
    return recommendation_mod.SellerRecommendationService(db), db


@pytest.mark.asyncio
async def test_recommendation_filters_config_details_and_error_paths(monkeypatch):
    service, db = recommendation_service(monkeypatch)
    sellers = [SimpleNamespace(seller_id="1", seller_name="A", categories=["Tools"], location={"pincode": "1"},
                               opted_out_notifications=False, subscription_credits=1, ranking=None, last_active_at=None,
                               phone_number="1", email="a")]
    monkeypatch.setattr(recommendation_mod, "SellerDataAdapter", lambda: SimpleNamespace(get_sellers_from_remote=lambda: sellers))
    assert await service._filter_sellers_by_category(["tools"], ["1"]) == sellers
    assert await service._filter_sellers_by_category([]) == []
    monkeypatch.setattr(recommendation_mod.pincode_distance, "calculate_distance_between_pincodes", lambda *_: None)
    assert await service._filter_sellers_by_location(sellers, {"pincode": "1"}) == sellers
    db.query_value = Query(first=None)
    assert await service._filter_by_message_history(sellers, 1) == sellers
    assert await service._apply_cyclic_selection(sellers, ["Tools"]) == sellers
    config = await service._load_system_config()
    assert config["MAX_SUBSCRIBED_SELLERS_PER_RFQ"] == 10
    service.db_session.query_value = MagicMock()
    service.db_session.query_value.filter.return_value = service.db_session.query_value
    service.db_session.query_value.first.side_effect = [sellers[0], None]
    service.db_session.query_value.scalar.return_value = 3
    details = await service.get_seller_details("1")
    assert details["notification_stats"]["recent_notifications_30d"] == 3
    service.db_session.query_value = Query(first=None)
    assert await service.get_seller_details("missing") is None
    service.db_session.query_value = Query(first=None)
    result = await service.select_sellers_for_rfq({"rfq_id": "R", "categories": ["Tools"]})
    assert result["total_selected"] == 1


@pytest.mark.asyncio
async def test_seller_notification_workflow_template_and_bfs_paths(monkeypatch):
    wa = MagicMock(); monkeypatch.setattr(notification_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(notification_mod, "get_settings", lambda: SimpleNamespace(
        WHATSAPP_TEMPLATE_RFQ_NOTIFICATION="rfq", WHATSAPP_TEMPLATE_BFS_BID_NOTIFICATION="bfs", PROCUCEV_PORTAL_URL="url"))
    service = notification_mod.SellerNotificationService()
    db = DB(Query(first=None)); monkeypatch.setattr(notification_mod, "get_db_session", lambda: db)
    assert service.check_seller_workflow_status("+1") == (False, None, None)
    db.query_value = Query(first=("seller", None, 2))
    db.execute = MagicMock(return_value=SimpleNamespace(fetchone=MagicMock(return_value=("seller", None, 2))))
    assert service.check_seller_workflow_status("+1")[0]
    db.execute.return_value.fetchone.return_value = ("seller", None, 20)
    assert service.format_bfs_bid_message({"item_description": "x", "buy_price": 0, "ask_price": 2})
    wa.send_template_message = AsyncMock(return_value=MessageResponse(success=True, message_id="t"))
    assert (await service.send_bfs_bid_notification("1", {"item_description": "x"}, "b", "s", use_template_message=True))["success"]
    service.check_seller_workflow_status = MagicMock(return_value=(True, "seller", 1))
    assert (await service.send_bfs_bid_notification("1", {}, "b", "s"))["skipped"]
    wa.send_configurable_buttons = AsyncMock(side_effect=RuntimeError("wa"))
    service.check_seller_workflow_status = MagicMock(return_value=(False, None, None))
    result = await service.send_rfq_notifications({"rfq_id": "R"}, [{"seller_id": "s", "phone_number": "1"}])
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_seller_categorization_batch_job_and_statistics(monkeypatch):
    db = DB(Query(all_values=[], first=None, scalar=2, count=2))
    service = seller_cat_mod.SellerCategorizationService(db)
    seller = SimpleNamespace(seller_id="s", seller_name="S", location={}, ranking=None, categories=["Tools"])
    service._get_sellers_needing_categorization = AsyncMock(return_value=[seller])
    service._process_seller_batch = AsyncMock(return_value={"processed": 1, "errors": 0})
    monkeypatch.setattr(seller_cat_mod.asyncio, "sleep", AsyncMock())
    result = await service.process_all_sellers()
    assert result["sellers_processed"] == 1
    service._get_similar_category_items = AsyncMock(return_value=[])
    service.openai_service.generate_3_level_categorization = MagicMock(return_value={"success": False, "error": "ai"})
    assert not (await service._generate_3_level_mapping_for_category("Tools", seller))["success"]
    service._get_sellers_needing_categorization = AsyncMock(return_value=[])
    assert (await service.process_all_sellers())["sellers_processed"] == 0
    service.db_session.query_value = Query(scalar=0, count=0)
    stats = await service.get_categorization_statistics()
    assert stats["database_statistics"]["coverage_percentage"] == 0


# Seller service and webhook monitor ---------------------------------------


def seller_service(monkeypatch):
    db = MagicMock(); manager = MagicMock(); wa = MagicMock(); api = MagicMock()
    monkeypatch.setattr(seller_mod, "DatabaseManager", lambda **_: db)
    monkeypatch.setattr(seller_mod, "SellerAPIService", lambda: api)
    monkeypatch.setattr(seller_mod, "SessionManagementService", lambda *a, **k: manager)
    monkeypatch.setattr(seller_mod, "ChatSummaryService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_mod, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "RFQStatusService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_mod, "ResponseHelpers", lambda _: MagicMock())
    monkeypatch.setattr(seller_mod, "get_settings", lambda: SimpleNamespace(
        support_contact_info="support", procucev_rfq_details_url="url", rfq_max_allowed=3))
    return seller_mod.SellerService(wa, manager, db), api, manager


@pytest.mark.asyncio
async def test_seller_service_remaining_intent_plan_and_selection_paths(monkeypatch):
    service, api, manager = seller_service(monkeypatch)
    user = SimpleNamespace(id="u", org_id="o", phone_number="1", email="e")
    sess = session(workflow_state={"seller_workflow_state": "awaiting_general_response"}, conversation_history={"messages": []})
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 1})
    service._classify_seller_intent = AsyncMock(return_value={"intent": "general_question", "confidence": .9})
    service._generate_general_seller_response = AsyncMock(return_value={"status": "general"})
    assert (await service._handle_general_seller_response(user, sess, "help"))["status"] == "general"
    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("subscribe", {})["intent"] == "plan_upgrade_request"
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="plans")
    api.get_subscription_plans = AsyncMock(return_value={"success": True, "plans": [{"id": "p", "planName": "Basic"}]})
    manager.save_session = AsyncMock()
    assert (await service._handle_plan_upgrade_request(user, sess, "plans"))["workflow_step"] == "show_subscription_plans"
    service._extract_plan_selection = AsyncMock(return_value=None)
    assert (await service._handle_plan_selection_response(user, sess, "bad"))["success"] is False
    service._extract_plan_selection = AsyncMock(return_value={"id": "p"})
    api.generate_payment_link = AsyncMock(return_value={"success": False})
    assert (await service._handle_plan_selection_response(user, sess, "basic"))["workflow_step"] == "payment_link_error"
    service._extract_plan_selection = seller_mod.SellerService._extract_plan_selection.__get__(service)
    service.openai_service.extract_entities = AsyncMock(return_value={})
    assert await service._extract_plan_selection("select plan", [{"id": "p", "planName": "Basic"}]) == {"id": "p", "planName": "Basic"}
    sess.conversation_history = {"messages": [{"role": "assistant", "content": "1. RFQ123456789012"}]}
    service.openai_service.extract_rfq_ids_from_message = AsyncMock(return_value={"success": False})
    assert await service._extract_rfq_ids_from_message("1", sess, []) == ["RFQ123456789012"]
    assert (await service._handle_invalid_rfq_selection(user, sess, "x", {"success": True, "rfqs": [], "total_count": 0}))["success"] is False


@pytest.mark.asyncio
async def test_webhook_health_monitor_lifecycle_and_state_edges(monkeypatch):
    service = health_service(["a@x"])
    service._try_acquire_leader_lock = AsyncMock(side_effect=[False, True])
    service._interruptible_sleep = AsyncMock()
    service._run_as_leader = AsyncMock(side_effect=lambda: setattr(service, "_running", False))
    await service.start_monitoring()
    assert service._run_as_leader.called
    service._run_as_leader = health_mod.WebhookHealthMonitorService._run_as_leader.__get__(service)
    service._running = True
    service._renew_leader_lock = AsyncMock(return_value=False)
    await service._run_as_leader()
    assert service.is_leader is False
    service._running = False
    await service._interruptible_sleep(0)
    state = {"current_state": "RECOVERED", "consecutive_successes": 1, "consecutive_failures": 0,
             "consecutive_warnings": 0, "failure_start_time": None, "is_alerting": True,
             "last_latency_ms": 1, "last_error": None, "last_severity": "OK"}
    service._send_recovery_notification = AsyncMock()
    await service._handle_ok_status(state, health_mod.MonitorState.RECOVERED)
    assert state["current_state"] == "HEALTHY"
    service.email_service.send_email_by_template = AsyncMock(return_value={"status": "Failed"})
    state.update(failure_start_time=datetime.utcnow().isoformat(), consecutive_failures=1)
    await service._send_critical_alert(state)
    service.email_service.send_email_by_template.side_effect = RuntimeError("mail")
    await service._send_warning_alert(state)
    await service._send_recovery_notification(state)
    service.redis.init_client = AsyncMock(); service.redis.get = AsyncMock(return_value="not-json"); service.redis.set = AsyncMock()
    await service._add_to_history(health_mod.HealthStatus.OK, 1, None)
    service._session = None
    monkeypatch.setattr(health_mod.aiohttp, "ClientSession", lambda **_: SimpleNamespace(closed=False))
    assert await service._get_session()
