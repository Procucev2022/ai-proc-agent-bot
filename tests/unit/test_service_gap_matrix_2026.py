"""Deterministic gap coverage for conversation analytics and enhanced categorization."""
from __future__ import annotations

import builtins
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pandas as pd
import pytest

import app.services.conversation_analytics_service as analytics_mod
import app.services.enhanced_auto_categorization_service as enhanced_mod
import app.services.learning_categorization_service as learning_mod


class ChainQuery:
    """Small SQLAlchemy-shaped query fake with programmable result branches."""

    def __init__(self, *, all_values=None, first_values=None, count_value=0):
        self.all_value = [] if all_values is None else all_values
        self.first_values = list(first_values or [])
        self.count_value = count_value
        self.statement = "SELECT mocked"

    def filter(self, *_args, **_kwargs):
        return self

    def with_entities(self, *_args, **_kwargs):
        return self

    def distinct(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.all_value

    def first(self):
        value = self.first_values.pop(0) if self.first_values else None
        if isinstance(value, BaseException):
            raise value
        return value

    def count(self):
        return self.count_value


class FakeDB:
    def __init__(self, query):
        self.query_value = query
        self.add = MagicMock()
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.bind = "mock-bind"

    def query(self, *_args, **_kwargs):
        return self.query_value


class DbContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_args):
        return False


def analytics_service():
    service = object.__new__(analytics_mod.ConversationAnalyticsService)
    service.batch_size = 2
    service.settings = SimpleNamespace(procucev_db_name="proc", whatsapp_db="wa")
    service.openai_service = SimpleNamespace(
        tools_dir=".",
        default_model="mock-model",
        _load_prompt=MagicMock(return_value="mock system prompt"),
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())),
    )
    return service


def enhanced_service():
    service = object.__new__(enhanced_mod.EnhancedAutoCategorizationService)
    service.collection = MagicMock()
    service.category_collection = MagicMock()
    service.fallback_service = SimpleNamespace(
        collection=MagicMock(),
        get_collection_stats=MagicMock(return_value={"total_items": 2}),
        _get_similar_items=MagicMock(return_value=[]),
    )
    service.openai_service = SimpleNamespace(
        categorize_with_similar_items=AsyncMock(),
        client=object(),
    )
    service.chroma_path = "mock-chroma"
    return service


def _buyer_metric():
    return SimpleNamespace(
        number_of_chats=2,
        email="buyer@example.test",
        phone_number="100",
        total_rfq_raised=2,
        total_items_in_rfqs=4,
        total_distinct_categories_in_rfq=2,
        total_incomplete_rfq=1,
        failed_registration=0,
        buyers_started_but_not_raised_rfq=1,
        no_of_products_searched=2,
        bfs_searches=1,
        products_bid_for=1,
        bfs_stock_products_bid_placed_count=1,
    )


def _seller_metric():
    return SimpleNamespace(
        number_of_chats=2,
        email="seller@example.test",
        phone_number="200",
        total_rfqs_requested=2,
        subscription_plans_requested=1,
        seller_failed_registration=0,
        seller_successful_registration=1,
        zero_credit_rfq_attempt=1,
        rfq_response_ai=2,
    )


def _unknown_metric():
    return SimpleNamespace(
        email=None,
        phone_number="300",
        unregistered_seller_initiated_chat=1,
        unregistered_seller_requested_rfq=1,
        unregistered_buyer_bfs_only=1,
        number_of_faq_or_general_queries=1,
    )


