"""Replies to "My quoTe Status" when there is nothing (or nothing readable) to report.

A seller with no RFQs tapped the button and got "There seems to be a technical
issue": the reply was always written by the language model, and its failure
text went out even though the only news was "no RFQs yet".
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.rfq_service as rfq_module
from app.schemas.user import UserRole

TECH_ISSUE = ("There seems to be a technical issue at the moment. Our team is working on it. "
              "Please try again later.")
PLACEHOLDER = {"success": True, "data": [{"rfqid": None, "status": "Not Found"}]}


@pytest.fixture
def setup(monkeypatch):
    settings = SimpleNamespace(rfq_max_allowed=5, rfq_followup_note="https://equa.procucev.com/login",
                               support_email="support@procucev.com", support_contact_info="support@procucev.com")
    buyer_api, seller_api, openai, helpers = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    openai.extract_entities = AsyncMock(return_value={})
    helpers.generate_rfq_status_contextual_response = AsyncMock(return_value=TECH_ISSUE)
    seller_api.check_seller_rfq_status = AsyncMock(return_value=PLACEHOLDER)
    buyer_api.get_rfq_status = AsyncMock(return_value=PLACEHOLDER)
    monkeypatch.setattr(rfq_module, "get_settings", lambda: settings)
    monkeypatch.setattr(rfq_module, "RFQAPIService", lambda: buyer_api)
    monkeypatch.setattr(rfq_module, "SellerAPIService", lambda: seller_api)
    monkeypatch.setattr(rfq_module, "OpenAIService", lambda: openai)
    monkeypatch.setattr(rfq_module, "ResponseHelpers", lambda _: helpers)
    return SimpleNamespace(service=rfq_module.RFQService(), buyer_api=buyer_api, seller_api=seller_api,
                           openai=openai, helpers=helpers)


def seller(role="seller"):
    return SimpleNamespace(role=role, id=1, org_id="org-1", email="seller@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["seller", UserRole.SELLER])
async def test_seller_with_no_rfqs_is_told_so_without_asking_the_model(setup, role):
    result = await setup.service.process_rfq_status_request(seller(role), "Check my RFQ status")

    message = result["response_message"]
    assert "haven't requested any RFQs yet" in message
    assert "*seller@example.com*" in message and "Other Active RFQs" in message
    assert "technical issue" not in message
    setup.helpers.generate_rfq_status_contextual_response.assert_not_awaited()
    setup.seller_api.check_seller_rfq_status.assert_awaited_once_with(seller_id="org-1", rfq_ids=[])


@pytest.mark.asyncio
async def test_buyer_with_no_rfqs_is_told_so(setup):
    user = SimpleNamespace(role=UserRole.BUYER, id=9, org_id="o", email=None)
    setup.buyer_api.get_rfq_status = AsyncMock(return_value={"success": True, "data": []})

    result = await setup.service.process_rfq_status_request(user, "status")

    assert result["response_message"].startswith("It looks like you haven't created any RFQs yet with this profile.")
    setup.helpers.generate_rfq_status_contextual_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_requested_rfq_with_nothing_found_names_the_id(setup):
    setup.openai.extract_entities.return_value = {"rfq_id": ["RFQ261311863724"]}

    result = await setup.service.process_rfq_status_request(seller(), "status of RFQ261311863724")

    assert result["status"] == "rfq_status_found"
    assert "*RFQ261311863724*" in result["response_message"]
    setup.helpers.generate_rfq_status_contextual_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_lookup_asks_to_retry_instead_of_reporting_a_technical_issue(setup):
    setup.seller_api.check_seller_rfq_status = AsyncMock(return_value={"success": False, "error": "timeout"})

    result = await setup.service.process_rfq_status_request(seller(), "status")

    assert result["response_message"].startswith("I couldn't fetch your RFQ status right now.")
    setup.helpers.generate_rfq_status_contextual_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_reply", [TECH_ISSUE, None, ""])
async def test_model_failure_with_real_statuses_lists_them_plainly(setup, model_reply):
    setup.helpers.generate_rfq_status_contextual_response.return_value = model_reply
    setup.seller_api.check_seller_rfq_status = AsyncMock(return_value={"success": True, "data": [
        {"rfqid": "RFQ261311863724", "status": "Downloaded"},
        {"rfqid": "RFQ261211766446", "status": None},
        "junk",
    ]})

    result = await setup.service.process_rfq_status_request(seller(), "status")

    assert result["response_message"] == (
        "ℹ️ Here is the status of your recent RFQs:\n\n"
        "*RFQ261311863724* : Downloaded.\n*RFQ261211766446* : Unknown.\n\n"
        "If you are looking for the status of any specific RFQ please provide the RFQID "
        "and I shall fetch the status for the same."
    )


@pytest.mark.asyncio
async def test_model_reply_is_kept_when_it_succeeds(setup):
    setup.helpers.generate_rfq_status_contextual_response.return_value = "*RFQ1* : Quote_Submitted."
    setup.seller_api.check_seller_rfq_status = AsyncMock(return_value={"success": True, "data": [
        {"rfqid": "RFQ1", "status": "Quote_Submitted"}]})

    result = await setup.service.process_rfq_status_request(seller(), "status")

    assert result["response_message"] == "*RFQ1* : Quote_Submitted."


def test_formatter_without_listable_rows_falls_back_to_no_rfqs(setup):
    message = setup.service._format_rfq_statuses([{"id": "x"}], ["RFQ1"], "seller", "s@example.com")
    assert "haven't requested any RFQs yet" in message


def test_formatter_heading_for_requested_ids(setup):
    message = setup.service._format_rfq_statuses([{"rfqid": "RFQ1", "status": "Downloaded"}], ["RFQ1"], "seller", None)
    assert message.startswith("✅ Here is the status of the RFQs you requested:")
