"""
BFS Bid Format Parser

Handles parsing and generation of bid format for BFS Place Bid workflow.
Similar pattern to sectioned_rfq_format_parser.py.

Format: "Description - Specification : Price" (no indices)
Matching: Case-insensitive match on "Description - Specification"
"""

import re
from typing import Dict, List, Any, Tuple
import logging

logger = logging.getLogger(__name__)

MAX_BID_RETRY_ATTEMPTS = 3


def _normalize_bid_text_newlines(text: str) -> str:
    """
    Normalize text by inserting newlines where WhatsApp may have stripped them.

    WhatsApp sometimes strips newlines when copy-pasting, resulting in:
    "1. Laptop -- Age: 2:    a. Price: 6000    b. Qty: 10  2. Dell Laptops..."

    This function detects boundaries and inserts newlines:
    - Before "a. Price:"
    - Before "b. Qty:"
    - Before item numbers like "2.", "3.", etc.

    Args:
        text: Input text that may have stripped newlines

    Returns:
        Text with newlines inserted at proper boundaries
    """
    normalized = text

    # Insert newline before "a. Price:" (with optional spaces)
    normalized = re.sub(
        r'\s+(a\.\s*Price:)',
        r'\n   \1',
        normalized,
        flags=re.IGNORECASE
    )

    # Insert newline before "b. Qty:" (with optional spaces)
    normalized = re.sub(
        r'\s+(b\.\s*Qty:)',
        r'\n   \1',
        normalized,
        flags=re.IGNORECASE
    )

    # Insert newline before item numbers (2., 3., 4., etc.) that follow a quantity
    # Pattern: "Qty: <number>" followed by spaces and a new item number
    normalized = re.sub(
        r'(Qty:\s*\d+)\s+(\d+\.)',
        r'\1\n\n\2',
        normalized,
        flags=re.IGNORECASE
    )

    if normalized != text:
        logger.info(f"Normalized stripped newlines in bid text")
        logger.debug(f"Original: {text[:100]}...")
        logger.debug(f"Normalized: {normalized[:100]}...")

    return normalized


def _extract_bid_format_section(text: str) -> Tuple[str, str]:
    """
    Extract only the bid format section from user's message.

    This function handles cases where users copy-paste extra text along with the format,
    such as "Place your bids:", header text, footer instructions, etc.

    It looks for lines that match the bid format pattern: "Product - Spec : Price"

    Args:
        text: User's full message

    Returns:
        Tuple of (format_section, remaining_text)
        - format_section: Cleaned text containing only the bid format lines
        - remaining_text: Any text found after the format (may be empty)
    """
    text = text.strip()

    # Pattern to match bid format lines: "Something : number" or "Something - Something : number"
    bid_line_pattern = re.compile(
        r'^.+\s*:\s*[\d,]+(?:\.\d+)?\s*$'
    )

    # Pattern to detect header/instruction lines that should be skipped
    header_patterns = [
        r'^\s*\*?place\s+your\s+bids',
        r'^\s*\*?place\s+bid\*?\s*$',  # Matches "*Place Bid*" header
        r'^\s*\*?modify\s+the\s+prices',
        r'^\s*_?copy\s+the\s+format',
        r'^\s*_?remove\s+any\s+items',
        r'^\s*\*?stock\s+available',
        r'^\s*\*?verify\s+your\s+bids',
    ]
    header_pattern = re.compile('|'.join(header_patterns), re.IGNORECASE)

    lines = text.split('\n')
    bid_lines = []
    remaining_lines = []
    found_bid_section = False

    for line in lines:
        line_stripped = line.strip()

        # Skip empty lines
        if not line_stripped:
            continue

        # Skip header/instruction lines
        if header_pattern.search(line_stripped):
            logger.debug(f"Skipping header line: {line_stripped}")
            continue

        # Skip lines starting with underscore (WhatsApp italics instruction)
        if line_stripped.startswith('_'):
            logger.debug(f"Skipping instruction line: {line_stripped}")
            continue

        # Check if this line matches bid format
        if bid_line_pattern.match(line_stripped):
            bid_lines.append(line_stripped)
            found_bid_section = True
        elif found_bid_section:
            # We've passed the bid section, this is remaining text
            remaining_lines.append(line_stripped)

    if bid_lines:
        logger.info(f"Extracted {len(bid_lines)} bid format lines from message")
        return '\n'.join(bid_lines), '\n'.join(remaining_lines)

    # If no bid lines found, return original text (will fail validation later)
    logger.warning("No bid format lines detected in message")
    return text, ''


