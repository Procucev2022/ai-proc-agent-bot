"""
Sectioned RFQ Format Parser

Pure format parsing utilities for sectioned RFQ creation workflow.
NO AI/entity extraction - just regex-based parsing and validation.

This module handles:
- Parsing delivery details from structured format
- Parsing items from structured format
- Generating display formats for user confirmation
- Format validation with detailed error messages
"""

import re
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class FormatValidationError(Exception):
    """Raised when format validation fails"""
    pass


def _extract_delivery_format_section(text: str) -> tuple[str, str]:
    """
    Extract only the delivery format section from user's message.

    This function handles cases where users copy-paste extra text along with the format,
    such as "Got it!", "This is what I have understood:", etc.

    It looks for the first occurrence of delivery-related fields and extracts only that portion.
    Also returns any remaining text that appears after the format (e.g., questions).

    Args:
        text: User's full message

    Returns:
        Tuple of (format_section, remaining_text)
        - format_section: Cleaned text containing only the delivery format section
        - remaining_text: Any text found after the format (may be empty)
    """
    text = text.strip()

    # Find the first line that starts with a delivery field (case-insensitive)
    # Look for patterns like "Delivery Date:", "Date:", etc.
    lines = text.split('\n')
    start_idx = -1
    end_idx = len(lines)

    delivery_field_pattern = re.compile(
        r'^\s*(?:delivery\s+)?(?:date|pincode|city|state)\s*:',
        re.IGNORECASE
    )

    # Find first line with a delivery field
    for i, line in enumerate(lines):
        if delivery_field_pattern.match(line):
            start_idx = i
            break

    # If we found a start, find where delivery fields end
    # Strategy: Look for 4 consecutive delivery field matches (date, pincode, city, state)
    # Everything after is considered additional text
    if start_idx >= 0:
        found_fields = set()
        last_field_idx = start_idx

        for i in range(start_idx, len(lines)):
            line = lines[i].strip()
            if not line:
                continue

            # Check if this line contains a delivery field
            if delivery_field_pattern.match(line):
                # Determine which field this is
                line_lower = line.lower()
                if 'date' in line_lower:
                    found_fields.add('date')
                elif 'pincode' in line_lower:
                    found_fields.add('pincode')
                elif 'city' in line_lower:
                    found_fields.add('city')
                elif 'state' in line_lower:
                    found_fields.add('state')
                last_field_idx = i

                # If we've found all 4 fields, stop here
                if len(found_fields) >= 4:
                    end_idx = i + 1
                    break
            else:
                # Line doesn't match pattern - check if we should continue or stop
                # If we haven't found all fields yet and this might be a value continuation, continue
                # Otherwise, this is where additional text starts
                if len(found_fields) >= 3:  # We have most fields, next non-matching line is end
                    end_idx = i
                    break

        # If we didn't set end_idx in the loop, set it after the last field found
        if end_idx == len(lines) and last_field_idx >= start_idx:
            end_idx = last_field_idx + 1

        # Extract just the delivery format section
        extracted = '\n'.join(lines[start_idx:end_idx])
        # Extract any remaining text after the format
        remaining = '\n'.join(lines[end_idx:]).strip() if end_idx < len(lines) else ''

        logger.info(f"Extracted delivery format section from message (lines {start_idx} to {end_idx})")
        if remaining:
            logger.info(f"Found additional text after format: {remaining[:100]}...")

        return extracted.strip(), remaining

    # If no delivery fields found, return original text (will fail validation later)
    logger.warning("No delivery format fields detected in message")
    return text, ''


