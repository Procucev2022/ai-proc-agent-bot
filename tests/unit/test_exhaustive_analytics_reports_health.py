from __future__ import annotations

import asyncio
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.conversation_analytics_service as analytics_module
import app.services.daily_aggregation_service as aggregation_module
import app.services.daily_summary_service as summary_module
import app.services.enhanced_excel_report_service as report_module
import app.services.excel_processing_service as processing_module
import app.services.excel_validation_service as validation_module
import app.services.webhook_health_monitor_service as health_module
from app.models import SessionState, UserType


class DbContext:
    def __init__(self, value, error=None):
        self.value = value
        self.error = error

    def __enter__(self):
        if self.error:
            raise self.error
        return self.value

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


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {"status": "ok"}
        self.closed = False

    async def read(self):
        return b"PK\x03\x04fake"


class FakeWriter:
    def __init__(self):
        self.book = SimpleNamespace(sheetnames=[])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def query_db(rows=None, first=None):
    query = MagicMock()
    query.filter.return_value = query
    query.with_entities.return_value = query
    query.all.return_value = [] if rows is None else rows
    query.first.return_value = first
    db = MagicMock()
    db.query.return_value = query
    return db, query


def analytics_service():
    service = analytics_module.ConversationAnalyticsService.__new__(analytics_module.ConversationAnalyticsService)
    service.batch_size = 2
    service.settings = SimpleNamespace(procucev_db_name="proc", whatsapp_db="whatsapp")
    service.openai_service = SimpleNamespace(
        tools_dir=Path("."),
        default_model="model",
        _load_prompt=MagicMock(return_value="system"),
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())),
    )
    return service


def base_session(**overrides):
    values = dict(
        session_id="S1",
        external_user_id="U1",
        created_at=datetime(2024, 1, 1),
        conversation_history={"messages": [{"role": "user", "content": "hello"}]},
        user_type=UserType.buyer,
        rfq_ids=["R1", None],
        rfq_id=None,
        product_items=[{"category": "Tools"}],
        products_bid_for=["p"],
        bids_accepted={"count": 1},
        rfqs_with_response=["R1"],
        bfs_search_count=1,
        bfs_price_accepted=["p"],
        counter_offers_accepted={"count": 1},
        counter_offers_made=["offer"],
        seller_responses=["response"],
        extracted_entities={"category": "Tools", "description": "Drill"},
        parent_session_id=None,
        session_state=SessionState.completed,
        completed_at=datetime(2024, 1, 1, 1),
        last_activity_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# Conversation analytics ----------------------------------------------------


def test_analytics_dataframe_helpers_cover_all_roles_and_empty_values():
    service = analytics_service()
    sessions = [
        {
            "session_id": "B", "phone_number": "+911", "user_type": "buyer",
            "confidence_score": 90, "analysis_reasoning": "buyer",
            "buyer_identities": [{"buyer_email": "b@example.com", "buyer_metrics": {
                "successful_rfqs_ai": 2, "incomplete_rfqs": 1,
                "bfs_search_details": [{"search_keyword": "bolt", "results_found": ["A", "", "B"]}],
            }}],
            "registration_metrics": {"buyer_successful_registration": 1},
        },
        {
            "session_id": "S", "phone_number": "922", "user_type": "seller",
            "seller_identities": [{"seller_email": "s@example.com", "seller_metrics": {
                "rfq_requested_ai": 3, "rfq_response_ai": 2,
            }}],
        },
        {"session_id": "U", "phone_number": "933", "user_type": "unknown",
         "unknown_user_metrics": {"number_of_faq_or_general_queries": 4}},
    ]
    buyer, seller, unknown = service._create_dataframes_from_sessions(sessions, "2024-01-01")
    assert list(buyer["buyer_email"]) == ["b@example.com"]
    assert buyer.iloc[0]["successful_rfqs_ai"] == 2
    assert list(seller["seller_email"]) == ["s@example.com"]
    assert unknown.iloc[0]["number_of_faq_or_general_queries"] == 4
    bfs = service._create_bfs_search_dataframe(sessions, "2024-01-01")
    assert bfs.iloc[0]["searched_result"] == "A, B"
    assert service._create_bfs_search_dataframe([{"buyer_identities": []}], "x").empty
    events = service._create_seller_rfq_interest_event_df([{
        "session_id": "S", "phone_number": "9", "seller_rfq_interest_event": [{"rfq_id": "R", "seller_id": "V"}]
    }], "2024-01-01")
    assert events.iloc[0]["response_date"] == "2024-01-01"
    assert service._create_seller_rfq_interest_event_df([], "x").empty
    assert service._extract_chat_sequence({"messages": [
        {"role": "user", "content": {"body": {"text": "hello"}}, "timestamp": "t"},
        {"role": "assistant", "content": {"button_reply": {"title": "Yes"}}},
        {"role": "assistant", "content": {"type": "button_reply", "button_reply": {"title": "Again"}}},
        {"role": "system", "content": "ignored"},
    ]}) == "[t] User: hello\nAssistant: Yes\nAssistant: Again"
    assert service._extract_chat_sequence([]) == ""
    assert service._safe_json_value(float("nan")) is None
    assert service._safe_json_value("nan") is None
    assert service._safe_json_value([]) is None
    assert service._safe_json_value("value") == "value"


@pytest.mark.asyncio
async def test_analytics_batch_prepare_ai_success_none_and_rate_limit(monkeypatch):
    service = analytics_service()
    sessions = [base_session(session_id="S1", conversation_history={"messages": [
        {"role": "user", "content": "x" * 600, "timestamp": "t"}, {"role": "system", "content": "skip"}
    ]}), base_session(session_id="S2", conversation_history=[{"role": "assistant", "content": "ok"}])]
    prepared = await service._prepare_batch_session_data(sessions)
    assert prepared[0]["conversation_history"][0]["content"] == "x" * 500
    assert prepared[1]["conversation_history"][0]["role"] == "assistant"
    assert await service._prepare_batch_session_data([base_session(conversation_history=None)])
    fake_file = MagicMock()
    fake_file.__enter__.return_value = fake_file
    fake_file.__exit__.return_value = False
    fake_file.read.return_value = json.dumps({"name": "tool"})
    monkeypatch.setattr("builtins.open", MagicMock(return_value=fake_file))
    service.openai_service.client.responses.create.return_value = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", arguments=json.dumps({"sessions": [{"session_id": "S1"}]}))]
    )
    result = await service._analyze_batch_with_ai(prepared, 1, date(2024, 1, 1))
    assert result["sessions"][0]["session_id"] == "S1"
    service.openai_service.client.responses.create.return_value = SimpleNamespace(output=[])
    assert await service._analyze_batch_with_ai(prepared, 1, date(2024, 1, 1)) is None
    service.openai_service.client.responses.create.side_effect = RuntimeError("ordinary failure")
    assert await service._analyze_batch_with_ai(prepared, 1, date(2024, 1, 1)) is None
    fallback = AsyncMock(return_value={"sessions": [{"session_id": "S1"}]})
    service._process_sessions_individually = fallback
    service.openai_service.client.responses.create.side_effect = RuntimeError("rate limit exceeded")
    assert (await service._analyze_batch_with_ai(prepared, 1, date(2024, 1, 1)))["sessions"]
    assert service._build_batch_prompt([{"session_id": "S1"}], 3, date(2024, 1, 1)).find("BATCH NUMBER: 3") >= 0


