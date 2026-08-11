from __future__ import annotations

from app.utils.sectioned_rfq_format_parser import (
    _extract_delivery_format_section,
    _extract_items_format_section,
    _format_quantity,
    _sanitize_text,
    _truncate_text,
    generate_delivery_display,
    generate_delivery_display_with_invalid_pincode,
    generate_delivery_display_with_missing,
    generate_items_display,
    generate_items_display_with_missing,
    parse_delivery_format,
    parse_items_format,
    validate_delivery_completeness,
    validate_items_completeness,
)


def test_delivery_extraction_parse_and_display_branches():
    extracted, remaining = _extract_delivery_format_section("Intro\nDate: 12 Nov 2025\nPincode: 411005\nCity: Pune\nState: MH\nPlease confirm")
    assert "Date" in extracted and remaining == "Please confirm"
    assert _extract_delivery_format_section("nothing") == ("nothing", "")
    parsed = parse_delivery_format("Thanks\nDelivery Date: 12 Nov 2025\nDelivery Pincode: 411005\nDelivery City: Pune\nDelivery State: MH\nQuestion?")
    assert parsed["pincode"] == "411005" and parsed["additional_text"] == "Question?"
    assert "Delivery Date" in parse_delivery_format("Delivery Pincode: 41100")["error"]
    assert "6-digit" in parse_delivery_format("Date: tomorrow\nPincode: 123")["error"]
    assert generate_delivery_display({"deliveryDate": "today", "pincode": "411005"}).startswith("Delivery Date")
    display, missing = generate_delivery_display_with_missing({})
    assert "Please provide" in display and set(missing) == {"Delivery Date", "Delivery Pincode"}
    assert generate_delivery_display_with_missing({"deliveryDate": "today", "pincode": "411005"})[1] == []
    assert "Valid 6-digit" in generate_delivery_display_with_invalid_pincode({"deliveryDate": "today"})
    assert validate_delivery_completeness({"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    assert not validate_delivery_completeness({})


def test_items_parser_extraction_and_validation_branches():
    text = "RFQ Items (2):\nItem 1: Bottle\nQty: 2\nBrand: Cello\nSpecification: UoM: pieces, blue\n\nItem 2: Motor\nQty: 1\nSpecification: Exide\nPlease confirm"
    extracted, remaining = _extract_items_format_section(text)
    assert "Item 1" in extracted and remaining == "Please confirm"
    assert _extract_items_format_section("hello") == ("hello", "")
    parsed = parse_items_format(text)
    assert parsed["items"][0]["description"] == "Bottle"
    assert parsed["items"][0]["unitofMeasures"] == "pieces"
    assert parsed["items"][0]["remarks"] == "blue"
    assert len(parsed["items"]) == 2
    compact = parse_items_format("Item 1: Cable\nQty: 2\nBrand: A\nSpecification: UoM: roll, coated\nItem 2: Plug\nQty: 3")
    assert len(compact["items"]) == 2
    assert "missing" in parse_items_format("Item 1: Cable")["error"]
    assert "missing" in parse_items_format("Qty: 2")["error"]
    assert "No valid" in parse_items_format("RFQ Items (1):")["error"]
    assert parse_items_format("\n")["error"]


def test_item_formatting_missing_fields_truncation_and_completeness():
    assert _format_quantity(None) == ""
    assert _format_quantity(2.0) == "2"
    assert _format_quantity(2.5) == "2.5"
    assert _format_quantity("bad") == "bad"
    assert _sanitize_text("a\x00b\n") == "ab\n"
    assert _sanitize_text(None) == ""
    assert _truncate_text("short", 10) == "short"
    assert _truncate_text("abcdefghijk", 8) == "abcde..."
    products = [{"description": "Laptop", "quantity": 2, "unitofMeasures": "pcs", "brand": "Dell", "remarks": "i7"}]
    assert "Item 1" in generate_items_display(products)
    assert generate_items_display([]) == "No items"
    many = products * 7
    assert "+2 more items" in generate_items_display(many)
    missing_display, labels = generate_items_display_with_missing([{"description": "", "quantity": None}], [{"index": 1, "missing_fields": ["description", "quantity"]}])
    assert "Please provide" in missing_display and labels == ["Product Name", "Quantity"]
    assert generate_items_display_with_missing([], []) == ("No items", [])
    assert validate_items_completeness(products)
    assert not validate_items_completeness([])
    assert not validate_items_completeness([{"description": "x", "quantity": 0}])