def _extract_items_format_section(text: str) -> tuple[str, str]:
    """
    Extract only the items format section from user's message.

    This function handles cases where users copy-paste extra text along with the format,
    such as "Got it!", "I've captured X product(s):", "RFQ Items (N):", etc.

    It looks for the first occurrence of item-related fields and extracts only that portion.
    Also returns any remaining text that appears after the format (e.g., questions).

    Args:
        text: User's full message

    Returns:
        Tuple of (format_section, remaining_text)
        - format_section: Cleaned text containing only the items format section
        - remaining_text: Any text found after the format (may be empty)
    """
    text = text.strip()

    # Find the first line that starts with an item field (case-insensitive)
    # Look for patterns like "Item 1:", "Name:", etc.
    # NOTE: "Item N:" must NOT match header patterns like "RFQ Items (N):" or "Items (N):"
    lines = text.split('\n')
    start_idx = -1
    end_idx = len(lines)

    # Pattern for actual item fields - Item must be followed by space+digit (not "Items")
    # and must NOT have parentheses around the number (to exclude "RFQ Items (1):")
    item_field_pattern = re.compile(
        r'^\s*(?:item\s+\d+|name|qty|quantity|specification|brand|uom)\s*:',
        re.IGNORECASE
    )

    # Pattern to detect header lines like "RFQ Items (N):" or "Items (N):" which should be skipped
    header_pattern = re.compile(
        r'^\s*(?:rfq\s+)?items?\s*\(\d+\)\s*:',
        re.IGNORECASE
    )

    # Find first line with an item field (but skip header lines like "RFQ Items (N):")
    for i, line in enumerate(lines):
        # Skip header lines like "RFQ Items (2):" or "Items (1):"
        if header_pattern.match(line):
            logger.debug(f"Skipping header line: {line}")
            continue
        if item_field_pattern.match(line):
            start_idx = i
            break

    # If we found a start, find where item fields end
    # Strategy: Items are in blocks. Once we hit text that isn't an item field and isn't empty,
    # it's likely additional text (e.g., a question)
    if start_idx >= 0:
        last_item_line = start_idx

        for i in range(start_idx, len(lines)):
            line = lines[i].strip()

            # Check if this line is part of the items format
            if item_field_pattern.match(line):
                last_item_line = i
            elif not line:
                # Empty line - could be item separator, continue
                continue
            else:
                # Non-empty line that doesn't match item pattern
                # Check if it looks like common post-format text
                line_lower = line.lower()
                if any(phrase in line_lower for phrase in [
                    'please reply', 'confirm', 'modify', 'got it',
                    'click', 'to proceed', 'make changes', 'by the way',
                    'what', 'how', 'can you', 'do you', '?'
                ]):
                    # This is clearly additional text
                    end_idx = i
                    break
                # Otherwise, might still be part of specification, continue

        # If we didn't find an explicit end, use the line after the last item field
        if end_idx == len(lines):
            end_idx = last_item_line + 1

        # Extract just the items format section
        extracted = '\n'.join(lines[start_idx:end_idx])
        # Extract any remaining text after the format
        remaining = '\n'.join(lines[end_idx:]).strip() if end_idx < len(lines) else ''

        logger.info(f"Extracted items format section from message (lines {start_idx} to {end_idx})")
        if remaining:
            logger.info(f"Found additional text after format: {remaining[:100]}...")

        return extracted.strip(), remaining

    # If no item fields found, return original text (will fail validation later)
    logger.warning("No item format fields detected in message")
    return text, ''


