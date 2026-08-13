import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
from app.models import ConversationSession

@pytest.fixture
def mock_dependencies():
    entity_service = MagicMock()
    whatsapp_service = MagicMock()
    whatsapp_service.send_configurable_buttons = AsyncMock()
    whatsapp_service.send_message = AsyncMock()
    cancel_service = MagicMock()
    session_manager = MagicMock()
    session_manager.save_session = AsyncMock()

    return {
        "entity_service": entity_service,
        "whatsapp_service": whatsapp_service,
        "cancel_service": cancel_service,
        "session_manager": session_manager,
    }

@pytest.mark.asyncio
async def test_display_invalid_pincode_message(mock_dependencies):
    handler = SectionedRFQCreationHandler(**mock_dependencies)
    user = MagicMock()
    user.phone_number = "919808494950"
    session = ConversationSession(session_id="test_session", external_user_id="919808494950")
    delivery_data = {"deliveryDate": "15 August", "pincode": "000000", "city": "", "state": ""}

    res = await handler._display_invalid_pincode_message(user, session, delivery_data, "000000")
    assert res == {"status": "validation_error"}
    mock_dependencies["whatsapp_service"].send_configurable_buttons.assert_called_once()
    args, kwargs = mock_dependencies["whatsapp_service"].send_configurable_buttons.call_args
    assert "919808494950" in args
    assert "000000" in args[1]

@pytest.mark.asyncio
async def test_autofill_location_from_pincode_valid(mock_dependencies):
    handler = SectionedRFQCreationHandler(**mock_dependencies)
    delivery_data = {"deliveryDate": "20 August", "pincode": "560037", "city": "", "state": ""}

    with patch("app.services.handlers.sectioned_rfq_creation_handler.get_location_from_pincode_async",
               new=AsyncMock(return_value={"pincode": "560037", "city": "Bengaluru", "state": "Karnataka"})):
        result = await handler._autofill_location_from_pincode(delivery_data)
        assert result["is_valid"] is True
        assert result["delivery_data"]["city"] == "Bengaluru"
        assert result["delivery_data"]["state"] == "Karnataka"

@pytest.mark.asyncio
async def test_autofill_location_from_pincode_invalid(mock_dependencies):
    handler = SectionedRFQCreationHandler(**mock_dependencies)
    delivery_data = {"deliveryDate": "20 August", "pincode": "invalid", "city": "", "state": ""}

    result = await handler._autofill_location_from_pincode(delivery_data)
    assert result["is_valid"] is False
    assert "Invalid pincode format" in result["error"]