@pytest.mark.asyncio
async def test_analytics_batch_malformed_history_and_individual_ai_fallback(monkeypatch):
    service = analytics_service()
    malformed = SimpleNamespace(
        session_id="malformed",
        external_user_id="u1",
        created_at=None,
        conversation_history="not-a-message-list",
    )
    assert await service._prepare_batch_session_data([malformed]) == [{
        "session_id": "malformed",
        "external_user_id": "u1",
        "conversation_history": [],
        "created_at": None,
    }]

    response = SimpleNamespace(output=[SimpleNamespace(type="text", arguments="{}")] )
    service.openai_service.client.responses.create.return_value = response
    file_handle = MagicMock()
    file_handle.__enter__.return_value = file_handle
    monkeypatch.setattr(builtins, "open", MagicMock(return_value=file_handle))
    monkeypatch.setattr(analytics_mod.json, "load", MagicMock(return_value={"name": "tool"}))
    assert await service._analyze_batch_with_ai([], 1, date(2024, 1, 1)) is None

    class RateLimitedPath:
        def __truediv__(self, _name):
            raise RuntimeError("rate limit")

    real_analyze = analytics_mod.ConversationAnalyticsService._analyze_batch_with_ai.__get__(service)
    real_individual = analytics_mod.ConversationAnalyticsService._process_sessions_individually.__get__(service)
    service.openai_service.tools_dir = RateLimitedPath()
    service._process_sessions_individually = AsyncMock(return_value={"sessions": [], "total_sessions": 0})
    assert await real_analyze([], 2, date(2024, 1, 1)) == {"sessions": [], "total_sessions": 0}

    monkeypatch.setattr(analytics_mod.asyncio, "sleep", AsyncMock())
    service._analyze_batch_with_ai = AsyncMock(return_value={"sessions": [{"session_id": "s1"}]})
    individual = await real_individual([{"session_id": "s1"}], 2, date(2024, 1, 1))
    assert individual == {"sessions": [{"session_id": "s1"}], "total_sessions": 1}


def test_analytics_button_reply_remote_data_and_empty_inputs(monkeypatch):
    service = analytics_service()
    sequence = service._extract_chat_sequence({
        "messages": [
            {"role": "user", "content": {"button_reply": {"title": "Confirm"}}},
            {"role": "assistant", "content": {"type": "button_reply", "button_reply": {"title": "Continue"}}},
        ]
    })
    assert sequence == "User: Confirm\nAssistant: Continue"
    assert service._extract_chat_sequence({}) == ""

    class FixedDatetime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            return datetime(2024, 2, 3, tzinfo=timezone.utc)

    empty_query = ChainQuery(all_values=[])
    monkeypatch.setattr(analytics_mod, "datetime", FixedDatetime)
    monkeypatch.setattr(analytics_mod, "get_db_session", lambda: DbContext(FakeDB(empty_query)))
    # Keep the async invocation explicit so this test remains independent of event-loop plugins.
    result = asyncio_run(service.analyze_daily_conversations(None))
    assert result["success"] is True
    assert result["date"] == "2024-02-03"
    assert result["total_sessions"] == 0


def asyncio_run(awaitable):
    """Run one coroutine from a synchronous test without touching external services."""
    import asyncio

    return asyncio.run(awaitable)


