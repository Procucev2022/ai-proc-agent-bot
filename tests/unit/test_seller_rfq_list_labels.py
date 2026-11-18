"""Labelled RFQ list for sellers on WhatsApp.

The list is a WhatsApp button message, whose body WhatsApp rejects above 1024
characters, and a serial-number reply is resolved by reading the
"1. *RFQ…*" lines back. So the labels must shrink to fit and those lines must
never change.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.procucev_apis.seller_apis as seller_apis
from app.services.seller_service import RFQ_LIST_MAX_CHARS, SellerService

RFQ = {
    "rfq_id": "RFQ261311863724", "delivery_date": "18-Nov-2026", "location": "Karnataka",
    "city": "Bengaluru", "categories": "Pumps & Motors",
    "project_description": "Industrial Water Pump & Allied Items Procurement",
    "items_summary": "Industrial Water Pump (2 Nos) +2 more",
}


def service():
    s = SellerService.__new__(SellerService)
    s.settings = SimpleNamespace(procucev_rfq_details_url="https://qua.procucev.com/login",
                                 support_contact_info="support@procucev.com")
    return s


def rfqs(count, **overrides):
    return [dict(RFQ, rfq_id=f"RFQ26131186372{i}", **overrides) for i in range(count)]


def test_one_rfq_shows_every_label():
    message = service()._generate_hardcoded_rfq_display([RFQ], 1, 5)

    assert "1. *RFQ261311863724*\n" in message
    for line in ("    • *Target Date:* 18-Nov-2026", "    • *Location:* Bengaluru, Karnataka",
                 "    • *Category:* Pumps & Motors",
                 "    • *Project:* Industrial Water Pump & Allied Items Procurement",
                 "    • *Items:* Industrial Water Pump (2 Nos) +2 more"):
        assert line in message
    assert message.startswith("Here are some RFQs available")
    assert "*Available Credit:* 5" in message and "*Total RFQs Available:* 1" in message
    assert message.endswith("to subscribe, view and download more RFQ’s.")


def test_missing_values_are_left_out_not_printed_as_none():
    message = service()._generate_hardcoded_rfq_display(
        [{"rfq_id": "RFQ261311863724", "location": None, "project_description": "Pump"}], 1, 2)

    assert "None" not in message
    assert "Target Date" not in message and "Location" not in message
    assert "    • *Project:* Pump" in message


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_every_list_size_fits_and_serials_still_resolve(count):
    s = service()
    message = s._generate_hardcoded_rfq_display(rfqs(count), count, 5)

    assert len(message) <= RFQ_LIST_MAX_CHARS
    assert "*Target Date:*" in message
    for n in range(1, count + 1):
        assert s._map_sequence_to_rfq_ids([n], {"content": message}) == [f"RFQ26131186372{n - 1}"]


def test_five_rfqs_keep_date_location_and_project():
    message = service()._generate_hardcoded_rfq_display(rfqs(5), 5, 5)

    assert "    • *Target Date:* 18-Nov-2026 | *Location:* Karnataka" in message
    assert "    • *Project:* Industrial Water Pump & All..." in message
    assert "*Category:*" not in message


def test_long_state_names_still_get_labels_within_the_limit():
    message = service()._generate_hardcoded_rfq_display(rfqs(5, location="Andhra Pradesh, India"), 5, 5)

    assert "*Location:* Andhra Pradesh, India" in message and "*Project:*" in message
    assert len(message) <= RFQ_LIST_MAX_CHARS


def test_shortest_level_clips_the_project_hardest():
    assert SellerService._rfq_detail_lines(RFQ, "tightest") == [
        "    • *Target Date:* 18-Nov-2026 | *Location:* Karnataka",
        "    • *Project:* Industrial Wate...",
    ]


def test_original_layout_when_no_labelled_layout_fits():
    long_place = "Karnataka " * 15
    message = service()._generate_hardcoded_rfq_display(rfqs(5, location=long_place), 5, 0)

    assert "*Target Date:*" not in message
    assert f"1. *RFQ261311863720*\n    • 18-Nov-2026, {long_place}\n    • Industrial Water Pump & Allied Items Procurement" in message
    assert "You do not have enough credits" in message


def test_no_rfqs_message_is_unchanged():
    assert service()._generate_hardcoded_rfq_display([], 0, 5).startswith(
        "Currently, there are no active RFQs available in your selected categories.")


# ── full list sent as its own text message ────────────────────────────────

FULL_RFQ = dict(RFQ, pincode="560058",
                project_description="Industrial Water Pump & Allied Items Procurement for the Bengaluru plant")


def test_full_list_puts_every_label_on_its_own_line_in_full():
    list_message, prompt = service()._rfq_list_sections([FULL_RFQ] * 5, 15, 5)

    block = ("1. *RFQ261311863724*\n"
             "- *Target Date:* 18-Nov-2026\n"
             "- *Location:* Bengaluru, Karnataka - 560058\n"
             "- *Category:* Pumps & Motors\n"
             "- *Project:* Industrial Water Pump & Allied Items Procurement for the Bengaluru plant\n"
             "- *Items:* Industrial Water Pump (2 Nos) +2 more")
    assert block in list_message
    assert "..." not in list_message
    assert list_message.startswith("Here are some RFQs available in your selected categories.")
    assert "*Available Credit:* 5\n*Total RFQs Available:* 15\n\n1. *RFQ" in list_message
    assert "\n\n5. *RFQ261311863724*\n" in list_message
    assert prompt.startswith("To download RFQ’s please enter Serial Number/RFQ ID.")
    assert "https://qua.procucev.com/login" in prompt


def test_full_list_serial_numbers_still_resolve():
    s = service()
    list_message, prompt = s._rfq_list_sections(rfqs(5), 5, 5)

    content = f"{list_message}\n\n{prompt}"
    assert s._map_sequence_to_rfq_ids([1, 3, 5], {"content": content}) == [
        "RFQ261311863720", "RFQ261311863722", "RFQ261311863724"]


def test_full_list_intro_follows_credits_and_can_be_skipped():
    no_credit, _ = service()._rfq_list_sections([RFQ], 1, 0)
    skipped, _ = service()._rfq_list_sections([RFQ], 1, 5, skip_intro=True)

    assert no_credit.startswith("You do not have enough credits")
    assert skipped.startswith("*Available Credit:* 5")


def test_full_list_leaves_out_missing_values():
    list_message, _ = service()._rfq_list_sections(
        [{"rfq_id": "RFQ261311863724", "location": None, "pincode": "560058", "project_description": "Pump"}], 1, 2)

    assert "None" not in list_message and "560058" not in list_message
    assert list_message.endswith("1. *RFQ261311863724*\n- *Project:* Pump")


@pytest.mark.parametrize("count, fields, kept", [
    (5, ("project_description", "items_summary"), 120),
    (8, ("project_description", "items_summary", "categories"), 60),
])
def test_full_list_shortens_values_only_when_too_long_for_a_text_message(count, fields, kept):
    huge = [dict(RFQ, **{field: "P" * 900 for field in fields})] * count
    list_message, prompt = service()._rfq_list_sections(huge, count, 5)

    assert len(list_message) + len(prompt) + 2 <= 3800
    assert "- *Project:* " + "P" * kept + "...\n" in list_message


def test_full_list_is_none_without_rfqs():
    assert service()._rfq_list_sections([], 0, 5) is None
    assert service()._rfq_list_sections([RFQ], 0, 5) is None


def test_full_list_text_for_plain_replies_and_its_fallback():
    s = service()
    assert s._full_rfq_list_text([RFQ], 1, 5).endswith("to subscribe, view and download more RFQ’s.")
    assert "- *Target Date:* 18-Nov-2026" in s._full_rfq_list_text([RFQ], 1, 5)
    assert s._full_rfq_list_text(None, 3, 5).startswith("Currently, there are no active RFQs")


@pytest.mark.asyncio
async def test_display_returns_full_list_prompt_and_short_fallback():
    s = service()
    s._fetch_seller_rfqs = AsyncMock(return_value={"success": True, "rfqs": [RFQ], "total_count": 1})
    s._check_seller_credits = AsyncMock(return_value={"success": True, "credits_available": 5})
    s.session_manager = MagicMock(save_session=AsyncMock())
    session = SimpleNamespace(session_id="s1", workflow_type=None, workflow_state={})

    result = await s._display_rfqs_to_seller(SimpleNamespace(org_id="org"), session, "show")

    assert result["workflow_step"] == "display_rfqs_to_seller"
    assert result["message"] == f"{result['rfq_list_message']}\n\n{result['rfq_list_prompt']}"
    assert result["button_fallback_message"] == s._generate_hardcoded_rfq_display([RFQ], 1, 5)
    assert session.workflow_state == {"seller_workflow_state": "awaiting_general_response"}


@pytest.mark.asyncio
async def test_display_without_rfqs_is_unchanged():
    s = service()
    s._fetch_seller_rfqs = AsyncMock(return_value={"success": True, "rfqs": [], "total_count": 0})
    s._check_seller_credits = AsyncMock(return_value={"success": True, "credits_available": 5})
    s.session_manager = MagicMock(save_session=AsyncMock())

    result = await s._display_rfqs_to_seller(SimpleNamespace(org_id="org"),
                                             SimpleNamespace(session_id="s1", workflow_type=None,
                                                             workflow_state={}), "show")

    assert result["message"].startswith("Currently, there are no active RFQs")
    assert "rfq_list_message" not in result


# ── sending: list as text, then the buttons ────────────────────────────────

def chat(send_message):
    from app.services.chat_service import ChatService
    c = ChatService.__new__(ChatService)
    c.whatsapp_service = MagicMock(send_message=send_message)
    return c


FULL_RESULT = {"message": "LIST\n\nPROMPT", "rfq_list_message": "LIST", "rfq_list_prompt": "PROMPT",
               "button_fallback_message": "SHORT"}


@pytest.mark.asyncio
async def test_list_sent_as_text_then_buttons_carry_the_prompt():
    send = AsyncMock(return_value=SimpleNamespace(success=True))
    body = await chat(send)._send_seller_rfq_list("9186", FULL_RESULT, FULL_RESULT["message"])

    send.assert_awaited_once_with("9186", "LIST")
    assert body == "PROMPT"


@pytest.mark.asyncio
@pytest.mark.parametrize("send", [
    AsyncMock(return_value=SimpleNamespace(success=False, error="down")),
    AsyncMock(side_effect=RuntimeError("boom")),
])
async def test_buttons_carry_the_short_list_when_the_text_fails(send):
    assert await chat(send)._send_seller_rfq_list("9186", FULL_RESULT, "LIST\n\nPROMPT") == "SHORT"


@pytest.mark.asyncio
async def test_failed_text_without_fallback_uses_the_message():
    send = AsyncMock(return_value=SimpleNamespace(success=False))
    result = {k: v for k, v in FULL_RESULT.items() if k != "button_fallback_message"}
    assert await chat(send)._send_seller_rfq_list("9186", result, "MSG") == "MSG"


@pytest.mark.asyncio
async def test_other_seller_replies_keep_their_single_button_message():
    send = AsyncMock()
    assert await chat(send)._send_seller_rfq_list("9186", {"message": "Hi"}, "Hi") == "Hi"
    send.assert_not_awaited()


# ── data behind the labels ────────────────────────────────────────────────

@pytest.mark.parametrize("items, expected", [
    ([], ""),
    (None, ""),
    (["junk"], ""),
    ([{"description": "Pump", "quantity": 2, "unitofMeasures": "Nos"}], "Pump (2 Nos)"),
    ([{"description": "Pump", "quantity": 2}], "Pump (2)"),
    ([{"description": "Pump"}, {"description": "Valve"}, {}], "Pump +2 more"),
    ([{"quantity": 3}, {}], "2 items"),
    ([{}], "1 item"),
])
def test_items_summary(items, expected):
    assert seller_apis._items_summary(items) == expected


@pytest.mark.asyncio
async def test_fetched_rfqs_carry_the_extra_fields_and_keep_the_old_ones(monkeypatch):
    client = MagicMock(post=AsyncMock(return_value={"success": True, "data": {"count": 2, "rfqs": [
        {"rfqId": "RFQ1", "deliveryDate": "2026-11-18T00:00:00Z", "projectDesc": "Pumps",
         "clientdeliverylocationrfq": [{"state": "Karnataka", "city": "Bengaluru", "pincode": "560001"}],
         "rfqItem": [{"description": "Pump", "quantity": 2, "unitofMeasures": "Nos", "category": "Pumps"}]},
        {"rfqId": "RFQ2", "category": "Electrical", "rfqItem": [{"description": "Cable"}]},
    ]}}))
    monkeypatch.setattr(seller_apis, "get_procucev_api_client", lambda: client)
    monkeypatch.setattr(seller_apis, "get_settings", lambda: SimpleNamespace())

    result = await seller_apis.SellerAPIService().fetch_active_rfqs("org")

    first, second = result["rfqs"]
    assert first == {
        "rfq_id": "RFQ1", "categories": "Pumps", "delivery_date": "18-Nov-2026",
        "project_description": "Pumps", "location": "Karnataka",
        "city": "Bengaluru", "pincode": "560001", "item_count": 1, "items_summary": "Pump (2 Nos)",
    }
    assert second["categories"] == "Electrical" and second["city"] == "" and second["location"] is None
    assert second["items_summary"] == "Cable" and second["item_count"] == 1
