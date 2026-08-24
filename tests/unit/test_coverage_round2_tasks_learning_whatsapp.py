"""Deterministic coverage for task orchestration, learning, and WhatsApp edges."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.services import learning_categorization_service as learning_mod
from app.services import whatsapp_service as whatsapp_mod
from app.services.whatsapp_service import MessageResponse
from app.tasks import taxonomy_build_task as taxonomy
from app.tasks import vector_store_sync_task as vector_sync


class TaskRedis:
    def __init__(self, checkpoint=None):
        self.get = AsyncMock(return_value=checkpoint)
        self.set = AsyncMock()
        self.delete = AsyncMock()


class TaskQuery:
    def __init__(self, *, all_values=(), first_value=None, delete_value=0):
        self.all_values = list(all_values)
        self.first_value = first_value
        self.delete_value = delete_value

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.all_values

    def first(self):
        return self.first_value

    def delete(self, **_kwargs):
        return self.delete_value


class TaskDB:
    def __init__(self, query=None):
        self.query_value = query or TaskQuery()
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def query(self, *_args, **_kwargs):
        return self.query_value

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def install_taxonomy_loader(monkeypatch, process):
    module = types.SimpleNamespace(process_category_mappings=process)
    spec = SimpleNamespace(
        loader=SimpleNamespace(
            exec_module=lambda target: target.__dict__.update(module.__dict__)
        )
    )
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_a: spec)
    monkeypatch.setattr(
        importlib.util,
        "module_from_spec",
        lambda _spec: types.SimpleNamespace(),
    )


# Taxonomy task -------------------------------------------------------------


@pytest.mark.asyncio
async def test_taxonomy_parallel_resume_checkpoint_failure_and_worker_exhaustion(monkeypatch):
    settings = SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1)
    monkeypatch.setattr(taxonomy, "get_settings", lambda: settings)
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"id": i} for i in range(3)])
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())

    redis = TaskRedis(json.dumps({"next_batch": 0, "batches_processed": 0}))
    redis.set.side_effect = RuntimeError("checkpoint write")
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    successful = AsyncMock(return_value={
        "success": True,
        "processed_count": 1,
        "created_categories": 2,
        "existing_categories": 0,
    })
    install_taxonomy_loader(monkeypatch, successful)
    resumed = await taxonomy.build_taxonomy_async(
        None, batch_size=2, process_all=True, parallel=True, resume=True
    )
    assert resumed["success"] is True
    assert resumed["checkpoint_cleared"] is True
    assert successful.await_count == 2
    redis.delete.assert_awaited_once_with("taxonomy_build_checkpoint")

    failing_redis = TaskRedis()
    monkeypatch.setattr(
        taxonomy.AsyncRedisConnectionManager,
        "get_client",
        AsyncMock(return_value=failing_redis),
    )
    failing = AsyncMock(side_effect=RuntimeError("worker failed"))
    install_taxonomy_loader(monkeypatch, failing)
    failed = await taxonomy.build_taxonomy_async(
        None, batch_size=10, process_all=True, parallel=True, resume=False
    )
    assert failed["success"] is False
    assert failed["total_errors"] == 1
    assert failed["batches_processed"] == 0
    assert failing.await_count == 3


@pytest.mark.asyncio
async def test_taxonomy_sequential_multiple_batches_empty_and_outer_errors(monkeypatch):
    settings = SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1)
    monkeypatch.setattr(taxonomy, "get_settings", lambda: settings)
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"id": i} for i in range(3)])
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())
    redis = TaskRedis()
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    process = AsyncMock(side_effect=[
        {"success": True, "processed_count": 2, "created_categories": 1, "existing_categories": 1, "errors": []},
        {"success": False, "error": "bad"},
        {"success": False, "error": "bad"},
        {"success": False, "error": "bad"},
    ])
    install_taxonomy_loader(monkeypatch, process)
    result = await taxonomy.build_taxonomy_async(
        None, batch_size=2, process_all=True, parallel=False, resume=False
    )
    assert result["batches_processed"] == 1
    assert result["total_errors"] >= 1
    assert process.await_count == 4

    monkeypatch.setattr(
        taxonomy.AsyncRedisConnectionManager,
        "get_client",
        AsyncMock(side_effect=RuntimeError("redis")),
    )
    outer = await taxonomy.build_taxonomy_async(None, resume=False)
    assert outer["success"] is False and "redis" in outer["error"]


# Vector-store task ---------------------------------------------------------


def test_vector_sync_outer_timeout_cleanup_and_embedding_error(monkeypatch):
    monkeypatch.setattr(vector_sync, "get_settings", Mock(side_effect=SoftTimeLimitExceeded()))
    timeout = vector_sync.sync_vector_store.run()
    assert timeout["status"] == "timeout"

    monkeypatch.setattr(vector_sync, "get_settings", Mock(side_effect=RuntimeError("settings")))
    failed = vector_sync.sync_vector_store.run()
    assert failed["status"] == "failed" and "settings" in failed["error"]

    database = importlib.import_module("app.database")
    db = TaskDB(TaskQuery())
    db.query = Mock(side_effect=RuntimeError("query"))
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(database, "execute_remote_query", lambda *_a: [{"uuid": "buyer"}])
    cleaned = vector_sync._cleanup_buyer_mappings()
    assert cleaned["success"] is False and "query" in cleaned["error"]
    assert db.rollbacks == 1 and db.closed

    def raising_run(coro):
        coro.close()
        raise RuntimeError("mapping")

    monkeypatch.setattr(asyncio, "run", raising_run)
    mapping = vector_sync._run_seller_category_mapping()
    assert mapping["success"] is False and "mapping" in mapping["error"]

    module = types.ModuleType("create_category_embeddings")
    module.create_unified_vector_store = Mock(side_effect=RuntimeError("chroma"))
    monkeypatch.setitem(sys.modules, "create_category_embeddings", module)
    embedding = vector_sync._run_vector_embedding_creation()
    assert embedding["success"] is False and "chroma" in embedding["error"]


def test_vector_sync_cleanup_failure_and_embedding_exception_statuses(monkeypatch):
    settings = SimpleNamespace(enable_vector_store_sync=True)
    monkeypatch.setattr(vector_sync, "get_settings", lambda: settings)
    monkeypatch.setattr(vector_sync, "_cleanup_buyer_mappings", Mock(side_effect=RuntimeError("cleanup")))
    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", lambda: {"success": True})
    monkeypatch.setattr(vector_sync, "_run_vector_embedding_creation", Mock(side_effect=RuntimeError("embed")))
    result = vector_sync.sync_vector_store.run(cleanup_buyer_mappings=True)
    assert result["status"] == "partial_failure"
    assert result["steps_failed"][0]["step"] == "buyer_mapping_cleanup"
    assert result["steps_failed"][1]["step"] == "vector_embedding_creation"

    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", lambda: {"success": True})
    monkeypatch.setattr(vector_sync, "_run_vector_embedding_creation", lambda **_: {"success": True, "total_items": 4})
    completed = vector_sync.sync_vector_store.run()
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_vector_async_mapping_no_categories_existing_mapping_and_missing_target(monkeypatch):
    database = importlib.import_module("app.database")
    models = importlib.import_module("app.models")
    openai_module = importlib.import_module("app.services.openai_service")
    adapter_module = importlib.import_module("app.services.seller_data_adapter")

    class Category:
        id = SimpleNamespace(__eq__=lambda self, _value: True)

    class Mapping:
        seller_id = SimpleNamespace(__eq__=lambda self, _value: True)
        original_category = SimpleNamespace(__eq__=lambda self, _value: True)

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(models, "LearningCategory", Category)
    monkeypatch.setattr(models, "SellerLearningMapping", Mapping)
    db = TaskDB(TaskQuery(all_values=[]))
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: object())
    seller = SimpleNamespace(seller_id="s1", categories=["tools"])
    monkeypatch.setattr(
        adapter_module,
        "SellerDataAdapter",
        lambda: SimpleNamespace(test_connection=lambda: True, get_sellers_from_remote=lambda: [seller]),
    )
    no_categories = await vector_sync._async_map_sellers_to_categories()
    assert no_categories["success"] is False
    assert no_categories["error"] == "No learning categories found"

    category = SimpleNamespace(id="c1", level_1_category="L1", level_2_category="L2", level_3_category="L3")
    existing_mapping = SimpleNamespace()

    class ExistingDB(TaskDB):
        def __init__(self, mapping_value, learning_value):
            super().__init__()
            self.calls = 0
            self.mapping_value = mapping_value
            self.learning_value = learning_value

        def query(self, model):
            self.calls += 1
            if model is Category:
                return TaskQuery(all_values=[category], first_value=self.learning_value)
            return TaskQuery(first_value=self.mapping_value)

    existing_db = ExistingDB(existing_mapping, category)
    monkeypatch.setattr(database, "get_db_session", lambda: existing_db)
    openai = SimpleNamespace(map_seller_categories_batch=AsyncMock())
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: openai)
    result = await vector_sync._async_map_sellers_to_categories()
    assert result["existing_mappings"] == 1
    assert result["openai_calls"] == 0
    openai.map_seller_categories_batch.assert_not_awaited()

    class MissingTargetDB(ExistingDB):
        def query(self, model):
            self.calls += 1
            if model is Category:
                if self.calls == 1:
                    return TaskQuery(all_values=[category])
                return TaskQuery(first_value=None)
            return TaskQuery(first_value=None)

    missing_db = MissingTargetDB(None, None)
    monkeypatch.setattr(database, "get_db_session", lambda: missing_db)
    openai = SimpleNamespace(map_seller_categories_batch=AsyncMock(return_value={
        "tools": {
            "success": True,
            "selected_category": {"id": "missing"},
            "similarity_score": 0.7,
        }
    }))
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: openai)
    result = await vector_sync._async_map_sellers_to_categories()
    assert result["success"] is True
    assert result["created_mappings"] == 0
    assert result["errors"]


# Learning categorization ---------------------------------------------------


def learning_service():
    return learning_mod.LearningCategorizationService.__new__(learning_mod.LearningCategorizationService)


def test_learning_hierarchy_rules_fuzzy_empty_tokens_and_similarity(monkeypatch):
    service = learning_service()
    empty_db = TaskDB(TaskQuery(all_values=[]))
    assert service._deduplicate_category_hierarchy(empty_db, "A", "B", "C") == {
        "level_1": "A", "level_2": "B", "level_3": "C"
    }

    categories = [
        SimpleNamespace(level_1_category="Parent", level_2_category="Generated", level_3_category="Leaf", usage_frequency=2),
        SimpleNamespace(level_1_category="Dominant", level_2_category="Other", level_3_category="Leaf", usage_frequency=5),
        SimpleNamespace(level_1_category="Parent2", level_2_category="Shared", level_3_category="Leaf", usage_frequency=4),
        SimpleNamespace(level_1_category="Tools", level_2_category="Valves & Fittings", level_3_category="Leaf", usage_frequency=3),
    ]
    db = TaskDB(TaskQuery(all_values=categories))
    rule1 = service._deduplicate_category_hierarchy(db, "Generated", "New", "Leaf2")
    assert rule1 == {"level_1": "Parent", "level_2": "Generated", "level_3": "New"}
    rule2 = service._deduplicate_category_hierarchy(db, "New", "Dominant", "Leaf2")
    assert rule2["level_1"] == "Dominant" and rule2["level_2"] == "New"
    rule3 = service._deduplicate_category_hierarchy(db, "NewParent", "Shared", "Leaf2")
    assert rule3["level_1"] == "Parent2"
    rule4 = service._deduplicate_category_hierarchy(db, "Tools", "Valves", "Leaf2")
    assert rule4["level_2"] == "Valves & Fittings"

    assert service._find_similar_l2("", {"Valves"}, {}, "Tools") is None
    assert service._find_similar_l2(
        "Valves", {"Valves & Fittings"}, {"Valves & Fittings": ("Other", 1)}, "Tools"
    ) == "Valves & Fittings"
    assert service._calculate_keyword_similarity([], ["a"]) == 0.0
    assert service._calculate_keyword_similarity(["a"], []) == 0.0
    assert service._calculate_category_similarity("", "") == 0.0
    assert service._determine_mapping_confidence(0.9) == "high"
    assert service._determine_mapping_confidence(0.7) == "medium"
    assert service._determine_mapping_confidence(0.2) == "low"


def test_learning_existing_backfill_update_and_suggestion_filters(monkeypatch):
    service = learning_service()
    db = TaskDB()
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    service.update_usage_frequency = Mock(return_value=True)
    service.update_client_category = Mock(return_value=True)
    existing = {
        "learning_category_id": "c1",
        "learning_item_id": "i1",
        "client_category_name": "Other",
    }
    service.check_existing_learning_category = Mock(return_value=existing)
    service.openai_service = SimpleNamespace()
    result = asyncio.run(service.create_3_level_category("bolt", "Fasteners"))
    assert result["existing"] is True
    service.update_client_category.assert_called_once_with("i1", "Fasteners")
    assert existing["client_category_name"] == "Fasteners"

    service.update_client_category.reset_mock()
    service.check_existing_learning_category.return_value = {
        "learning_category_id": "c1",
        "learning_item_id": "i1",
        "client_category_name": "Existing",
    }
    result = asyncio.run(service.create_3_level_category("bolt", "Fasteners"))
    assert result["existing"] is True
    service.update_client_category.assert_not_called()

    category = SimpleNamespace(
        id="c1", level_1_category="Tools", level_2_category="Hand", level_3_category="Pliers",
        usage_frequency=2, confidence_score=0.9,
    )
    items = [
        SimpleNamespace(normalized_keywords={"keywords": ["steel"]}, learning_category_id="c1"),
        SimpleNamespace(normalized_keywords={"keywords": ["unrelated", "tokens"]}, learning_category_id="missing"),
        SimpleNamespace(normalized_keywords=["not", "dict"], learning_category_id="c1"),
    ]

    class SuggestDB(TaskDB):
        def query(self, model):
            if model is learning_mod.LearningCategoryItem:
                return TaskQuery(all_values=items)
            if model is learning_mod.LearningCategory:
                return TaskQuery(first_value=category)
            return TaskQuery()

    suggest_db = SuggestDB()
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: suggest_db)
    assert service.get_learning_category_suggestions("steel")
    assert service.get_learning_category_suggestions("the and use") == []


def test_learning_existing_lookup_update_and_logging_failures(monkeypatch):
    service = learning_service()
    category = SimpleNamespace(id="c1", level_1_category="A", level_2_category="B", level_3_category="C", confidence_score=0.8, usage_frequency=1)
    item = SimpleNamespace(id="i1", learning_category_id="c1", item_description="bolt", client_category_name="Other")

    class LookupDB(TaskDB):
        def __init__(self, item_value, category_value):
            super().__init__()
            self.item_value = item_value
            self.category_value = category_value

        def query(self, model):
            if model is learning_mod.LearningCategoryItem:
                return TaskQuery(first_value=self.item_value)
            return TaskQuery(first_value=self.category_value)

    db = LookupDB(item, category)
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    found = service.check_existing_learning_category("bolt")
    assert found["learning_category_id"] == "c1"
    db.item_value = item
    db.category_value = None
    assert service.check_existing_learning_category("bolt") is None
    db.query = Mock(side_effect=RuntimeError("lookup"))
    assert service.check_existing_learning_category("bolt") is None

    db = LookupDB(category, category)
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    assert service.update_usage_frequency("c1") is True
    db.category_value = None
    assert service.update_usage_frequency("c1") is False
    db.query = Mock(side_effect=RuntimeError("usage"))
    assert service.update_usage_frequency("c1") is False

    assert service.update_client_category("i1", "Other") is False
    item.client_category_name = "Existing"
    db = LookupDB(item, None)
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: db)
    assert service.update_client_category("i1", "New") is False
    item.client_category_name = "Other"
    assert service.update_client_category("i1", "New") is True
    db.commit = Mock(side_effect=RuntimeError("commit"))
    assert service.update_client_category("i1", "Newer") is False

    log_db = TaskDB()
    monkeypatch.setattr(learning_mod, "get_db_session", lambda: log_db)
    service._log_learning_categorization("bolt", "c1", "Tools", 0.8)
    assert log_db.commits == 1
    log_db.add = Mock(side_effect=RuntimeError("log"))
    service._log_learning_categorization("bolt", "c1", "Tools", 0.8)
    assert log_db.rollbacks == 1


# WhatsApp service ----------------------------------------------------------


class RetryFake:
    def __init__(self, result=None):
        self.result = result
        self.max_retries = 0
        self.initial_delay = 0

    async def retry_with_backoff(self, operation):
        if self.result is not None:
            return self.result
        try:
            value = await operation()
            return {"success": True, "attempts": 1, "result": value}
        except Exception as exc:
            return {"success": False, "attempts": 1, "error": str(exc)}


def wa_service(monkeypatch, *, mock_mode=False, retry_result=None):
    settings = SimpleNamespace(
        WHATSAPP_USERNAME="user",
        WHATSAPP_PASSWORD="pass",
        WHATSAPP_FROM_NUMBER="from",
        WHATSAPP_BASE_URL="https://wa.test",
        WHATSAPP_TEMPLATE_BASE_URL="https://wa.test/templates",
        WHATSAPP_MOCK_MODE=mock_mode,
        retry_max_attempts=1,
        retry_initial_delay=0,
    )
    retry = RetryFake(retry_result)
    monkeypatch.setattr(whatsapp_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(whatsapp_mod, "get_retry_service", lambda: retry)
    return whatsapp_mod.WhatsAppService()


@pytest.mark.asyncio
async def test_whatsapp_pending_cache_template_and_interactive_error_paths(monkeypatch):
    service = wa_service(monkeypatch)
    redis = SimpleNamespace(delete=AsyncMock(return_value=0), get=AsyncMock(return_value={
        "irrelevant_response": {"user_message": "Earlier"}
    }), set=AsyncMock())
    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: redis)
    await service._clear_pending_reply_flag("+919999999999")
    redis.delete.return_value = 1
    await service._clear_pending_reply_flag("919999999999")
    redis.delete.side_effect = RuntimeError("redis")
    await service._clear_pending_reply_flag("+919999999999")

    posted = SimpleNamespace(status_code=200, text="ok", json=lambda: {"mid": "m"})
    monkeypatch.setattr(whatsapp_mod, "post_to_gateway", AsyncMock(return_value=posted))
    service._clear_pending_reply_flag = AsyncMock()
    result = await service.send_message("+919999999999", "answer")
    assert result.success
    assert redis.set.await_count == 1
    redis.get.return_value = None
    result = await service.send_message("919999999999", "system", clear_pending_reply=False, skip_concatenation=True)
    assert result.success

    invalid_template = await service.send_template_message("12", "welcome", [])
    assert invalid_template.success is False
    retry_failure = wa_service(monkeypatch, retry_result={"success": False, "attempts": 2, "error": "retry"})
    assert (await retry_failure.send_message("919999999999", "x")).success is False

    monkeypatch.setattr(whatsapp_mod, "post_to_gateway", AsyncMock(side_effect=RuntimeError("http")))
    interactive = await service.send_interactive_message("919999999999", "button", {})
    assert interactive.success is False and "http" in interactive.error


@pytest.mark.asyncio
async def test_whatsapp_configurable_buttons_and_formatters_all_optional_paths(monkeypatch):
    service = wa_service(monkeypatch)
    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True, "id"))
    service._track_message_in_history = AsyncMock()
    redis = SimpleNamespace(
        get=AsyncMock(return_value={"irrelevant_response": {"user_message": "Earlier\x00"}}),
        set=AsyncMock(),
        delete=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: redis)

    assert (await service.send_configurable_buttons("919999999999", "Body", [
        {"id": "1", "title": "One"}, {"id": "2", "title": "Two"},
        {"id": "3", "title": "Three"}, {"id": "4", "title": "Four"},
    ], header="H\x01", session_id="sid")).success
    service.send_interactive_message.assert_awaited_once()
    assert service._track_message_in_history.await_count == 1
    assert (await service.send_configurable_buttons("12", "Body", [{"title": "One"}])).success is False
    assert (await service.send_configurable_buttons("919999999999", "Body", [])).success is False
    assert (await service.send_configurable_buttons("919999999999", "Body", [{"id": "1"}])).success is False

    assert service.format_vendor_results([{
        "name": "Vendor", "description": "desc", "contact": "contact", "location": "Pune"
    }]).startswith("*Found Vendors:*")
    assert service.format_bfs_results([{
        "name": "Bolt", "price": 5, "quantity": 3, "description": "steel"
    }]).startswith("*Available Products:*")
    assert service.format_vendor_results([]).startswith("No vendors")
    assert service.format_bfs_results([]).startswith("No products")
    full = service.format_rfq_summary({
        "product_name": "Bolt", "quantity": 2, "unit_of_measure": "box",
        "deadline": "tomorrow", "delivery_city": "Pune", "delivery_state": "MH",
        "division": "D", "specifications": "steel", "remarks": "urgent",
    })
    assert "Additional Notes" in full
    assert "RFQ Summary" in service.format_rfq_summary({})


def test_whatsapp_api_response_and_phone_normalization_edges(monkeypatch):
    service = wa_service(monkeypatch)

    class Response:
        def __init__(self, status, payload, text="body"):
            self.status_code = status
            self._payload = payload
            self.text = text

        def json(self):
            if isinstance(self._payload, BaseException):
                raise self._payload
            return self._payload

    assert service._handle_api_response(Response(200, [{"other": 1}])).success
    assert service._handle_api_response(Response(200, {"other": 1})).success
    assert not service._handle_api_response(Response(500, ValueError("bad"))).success
    bad_text = Response(200, {"mid": "x"})
    bad_text.text = MagicMock(side_effect=RuntimeError("text"))
    assert service._handle_api_response(bad_text).success

    assert service._format_phone_number("") == ""
    assert service._format_phone_number("abc") == ""
    assert service._format_phone_number("9876543210") == "919876543210"
    assert service._format_phone_number("09876543210") == "919876543210"
    assert service._format_phone_number("9198765432101") == "9198765432101"
    assert service._format_phone_number("123456789012") == "123456789012"
    assert service._format_phone_number("123456789") == ""
    assert service._format_phone_number("1" * 16) == ""
    assert service._format_phone_number("1234567890123") == "1234567890123"


@pytest.mark.asyncio
async def test_whatsapp_mock_and_template_success_paths(monkeypatch):
    service = wa_service(monkeypatch, mock_mode=True)
    service._clear_pending_reply_flag = AsyncMock()
    assert (await service.send_message("919999999999", "mock")).message_id == "mock_message_id"
    assert (await service.send_template_message("919999999999", "welcome", ["x"])).message_id == "mock_template_id"
    service._clear_pending_reply_flag.assert_awaited()
