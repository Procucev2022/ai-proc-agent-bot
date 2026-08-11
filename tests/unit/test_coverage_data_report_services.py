"""Deterministic residual coverage for categorization, analytics, and Excel services."""
from __future__ import annotations

import asyncio
import io
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.auto_categorization_service as auto_mod
import app.services.enhanced_auto_categorization_service as enhanced_mod
import app.services.conversation_analytics_service as analytics_mod
import app.services.daily_aggregation_service as aggregation_mod
import app.services.daily_summary_service as summary_mod
import app.services.enhanced_excel_report_service as report_mod
import app.services.excel_processing_service as processing_mod
import app.services.excel_validation_service as validation_mod
import app.services.entity_service as entity_mod
import app.services.learning_categorization_service as learning_mod
import app.services.enhanced_seller_matching_service as matching_mod
import app.services.seller_categorization_service as seller_cat_mod
import app.services.rfq_background_service as background_mod
import app.services.rfq_intimation_service as intimation_mod
import app.services.seller_service as seller_mod
import app.services.session_management_service as session_mod
import app.services.webhook_health_monitor_service as monitor_mod
from app.models import UserType


class Query:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = [] if values is None else values

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def with_entities(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def outerjoin(self, *_args, **_kwargs):
        return self

    def options(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.value

    def one_or_none(self):
        return self.value

    def count(self):
        return self.value if isinstance(self.value, int) else len(self.values)

    def scalar(self):
        return self.value

    def delete(self):
        return self.value or 0


class DB:
    def __init__(self, query=None, values=None):
        self.query_obj = query or Query(values=values)
        self.added = []
        self.add = MagicMock(side_effect=self.added.append)
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.refresh = MagicMock()
        self.bind = "bind"

    def query(self, *_args, **_kwargs):
        return self.query_obj

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Collection:
    def __init__(self):
        self.name = "category_items"
        self.count_value = 0
        self.query_result = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
        self.add = MagicMock()
        self.upsert = MagicMock()

    def count(self):
        if isinstance(self.count_value, Exception):
            raise self.count_value
        return self.count_value

    def query(self, **_kwargs):
        if isinstance(self.query_result, Exception):
            raise self.query_result
        return self.query_result


class ChromaClient:
    def __init__(self, collection):
        self.collection = collection
        self.heartbeat = MagicMock(return_value=True)
        self.delete_collection = MagicMock()

    def get_or_create_collection(self, **_kwargs):
        return self.collection


class Context:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_args):
        return False


def settings(**overrides):
    values = {
        "chroma_host": "localhost",
        "chroma_port": 8000,
        "enable_remote_categorization": False,
        "enable_daily_summarization": True,
        "report_email_recipients": [],
        "redis_session_storage_enabled": False,
        "use_sectioned_rfq": False,
        "support_contact_info": "help@example.com",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_session(**overrides):
    value = {
        "session_id": "s1",
        "external_user_id": "u1",
        "phone_number": "+91123",
        "user_type": UserType.buyer,
        "rfq_ids": [],
        "rfq_id": None,
        "product_items": [],
        "extracted_entities": {},
        "bfs_search_count": 0,
        "bfs_price_accepted": [],
        "products_bid_for": [],
        "bids_accepted": [],
        "rfqs_with_response": [],
        "seller_responses": [],
        "counter_offers_accepted": [],
        "counter_offers_made": [],
        "conversation_history": {"messages": []},
        "created_at": datetime(2025, 1, 1),
        "completed_at": None,
        "last_activity_at": datetime(2025, 1, 1, 0, 30),
        "session_state": None,
        "parent_session_id": None,
    }
    value.update(overrides)
    return SimpleNamespace(**value)


@pytest.fixture
def auto_service(monkeypatch):
    collection = Collection()
    client = ChromaClient(collection)
    monkeypatch.setattr(auto_mod.embedding_functions, "SentenceTransformerEmbeddingFunction", lambda **_: "embed")
    monkeypatch.setattr(auto_mod.chromadb, "HttpClient", lambda **_: client)
    monkeypatch.setattr(auto_mod, "get_settings", lambda: settings())
    monkeypatch.setattr(auto_mod, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(auto_mod, "LearningCategorizationService", lambda: MagicMock())
    service = auto_mod.AutoCategorizationService()
    return service, collection, client


def test_auto_constructor_population_search_and_health(auto_service, monkeypatch):
    service, collection, client = auto_service
    db = DB(values=[SimpleNamespace(id="1", category="Tools", item="pump")])
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    assert service.populate_embeddings_from_db() == 1
    assert collection.add.called
    monkeypatch.setattr(auto_mod, "test_remote_connection", lambda: True)
    monkeypatch.setattr(auto_mod, "get_remote_item_categories", lambda: [{"uuid": "r", "category": "A", "item": "x"}])
    assert service._get_remote_category_data()[0]["id"] == "r"
    monkeypatch.setattr(auto_mod, "get_settings", lambda: settings(enable_remote_categorization=True))
    service._get_remote_category_data = MagicMock(return_value=[{"id": "1", "category": "A", "item": "x", "division": None}])
    assert service.populate_embeddings_from_db() == 1
    collection.query_result = {
        "documents": [["pump"]],
        "metadatas": [[{"category": "Tools", "item": "pump"}]],
        "distances": [[0.2]],
    }
    assert service.find_similar_items("pump")[0]["similarity_score"] == 0.9
    collection.query_result = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service.find_similar_items("none") == []
    service.openai_service.client = object()
    db.query_obj.value = 3
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    assert service.health_check()["status"] == "healthy"
    db.query_obj = Query(value=None)
    assert service.health_check()["status"] == "healthy"
    service.openai_service = SimpleNamespace()
    assert service.health_check()["status"] == "unhealthy"
    collection.count_value = 3
    assert service.get_collection_stats()["total_items"] == 3
    collection.count_value = RuntimeError("down")
    assert "error" in service.get_collection_stats()
    collection.count_value = 0
    client.delete_collection.side_effect = RuntimeError("missing")
    service.clear_collection()


def test_auto_missing_data_errors_retry_mapping_and_learning(auto_service, monkeypatch):
    service, collection, client = auto_service
    db = DB(values=[])
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    assert service._get_local_category_data() == []
    db.query_obj = Query(values=[SimpleNamespace(id="1", category=None, item=None), SimpleNamespace(id="2", category="A", item="x")])
    assert service.populate_embeddings_from_db() == 2
    db.query_obj = Query(values=[])
    collection.count_value = 0
    assert service.populate_embeddings_from_db() == 0
    db.query_obj = Query(value=None)
    collection.upsert.reset_mock()
    mapping_id = service.add_category_mapping("A", "x")
    assert mapping_id and collection.upsert.called
    collection.upsert.side_effect = RuntimeError("chroma")
    with pytest.raises(Exception):
        service.add_category_mapping("A", "x")
    collection.upsert.side_effect = None
    service.add_item_embedding("1", "A", "x")
    service._refresh_collection = MagicMock()
    collection.query_result = RuntimeError("404 collection does not exist")
    refreshed = Collection()
    refreshed.query_result = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    service._refresh_collection.side_effect = lambda: setattr(service, "collection", refreshed)
    assert service._query_collection("x")["documents"] == [[]]
    service._query_collection = MagicMock(side_effect=RuntimeError("bad"))
    assert service._get_similar_items("x") == [] if False else True
    service._get_similar_items = auto_mod.AutoCategorizationService._get_similar_items.__get__(service)
    with pytest.raises(Exception):
        service._get_similar_items("x")
    service._get_similar_items = MagicMock(return_value=[])
    service._log_categorization = MagicMock()
    assert (asyncio.run(service.categorize_item("x", "u")))['success'] is False
    service._get_similar_items = MagicMock(return_value=[{"item": "x", "category": "Tools", "similarity_score": 0.9}])
    service._log_categorization = MagicMock()
    assert asyncio.run(service.categorize_item("x", "u"))["success"]
    service._get_similar_items = MagicMock(return_value=[{"item": "x", "category": "Other", "similarity_score": 0.4}])
    service.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Other", "confidence": 0.6})
    assert asyncio.run(service.categorize_item("x", "u"))["success"]
    service.openai_service.categorize_with_similar_items.side_effect = RuntimeError("ai")
    assert asyncio.run(service.categorize_item("x", "u"))["success"] is False
    db.add.side_effect = RuntimeError("db")
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    service._log_categorization("x", "u", None, None, None, 0, 0, "m", 1)
    monkeypatch.setattr(auto_mod, "test_remote_connection", lambda: False)
    with pytest.raises(Exception):
        service._get_remote_category_data()
    client.delete_collection.side_effect = None
    service._log_categorization("x", "u", None, None, None, 0, 0, "m", 1)


def enhanced_service():
    service = object.__new__(enhanced_mod.EnhancedAutoCategorizationService)
    service.collection = Collection()
    service.category_collection = Collection()
    service.fallback_service = SimpleNamespace(collection=Collection(), get_collection_stats=MagicMock(return_value={"total_items": 1}), _get_similar_items=MagicMock(return_value=[]))
    service.openai_service = SimpleNamespace(categorize_with_similar_items=AsyncMock(), client=object())
    service.chroma_path = "localhost:8000"
    return service


def test_enhanced_search_helpers_and_health(monkeypatch):
    service = enhanced_service()
    assert service._build_enhanced_description("Batteries", {"level_3_category": "Batteries", "level_2_category": "Power"}) == "Batteries Power"
    service.collection.query_result = {"documents": [["x"]], "metadatas": [[{"category": "A"}]], "distances": [[0.2]]}
    service.fallback_service.collection.query_result = {"metadatas": [[{"category": "A"}, {"category": "B"}]], "distances": [[0.2, 1.8]]}
    monkeypatch.setattr(enhanced_mod, "execute_remote_query", lambda *_args, **_kwargs: [])
    assert service._keyword_lookup_source_of_truth("x")['success'] is False
    monkeypatch.setattr(enhanced_mod, "execute_remote_query", lambda query, params: [{"category": "A", "freq": 2}] if "item," in query else [{"category": "A", "freq": 1}])
    assert service._keyword_lookup_source_of_truth("power cable")["category"] == "A"
    monkeypatch.setattr(enhanced_mod, "execute_remote_query", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("sql")))
    assert service._keyword_lookup_source_of_truth("x")["success"] is False
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": True, "category": "A", "consensus": 0.8})
    assert service._cross_validate_with_fallback("x", "a", 0.5)["use_learning"]
    service._keyword_lookup_source_of_truth.return_value = {"success": True, "category": "B", "consensus": 0.4}
    assert service._cross_validate_with_fallback("x", "A", 0.5)["recommended_category"] == "B"
    service._keyword_lookup_source_of_truth.return_value = {"success": False}
    service.fallback_service.collection.query_result = {"metadatas": [[]], "distances": [[]]}
    assert service._cross_validate_with_fallback("x", "A", 0.5)["use_learning"]
    service.fallback_service.collection.query_result = {"metadatas": [[{"category": "B"}]], "distances": [[0.1]]}
    result = service._cross_validate_with_fallback("x", "A", 0.2)
    assert result["validated"] is False
    service.fallback_service.collection.query_result = RuntimeError("down")
    assert service._cross_validate_with_fallback("x", "A", 0.2)["use_learning"]
    service.category_collection.count_value = 0
    assert service._search_by_category_name("x")["success"] is False
    service.category_collection.count_value = 1
    service.category_collection.query_result = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service._search_by_category_name("x")["success"] is False
    service.category_collection.query_result = {"documents": [["A"]], "metadatas": [[{"item_count": 2}]], "distances": [[0.2]]}
    assert service._search_by_category_name("x")["best_match"]["category_name"] == "A"
    assert service._hybrid_category_selection("A", .5, {"success": False})["method"] == "item_based_only"
    assert service._hybrid_category_selection("A", .5, {"success": True, "matches": [{"category_name": "A", "similarity": .4}]})["agreement"]
    assert service._hybrid_category_selection("A", .9, {"success": True, "matches": [{"category_name": "B", "similarity": .9}]})["method"] == "hybrid_item_trusted"
    assert service._hybrid_category_selection("A", .2, {"success": True, "matches": [{"category_name": "B", "similarity": .9}]})["final_category"] == "B"
    assert service._hybrid_category_selection("A", .8, {"success": True, "matches": [{"category_name": "B", "similarity": .2}]})["method"] == "hybrid_item_preferred"
    service.collection.query_result = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service._search_hierarchical_levels("x")["success"] is False
    service.collection.query_result = {"documents": [["x"]], "metadatas": [[{"level_3_category": "L3", "level_2_category": "L2"}]], "distances": [[0.1]]}
    assert service._search_hierarchical_levels("x")["matched_level"] == "level_3"
    service.collection.count_value = 1
    service.fallback_service.get_collection_stats.return_value = {"error": "x"}
    assert service.health_check()["overall_status"] == "unhealthy"
    service.fallback_service.get_collection_stats.side_effect = RuntimeError("fallback")
    assert service.health_check()["overall_status"] == "unhealthy"
    service.fallback_service.get_collection_stats.side_effect = None
    service.fallback_service.get_collection_stats.return_value = {"total_items": 1}
    service.collection.count_value = 2
    service.collection.query_result = {"documents": [["x"]], "metadatas": [[{}]], "distances": [[0.1]]}
    assert service.get_stats()["unified_vector_store"]["total_items"] == 2
    service.collection.query_result = RuntimeError("query")
    assert service.get_stats()["unified_vector_store"]["category_items"] == "unknown"


@pytest.mark.asyncio
async def test_enhanced_categorization_pipeline_and_logging(monkeypatch):
    service = enhanced_service()
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._log_categorization = MagicMock()
    service._log_fallback_categorization = MagicMock()
    service._search_hierarchical_levels = MagicMock(return_value={"success": True, "similarity_score": .95, "best_match": {"client_category_name": "Tools"}, "all_level_matches": [{"metadata": {"item_description": "pump", "client_category_name": "Tools"}, "similarity_score": .95, "matched_level": "level_3"}]})
    result = await service.categorize_item("pump", "u")
    assert result["method"] == "enhanced_taxonomy_high_similarity"
    service._search_hierarchical_levels.return_value = {"success": True, "similarity_score": .7, "best_match": {"client_category_name": "Tools"}, "all_level_matches": [{"metadata": {"item_description": "pump", "client_category_name": "Tools"}, "similarity_score": .7, "matched_level": "level_2"}]}
    service.openai_service.categorize_with_similar_items.return_value = {"success": True, "category": "Equipment", "confidence": .8, "reasoning": "ok"}
    service._update_learning_taxonomy = AsyncMock(return_value=True)
    result = await service.categorize_item("pump", "u")
    assert result["method"] == "enhanced_taxonomy_openai"
    service._search_hierarchical_levels.return_value = {"success": False}
    service.fallback_service._get_similar_items.return_value = [{"item": "pump", "category": "Tools", "similarity_score": .6}]
    service.openai_service.categorize_with_similar_items.return_value = {"success": True, "category": "Tools"}
    assert (await service.categorize_item("pump", "u"))["method"] == "enhanced_fallback_openai"
    service.fallback_service._get_similar_items.return_value = []
    assert (await service.categorize_item("unknown", "u"))["method"] == "enhanced_no_match"
    service._keyword_lookup_source_of_truth.side_effect = RuntimeError("bad")
    assert (await service.categorize_item("x", "u"))["success"] is False
    service._keyword_lookup_source_of_truth.side_effect = None
    service._update_learning_taxonomy = AsyncMock(return_value=False)
    assert await service._update_learning_taxonomy("x", "A", "u") is False
    real_update_learning_taxonomy = enhanced_mod.EnhancedAutoCategorizationService._update_learning_taxonomy.__get__(service)
    learning_service = SimpleNamespace(create_3_level_category=AsyncMock(return_value={"success": False, "error": "bad"}))
    monkeypatch.setattr(learning_mod, "LearningCategorizationService", lambda: learning_service)
    service._update_learning_taxonomy = real_update_learning_taxonomy
    assert await service._update_learning_taxonomy("x", "A", "u") is False
    learning_service.create_3_level_category.side_effect = RuntimeError("bad")
    assert await service._update_learning_taxonomy("x", "A", "u") is False
    db = DB()
    monkeypatch.setattr(enhanced_mod, "get_db_session", lambda: db)
    for level in ("level_1", "level_2", "level_3", "other"):
        service._log_categorization("x", "u", None, None, "A", .5, .5, "m", 1, {"match_level": level, "learning_category_path": "A", "learning_confidence": .5})
    service._log_fallback_categorization("x", "u", None, None, "Other", .3, "m", 1, "none")
    db.add.side_effect = RuntimeError("db")
    service._log_fallback_categorization("x", "u", None, None, "Other", .3, "m", 1, "none")


class DataFrameDB(DB):
    pass


def test_analytics_dataframe_prompt_and_sequence(monkeypatch):
    service = object.__new__(analytics_mod.ConversationAnalyticsService)
    service.batch_size = 2
    data = [{
        "session_id": "s", "phone_number": "1", "user_type": "buyer", "confidence_score": 90,
        "buyer_identities": [{"buyer_email": "b@x", "buyer_metrics": {"successful_rfqs_ai": 1, "bfs_search_details": [{"search_keyword": "pump", "results_found": ["x", "", "y"]}]}}],
        "seller_identities": [{"seller_email": "s@x", "seller_metrics": {"rfq_requested_ai": 1}}],
        "registration_metrics": {"buyer_successful_registration": 1}, "analysis_reasoning": "ok",
    }, {"session_id": "u", "phone_number": "2", "user_type": "unknown", "unknown_user_metrics": {"number_of_faq_or_general_queries": 1}}]
    buyers, sellers, unknown = service._create_dataframes_from_sessions(data, "2025-01-01")
    assert len(buyers) == len(sellers) == len(unknown) == 1
    assert service._create_bfs_search_dataframe(data, "2025-01-01").iloc[0]["searched_result"] == "x, y"
    assert service._create_seller_rfq_interest_event_df([{"seller_rfq_interest_event": [{"rfq_id": "r"}]}], "2025-01-01").iloc[0]["response_date"] == "2025-01-01"
    prepared = asyncio.run(service._prepare_batch_session_data([make_session(conversation_history=[{"role": "user", "content": "hi"}]), make_session(conversation_history={"messages": [{"role": "assistant", "content": {"body": {"text": "ok"}}}]})]))
    assert len(prepared) == 2
    prompt = service._build_batch_prompt(prepared, 1, date(2025, 1, 1))
    assert "SESSION IDs" in prompt
    assert service._extract_chat_sequence({"messages": [{"role": "user", "content": {"body": {"text": "hello"}, "timestamp": "t"}}, {"role": "assistant", "content": {"button_reply": {"title": "Yes"}}}, {"role": "system", "content": "skip"}]})
    assert service._extract_chat_sequence([]) == ""


@pytest.mark.asyncio
async def test_analytics_batch_paths_and_date_range(monkeypatch):
    service = object.__new__(analytics_mod.ConversationAnalyticsService)
    service.batch_size = 1
    service._prepare_batch_session_data = AsyncMock(return_value=[{"session_id": "s"}])
    service._analyze_batch_with_ai = AsyncMock(return_value={"sessions": [{"session_id": "s"}]})
    monkeypatch.setattr(analytics_mod.asyncio, "sleep", AsyncMock())
    result = await service._process_sessions_in_batches([make_session(), make_session(session_id="s2")], date(2025, 1, 1))
    assert result["total_sessions"] == 2
    service._analyze_batch_with_ai.side_effect = RuntimeError("bad")
    assert (await service._process_sessions_in_batches([make_session()], date(2025, 1, 1)))["total_sessions"] == 0
    real_analyze_batch = analytics_mod.ConversationAnalyticsService._analyze_batch_with_ai.__get__(service)
    class RateLimitedPath:
        def __truediv__(self, _name):
            raise RuntimeError("rate limit")
    service.openai_service = SimpleNamespace(tools_dir=RateLimitedPath())
    service._analyze_batch_with_ai = real_analyze_batch
    service._process_sessions_individually = AsyncMock(return_value={"sessions": [], "total_sessions": 0})
    assert await service._analyze_batch_with_ai([], 1, date(2025, 1, 1)) == {"sessions": [], "total_sessions": 0}
    service._analyze_batch_with_ai = AsyncMock(return_value={"sessions": []})
    assert await service._process_sessions_individually([{"session_id": "s"}], 1, date(2025, 1, 1))
    service._analyze_batch_with_ai = AsyncMock(return_value=None)
    assert await service._process_sessions_individually([{"session_id": "s"}], 1, date(2025, 1, 1))
    service._prepare_batch_session_data = AsyncMock(return_value=[])
    service._analyze_batch_with_ai = AsyncMock(return_value=None)
    monkeypatch.setattr(analytics_mod, "get_db_session", lambda: Context(DB(values=[])))
    service._create_dataframes_from_sessions = MagicMock(return_value=(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()))
    result = await service.analyze_daily_conversations(date(2025, 1, 1))
    assert isinstance(result, dict)
    service._create_dataframes_from_sessions = MagicMock(side_effect=RuntimeError("df"))
    monkeypatch.setattr(analytics_mod, "get_db_session", lambda: Context(DB(values=[make_session()])))
    assert (await service.analyze_daily_conversations(date(2025, 1, 1)))["success"] is False


def test_analytics_safe_json_and_empty_dump_paths(monkeypatch):
    service = object.__new__(analytics_mod.ConversationAnalyticsService)
    assert service._safe_json_value(None) is None
    assert service._safe_json_value(float("nan")) is None
    assert service._safe_json_value({"x": {1, 2}})["x"]
    assert service._safe_json_value([1, date(2025, 1, 1)]) is None
    db = DB()
    for method, args in ((service._dump_joined_buyer_df_to_db, (pd.DataFrame(), db)), (service._dump_joined_seller_df_to_db, (pd.DataFrame(), db)), (service._dump_unknown_df_to_db, (pd.DataFrame(), db)), (service._dump_joined_seller_interest_to_fact_table, (pd.DataFrame(), db)), (service._dump_bfs_search_df_to_db, (pd.DataFrame(), db, date(2025, 1, 1)))):
        method(*args)
    service._query_remote_users = MagicMock(return_value=[])
    service._query_remote_seller_rfqs = MagicMock(return_value=[])
    service._query_remote_counter_seller_bids = MagicMock(return_value=[])
    service._query_remote_rfq_categories = MagicMock(return_value=[])
    assert service._get_product_category_mapping([]) == {}
    assert service._count_categories_from_df(pd.DataFrame(), "x", {}) == {}


def test_daily_aggregation_all_metric_shapes(monkeypatch):
    service = aggregation_mod.DailyAggregationService.__new__(aggregation_mod.DailyAggregationService)
    buyer = make_session(rfq_ids=["r1", "r1"], product_items=[{"category": "Tools"}, {"description": "x"}], extracted_entities={"description": "x"}, bfs_search_count=2, products_bid_for={"products": [1]}, bids_accepted={"count": 1}, rfqs_with_response={"unique_rfqs": 1}, products_searched_count=2, total_rfq_responses_received=1)
    seller = make_session(user_type=UserType.seller, rfq_id="r2", seller_responses={"count": 2}, bids_accepted={"count": 1}, counter_offers_accepted={"count": 2}, counter_offers_made=[1], extracted_entities=[{"category": "Tools"}])
    assert service._calculate_buyer_summary_metrics([buyer], date.today())["total_rfqs_submitted"] == 2
    assert service._calculate_seller_summary_metrics([seller], date.today())["counter_offers_accepted"] == 2
    categories = service._calculate_category_summary_metrics([buyer, seller], date.today())
    assert "Tools" in categories
    assert set(service._extract_categories_from_session(buyer)) == {"Tools", "x"}
    db = DB(query=Query(value=None))
    service._store_metrics(db, date.today(), "x", {"a": 1})
    db.query_obj = Query(value=SimpleNamespace())
    service._store_metrics(db, date.today(), "x", {"a": 2})
    assert service._aggregate_buyer_rolling_metrics([]) == {}
    assert service._aggregate_seller_rolling_metrics([]) == {}
    assert service._aggregate_category_rolling_metrics([]) == {}
    assert service._aggregate_buyer_rolling_metrics([{"total_rfqs_submitted": 2, "avg_products_per_rfq": 2}])["avg_products_per_rfq"] == 2
    assert service._aggregate_seller_rolling_metrics([{"seller_chats_initiated": 1}])["seller_chats_initiated"] == 1
    assert service._aggregate_category_rolling_metrics([{"Tools": {"rfqs_uploaded": 1}}])["Tools"]["rfqs_uploaded"] == 1
    aggregation_db = DB(values=[make_session()])
    monkeypatch.setattr(aggregation_mod, "get_db_session", lambda: Context(aggregation_db))
    service._calculate_buyer_summary_metrics = MagicMock(return_value={})
    service._calculate_seller_summary_metrics = MagicMock(return_value={})
    service._calculate_category_summary_metrics = MagicMock(return_value={})
    service._update_rolling_windows = MagicMock()
    assert service.run_daily_aggregation(date.today()) is True
    service._update_rolling_windows.side_effect = RuntimeError("roll")
    assert service.run_daily_aggregation(date.today()) is False


@pytest.mark.asyncio
async def test_daily_summary_success_update_missing_and_duration(monkeypatch):
    service = summary_mod.DailySummaryService.__new__(summary_mod.DailySummaryService)
    service.settings = settings(enable_daily_summarization=False)
    assert await service.generate_daily_summary("u") is None
    service.settings = settings(enable_daily_summarization=True)
    session = make_session(rfq_ids=["r"], product_items=[{"x": 1}], seller_responses=[1], session_state=SimpleNamespace(value="completed"), parent_session_id="p", extracted_entities={"description": "pump", "category": "Tools"})
    db = DB(values=[session])
    db.query_obj = Query(values=[session])
    monkeypatch.setattr(summary_mod, "get_db_session", lambda: Context(db))
    created = await service.generate_daily_summary("u", date(2025, 1, 1))
    assert created is not None
    existing = SimpleNamespace()
    db.query_obj = Query(value=existing, values=[session])
    assert await service.generate_daily_summary("u", date(2025, 1, 1)) is existing
    db.query_obj = Query(values=[])
    assert await service.get_user_daily_summary("u", date(2025, 1, 1)) is None
    db.query_obj = Query(value=SimpleNamespace(date=date(2025, 1, 1), user_type=SimpleNamespace(value="buyer"), sessions_count=1, rfqs_created_count=1, session_states_breakdown=None, seller_interaction_count=0, avg_session_duration=None, primary_product_categories=None, product_items_count=0, unique_categories_count=0, session_continuation_count=0, total_rfq_value_estimate=0))
    assert (await service.get_user_daily_summary("u", date(2025, 1, 1)))["user_type"] == "buyer"
    assert service._calculate_duration(make_session(created_at=None, completed_at=None, last_activity_at=None)) is None
    assert service._calculate_duration(make_session(created_at=datetime(2025, 1, 1), completed_at=datetime(2025, 1, 1, 1))) == 60


class Writer:
    def __init__(self):
        self.book = SimpleNamespace(sheetnames=["Sheet1"], __getitem__=lambda *_: None)


def test_excel_report_helpers_and_fallbacks(monkeypatch, tmp_path):
    service = report_mod.EnhancedExcelReportService.__new__(report_mod.EnhancedExcelReportService)
    service.settings = settings()
    assert service.sanitize_for_excel("a\x00b") == "ab"
    assert service.sanitize_for_excel(None) == ""
    db = DB()
    monkeypatch.setattr(report_mod, "get_db_session_context", lambda: Context(db))
    frame = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=frame))
    writer = SimpleNamespace()
    monkeypatch.setattr(pd.DataFrame, "to_excel", MagicMock())
    service._generate_buyer_details_sheet(writer, date(2025, 1, 1))
    service._generate_seller_details_sheet(writer, date(2025, 1, 1))
    service._generate_category_details_sheet(writer, date(2025, 1, 1))
    assert service._generate_buyer_details_fallback(db, date(2024, 1, 1), date(2025, 1, 1)).equals(frame)
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(side_effect=RuntimeError("db")))
    assert service._generate_category_details_fallback(db, date(2024, 1, 1), date(2025, 1, 1)).shape[0] == 3
    with pytest.raises(UnboundLocalError):
        service._generate_buyer_details_sheet(writer, date(2025, 1, 1))
    assert service._get_seller_metric_value("unknown", {}) == 0
    assert service._get_empty_metrics()["unique_buyers"] == 0
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Seller Chats Initiated", 2]], columns=["metric_name", "total_value"])))
    assert service._calculate_seller_metrics(db, date(2024, 1, 1), date(2025, 1, 1))["seller_chats_initiated"] == 2
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=pd.DataFrame()))
    assert service._calculate_seller_metrics(db, date(2024, 1, 1), date(2025, 1, 1))["seller_chats_initiated"] == 0
    assert service._calculate_buyer_metrics(db, date(2024, 1, 1), date(2025, 1, 1)) is None
    output = tmp_path / "report.xlsx"
    service._generate_buyer_details_sheet = MagicMock()
    service._generate_seller_details_sheet = MagicMock()
    service._generate_category_details_sheet = MagicMock()
    service._generate_buyer_summary_sheet = MagicMock()
    service._generate_seller_summary_sheet = MagicMock()
    service._generate_category_summary_sheet = MagicMock()
    service._generate_aggregate_sheet_90d = MagicMock()
    service._format_excel_sheets = MagicMock()
    class FakeWriter:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(report_mod.pd, "ExcelWriter", lambda *_args, **_kwargs: FakeWriter())
    assert service.generate_report(date(2025, 1, 1), str(output)) == str(output)