@pytest.mark.asyncio
async def test_analytics_batch_orchestration_and_date_range(monkeypatch):
    service = analytics_service()
    service._prepare_batch_session_data = AsyncMock(return_value=[{"session_id": "S1"}])
    service._analyze_batch_with_ai = AsyncMock(return_value={"sessions": [{"session_id": "S1"}]})
    monkeypatch.setattr(analytics_module.asyncio, "sleep", AsyncMock())
    result = await service._process_sessions_in_batches([base_session(), base_session(session_id="S2"), base_session(session_id="S3")], date(2024, 1, 1))
    assert result["total_sessions"] == 2
    service._analyze_batch_with_ai.side_effect = [None, RuntimeError("bad")]
    result = await service._process_sessions_in_batches([base_session(), base_session(session_id="S2")], date(2024, 1, 1))
    assert result["total_sessions"] == 0
    service._analyze_batch_with_ai = AsyncMock(side_effect=[{"sessions": [{"session_id": "S1"}]}, RuntimeError("bad")])
    individual = await service._process_sessions_individually([{"session_id": "S1"}, {"session_id": "S2"}], 1, date(2024, 1, 1))
    assert individual["total_sessions"] == 1
    service.analyze_daily_conversations = AsyncMock(side_effect=[{"success": True}, {"success": False}])
    assert (await service.analyze_date_range(date(2024, 1, 1), date(2024, 1, 2)))["processed_dates"] == 2
    assert await service.__aenter__() is service


@pytest.mark.asyncio
async def test_analytics_daily_empty_error_and_success_orchestration(monkeypatch):
    service = analytics_service()
    db, query = query_db([])
    monkeypatch.setattr(analytics_module, "get_db_session", lambda: DbContext(db))
    result = await service.analyze_daily_conversations(date(2024, 1, 1))
    assert result["success"] and result["total_sessions"] == 0 and result["buyer_df"].empty
    query.all.side_effect = RuntimeError("db down")
    result = await service.analyze_daily_conversations(date(2024, 1, 1))
    assert result["success"] is False and "db down" in result["error"]

    query.all.side_effect = None
    query.all.return_value = [base_session()]
    service._process_sessions_in_batches = AsyncMock(return_value={"date": "2024-01-01", "total_sessions": 3, "sessions": [
        {"session_id": "B", "phone_number": "1", "user_type": "buyer", "buyer_identities": [{"buyer_email": "b", "buyer_metrics": {}}]},
        {"session_id": "S", "phone_number": "2", "user_type": "seller", "seller_identities": [{"seller_email": "s", "seller_metrics": {}}]},
        {"session_id": "U", "phone_number": "3", "user_type": "unknown"},
    ]})
    service._dump_joined_buyer_df_to_db = MagicMock()
    service._dump_joined_seller_df_to_db = MagicMock()
    service._dump_unknown_df_to_db = MagicMock()
    service._dump_joined_seller_interest_to_fact_table = MagicMock()
    service.calculate_and_store_daily_aggregates = MagicMock()
    service.calculate_and_store_category_aggregates = MagicMock()
    service.calculate_and_store_seller_daily_aggregates = MagicMock()
    service.calculate_and_store_unknown_daily_aggregates = MagicMock()
    service._query_remote_users = MagicMock(return_value=pd.DataFrame())
    service._query_remote_seller_rfqs = MagicMock(return_value=pd.DataFrame())
    service._query_remote_counter_seller_bids = MagicMock(return_value=pd.DataFrame())
    service._query_remote_rfq_categories = MagicMock(return_value=pd.DataFrame())
    result = await service.analyze_daily_conversations(date(2024, 1, 1))
    assert result["success"] is True and result["sessions_df"].shape[0] == 1
    assert service._dump_joined_buyer_df_to_db.called and service._dump_joined_seller_df_to_db.called
    service.close = AsyncMock()
    await service.__aexit__(None, None, None)
    service.close.assert_awaited_once()


def remote_result(rows):
    return [SimpleNamespace(_mapping=row) for row in rows]


def test_analytics_remote_queries_success_and_failure(monkeypatch):
    service = analytics_service()
    remote = MagicMock()
    remote.execute.return_value = remote_result([{"username": "u", "phone": "+1"}])
    monkeypatch.setattr(analytics_module, "get_remote_db_session", lambda: remote)
    assert service._query_remote_users(date(2024, 1, 1)).iloc[0]["username"] == "u"
    assert service._query_remote_seller_rfqs(date(2024, 1, 1)).shape[0] == 1
    assert service._query_remote_counter_seller_bids(date(2024, 1, 1)).shape[0] == 1
    assert service._query_remote_rfq_categories(date(2024, 1, 1), ["R1"]).shape[0] == 1
    remote.execute.side_effect = RuntimeError("remote")
    assert service._query_remote_users(date(2024, 1, 1)) == []
    assert service._query_remote_seller_rfqs(date(2024, 1, 1)).empty
    assert service._query_remote_counter_seller_bids(date(2024, 1, 1)).empty
    assert service._query_remote_rfq_categories(date(2024, 1, 1)).empty


