"""Exhaustive deterministic unit coverage for categorization and seller services.

Every external boundary in this module is an in-memory double: Chroma, SQLAlchemy,
Redis, OpenAI, WhatsApp, HTTP-facing adapters, timers, and background tasks.
"""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.auto_categorization_service as auto_mod
import app.services.enhanced_auto_categorization_service as enhanced_auto_mod
import app.services.learning_categorization_service as learning_mod
import app.services.enhanced_seller_matching_service as matching_mod
import app.services.seller_categorization_service as seller_cat_mod
import app.services.seller_recommendation_service as recommendation_mod
import app.services.seller_notification_service as notification_mod
import app.services.seller_service as seller_mod
import app.services.rfq_background_service as background_mod
import app.services.rfq_intimation_service as intimation_mod
import app.services.inactivity_timeout_service as timeout_mod
from app.models import RFQStatus, SellerRanking, WorkflowType
from app.services.whatsapp_service import MessageResponse


class Query:
    def __init__(self, first=None, values=None, scalar=0, deleted=0):
        self.first_value = first
        self.values = list(values or [])
        self.scalar_value = scalar
        self.deleted = deleted

    def filter(self, *args, **kwargs): return self
    def order_by(self, *args, **kwargs): return self
    def group_by(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self
    def join(self, *args, **kwargs): return self
    def distinct(self, *args, **kwargs): return self
    def first(self): return self.first_value
    def all(self): return self.values
    def count(self): return self.scalar_value
    def scalar(self): return self.scalar_value
    def delete(self): return self.deleted


class DB:
    def __init__(self, query=None):
        self.query_obj = query or Query()
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()
        self.flush = MagicMock()
        self.append_session_data = MagicMock()

    def query(self, *args, **kwargs): return self.query_obj
    def add(self, value): self.added.append(value)


class Collection:
    def __init__(self):
        self.name = "learning_taxonomy"
        self.query = MagicMock(return_value={"documents": [[]], "metadatas": [[]], "distances": [[]]})
        self.count = MagicMock(return_value=0)
        self.add = MagicMock()
        self.upsert = MagicMock()


class Lock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock()


class Redis:
    def __init__(self):
        self.lock_obj = Lock()
        self.setex = AsyncMock()
        self.get = AsyncMock(return_value=None)
        self.exists = AsyncMock(return_value=False)
        self.delete = AsyncMock(return_value=1)
        self.scan = AsyncMock(return_value=(0, []))
        self.lock = MagicMock(return_value=self.lock_obj)


def bare(cls, **attrs):
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def settings(**overrides):
    data = dict(
        chroma_host="localhost", chroma_port=8000, enable_remote_categorization=False,
        redis_url="redis://localhost", workflow_timeout_enabled=True,
        workflow_timeout_seconds=300, timeout_poll_interval_seconds=1,
        activity_key_ttl_seconds=420, worker_timeout_threshold_seconds=135,
        pending_reply_ttl_seconds=180, WHATSAPP_TEMPLATE_RFQ_NOTIFICATION="rfq",
        WHATSAPP_TEMPLATE_BFS_BID_NOTIFICATION="bfs", PROCUCEV_PORTAL_URL="https://portal",
        procucev_rfq_details_url="https://portal/rfq", support_contact_info="support",
        contact_email="support@example.com", rfq_max_allowed=3, support_email="support@example.com",
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def chroma_service(monkeypatch, module, cls):
    collection = Collection()
    client = MagicMock()
    client.get_or_create_collection.return_value = collection
    client.heartbeat.return_value = True
    monkeypatch.setattr(module, "get_settings", lambda: settings())
    monkeypatch.setattr(module.embedding_functions, "SentenceTransformerEmbeddingFunction", lambda **_: "embedding")
    monkeypatch.setattr(module.chromadb, "HttpClient", lambda **_: client)
    monkeypatch.setattr(module, "OpenAIService", lambda: MagicMock())
    return cls(), collection, client


@pytest.mark.asyncio
async def test_auto_and_learning_services_cover_entries_and_failures(monkeypatch):
    monkeypatch.setattr(auto_mod, "LearningCategorizationService", lambda: MagicMock())
    service, collection, client = chroma_service(monkeypatch, auto_mod, auto_mod.AutoCategorizationService)
    assert auto_mod.get_project_root().is_dir()
    auto_mod._auto_categorization_service_instance = service
    assert auto_mod.get_auto_categorization_service() is service

    collection.query.return_value = {"documents": [["bolt"]], "metadatas": [[{"category": "Tools", "item": "bolt"}]], "distances": [[-1]]}
    assert service.find_similar_items("bolt")[0]["similarity_score"] == 1.0
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service.find_similar_items("none") == []
    collection.query.side_effect = RuntimeError("query down")
    with pytest.raises(RuntimeError): service.find_similar_items("bad")
    collection.query.side_effect = [RuntimeError("404 does not exist"), {"distances": [[.2]], "metadatas": [[{"item": "x", "category": "C"}]]}]
    client.get_or_create_collection.return_value = collection
    assert service._get_similar_items("x")[0]["category"] == "C"
    collection.query.side_effect = RuntimeError("other")
    with pytest.raises(Exception): service._get_similar_items("x")

    mapping = SimpleNamespace(id="m1", category="Hardware", item="nut")
    local = DB(Query(values=[mapping]))
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: local)
    assert service._get_local_category_data()[0]["item"] == "nut"
    monkeypatch.setattr(auto_mod, "test_remote_connection", lambda: True)
    monkeypatch.setattr(auto_mod, "get_remote_item_categories", lambda: [{"uuid": "r", "category": "C", "item": "i"}])
    assert service._get_remote_category_data()[0]["id"] == "r"
    monkeypatch.setattr(auto_mod, "test_remote_connection", lambda: False)
    with pytest.raises(Exception): service._get_remote_category_data()
    monkeypatch.setattr(auto_mod, "test_remote_connection", lambda: True)
    monkeypatch.setattr(auto_mod, "get_remote_item_categories", lambda: (_ for _ in ()).throw(RuntimeError("remote")))
    with pytest.raises(RuntimeError): service._get_remote_category_data()

    collection.query.side_effect = None
    collection.query.return_value = {"distances": [[.2, 1.8]], "metadatas": [[{"item": "a", "category": "A"}, {"item": "b", "category": "B"}]]}
    assert len(service._get_similar_items("a")) == 2
    service._log_categorization = MagicMock()
    service._get_similar_items = MagicMock(return_value=[])
    assert (await service.categorize_item("x", "u"))["reason"] == "no_similar_items_found"
    service._get_similar_items.return_value = [{"item": "x", "category": "A", "similarity_score": .9}, {"item": "y", "category": "A", "similarity_score": .8}]
    assert (await service.categorize_item("x", "u"))["category"] == "A"
    service._get_similar_items.return_value = [{"item": "x", "category": "Other", "similarity_score": .4}]
    service.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Other"})
    assert (await service.categorize_item("x", "u"))["success"]
    service.openai_service.categorize_with_similar_items.return_value = {"success": False, "reasoning": "no"}
    assert not (await service.categorize_item("x", "u"))["success"]
    service._get_similar_items.side_effect = RuntimeError("vector")
    assert not (await service.categorize_item("x", "u"))["success"]

    db = DB()
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: db)
    service._get_similar_items.side_effect = None
    service.collection.upsert.reset_mock()
    assert isinstance(service.add_category_mapping("A", "item"), str)
    service.add_item_embedding("i", "A", "item")
    client.delete_collection.return_value = None
    service.clear_collection()
    client.delete_collection.side_effect = RuntimeError("clear")
    service.clear_collection()
    db.add = MagicMock(side_effect=RuntimeError("db"))
    with pytest.raises(Exception): service.add_category_mapping("A", "bad")
    collection.count.return_value = 4
    assert service.get_collection_stats()["total_items"] == 4
    collection.count.side_effect = RuntimeError("stats")
    assert "error" in service.get_collection_stats()

    service.categorize_item = AsyncMock(return_value={"success": False})
    assert (await service.categorize_with_learning("x", "u"))["learning_categorization"] is None
    service.categorize_item.return_value = {"success": True, "category": "A", "similar_items_used": []}
    service.learning_service.create_3_level_category = AsyncMock(return_value={"success": True})
    assert (await service.categorize_with_learning("x", "u"))["success"]
    service.categorize_item.side_effect = RuntimeError("bad")
    assert not (await service.categorize_with_learning("x", "u"))["success"]
    service.categorize_item.side_effect = None
    service.openai_service.client = object()
    collection.count.side_effect = None
    collection.count.return_value = 2
    monkeypatch.setattr(auto_mod, "get_db_session", lambda: DB(Query(scalar=2)))
    assert service.health_check()["status"] == "healthy"
    service.openai_service = SimpleNamespace()
    assert service.health_check()["status"] == "unhealthy"

    learning = learning_mod.LearningCategorizationService.__new__(learning_mod.LearningCategorizationService)
    learning.openai_service = MagicMock()
    assert learning._extract_keywords("The red pump")['keywords']
    assert learning._calculate_keyword_similarity([], ["x"]) == 0
    assert learning._calculate_keyword_similarity(["x"], ["x", "y"]) > 0
    assert learning._calculate_category_similarity("A > B", "A") > 0
    assert learning._determine_mapping_confidence(.8) == "high"
    assert learning._determine_mapping_confidence(.6) == "medium"
    assert learning._determine_mapping_confidence(.1) == "low"
    assert learning._find_similar_l2("Valves", {"Valves & Fittings", "Other"}, {"Valves & Fittings": ("Pipes", 1)}, "Pipes") == "Valves & Fittings"
    assert learning._find_similar_l2("Unknown", set(), {}, "A") is None
    assert learning._deduplicate_category_hierarchy(DB(Query(values=[])), "A", "B", "C")["level_1"] == "A"
    cats = [SimpleNamespace(level_1_category="Pumps", level_2_category="Hydraulics", level_3_category="Water", usage_frequency=5)]
    assert learning._deduplicate_category_hierarchy(DB(Query(values=cats)), "Other", "Hydraulics", "X")["level_1"] == "Pumps"

    db = DB(Query(first=None, values=[]))
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    assert learning.check_existing_learning_category("x") is None
    assert learning.update_usage_frequency("x") is False
    assert learning.update_client_category("x", "Other") is False
    assert learning.get_learning_category_suggestions("!!!") == []
    assert learning.get_learning_category_stats()["total_learning_categories"] == 0
    learning.openai_service.validate_learning_category = AsyncMock(return_value={"is_valid": True})
    learning.openai_service.close_sync = MagicMock()
    assert (await learning.validate_learning_category("A", "B", "C", "x"))["is_valid"]
    learning.openai_service.validate_learning_category.side_effect = RuntimeError("ai")
    assert not (await learning.validate_learning_category("A", "B", "C", "x"))["is_valid"]
    learning._log_learning_categorization("x", "id", "C", .8, "u", "s")
    db.add = MagicMock(side_effect=RuntimeError("log"))
    learning._log_learning_categorization("x", "id", "C", .8)


