"""
ENABLE_VECTOR_SEARCH=false must switch off every ChromaDB path without breaking
the flows that use it: nothing loads the embedding model or opens a Chroma client,
categorization reports "disabled", and callers take their existing no-category path.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import config as config_mod
from app.services import auto_categorization_service as auto_cat_mod
from app.services import registration_service as registration_mod
from app.services.handlers import bfs_search_handler as bfs_handler_mod
from app.tasks import auto_categorization_task as auto_cat_task
from app.tasks import category_name_sync_task as category_sync_task
from app.tasks import daily_category_vector_rebuild_task as rebuild_task
from app.tasks import seller_matching_task as seller_task
from app.tasks import vector_store_sync_task as vector_sync_task

DISABLED = SimpleNamespace(
    enable_vector_search=False,
    enable_remote_categorization=True,
    enable_daily_category_rebuild=True,
    enable_vector_store_sync=True,
    categorization_init_timeout_seconds=1,
    categorization_item_timeout_seconds=1,
)


@pytest.fixture
def vector_search_off(monkeypatch):
    """Switch vector search off and fail loudly if anything touches ChromaDB."""
    monkeypatch.setattr(auto_cat_mod, "get_settings", lambda: DISABLED)
    monkeypatch.setattr(auto_cat_mod, "_auto_categorization_service_instance", None)
    real_service = Mock(side_effect=AssertionError("AutoCategorizationService must not be built"))
    monkeypatch.setattr(auto_cat_mod, "AutoCategorizationService", real_service)
    return real_service


@pytest.mark.parametrize("raw, expected", [(None, False), ("true", True), ("FALSE", False), ("no", False)])
def test_settings_reads_enable_vector_search(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ENABLE_VECTOR_SEARCH", raising=False)
    else:
        monkeypatch.setenv("ENABLE_VECTOR_SEARCH", raw)
    assert config_mod.Settings().enable_vector_search is expected


def test_flag_defaults_to_enabled_when_setting_is_absent(monkeypatch):
    monkeypatch.setattr(auto_cat_mod, "get_settings", lambda: SimpleNamespace())
    assert auto_cat_mod.is_vector_search_enabled() is True


def test_sync_getter_returns_disabled_stub_without_loading_model(vector_search_off):
    service = auto_cat_mod.get_auto_categorization_service()
    assert isinstance(service, auto_cat_mod.DisabledCategorizationService)
    vector_search_off.assert_not_called()
    # Nothing is cached, so turning the flag back on still builds the real service.
    assert auto_cat_mod._auto_categorization_service_instance is None


@pytest.mark.asyncio
async def test_async_getter_and_stub_result(vector_search_off):
    service = await auto_cat_mod.get_auto_categorization_service_async(timeout=0.01)
    assert isinstance(service, auto_cat_mod.DisabledCategorizationService)

    result = await service.categorize_item("steel pipe", "user-1", session_id="s", rfq_id="r")
    assert result == {
        "success": False,
        "method": "disabled",
        "reason": "vector_search_disabled",
        "error": "Vector search is disabled",
        "processing_time_ms": 0,
    }
    vector_search_off.assert_not_called()


@pytest.mark.asyncio
async def test_bfs_payload_is_built_without_category(vector_search_off):
    handler = bfs_handler_mod.BFSSearchHandler.__new__(bfs_handler_mod.BFSSearchHandler)
    handler.auto_categorization_service = None

    payload = await handler._build_api_payload([{"description": "steel pipe"}], "user-1", "session-1")

    assert payload == [{"category": [], "description": ["steel pipe"]}]
    vector_search_off.assert_not_called()


@pytest.mark.asyncio
async def test_seller_registration_completes_without_categories(vector_search_off):
    service = registration_mod.RegistrationService.__new__(registration_mod.RegistrationService)
    service.settings = DISABLED
    service.openai_service = SimpleNamespace(
        parse_seller_product_items=AsyncMock(return_value={"success": True, "items": ["pipes", "valves"]})
    )
    session = SimpleNamespace(session_id="s", workflow_state=None)

    result = await service._categorize_seller_products("pipes and valves", "9100", session)

    assert result == []
    assert session.workflow_state["seller_categorizations"]["total_items"] == 2
    assert session.workflow_state["seller_categorizations"]["successfully_categorized"] == 0
    vector_search_off.assert_not_called()


def test_uncategorized_rfq_task_skips_before_touching_chroma(monkeypatch):
    monkeypatch.setattr(auto_cat_task, "get_settings", lambda: DISABLED)
    items = Mock()
    enhanced = Mock()
    monkeypatch.setattr(auto_cat_task, "get_uncategorized_items", items)
    monkeypatch.setattr(auto_cat_task, "EnhancedAutoCategorizationService", enhanced)

    assert auto_cat_task.process_uncategorized_rfqs.run() == {
        "status": "skipped",
        "reason": "vector_search_disabled",
    }
    items.assert_not_called()
    enhanced.assert_not_called()


def test_daily_rebuild_keeps_mysql_sync_and_skips_vector_rebuild(monkeypatch):
    monkeypatch.setattr(rebuild_task, "get_settings", lambda: DISABLED)
    sync_result = {"success": True, "inserted_count": 1, "total_remote_items": 2, "unique_categories": 1}
    sync = Mock(return_value=sync_result)
    monkeypatch.setattr(rebuild_task, "_sync_category_mappings", sync)
    service = Mock(side_effect=AssertionError("vector store must not be rebuilt"))
    monkeypatch.setattr(auto_cat_mod, "AutoCategorizationService", service)

    result = rebuild_task.rebuild_category_vector_store.run()

    assert result["status"] == "skipped"
    assert result["reason"] == "vector_search_disabled"
    assert result["category_mappings_sync"] == sync_result
    sync.assert_called_once()
    service.assert_not_called()


def test_category_name_sync_skips(monkeypatch):
    monkeypatch.setattr(category_sync_task, "get_settings", lambda: DISABLED)
    load = Mock()
    monkeypatch.setattr(category_sync_task, "_load_categories_from_remote", load)

    result = category_sync_task.sync_category_names.run()

    assert result["status"] == "skipped"
    assert result["reason"] == "vector_search_disabled"
    load.assert_not_called()


def test_vector_store_sync_runs_mapping_and_skips_embeddings(monkeypatch):
    monkeypatch.setattr(vector_sync_task, "get_settings", lambda: DISABLED)
    mapping = Mock(return_value={"success": True, "processed_count": 3})
    embeddings = Mock(side_effect=AssertionError("embeddings must not be created"))
    monkeypatch.setattr(vector_sync_task, "_run_seller_category_mapping", mapping)
    monkeypatch.setattr(vector_sync_task, "_run_vector_embedding_creation", embeddings)

    result = vector_sync_task.sync_vector_store.run()

    assert result["status"] == "completed"
    assert result["steps_completed"] == ["seller_category_mapping"]
    assert result["steps_skipped"] == ["vector_embedding_creation"]
    mapping.assert_called_once()
    embeddings.assert_not_called()


@pytest.mark.asyncio
async def test_seller_matching_skips_semantic_phase_and_uses_standard_rules(monkeypatch):
    monkeypatch.setattr(seller_task, "get_settings", lambda: DISABLED)
    enhanced = Mock(side_effect=AssertionError("EnhancedSellerMatchingService must not be built"))
    monkeypatch.setattr("app.services.enhanced_seller_matching_service.EnhancedSellerMatchingService", enhanced)
    seller_service = SimpleNamespace(
        select_sellers_for_rfq=AsyncMock(return_value={"subscribed_sellers": [], "unsubscribed_sellers": []})
    )
    monkeypatch.setattr(seller_task, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller_task, "get_users_active_in_last_24hrs", lambda _: set())
    monkeypatch.setattr(seller_task, "get_rfq_item_categories", lambda _: ["Tools"])
    rfq = {"rfq_id": "r", "rfq_uuid": "u", "categories": ["Tools"], "description": "pump",
           "subscribed_notified": 0, "unsubscribed_notified": 0}

    await seller_task.process_single_rfq_matching(rfq, seller_service, set())

    enhanced.assert_not_called()
    seller_service.select_sellers_for_rfq.assert_awaited_once()
    assert seller_service.select_sellers_for_rfq.await_args.kwargs["candidate_seller_ids"] is None
