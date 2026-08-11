from datetime import datetime

from app.utils.rfq_message_formatter import (
    _format_quantity,
    format_excel_validation_error_message,
    format_rfq_response_message,
    format_simple_missing_fields_message,
)


def test_rfq_formatter_items_dates_locations_and_questions():
    assert _format_quantity(None) == ""
    assert _format_quantity(2.0) == "2"
    assert _format_quantity(2.5) == "2.5"
    assert _format_quantity("x") == "x"
    entities = [{"description": "laptop", "quantity": 2, "unitofMeasures": "pcs", "brand": "Dell", "remarks": "i7"}, {"brand": "Acme", "quantity": 3, "remarks": "blue"}, {"description": ""}]
    fields = {"deliveryDate": datetime(2025, 11, 5), "city": "Pune", "state": "MH", "pincode": "411005"}
    message = format_rfq_response_message(entities, fields, ["What is the required delivery date?", "Where should the items be delivered?"])
    assert "2 pcs Laptop" in message and "5th Nov 2025" in message and "Delivery Location" in message
    assert "Delivery Date" in message and "Pincode" in message
    assert "Thank" not in message
    string_date = format_rfq_response_message([], {"deliveryDate": "2025-01-02"}, [], show_only_collected=True)
    assert "2nd Jan 2025" in string_date
    invalid_date = format_rfq_response_message([], {"deliveryDate": "tomorrow"}, [], show_only_collected=True)
    assert "tomorrow" in invalid_date


def test_rfq_formatter_truncation_excel_and_optional_branches():
    entities = [{"description": f"item-{i}", "quantity": i + 1} for i in range(7)]
    text = format_rfq_response_message(entities, {}, [], show_only_collected=True)
    assert "and 2 more items" in text
    excel = format_rfq_response_message([], {}, ["more than 50 rows are allowed"], excel_source=True)
    assert "more than 50 rows" in excel
    merged = format_rfq_response_message([], {}, ["merged cells found"])
    assert "merged cells" in merged
    missing_file = format_rfq_response_message([], {}, ["Excel file missing"])
    assert "Excel file missing" in missing_file
    optional = format_rfq_response_message([], {}, ["Optional brand"], include_optional=True)
    assert "Optional brand" in optional
    assert format_rfq_response_message([], {"city": "Pune"}, [], show_only_collected=True).endswith("Pune")
    assert format_simple_missing_fields_message([]) == "Thank you for the information!"
    assert "• Name" in format_simple_missing_fields_message(["Name"])
    assert "50 rows" in format_excel_validation_error_message("50 rows allowed")
    assert "merged cells" in format_excel_validation_error_message("MERGED CELLS")
    assert format_excel_validation_error_message("bad") == "[ERROR] bad"
