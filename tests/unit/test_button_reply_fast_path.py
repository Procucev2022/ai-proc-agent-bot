"""
Tests for the local button-reply fast path.

Production logs from 2026-08-14 showed every button press paying a full intent
classification: four clicks, each ~2.7s and ~7300 input tokens, whose result was
then discarded because `_handle_button_response` dispatches on the button id
alone.

    openai_service | [TOKEN_USAGE] [INTENT_CLASSIFICATION] ... 2.66s | Input tokens: 7340
    chat_service   | [SECTIONED_RFQ] Button click detected: confirm_date_location

The fast path resolves a mapped id locally. The riskier half of this change is
*which* intent it returns: several checks sit between classification and button
dispatch in `process_message`, and the wrong intent silently hijacks the click.
Those invariants are asserted here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.intent_service as intent_module


# Mirrors the checks in chat_service.process_message that run between
# classification and _handle_button_response.
IRRELEVANT_MESSAGE_INTENTS = ("general_inquiry", "greeting", "support")
HIJACKING_INTENTS = ("exit_system", "cancel_workflow")
# _track_meaningful_message_during_auth_flow stores the raw button payload as
# last_meaningful_message for these, which the create_rfq branch later replays.
MEANINGFUL_INTENTS = (
    "buy_something", "sell_something", "greeting",
    "modification_request", "reference_request", "rfq_status_check",
)


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


# ---------------------------------------------------------------------------
# The API call is skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mapped_button_skips_the_openai_call(service):
    intent_service, ai = service

    result = await intent_service.classify_intent(
        "Confirm", {"button_id": "confirm_date_location"}, user_phone="919808494950"
    )

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == "confirmation_response"
    assert result["confidence"] == 100
    assert result["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("button_id", sorted(intent_module.IntentService.BUTTON_REPLY_INTENTS))
async def test_every_mapped_button_resolves_locally(service, button_id):
    intent_service, ai = service

    result = await intent_service.classify_intent("Any Title", {"button_id": button_id})

    ai.classify_intent.assert_not_awaited()
    assert result["intent"] == intent_module.IntentService.BUTTON_REPLY_INTENTS[button_id]
    assert result["success"] is True
    assert isinstance(result["confidence"], int)


@pytest.mark.asyncio
async def test_the_four_buttons_from_the_incident_are_all_covered(service):
    """confirm_date_location, confirm_items, continue_rfq and confirm_rfq."""
    intent_service, ai = service

    for button_id in ("confirm_date_location", "confirm_items", "continue_rfq", "confirm_rfq"):
        assert (await intent_service.classify_intent("Confirm", {"button_id": button_id}))["success"]

    ai.classify_intent.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unmapped and non-button messages are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmapped_button_still_uses_openai(service):
    """Adding a button must not silently misclassify; it falls through."""
    intent_service, ai = service

    result = await intent_service.classify_intent("Brand New", {"button_id": "something_new"})

    ai.classify_intent.assert_awaited_once()
    assert result["intent"] == "from_openai"


@pytest.mark.asyncio
async def test_restart_and_cancel_buttons_deliberately_stay_on_the_model_path(service):
    """
    These rely on intent-level handling to break out of authentication loops, so
    they are excluded from the map on purpose.
    """
    intent_service, ai = service

    for button_id in ("restart_rfq", "confirm_cancel", "confirm_exit", "get_support", "confirm_email"):
        assert button_id not in intent_module.IntentService.BUTTON_REPLY_INTENTS
        await intent_service.classify_intent("x", {"button_id": button_id})

    assert ai.classify_intent.await_count == 5


@pytest.mark.asyncio
async def test_text_messages_are_untouched(service):
    intent_service, ai = service

    # No button_id at all.
    assert (await intent_service.classify_intent("2 laptops", {}))["intent"] == "from_openai"
    assert (await intent_service.classify_intent("2 laptops", None))["intent"] == "from_openai"
    # An empty id must not be treated as a button.
    assert (await intent_service.classify_intent("2 laptops", {"button_id": ""}))["intent"] == "from_openai"
    assert ai.classify_intent.await_count == 3

    # The existing greeting fast path still wins for typed text.
    ai.classify_intent.reset_mock()
    assert (await intent_service.classify_intent("hi", {}))["intent"] == "greeting"
    ai.classify_intent.assert_not_awaited()


def test_helper_returns_none_without_a_button_id(service):
    intent_service, _ = service
    assert intent_service._classify_button_reply(None) is None
    assert intent_service._classify_button_reply({}) is None
    assert intent_service._classify_button_reply({"button_id": ""}) is None
    assert intent_service._classify_button_reply({"button_id": "not_mapped"}) is None


# ---------------------------------------------------------------------------
# Routing invariants: a mapped intent must not hijack the click
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "button_id, intent",
    sorted(intent_module.IntentService.BUTTON_REPLY_INTENTS.items()),
)
def test_no_mapped_intent_triggers_the_irrelevant_message_flow(button_id, intent):
    """
    chat_service fires handle_irrelevant_message_flow for these intents, which
    generates and caches an out-of-scope reply that is later prepended to the
    next outbound message.
    """
    assert intent not in IRRELEVANT_MESSAGE_INTENTS


@pytest.mark.parametrize(
    "button_id, intent",
    sorted(intent_module.IntentService.BUTTON_REPLY_INTENTS.items()),
)
def test_no_mapped_intent_is_intercepted_before_dispatch(button_id, intent):
    """
    exit_system and cancel_workflow are handled by the exit/cancel checks and the
    authentication orchestrator, which return before the button is dispatched.
    """
    assert intent not in HIJACKING_INTENTS


@pytest.mark.parametrize(
    "button_id, intent",
    sorted(intent_module.IntentService.BUTTON_REPLY_INTENTS.items()),
)
def test_only_menu_entry_buttons_are_tracked_as_meaningful(button_id, intent):
    """
    Confirmation clicks must not be stored as last_meaningful_message and replayed
    later. The create/new/raise RFQ buttons are the deliberate exception, matching
    what the title-based menu fast path already did.
    """
    if intent in MEANINGFUL_INTENTS:
        assert button_id in {"create_rfq", "new_rfq", "raise_rfq"}
        assert intent == "buy_something"


def test_menu_buttons_agree_with_the_existing_title_fast_path():
    """
    "Create new RFQ" already resolved to buy_something through the menu-choice
    fast path, so keying on the id must not change the intent.
    """
    mapping = intent_module.IntentService.BUTTON_REPLY_INTENTS
    for button_id in ("create_rfq", "new_rfq", "raise_rfq"):
        assert mapping[button_id] == "buy_something"


# ---------------------------------------------------------------------------
# chat_service passes the id through
# ---------------------------------------------------------------------------


def _chat_service(monkeypatch, captured):
    """
    Minimal ChatService that runs the real classification block in
    process_message and short-circuits immediately after it.
    """
    import app.services.chat_service as chat_module

    async def fake_build_context(session, content):
        return {"conversation_history": {}}

    monkeypatch.setattr(
        chat_module.ChatServiceHelpers, "build_conversation_context", fake_build_context
    )

    async def fake_classify(content, context, phone=None):
        captured["content"] = content
        captured["context"] = context
        return {"intent": "confirmation_response", "confidence": 100, "success": True}

    session = SimpleNamespace(workflow_type=None, workflow_state={}, external_user_id="919808494950")

    service = chat_module.ChatService.__new__(chat_module.ChatService)
    service._intent_service = SimpleNamespace(classify_intent=fake_classify)
    service.session_manager = MagicMock()
    service.session_manager.get_conversation_context = AsyncMock(return_value=session)
    service.session_manager.add_message_to_history = MagicMock()
    service.session_manager.save_session = AsyncMock()
    service._track_meaningful_message_during_auth_flow = MagicMock()
    # Returning an in-progress auth status makes process_message return right
    # after the classification block, which is all this test needs.
    service.authentication_orchestrator_flow = AsyncMock(return_value={"status": "greeting_handled"})
    return service


@pytest.mark.asyncio
async def test_process_message_forwards_the_button_id(monkeypatch):
    """
    The fast path is only reachable if process_message forwards the id:
    classification_content carries the human-readable title, not the id.
    """
    captured = {}
    service = _chat_service(monkeypatch, captured)

    await service.process_message(
        "919808494950",
        {"type": "button_reply", "button_reply": {"id": "confirm_items", "title": "Confirm"}},
        "interactive",
    )

    assert captured["content"] == "Confirm", "the model still receives the readable title"
    assert captured["context"]["button_id"] == "confirm_items"


@pytest.mark.asyncio
async def test_process_message_adds_no_button_id_for_text(monkeypatch):
    captured = {}
    service = _chat_service(monkeypatch, captured)

    await service.process_message("919808494950", "2 laptops, pincode 251002", "text")

    assert captured["content"] == "2 laptops, pincode 251002"
    assert "button_id" not in captured["context"]
