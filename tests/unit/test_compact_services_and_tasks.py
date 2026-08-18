from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.user import User, UserRole
from app.services.confirmation_service import ConfirmationService
from app.services.faq_service import FAQService
from app.services.vendor_service import VendorService
from app.tasks import task_utils


@pytest.mark.asyncio
async def test_faq_and_confirmation_services(monkeypatch):
    openai = AsyncMock()
    openai.get_completion.return_value = "*hello*\n- world"
    monkeypatch.setattr("app.services.faq_service.OpenAIService", lambda: openai)
    service = FAQService()
    assert service.clean_text("*hi* _there_ #now - item •") == "hi there now item"
    result = await service.get_faq_answer("question", {"messages": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]})
    assert result == "hello world"
    openai.get_completion.side_effect = RuntimeError("openai")
    assert "trouble" in await service.get_faq_answer("question")
    tool = AsyncMock()
    tool.parse_confirmation.return_value = "yes"
    assert await ConfirmationService(tool).parse_confirmation("yes") == "yes"


@pytest.mark.asyncio
async def test_bfs_search_service_paths(monkeypatch):
    categorizer = AsyncMock()
    monkeypatch.setattr(
        "app.services.bfs_search_service.get_auto_categorization_service_async",
        AsyncMock(return_value=categorizer),
    )
    from app.services.bfs_search_service import BFSSearchService

    service = BFSSearchService()
    categorizer.categorize_item.return_value = {"success": True, "category": "steel", "confidence_score": .9, "method": "ai"}
    assert (await service.get_product_category("steel", "u", "s"))["category"] == "steel"
    categorizer.categorize_item.return_value = {"success": False, "reason": "no"}
    assert (await service.get_product_category("steel", "u"))["error"] == "no"
    categorizer.categorize_item.side_effect = RuntimeError("bad")
    assert "bad" in (await service.get_product_category("steel", "u"))["error"]


class Query:
    def __init__(self, vendors):
        self.vendors = vendors
        self.filters = []

    def filter(self, condition):
        self.filters.append(condition)
        return self

    def all(self):
        return self.vendors


class DB:
    def __init__(self, vendors):
        self.query_obj = Query(vendors)

    def query(self, model):
        return self.query_obj


def test_vendor_search_and_relevance(monkeypatch):
    class Column:
        def any(self, value):
            return value

    class FakeVendorModel:
        vendor_services = Column()
        geographic_coverage = Column()

    monkeypatch.setattr("app.services.vendor_service.Vendor", FakeVendorModel)
    vendor = SimpleNamespace(vendor_id="v1", vendor_name="Acme", geographic_coverage=["Pune"], vendor_services=["steel", "tools"])
    service = VendorService(DB([vendor]))
    assert service.search_vendors({}) == []
    assert service.search_vendors({"entities": {}}) == []
    assert service.search_vendors({"entities": {"category": "steel", "location": "Pune"}})[0]["relevance_score"] == 85.0
    assert service.search_bfs_inventory({"entities": {"category": "steel"}})
    assert len(service.get_vendor_recommendations({"entities": {"location": "Pune"}})) == 1
    assert service._calculate_relevance(SimpleNamespace(vendor_services=["steel"], geographic_coverage=["Pune City"]), {"location": "pune"}) == 18.0
    assert service.update_vendor_learning("r", "v", []) is None


def test_task_utils_database_and_input_branches(monkeypatch):
    assert task_utils.normalize_phone_for_comparison(None) == ""
    assert task_utils.normalize_phone_for_comparison("++123") == "123"
    assert task_utils.get_users_active_in_last_24hrs([]) == set()
    monkeypatch.setattr(task_utils, "get_db_session", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    assert task_utils.get_users_active_in_last_24hrs(["+123"]) == set()
    assert not task_utils.is_user_active_in_last_24hrs("")
    original_activity_lookup = task_utils.get_users_active_in_last_24hrs
    monkeypatch.setattr(task_utils, "get_users_active_in_last_24hrs", lambda values: {"123"})
    assert task_utils.is_user_active_in_last_24hrs("+123")
    monkeypatch.setattr(task_utils, "get_users_active_in_last_24hrs", original_activity_lookup)

    class FakeDB:
        def execute(self, query, params):
            return self
        def fetchall(self):
            return [("123",), (None,), ("456",)]
        def close(self):
            pass

    db = FakeDB()
    monkeypatch.setattr(task_utils, "get_db_session", lambda: db)
    assert task_utils.get_users_active_in_last_24hrs(["+123", "456"]) == {"123", "456"}