@pytest.mark.asyncio
async def test_analytics_date_range_and_all_dataframe_join_matrix(monkeypatch):
    service = analytics_service()
    service.analyze_daily_conversations = AsyncMock(side_effect=[{"success": True}, {"success": False}])
    ranged = await service.analyze_date_range(date(2024, 1, 1), date(2024, 1, 2))
    assert ranged["processed_dates"] == 2
    assert [item["result"]["success"] for item in ranged["results"]] == [True, False]

    session = SimpleNamespace(
        created_at=datetime(2024, 1, 1),
        external_user_id="+100",
        conversation_history={"messages": []},
    )
    empty_seller = pd.DataFrame()
    empty_unknown = pd.DataFrame()
    empty_interest = pd.DataFrame()
    empty_bfs = pd.DataFrame()

    async def run_case(buyer_df, seller_df, remote_buyer, remote_seller, remote_bids):
        current = analytics_service()
        query = ChainQuery(all_values=[session])
        monkeypatch.setattr(analytics_mod, "get_db_session", lambda: DbContext(FakeDB(query)))
        current._process_sessions_in_batches = AsyncMock(return_value={"date": "2024-01-01", "total_sessions": 0, "sessions": []})
        current._create_dataframes_from_sessions = MagicMock(return_value=(buyer_df, seller_df, empty_unknown))
        current._create_seller_rfq_interest_event_df = MagicMock(return_value=empty_interest)
        current._create_bfs_search_dataframe = MagicMock(return_value=empty_bfs)
        current._query_remote_users = MagicMock(return_value=remote_buyer)
        current._query_remote_seller_rfqs = MagicMock(return_value=remote_seller)
        current._query_remote_counter_seller_bids = MagicMock(return_value=remote_bids)
        current._query_remote_rfq_categories = MagicMock(return_value=pd.DataFrame())
        for name in (
            "_dump_joined_buyer_df_to_db",
            "_dump_joined_seller_df_to_db",
            "calculate_and_store_daily_aggregates",
            "calculate_and_store_seller_daily_aggregates",
        ):
            setattr(current, name, MagicMock())
        return await current.analyze_daily_conversations(date(2024, 1, 1))

    buyer_only = pd.DataFrame({"date": ["2024-01-01"], "session_id": ["b"], "phone_number": ["100"], "buyer_email": ["b"]})
    assert (await run_case(buyer_only, empty_seller, pd.DataFrame(), empty_seller, pd.DataFrame()))["success"]

    remote_buyer_only = pd.DataFrame({"date": ["2024-01-01"], "phone": ["100"], "username": ["b"]})
    assert (await run_case(pd.DataFrame(), empty_seller, remote_buyer_only, empty_seller, pd.DataFrame()))["success"]

    buyer_both = pd.DataFrame({
        "date": ["2024-01-01"], "session_id": ["b"], "phone_number": ["100"],
        "buyer_email": ["b"], "bfs_products_searched_list": [[]],
    })
    assert (await run_case(buyer_both, empty_seller, remote_buyer_only, empty_seller, pd.DataFrame()))["success"]

    seller_only = pd.DataFrame({"date": ["2024-01-01"], "session_id": ["s"], "phone_number": ["200"], "seller_email": ["s"]})
    assert (await run_case(pd.DataFrame(), seller_only, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))["success"]

    remote_seller = pd.DataFrame({"rfq_date": ["2024-01-01"], "phone": ["200"], "username": ["s"]})
    assert (await run_case(pd.DataFrame(), seller_only, pd.DataFrame(), remote_seller, pd.DataFrame()))["success"]
    assert (await run_case(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), remote_seller, pd.DataFrame()))["success"]

    remote_bids = pd.DataFrame({"date": ["2024-01-01"], "phone_number": ["200"], "username": ["s"], "bids_accepted": [1], "counter_offer_accepted": [1]})
    assert (await run_case(pd.DataFrame(), seller_only, pd.DataFrame(), remote_seller, remote_bids))["success"]


def _buyer_row(session_id, value_date):
    return {
        "date_x": pd.NaT,
        "date_y": pd.NaT,
        "date": value_date,
        "session_id": session_id,
        "buyer_email": "buyer@example.test",
        "phone_number": "100",
        "total_rfqs_raised": 2,
        "total_items_in_rfqs": 4,
        "total_distinct_rfq_category": 2,
        "number_of_buyer_chats": 1,
        "successful_rfqs_ai": 1,
        "bfs_searches": 1,
        "products_bid_for": 1,
        "no_of_products_searched": 1,
        "bfs_stock_products_bid_placed_count": 1,
        "bfs_products_searched_list": ["pump"],
    }


def _seller_row(session_id, value_date):
    return {
        "date": value_date,
        "rfq_date": pd.NaT,
        "session_id": session_id,
        "seller_email": "seller@example.test",
        "phone_number": "200",
        "number_of_seller_chats": 1,
        "requested_rfq_ai": 1,
        "rfq_response_ai": 1,
        "subscription_plans_requested": 1,
        "zero_credit_rfq_attempt": 1,
    }


