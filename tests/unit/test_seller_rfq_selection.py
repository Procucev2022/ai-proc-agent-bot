"""A seller picking an RFQ to download from the list the bot just showed.

Covers the replies that used to end in "I apologize for the technical issue":
a serial number the list parser missed because the RFQ ID is bold, a failed
credit check read as ``None <= 0``, a "download RFQ" request bouncing between
the selection and general handlers forever, and a "yes" that called a method
which did not exist.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.seller_service import SellerService

RFQS = [
    {"rfq_id": "RFQ261311863724", "delivery_date": "18-Nov-2026", "location": "Karnataka",
     "project_description": "Industrial Water Pump & Allied Items Procurement"},
    {"rfq_id": "RFQ261211766446", "delivery_date": "25-Dec-2026", "location": "Telangana",
     "project_description": "RFQ Items"},
]
CREDITS_OK = {"success": True, "credits_available": 3}
FORWARD_OK = {"success": True, "data": {"success": True, "results": {
    "successful": [{"rfq_id": "RFQ261311863724"}], "failed": []}}}


def make_service(credits=CREDITS_OK, rfq_result=None, extracted=None):
    service = SellerService.__new__(SellerService)
    service.settings = SimpleNamespace(
        rfq_max_allowed=5, procucev_rfq_details_url="https://equa.procucev.com/login",
        support_contact_info="support@procucev.com", support_email="support@procucev.com",
    )
    api = MagicMock()
    api.check_seller_credits = AsyncMock(return_value=credits)
    api.fetch_active_rfqs = AsyncMock(
        return_value=rfq_result or {"success": True, "rfqs": RFQS, "total_count": len(RFQS)})
    api.send_rfq_email = AsyncMock(return_value=FORWARD_OK)
    api.get_subscription_plans = AsyncMock(return_value={"success": True, "plans": []})
    service.seller_api_service = api
    service.openai_service = MagicMock()
    service.openai_service.extract_rfq_ids_from_message = AsyncMock(
        return_value={"success": True, "rfq_ids": extracted or []})
    service.response_helpers = MagicMock()
    service.response_helpers.generate_seller_contextual_response = AsyncMock(
        side_effect=lambda context: f"<{context['workflow_state']}>")
    service.session_manager = MagicMock(save_session=AsyncMock())
    service.whatsapp_service = MagicMock(send_message=AsyncMock())
    return service


def make_session(last_bot_message=None):
    messages = [] if last_bot_message is None else [{"role": "assistant", "content": last_bot_message}]
    return SimpleNamespace(
        conversation_history={"messages": messages},
        workflow_state={"seller_workflow_state": "awaiting_general_response"},
        workflow_type=SimpleNamespace(value="seller_rfq_view"),
    )


USER = SimpleNamespace(id=7, org_id="org-1", email="seller@example.com", phone_number="919999999999")


def listing(service):
    return service._generate_hardcoded_rfq_display(RFQS, len(RFQS), 3)


def forwarded_ids(service):
    return service.seller_api_service.send_rfq_email.call_args.kwargs["rfq_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, expected", [
    ("1", ["RFQ261311863724"]),
    ("2", ["RFQ261211766446"]),
    ("1, 2", ["RFQ261311863724", "RFQ261211766446"]),
])
async def test_serial_number_downloads_the_rfq_at_that_position_of_the_bold_list(reply, expected):
    service = make_service()

    result = await service._handle_rfq_selection_response(USER, make_session(listing(service)), reply)

    assert result["workflow_step"] == "rfq_emails_processed"
    assert forwarded_ids(service) == expected
    service.openai_service.extract_rfq_ids_from_message.assert_not_awaited()


def test_list_parser_reads_bold_and_plain_rfq_ids():
    service = make_service()
    bold = {"content": "1. *RFQ261311863724*\n2. *RFQ261211766446*"}
    plain = {"content": "1. RFQ261311863724\n2. RFQ261211766446"}

    assert service._map_sequence_to_rfq_ids([2], bold) == ["RFQ261211766446"]
    assert service._map_sequence_to_rfq_ids([1], plain) == ["RFQ261311863724"]


@pytest.mark.asyncio
async def test_serial_number_resolves_against_the_rfqs_when_the_list_is_not_the_last_message():
    service = make_service()
    session = make_session("I apologize for the technical issue.")

    await service._handle_rfq_selection_response(USER, session, "2")

    assert forwarded_ids(service) == ["RFQ261211766446"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["RFQ261211766446", "send me rfq 261211766446 please"])
async def test_typed_rfq_id_downloads_that_rfq(reply):
    service = make_service()

    await service._handle_rfq_selection_response(USER, make_session(listing(service)), reply)

    assert forwarded_ids(service) == ["RFQ261211766446"]


@pytest.mark.asyncio
async def test_serial_number_outside_the_list_is_left_to_the_language_model():
    service = make_service(extracted=["RFQ261311863724"])

    await service._handle_rfq_selection_response(USER, make_session(listing(service)), "9")

    service.openai_service.extract_rfq_ids_from_message.assert_awaited_once()
    assert forwarded_ids(service) == ["RFQ261311863724"]


@pytest.mark.asyncio
async def test_processing_acknowledgement_is_sent_before_the_rfq_is_forwarded():
    service = make_service()
    order = []
    service.whatsapp_service.send_message = AsyncMock(side_effect=lambda *a, **k: order.append("ack"))
    service.seller_api_service.send_rfq_email = AsyncMock(
        side_effect=lambda **k: order.append("forward") or FORWARD_OK)

    result = await service._handle_rfq_selection_response(USER, make_session(listing(service)), "1")

    assert order == ["ack", "forward"]
    service.whatsapp_service.send_message.assert_awaited_once_with(USER.phone_number, "<rfq_email_processing>")
    assert result["message"] == "<rfq_email_status_with_errors>"


@pytest.mark.asyncio
async def test_failed_credit_check_still_downloads_instead_of_raising():
    service = make_service(credits={"success": False, "error": "timeout"})

    result = await service._handle_rfq_selection_response(USER, make_session(listing(service)), "1")

    assert result["workflow_step"] == "rfq_emails_processed"
    assert forwarded_ids(service) == ["RFQ261311863724"]


@pytest.mark.asyncio
async def test_confirmed_zero_credits_offers_plans_without_downloading():
    service = make_service(credits={"success": True, "credits_available": 0})

    result = await service._handle_rfq_selection_response(USER, make_session(listing(service)), "1")

    assert result["workflow_step"] == "no_credits_available"
    service.seller_api_service.send_rfq_email.assert_not_awaited()


@pytest.mark.parametrize("credits, exhausted", [
    ({"success": True, "credits_available": 0}, True),
    ({"success": True, "credits_available": None}, True),
    ({"credits_available": 0}, True),
    ({"success": True, "credits_available": 2}, False),
    ({"success": False, "error": "timeout"}, False),
    ({"success": False, "error": "timeout", "credits_available": 0}, False),
])
def test_only_a_successful_check_can_report_no_credits(credits, exhausted):
    assert SellerService._has_no_credits(credits) is exhausted


@pytest.mark.asyncio
async def test_download_request_without_an_rfq_shows_the_list_instead_of_looping(monkeypatch):
    service = make_service()
    monkeypatch.setattr(service, "_classify_seller_intent", AsyncMock(
        return_value={"intent": "rfq_access_request", "confidence": 0.9}))
    selection = AsyncMock(wraps=service._handle_rfq_selection_response)
    monkeypatch.setattr(service, "_handle_rfq_selection_response", selection)

    result = await service._handle_general_seller_response(
        USER, make_session(listing(service)), "I want to download RFQ")

    assert result["workflow_step"] == "invalid_rfq_selection"
    assert "RFQ261311863724" in result["message"]
    assert selection.await_count == 1
    assert selection.await_args.kwargs == {"allow_general_fallback": False}
    service.seller_api_service.send_rfq_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_request_naming_a_serial_downloads_it(monkeypatch):
    service = make_service()
    monkeypatch.setattr(service, "_classify_seller_intent", AsyncMock(
        return_value={"intent": "rfq_access_request", "confidence": 0.9}))

    result = await service._handle_general_seller_response(
        USER, make_session(listing(service)), "download 2")

    assert result["workflow_step"] == "rfq_emails_processed"
    assert forwarded_ids(service) == ["RFQ261211766446"]


@pytest.mark.asyncio
async def test_general_request_with_confirmed_zero_credits_offers_plans(monkeypatch):
    service = make_service(credits={"success": True, "credits_available": 0})
    monkeypatch.setattr(service, "_classify_seller_intent", AsyncMock(
        return_value={"intent": "rfq_access_request", "confidence": 0.9}))

    result = await service._handle_general_seller_response(USER, make_session(listing(service)), "download 1")

    assert result["workflow_step"] == "no_credits_available"


@pytest.mark.asyncio
async def test_yes_to_rfq_access_asks_which_rfq_using_the_org_credits():
    service = make_service()
    session = make_session("Would you like the RFQ details by email?")

    result = await service._handle_contextual_affirmative_response(USER, session, "yes", {})

    assert result["workflow_step"] == "rfq_selection_prompt"
    assert "RFQ261311863724" in result["message"]
    assert session.workflow_state["seller_workflow_state"] == "awaiting_rfq_selection"
    service.seller_api_service.check_seller_credits.assert_awaited_with("org-1")
    service.session_manager.save_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_yes_to_rfq_access_with_zero_credits_offers_plans():
    service = make_service(credits={"success": True, "credits_available": 0})

    result = await service._handle_contextual_affirmative_response(
        USER, make_session("Would you like the RFQ details?"), "yes", {})

    assert result["workflow_step"] == "no_credits_available"


@pytest.mark.asyncio
async def test_selection_prompt_with_no_open_rfqs_says_so():
    service = make_service(rfq_result={"success": True, "rfqs": [], "total_count": 0})
    session = make_session()
    session.workflow_state = None

    result = await service._handle_rfq_selection_prompt(USER, session)

    assert result["message"] == "You don't have any RFQs available at the moment to request."
    assert session.workflow_state["seller_workflow_state"] == "awaiting_rfq_selection"


@pytest.mark.asyncio
async def test_selection_prompt_reports_a_failed_rfq_fetch():
    service = make_service(rfq_result={"success": False, "error": "down"})

    result = await service._handle_rfq_selection_prompt(USER, make_session())

    assert result["workflow_step"] == "rfq_fetch_error"
    service.session_manager.save_session.assert_not_awaited()