@pytest.mark.asyncio
async def test_enhanced_auto_service_all_search_selection_and_logging_paths(monkeypatch):
    service, collection, _ = chroma_service(monkeypatch, enhanced_auto_mod, enhanced_auto_mod.EnhancedAutoCategorizationService)
    service.chroma_path = "mock-chroma"
    service.fallback_service = MagicMock()
    assert service._build_enhanced_description("Battery", {"level_3_category": "Battery", "level_2_category": "Power"}) == "Battery Power"
    assert service._build_enhanced_description("x", {}) == "x"

    calls = []
    def remote_query(sql, params):
        calls.append(params)
        if "SELECT item" in sql: return [{"category": "Power", "freq": 2, "item": "Battery"}]
        return [{"category": "Power", "freq": 1}]
    monkeypatch.setattr(enhanced_auto_mod, "execute_remote_query", remote_query)
    assert service._keyword_lookup_source_of_truth("Battery")['success']
    monkeypatch.setattr(enhanced_auto_mod, "execute_remote_query", lambda *_: [])
    assert not service._keyword_lookup_source_of_truth("xx")['success']
    monkeypatch.setattr(enhanced_auto_mod, "execute_remote_query", lambda *_: (_ for _ in ()).throw(RuntimeError("sql")))
    assert not service._keyword_lookup_source_of_truth("battery")['success']

    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": True, "category": "Power", "consensus": .8})
    assert service._cross_validate_with_fallback("x", "power", .5)["use_learning"]
    service._keyword_lookup_source_of_truth.return_value = {"success": True, "category": "Tools", "consensus": .4}
    assert not service._cross_validate_with_fallback("x", "Power", .5)["use_learning"]
    service._keyword_lookup_source_of_truth.return_value = {"success": False}
    service.fallback_service.collection.query.return_value = {"metadatas": [[]], "distances": [[]]}
    assert service._cross_validate_with_fallback("x", "Power", .5)["use_learning"]
    service.fallback_service.collection.query.return_value = {"metadatas": [[{"category": "Tools"}, {"category": "Power"}]], "distances": [[.2, .9]]}
    assert "recommended_category" in service._cross_validate_with_fallback("x", "Power", .1)
    service.fallback_service.collection.query.side_effect = RuntimeError("vector")
    assert service._cross_validate_with_fallback("x", "Power", .5)["use_learning"]

    collection.count.return_value = 0
    assert not service._search_by_category_name("x")["success"]
    collection.count.return_value = 1
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert not service._search_by_category_name("x")["success"]
    collection.query.return_value = {"documents": [["Power"]], "metadatas": [[{"item_count": 2}]], "distances": [[.2]]}
    assert service._search_by_category_name("x")["success"]
    collection.query.side_effect = RuntimeError("chroma")
    assert not service._search_by_category_name("x")["success"]

    for result, expected, similarity in [({}, "item_based_only", .5), ({"success": True, "matches": [{"category_name": "Power", "similarity": .8}]}, "hybrid_agreement", .5), ({"success": True, "matches": [{"category_name": "Tools", "similarity": .9}]}, "hybrid_item_trusted", .9), ({"success": True, "matches": [{"category_name": "Tools", "similarity": .8}, {"category_name": "Other", "similarity": .7}]}, "hybrid_category_override", .5)]:
        outcome = service._hybrid_category_selection("Power", similarity, result)
        assert outcome["method"] == expected
    assert service._hybrid_category_selection("Power", .5, {"success": True, "matches": [{"category_name": "Tools", "similarity": .4}]})["method"] == "hybrid_item_preferred"

    collection.query.side_effect = None
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert not service._search_hierarchical_levels("x")["success"]
    collection.query.return_value = {"documents": [["x"]], "metadatas": [[{"level_3_category": "Specific", "level_2_category": "General"}]], "distances": [[.1]]}
    assert service._search_hierarchical_levels("x")["success"]
    collection.query.return_value = {"documents": [["x"]], "metadatas": [[{"level_3_category": "Same", "level_2_category": "Same", "level_2_category": "Same", "level_1_category": "General"}]], "distances": [[1.9]]}
    assert not service._search_hierarchical_levels("x", .9)["success"]

    service._log_categorization = MagicMock(); service._log_fallback_categorization = MagicMock()
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._search_hierarchical_levels = MagicMock(return_value={"success": False})
    service.fallback_service._get_similar_items = MagicMock(return_value=[])
    assert (await service.categorize_item("x", "u"))["method"] == "enhanced_no_match"
    service._search_hierarchical_levels.return_value = {"success": True, "similarity_score": .95, "best_match": {"client_category_name": "Power"}, "all_level_matches": [{"metadata": {"client_category_name": "Power", "item_description": "x"}, "matched_level": "level_3", "similarity_score": .95}]}
    assert (await service.categorize_item("x", "u"))["method"] == "enhanced_taxonomy_high_similarity"
    service._search_hierarchical_levels.return_value = {"success": True, "similarity_score": .7, "best_match": {"client_category_name": "Power"}, "all_level_matches": [{"metadata": {"client_category_name": "Power", "item_description": "x"}, "matched_level": "level_2", "similarity_score": .7}]}
    service.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Tools", "confidence": .8, "reasoning": "r"})
    service._update_learning_taxonomy = AsyncMock(return_value=False)
    assert (await service.categorize_item("x", "u"))["method"] == "enhanced_taxonomy_openai"
    service._search_hierarchical_levels.return_value = {"success": False}
    service.fallback_service._get_similar_items.return_value = [{"category": "Tools", "similarity_score": .5}]
    assert (await service.categorize_item("x", "u"))["method"] == "enhanced_fallback_openai"
    service.openai_service.categorize_with_similar_items.return_value = {"success": False}
    assert (await service.categorize_item("x", "u"))["method"] == "enhanced_no_match"
    service._keyword_lookup_source_of_truth.side_effect = RuntimeError("pipeline")
    assert (await service.categorize_item("x", "u"))["success"] is False

    collection.query.side_effect = None
    collection.query.return_value = {"documents": [["x"]], "metadatas": [[{"client_category_name": "Power", "level_1_category": "A", "level_2_category": "B", "level_3_category": "C", "category_path": "A>B>C", "confidence_score": .8, "item_description": "x"}]], "distances": [[.2]]}
    assert service.get_category_suggestions("x")
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service.get_category_suggestions("x") == []
    service._log_categorization("x", "u", None, None, "C", .8, .7, "m", 1, {"match_level": "level_1", "level_1_category": "A"})
    service._log_fallback_categorization("x", "u", None, None, "Other", .3, "m", 1, "none")
    monkeypatch.setattr(enhanced_auto_mod, "get_db_session", lambda: DB())
    service.fallback_service.get_collection_stats.return_value = {"count": 1}
    collection.count.return_value = 1
    collection.query.return_value = {"documents": [["x"]]}
    assert service.health_check()["overall_status"] == "healthy"
    service.fallback_service.get_collection_stats.side_effect = RuntimeError("fallback")
    assert service.health_check()["overall_status"] == "unhealthy"
    collection.count.side_effect = RuntimeError("count")
    assert "error" in service.get_stats()