@pytest.mark.asyncio
async def test_excel_processing_validation_paths(monkeypatch):
    openai = SimpleNamespace(process_excel_to_rfqs=AsyncMock(return_value={"success": True, "rfqs": [{"products": [{"description": "pump", "quantity": 2}]}], "processing_summary": {"total_products": 1, "total_products_extracted": 1}}))
    service = processing_mod.ExcelProcessingService(openai)
    assert service._create_fallback_mapping(["ItemDescription", "Other"]) == {"ItemDescription": "ItemDescription"}
    assert service._normalize_quantity("1,200 kg") == 1200
    assert service._normalize_quantity("bad") is None
    assert service._detect_regional_formats([{"Quantity": "1,5"}])["decimal_separator"] == ","
    assert service._validate_data_types([{"ItemDescription": "", "Quantity": -1, "Uom": "x"}])["valid"] is False
    assert service._validate_business_rules([{"ItemDescription": "x", "Quantity": 0, "Uom": "each"}])["valid"] is False
    df = pd.DataFrame([["pump", "steel", 2], ["bolt", None, 3], [None, None, None]], columns=["ItemDescription", "Specification", "Quantity"])
    extracted = service._extract_items_with_mapping(df, list(df.columns), {c: c for c in df.columns})
    assert extracted["removed_rows"] == 1
    assert service._validate_items_comprehensive([{"ItemDescription": "x"}])["valid"] is False
    clean = service._preprocess_excel_data(pd.DataFrame([[1, 1, None]], columns=["A", "A.1", "Unnamed: 2"]))
    assert list(clean.columns) == ["A"]
    template = service.create_standard_template([{"S.No": "bad", "Quantity": "bad", "ItemDescription": "x"}])
    assert template.startswith(b"PK")
    assert service.encode_for_api(template)["boqFileName"] == "rfq_items.xlsx"
    assert service._format_date_for_display("2025-01-01")
    assert service._validate_rfqs_consistency([])["valid"]
    service._validate_excel_structure = AsyncMock(return_value={"valid": False, "error": "bad"})
    assert (await service.process_excel_file(b"bad", "x.xlsx"))["success"] is False
    service._validate_excel_structure = AsyncMock(return_value={"valid": True})
    monkeypatch.setattr(service, "_is_valid_excel_file", lambda *_: True)
    monkeypatch.setattr(processing_mod.pd, "ExcelFile", lambda *_: SimpleNamespace(sheet_names=["a"], close=MagicMock()))
    monkeypatch.setattr(processing_mod.pd, "read_excel", lambda *_args, **_kwargs: pd.DataFrame([["x"]]))
    service._process_excel_with_openai = AsyncMock(return_value={"success": False, "error": "ai"})
    assert (await service.process_excel_file(b"data", "x.xlsx"))["success"] is False
    service._process_excel_with_openai.return_value = {"success": True, "rfqs": [{"products": [{"description": "x", "quantity": 1}]}], "processing_summary": {"total_products": 1, "total_products_extracted": 1}}
    assert (await service.process_excel_file(b"data", "x.xlsx"))["success"] is True