def _build_item_key(item: Dict) -> str:
    """
    Build unique key from description + specification (truncated for display/matching).

    Args:
        item: BFS item dictionary with description and specification

    Returns:
        Combined key like "Dell XPS 13 -- Intel i7" or just "Dell XPS 13" if no spec
    """
    # Same limits as generate_bid_format() for consistent matching
    DESC_LIMIT = 50
    SPEC_LIMIT = 20

    desc = (item.get("description") or "")[:DESC_LIMIT].strip()
    spec = (item.get("specification") or "")[:SPEC_LIMIT].strip()
    if spec:
        return f"{desc} -- {spec}"
    return desc


def generate_bid_format(bfs_items: List[Dict]) -> str:
    """
    Generate editable bid format from BFS search results.
    Multi-line format with description, specification, age, price, and quantity.

    Args:
        bfs_items: List of BFS items with description, specification, sellPrice,
                   ageOfAsset, availableQuantity

    Returns:
        Formatted string like:
        1. Dell XPS 13 -- Intel i7 -- Age: 2yr:
           a. Price: 85000
           b. Qty: 10

        2. HP Pavilion -- AMD Ryzen -- Age: 1yr:
           a. Price: 70000
           b. Qty: 5
    """
    # Character limits for display
    DESC_LIMIT = 50
    SPEC_LIMIT = 20
    AGE_LIMIT = 30

    blocks = []
    for idx, item in enumerate(bfs_items, start=1):
        desc = (item.get("description") or "")[:DESC_LIMIT].strip()
        spec = (item.get("specification") or "")[:SPEC_LIMIT].strip()
        age = (item.get("ageOfAsset") or "-")[:AGE_LIMIT].strip()
        price = round(item.get("sellPrice") or 0)
        qty = int(item.get("availableQuantity") or 1)

        # Build key with truncated values
        if spec:
            key = f"{desc} -- {spec}"
        else:
            key = desc

        block = (
            f"{idx}. {key} -- Age: {age}:\n"
            f"   a. Price: {price}\n"
            f"   b. Qty: {qty}"
        )
        blocks.append(block)
    return "\n\n".join(blocks)