def parse_delivery_format(text: str) -> Dict[str, Any]:
    """
    Parse delivery format from user input.
    NO entity extraction - just direct value extraction from structured format.

    Expected format:
        Delivery Date: 12 Nov 2025
        Delivery Pincode: 411005
        Delivery City: Pune
        Delivery State: Maharashtra

    Args:
        text: User's message with delivery details in structured format

    Returns:
        Success: {
            "deliveryDate": "12 Nov 2025",
            "pincode": "411005",
            "city": "Pune",
            "state": "Maharashtra",
            "additional_text": "any text found after format (optional)"
        }
        Failure: {
            "error": "Missing required field: Delivery Date",
            "additional_text": "any text found after format (optional)"
        }
    """
    try:
        # Clean up text and extract only the format portion
        text, additional_text = _extract_delivery_format_section(text)

        # Initialize result
        result = {
            "deliveryDate": "",
            "pincode": "",
            "city": "",
            "state": ""
        }

        # Define field patterns (case-insensitive)
        # Updated to handle both multi-line and single-line formats
        # Capture until: newline, OR next field label, OR end of string
        patterns = {
            "deliveryDate": r"(?:delivery\s+)?date\s*:\s*(.+?)(?=\s*(?:delivery\s+)?(?:pincode|city|state)\s*:|$)",
            "pincode": r"(?:delivery\s+)?pincode\s*:\s*(.+?)(?=\s*(?:delivery\s+)?(?:city|state|date)\s*:|$)",
            "city": r"(?:delivery\s+)?city\s*:\s*(.+?)(?=\s*(?:delivery\s+)?(?:state|date|pincode)\s*:|$)",
            "state": r"(?:delivery\s+)?state\s*:\s*(.+?)(?=\s*(?:delivery\s+)?(?:date|pincode|city)\s*:|$)"
        }

        # Extract each field
        for field, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                result[field] = match.group(1).strip()

        # Validate required fields (only date and pincode - city/state are auto-filled from pincode)
        missing_fields = []
        if not result["deliveryDate"]:
            missing_fields.append("Delivery Date")
        if not result["pincode"]:
            missing_fields.append("Delivery Pincode")

        if missing_fields:
            error_msg = f"Missing required field(s): {', '.join(missing_fields)}"
            logger.warning(f"Delivery format validation failed: {error_msg}")
            error_result = {"error": error_msg}
            if additional_text:
                error_result["additional_text"] = additional_text
            return error_result

        # Validate pincode format (6 digits) - strip whitespace first
        pincode_cleaned = result["pincode"].strip()
        if not re.match(r"^\d{6}$", pincode_cleaned):
            error_msg = f"Delivery Pincode must be a 6-digit number (got: '{result['pincode']}')"
            logger.warning(f"Delivery format validation failed: {error_msg}")
            error_result = {"error": error_msg}
            if additional_text:
                error_result["additional_text"] = additional_text
            return error_result
        # Use cleaned pincode
        result["pincode"] = pincode_cleaned

        # Add additional text if present
        if additional_text:
            result["additional_text"] = additional_text

        logger.info("Delivery format parsed successfully")
        return result

    except Exception as e:
        logger.error(f"Error parsing delivery format: {str(e)}")
        return {"error": f"Invalid format. Please follow the exact format shown."}