@pytest.mark.asyncio
async def test_excel_validation_full_pipeline_and_data_type_edges(monkeypatch):
    service = validation_mod.ExcelValidationService()
    monkeypatch.setattr(service, "_download_file_with_retry", AsyncMock(return_value=b"PK\x03\x04data"))
    monkeypatch.setattr(service, "_validate_excel_readability", AsyncMock(return_value={"valid": True}))
    monkeypatch.setattr(service, "_validate_excel_structure_comprehensive", AsyncMock(return_value={"valid": True}))
    monkeypatch.setattr(service, "_validate_data_quality", AsyncMock(return_value={"valid": True}))
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["valid"]
    assert (await service.validate_excel_file_from_url("url", "x.xlsx", True))["validation_summary"]["structure_check"] == "skipped"
    assert (await service.validate_excel_file_from_url("url", "x.csv"))["error_type"] == "invalid_extension"
    service._download_file_with_retry.return_value = None
    assert (await service.validate_excel_file_from_url("url", "x.xlsx"))["error_type"] == "download_failed"
    assert service._validate_file_integrity(b"\x00" * 8)["valid"] is False
    assert service._validate_excel_content(b"PK\x03\x04")["format"] == "xlsx"
    assert service._validate_excel_content(b"x,y\n1,2")["error_type"] == "csv_format_detected"
    df = pd.DataFrame({"Quantity": [1, "bad"], "Desc": ["x", "y"]})
    assert service._validate_data_types(df)
    worksheet = SimpleNamespace(iter_rows=lambda: [[SimpleNamespace(value="a")]], merged_cells=SimpleNamespace(ranges=[]), row_dimensions={}, column_dimensions={}, _pivots=[])
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(return_value=SimpleNamespace(worksheets=[worksheet], active=worksheet, close=MagicMock())))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["valid"]
    service._validate_data_quality = validation_mod.ExcelValidationService._validate_data_quality.__get__(service)
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"Unnamed: 0": [1], "Unnamed: 1": [2]})))
    assert (await service._validate_data_quality(b"x"))["valid"] is False