def parse_bid_format(text: str, original_items: List[Dict]) -> Dict[str, Any]:
    """
    Parse user's bid response in multi-line format and validate against original items.
    Match by "Description -- Specification" (case-insensitive).

    Expected format:
        1. Dell XPS 13 -- Intel i7 -- Age: 2yr:
           a. Price: 85000
           b. Qty: 10

    Args:
        text: User's message with bid format
        original_items: Original BFS items for validation

    Returns:
        Success: {"bids": [{"key": "...", "price": 4800, "quantity": 10, "original_item": {...}}, ...]}
        Failure: {"error": "Error message"}
    """
    text = text.strip()
    if not text:
        return {"error": "No bid items provided. Please copy the format and modify prices/quantities."}

    # Normalize newlines (handles WhatsApp stripping newlines on copy-paste)
    text = _normalize_bid_text_newlines(text)
    logger.debug(f"Normalized bid text: {text[:200]}...")

    # Build lookup for original items by key (case-insensitive)
    original_lookup = {}
    for item in original_items:
        key = _build_item_key(item).lower()
        original_lookup[key] = item

    # Split into blocks (separated by double newlines or by numbered items)
    blocks = re.split(r'\n(?=\d+\.)', text)
    blocks = [b.strip() for b in blocks if b.strip()]

    if not blocks:
        return {"error": "No bid items found. Please use the provided format."}

    bids = []

    # Patterns for parsing
    # Header: "1. Description -- Specification -- Age: 2yr:" or "1. Description -- Age: 2yr:"
    header_pattern = r'^(\d+)\.\s*(.+?)\s*--\s*Age:\s*[^:]*:\s*$'
    # Price line: "a. Price: 85000" or "a.Price:85000"
    price_pattern = r'^\s*a\.\s*Price:\s*([\d,]+(?:\.\d+)?)\s*$'
    # Qty line: "b. Qty: 10" or "b.Qty:10"
    qty_pattern = r'^\s*b\.\s*Qty:\s*(\d+)\s*$'

    for block in blocks:
        lines = block.strip().split('\n')
        if not lines:
            continue

        # Parse header line
        header_line = lines[0].strip()
        header_match = re.match(header_pattern, header_line, re.IGNORECASE)

        if not header_match:
            # Try alternative: maybe age is on same line differently
            # Pattern: "1. Description -- Spec -- Age: 2yr:"
            alt_pattern = r'^(\d+)\.\s*(.+?)(?:\s*--\s*Age:[^:]*)?:\s*$'
            header_match = re.match(alt_pattern, header_line, re.IGNORECASE)
            if not header_match:
                return {"error": f"Invalid header format: '{header_line}'\nExpected: 1. Product -- Spec -- Age: Xyr:"}

        item_key_with_age = header_match.group(2).strip()
        # Remove the "-- Age: ..." part to get the item key
        item_key = re.sub(r'\s*--\s*Age:.*$', '', item_key_with_age, flags=re.IGNORECASE).strip()

        # Find price and qty lines
        price = None
        quantity = None

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue

            price_match = re.match(price_pattern, line, re.IGNORECASE)
            if price_match:
                price_str = price_match.group(1).replace(',', '')
                try:
                    price = float(price_str)
                except ValueError:
                    return {"error": f"Invalid price '{price_str}' for '{item_key}'."}
                continue

            qty_match = re.match(qty_pattern, line, re.IGNORECASE)
            if qty_match:
                try:
                    quantity = int(qty_match.group(1))
                except ValueError:
                    return {"error": f"Invalid quantity for '{item_key}'."}
                continue

        # Validate required fields
        if price is None:
            return {"error": f"Missing price for '{item_key}'.\nExpected: a. Price: <amount>"}

        if quantity is None:
            return {"error": f"Missing quantity for '{item_key}'.\nExpected: b. Qty: <number>"}

        if price <= 0:
            return {"error": f"Invalid price for '{item_key}'. Price must be greater than 0."}

        if quantity <= 0:
            return {"error": f"Invalid quantity for '{item_key}'. Quantity must be greater than 0."}

        # Validate item exists in original list
        lookup_key = item_key.lower()
        if lookup_key not in original_lookup:
            return {"error": f"Item '{item_key}' not found in original list.\nPlease use the exact name from the format."}

        original_item = original_lookup[lookup_key]

        # Validate quantity doesn't exceed available
        available_qty = int(original_item.get("availableQuantity") or 1)
        if quantity > available_qty:
            return {"error": f"Quantity {quantity} exceeds available stock ({available_qty}) for '{item_key}'."}

        bids.append({
            "key": item_key,
            "price": price,
            "quantity": quantity,
            "original_item": original_item
        })

    if not bids:
        return {"error": "No valid bid items found."}

    logger.info(f"[BFS Bid] Parsed {len(bids)} bid items successfully")
    return {"bids": bids}


def generate_bid_summary(bid_items: List[Dict]) -> str:
    """
    Generate bid summary for OTP message.
    Uses same format as bid input for consistency.

    Args:
        bid_items: List of parsed bid items with price, quantity, and original_item

    Returns:
        Formatted summary string matching bid format
    """
    # Same limits as generate_bid_format()
    AGE_LIMIT = 30

    blocks = []
    for idx, bid in enumerate(bid_items, start=1):
        key = bid["key"]
        price = bid["price"]
        qty = bid.get("quantity")
        if not qty:
            logger.warning(f"[BFS Bid] Missing quantity for bid: {key}")
            continue
        original_item = bid.get("original_item", {})
        age = (original_item.get("ageOfAsset") or "-")[:AGE_LIMIT].strip()

        block = (
            f"{idx}. {key} -- Age: {age}:\n"
            f"   a. Price: ₹{price:,.0f}\n"
            f"   b. Qty: {qty}"
        )
        blocks.append(block)
    return "\n\n".join(blocks)
