"""
ChromaDB / RAG vector search is removed. The flows that used it keep working
without categories: categorization always reports "no category", BFS search and
seller registration take their existing no-category path, seller matching uses
its standard rules, and the sync tasks keep only their MySQL/OpenAI work.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import config as config_mod
from app.services import auto_categorization_service as auto_cat_mod
from app.services import registration_service as registration_mod
from app.services.handlers import bfs_search_handler as bfs_handler_mod
from app.tasks import daily_category_vector_rebuild_task as rebuild_task
from app.tasks import seller_matching_task as seller_task
from app.tasks import vector_store_sync_task as vector_sync_task

SETTINGS = SimpleNamespace(
    enable_remote_categorization=True,
    enable_daily_category_rebuild=True,
    enable_vector_store_sync=True,
    categorization_init_timeout_seconds=1,
    categorization_item_timeout_seconds=1,
)

UNCATEGORIZED = {
    "success": False,
    "method": "disabled",
    "reason": "categorization_disabled",
    "error": "Item categorization is not available",
    "processing_time_ms": 0,
}


def test_settings_no_longer_expose_vector_search_or_chroma():
    settings = config_mod.Settings()
    for name in ("enable_vector_search", "chroma_host", "chroma_port", "chroma_use_server",
                 "chroma_persist_directory", "chroma_unified_persist_directory"):
        assert not hasattr(settings, name)


@pytest.mark.asyncio
async def test_sync_and_async_getters_share_the_uncategorized_service():
    sync_service = auto_cat_mod.get_auto_categorization_service()
    async_service = await auto_cat_mod.get_auto_categorization_service_async(timeout=0.01)

    assert sync_service is async_service
    assert isinstance(sync_service, auto_cat_mod.UncategorizedCategorizationService)
    result = await sync_service.categorize_item("steel pipe", "user-1", session_id="s", rfq_id="r")
    assert result == UNCATEGORIZED


@pytest.mark.asyncio
async def test_bfs_payload_is_built_without_category():
    handler = bfs_handler_mod.BFSSearchHandler.__new__(bfs_handler_mod.BFSSearchHandler)
    handler.auto_categorization_service = None

    payload = await handler._build_api_payload([{"description": "steel pipe"}], "user-1", "session-1")

    assert payload == [{"category": [], "description": ["steel pipe"]}]


@pytest.mark.asyncio
async def test_seller_registration_completes_without_categories():
    service = registration_mod.RegistrationService.__new__(registration_mod.RegistrationService)
    service.settings = SETTINGS
    service.openai_service = SimpleNamespace(
        parse_seller_product_items=AsyncMock(return_value={"success": True, "items": ["pipes", "valves"]})
    )
    session = SimpleNamespace(session_id="s", workflow_state=None)

    result = await service._categorize_seller_products("pipes and valves", "9100", session)

    assert result == []
    assert session.workflow_state["seller_categorizations"]["total_items"] == 2
    assert session.workflow_state["seller_categorizations"]["successfully_categorized"] == 0


@pytest.mark.asyncio
async def test_seller_matching_uses_standard_rules_only(monkeypatch):
    seller_service = SimpleNamespace(
        select_sellers_for_rfq=AsyncMock(return_value={"subscribed_sellers": [], "unsubscribed_sellers": []})
    )
    monkeypatch.setattr(seller_task, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller_task, "get_users_active_in_last_24hrs", lambda _: set())
    monkeypatch.setattr(seller_task, "get_rfq_item_categories", lambda _: ["Tools"])
    rfq = {"rfq_id": "r", "rfq_uuid": "u", "categories": ["Tools"], "description": "pump",
           "subscribed_notified": 0, "unsubscribed_notified": 0}

    await seller_task.process_single_rfq_matching(rfq, seller_service, set())

    seller_service.select_sellers_for_rfq.assert_awaited_once()
    kwargs = seller_service.select_sellers_for_rfq.await_args.kwargs
    assert kwargs["rfq_data"]["categories"] == ["Tools"]
    assert "candidate_seller_ids" not in kwargs


def test_daily_task_only_syncs_category_mappings(monkeypatch):
    monkeypatch.setattr(rebuild_task, "get_settings", lambda: SETTINGS)
    sync_result = {"success": True, "inserted_count": 1, "total_remote_items": 2, "unique_categories": 1}
    sync = Mock(return_value=sync_result)
    monkeypatch.setattr(rebuild_task, "_sync_category_mappings", sync)

    result = rebuild_task.rebuild_category_vector_store.run()

    assert result["status"] == "completed"
    assert result["category_mappings_sync"] == sync_result
    sync.assert_called_once()


def test_vector_store_sync_only_maps_seller_categories(monkeypatch):
    monkeypatch.setattr(vector_sync_task, "get_settings", lambda: SETTINGS)
    mapping = Mock(return_value={"success": True, "processed_count": 3})
    monkeypatch.setattr(vector_sync_task, "_run_seller_category_mapping", mapping)

    result = vector_sync_task.sync_vector_store.run()

    assert result["status"] == "completed"
    assert result["steps_completed"] == ["seller_category_mapping"]
    assert "steps_skipped" not in result
    mapping.assert_called_once()
    assert not hasattr(vector_sync_task, "_run_vector_embedding_creation")