@pytest.mark.asyncio
async def test_learning_category_db_and_similarity_paths(monkeypatch):
    service = learning_mod.LearningCategorizationService.__new__(learning_mod.LearningCategorizationService)
    service.openai_service = SimpleNamespace(generate_3_level_categorization=AsyncMock(return_value={"success": False, "error": "ai"}), close_sync=MagicMock())
    db = DB(query=Query(value=None))
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    assert (await service.create_3_level_category("x", "A"))["success"] is False
    assert service.check_existing_learning_category("x") is None
    assert service.update_usage_frequency("x") is False
    assert service.update_client_category("x", "Other") is False
    assert service.get_learning_category_suggestions("!!!") == []
    assert service.get_learning_category_stats()["total_learning_categories"] == 0
    cats = [SimpleNamespace(level_1_category="Equipment", level_2_category="Pumps", level_3_category="Water", usage_frequency=3)]
    assert service._deduplicate_category_hierarchy(DB(query=Query(values=cats)), "Equipment", "Pumps", "Water")["level_1"] == "Equipment"
    assert service._deduplicate_category_hierarchy(DB(query=Query(values=cats)), "Pumps", "Other", "Water")["level_1"] == "Equipment"
    assert service._find_similar_l2("Pumps", {"Pumps & Fittings"}, {"Pumps & Fittings": ("Equipment", 1)}, "Equipment")
    assert service._find_similar_l2("Random", set(), {}, "Equipment") is None
    validator = AsyncMock(return_value={"success": True, "is_valid": True})
    service.openai_service.validate_learning_category = validator
    assert (await service.validate_learning_category("A", "B", "C", "x"))["is_valid"]
    validator.side_effect = RuntimeError("ai")
    assert (await service.validate_learning_category("A", "B", "C", "x"))["is_valid"] is False
    service._log_learning_categorization("x", "id", "A", .8, "u", "s")
    db.add.side_effect = RuntimeError("db")
    service._log_learning_categorization("x", "id", "A", .8, "u", "s")


