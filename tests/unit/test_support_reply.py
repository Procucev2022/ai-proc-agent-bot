"""Tapping "Get Support Info" must produce a WhatsApp reply.

The support reply used to be a button message with an empty body; WhatsApp
rejects those, so the seller saw nothing after tapping the button.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import chat_service as chat_mod
from app.services import whatsapp_service as whatsapp_mod
from app.services.whatsapp_service import MessageResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("role, first_button", [("seller", "view_rfqs"), ("other", "contact_support")])
async def test_support_reply_carries_the_contact_details_as_its_body(monkeypatch, role, first_button):
    monkeypatch.setattr(chat_mod, "get_settings", lambda: SimpleNamespace(support_contact_info="support@procucev.com"))
    service = SimpleNamespace(whatsapp_service=MagicMock(send_configurable_buttons=AsyncMock()))
    user = SimpleNamespace(role=role, phone_number="919999999999")

    result = await chat_mod.ChatService._handle_support_request(service, user, "I need support", "sid")

    assert result == {"status": "support_handled"}
    args = service.whatsapp_service.send_configurable_buttons.await_args.args
    assert args[1] == "For any help or queries, please reach our support team at *support@procucev.com*."
    assert args[2][0]["id"] == first_button


def wa_service(monkeypatch):
    settings = SimpleNamespace(
        WHATSAPP_USERNAME="user", WHATSAPP_PASSWORD="pass", WHATSAPP_FROM_NUMBER="from",
        WHATSAPP_BASE_URL="https://wa.test", WHATSAPP_TEMPLATE_BASE_URL="https://wa.test/templates",
        WHATSAPP_MOCK_MODE=False, retry_max_attempts=1, retry_initial_delay=0,
    )
    monkeypatch.setattr(whatsapp_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(whatsapp_mod, "get_retry_service", lambda: MagicMock())
    monkeypatch.setattr(whatsapp_mod, "get_redis_service", lambda: SimpleNamespace(get=AsyncMock(return_value=None)))
    service = whatsapp_mod.WhatsAppService()
    service.send_interactive_message = AsyncMock(return_value=MessageResponse(True, "id"))
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("body, header, expected_body", [
    ("", "What else would you like to do?", "What else would you like to do?"),
    ("   ", None, "How can I help you?"),
])
async def test_empty_button_body_is_never_sent(monkeypatch, body, header, expected_body):
    service = wa_service(monkeypatch)

    result = await service.send_configurable_buttons(
        "919999999999", body, [{"id": "get_support", "title": "Get Support Info"}], header)

    assert result.success
    content = service.send_interactive_message.await_args.args[2]
    assert content["body"]["text"] == expected_body
    assert "header" not in content


@pytest.mark.asyncio
async def test_non_empty_body_keeps_its_header(monkeypatch):
    service = wa_service(monkeypatch)

    await service.send_configurable_buttons("919999999999", "Body", [{"id": "a", "title": "A"}], "Header")

    content = service.send_interactive_message.await_args.args[2]
    assert content["body"]["text"] == "Body"
    assert content["header"] == {"type": "text", "text": "Header"}
