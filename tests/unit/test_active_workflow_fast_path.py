"""
Unit tests for active workflow intent fast-pathing in IntentService.

Asserts that active multi-step workflows (e.g. sectioned RFQ, seller intimation)
bypass the heavy OpenAI classification prompt while still allowing user exit,
cancel, and help commands.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

import app.services.intent_service as intent_module
from app.models import WorkflowType


@pytest.fixture
def service(monkeypatch):
    ai = MagicMock()
    ai.classify_intent = AsyncMock(return_value={"success": True, "intent": "from_openai", "confidence": 42})
    monkeypatch.setattr(intent_module, "OpenAIService", lambda: ai)
    monkeypatch.setattr(
        intent_module, "get_settings",
        lambda: SimpleNamespace(support_contact_info="help@example.test"),
    )
    return intent_module.IntentService(), ai


@pytest.mark.asyncio
async def test_sectioned_rfq_active_fast_paths_to_buy_something(service):
    intent_service, ai = service

    session = SimpleNamespace(
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={"sectioned_rfq": {"active": True, "current_section": "date_location"}},
    )

    result = await intent_service.classify_intent(
        "20-09-2026\n533431",
        context={"session": session},
        user_phone="919808494950"
    )

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == "buy_something"
    assert result["confidence"] == 99
    assert result["success"] is True


@pytest.mark.asyncio
async def test_active_workflow_respects_exit_keyword(service):
    intent_service, ai = service

    session = SimpleNamespace(
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={"sectioned_rfq": {"active": True, "current_section": "date_location"}},
    )

    result = await intent_service.classify_intent(
        "exit",
        context={"session": session},
        user_phone="919808494950"
    )

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == "exit_system"
    assert result["confidence"] >= 90
    assert result["success"] is True


@pytest.mark.asyncio
async def test_active_workflow_respects_cancel_keyword(service):
    intent_service, ai = service

    session = SimpleNamespace(
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={"sectioned_rfq": {"active": True, "current_section": "date_location"}},
    )

    result = await intent_service.classify_intent(
        "cancel",
        context={"session": session},
        user_phone="919808494950"
    )

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == "cancel_workflow"
    assert result["confidence"] >= 90
    assert result["success"] is True


@pytest.mark.asyncio
async def test_seller_active_workflow_fast_paths_to_sell_something(service):
    intent_service, ai = service

    session = SimpleNamespace(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={},
    )

    result = await intent_service.classify_intent(
        "I can supply 50 units",
        context={"session": session},
        user_phone="919808494950"
    )

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == "sell_something"
    assert result["confidence"] == 99
    assert result["success"] is True


@pytest.mark.asyncio
async def test_legacy_rfq_creation_with_extracted_entities_fast_paths(service):
    intent_service, ai = service

    session = SimpleNamespace(
        workflow_type=WorkflowType.buy_something,
        workflow_state={"extracted_entities": [{"name": "item"}]},
    )

    result = await intent_service.classify_intent(
        "delhi delivery",
        context={"session": session},
        user_phone="919808494950"
    )

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == "buy_something"
    assert result["confidence"] == 99
    assert result["success"] is True


@pytest.mark.asyncio
async def test_empty_session_falls_through_to_openai(service):
    intent_service, ai = service

    session = SimpleNamespace(
        workflow_type=None,
        workflow_state={},
    )

    result = await intent_service.classify_intent(
        "some complex custom query",
        context={"session": session},
        user_phone="919808494950"
    )

    ai.classify_intent.assert_awaited_once()
    assert result["intent"] == "from_openai"