def parse_items_format(text: str) -> Dict[str, Any]:
    """
    Parse items format from user input.
    NO entity extraction - just direct value extraction from structured format.

    Expected format:
        Item 1: Water bottle
        Qty: 100
        Specification: Cello, M34567, 1000 ml

        Item 2: Motor
        Qty: 12
        Specification: Exide Red Color

    Args:
        text: User's message with items in structured format

    Returns:
        Success: {
            "items": [
                {
                    "description": "Water bottle",
                    "quantity": 100,
                    "brand": "",
                    "remarks": "Cello, M34567, 1000 ml",
                    "unitofMeasures": ""
                },
                ...
            ],
            "additional_text": "any text found after format (optional)"
        }
        Failure: {
            "error": "Item 2 missing required field: Qty",
            "additional_text": "any text found after format (optional)"
        }
    """
    try:
        # Clean up text and extract only the format portion
        text, additional_text = _extract_items_format_section(text)

        # Split by double newlines or "Item N:" pattern to separate items
        # First, split by blank lines
        item_blocks = re.split(r'\n\s*\n', text)

        # If no blank lines found, try to split by "Item N:" pattern
        if len(item_blocks) == 1:
            item_blocks = re.split(r'(?=Item\s+\d+\s*:)', text, flags=re.IGNORECASE)
            item_blocks = [block.strip() for block in item_blocks if block.strip()]

        if not item_blocks:
            return {"error": "No items found. Please provide at least one item."}

        items = []
        item_number = 0  # Track actual item number (increment only for valid blocks)

        for block in item_blocks:
            block = block.strip()
            if not block:
                continue

            # Skip blocks that look like headers (e.g., "RFQ Items (2):" that weren't filtered earlier)
            if re.match(r'^\s*(?:rfq\s+)?items?\s*\(\d+\)\s*:?\s*$', block, re.IGNORECASE):
                logger.debug(f"Skipping header block: {block}")
                continue

            item_number += 1  # Increment for each valid item block

            # Parse individual item
            item = {
                "description": "",
                "quantity": None,
                "brand": "",
                "remarks": "",
                "unitofMeasures": ""
            }

            # Extract item name/description (Item N: VALUE or Name: VALUE)
            # Stop at newline OR next field label (Qty, Brand, Specification, UoM)
            name_match = re.search(r'(?:Item\s+\d+|Name)\s*:\s*(.+?)(?=\s*(?:Qty|Brand|Specification|UoM)\s*:|\n|$)', block, re.IGNORECASE)
            if name_match:
                description = name_match.group(1).strip()
                # Validate that description doesn't contain field labels (indicates missing value)
                if not re.match(r'^\s*(?:Qty|Brand|Specification|UoM)\s*:', description, re.IGNORECASE):
                    item["description"] = description

            # Extract quantity
            qty_match = re.search(r'Qty\s*:\s*(\d+(?:\.\d+)?)', block, re.IGNORECASE)
            if qty_match:
                try:
                    item["quantity"] = float(qty_match.group(1))
                except ValueError:
                    pass

            # Extract brand (optional) - stop at next field or newline
            brand_match = re.search(r'Brand\s*:\s*(.+?)(?=\s*(?:Qty|Specification|UoM|Item\s+\d+)\s*:|\n|$)', block, re.IGNORECASE)
            if brand_match:
                item["brand"] = brand_match.group(1).strip()

            # Extract specification/remarks (optional) - stop at next item or end
            spec_match = re.search(r'Specification\s*:\s*(.+?)(?=\s*(?:Item\s+\d+)\s*:|$)', block, re.IGNORECASE)
            if spec_match:
                item["remarks"] = spec_match.group(1).strip()

            # Extract unit of measures (optional) - stop at next field or newline
            uom_match = re.search(r'UoM\s*:\s*(.+?)(?=\s*(?:Item\s+\d+|Brand|Specification)\s*:|\n|$)', block, re.IGNORECASE)
            if uom_match:
                item["unitofMeasures"] = uom_match.group(1).strip()

            # Validate required fields for this item
            missing_fields = []
            if not item["description"]:
                missing_fields.append("Item name/description")
            if item["quantity"] is None:
                missing_fields.append("Qty")

            if missing_fields:
                error_msg = f"Item {item_number} missing required field(s): {', '.join(missing_fields)}"
                logger.warning(f"Items format validation failed: {error_msg}")
                error_result = {"error": error_msg}
                if additional_text:
                    error_result["additional_text"] = additional_text
                return error_result

            items.append(item)

        if not items:
            error_result = {"error": "No valid items found. Please provide at least one item with name and quantity."}
            if additional_text:
                error_result["additional_text"] = additional_text
            return error_result

        logger.info(f"Items format parsed successfully: {len(items)} items")
        result = {"items": items}
        if additional_text:
            result["additional_text"] = additional_text
        return result

    except Exception as e:
        logger.error(f"Error parsing items format: {str(e)}")
        return {"error": "Invalid format. Please follow the exact format shown."}


def generate_delivery_display(data: Dict[str, str]) -> str:
    """
    Generate display format for delivery details.
    Note: City and State are stored internally but not shown to user.

    Args:
        data: Dictionary with deliveryDate, pincode, city, state

    Returns:
        Formatted string for display (only Date and Pincode shown)
    """
    return f"""Delivery Date: {data.get('deliveryDate', '')}
Delivery Pincode: {data.get('pincode', '')}"""


