"""Targeted unit tests for SectionedRFQCreationHandler to achieve >95% branch coverage."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.workflow_manager import WorkflowManager


def make_user():
    return SimpleNamespace(
        id=1,
        phone_number="+919876543210",
        name="Test User",
        company_name="Test Org",
        pincode="560001",
        email="test@example.com",
    )


def make_session():
    return SimpleNamespace(
        session_id="test-branch-session",
        phone_number="+919876543210",
        workflow_type=None,
        workflow_state={},
        product_items=[],
        rfq_ids=[],
        outcome=None,
        completed_at=None,
        user_type=None,
        conversation_history=[],
    )


@pytest.mark.asyncio
async def test_usable_items_variations():
    assert SectionedRFQCreationHandler._usable_items(None) == []
    assert SectionedRFQCreationHandler._usable_items([]) == []

    items = [
        "not-a-dict",
        {"description": "NO_PRODUCTS_MENTIONED"},
        {"description": "NONE MENTIONED"},
        {"description": "   "},
        {"description": "Steel Rod", "quantity": 10},
        {"brand": "Tata"},
    ]
    usable = SectionedRFQCreationHandler._usable_items(items)
    assert len(usable) == 2
    assert usable[0]["description"] == "Steel Rod"
    assert usable[1]["brand"] == "Tata"


@pytest.mark.asyncio
async def test_handle_sectioned_rfq_flow_confirm_and_modify_branches():
    entity_service = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp_service = AsyncMock()
    cancel_service = SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock())
    session_manager = SimpleNamespace(save_session=AsyncMock())
    confirmation_handler = SimpleNamespace(handle_pending_confirmations=AsyncMock(return_value={"status": "confirmed"}))

    handler = SectionedRFQCreationHandler(
        entity_service, whatsapp_service, cancel_service, session_manager, confirmation_handler
    )
    user = make_user()

    # 1. Confirm on items section
    session = make_session()
    WorkflowManager.initialize_sectioned_rfq(session)
    WorkflowManager.set_sectioned_rfq_section(session, "items")
    WorkflowManager.update_section_data(session, "items", [{"description": "Box", "quantity": 1}])
    handler._handle_section_confirm = AsyncMock(return_value={"status": "confirmed_items"})
    res = await handler.handle_sectioned_rfq(user, session, "confirm", [])
    assert res["status"] == "confirmed_items"

    # 2. Modify on items section
    handler._handle_section_modify = AsyncMock(return_value={"status": "modifying_items"})
    res = await handler.handle_sectioned_rfq(user, session, "modify", [])
    assert res["status"] == "modifying_items"

    # 3. Modify on date_location section
    WorkflowManager.set_sectioned_rfq_section(session, "date_location")
    WorkflowManager.update_section_data(session, "date_location", {
        "deliveryDate": "10-10-2026",
        "pincode": "560001",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    handler._handle_section_modify = AsyncMock(return_value={"status": "modifying_date"})
    res = await handler.handle_sectioned_rfq(user, session, "change", [])
    assert res["status"] == "modifying_date"

    # 4. Modify skipped when is_excel_source is True
    session.workflow_state["excel_source"] = True
    handler._handle_date_location_section = AsyncMock(return_value={"status": "routed_date"})
    res = await handler.handle_sectioned_rfq(user, session, "modify", [])
    assert res["status"] == "routed_date"
    assert session.workflow_state.get("excel_source") is False

    # 5. Unknown section error
    WorkflowManager.set_sectioned_rfq_section(session, "unknown_random_section")
    res = await handler.handle_sectioned_rfq(user, session, "hello", [])
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_handle_date_location_structured_and_validation_branches():
    entity_service = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp_service = AsyncMock()
    cancel_service = SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock())
    session_manager = SimpleNamespace(save_session=AsyncMock())
    confirmation_handler = SimpleNamespace(handle_pending_confirmations=AsyncMock())

    handler = SectionedRFQCreationHandler(
        entity_service, whatsapp_service, cancel_service, session_manager, confirmation_handler
    )
    user = make_user()

    # 1. Complete delivery data + structured format keyword
    session = make_session()
    WorkflowManager.initialize_sectioned_rfq(session)
    WorkflowManager.update_section_data(session, "date_location", {
        "deliveryDate": "10-10-2026",
        "pincode": "560001",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    handler._process_delivery_modification_direct = AsyncMock(return_value={"status": "direct_mod_complete"})
    res = await handler._handle_date_location_section(user, session, "delivery date: 12-10-2026", [])
    assert res["status"] == "direct_mod_complete"

    # 2. Incomplete delivery data + structured format keyword
    session2 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session2)
    WorkflowManager.update_section_data(session2, "date_location", {
        "deliveryDate": "10-10-2026",
        "pincode": "",
    })
    res = await handler._handle_date_location_section(user, session2, "delivery pincode: 560002", [])
    assert res["status"] == "direct_mod_complete"

    # 3. Extraction with date and pincode validation errors
    session3 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session3)
    entity_service.extract_entities.return_value = {
        "deliveryDate": "invalid-date",
        "pincode": "123",
        "city": "",
        "state": "",
    }
    handler._validate_delivery_date = AsyncMock(return_value={"is_valid": False, "error": "Bad date format"})
    handler._autofill_location_from_pincode = AsyncMock(return_value={
        "delivery_data": {"deliveryDate": "", "pincode": "123", "city": "", "state": ""},
        "is_valid": False,
        "error": "Bad pincode",
    })
    handler._display_delivery_validation_error = AsyncMock(return_value={"status": "validation_error_displayed"})
    res = await handler._handle_date_location_section(user, session3, "need item by tomorrow 123", [])
    assert res["status"] == "validation_error_displayed"

    # 4. Partial delivery data display missing fields
    session4 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session4)
    WorkflowManager.update_section_data(session4, "date_location", {
        "deliveryDate": "15-10-2026",
        "pincode": "",
    })
    handler._display_delivery_missing_fields = AsyncMock(return_value={"status": "missing_fields"})
    res = await handler._handle_date_location_section(user, session4, "", [])
    assert res["status"] == "missing_fields"


@pytest.mark.asyncio
async def test_handle_items_section_structured_limit_and_merge():
    entity_service = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp_service = AsyncMock()
    cancel_service = SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock())
    session_manager = SimpleNamespace(save_session=AsyncMock())
    confirmation_handler = SimpleNamespace(handle_pending_confirmations=AsyncMock())

    handler = SectionedRFQCreationHandler(
        entity_service, whatsapp_service, cancel_service, session_manager, confirmation_handler
    )
    user = make_user()

    # 1. Awaiting modification
    session = make_session()
    WorkflowManager.initialize_sectioned_rfq(session)
    WorkflowManager.set_awaiting_section_modification(session, "items", True)
    handler._process_items_modification_direct = AsyncMock(return_value={"status": "items_mod_direct"})
    res = await handler._handle_items_section(user, session, "modify item 1", [])
    assert res["status"] == "items_mod_direct"

    # 2. Existing items + structured format keyword
    session2 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session2)
    WorkflowManager.update_section_data(session2, "items", [{"description": "Pump", "quantity": 2}])
    res = await handler._handle_items_section(user, session2, "Item 1 Qty: 4", [])
    assert res["status"] == "items_mod_direct"

    # 3. Text item limit exceeded on initial extraction
    session3 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session3)
    many_products = [{"description": f"Item {i}", "quantity": 1} for i in range(15)]
    entity_service.extract_entities.return_value = {"products": many_products}
    handler._display_item_limit_exceeded = AsyncMock(return_value={"status": "limit_exceeded"})
    res = await handler._handle_items_section(user, session3, "lots of items", [])
    assert res["status"] == "limit_exceeded"

    # 4. Merging new items with existing items and limit check
    session4 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session4)
    WorkflowManager.update_section_data(session4, "items", [{"description": "Bolt", "quantity": 10}])
    entity_service.extract_entities.return_value = {"products": many_products}
    res = await handler._handle_items_section(user, session4, "also add many items", [])
    assert res["status"] == "limit_exceeded"

    # 5. Normal merge of new fields into existing items
    session5 = make_session()
    WorkflowManager.initialize_sectioned_rfq(session5)
    WorkflowManager.update_section_data(session5, "items", [{"description": "Bolt", "quantity": 10}])
    entity_service.extract_entities.return_value = {
        "products": [{"description": "Bolt", "quantity": 20, "brand": "Standard"}]
    }
    handler._display_items_confirmation = AsyncMock(return_value={"status": "items_confirmed"})
    res = await handler._handle_items_section(user, session5, "make it 20 bolts brand standard", [])
    assert res["status"] == "items_confirmed"
    updated_items = WorkflowManager.get_section_data(session5, "items")
    assert updated_items[0]["quantity"] == 20
    assert updated_items[0]["brand"] == "Standard"


@pytest.mark.asyncio
async def test_handle_final_confirmation_and_helpers():
    entity_service = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp_service = AsyncMock()
    cancel_service = SimpleNamespace(
        _clear_workflow_state=AsyncMock(),
        _send_cancellation_message=AsyncMock(),
        handle_cancel_intent=AsyncMock(return_value={"status": "cancelled_aborted"}),
    )
    session_manager = SimpleNamespace(save_session=AsyncMock())
    confirmation_handler = SimpleNamespace(handle_pending_confirmations=AsyncMock(return_value={"status": "pending_handled"}))

    handler = SectionedRFQCreationHandler(
        entity_service, whatsapp_service, cancel_service, session_manager, confirmation_handler
    )
    user = make_user()

    # Final confirmation builds pending_combined_rfq if missing
    session = make_session()
    WorkflowManager.initialize_sectioned_rfq(session)
    WorkflowManager.update_section_data(session, "date_location", {
        "deliveryDate": "20-10-2026",
        "pincode": "560001",
        "city": "Bengaluru",
        "state": "Karnataka",
    })
    WorkflowManager.update_section_data(session, "items", [{"description": "Gear", "quantity": 5}])
    res = await handler._handle_final_confirmation(user, session, "looks good")
    assert res["status"] == "pending_handled"
    assert session.workflow_state.get("pending_combined_rfq") is not None

    # Restart button clicked and aborted on date_location redisplays delivery confirmation
    WorkflowManager.set_sectioned_rfq_section(session, "date_location")
    handler._display_delivery_confirmation = AsyncMock(return_value={"status": "delivery_redisplayed"})
    res = await handler._handle_restart_rfq(user, session)
    assert res["status"] == "delivery_redisplayed"

    # Restart button clicked and aborted on items redisplays items confirmation
    WorkflowManager.set_sectioned_rfq_section(session, "items")
    handler._display_items_confirmation = AsyncMock(return_value={"status": "items_redisplayed"})
    res = await handler._handle_restart_rfq(user, session)
    assert res["status"] == "items_redisplayed"

    # Attachment decision yes
    res = await handler._handle_attachment_decision(user, session, "attachments_yes")
    assert res["status"] == "awaiting_attachments"
    whatsapp_service.send_message.assert_awaited()

    # Missing fields messages
    incomplete_items = [
        {"index": 1, "item": {"description": "Part"}, "missing_fields": ["quantity"]},
        {"index": 2, "item": {}, "missing_fields": ["description"]},
    ]
    incomplete_msg = handler._generate_missing_items_fields_message(incomplete_items, [])
    assert "Quantity" in incomplete_msg
    assert "Product description" in incomplete_msg

    del_msg1 = handler._generate_delivery_missing_fields_message({"deliveryDate": "20-10-2026"})
    assert "Delivery Pincode" in del_msg1

    del_msg2 = handler._generate_delivery_missing_fields_message({"pincode": "560001"})
    assert "Delivery Date" in del_msg2

    del_msg3 = handler._generate_delivery_missing_fields_message({"deliveryDate": "20-10-2026", "pincode": "560001"})
    assert "Let me fetch the location details" in del_msg3

    # _get_next_section
    assert handler._get_next_section("date_location") == "items"
    assert handler._get_next_section("items") == "attachments"
    assert handler._get_next_section("attachments") == "final_confirmation"
    assert handler._get_next_section("final_confirmation") is None
    assert handler._get_next_section("nonexistent") is None

    # _initiate_next_section with BFS products
    session_bfs = make_session()
    WorkflowManager.initialize_sectioned_rfq(session_bfs)
    session_bfs.workflow_state["bfs_rfq_products"] = ["Steel Pipe", "Copper Wire"]
    handler._handle_items_section = AsyncMock(return_value={"status": "bfs_items_handled"})
    res = await handler._initiate_next_section(user, session_bfs, "items")
    assert res["status"] == "bfs_items_handled"

    # _initiate_next_section empty items
    session_empty = make_session()
    WorkflowManager.initialize_sectioned_rfq(session_empty)
    res = await handler._initiate_next_section(user, session_empty, "items")
    assert res["status"] == "awaiting_items"


@pytest.mark.asyncio
async def test_autofill_location_from_pincode_branches():
    entity_service = SimpleNamespace(extract_entities=AsyncMock())
    whatsapp_service = AsyncMock()
    cancel_service = SimpleNamespace(_clear_workflow_state=AsyncMock(), _send_cancellation_message=AsyncMock())
    session_manager = SimpleNamespace(save_session=AsyncMock())
    confirmation_handler = SimpleNamespace(handle_pending_confirmations=AsyncMock())

    handler = SectionedRFQCreationHandler(
        entity_service, whatsapp_service, cancel_service, session_manager, confirmation_handler
    )

    # Invalid pincode format (e.g. letters / wrong length)
    res1 = await handler._autofill_location_from_pincode({"pincode": "abc"})
    assert not res1["is_valid"]
    assert "Invalid pincode format" in res1["error"]

    # Location lookup returns None
    with patch("app.services.handlers.sectioned_rfq_creation_handler.get_location_from_pincode_async", AsyncMock(return_value=None)):
        res2 = await handler._autofill_location_from_pincode({"pincode": "999999"})
        assert not res2["is_valid"]
        assert "Could not find location" in res2["error"]

    # Location lookup succeeds with city and state
    with patch("app.services.handlers.sectioned_rfq_creation_handler.get_location_from_pincode_async", AsyncMock(return_value={"city": "Mumbai", "state": "Maharashtra"})):
        res3 = await handler._autofill_location_from_pincode({"pincode": "400001"})
        assert res3["is_valid"]
        assert res3["delivery_data"]["city"] == "Mumbai"
        assert res3["delivery_data"]["state"] == "Maharashtra"
