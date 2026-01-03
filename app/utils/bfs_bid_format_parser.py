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
    "Dell XPS 13 - Intel i7 : 85000 HP Pavilion - AMD Ryzen : 55000"

    This function detects boundaries (price followed by product name) and inserts newlines.
    Pattern: number followed by space and capital letter (start of next product)

    Args:
        text: Input text that may have stripped newlines

    Returns:
        Text with newlines inserted at item boundaries
    """
    # Pattern: digit(s) followed by space(s) and then a capital letter
    # This indicates: end of price, start of next product name
    # Replace with: digit + newline + capital letter
    normalized = re.sub(
        r'(\d)\s+([A-Z])',
        r'\1\n\2',
        text
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
    Build unique key from description + specification.

    Args:
        item: BFS item dictionary with description and specification

    Returns:
        Combined key like "Dell XPS 13 - Intel i7" or just "Dell XPS 13" if no spec
    """
    desc = (item.get("description") or "").strip()
    spec = (item.get("specification") or "").strip()
    if spec:
        return f"{desc} - {spec}"
    return desc


def generate_bid_format(bfs_items: List[Dict]) -> str:
    """
    Generate editable bid format from BFS search results.
    Format: Description - Specification : Price (no indices)

    Args:
        bfs_items: List of BFS items with description, specification, sellPrice

    Returns:
        Formatted string like:
        Dell XPS 13 - Intel i7, 16GB RAM : 85000
        HP Pavilion - AMD Ryzen : 70000
    """
    lines = []
    for item in bfs_items:
        key = _build_item_key(item)
        price = int(item.get("sellPrice") or 0)
        lines.append(f"{key} : {price}")
    return "\n".join(lines)


def parse_bid_format(text: str, original_items: List[Dict]) -> Dict[str, Any]:
    """
    Parse user's bid response and validate against original items.
    Match by "Description - Specification" (case-insensitive).

    Args:
        text: User's message with bid format
        original_items: Original BFS items for validation

    Returns:
        Success: {"bids": [{"key": "...", "price": 4800, "original_item": {...}}, ...]}
        Failure: {"error": "Error message"}
    """
    text = text.strip()
    if not text:
        return {"error": "No bid items provided. Please copy the format and modify prices."}

    # Normalize newlines (handles WhatsApp stripping newlines on copy-paste)
    text = _normalize_bid_text_newlines(text)

    # Extract only the bid format section (handles copy-paste with extra text)
    text, additional_text = _extract_bid_format_section(text)
    logger.debug(f"Extracted bid format text: {text[:100]}...")
    if additional_text:
        logger.debug(f"Additional text after format: {additional_text[:50]}...")

    # Build lookup for original items by key (case-insensitive)
    original_lookup = {}
    for item in original_items:
        key = _build_item_key(item).lower()
        original_lookup[key] = item

    # Parse each line: "Name - Spec : Price"
    pattern = r'^(.+?)\s*:\s*([\d,]+(?:\.\d+)?)\s*$'
    bids = []

    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue

        match = re.match(pattern, line)
        if not match:
            return {"error": f"Invalid format: '{line}'\nExpected: Product Name - Spec : Price"}

        item_key = match.group(1).strip()
        price_str = match.group(2).replace(',', '')

        # Validate item exists in original list
        lookup_key = item_key.lower()
        if lookup_key not in original_lookup:
            return {"error": f"Item '{item_key}' not found in original list.\nPlease use the exact name from the format."}

        # Validate price
        try:
            price = float(price_str)
            if price <= 0:
                return {"error": f"Invalid price for '{item_key}'. Price must be greater than 0."}
        except ValueError:
            return {"error": f"Invalid price '{price_str}' for '{item_key}'."}

        bids.append({
            "key": item_key,
            "price": price,
            "original_item": original_lookup[lookup_key]
        })

    if not bids:
        return {"error": "No valid bid items found."}

    logger.info(f"[BFS Bid] Parsed {len(bids)} bid items successfully")
    return {"bids": bids}


def generate_bid_summary(bid_items: List[Dict]) -> str:
    """
    Generate bid summary for OTP message.

    Args:
        bid_items: List of parsed bid items with price and original_item

    Returns:
        Formatted summary string
    """
    lines = []
    for bid in bid_items:
        key = bid["key"][:40]
        new_price = bid["price"]
        lines.append(f"{key} : ₹{new_price:,.0f}")
    return "\n".join(lines)