def generate_delivery_display_with_missing(data: Dict[str, str]) -> tuple[str, list[str]]:
    """
    Generate display format for delivery details with missing field indicators.
    Note: City and State are stored internally but not shown to user.

    Args:
        data: Dictionary with deliveryDate, pincode, city, state (some may be missing)

    Returns:
        Tuple of (formatted_string, list_of_missing_fields) - only Date and Pincode shown
    """
    missing_fields = []

    # Check and format each field
    date_value = data.get('deliveryDate', '').strip() if data.get('deliveryDate') else ''
    pincode_value = data.get('pincode', '').strip() if data.get('pincode') else ''

    if not date_value:
        date_value = "[Please provide delivery date]"
        missing_fields.append("Delivery Date")

    if not pincode_value:
        pincode_value = "[Please provide 6-digit pincode]"
        missing_fields.append("Delivery Pincode")

    # City and state are auto-filled from pincode but not shown to user
    display = f"""Delivery Date: {date_value}
Delivery Pincode: {pincode_value}"""

    return display, missing_fields


def _format_quantity(qty) -> str:
    """Format quantity as integer if it's a whole number, otherwise as-is."""
    if qty is None or qty == '':
        return ''
    try:
        qty_float = float(qty)
        # If it's a whole number, display as integer
        if qty_float == int(qty_float):
            return str(int(qty_float))
        return str(qty)
    except (ValueError, TypeError):
        return str(qty)


MAX_DISPLAY_ITEMS = 5  # Maximum items to display before truncating
MAX_DESCRIPTION_LENGTH = 40  # Truncate item descriptions
MAX_SPECIFICATION_LENGTH = 70  # Truncate specifications (brand + remarks)
MAX_MESSAGE_LENGTH = 900  # Reserve ~124 chars for header/buttons overhead


def _sanitize_text(text: str) -> str:
    """Remove null bytes and control characters (except newlines/tabs)."""
    if not text or not isinstance(text, str):
        return text or ''
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)


def _truncate_text(text: str, max_length: int) -> str:
    """Sanitize and truncate text with ellipsis if it exceeds max_length."""
    text = _sanitize_text(text)
    if not text or len(text) <= max_length:
        return text
    return text[:max_length - 3].rstrip() + "..."


def generate_items_display(products: List[Dict[str, Any]]) -> str:
    """
    Generate display format for items.
    Truncates to MAX_DISPLAY_ITEMS and shows "+n items" for the rest.
    Also truncates long descriptions/specifications and respects MAX_MESSAGE_LENGTH.

    Args:
        products: List of product dictionaries

    Returns:
        Formatted string for display
    """
    if not products:
        return "No items"

    total_items = len(products)
    display_count = min(total_items, MAX_DISPLAY_ITEMS)

    # Try with current display_count, reduce if message too long
    while display_count > 0:
        result = []

        for idx, item in enumerate(products[:display_count], 1):
            description = _truncate_text(item.get('description', ''), MAX_DESCRIPTION_LENGTH)
            result.append(f"Item {idx}: {description}")
            result.append(f"Qty: {_format_quantity(item.get('quantity', ''))}")

            # Combine brand and remarks into specification
            spec_parts = []
            if item.get('brand'):
                spec_parts.append(item['brand'])
            if item.get('remarks'):
                spec_parts.append(item['remarks'])

            specification = ', '.join(spec_parts) if spec_parts else ''
            specification = _truncate_text(specification, MAX_SPECIFICATION_LENGTH)
            result.append(f"Specification: {specification}")

            # Add blank line separator between items (except after last displayed item)
            if idx < display_count:
                result.append("")

        # Add "+n items" indicator if truncated
        if total_items > display_count:
            remaining = total_items - display_count
            result.append("")
            result.append(f"+{remaining} more item{'s' if remaining > 1 else ''}")

        output = "\n".join(result)

        # Check if within limit
        if len(output) <= MAX_MESSAGE_LENGTH:
            return output

        # Too long, reduce display count and retry
        display_count -= 1

    # Fallback: show minimal info
    return f"{total_items} items (details too long to display)"