def test_analytics_dump_helpers_and_response_counters():
    service = analytics_service()
    q = MagicMock()
    q.filter.return_value = q
    q.first.return_value = None
    db = MagicMock()
    db.query.return_value = q
    buyer = pd.DataFrame([{"date": date(2024, 1, 1), "session_id": "B", "buyer_email": "b", "phone_number": "1", "total_rfqs_raised": 1}])
    service._dump_joined_buyer_df_to_db(buyer, db)
    seller = pd.DataFrame([{"date": date(2024, 1, 1), "session_id": "S", "seller_email": "s", "phone_number": "2"}])
    service._dump_joined_seller_df_to_db(seller, db)
    unknown = pd.DataFrame([{"date": date(2024, 1, 1), "session_id": "U", "phone_number": "3"}])
    service._dump_unknown_df_to_db(unknown, db)
    interest = pd.DataFrame([{"response_date": date(2024, 1, 1), "session_id": "S", "rfq_id": "R", "seller_id": "V", "rfq_notified_at": "bad", "seller_response_at": None}])
    service._dump_joined_seller_interest_to_fact_table(interest, db)
    bfs = pd.DataFrame([{"session_id": "B", "email": "b", "phone_number": "1", "searched_keywords": "bolt"}])
    service._dump_bfs_search_df_to_db(bfs, db, date(2024, 1, 1))
    assert db.add.called and db.commit.called
    q.first.return_value = SimpleNamespace()
    service._dump_joined_buyer_df_to_db(buyer, db)
    service._dump_joined_seller_df_to_db(seller, db)
    service._dump_unknown_df_to_db(unknown, db)
    service._dump_joined_seller_interest_to_fact_table(interest, db)
    service._dump_bfs_search_df_to_db(bfs, db, date(2024, 1, 1))
    assert service._calculate_rfqs_with_response(date(2024, 1, 1), db) == q.filter.return_value.distinct.return_value.count.return_value
    assert service._calculate_total_rfq_responses(date(2024, 1, 1), db) == q.filter.return_value.count.return_value
    failing = MagicMock()
    failing.query.side_effect = RuntimeError("query")
    assert service._calculate_rfqs_with_response(date(2024, 1, 1), failing) == 0
    assert service._calculate_total_rfq_responses(date(2024, 1, 1), failing) == 0


def test_analytics_aggregate_and_bfs_category_paths(monkeypatch):
    service = analytics_service()
    target = date(2024, 1, 1)
    buyer_metric = SimpleNamespace(email="b", phone_number="1", number_of_chats=2, total_rfq_raised=1,
        total_items_in_rfqs=2, total_distinct_categories_in_rfq=1, total_incomplete_rfq=0,
        failed_registration=0, buyers_started_but_not_raised_rfq=0, no_of_products_searched=1,
        bfs_searches=1, products_bid_for=1, bfs_stock_products_bid_placed_count=1)
    q = MagicMock(); q.filter.return_value = q; q.all.return_value = [buyer_metric]; q.first.return_value = None
    db = MagicMock(); db.query.return_value = q
    service._calculate_rfqs_with_response = MagicMock(return_value=1)
    service._calculate_total_rfq_responses = MagicMock(return_value=2)
    service.calculate_and_store_daily_aggregates(target, db)
    seller_metric = SimpleNamespace(email="s", phone_number="2", number_of_chats=1, total_rfqs_requested=2,
        subscription_plans_requested=1, seller_failed_registration=0, seller_successful_registration=1,
        zero_credit_rfq_attempt=0, rfq_response_ai=1)
    q.all.return_value = [seller_metric]
    service.calculate_and_store_seller_daily_aggregates(target, db)
    unknown_metric = SimpleNamespace(email="", phone_number="3", unregistered_seller_initiated_chat=1,
        unregistered_seller_requested_rfq=1, unregistered_buyer_bfs_only=1, number_of_faq_or_general_queries=1)
    q.all.return_value = [unknown_metric]
    service.calculate_and_store_unknown_daily_aggregates(target, db)
    q.all.return_value = []
    service.calculate_and_store_daily_aggregates(target, db)
    service.calculate_and_store_seller_daily_aggregates(target, db)
    service.calculate_and_store_unknown_daily_aggregates(target, db)
    db.commit.side_effect = RuntimeError("commit")
    service.calculate_and_store_daily_aggregates(target, db)
    remote = MagicMock(); remote.execute.return_value.fetchall.return_value = [(target, "Tools", 2, 1, 1, 1, 0, 0)]
    monkeypatch.setattr(analytics_module, "get_remote_db_session", lambda: remote)
    service._get_bfs_category_counts_combined = MagicMock(return_value=({"Tools": 2}, {"Other": 1}))
    db.commit.side_effect = None
    db.execute.return_value = [("Tools", 2)]
    q.first.return_value = None
    service.calculate_and_store_category_aggregates(target, db)
    remote.execute.return_value.fetchall.return_value = []
    service.calculate_and_store_category_aggregates(target, db)
    remote.execute.side_effect = RuntimeError("category")
    service.calculate_and_store_category_aggregates(target, db)
    monkeypatch.setattr(analytics_module.pd, "read_sql", MagicMock(side_effect=[
        pd.DataFrame({"bfs_products_searched_list": [["bolt"]]}),
        pd.DataFrame({"bfs_products_searched_by_unregistered": [["nut"]]}),
    ]))
    service._get_bfs_category_counts_combined = analytics_module.ConversationAnalyticsService._get_bfs_category_counts_combined.__get__(service)
    service._get_product_category_mapping = MagicMock(return_value={"bolt": "Tools", "nut": "Hardware"})
    assert service._get_bfs_category_counts_combined(target, db) == ({"Tools": 1}, {"Hardware": 1})
    monkeypatch.setattr(analytics_module.pd, "read_sql", MagicMock(side_effect=RuntimeError("sql")))
    assert service._get_bfs_category_counts_combined(target, db) == ({}, {})
    assert service._count_categories_from_df(pd.DataFrame(), "x", {}) == {}
    assert service._count_categories_from_df(pd.DataFrame({"x": [["a", "b"]]}), "x", {"a": "A", "b": "B"}) == {"A": 1, "B": 1}
    mapping_db = MagicMock(); mapping_db.execute.return_value.fetchall.return_value = [("Tools", "bolt")]
    monkeypatch.setattr(analytics_module, "get_remote_db_session", lambda: mapping_db)
    service._get_product_category_mapping = analytics_module.ConversationAnalyticsService._get_product_category_mapping.__get__(service)
    assert service._get_product_category_mapping(["bolt"]) == {"bolt": "Tools"}
    mapping_db.execute.side_effect = RuntimeError("mapping")
    assert service._get_product_category_mapping(["bolt"]) == {}


# Daily aggregation and summary --------------------------------------------