def _unknown_row(session_id, value_date):
    return {
        "date": value_date,
        "session_id": session_id,
        "phone_number": "300",
        "user_type": "unknown",
        "email": "unknown@example.test",
        "confidence_score": 0.5,
    }


def test_analytics_persistence_upserts_skips_and_errors(monkeypatch):
    service = analytics_service()
    value_date = date(2024, 1, 1)

    existing_buyer = SimpleNamespace()
    buyer_query = ChainQuery(first_values=[existing_buyer, None, None])
    buyer_db = FakeDB(buyer_query)
    buyer_db.add.side_effect = [None, RuntimeError("buyer add")]
    buyer_db.commit.side_effect = [None, RuntimeError("buyer commit")]
    buyer_df = pd.DataFrame([_buyer_row("existing", value_date), _buyer_row("commit-fails", value_date), _buyer_row("row-fails", value_date), _buyer_row("skip", pd.NaT)])
    service._dump_joined_buyer_df_to_db(buyer_df, buyer_db)
    assert existing_buyer.session_id == "existing"
    assert buyer_db.rollback.call_count == 1

    existing_seller = SimpleNamespace()
    seller_query = ChainQuery(first_values=[existing_seller, None, None])
    seller_db = FakeDB(seller_query)
    seller_db.add.side_effect = [None, RuntimeError("seller add")]
    seller_db.commit.side_effect = [None, RuntimeError("seller commit")]
    seller_df = pd.DataFrame([_seller_row("existing", value_date), _seller_row("commit-fails", value_date), _seller_row("row-fails", value_date), _seller_row("skip", pd.NaT)])
    service._dump_joined_seller_df_to_db(seller_df, seller_db)
    assert existing_seller.session_id == "existing"
    assert seller_db.rollback.call_count == 1

    existing_unknown = SimpleNamespace()
    unknown_query = ChainQuery(first_values=[existing_unknown, None, None])
    unknown_db = FakeDB(unknown_query)
    unknown_db.add.side_effect = [None, RuntimeError("unknown add")]
    unknown_db.commit.side_effect = [None, RuntimeError("unknown commit")]
    unknown_df = pd.DataFrame([_unknown_row("existing", value_date), _unknown_row("commit-fails", value_date), _unknown_row("row-fails", value_date), _unknown_row("skip", pd.NaT)])
    service._dump_unknown_df_to_db(unknown_df, unknown_db)
    assert existing_unknown.user_type == "unknown"
    assert unknown_db.rollback.call_count == 1

    class FixedDatetime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            return datetime(2024, 1, 2)

    monkeypatch.setattr(analytics_mod, "datetime", FixedDatetime)
    existing_fact = SimpleNamespace()
    fact_query = ChainQuery(first_values=[existing_fact, None, RuntimeError("fact query")])
    fact_db = FakeDB(fact_query)
    fact_db.add.side_effect = [None]
    fact_db.commit.side_effect = [None, RuntimeError("fact commit")]
    fact_df = pd.DataFrame([
        {"response_date": None, "date": None, "session_id": "", "rfq_id": "", "seller_id": ""},
        {"response_date": "2024-01-01", "session_id": "s1", "rfq_id": "r1", "seller_id": "v1", "category": "Tools", "rfq_notified_at": "invalid", "seller_response_at": "also-invalid"},
        {"response_date": "2024-01-01", "session_id": "s2", "rfq_id": "r2", "seller_id": "v2", "category": "Tools"},
        {"response_date": "2024-01-01", "session_id": "s3", "rfq_id": "r3", "seller_id": "v3", "category": "Tools"},
    ])
    service._dump_joined_seller_interest_to_fact_table(fact_df, fact_db)
    assert existing_fact.category == "Tools"
    assert fact_db.rollback.call_count == 1

    bfs_existing = SimpleNamespace()
    bfs_query = ChainQuery(first_values=[bfs_existing, None])
    bfs_db = FakeDB(bfs_query)
    bfs_db.commit.side_effect = [None, RuntimeError("bfs commit")]
    bfs_df = pd.DataFrame([
        {"session_id": "s1", "email": "b@example.test", "phone_number": "100", "searched_keywords": "pump", "searched_result": "x", "action_taken": "viewed"},
        {"session_id": "s2", "email": "b@example.test", "phone_number": "100", "searched_keywords": "wire", "searched_result": "y", "action_taken": "viewed"},
    ])
    service._dump_bfs_search_df_to_db(bfs_df, bfs_db, value_date)
    assert bfs_db.rollback.call_count == 1