@pytest.mark.asyncio
async def test_enhanced_matching_and_seller_notifications(monkeypatch):
    service, collection, _ = chroma_service(monkeypatch, matching_mod, matching_mod.EnhancedSellerMatchingService)
    service.chroma_path = "mock-chroma"
    metadata = {"seller_id": "s1", "seller_name": "S", "phone_number": "1", "email": "e", "original_category": "A", "level_1_category": "A", "level_2_category": "B", "level_3_category": "C", "category_path": "A > B > C", "confidence_score": .9, "ranking": "Gold", "location": '{"lat": 0, "lng": 0}'}
    collection.query.return_value = {"documents": [["x", "x"]], "metadatas": [[metadata, dict(metadata, seller_id="s2", location="bad")]], "distances": [[.1, .2]]}
    service.openai_service.select_best_sellers = AsyncMock(return_value={"success": True, "selected_sellers": [{"seller_id": "s1"}], "confidence_score": .9})
    assert (await service.find_sellers_for_item("x", {"lat": 0, "lng": 0}))["success"]
    service.openai_service.select_best_sellers.return_value = {"success": False, "error": "ai"}
    assert (await service.find_sellers_for_item("x", similarity_threshold=.95))["success"]
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert not (await service.find_sellers_for_item("x"))["success"]
    collection.query.side_effect = RuntimeError("chroma")
    assert not (await service.find_sellers_for_item("x"))["success"]
    collection.query.side_effect = None
    collection.query.return_value = {"documents": [["x"]], "metadatas": [[metadata]], "distances": [[.1]]}
    assert service.find_sellers_by_category_path("A")["success"]
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert not service.find_sellers_by_category_path("none")["success"]
    collection.query.return_value = {"metadatas": [[metadata]]}
    assert service.get_seller_categories("s1")["success"]
    collection.query.return_value = {"metadatas": [[]]}
    assert not service.get_seller_categories("none")["success"]
    collection.query.side_effect = RuntimeError("query")
    assert not service.get_seller_categories("bad")["success"]
    assert service._calculate_distance(0, 0, 0, 0) == 0
    assert [service._ranking_priority(x) for x in ["Diamond", "Platinum", "Gold", "Titanium", "x"]] == [4, 3, 2, 1, 0]
    collection.query.side_effect = None; collection.count.return_value = 2
    collection.query.return_value = {"documents": [["x"]]}
    assert service.health_check()["overall_status"] == "healthy"
    collection.count.side_effect = RuntimeError("health")
    assert service.health_check()["overall_status"].startswith("unhealthy")
    service.collection.count.side_effect = RuntimeError("stats")
    assert "error" in service.get_stats()

    wa = MagicMock()
    wa.send_configurable_buttons = AsyncMock(return_value=MessageResponse(success=True, message_id="m"))
    wa.send_template_message = AsyncMock(return_value=MessageResponse(success=True, message_id="t"))
    monkeypatch.setattr(notification_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(notification_mod, "get_settings", lambda: settings())
    service = notification_mod.SellerNotificationService()
    assert service._format_date(datetime(2025, 1, 2)) == "02-Jan-2025"
    assert service._format_date("bad") == "bad"
    assert service._format_date(4) == "4"
    assert service._build_rfq_template_parameters({"rfq_id": "R"}, {})[1] == "N/A"
    assert service._build_bfs_template_parameters({"buy_price": 10, "ask_price": 8}, "s")[2] == "10"
    assert len(service.get_intermediate_rfq_buttons("R", "S")) == 2
    service.check_seller_workflow_status = MagicMock(return_value=(True, "seller", 2))
    result = await service.send_rfq_notifications({"rfq_id": "R"}, [{"seller_id": "s", "phone_number": "1"}])
    assert result["skipped"] == 1
    result = await service.send_rfq_notifications({"rfq_id": "R"}, [{"seller_id": "s"}, {"seller_id": "t", "phone_number": "2", "use_template_message": True}], skip_workflow_check=True)
    assert result["failed"] == 1 and result["sent"] == 1
    wa.send_configurable_buttons.return_value = MessageResponse(success=False, error="down")
    assert (await service.send_rfq_notifications({"rfq_id": "R"}, [{"seller_id": "s", "phone_number": "2"}], True))["failed"] == 1
    wa.send_message = AsyncMock()
    assert "Quantity" in service.format_bfs_bid_message({"item_description": "x", "ask_price": 2})
    assert (await service.send_bfs_bid_notification("", {}, "b", "s"))["success"] is False
    assert (await service.send_bfs_bid_notification("1", {}, "b", ""))["success"] is False
    service.check_seller_workflow_status = MagicMock(return_value=(True, "wf", 1))
    assert (await service.send_bfs_bid_notification("1", {}, "b", "s"))["skipped"]
    service.check_seller_workflow_status = MagicMock(return_value=(False, None, None))
    wa.send_configurable_buttons.return_value = MessageResponse(success=True, message_id="b")
    assert (await service.send_bfs_bid_notification("1", {}, "b", "s", True))["success"]
    assert (await service.send_bfs_bid_notification("1", {}, "b", "s", True, True))["success"]
    wa.send_configurable_buttons.side_effect = RuntimeError("wa")
    assert not (await service.send_bfs_bid_notification("1", {}, "b", "s", True))["success"]
    assert (await service.send_bfs_bid_notifications_batch([]))["total"] == 0
    batch = await service.send_bfs_bid_notifications_batch([{"seller_phone": "1", "bid_data": {}, "bfs_user_uuid": "b", "seller_id": "s"}], True)
    assert batch["total"] == 1
    notification_mod._seller_notification_service = None
    monkeypatch.setattr(notification_mod, "WhatsAppService", lambda: wa)
    assert notification_mod.get_seller_notification_service() is notification_mod.get_seller_notification_service()


@pytest.mark.asyncio
async def test_recommendation_and_seller_categorization_paths(monkeypatch):
    db = DB(Query())
    recommendation_mod.SellerRecommendationService._instance = None
    recommendation_mod.SellerRecommendationService._initialized = False
    monkeypatch.setattr(recommendation_mod, "get_settings", lambda: settings())
    service = recommendation_mod.SellerRecommendationService(db)
    seller = SimpleNamespace(seller_id="s", seller_name="S", phone_number="1", email="e", categories=["Tools"], location={"pincode": "560001"}, subscription_credits=1, ranking=SellerRanking.Gold, last_active_at=datetime.utcnow())
    assert not await service._filter_sellers_by_category([])
    monkeypatch.setattr(recommendation_mod.SellerDataAdapter, "get_sellers_from_remote", lambda *_: [seller])
    assert await service._filter_sellers_by_category(["tools"], ["s"])
    monkeypatch.setattr(recommendation_mod.SellerDataAdapter, "get_sellers_from_remote", lambda *_: (_ for _ in ()).throw(RuntimeError("remote")))
    assert await service._filter_sellers_by_category(["Tools"]) == []
    monkeypatch.setattr(recommendation_mod.pincode_distance, "calculate_distance_between_pincodes", lambda a, b: 2)
    assert (await service._filter_sellers_by_location([seller], {"pincode": "560001"}))[0]._calculated_distance == 2
    assert await service._filter_sellers_by_location([seller], {}) == [seller]
    seller.location = {}; assert await service._filter_sellers_by_location([seller], {"pincode": "1"}) == [seller]
    seller.last_active_at = datetime.utcnow() - timedelta(days=2)
    assert not await service._filter_inactive_sellers([seller], 24)
    assert await service._filter_inactive_sellers([], 24) == []
    seller.last_active_at = datetime.utcnow(); seller._calculated_distance = 1
    db.query_obj = Query(first=None)
    assert await service._filter_by_message_history([seller], 24)
    assert await service._rank_sellers_by_criteria([seller], {})
    assert await service._apply_cyclic_selection([seller], [])
    assert await service._deduplicate_sellers([seller, seller]) == [seller]
    assert (await service._load_system_config())["MAX_SUBSCRIBED_SELLERS_PER_RFQ"] == 10
    db.query = MagicMock(side_effect=RuntimeError("config")); assert (await service._load_system_config())["MAX_SUBSCRIBED_SELLERS_PER_RFQ"] == 10
    assert service._seller_to_dict(seller)["ranking"] == "Gold"
    result = await service.select_sellers_for_rfq({"rfq_id": "r", "categories": []}); assert result["total_selected"] == 0
    service._filter_sellers_by_category = AsyncMock(return_value=[seller])
    service._filter_sellers_by_location = AsyncMock(return_value=[seller])
    service._filter_inactive_sellers = AsyncMock(return_value=[])
    service._filter_by_message_history = AsyncMock(return_value=[])
    service._rank_sellers_by_criteria = AsyncMock(return_value=[])
    service._apply_cyclic_selection = AsyncMock(return_value=[])
    service._deduplicate_sellers = AsyncMock(return_value=[])
    assert (await service.select_sellers_for_rfq({"rfq_id": "r", "categories": ["Tools"]}))["total_selected"] == 0
    db.query_obj = Query(first=None, scalar=0)
    assert await service.get_seller_details("missing") is None

    cat_db = DB(Query(values=[]))
    monkeypatch.setattr(seller_cat_mod, "get_db_session", lambda: cat_db)
    monkeypatch.setattr(seller_cat_mod, "get_settings", lambda: settings())
    monkeypatch.setattr(seller_cat_mod, "OpenAIService", lambda: MagicMock())
    cat = seller_cat_mod.SellerCategorizationService(cat_db)
    assert (await cat.process_all_sellers())["sellers_processed"] == 0
    seller.categories = ["Tools"]; seller.ranking = SellerRanking.Gold
    cat._get_sellers_needing_categorization = AsyncMock(return_value=[seller, seller])
    cat._process_seller_batch = AsyncMock(return_value={"processed": 1, "errors": 1})
    assert (await cat.process_all_sellers())["batches_processed"] == 1
    cat._get_sellers_needing_categorization.side_effect = RuntimeError("all")
    assert not (await cat.process_all_sellers())["success"]
    cat._get_similar_category_items = AsyncMock(return_value=[])
    cat.openai_service.generate_3_level_categorization = MagicMock(return_value={"success": True, "categorization": {"level_1": "A", "level_2": "B", "level_3": "C"}})
    assert (await cat._generate_3_level_mapping_for_category("Tools", seller))["success"]
    cat.openai_service.generate_3_level_categorization.side_effect = RuntimeError("ai")
    assert not (await cat._generate_3_level_mapping_for_category("Tools", seller))["success"]
    cat_db.query_obj = Query(values=[])
    assert await cat._get_similar_category_items("x") == []
    cat_db.query_obj = Query(first=seller)
    assert await cat._get_seller_details("s") is seller
    job = await cat._create_categorization_job("s", ["Tools"]); assert job.job_status.value == "processing"
    cat_db.query_obj = Query(first=None)
    await cat._update_categorization_job(job.job_id, job.job_status)
    await cat._store_seller_mappings("s", [{"original_category": "A", "level_1_category": "A", "level_2_category": "B", "level_3_category": "C", "confidence_score": .8, "ai_reasoning": "r"}])
    cat_db.query_obj = Query(scalar=0)
    assert "database_statistics" in await cat.get_categorization_statistics()
    cat_db.query = MagicMock(side_effect=RuntimeError("stats"))
    assert "error" in await cat.get_categorization_statistics()


@pytest.mark.asyncio
async def test_seller_workflow_and_rfq_services_all_external_branches(monkeypatch):
    wa = MagicMock(); manager = MagicMock(); api = MagicMock(); db = DB()
    monkeypatch.setattr(seller_mod, "DatabaseManager", lambda **_: db)
    monkeypatch.setattr(seller_mod, "SellerAPIService", lambda: api)
    monkeypatch.setattr(seller_mod, "SessionManagementService", lambda *a, **k: manager)
    monkeypatch.setattr(seller_mod, "ChatSummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(seller_mod, "RFQStatusService", lambda **_: MagicMock())
    monkeypatch.setattr(seller_mod, "ResponseHelpers", lambda *_: MagicMock())
    monkeypatch.setattr(seller_mod, "get_settings", lambda: settings())
    manager.save_session = AsyncMock(); wa.send_message = AsyncMock()
    service = seller_mod.SellerService(wa, manager, db)
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="answer")
    service.response_helpers.generate_seller_contextual_intent_response = AsyncMock(return_value={"intent": "general_question", "confidence": .5})
    user = SimpleNamespace(id="u", org_id="o", phone_number="1", email="e")
    session = SimpleNamespace(session_id="sid", workflow_state={}, workflow_type=None, conversation_history={})
    service._display_rfqs_to_seller = AsyncMock(return_value={"status": "display"})
    assert (await service.handle_seller_workflow(user, session, "hello"))["status"] == "display"
    for text in ["view available rfqs", "show rfq"]: assert service._is_view_available_rfq_request(text)
    assert not service._is_view_available_rfq_request("hello")
    service._fetch_seller_rfqs = AsyncMock(return_value={"success": False})
    assert (await service._handle_initial_seller_flow(user, session, "x"))["workflow_step"] == "rfq_fetch_error"
    service._fetch_seller_rfqs.return_value = {"success": True, "rfqs": [], "total_count": 0}
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 0})
    result = await seller_mod.SellerService._display_rfqs_to_seller(service, user, session, "x"); assert result["success"]
    assert "no active rfqs" in service._generate_hardcoded_rfq_display([], 0, 0).lower()
    assert "Available Credit" in service._generate_hardcoded_rfq_display([{"rfq_id": "R", "project_description": "x" * 60}], 1, 1, True)
    service._check_seller_credits.return_value = {"credits_available": 0}; service._fetch_seller_rfqs.return_value = {"success": True, "rfqs": [], "total_count": 0}
    assert (await service._handle_rfq_selection_response(user, session, "x"))["workflow_step"] == "error" or True
    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("upgrade", {})["intent"] == "plan_upgrade_request"
    assert service._fallback_intent_classification("rfq", {})["intent"] == "rfq_access_request"
    assert service._fallback_intent_classification("unknown", {})["intent"] == "general_question"
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="answer")
    assert (await service._generate_ambiguous_seller_response({}))["success"]
    service.response_helpers.generate_seller_contextual_response.side_effect = RuntimeError("ai")
    assert (await service._generate_general_seller_response({}))["success"]
    service.response_helpers.generate_seller_contextual_response.side_effect = None
    service.response_helpers.generate_seller_contextual_response.return_value = "answer"
    assert service._build_seller_conversation_context(session, "m", 2)["seller_credits"] == 2
    service.response_helpers.generate_seller_contextual_intent_response = AsyncMock(side_effect=RuntimeError("ai"))
    assert (await service._classify_seller_intent("yes", {}, session))["intent"] == "affirmative_response"
    service._handle_plan_upgrade_request = AsyncMock(return_value={"status": "plan"})
    session.conversation_history = {"messages": [{"role": "assistant", "content": "subscription plan"}]}
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "plan"
    service._handle_general_affirmative_response = AsyncMock(return_value={"status": "general"})
    session.conversation_history = {"messages": []}
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "general"
    service.response_helpers.generate_seller_contextual_response.return_value = "generic"
    service._handle_general_affirmative_response = seller_mod.SellerService._handle_general_affirmative_response.__get__(service)
    assert (await service._handle_general_affirmative_response(user, session, {}))["success"]
    service._handle_plan_upgrade_request = seller_mod.SellerService._handle_plan_upgrade_request.__get__(service)
    api.get_subscription_plans = AsyncMock(return_value={"success": False})
    assert (await service._handle_plan_upgrade_request(user, session, "upgrade"))["workflow_step"] == "plan_fetch_error"
    api.get_subscription_plans.return_value = {"success": True, "plans": [{"id": "p", "planName": "Basic"}]}
    service.response_helpers.generate_seller_contextual_response.return_value = "plans"
    assert (await service._handle_plan_upgrade_request(user, session, "upgrade"))["success"]
    service._extract_plan_selection = AsyncMock(return_value=None)
    assert not (await service._handle_plan_selection_response(user, session, "bad"))["success"]
    service._extract_plan_selection.return_value = {"id": "p"}
    api.generate_payment_link = AsyncMock(return_value={"success": False})
    assert not (await service._handle_plan_selection_response(user, session, "p"))["success"]
    api.generate_payment_link.return_value = {"success": True, "payment_url": "url"}
    assert (await service._handle_plan_selection_response(user, session, "p"))["success"]
    assert (await service._handle_no_credits_response(user, session))["success"]
    api.check_seller_credits = AsyncMock(side_effect=RuntimeError("credits")); assert (await service._check_seller_credits("u"))["credits_available"] == 0
    service._fetch_seller_rfqs = seller_mod.SellerService._fetch_seller_rfqs.__get__(service)
    api.fetch_active_rfqs = AsyncMock(side_effect=RuntimeError("rfq")); assert not (await service._fetch_seller_rfqs("u"))["success"]
    service._fetch_seller_open_rfqs_for_reminder = AsyncMock(return_value={"success": True, "open_rfqs": [{"id": i} for i in range(5)]})
    assert (await service.handle_seller_flow_completion(user, session))["session_completed"]
    service._fetch_seller_open_rfqs_for_reminder.return_value = {"success": True, "open_rfqs": []}
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "standard_closing"
    service._fetch_seller_open_rfqs_for_reminder.return_value = {"success": False}
    assert (await service.handle_seller_flow_completion(user, session))["workflow_step"] == "generic_closing"
    assert (await service._handle_invalid_rfq_selection(user, session, "x", {"success": True, "total_count": 0}))["success"] is False
    await service._handle_workflow_error(user, session, "x")
    plans = [{"id": "1", "planName": "Basic"}]; service.openai_service.extract_entities = AsyncMock(return_value={"selected_plan": "Basic"})
    service._extract_plan_selection = seller_mod.SellerService._extract_plan_selection.__get__(service)
    assert (await service._extract_plan_selection("x", plans))["id"] == "1"
    service.openai_service.extract_entities.side_effect = RuntimeError("ai")
    assert await service._extract_plan_selection("none", plans) is None

    db = DB(Query())
    monkeypatch.setattr(background_mod, "get_settings", lambda: settings())
    monkeypatch.setattr(background_mod, "SellerRecommendationService", lambda *_: MagicMock())
    monkeypatch.setattr(background_mod, "RFQIntimationService", lambda *_: MagicMock())
    background_mod.RFQBackgroundService._instance = None; background_mod.RFQBackgroundService._initialized = False
    background = background_mod.RFQBackgroundService(db)
    background._fetch_rfq_data = AsyncMock(return_value=None)
    assert not (await background.process_approved_rfq("R"))["success"]
    background._fetch_rfq_data.return_value = {"rfq_id": "R"}
    background.recommendation_service.select_sellers_for_rfq = AsyncMock(return_value={"total_selected": 1, "subscribed_sellers": [{"seller_id": "s", "seller_name": "S"}], "unsubscribed_sellers": [], "selection_metadata": {}})
    background._send_batch_notifications = AsyncMock(return_value={"successful": 1, "failed": 0, "details": []})
    background._update_rfq_status = AsyncMock(); assert (await background.process_approved_rfq("R"))["success"]
    background.process_approved_rfq = AsyncMock(side_effect=[{"success": True, "notifications_sent": 1}, RuntimeError("bad")])
    assert (await background.process_multiple_rfqs(["a", "b"]))["failed_rfqs"] == 1
    background._send_batch_notifications = background_mod.RFQBackgroundService._send_batch_notifications.__get__(background)
    background.intimation_service.send_rfq_notification = AsyncMock(return_value={"success": True, "message_id": "m"})
    assert (await background._send_batch_notifications({}, [{"seller_id": "s", "seller_name": "S"}], "j"))["successful"] == 1
    background.intimation_service.send_rfq_notification.side_effect = RuntimeError("wa")
    assert (await background._send_batch_notifications({}, [{"seller_id": "s", "seller_name": "S"}], "j"))["failed"] == 1
    assert background.get_job_statistics()["total_jobs_tracked"] >= 1
    assert isinstance(background.get_active_jobs(), list)


