"""A plain "DD/MM/YYYY, pincode" reply must move the RFQ forward without OpenAI.

Production regression: the user answered the delivery prompt with
"01/10/2026,560045", the OpenAI extractor returned nothing, and the bot sent
the same prompt again with no hint that the reply was never read.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
from app.services.handlers.sectioned_rfq_creation_handler import (
    DELIVERY_EXTRACTION_FAILED_MESSAGE,
    SectionedRFQCreationHandler,
    _parse_plain_delivery_reply,
    _validate_plain_delivery_date,
)

FUTURE = date.today() + timedelta(days=30)
FUTURE_TEXT = FUTURE.strftime("%d/%m/%Y")


def session(**state):
    return SimpleNamespace(session_id="sid", workflow_state=dict(state))


def user():
    return SimpleNamespace(phone_number="+91123")


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", lambda s, name: s.workflow_state.get(name))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", lambda s, name, value: s.workflow_state.__setitem__(name, value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_awaiting_section_modification", lambda *_: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification", lambda *_: None)
    h = SectionedRFQCreationHandler.__new__(SectionedRFQCreationHandler)
    h.entity_service = AsyncMock()
    h.whatsapp_service = AsyncMock()
    h.session_manager = AsyncMock()
    h._build_entity_context = lambda s: {}
    h._store_extracted_items = lambda *_: None
    h._validate_delivery_date = AsyncMock(return_value={"is_valid": True, "normalized_date": "from openai"})
    h._autofill_location_from_pincode = AsyncMock(side_effect=lambda data: {
        "delivery_data": {**data, "city": "Bengaluru", "state": "Karnataka"}, "is_valid": True, "error": None,
    })
    h._display_delivery_confirmation = AsyncMock(return_value={"status": "confirmation"})
    h._display_delivery_validation_error = AsyncMock(return_value={"status": "validation"})
    h._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing"})
    return h


@pytest.mark.parametrize("text, expected_date, expected_pincode, delivery_only", [
    ("01/10/2026,560045", date(2026, 10, 1), "560045", True),
    ("Delivery Date 5-11-2026 and Pincode: 411005", date(2026, 11, 5), "411005", True),
    ("560045 by 31.12.2026", date(2026, 12, 31), "560045", True),
    ("laptop 30, 01/10/2026, 560045", date(2026, 10, 1), "560045", False),
    ("31/02/2026, 560045", None, "560045", False),
    ("01/10/2026", date(2026, 10, 1), "", False),
    ("", None, "", False),
    (None, None, "", False),
    ("call 9876543210", None, "", False),
])
def test_parse_plain_delivery_reply(text, expected_date, expected_pincode, delivery_only):
    assert _parse_plain_delivery_reply(text) == {
        "date": expected_date, "pincode": expected_pincode, "delivery_only": delivery_only,
    }


def test_validate_plain_delivery_date():
    assert _validate_plain_delivery_date(date.today())["is_valid"]
    assert _validate_plain_delivery_date(FUTURE)["normalized_date"] == f"{FUTURE.day} {FUTURE.strftime('%B %Y')}"
    past = _validate_plain_delivery_date(date.today() - timedelta(days=1))
    assert past["is_valid"] is False and "past" in past["error"]


@pytest.mark.asyncio
async def test_plain_reply_skips_openai_and_reaches_confirmation(handler):
    s = session()
    result = await handler._handle_date_location_section(user(), s, f"{FUTURE_TEXT},560045", [])

    assert result["status"] == "confirmation"
    handler.entity_service.extract_entities.assert_not_called()
    handler._validate_delivery_date.assert_not_called()
    stored = s.workflow_state["date_location"]
    assert stored["pincode"] == "560045"
    assert stored["deliveryDate"] == f"{FUTURE.day} {FUTURE.strftime('%B %Y')}"
    assert stored["city"] == "Bengaluru"


@pytest.mark.asyncio
async def test_plain_reply_with_past_date_shows_validation_error(handler):
    past = (date.today() - timedelta(days=2)).strftime("%d/%m/%Y")
    result = await handler._handle_date_location_section(user(), session(), f"{past}, 560045", [])

    assert result["status"] == "validation"
    assert "past" in handler._display_delivery_validation_error.await_args.kwargs["date_error"]


@pytest.mark.asyncio
async def test_reply_with_items_still_goes_to_the_extractor(handler):
    # "laptop 30" must reach the extractor; when it fails, the local parse still
    # carries the delivery details so the step moves on.
    handler.entity_service.extract_entities.return_value = {"success": False, "products": []}
    s = session()
    result = await handler._handle_date_location_section(user(), s, f"laptop 30, {FUTURE_TEXT}, 560045", [])

    assert result["status"] == "confirmation"
    handler.entity_service.extract_entities.assert_awaited_once()
    assert s.workflow_state["date_location"]["pincode"] == "560045"


@pytest.mark.asyncio
async def test_local_parse_fills_fields_the_extractor_missed(handler):
    # Pincode only in text: OpenAI is still asked, and its date is kept.
    handler.entity_service.extract_entities.return_value = {"deliveryDate": "tomorrow", "pincode": ""}
    s = session()
    result = await handler._handle_date_location_section(user(), s, "tomorrow at 560045", [])

    assert result["status"] == "confirmation"
    handler._validate_delivery_date.assert_awaited_once_with("tomorrow")
    assert s.workflow_state["date_location"]["pincode"] == "560045"

    # Date only in text and the extractor found nothing: the local date is validated locally.
    handler._validate_delivery_date.reset_mock()
    handler.entity_service.extract_entities.return_value = {"success": False, "products": []}
    result = await handler._handle_date_location_section(user(), session(), f"by {FUTURE_TEXT}", [])

    assert result["status"] == "missing"
    handler._validate_delivery_date.assert_not_called()


@pytest.mark.asyncio
async def test_failed_extraction_tells_the_user_instead_of_repeating_the_prompt(handler):
    handler.entity_service.extract_entities.return_value = {"products": [], "confidence": 0, "success": False}
    result = await handler._handle_date_location_section(user(), session(), "next month to my office", [])

    assert result["status"] == "awaiting_delivery_details"
    assert handler.whatsapp_service.send_configurable_buttons.await_args.args[1] == DELIVERY_EXTRACTION_FAILED_MESSAGE


@pytest.mark.asyncio
async def test_deliberate_rejection_keeps_the_normal_prompt(handler):
    handler.entity_service.extract_entities.return_value = {"success": False, "error_type": "non_procurable", "products": []}
    await handler._handle_date_location_section(user(), session(), "a pet dog", [])

    assert handler.whatsapp_service.send_configurable_buttons.await_args.args[1] == (
        "Please provide your RFQ items Delivery Date and Delivery Location Pincode."
    )