def test_daily_aggregation_all_metric_shapes_and_storage(monkeypatch):
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    buyer = service._calculate_buyer_summary_metrics([base_session(), base_session(user_type=UserType.seller, external_user_id="S")], date(2024, 1, 1))
    assert buyer["chats_initiated_by_buyers"] == 1 and buyer["total_rfqs_submitted"] == 1
    seller = service._calculate_seller_summary_metrics([base_session(user_type=UserType.seller, rfq_ids=None, rfq_id="R")], date(2024, 1, 1))
    assert seller["rfqs_requested"] == 1 and seller["counter_offers_accepted"] == 1
    assert service._calculate_buyer_summary_metrics([base_session(rfq_ids=None, rfq_id=None, product_items=None, products_bid_for={"products": ["x"]}, bids_accepted=["b"])], date.today())["total_rfqs_submitted"] == 0
    categories = service._calculate_category_summary_metrics([base_session(), base_session(user_type=UserType.seller)], date.today())
    assert categories["Tools"]["rfqs_uploaded"] == 1
    assert set(service._extract_categories_from_session(base_session(extracted_entities=[{"category": "Chem"}, "bad"]))) == {"Tools", "Chem"}
    assert service._extract_categories_from_session(base_session(product_items=None, extracted_entities=None)) == []
    assert service._aggregate_buyer_rolling_metrics([]) == {}
    totals = service._aggregate_buyer_rolling_metrics([{"chats_initiated_by_buyers": 2, "total_rfqs_submitted": 2, "avg_products_per_rfq": 1.5, "avg_categories_per_rfq": 1}])
    assert totals["avg_products_per_rfq"] == 1.5
    assert service._aggregate_seller_rolling_metrics([]) == {}
    assert service._aggregate_category_rolling_metrics([{"Tools": {"rfqs_uploaded": 2}}])["Tools"]["rfqs_uploaded"] == 2
    db, query = query_db([])
    monkeypatch.setattr(aggregation_module, "get_db_session", lambda: DbContext(db))
    assert service.run_daily_aggregation(date(2024, 1, 1)) is True
    query.all.return_value = [base_session()]
    service._calculate_buyer_summary_metrics = MagicMock(return_value={})
    service._calculate_seller_summary_metrics = MagicMock(return_value={})
    service._calculate_category_summary_metrics = MagicMock(return_value={})
    service._store_metrics = MagicMock()
    service._update_rolling_windows = MagicMock()
    assert service.run_daily_aggregation(date(2024, 1, 1)) is True
    query.all.side_effect = RuntimeError("query")
    assert service.run_daily_aggregation(date(2024, 1, 1)) is False
    query.all.side_effect = None
    existing = SimpleNamespace(metric_data={}, is_complete=False)
    query.first.return_value = existing
    service._store_metrics = aggregation_module.DailyAggregationService._store_metrics.__get__(service)
    service._store_metrics(db, date.today(), "buyer_summary", {"x": 1})
    assert existing.is_complete is True
    query.first.return_value = None
    service._store_metrics(db, date.today(), "seller_summary", {"x": 2})
    assert db.add.called


def test_daily_aggregation_rolling_window_success_existing_and_failure(monkeypatch):
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    service._calculate_rolling_window = MagicMock()
    service._update_rolling_windows(date(2024, 1, 1))
    service._calculate_rolling_window.side_effect = RuntimeError("window")
    with pytest.raises(RuntimeError):
        service._update_rolling_windows(date(2024, 1, 1))
    service._calculate_rolling_window = aggregation_module.DailyAggregationService._calculate_rolling_window.__get__(service)
    metric_rows = [SimpleNamespace(metric_type="buyer_summary", metric_data={"x": 1}, is_complete=True),
                   SimpleNamespace(metric_type="seller_summary", metric_data={"y": 1}, is_complete=True),
                   SimpleNamespace(metric_type="category_summary", metric_data={"Tools": {"x": 1}}, is_complete=True)]
    db, query = query_db(metric_rows, first=SimpleNamespace())
    query.first.return_value = SimpleNamespace()
    monkeypatch.setattr(aggregation_module, "get_db_session", lambda: DbContext(db))
    service._aggregate_buyer_rolling_metrics = MagicMock(return_value={"b": 1})
    service._aggregate_seller_rolling_metrics = MagicMock(return_value={"s": 1})
    service._aggregate_category_rolling_metrics = MagicMock(return_value={"c": 1})
    service._calculate_rolling_window(date(2024, 1, 1), "7day", 7)
    assert query.first.return_value.buyer_metrics == {"b": 1}
    query.first.return_value = None
    service._calculate_rolling_window(date(2024, 1, 1), "7day", 7)
    db.commit.side_effect = RuntimeError("commit")
    with pytest.raises(RuntimeError):
        service._calculate_rolling_window(date(2024, 1, 1), "7day", 7)


@pytest.mark.asyncio
async def test_daily_summary_disabled_empty_create_update_get_and_duration(monkeypatch):
    service = summary_module.DailySummaryService.__new__(summary_module.DailySummaryService)
    service.settings = SimpleNamespace(enable_daily_summarization=True)
    db, query = query_db([base_session()], first=None)
    monkeypatch.setattr(summary_module, "get_db_session", lambda: DbContext(db))
    result = await service.generate_daily_summary("U1", date(2024, 1, 1))
    assert result.sessions_count == 1 and result.avg_session_duration == 60
    query.all.return_value = []
    assert await service.generate_daily_summary("U1", date(2024, 1, 1)) is None
    service.settings.enable_daily_summarization = False
    assert await service.generate_daily_summary("U1", date(2024, 1, 1)) is None
    service.settings.enable_daily_summarization = True
    query.all.return_value = [base_session()]
    existing = SimpleNamespace(user_type=None, session_states_breakdown=None, primary_product_categories=None)
    query.first.return_value = existing
    result = await service.generate_daily_summary("U1", date(2024, 1, 1))
    assert result is existing and db.refresh.called
    summary = SimpleNamespace(date=date(2024, 1, 1), user_type=UserType.buyer, sessions_count=1,
        rfqs_created_count=2, session_states_breakdown=None, seller_interaction_count=0,
        avg_session_duration=1, primary_product_categories=None, product_items_count=1,
        unique_categories_count=1, session_continuation_count=0, total_rfq_value_estimate=2.5)
    query.first.return_value = summary
    found = await service.get_user_daily_summary("U1", date(2024, 1, 1))
    assert found["user_type"] == "buyer" and found["total_rfq_value_estimate"] == 2.5
    query.first.return_value = None
    assert await service.get_user_daily_summary("U1", date(2024, 1, 1)) is None
    query.first.side_effect = RuntimeError("summary")
    assert await service.get_user_daily_summary("U1", date(2024, 1, 1)) is None
    assert service._calculate_duration(base_session(completed_at=None, last_activity_at=datetime(2024, 1, 1, 0, 30))) == 30
    assert service._calculate_duration(base_session(created_at=None, completed_at=None, last_activity_at=None)) is None
    assert service._calculate_duration(base_session(created_at="bad", completed_at="bad")) is None


