"""
Unit tests for RFQ entity retention across conversation turns.

Verifies that when a user provides product details along with or prior to delivery details
(date and pincode), product details (name, quantity) are preserved and never lost or re-prompted.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from app.models import User, ConversationSession, WorkflowType
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.services.entity_service import EntityService, strip_no_products_sentinel
from app.services.workflow_manager import WorkflowManager


@pytest.mark.asyncio
async def test_strip_no_products_sentinel_case_insensitive():
    """Verify strip_no_products_sentinel removes variations of NO_PRODUCTS_MENTIONED."""
    products = [
        {"description": "NO_PRODUCTS_MENTIONED", "quantity": None},
        {"description": "no_products_mentioned", "quantity": None},
        {"description": "No Products Mentioned", "quantity": None},
        {"description": "laptop", "quantity": 30}
    ]
    cleaned = strip_no_products_sentinel(products)
    assert len(cleaned) == 1
    assert cleaned[0]["description"] == "laptop"
    assert cleaned[0]["quantity"] == 30


@pytest.mark.asyncio
async def test_sectioned_rfq_all_details_provided_together():
    """
    Test scenario: User provides all details in one message: 'laptop 30, 20-09-2026, 560037'.
    Verify product, quantity, delivery date, and pincode are extracted and stored in session.
    """
    entity_service = MagicMock(spec=EntityService)
    whatsapp_service = MagicMock()
    whatsapp_service.send_configurable_buttons = AsyncMock()
    cancel_service = MagicMock()
    session_manager = MagicMock()
    session_manager.save_session = AsyncMock()

    # Mock entity extraction returning both delivery details and product details
    entity_service.extract_entities = AsyncMock(return_value={
        "products": [{"description": "laptop", "quantity": 30}],
        "deliveryDate": "20-09-2026",
        "pincode": "560037",
        "city": "Bengaluru",
        "state": "Karnataka"
    })
    entity_service.openai_service = MagicMock()
    entity_service.openai_service.validate_delivery_date = AsyncMock(return_value={
        "is_valid": True,
        "normalized_date": "20-09-2026"
    })

    handler = SectionedRFQCreationHandler(
        entity_service=entity_service,
        whatsapp_service=whatsapp_service,
        cancel_service=cancel_service,
        session_manager=session_manager
    )

    user = MagicMock(spec=User)
    user.phone_number = "919999999999"

    session = ConversationSession(
        session_id="test_session_1",
        external_user_id="919999999999",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={}
    )
    WorkflowManager.initialize_sectioned_rfq(session)
    WorkflowManager.set_sectioned_rfq_section(session, "date_location")

    # Step 1: User sends message with all details together
    res = await handler.handle_sectioned_rfq(user, session, "laptop 30, 20-09-2026, 560037")

    assert res["status"] == "awaiting_delivery_confirmation"
    stored_delivery = WorkflowManager.get_section_data(session, "date_location")
    assert stored_delivery["pincode"] == "560037"

    stored_items = WorkflowManager.get_section_data(session, "items")
    assert len(stored_items) == 1
    assert stored_items[0]["description"] == "laptop"
    assert stored_items[0]["quantity"] == 30

    # Step 2: User confirms delivery details
    res_confirm = await handler.handle_sectioned_rfq(user, session, "Confirm")
    assert res_confirm["status"] == "awaiting_items_confirmation"

    # Verify items are displayed for confirmation directly without re-extracting or prompting
    whatsapp_service.send_configurable_buttons.assert_called()
    last_call_args = whatsapp_service.send_configurable_buttons.call_args[0]
    message_text = last_call_args[1]
    header = last_call_args[3]

    assert header == "Confirmation Required"
    assert "laptop" in message_text
    assert "NO_PRODUCTS_MENTIONED" not in message_text


@pytest.mark.asyncio
async def test_sectioned_rfq_staged_details_retention():
    """
    Test scenario:
    1. User provides product first ('laptop 30').
    2. System asks for date/pincode.
    3. User provides date/pincode ('560037 31-08-2026').
    4. Delivery details confirmed.
    5. Next section (items) automatically displays existing items for confirmation.
    """
    entity_service = MagicMock(spec=EntityService)
    whatsapp_service = MagicMock()
    whatsapp_service.send_configurable_buttons = AsyncMock()
    cancel_service = MagicMock()
    session_manager = MagicMock()
    session_manager.save_session = AsyncMock()

    # Call 1: Only product extracted
    async def mock_extract(message, context=None, workflow_type="buy_something"):
        if "laptop" in message.lower():
            return {
                "products": [{"description": "laptop", "quantity": 30}],
                "deliveryDate": "",
                "pincode": "",
                "city": "",
                "state": ""
            }
        else:
            # Pincode message - no new products extracted, but context retains items
            existing_items = []
            if context and context.get("workflow_state"):
                existing_items = context["workflow_state"].get("incomplete_products", [])
            return {
                "products": [{"description": "NO_PRODUCTS_MENTIONED"}],
                "deliveryDate": "31-08-2026",
                "pincode": "560037",
                "city": "Bengaluru",
                "state": "Karnataka"
            }

    entity_service.extract_entities = AsyncMock(side_effect=mock_extract)
    entity_service.openai_service = MagicMock()
    entity_service.openai_service.validate_delivery_date = AsyncMock(return_value={
        "is_valid": True,
        "normalized_date": "31-08-2026"
    })

    handler = SectionedRFQCreationHandler(
        entity_service=entity_service,
        whatsapp_service=whatsapp_service,
        cancel_service=cancel_service,
        session_manager=session_manager
    )

    user = MagicMock(spec=User)
    user.phone_number = "919999999999"

    session = ConversationSession(
        session_id="test_session_2",
        external_user_id="919999999999",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={}
    )
    WorkflowManager.initialize_sectioned_rfq(session)
    WorkflowManager.set_sectioned_rfq_section(session, "date_location")

    # Step 1: User provides product info first
    res1 = await handler.handle_sectioned_rfq(user, session, "laptop 30")
    assert res1["status"] == "awaiting_delivery_details"

    # Items stored from step 1
    items1 = WorkflowManager.get_section_data(session, "items")
    assert len(items1) == 1
    assert items1[0]["description"] == "laptop"

    # Step 2: User provides pincode and date
    res2 = await handler.handle_sectioned_rfq(user, session, "560037 31-08-2026")
    assert res2["status"] == "awaiting_delivery_confirmation"

    # Step 3: User confirms delivery details
    res3 = await handler.handle_sectioned_rfq(user, session, "Confirm")
    assert res3["status"] == "awaiting_items_confirmation"

    # Check stored items are still intact
    final_items = WorkflowManager.get_section_data(session, "items")
    assert len(final_items) == 1
    assert final_items[0]["description"] == "laptop"
    assert final_items[0]["quantity"] == 30


@pytest.mark.asyncio
async def test_full_rfq_creation_flow_with_technical_symbols_to_gmt_success():
    """
    Test complete 4-item RFQ creation flow with technical symbols (ohm, +/-, micro).
    Verifies that:
    1. Items with special characters (10KΩ, ±5%, 1000µF) are retained across sections.
    2. Optional attachments section can be skipped with 'Continue'.
    3. Final confirmation delegates to ConfirmationHandler and calls GMT create_rfq API.
    4. Outbound GMT payload is sanitized to valid ASCII (10Kohm, +/-5%, 1000uF).
    5. Success message with RFQ ID is delivered to the user.
    """
    from app.services.handlers.confirmation_handler import ConfirmationHandler
    from app.procucev_apis.rfq_apis import RFQAPIService

    entity_service = MagicMock(spec=EntityService)
    whatsapp_service = MagicMock()
    whatsapp_service.send_message = AsyncMock()
    whatsapp_service.send_configurable_buttons = AsyncMock()
    cancel_service = MagicMock()
    session_manager = MagicMock()
    session_manager.save_session = AsyncMock()

    api_client = MagicMock()
    captured_payloads = []

    async def mock_post(endpoint, json_data, require_auth=True, api_title=""):
        captured_payloads.append(json_data)
        return {
            "statusCode": "200",
            "status": "Success",
            "message": "RFQ created successfully",
            "data": {"rfqId": "RFQ98765"}
        }

    api_client.post = AsyncMock(side_effect=mock_post)
    rfq_api_service = RFQAPIService()
    rfq_api_service.api_client = api_client

    response_helpers = MagicMock()
    response_helpers.generate_rfq_summary_and_confirmation = AsyncMock(return_value="Confirmation Required:\n4 items summary")

    confirmation_handler = ConfirmationHandler(
        whatsapp_service=whatsapp_service,
        response_helpers=response_helpers,
        cancel_service=cancel_service,
        session_manager=session_manager
    )
    confirmation_handler.confirmation_service = MagicMock()
    confirmation_handler.confirmation_service.parse_confirmation = AsyncMock(return_value={
        "is_confirmation": True,
        "action": "confirm"
    })
    confirmation_handler.confirmation_service.analyze_confirmation_context = AsyncMock(return_value={
        "intent_type": "confirmation_response",
        "confidence": 0.95,
        "confirmation_details": {
            "response_type": "accept",
            "has_conditions": False
        }
    })
    confirmation_handler.rfq_service = rfq_api_service

    handler = SectionedRFQCreationHandler(
        entity_service=entity_service,
        whatsapp_service=whatsapp_service,
        cancel_service=cancel_service,
        session_manager=session_manager,
        confirmation_handler=confirmation_handler
    )

    user = MagicMock(spec=User)
    user.id = "user_123"
    user.org_id = "org_456"
    user.phone_number = "919876543210"

    session = ConversationSession(
        session_id="test_session_gmt_full",
        external_user_id="919876543210",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={}
    )
    WorkflowManager.initialize_sectioned_rfq(session)

    # Populate 4 items matching the screenshot with technical symbols
    items = [
        {"description": "LED Bulb", "quantity": 100, "unitofMeasures": "Nos",
         "brand": "Philips", "remarks": "12W, 6500K, B22"},
        {"description": "SMPS", "quantity": 50, "unitofMeasures": "Nos",
         "brand": "", "remarks": "24V, 10A, Industrial Grade"},
        {"description": "Resistor", "quantity": 200, "unitofMeasures": "Nos",
         "brand": "", "remarks": "10K\u03a9, 1/4W, \u00b15%, Through Hole"},
        {"description": "Capacitor", "quantity": 100, "unitofMeasures": "Nos",
         "brand": "", "remarks": "1000\u00b5F, 25V, Electrolytic"}
    ]
    delivery = {
        "deliveryDate": "2026-09-06",
        "pincode": "560021",
        "city": "Bangalore",
        "state": "Karnataka"
    }
    WorkflowManager.update_section_data(session, "items", items)
    WorkflowManager.update_section_data(session, "date_location", delivery)

    # 1. Confirm date_location
    WorkflowManager.set_sectioned_rfq_section(session, "date_location")
    res1 = await handler.handle_section_button_click(user, session, "confirm_date_location")
    assert res1["status"] == "awaiting_items_confirmation"

    # 2. Confirm items
    res2 = await handler.handle_section_button_click(user, session, "confirm_items")
    assert res2["status"] == "awaiting_attachments_decision"

    # 3. Skip optional attachments with 'Continue'
    res3 = await handler.handle_sectioned_rfq(user, session, "Continue")
    assert res3["status"] == "optional_fields_skipped"

    # 4. Final confirmation response -> 'Confirm' button click
    res4 = await confirmation_handler.handle_confirmation_button(user, session, "confirm_rfq")

    # Verify GMT API was called
    assert len(captured_payloads) == 1
    payload = captured_payloads[0]

    # Verify payload is completely valid ASCII
    import json
    payload_str = json.dumps(payload)
    assert payload_str.isascii()

    # Verify technical symbols were properly transliterated
    rfq_items = payload["rfqItem"]
    assert len(rfq_items) == 4
    assert rfq_items[0]["brand"] == "Philips"
    assert rfq_items[0]["remarks"] == "12W, 6500K, B22"
    assert rfq_items[2]["remarks"] == "10Kohm, 1/4W, +/-5%, Through Hole"
    assert rfq_items[3]["remarks"] == "1000uF, 25V, Electrolytic"

    # Verify location and user mapping
    assert payload["clientdeliverylocationrfq"] == [
        {"state": "Karnataka", "city": "Bangalore", "pincode": "560021"}
    ]
    assert payload["createdBy"] == "user_123"
    assert payload["org"] == {"id": "org_456"}

    # Verify user received success message with RFQ ID
    whatsapp_service.send_configurable_buttons.assert_called()
    sent_calls = whatsapp_service.send_configurable_buttons.call_args_list
    sent_msgs = [call.args[1] for call in sent_calls]
    success_msg = next((m for m in sent_msgs if "RFQ created successfully" in m), None)
    assert success_msg is not None
    assert "RFQ98765" in success_msg