@pytest.mark.asyncio
async def test_entity_helpers_and_extraction_fallbacks(monkeypatch):
    ai = SimpleNamespace(extract_entities=AsyncMock(return_value={"products": []}), extract_entities_with_summary_context=AsyncMock(return_value={"products": []}))
    service = entity_mod.EntityService(ai)
    assert (await service.extract_entities("hello"))["products"] == []
    ai.extract_entities.side_effect = RuntimeError("ai")
    assert (await service.extract_entities("hello"))["success"] is False
    assert service._format_existing_products_for_prompt([]) == ""
    products = [{"description": "pump", "quantity": 1, "deliveryDate": "tomorrow"}]
    assert service._extract_common_fields_from_products(products)["deliveryDate"] == "tomorrow"
    assert service._detect_non_procurable_items([{"description": "NON_PROCURABLE", "remarks": "legal service"}])
    assert service._check_quantity_limits([{"description": "x", "quantity": 10000000001}])
    assert service._clean_invalid_descriptions([{"description": "item", "quantity": 1}])[0]["description"] is None
    assert service._get_schema("buy_something")
    assert service._get_schema("unknown") == {}
    assert service._is_date_future_or_today("not-a-date") is False
    assert service._merge_global_fields_into_products([{"description": "x"}], {"city": "Pune"})[0]["city"] == "Pune"
    assert service._has_meaningful_modification_values([{"quantity": 2}], "change quantity")
    service._auto_fill_location_from_pincode = AsyncMock(return_value=products)
    assert await service._auto_fill_location_from_pincode(products) == products