# Enhanced report -----------------------------------------------------------


def report_service():
    service = report_module.EnhancedExcelReportService.__new__(report_module.EnhancedExcelReportService)
    service.settings = SimpleNamespace()
    return service


def test_report_detail_fallbacks_summaries_and_metric_helpers(monkeypatch):
    service = report_service()
    assert service.sanitize_for_excel("a\x00b\x7fc") == "abc"
    assert service.sanitize_for_excel(None) == ""
    assert service._get_empty_metrics()["total_rfqs"] == 0
    assert service._get_metric_value("Total RFQs Submitted", {"total_rfqs": 3}) == 3
    assert service._get_seller_metric_value("RFQs Requested", {"rfqs_requested": 2}) == 2
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Seller Chats Initiated", 4]], columns=["metric_name", "total_value"])))
    assert service._calculate_seller_metrics(db, date(2024, 1, 1), date(2024, 1, 2))["seller_chats_initiated"] == 4
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Total RFQs Submitted", 4], ["Total Items in All RFQs", 8], ["Total Distinct RFQ Category Combinations", 4]], columns=["metric_name", "total_value"])))
    assert service._calculate_buyer_metrics(db, date(2024, 1, 1), date(2024, 1, 2))["avg_products_per_rfq"] == 2
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(side_effect=RuntimeError("db")))
    assert service._calculate_seller_metrics(db, date(2024, 1, 1), date(2024, 1, 2))["rfqs_requested"] == 0
    assert service._calculate_buyer_metrics(db, date(2024, 1, 1), date(2024, 1, 2)) is None
    assert service._generate_category_details_fallback(db, date(2024, 1, 1), date(2024, 1, 2)).shape[0] == 3
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=SimpleNamespace(empty=False, iloc=[[{"chats_initiated": 3}]])))
    assert service._get_rolling_window_metrics(db, date(2024, 1, 1), 7) == {"chats_initiated": 3}
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=pd.DataFrame()))
    assert service._get_rolling_window_metrics(db, date(2024, 1, 1), 7) is None
    assert service._get_rolling_window_metrics(db, date(2024, 1, 1), 2) is None
    assert service._get_metric_value("unknown", {}) == 0
    assert service._get_seller_metric_value("unknown", {}) == 0


def test_report_sheet_methods_and_generation(monkeypatch):
    service = report_service()
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(report_module, "get_db_session_context", lambda: DbContext(db))
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=pd.DataFrame({"x": [1]})))
    writer = FakeWriter()
    monkeypatch.setattr(pd.DataFrame, "to_excel", MagicMock())
    for method in (service._generate_buyer_details_sheet, service._generate_seller_details_sheet, service._generate_category_details_sheet,
                   service._generate_category_summary_sheet, service._generate_aggregate_sheet_90d):
        method(writer, date(2024, 1, 1))
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(side_effect=RuntimeError("query")))
    # These methods intentionally retain their existing fallback/empty-data behavior.
    with pytest.raises(UnboundLocalError):
        service._generate_buyer_details_sheet(writer, date(2024, 1, 1))
    assert service._generate_category_details_fallback(db, date(2024, 1, 1), date(2024, 1, 2)).shape[0] == 3
    service._generate_buyer_details_sheet = MagicMock()
    service._generate_seller_details_sheet = MagicMock()
    service._generate_category_details_sheet = MagicMock()
    service._generate_buyer_summary_sheet = MagicMock()
    service._generate_seller_summary_sheet = MagicMock()
    service._generate_category_summary_sheet = MagicMock()
    service._generate_aggregate_sheet_90d = MagicMock()
    service._format_excel_sheets = MagicMock()
    monkeypatch.setattr(report_module.pd, "ExcelWriter", MagicMock(return_value=FakeWriter()))
    assert service.generate_report(date(2024, 1, 1), "report.xlsx") == "report.xlsx"
    assert service._format_excel_sheets.called


def test_report_formatting_and_empty_summary(monkeypatch):
    service = report_service()
    worksheet = MagicMock()
    worksheet.__getitem__.return_value = [MagicMock()]
    worksheet.columns = [[SimpleNamespace(column_letter="A", value="header")]]
    workbook = SimpleNamespace(sheetnames=["Sheet1"], __getitem__=lambda self, key: worksheet)
    writer = SimpleNamespace(book=workbook)
    service._format_excel_sheets(writer)
    assert service._get_empty_metrics()["bfs_products_bids_count"] == 0


@pytest.mark.asyncio
async def test_report_email_success_and_failures(monkeypatch, tmp_path):
    service = report_service()
    file_path = tmp_path / "report.xlsx"
    file_path.write_bytes(b"excel")
    email = SimpleNamespace(send_email_by_template=AsyncMock(return_value={"status": "Success"}))
    monkeypatch.setattr(report_module, "EmailService", MagicMock(return_value=email))
    fake_api = SimpleNamespace(init_procucev_api_client=AsyncMock(), close_procucev_api_client=AsyncMock())
    monkeypatch.setitem(__import__("sys").modules, "app.procucev_apis.procucev_api_client", fake_api)
    await service._send_report_email(str(file_path), date(2024, 1, 1))
    email.send_email_by_template.return_value = {"status": "Failed"}
    await service._send_report_email(str(file_path), date(2024, 1, 1))


# Webhook health monitor ----------------------------------------------------


def health_service(recipients=None):
    service = health_module.WebhookHealthMonitorService.__new__(health_module.WebhookHealthMonitorService)
    service.settings = SimpleNamespace(WHATSAPP_BASE_URL="http://wa", WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p",
        webhook_alert_state_ttl_seconds=60, webhook_health_monitoring_enabled=True)
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