def generate_items_display_with_missing(products: List[Dict[str, Any]], incomplete_items: List[Dict]) -> tuple[str, list[str]]:
    """
    Generate display format for items with missing field indicators.
    Truncates to MAX_DISPLAY_ITEMS and shows "+n items" for the rest.
    Also truncates long descriptions/specifications and respects MAX_MESSAGE_LENGTH.

    Args:
        products: List of product dictionaries
        incomplete_items: List of dicts with format: {"index": int, "item": dict, "missing_fields": list}

    Returns:
        Tuple of (formatted_string, list_of_missing_field_labels)
    """
    if not products:
        return "No items", []

    # Build a map of item index to missing fields
    missing_map = {}
    for incomplete in incomplete_items:
        missing_map[incomplete["index"]] = incomplete["missing_fields"]

    total_items = len(products)
    display_count = min(total_items, MAX_DISPLAY_ITEMS)

    # Try with current display_count, reduce if message too long
    while display_count > 0:
        result = []
        all_missing_labels = []

        for idx, item in enumerate(products[:display_count], 1):
            missing_fields = missing_map.get(idx, [])

            # Description
            description = item.get('description', '')
            if not description or 'description' in missing_fields:
                description = "[Please provide product name]"
                if "Product Name" not in all_missing_labels:
                    all_missing_labels.append("Product Name")
            else:
                description = _truncate_text(description, MAX_DESCRIPTION_LENGTH)
            result.append(f"Item {idx}: {description}")

            # Quantity
            quantity = item.get('quantity', '')
            if not quantity or 'quantity' in missing_fields:
                quantity = "[Please provide quantity]"
                if "Quantity" not in all_missing_labels:
                    all_missing_labels.append("Quantity")
            else:
                quantity = _format_quantity(quantity)
            result.append(f"Qty: {quantity}")

            # Combine brand and remarks into specification
            spec_parts = []
            if item.get('brand'):
                spec_parts.append(item['brand'])
            if item.get('remarks'):
                spec_parts.append(item['remarks'])

            specification = ', '.join(spec_parts) if spec_parts else ''
            specification = _truncate_text(specification, MAX_SPECIFICATION_LENGTH)
            result.append(f"Specification: {specification}")

            # Add blank line separator between items (except after last displayed item)
            if idx < display_count:
                result.append("")

        # Add "+n items" indicator if truncated
        if total_items > display_count:
            remaining = total_items - display_count
            result.append("")
            result.append(f"+{remaining} more item{'s' if remaining > 1 else ''}")

        output = "\n".join(result)

        # Check if within limit
        if len(output) <= MAX_MESSAGE_LENGTH:
            return output, all_missing_labels

        # Too long, reduce display count and retry
        display_count -= 1

    # Fallback: show minimal info
    return f"{total_items} items (details too long to display)", []


def generate_delivery_display_with_invalid_pincode(data: Dict[str, str]) -> str:
    """
    Generate display format for delivery details when pincode is invalid (lookup failed).
    Shows the date as-is but replaces pincode with placeholder.

    Args:
        data: Dictionary with deliveryDate, pincode, city, state

    Returns:
        Formatted string for display with pincode placeholder
    """
    date_value = data.get('deliveryDate', '').strip() if data.get('deliveryDate') else ''

    display = f"""Delivery Date: {date_value}
Delivery Pincode: [Valid 6-digit pin-code]"""

    return display


def validate_delivery_completeness(data: Dict[str, str]) -> bool:
    """
    Check if delivery data has all required fields.

    Args:
        data: Delivery data dictionary

    Returns:
        True if all required fields present, False otherwise
    """
    if not data:
        return False

    required_fields = ["deliveryDate", "pincode", "city", "state"]
    return all(data.get(field) for field in required_fields)


def validate_items_completeness(items: List[Dict[str, Any]]) -> bool:
    """
    Check if items list has at least one complete item.

    Args:
        items: List of item dictionaries

    Returns:
        True if at least one complete item exists, False otherwise
    """
    if not items:
        return False

    for item in items:
        if item.get("description") and item.get("quantity"):
            return True

    return False
