"""
Tests verifying the new WhatsApp Buyer Flow:
- Buyers register and verify accounts through WhatsApp.
- Confirmation messages contain login details and the correct portal link.
- RFQ creation is hidden/disabled on WhatsApp when ENABLE_WHATSAPP_RFQ_CREATION is false.
- All RFQ creation code remains intact and can be re-enabled when the flag is true.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from app.models import WorkflowType
from app.services.chat_service import ChatService
from app.services.profile_selection_service import ProfileSelectionService
from app.services.registration_service import RegistrationService
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler


def fake_user(role="buyer"):
    return SimpleNamespace(
        phone_number="+919876543210",
        id="user-123",
        email="buyer@procucev.com",
        username="buyer_test",
        name="John Doe",
        role=role,
        is_registered=True,
    )


def fake_session():
    return SimpleNamespace(
        session_id="session-123",
        external_user_id="919876543210",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state={},
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
        extracted_entities=[],
        whatsapp_context={},
        retention_date=None,
        last_activity_at=None,
        completed_at=None,
        outcome=None,
    )


@pytest.mark.asyncio
async def test_chat_service_rfq_disabled_button_and_helpers():
    service = object.__new__(ChatService)
    service.settings = SimpleNamespace(
        enable_whatsapp_rfq_creation=False,
        PROCUCEV_PORTAL_URL="https://p2pdevuiindia.azurewebsites.net/login",
    )
    service.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock())
    service.session_manager = SimpleNamespace(save_session=AsyncMock())

    user = fake_user()
    session = fake_session()

    # Test create_rfq button click is intercepted
    button_result = await service._handle_button_response(user, session, "create_rfq")
    assert button_result["status"] == "rfq_creation_disabled"
    assert "Procucev Enterprise website" in button_result["message"]
    assert "rfq@procucev.com" in button_result["message"]
    assert service.whatsapp_service.send_configurable_buttons.called

    # Test menu buttons do not include create_rfq
    menu_buttons = service._get_buyer_menu_buttons()
    button_ids = [b["id"] for b in menu_buttons]
    assert "create_rfq" not in button_ids
    assert "rfq_status" in button_ids
    assert "search_bfs" in button_ids

    # Test buyer profile message includes policy note
    profile_msg = service._get_buyer_profile_message(user.email)
    assert "Important RFQ Policy" in profile_msg
    assert "rfq@procucev.com" in profile_msg


@pytest.mark.asyncio
async def test_purchase_intent_handler_rfq_disabled():
    handler = object.__new__(PurchaseIntentHandler)
    handler.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock())
    handler.settings = SimpleNamespace(
        enable_whatsapp_rfq_creation=False,
        PROCUCEV_PORTAL_URL="https://p2pdevuiindia.azurewebsites.net/login"
    )

    user = fake_user()
    session = fake_session()

    result = await handler.handle_purchase_intent(
        user=user,
        session=session,
        message="I want to buy 50 steel pipes",
        intent_result={"intent": "purchase_intent"}
    )

    assert result["status"] == "rfq_creation_disabled"
    assert "rfq@procucev.com" in result["message"]
    assert "Procucev Enterprise website" in result["message"]
    assert handler.whatsapp_service.send_configurable_buttons.called


@pytest.mark.asyncio
async def test_profile_selection_service_menu_rfq_disabled():
    service = object.__new__(ProfileSelectionService)
    service.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock())
    service.settings = SimpleNamespace(
        enable_whatsapp_rfq_creation=False,
        PROCUCEV_PORTAL_URL="https://p2pdevuiindia.azurewebsites.net/login"
    )
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )

    profile_data = {
        "email": "buyer@procucev.com",
        "role": "buyer",
        "name": "Jane Buyer",
        "company": "Enterprise Inc"
    }
    session = fake_session()

    await service._show_role_based_menu("+919876543210", profile_data, session)
    calls = service.whatsapp_service.send_configurable_buttons.call_args_list
    assert len(calls) == 1
    args, kwargs = calls[0]
    buttons = args[2]
    button_ids = [b["id"] for b in buttons]
    assert "create_rfq" not in button_ids
    assert "rfq_status" in button_ids
    assert "search_bfs" in button_ids
    body_text = args[1]
    assert "Important RFQ Policy" in body_text
    assert "rfq@procucev.com" in body_text


@pytest.mark.asyncio
async def test_registration_service_confirmation_message_contains_login_and_rfq_policy(monkeypatch):
    service = object.__new__(RegistrationService)
    service.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock())
    service.session_manager = SimpleNamespace(save_session=AsyncMock())
    service.settings = SimpleNamespace(
        enable_whatsapp_rfq_creation=False,
        PROCUCEV_PORTAL_URL="https://p2pdevuiindia.azurewebsites.net/login",
        procucev_rfq_details_url="https://p2pdevuiindia.azurewebsites.net/login"
    )
    service._check_exit_command = AsyncMock(return_value=False)
    service.otp_service = SimpleNamespace(
        handle_user_message=AsyncMock(return_value={"status": "otp_valid"})
    )
    service.auth_api_service = SimpleNamespace(
        authenticate_user=AsyncMock(return_value={
            "success": True,
            "data": [{"id": "u1", "username": "buyer@example.com", "role": "buyer", "selfClient": True}]
        })
    )
    service.authentication_service = SimpleNamespace(
        store_user_session=AsyncMock(return_value=True)
    )

    class MockVerificationService:
        def __init__(self, *args, **kwargs):
            pass
        async def _check_domain_approval(self, *args, **kwargs):
            return {"approved": True}

    monkeypatch.setattr("app.services.verification_check_service.VerificationCheckService", MockVerificationService)

    session = fake_session()
    session.workflow_state = {
        "pending_registration_data": {
            "email": "buyer@example.com",
            "name": "Alex Buyer",
            "user_id": "u1"
        },
        "user_type": "buyer"
    }

    result = await service.handle_registration_otp_validation("+919876543210", "123456", session)
    assert result["status"] == "buyer_options_presented"

    calls = service.whatsapp_service.send_configurable_buttons.call_args_list
    assert len(calls) == 1
    args, kwargs = calls[0]
    msg = args[1]
    buttons = args[2]

    # Verify login credentials and portal are sent
    assert "Username / Email:* buyer@example.com" in msg
    assert "Registered Mobile:* +919876543210" in msg
    assert "Website Portal:* https://p2pdevuiindia.azurewebsites.net/login" in msg

    # Verify RFQ policy is sent
    assert "Important RFQ Policy" in msg
    assert "rfq@procucev.com" in msg
    assert "WhatsApp will no longer allow creating RFQs" in msg

    # Verify buttons do not contain create_rfq
    button_ids = [b["id"] for b in buttons]
    assert "create_rfq" not in button_ids
    assert "rfq_status" in button_ids
    assert "search_bfs" in button_ids