def test_analytics_aggregate_error_and_commit_paths():
    service = analytics_service()
    target = date(2024, 1, 1)

    buyer_query = ChainQuery(all_values=[_buyer_metric()], first_values=[RuntimeError("buyer aggregate") ] + [None] * 20)
    buyer_db = FakeDB(buyer_query)
    buyer_db.commit.side_effect = RuntimeError("buyer aggregate commit")
    service._calculate_rfqs_with_response = MagicMock(return_value=1)
    service._calculate_total_rfq_responses = MagicMock(return_value=2)
    service.calculate_and_store_daily_aggregates(target, buyer_db)
    assert buyer_db.rollback.called

    seller_query = ChainQuery(all_values=[_seller_metric()], first_values=[RuntimeError("seller aggregate")] + [None] * 10)
    seller_db = FakeDB(seller_query)
    seller_db.commit.side_effect = RuntimeError("seller aggregate commit")
    service.calculate_and_store_seller_daily_aggregates(target, seller_db)
    assert seller_db.rollback.called

    unknown_query = ChainQuery(all_values=[_unknown_metric()], first_values=[RuntimeError("unknown aggregate")] + [None] * 10)
    unknown_db = FakeDB(unknown_query)
    unknown_db.commit.side_effect = RuntimeError("unknown aggregate commit")
    service.calculate_and_store_unknown_daily_aggregates(target, unknown_db)
    assert unknown_db.rollback.called


def test_analytics_category_and_pandas_boundaries(monkeypatch):
    service = analytics_service()
    target = date(2024, 1, 1)

    class RemoteDB:
        def __init__(self, rows):
            self.rows = rows
            self.close = MagicMock()
            self.execute = MagicMock(return_value=SimpleNamespace(fetchall=lambda: self.rows))

    rows = [
        (target, "Tools", 2, 1, 3, 2, 1, 0),
        (target, "Malformed", "not-an-int", 0, 0, 0, 0, 0),
    ]
    remote = RemoteDB(rows)
    monkeypatch.setattr(analytics_mod, "get_remote_db_session", lambda: remote)
    category_existing = SimpleNamespace()
    category_query = ChainQuery(first_values=[category_existing, None])
    db = FakeDB(category_query)
    db.execute = MagicMock(return_value=[("Tools", 4)])
    service._get_bfs_category_counts_combined = MagicMock(return_value=({"Tools": 5, "BFS Only": 2}, {"BFS Only": 1}))
    service.calculate_and_store_category_aggregates(target, db)
    assert category_existing.total_rfq_raised_category == 2
    assert db.commit.called

    failing_remote = RemoteDB([])
    failing_remote.execute.side_effect = RuntimeError("remote category down")
    monkeypatch.setattr(analytics_mod, "get_remote_db_session", lambda: failing_remote)
    service.calculate_and_store_category_aggregates(target, db)
    assert db.rollback.called

    buyer_frame = pd.DataFrame({"bfs_products_searched_list": [["pump", "wire"], "malformed", None]})
    unknown_frame = pd.DataFrame({"bfs_products_searched_by_unregistered": [["wire"], ["bolt"]]})
    monkeypatch.setattr(analytics_mod.pd, "read_sql", MagicMock(side_effect=[buyer_frame, unknown_frame]))
    mapping_remote = RemoteDB([])
    mapping_remote.execute.return_value = SimpleNamespace(fetchall=lambda: [("Tools", "pump"), ("Electrical", "wire"), ("Fasteners", "bolt")])
    monkeypatch.setattr(analytics_mod, "get_remote_db_session", lambda: mapping_remote)
    service._get_bfs_category_counts_combined = analytics_mod.ConversationAnalyticsService._get_bfs_category_counts_combined.__get__(service)
    counts = service._get_bfs_category_counts_combined(target, FakeDB(ChainQuery()))
    assert counts == ({"Tools": 1, "Electrical": 1}, {"Electrical": 1, "Fasteners": 1})
    assert service._count_categories_from_df(pd.DataFrame(), "missing", {}) == {}


