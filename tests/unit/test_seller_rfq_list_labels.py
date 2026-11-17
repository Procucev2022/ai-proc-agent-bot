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