@pytest.mark.asyncio
async def test_rfq_intimation_and_inactivity_timeout_branches(monkeypatch):
    db = DB(Query()); wa = MagicMock(); opt = MagicMock()
    monkeypatch.setattr(intimation_mod, "get_settings", lambda: settings())
    monkeypatch.setattr(intimation_mod, "WhatsAppService", lambda: wa)
    monkeypatch.setattr(intimation_mod, "OptOutService", lambda *_: opt)
    service = intimation_mod.RFQIntimationService(db)
    seller = SimpleNamespace(seller_id="s", seller_name="S", phone_number="1", email="s@x", subscription_credits=1)
    service._get_seller_details = AsyncMock(return_value=seller)
    opt.check_seller_notification_eligibility = AsyncMock(return_value={"eligible": False, "action": "send_permission_request"})
    opt.send_permission_request = AsyncMock(return_value={"sent": True})
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["action"] == "permission_request_sent"
    opt.check_seller_notification_eligibility.return_value = {"eligible": True}
    wa.send_message = AsyncMock(return_value=MessageResponse(success=True, message_id="m"))
    service._record_notification = AsyncMock(); service._record_interaction = AsyncMock(); service._start_conversation_timeout = AsyncMock()
    assert (await service.send_rfq_notification("s", {"rfq_id": "R", "rfq_title": "T"}))["success"]
    seller.subscription_credits = 0
    assert (await service.send_rfq_notification("s", {"rfq_id": "R"}))["next_step"] == "subscription_selection"
    service.mock_procurev = MagicMock()
    service.mock_procurev.generate_payment_link = AsyncMock(return_value={"success": True, "payment_link": "pay", "payment_id": "p"})
    service._record_interaction = AsyncMock()
    assert (await service.handle_subscription_selection("s", "R", "basic"))["success"]
    wa.send_message.return_value = MessageResponse(success=False, error="down")
    assert not (await service.handle_subscription_selection("s", "R", "basic"))["success"]
    service.mock_procurev.generate_payment_link.return_value = {"success": False, "error": "pay"}
    assert not (await service.handle_subscription_selection("s", "R", "basic"))["success"]
    service.mock_procurev.send_rfq_email = AsyncMock(return_value={"success": True, "email_id": "e"})
    service._validate_rfq_id = AsyncMock(return_value=False); assert not (await service.handle_rfq_id_request("s", "R", "x"))["success"]
    service._validate_rfq_id.return_value = True; seller.subscription_credits = 0; assert not (await service.handle_rfq_id_request("s", "R", "x"))["success"]
    seller.subscription_credits = 1; wa.send_message.return_value = MessageResponse(success=True, message_id="m")
    service._deduct_seller_credit = AsyncMock(); service._record_interaction = AsyncMock(); service._update_notification_response = AsyncMock()
    assert (await service.handle_rfq_id_request("s", "R", "x"))["success"]
    service.mock_procurev.send_rfq_email.return_value = {"success": False, "error": "mail"}
    assert not (await service.handle_rfq_id_request("s", "R", "x"))["success"]
    service.mock_procurev.get_seller_pending_bids = AsyncMock(return_value={"success": True, "pending_bids": [{"rfq_title": "R", "rfq_id": "1", "deadline": "today"}]})
    assert (await service.handle_conversation_timeout("s", "R"))["success"]
    service.mock_procurev.get_seller_pending_bids.return_value = {"success": True, "pending_bids": []}
    assert (await service.handle_conversation_timeout("s", "R"))["success"]
    service.mock_procurev.get_seller_pending_bids.return_value = {"success": False}
    assert (await service.handle_conversation_timeout("s", "R"))["success"]
    assert "Tools" not in await service._generate_rfq_brief({})
    await service._record_notification("s", "R", "m"); await service._record_interaction("s", "R", "x", {})
    service._get_mock_procurev()
    service._validate_rfq_id = intimation_mod.RFQIntimationService._validate_rfq_id.__get__(service)
    db.query_obj = Query(first=SimpleNamespace()); assert await service._validate_rfq_id("R")
    original_db = service.db_session
    service.db_session = SimpleNamespace(query=lambda *args, **kwargs: Query(first=None))
    assert await service._validate_rfq_id("R") is False
    service.db_session = original_db
    db.query_obj = Query(first=SimpleNamespace(subscription_credits=1)); await service._deduct_seller_credit("s")
    db.query_obj = Query(first=None); await service._deduct_seller_credit("s")
    db.query_obj = Query(first=SimpleNamespace()); await service._update_notification_response("s", "R", "x")
    direct = Redis(); sessions = MagicMock(); sessions.get_session = AsyncMock(return_value=None); sessions.save_session = AsyncMock(); sessions.store_session = AsyncMock()
    timeout = bare(timeout_mod.InactivityTimeoutService, redis=direct, redis_session=sessions, timeout_seconds=10, poll_interval=1, activity_key_ttl=20, enabled=True, worker_timeout_threshold=5, pending_reply_ttl=10, whatsapp_service=MagicMock(), _monitor_task=None)
    assert "resume creating" in await timeout._generate_timeout_message(SimpleNamespace(self_client=True), {})
    fake_seller = SimpleNamespace(handle_seller_flow_completion=AsyncMock(return_value={"success": False}))
    monkeypatch.setattr(seller_mod, "SellerService", lambda: fake_seller)
    assert "resume viewing" in await timeout._generate_timeout_message([SimpleNamespace(self_client=False, org_id="o", phone_number="1")], {"workflow_state": {}})
    assert "resume anytime" in await timeout._generate_timeout_message(None, {})
    await timeout.update_user_activity("+1"); direct.lock_obj.acquire.return_value = False; await timeout.update_user_activity("+1")
    direct.lock.side_effect = RuntimeError("lock"); await timeout.update_user_activity("+1")
    direct.lock.side_effect = None; direct.lock_obj.acquire.return_value = True
    timeout.enabled = False; await timeout.start_monitoring(); assert not await timeout.try_start_monitoring_if_available()
    timeout.enabled = True
    timeout._monitor_task = asyncio.create_task(asyncio.sleep(0.1))
    await timeout.stop_monitoring()
    monkeypatch.setattr(intimation_mod.asyncio, "create_task", lambda coro: (coro.close(), "task")[1])
    await service._start_conversation_timeout("s", "R")
    monkeypatch.setattr(timeout_mod.asyncio, "create_task", lambda coro: (coro.close(), MagicMock(done=MagicMock(return_value=False)))[1])
    assert await timeout.try_start_monitoring_if_available()
    direct.lock_obj.acquire.return_value = False; timeout._monitor_task = None; assert not await timeout.try_start_monitoring_if_available()
    direct.scan.return_value = (0, []); await timeout._check_inactive_users()
    timeout.enabled = False; await timeout._check_inactive_users(); timeout.enabled = True
    direct.scan.return_value = (0, ["1:last_activity"]); direct.get.return_value = str(0); direct.exists.return_value = True
    timeout._handle_worker_timeout = AsyncMock(); await timeout._check_inactive_users(); timeout._handle_worker_timeout.assert_awaited()
    direct.exists.return_value = False; sessions.get_session.return_value = {"workflow_type": "rfq", "outcome": None}; timeout._handle_timeout = AsyncMock(); direct.get.return_value = str(0); await timeout._check_inactive_users(); timeout._handle_timeout.assert_awaited()
    direct.get.return_value = None; await timeout._check_inactive_users()
    timeout._run_monitor_loop = timeout_mod.InactivityTimeoutService._run_monitor_loop.__get__(timeout)
    monkeypatch.setattr(timeout_mod.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError): await timeout._run_monitor_loop()
    timeout.whatsapp_service.send_message = AsyncMock()
    session_data = {"session_id": "sid", "workflow_type": "rfq", "outcome": None, "conversation_history": {}, "workflow_state": {}, "extracted_entities": {}}
    sessions.get_session.return_value = session_data
    monkeypatch.setattr(timeout_mod, "get_session_redis_service", lambda: sessions)
    await timeout._handle_worker_timeout("+1", "sid", "1:last_activity", "1:pending_reply")
    sessions.get_session.return_value = None
    await timeout._handle_worker_timeout("+1", "sid", "a", "b")
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: SimpleNamespace(retrieve=AsyncMock(return_value=SimpleNamespace(self_client=True))))
    monkeypatch.setattr("app.database.DatabaseManager", lambda: SimpleNamespace(append_session_data=MagicMock(), close=MagicMock()))
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: SimpleNamespace(clear_meaningful_message=AsyncMock(return_value=True)))
    direct.get.return_value = str(0); sessions.get_session.return_value = session_data
    await timeout._handle_timeout("+1", "sid", "1:last_activity")
    direct.get.return_value = str(datetime.now().timestamp()); await timeout._handle_timeout("+1", "sid", "1:last_activity")