def health_state(current="HEALTHY"):
    return {"current_state": current, "is_alerting": False, "last_alert_time": None,
        "failure_start_time": None, "consecutive_failures": 0, "consecutive_successes": 0,
        "consecutive_warnings": 0, "last_severity": "OK", "last_check_time": None,
        "last_latency_ms": None, "last_error": None}


@pytest.mark.asyncio
async def test_health_session_http_statuses_and_locks(monkeypatch):
    service = health_service()
    session = MagicMock(); session.closed = False
    session.post.return_value = AsyncContext(FakeResponse(200))
    service._session = session
    assert (await service._check_api_health())[0] == health_module.HealthStatus.OK
    session.post.return_value = AsyncContext(FakeResponse(400))
    assert (await service._check_api_health())[0] == health_module.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(FakeResponse(500))
    assert (await service._check_api_health())[0] == health_module.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(FakeResponse(201))
    assert (await service._check_api_health())[0] == health_module.HealthStatus.CRITICAL
    session.post.return_value = AsyncContext(error=asyncio.TimeoutError())
    assert "Timeout" in (await service._check_api_health())[2]
    session.post.return_value = AsyncContext(error=health_module.aiohttp.ClientError("down"))
    assert "Connection" in (await service._check_api_health())[2]
    session.post.return_value = AsyncContext(error=RuntimeError("bad"))
    assert "Unexpected" in (await service._check_api_health())[2]
    service.redis.init_client = AsyncMock(); service.redis.client = MagicMock()
    service.redis.client.set = AsyncMock(return_value=True)
    assert await service._try_acquire_leader_lock()
    service.redis.client.set.return_value = False; service.redis.get = AsyncMock(return_value="other")
    assert await service._try_acquire_leader_lock() is False
    service.redis.client.set.side_effect = RuntimeError("redis")
    assert await service._try_acquire_leader_lock() is False
    service.redis.get.return_value = "worker"; service.redis.expire = AsyncMock()
    assert await service._renew_leader_lock()
    service.redis.get.return_value = "other"
    assert await service._renew_leader_lock() is False
    service.redis.get.return_value = "worker"; service.redis.delete = AsyncMock()
    await service._release_leader_lock(); service.redis.delete.assert_awaited_once()
    service.redis.get.side_effect = RuntimeError("redis")
    await service._release_leader_lock()