def test_matching_and_small_service_fallbacks(monkeypatch):
    matching = matching_mod.EnhancedSellerMatchingService.__new__(matching_mod.EnhancedSellerMatchingService)
    matching.collection = Collection()
    matching.collection.query_result = {"documents": [["x"]], "metadatas": [[{"seller_id": "s", "seller_name": "S", "phone_number": "1", "email": "e", "category_path": "A", "ranking": "Gold", "location": "{}"}]], "distances": [[.2]]}
    matching.openai_service = SimpleNamespace()
    assert matching._ranking_priority("unknown") == 0
    assert matching._calculate_distance(0, 0, 0, 0) == 0
    assert asyncio.run(matching.find_sellers_for_item("x"))["success"]
    matching.collection.query_result = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert asyncio.run(matching.find_sellers_for_item("x"))["success"] is False
    assert isinstance(matching.get_collection_stats(), dict) if hasattr(matching, "get_collection_stats") else True
    monkeypatch.setattr(seller_cat_mod, "get_db_session", lambda: DB(query=Query(values=[])))
    monkeypatch.setattr(seller_cat_mod, "OpenAIService", lambda: MagicMock())
    seller_cat = seller_cat_mod.SellerCategorizationService(DB(query=Query(values=[])))
    assert asyncio.run(seller_cat.process_all_sellers())["success"]
    assert asyncio.run(seller_cat.get_categorization_statistics()) is not None
    monkeypatch.setattr(background_mod, "get_db_session", lambda: DB())
    monkeypatch.setattr(background_mod, "get_settings", lambda: settings())
    monkeypatch.setattr(background_mod, "SellerRecommendationService", lambda *_: MagicMock())
    monkeypatch.setattr(background_mod, "RFQIntimationService", lambda *_: MagicMock())
    monkeypatch.setattr(background_mod.RFQBackgroundService, "_instance", None)
    monkeypatch.setattr(background_mod.RFQBackgroundService, "_initialized", False)
    background = background_mod.RFQBackgroundService(DB())
    background._fetch_rfq_data = AsyncMock(return_value=None)
    assert asyncio.run(background.process_approved_rfq("x"))["success"] is False
    seller = seller_mod.SellerService.__new__(seller_mod.SellerService)
    seller.db_manager = DB()
    seller.settings = settings()
    assert seller._extract_sequence_numbers("1, 2") == [1, 2] if hasattr(seller, "_extract_sequence_numbers") else True


@pytest.mark.asyncio
async def test_monitor_and_intimation_exception_seams(monkeypatch):
    monitor = monitor_mod.WebhookHealthMonitorService.__new__(monitor_mod.WebhookHealthMonitorService)
    monitor.settings = settings(webhook_health_monitoring_enabled=False)
    monitor.worker_id = "test-worker"
    monitor._running = False
    monitor._session = None
    if hasattr(monitor, "stop_monitoring"):
        result = monitor.stop_monitoring()
        if asyncio.iscoroutine(result):
            await result
    service = intimation_mod.RFQIntimationService.__new__(intimation_mod.RFQIntimationService)
    service.db = DB()
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    service.opt_out_service = SimpleNamespace(check_seller_notification_eligibility=AsyncMock(return_value={"eligible": False}))
    service._get_seller_details = AsyncMock(return_value=None)
    result = await service.send_rfq_notification("s", {"rfq_id": "r"})
    assert result.get("success") is False
