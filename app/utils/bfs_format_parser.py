"""
BFS Format Parser

Simple format parsing for BFS stock search workflow.
"""

import re
import logging
from typing import Dict, List, Any, Tuple

logger = logging.getLogger(__name__)


def parse_bfs_format(text: str) -> Dict[str, Any]:
    """
    Parse BFS product format from user input.

    Expected format (simplified - only description required):
        Product 1: Laptop
        Product 2: Monitor

    Returns:
        Success: {"products": [...]}
        Failure: {"error": "error message"}
    """
    try:
        text = text.strip()

        # Split by double newlines or "Product N:" pattern
        blocks = re.split(r'\n\s*\n', text)

        if len(blocks) == 1:
            blocks = re.split(r'(?=Product\s+\d+\s*:)', text, flags=re.IGNORECASE)
            blocks = [b.strip() for b in blocks if b.strip()]

        if not blocks:
            return {"error": "No products found"}

        products = []

        for idx, block in enumerate(blocks, 1):
            block = block.strip()
            if not block:
                continue

            product = {
                "description": ""
            }

            # Extract product name
            name_match = re.search(r'Product\s*\d*\s*:\s*(.+?)(?=\n|$)', block, re.IGNORECASE)
            if name_match:
                product["description"] = name_match.group(1).strip()

            # Validate required fields - only description is required
            if not product["description"]:
                return {"error": f"Product {idx} missing: Product name"}

            products.append(product)

        if not products:
            return {"error": "No valid products found"}

        return {"products": products}

    except Exception as e:
        logger.error(f"Error parsing BFS format: {e}")
        return {"error": "Invalid format"}


def generate_bfs_display(products: List[Dict]) -> str:
    """Generate display format for BFS products (description only)."""
    if not products:
        return "No products"

    lines = []
    for idx, p in enumerate(products, 1):
        lines.append(f"Product {idx}: {p.get('description', '')}")
        if idx < len(products):
            lines.append("")

    return "\n".join(lines)


def generate_bfs_display_with_missing(products: List[Dict]) -> Tuple[str, List[str]]:
    """Generate display format with missing field placeholders (description only)."""
    if not products:
        return "No products", []

    missing_labels = []
    lines = []

    for idx, p in enumerate(products, 1):
        desc = p.get('description', '')

        if not desc:
            desc = "[Please provide product name]"
            if "Product name" not in missing_labels:
                missing_labels.append("Product name")

        lines.append(f"Product {idx}: {desc}")
        if idx < len(products):
            lines.append("")

    return "\n".join(lines), missing_labels


def is_bfs_complete(products: List[Dict]) -> bool:
    """Check if all products have required fields (only description required)."""
    if not products:
        return False

    for p in products:
        if not p.get("description"):
            return False

    return True