@pytest.mark.asyncio
async def test_health_state_history_machine_and_alert_branches(monkeypatch):
    service = health_service()
    service.redis.init_client = AsyncMock(); service.redis.get = AsyncMock(return_value=None); service.redis.set = AsyncMock()
    assert (await service._get_state())["current_state"] == "HEALTHY"
    service.redis.get.return_value = json.dumps(health_state())
    assert (await service._get_state())["current_state"] == "HEALTHY"
    service.redis.get.side_effect = RuntimeError("state")
    assert (await service._get_state())["current_state"] == "HEALTHY"
    await service._save_state(health_state()); service.redis.set.side_effect = RuntimeError("save"); await service._save_state(health_state())
    service.redis.get.side_effect = None; service.redis.get.return_value = json.dumps([{"i": i} for i in range(100)])
    await service._add_to_history(health_module.HealthStatus.OK, 1.2, None)
    assert len(json.loads(service.redis.set.await_args.args[1])) == 100
    service.redis.get.side_effect = RuntimeError("history"); await service._add_to_history(health_module.HealthStatus.CRITICAL, None, "down")
    state = health_state("HEALTHY"); service._save_state = AsyncMock()
    await service._process_check_result(state, health_module.HealthStatus.CRITICAL, 2, "down")
    assert state["current_state"] == "FAILING"
    state["failure_start_time"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat(); service._send_critical_alert = AsyncMock()
    await service._process_check_result(state, health_module.HealthStatus.CRITICAL, 2, "down")
    assert state["current_state"] == "ALERTING"
    state = health_state("HEALTHY"); await service._handle_warning_status(state, health_module.MonitorState.HEALTHY)
    assert state["current_state"] == "FAILING"
    state["failure_start_time"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat(); state["consecutive_warnings"] = 1; service._send_warning_alert = AsyncMock()
    await service._handle_warning_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "ALERTING"
    state = health_state("FAILING"); await service._handle_ok_status(state, health_module.MonitorState.FAILING)
    assert state["current_state"] == "HEALTHY"
    state = health_state("ALERTING"); await service._handle_ok_status(state, health_module.MonitorState.ALERTING)
    assert state["current_state"] == "RECOVERED"
    state["consecutive_successes"] = 1; service._send_recovery_notification = AsyncMock()
    await service._handle_ok_status(state, health_module.MonitorState.RECOVERED)
    assert state["current_state"] == "HEALTHY"
    state = health_state("RECOVERED"); service._send_relapse_alert = AsyncMock()
    await service._handle_critical_status(state, health_module.MonitorState.RECOVERED)
    assert state["current_state"] == "ALERTING"
    await service._handle_warning_status(state, health_module.MonitorState.RECOVERED)
    assert service._send_relapse_alert.await_count == 2
    service._check_api_health = AsyncMock(return_value=(health_module.HealthStatus.OK, 1, None))
    await service._health_check_cycle()
    assert service._save_state.await_count >= 1
    assert health_module.WebhookHealthMonitorService._format_duration(timedelta(hours=1, minutes=2, seconds=3)) == "1h 2m 3s"
    assert health_module.WebhookHealthMonitorService._format_duration(timedelta()) == "0s"


@pytest.mark.asyncio
async def test_health_alert_email_success_failure_and_loop_controls(monkeypatch):
    service = health_service(["a@example.com"])
    service.email_service.send_email_by_template = AsyncMock(return_value={"status": "Success"})
    state = health_state("ALERTING"); state.update(failure_start_time=datetime.utcnow().isoformat(), last_latency_ms=4, last_error="down", consecutive_failures=2)
    await service._send_critical_alert(state); await service._send_warning_alert(state); await service._send_recovery_notification(state); await service._send_relapse_alert(state)
    assert service.email_service.send_email_by_template.await_count == 4
    service.email_service.send_email_by_template.return_value = {"status": "Failed"}
    await service._send_critical_alert(state); await service._send_warning_alert(state); await service._send_recovery_notification(state); await service._send_relapse_alert(state)
    service.email_service.send_email_by_template.side_effect = RuntimeError("mail")
    await service._send_critical_alert(state); await service._send_warning_alert(state); await service._send_recovery_notification(state); await service._send_relapse_alert(state)
    no_mail = health_service([])
    await no_mail._send_critical_alert(state); await no_mail._send_warning_alert(state); await no_mail._send_recovery_notification(state); await no_mail._send_relapse_alert(state)
    service._running = False
    await service._interruptible_sleep(0)
    session = MagicMock(); session.closed = False; session.close = AsyncMock(); service._session = session
    await service._close_session(); session.close.assert_awaited_once()
    service._session = None; await service._close_session()
    service.settings.webhook_health_monitoring_enabled = False
    await service.start_monitoring()
    service._running = True; await service.start_monitoring(); service._running = False
    service._try_acquire_leader_lock = AsyncMock(return_value=False)
    service._interruptible_sleep = AsyncMock()
    await service.stop_monitoring()
    assert service._running is False
    service._renew_leader_lock = AsyncMock(return_value=False); service._health_check_cycle = AsyncMock(); service._interruptible_sleep = AsyncMock()
    service._running = True; await service._run_as_leader(); assert service.is_leader is False


# Excel processing and validation ------------------------------------------


def processing_service():
    service = processing_module.ExcelProcessingService.__new__(processing_module.ExcelProcessingService)
    service.target_columns = ["S.No", "ItemDescription", "Specification", "Uom", "Quantity", "Remarks"]
    service.openai_service = SimpleNamespace(process_excel_to_rfqs=AsyncMock())
    return service


@pytest.mark.asyncio
async def test_processing_helpers_and_pipeline_branches(monkeypatch):
    service = processing_service()
    assert service._create_fallback_mapping(["Quantity", "Other"]) == {"Quantity": "Quantity"}
    extracted = service._extract_items_with_mapping(pd.DataFrame([["bolt", "M10", 2], ["missing", "", 1]], columns=["ItemDescription", "Specification", "Quantity"]),
        ["ItemDescription", "Specification", "Quantity"], {"ItemDescription": "ItemDescription", "Specification": "Specification", "Quantity": "Quantity"})
    assert extracted["success"] and len(extracted["items"]) == 1
    assert service._normalize_quantity("1,200 kg") == 1200.0 and service._normalize_quantity(None) is None and service._normalize_quantity("bad") is None
    assert service._detect_regional_formats([{"Quantity": "1,5"}])["decimal_separator"] == ","
    assert service._validate_business_rules([{"ItemDescription": "x", "Quantity": 0, "Uom": "each"}])["valid"] is False
    assert service._validate_items_comprehensive([{"ItemDescription": "x", "Specification": "s", "Uom": "Units", "Quantity": 2}])["valid"]
    invalid = service._validate_items_comprehensive([{"ItemDescription": "x"}])
    assert invalid["valid"] is False and "field_explanations" in invalid
    cleaned = service._preprocess_excel_data(pd.DataFrame([[1, 2, 2], [1, 2, 2], [None, None, None]], columns=["A", "A.1", "Unnamed: 2"]))
    assert len(cleaned) == 1
    assert service.encode_for_api(b"abc", "x.xlsx")["boqFileName"] == "x.xlsx"
    assert service._format_date_for_display("2024-01-02") == "02 January" and service._format_date_for_display("bad") == "bad"
    assert service._validate_rfqs_consistency([{"city": "A"}, {"city": "A"}])["valid"]
    assert service._validate_rfqs_consistency([{"deliveryDate": "2024-01-01"}, {"deliveryDate": "2024-01-02"}])["valid"] is False
    service.openai_service.process_excel_to_rfqs.return_value = {"success": True, "rfqs": []}
    assert (await service._process_excel_with_openai(pd.DataFrame([[1]]), "x"))["success"]
    service.openai_service.process_excel_to_rfqs.return_value = {"success": False, "error": "bad"}
    assert (await service._process_excel_with_openai(pd.DataFrame([[1]]), "x"))["success"] is False
    service.openai_service.process_excel_to_rfqs.side_effect = RuntimeError("openai")
    assert "OpenAI" in (await service._process_excel_with_openai(pd.DataFrame([[1]]), "x"))["error"]
    monkeypatch.setattr(processing_module.pd, "ExcelFile", MagicMock(return_value=SimpleNamespace(close=MagicMock())))
    assert service._is_valid_excel_file(b"PK\x03\x04data", "x.xlsx") is True
    assert service._is_valid_excel_file(b"bad", "x.xlsx") is False
    assert service._is_valid_excel_file(b"PK\x03\x04data", "x.txt") is False
    monkeypatch.setattr(processing_module.pd, "ExcelFile", MagicMock(side_effect=RuntimeError("read")))
    assert service._is_valid_excel_file(b"PK\x03\x04data", "x.xlsx") is False
    monkeypatch.setattr(processing_module.pd, "ExcelFile", MagicMock(return_value=SimpleNamespace(sheet_names=["one", "two"], close=MagicMock())))
    monkeypatch.setattr(processing_module.pd, "read_excel", MagicMock(return_value=pd.DataFrame([["x", "s", 2]])))
    service._is_valid_excel_file = MagicMock(return_value=True); service._validate_excel_structure = AsyncMock(return_value={"valid": True})
    assert "worksheets" in (await service.process_excel_file(b"bytes", "x.xlsx"))["error"]
    monkeypatch.setattr(processing_module.pd, "ExcelFile", MagicMock(return_value=SimpleNamespace(sheet_names=["one"], close=MagicMock())))
    service._process_excel_with_openai = AsyncMock(return_value={"success": False, "error": "AI failed"})
    assert (await service.process_excel_file(b"bytes", "x.xlsx"))["success"] is False
    service._process_excel_with_openai = AsyncMock(return_value={"success": True, "rfqs": [{"products": [{"description": "x", "quantity": 2}], "deliveryDate": "d", "city": "c", "state": "s", "pincode": "p"}], "processing_summary": {"total_products": 1, "total_products_extracted": 1}})
    result = await service.process_excel_file(b"bytes", "x.xlsx")
    assert result["success"] and result["items"][0]["ItemDescription"] == "x"
    service._process_excel_with_openai.return_value = {"success": True, "rfqs": [], "processing_summary": {"total_products": 1, "skipped_rows": 1}}
    assert (await service.process_excel_file(b"bytes", "x.xlsx"))["should_skip_rfq_creation"] is True
    assert (await service.process_excel_file(b"x" * (3 * 1024 * 1024 + 1), "x.xlsx"))["success"] is False
    service._is_valid_excel_file.return_value = False
    assert (await service.process_excel_file(b"bytes", "x.xlsx"))["success"] is False


@pytest.mark.asyncio
async def test_processing_template_and_structure_validation(monkeypatch):
    service = processing_service()
    data = service.create_standard_template([{"S.No": "bad", "Quantity": "bad", "ItemDescription": "x"}])
    assert data.startswith(b"PK")
    import openpyxl
    monkeypatch.setattr(openpyxl, "load_workbook", MagicMock())
    workbook = MagicMock(); worksheet = MagicMock(); worksheet.iter_rows.return_value = [[SimpleNamespace(value="x")]]; worksheet.merged_cells.ranges = []
    openpyxl.load_workbook.return_value = workbook; workbook.active = worksheet
    assert await service._validate_excel_structure(b"bytes") == {"valid": True}
    worksheet.iter_rows.return_value = [[SimpleNamespace(value="x")] for _ in range(51)]
    assert (await service._validate_excel_structure(b"bytes"))["valid"] is False
    worksheet.iter_rows.return_value = [[SimpleNamespace(value="x")]]; worksheet.merged_cells.ranges = ["A1:B1"]
    assert (await service._validate_excel_structure(b"bytes"))["valid"] is False
    openpyxl.load_workbook.side_effect = RuntimeError("bad workbook")
    monkeypatch.setattr(processing_module.pd, "read_excel", MagicMock(side_effect=RuntimeError("bad pandas")))
    assert (await service._validate_excel_structure(b"bytes"))["valid"] is False


@pytest.mark.asyncio
async def test_validation_pipeline_download_readability_and_content(monkeypatch):
    service = validation_module.ExcelValidationService()
    assert not service._validate_file_extension("") and not service._validate_file_extension("x.csv") and service._validate_file_extension("x.XLSM")
    assert service._validate_file_integrity(b"short")["valid"] is False
    assert service._validate_file_integrity(b"\0" * 8)["error_type"] == "corrupted_file"
    assert service._validate_excel_content(b"a,b\nc,d")["error_type"] == "csv_format_detected"
    assert service._validate_excel_content(b"PK\x03\x04data")["format"] == "xlsx"
    assert service._validate_excel_content(b"bad")["error_type"] == "corrupted_file"
    class HttpSession:
        def __init__(self, *args, **kwargs): self.calls = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def get(self, url):
            self.calls += 1
            return AsyncContext(FakeResponse(200)) if self.calls == 1 else AsyncContext(FakeResponse(404))
    monkeypatch.setattr(validation_module.aiohttp, "ClientSession", HttpSession)
    assert await service._download_file_with_retry("url") == b"PK\x03\x04fake"
    class ErrorSession(HttpSession):
        def get(self, url): raise RuntimeError("network")
    monkeypatch.setattr(validation_module.aiohttp, "ClientSession", ErrorSession)
    assert await service._download_file_with_retry("url", max_retries=1) is None
    service._download_file_with_retry = AsyncMock(return_value=b"valid")
    service._validate_file_integrity = MagicMock(return_value={"valid": True})
    service._validate_excel_content = MagicMock(return_value={"valid": True, "format": "xlsx"})
    service._validate_excel_readability = AsyncMock(return_value={"valid": True})
    service._validate_excel_structure_comprehensive = AsyncMock(return_value={"valid": True})
    service._validate_data_quality = AsyncMock(return_value={"valid": True})
    result = await service.validate_excel_file_from_url("url", "a.xlsx")
    assert result["valid"] and result["validation_summary"]["structure_check"] == "passed"
    assert (await service.validate_excel_file_from_url("url", "a.xlsx", True))["validation_summary"]["data_quality_check"] == "skipped"
    assert (await service.validate_excel_file_from_url("url", "a.txt"))["error_type"] == "invalid_extension"
    service._download_file_with_retry.return_value = None
    assert (await service.validate_excel_file_from_url("url", "a.xlsx"))["error_type"] == "download_failed"
    service._download_file_with_retry.return_value = b"valid"; service._validate_file_integrity.return_value = {"valid": False, "error_type": "bad"}
    assert (await service.validate_excel_file_from_url("url", "a.xlsx"))["error_type"] == "bad"
    service._validate_file_integrity.side_effect = RuntimeError("unexpected")
    assert (await service.validate_excel_file_from_url("url", "a.xlsx"))["error_type"] == "validation_error"


@pytest.mark.asyncio
async def test_validation_readability_structure_quality_and_data_types(monkeypatch):
    service = validation_module.ExcelValidationService()
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(side_effect=ValueError("password protected")))
    assert (await service._validate_excel_readability(b"x", "a.xlsx"))["error_type"] == "password_protected"
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A": ["x"], "B": ["y"]})))
    assert (await service._validate_excel_readability(b"x", "a.xlsx"))["valid"]
    assert (await service._validate_data_quality(b"x"))["valid"]
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"Quantity": ["@bad"]})))
    assert (await service._validate_data_quality(b"x"))["valid"] is False
    assert service._validate_data_types(pd.DataFrame({"Quantity": [1, "bad"]}))
    assert not service._validate_data_types(pd.DataFrame({"Unnamed: 0": [1, "bad"]}))
    assert (await service._validate_data_quality(b"x"))["valid"] is False
    monkeypatch.setattr(validation_module, "load_workbook", MagicMock(side_effect=RuntimeError("invalid")))
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(side_effect=RuntimeError("pandas invalid")))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["valid"] is False
    workbook = MagicMock(); worksheet = MagicMock(); workbook.worksheets = [worksheet]; workbook.active = worksheet
    worksheet.iter_rows.return_value = [[SimpleNamespace(value="Header")]]; worksheet.merged_cells.ranges = []; worksheet.row_dimensions = {}; worksheet.column_dimensions = {}; worksheet._pivots = []
    monkeypatch.setattr(validation_module, "load_workbook", MagicMock(return_value=workbook))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["valid"]
    workbook.worksheets = [worksheet, MagicMock()]
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "multiple_worksheets"
    workbook.worksheets = [worksheet]; worksheet.iter_rows.return_value = [[SimpleNamespace(value="x")] for _ in range(51)]
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "too_many_rows"
    worksheet.iter_rows.return_value = [[SimpleNamespace(value="x")]]; worksheet.merged_cells.ranges = ["A1:B1"]
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "merged_cells_found"
    worksheet.merged_cells.ranges = []; worksheet._pivots = [object()]
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "pivot_tables_found"
