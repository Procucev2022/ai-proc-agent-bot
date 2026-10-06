"""Items written in the prompt's own "Qty N - Item - UoM unit - details" format.

Regression: after "Quantity Required", replying "Qty 25 - Cable - UoM meters - 10 mm"
was rejected as "Format is not valid", because only the copy-paste block format
("Item 1: ... / Qty: ...") was accepted at that point.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.handlers.sectioned_rfq_creation_handler as sectioned_mod
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.utils.sectioned_rfq_format_parser import parse_items_line_format


def item(description, quantity=None, uom="", remarks=""):
    return {"description": description, "quantity": quantity, "brand": "", "remarks": remarks, "unitofMeasures": uom}


@pytest.mark.parametrize("text, expected", [
    ("Qty 25 - Cable - UoM meters - 10 mm thickness", [item("Cable", 25.0, "meters", "10 mm thickness")]),
    ("Qty - Cable - UoM meters - 10 mm", [item("Cable", None, "meters", "10 mm")]),
    ("• Qty: 15 - Laptop - UoM pieces - HP - i7\n\n*Qty 2.5 - Sand*",
     [item("Laptop", 15.0, "pieces", "HP - i7"), item("Sand", 2.5)]),
    ("Qty 3 - T-shirt - Cotton", [item("T-shirt", 3.0, "", "Cotton")]),
])
def test_parse_items_line_format(text, expected):
    assert parse_items_line_format(text) == {"items": expected}


@pytest.mark.parametrize("text", [
    "", None, "\n  \n", "Cable 25 meters", "Qty 25", "Qty 25 -  - UoM m",
    "Qty 25 - Cable\nplease hurry",
])
def test_parse_items_line_format_rejects_other_text(text):
    assert "error" in parse_items_line_format(text)


def test_merge_items_by_name_fills_matches_and_keeps_the_rest():
    existing = [item("Cable", None, "meters", "10 mm"), item("Laptop", 5.0, "pieces")]
    merged = SectionedRFQCreationHandler._merge_items_by_name(
        existing, [item("cable", 25.0), item("Mouse", 2.0)]
    )
    assert merged == [
        {**item("cable", 25.0, "meters", "10 mm")},
        item("Laptop", 5.0, "pieces"),
        item("Mouse", 2.0),
    ]
    assert existing[0]["quantity"] is None  # inputs are not mutated


@pytest.fixture
def state():
    return {}


@pytest.fixture
def handler(monkeypatch, state):
    def get_section(session, name):
        return state.get(name)

    def set_section(session, name, value):
        state[name] = value

    monkeypatch.setattr(sectioned_mod.WorkflowManager, "get_section_data", get_section)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "update_section_data", set_section)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_sectioned_rfq_from_excel", lambda s: False)
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "is_awaiting_section_modification",
                        lambda s, name: state.get("awaiting", False))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "set_awaiting_section_modification",
                        lambda s, name, value: state.__setitem__("awaiting", value))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "reset_section_retry",
                        lambda s, name: state.__setitem__("retry", 0))
    monkeypatch.setattr(sectioned_mod.WorkflowManager, "increment_section_retry",
                        lambda s, name: state.__setitem__("retry", state.get("retry", 0) + 1) or state["retry"])

    h = SectionedRFQCreationHandler.__new__(SectionedRFQCreationHandler)
    h.entity_service = AsyncMock()
    h.whatsapp_service = AsyncMock()
    h.session_manager = AsyncMock()
    h.cancel_service = AsyncMock()
    h._build_entity_context = lambda s: {}
    h._display_items_confirmation = AsyncMock(return_value={"status": "awaiting_items_confirmation"})
    h._display_items_missing_fields = AsyncMock(return_value={"status": "awaiting_missing_item_fields"})
    h._display_item_limit_exceeded = AsyncMock(return_value={"status": "item_limit_exceeded_cancelled"})
    return h


USER = SimpleNamespace(phone_number="+91123")
SESSION = SimpleNamespace(session_id="sid", workflow_state={})


@pytest.mark.asyncio
async def test_line_reply_after_missing_quantity_reaches_confirmation(handler, state):
    state.update(items=[item("Cable", None, "meters", "10 mm")], awaiting=True, retry=1)

    result = await handler._handle_items_section(USER, SESSION, "Qty 25 - Cable - UoM meters - 10 mm thickness", [])

    assert result["status"] == "awaiting_items_confirmation"
    assert state["items"] == [item("Cable", 25.0, "meters", "10 mm thickness")]
    assert state["awaiting"] is False and state["retry"] == 0
    handler.entity_service.extract_entities.assert_not_called()


@pytest.mark.asyncio
async def test_line_reply_still_missing_quantity_asks_again(handler, state):
    state.update(items=[item("Cable", None, "meters")], awaiting=True)

    result = await handler._handle_items_section(USER, SESSION, "Qty - Cable - UoM meters", [])

    assert result["status"] == "awaiting_missing_item_fields"
    assert handler._display_items_missing_fields.await_args.args[3][0]["missing_fields"] == ["quantity"]


@pytest.mark.asyncio
async def test_line_reply_over_item_limit_is_rejected(handler, state, monkeypatch):
    monkeypatch.setattr(sectioned_mod, "MAX_TEXT_INPUT_ITEMS", 1)
    state.update(items=[item("Cable", None)], awaiting=True)

    result = await handler._handle_items_section(USER, SESSION, "Qty 1 - Mouse", [])

    assert result["status"] == "item_limit_exceeded_cancelled"


@pytest.mark.asyncio
async def test_unparseable_reply_keeps_the_format_error(handler, state):
    state.update(items=[item("Cable", None)], awaiting=True)

    result = await handler._handle_items_section(USER, SESSION, "cable twenty five please", [])

    assert result == {"status": "format_error", "retry_count": 1}


@pytest.mark.asyncio
async def test_first_reply_falls_back_to_line_parse_when_extractor_finds_nothing(handler, state):
    handler.entity_service.extract_entities.return_value = {"success": False, "products": []}

    result = await handler._handle_items_section(USER, SESSION, "Qty 25 - Cable - UoM meters - 10 mm", [])

    assert result["status"] == "awaiting_items_confirmation"
    assert state["items"] == [item("Cable", 25.0, "meters", "10 mm")]


@pytest.mark.asyncio
async def test_first_reply_prefers_extractor_items(handler, state):
    extracted = [{"description": "Copper cable", "quantity": 25, "unitofMeasures": "m"}]
    handler.entity_service.extract_entities.return_value = {"products": extracted}

    await handler._handle_items_section(USER, SESSION, "Qty 25 - Cable - UoM meters", [])

    assert state["items"] == extracted