def test_enhanced_constructor_and_vector_hierarchy_are_isolated(monkeypatch):
    settings = SimpleNamespace(chroma_host="mock-host", chroma_port=8000)
    client = MagicMock()
    client.get_or_create_collection.side_effect = [MagicMock(), MagicMock()]
    monkeypatch.setattr(enhanced_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(enhanced_mod.embedding_functions, "SentenceTransformerEmbeddingFunction", lambda **kwargs: "embedding")
    monkeypatch.setattr(enhanced_mod.chromadb, "HttpClient", lambda **kwargs: client)
    monkeypatch.setattr(enhanced_mod, "AutoCategorizationService", lambda: "fallback")
    monkeypatch.setattr(enhanced_mod, "OpenAIService", lambda: "openai")
    service = enhanced_mod.EnhancedAutoCategorizationService()
    assert service.embedding_function == "embedding"
    assert service.fallback_service == "fallback"
    assert service.openai_service == "openai"
    client.heartbeat.assert_called_once_with()

    vector_service = enhanced_service()
    metadata = {"level_1_category": "Industrial", "level_2_category": "Fasteners", "level_3_category": "Fasteners", "client_category_name": "Tools"}
    vector_service.collection.query.return_value = {
        "documents": [["bolt"]],
        "metadatas": [[metadata]],
        "distances": [[0.1]],
    }
    match = vector_service._search_hierarchical_levels("bolt", similarity_threshold=0.75)
    assert match["success"] is True
    assert match["matched_level"] == "level_2"


def test_enhanced_keyword_sliding_window_and_cross_validation(monkeypatch):
    service = enhanced_service()

    def remote_query(query, params):
        if "SELECT item, category" in query:
            return []
        phrase = params.get("q")
        if phrase == "%red power cable%":
            return []
        if phrase == "%power cable%":
            return [{"category": "Electrical", "freq": 2}]
        return []

    monkeypatch.setattr(enhanced_mod, "execute_remote_query", remote_query)
    keyword = service._keyword_lookup_source_of_truth("red power cable")
    assert keyword["success"] is True
    assert keyword["category"] == "Electrical"
    assert keyword["match_source"] == "category"

    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": True, "category": "Keyword", "consensus": 0.2})
    service.fallback_service.collection.query.return_value = {
        "metadatas": [[{"category": ""}, {"category": "Fallback"}]],
        "distances": [[0.8, 0.1]],
    }
    result = service._cross_validate_with_fallback("bolt", "Learning", 0.2)
    assert result["recommended_category"] == "Fallback"

    service.fallback_service.collection.query.return_value = {
        "metadatas": [[{"category": ""}]],
        "distances": [[0.1]],
    }
    no_categories = service._cross_validate_with_fallback("bolt", "Learning", 0.2)
    assert no_categories["use_learning"] is True


def test_enhanced_build_description_learning_update_and_health_error(monkeypatch):
    service = enhanced_service()
    assert service._build_enhanced_description(
        "Battery", {"level_3_category": "Lithium Battery", "level_2_category": "Power"}
    ) == "Battery Lithium Battery Power"

    learning = SimpleNamespace(create_3_level_category=AsyncMock(return_value={"success": True}))
    monkeypatch.setattr(learning_mod, "LearningCategorizationService", lambda: learning)
    assert asyncio_run(service._update_learning_taxonomy("Battery", "Power", "u1")) is True
    learning.create_3_level_category.return_value = {"success": False, "error": "rejected"}
    assert asyncio_run(service._update_learning_taxonomy("Battery", "Power", "u1")) is False
    learning.create_3_level_category.side_effect = RuntimeError("learning down")
    assert asyncio_run(service._update_learning_taxonomy("Battery", "Power", "u1")) is False

    class BrokenPathService(enhanced_mod.EnhancedAutoCategorizationService):
        @property
        def chroma_path(self):
            raise RuntimeError("path unavailable")

    broken = object.__new__(BrokenPathService)
    broken.collection = MagicMock()
    broken.collection.count.return_value = 1
    broken.fallback_service = SimpleNamespace(get_collection_stats=MagicMock(return_value={}))
    health = broken.health_check()
    assert health == {"overall_status": "unhealthy", "error": "path unavailable"}

    service.collection.count.side_effect = RuntimeError("chroma down")
    assert service.get_stats() == {"error": "chroma down"}


@pytest.mark.asyncio
async def test_enhanced_taxonomy_empty_candidate_falls_back_and_logs_learning_levels(monkeypatch):
    service = enhanced_service()
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._search_hierarchical_levels = MagicMock(return_value={
        "success": True,
        "similarity_score": 0.8,
        "best_match": {"client_category_name": "Other"},
        "all_level_matches": [{
            "metadata": {"item_description": "unknown", "client_category_name": "Other"},
            "similarity_score": 0.8,
            "matched_level": "level_3",
        }],
    })
    service.fallback_service._get_similar_items.return_value = [{"item": "unknown", "category": "Tools", "similarity_score": 0.7}]
    service.openai_service.categorize_with_similar_items.return_value = {
        "success": True,
        "category": "Tools",
        "confidence": 0.81,
        "reasoning": "fallback selection",
    }
    service._update_learning_taxonomy = AsyncMock(return_value=True)
    service._log_fallback_categorization = MagicMock()
    result = await service.categorize_item("unknown", "u1")
    assert result["method"] == "enhanced_fallback_openai"
    assert result["learning_updated"] is True
    service._log_fallback_categorization.assert_called_once()

    db = SimpleNamespace(add=MagicMock(), commit=MagicMock(), rollback=MagicMock(), close=MagicMock())
    monkeypatch.setattr(enhanced_mod, "get_db_session", lambda: db)
    service._log_fallback_categorization = enhanced_mod.EnhancedAutoCategorizationService._log_fallback_categorization.__get__(service)
    base_match = {
        "learning_item_id": "item-1",
        "category_path": "A/B/C",
        "learning_confidence": 0.8,
        "level_1_category": "A",
        "level_2_category": "B",
        "level_3_category": "C",
    }
    for level in ("level_1", "level_2", "level_3", "unknown"):
        match = dict(base_match, match_level=level)
        service._log_categorization("unknown", "u1", "s1", "r1", "Tools", 0.8, 0.7, "method", 3, match)
    assert db.add.call_count == 4
    entries = [call.args[0] for call in db.add.call_args_list]
    assert entries[0].learning_level_1 == "A" and entries[0].learning_level_2 is None
    assert entries[1].learning_level_1 is None and entries[1].learning_level_2 == "B"
    assert entries[2].learning_level_3 == "C"
    assert entries[3].learning_level_1 is None and entries[3].learning_level_2 is None and entries[3].learning_level_3 is None

    db.add.side_effect = RuntimeError("log add")
    service._log_categorization("unknown", "u1", None, None, "Tools", 0.8, None, "method", 3, base_match)
    db.add.side_effect = None
    db.commit.side_effect = RuntimeError("fallback commit")
    service._log_fallback_categorization("unknown", "u1", None, None, "Other", 0.3, "fallback", 1, "no match")
    assert db.rollback.call_count == 2
